"""Three-way package updates and explicit local removal candidates."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil
import tomllib
from uuid import uuid4

from .catalog import safe_path
from .errors import MooError
from .installation import Store, normalized_spec, spec_for
from .packages import Package
from .storage import atomic_write, json_text, read_json


def merge(base, local, new):
    if local == new:
        return local, False
    if local == base:
        return new, False
    if new == base:
        return local, False
    return local, True


def expected_files(store):
    paths = [*store.desired_paths(), store.world.dir / "world.toml", store.path]
    return {str(p.relative_to(store.world.dir)): p.read_bytes().hex() if p.exists() else None for p in paths}


def _check_expected(store, expected):
    if expected_files(store) != expected:
        raise MooError("active world changed since staging; abort and rebuild the update candidate")


def _check_requirements(store, packages, lock):
    for consumer, item in lock["instances"].items():
        if item["state"] != "prepared":
            continue
        source = packages.get(consumer) or store.load_source(consumer)
        selections = item["spec"]["dependencies"]
        if set(selections) != set(source.dependencies):
            raise MooError(f"{consumer}: missing or unexpected dependency selections")
        pins = {}
        for alias, requirement in source.dependencies.items():
            target = next((n for n in lock["instances"] if n.lower() == selections[alias]), None)
            provider = lock["instances"].get(target)
            if (provider is None or provider["state"] != "prepared"
                    or provider["package"].lower() != requirement["package"].lower()
                    or provider["version"] != requirement["version"]
                    or requirement.get("digest", provider["digest"]) != provider["digest"]):
                raise MooError(f"{consumer}: incompatible provider for {alias}; update compatible consumers together or select a separate instance")
            pins[alias] = {"instance": target, "package": provider["package"], "version": provider["version"], "digest": provider["digest"]}
        item["dependencies"] = pins


def prepare(store: Store, requested: list[str], *, source_keys=None, allow_replacements=False):
    store.validate(check_specs=False)
    store.record_moves()
    names = list(dict.fromkeys(store.name(n) for n in requested))
    for name in names:
        if store.instances[name]["state"] != "prepared":
            raise MooError(f"{name}: instance is not active")
    expected = expected_files(store)
    if source_keys:
        for name, mapping in source_keys.items():
            objects = store.instances[name]["objects"]
            if (not set(mapping) <= set(objects) or len(set(mapping.values())) != len(mapping)
                    or set(mapping.values()) & (set(objects) - set(mapping))):
                raise MooError("source-key mapping must have existing sources and distinct unoccupied targets")
            store.instances[name]["objects"] = {mapping.get(k, k): r for k, r in objects.items()}
    lock, config = deepcopy(store.lock), deepcopy(store.config)
    packages, specs = {}, {}
    for name in names:
        _, declaration = spec_for(config, name)
        spec = normalized_spec(name, declaration)
        if spec["namespace"] != store.instances[name]["spec"]["namespace"]:
            raise MooError(f"{name}: namespace changes require package migrate")
        source = Package.load((store.world.dir / spec["source"]).absolute())
        if source.name.lower() != store.instances[name]["package"].lower():
            raise MooError(f"{name}: update cannot replace the source package identity")
        previous_keys = set(store.instances[name]["objects"])
        if not allow_replacements and previous_keys - set(source.objects) and set(source.objects) - previous_keys:
            raise MooError(f"{name}: source keys were both added and removed; use package migrate --source-keys for identity-preserving renames, or update --allow-replacements to accept distinct removals and additions")
        packages[name], specs[name] = source, spec
        if source_keys and not set(source_keys.get(name, {}).values()) <= set(source.objects):
            raise MooError("new source keys must exist in the configured package source")
        lock["instances"][name].update(package=source.name, version=source.version, digest=source.digest, spec=spec)
    _check_requirements(store, packages, lock)
    graph = store.validate(check_specs=False)
    providers = dict(graph.membership)
    for name, source in packages.items():
        old = store.instances[name]
        mapping = {k: r["key"] for k, r in old["objects"].items()}
        mapping.update({r["source"]: r["key"] for r in old["tombstones"].values() if "source" in r})
        for key, installed in source.mapping(specs[name]["namespace"], mapping).items():
            if installed.lower() in providers and not (providers[installed.lower()] or "").startswith(name.lower() + "/"):
                raise MooError(f"{name}: occupied key {installed}")
            providers[installed.lower()] = f"{name}/{source.membership[key]}".lower()
    changes, conflicts = {}, {}
    for name, source in packages.items():
        item = lock["instances"][name]
        old = store.instances[name]
        mapping = {k: r["key"] for k, r in old["objects"].items()}
        mapping.update({r["source"]: r["key"] for r in old["tombstones"].values() if "source" in r})
        compiled = source.compile(name, specs[name]["namespace"], specs[name]["bindings"], providers,
                                  specs[name]["dependencies"], previous=mapping)
        objects, manifests = {}, {}
        for source_key in sorted(set(old["objects"]) | set(compiled.objects)):
            previous = old["objects"].get(source_key)
            new = compiled.objects.get(source_key)
            if source_key in compiled.objects:
                record = {"key": compiled.mapping[source_key], "path": compiled.paths[source_key],
                          "module": compiled.membership[source_key], "baseline": new}
                # Same-module local moves remain local preferences.
                if previous and previous["module"] == record["module"]:
                    record["path"] = previous["path"]
            else:
                record = {**previous, "baseline": None}
            base = previous["baseline"] if previous else None
            old_path = store.object_path(name, previous["path"]) if previous else None
            local = old_path.read_text() if old_path else None
            value, conflict = merge(base, local, new)
            destination = store.object_path(name, record["path"])
            if old_path and destination != old_path:
                changes[str(old_path.relative_to(store.world.dir))] = None
            relative = str(destination.relative_to(store.world.dir))
            changes[relative] = value
            if conflict:
                conflicts[relative] = {"base": base, "local": local, "new": new}
            objects[source_key] = record
        for module in sorted(set(old["manifests"]) | set(compiled.modules)):
            previous = old["manifests"].get(module)
            new = compiled.modules.get(module)
            record = {"path": str(Path(compiled.module_paths[module]) / "module.toml"), "baseline": new} if new is not None else {**previous, "baseline": None}
            base = previous["baseline"] if previous else None
            old_path = store.object_path(name, previous["path"]) if previous else None
            local = old_path.read_text() if old_path else None
            value, conflict = merge(base, local, new)
            destination = store.object_path(name, record["path"])
            if old_path and old_path != destination:
                changes[str(old_path.relative_to(store.world.dir))] = None
            relative = str(destination.relative_to(store.world.dir))
            changes[relative] = value
            if conflict:
                conflicts[relative] = {"base": base, "local": local, "new": new}
            manifests[module] = record
        item.update(objects=objects, manifests=manifests, revision=uuid4().hex)
        changes[str(store.source_path(name).relative_to(store.world.dir))] = json_text(source.snapshot)
    return {"schema_version": 1, "id": uuid4().hex, "instances": names, "expected": expected,
            "lock": lock, "config": config, "changes": changes, "conflicts": conflicts,
            "previous_objects": {n: store.instances[n]["objects"] for n in names}}


def _finalize(store, candidate, changes):
    lock = deepcopy(candidate["lock"])
    for name in candidate["instances"]:
        item = lock["instances"][name]
        old = store.instances[name]
        old_objects = candidate.get("previous_objects", {}).get(name, old["objects"])
        for key, record in list(item["objects"].items()):
            relative = str(store.object_path(name, record["path"]).relative_to(store.world.dir))
            if changes[relative] is None:
                del item["objects"][key]
                if key in old_objects:
                    previous = old_objects[key]
                    original = store.object_path(name, previous["path"]).read_text()
                    item["tombstones"][previous["key"]] = {**previous, "source": key, "content": original}
            else:
                item["tombstones"].pop(record["key"], None)
        for module, record in list(item["manifests"].items()):
            relative = str(store.object_path(name, record["path"]).relative_to(store.world.dir))
            if changes[relative] is None:
                del item["manifests"][module]
        item.setdefault("removed_manifests", {}).update({
            module: record for module, record in old["manifests"].items() if module not in item["manifests"]})
    return lock


def commit_candidate(store, candidate, changes, *, discard_candidate=False):
    _check_expected(store, candidate["expected"])
    lock = _finalize(store, candidate, changes)
    resolved = {safe_path(store.world.dir, store.world.dir / p): text for p, text in changes.items()}
    expected = {safe_path(store.world.dir, store.world.dir / p): bytes.fromhex(data) if data is not None else None
                for p, data in candidate["expected"].items()}
    if discard_candidate:
        path = candidate_path(store)
        expected[path] = path.read_bytes()
        resolved[path] = None
    store.commit(resolved, lock, candidate["config"], expected=expected)


def candidate_path(store):
    return store.world.dir / ".packages" / "update-candidate.json"


def update(store: Store, requested, *, resume=False, abort=False, source_keys=None, allow_replacements=False):
    path = candidate_path(store)
    if resume or abort:
        candidate = read_json(path)
        if not isinstance(candidate, dict) or candidate.get("schema_version") != 1 or not isinstance(candidate.get("id"), str) or not candidate["id"].isalnum():
            raise MooError("no valid update candidate to resume or abort")
        if {store.name(n) for n in requested} != set(candidate["instances"]):
            raise MooError("resume/abort must select the same instances as the candidate")
        root = safe_path(store.world.dir, store.world.dir / ".packages" / "candidates" / candidate["id"])
        if abort:
            if root.exists():
                shutil.rmtree(root)
            path.unlink()
            return "aborted uncommitted update candidate"
        changes = dict(candidate["changes"])
        for relative in changes:
            if relative.startswith("objects/"):
                file = safe_path(root, root / relative)
                content = file.read_text() if file.exists() else None
                if content and any(marker in content for marker in ("<<<<<<<", "=======", ">>>>>>>")):
                    raise MooError(f"unresolved update conflict: {file}")
                changes[relative] = content
        commit_candidate(store, candidate, changes, discard_candidate=True)
        if root.exists():
            shutil.rmtree(root)
        path.unlink(missing_ok=True)
        return "prepared resolved update"
    if path.exists():
        raise MooError("an update candidate already exists; use update --resume or --abort")
    candidate = prepare(store, requested, source_keys=source_keys, allow_replacements=allow_replacements)
    if not candidate["conflicts"]:
        commit_candidate(store, candidate, candidate["changes"])
        return "prepared update"
    root = safe_path(store.world.dir, store.world.dir / ".packages" / "candidates" / candidate["id"])
    for relative, text in candidate["changes"].items():
        if relative.startswith("objects/"):
            if relative in candidate["conflicts"]:
                conflict = candidate["conflicts"][relative]
                text = "<<<<<<< local\n" + (conflict["local"] or "") + "=======\n" + (conflict["new"] or "") + ">>>>>>> upstream\n"
            if text is not None:
                atomic_write(safe_path(root, root / relative), text)
    atomic_write(path, json_text(candidate))
    raise MooError(f"update conflicts retained in {root}; edit the candidate files (delete one to choose removal), then run package update {' '.join(requested)} --resume")


def migrate_source_keys(store, name, path):
    from .modules import identifier
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise MooError(f"cannot read source-key mapping: {e}") from None
    if data.get("schema_version") != 1 or set(data) != {"schema_version", "objects"} or not isinstance(data["objects"], dict):
        raise MooError("source-key mapping requires schema_version = 1 and [objects]")
    mapping = {identifier(k, "old source key").lower(): identifier(v, "new source key").lower() for k, v in data["objects"].items()}
    if len(mapping) != len(data["objects"]):
        raise MooError("duplicate source-key mapping")
    return update(store, [name], source_keys={name: mapping})


def remove(store: Store, requested, *, allow_modified=False):
    store.validate(parse=False, check_graph=False)
    store.record_moves()
    names = {store.name(n) for n in requested}
    lock, config = deepcopy(store.lock), deepcopy(store.config)
    expected = {store.world.dir / p: bytes.fromhex(text) if text is not None else None for p, text in expected_files(store).items()}
    changes = {}
    for name in names:
        record = lock["instances"][name]
        if record["state"] != "prepared":
            raise MooError(f"{name}: removal is already staged or completed")
        archive = {}
        for source, obj in record["objects"].items():
            path = store.object_path(name, obj["path"])
            content = path.read_text()
            if content != obj["baseline"] and not allow_modified:
                raise MooError(f"{name}: {path.name} has local edits; review them and use package remove --yes to archive and remove")
            record["tombstones"][obj["key"]] = {**obj, "source": source, "content": content}
            archive[obj["path"]] = content
            changes[path] = None
        for manifest in record["manifests"].values():
            path = store.object_path(name, manifest["path"])
            content = path.read_text()
            if content != manifest["baseline"] and not allow_modified:
                raise MooError(f"{name}: module has local edits; use package remove --yes after review")
            archive[manifest["path"]] = content
            changes[path] = None
        record.update(state="removing", objects={}, revision=uuid4().hex)
        if not record["tombstones"]:
            record["state"] = "removed"
        config.get("packages", {}).pop(name, None)
        changes[store.world.dir / ".packages" / name / "removal.json"] = json_text(archive)
    for consumer, item in lock["instances"].items():
        if item["state"] == "prepared" and any(pin["instance"] in names for pin in item["dependencies"].values()):
            raise MooError(f"{consumer} still requires a selected provider instance")
    store.commit(changes, lock, config, expected=expected)
