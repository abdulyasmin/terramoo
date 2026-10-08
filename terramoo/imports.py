"""Explicitly claim existing managed objects without recreating or renaming them."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tomllib
from uuid import uuid4

from . import export
from .catalog import discover, safe_path
from .errors import MooError
from .installation import Store
from .modules import references
from .ownership import identity, operation_path, read_receipt, read_remote
from .packages import Package
from .storage import atomic_write, fsync_dir, json_text, read_json, toml_text


def rewrite_membership(store, changes, lock, instance):
    before = store.validate(allow_uninstalled=True)
    after = dict(before.membership)
    item = lock["instances"][instance]
    for record in item["objects"].values():
        after[record["key"].lower()] = f"{instance}/{record['module']}".lower()
    transferred = {key for key in after if before.membership.get(key) != after[key]}
    replacements = {}
    for key, old in before.membership.items():
        if old:
            replacements.setdefault(old, set()).add(after[key])

    def rewrite(text, module_id):
        data = tomllib.loads(text)
        prefix = module_id.split("/")[0]
        for field in ("references", "depends_on"):
            targets = set()
            for name in data.get(field, []):
                old = (name if "/" in name else f"{prefix}/{name}").lower()
                targets |= replacements.get(old, {old})
            if module_id in targets:
                if field == "depends_on":
                    raise MooError(f"import would turn {module_id}'s initialization ordering into a self-dependency; revise the module boundaries explicitly")
                targets.remove(module_id)
            data[field] = sorted(targets)
        return data

    for mid, module in before.modules.items():
        path = module.path
        text = toml_text(rewrite(path.read_text(), mid))
        if text != path.read_text():
            changes[path] = text
        if module.instance:
            module_name = mid.split("/")[1]
            record = lock["instances"][module.instance]["manifests"][module_name]
            if record["baseline"] is not None:
                record["baseline"] = toml_text(rewrite(record["baseline"], mid))
    old_files = {k.lower(): o for k, o in store.world.load_files().items()}
    for module, record in item["manifests"].items():
        mid = f"{instance}/{module}".lower()
        path = store.object_path(instance, record["path"])
        data = tomllib.loads(changes[path])
        ordinary, hard = set(data.get("references", [])), set(data.get("depends_on", []))
        for key, owner in after.items():
            if owner != mid:
                continue
            old_module = before.modules.get(before.membership.get(key))
            for target in references(old_files[key]):
                provider = after.get(target)
                if provider is None:
                    raise MooError(f"{key}: imported object references ungrouped @{target}; give the provider a module")
                if provider == mid:
                    continue
                if old_module and before.membership.get(target) in old_module.depends_on:
                    hard.add(provider)
                else:
                    ordinary.add(provider)
        data.update(references=sorted(ordinary), depends_on=sorted(hard))
        changes[path] = toml_text(data)
    for record in lock["local_modules"].values():
        for key in transferred:
            record["objects"].pop(key, None)


def prepare(store, source, instance, mapping_path, *, namespace=None, bindings=None):
    try:
        cfg = tomllib.loads(Path(mapping_path).read_text())
    except (OSError, ValueError) as e:
        raise MooError(f"cannot read import mapping: {e}") from None
    if cfg.get("schema_version") != 1 or not isinstance(cfg.get("objects"), dict) or set(cfg) - {"schema_version", "objects"}:
        raise MooError("import mapping requires schema_version = 1 and an [objects] table")
    package = Package.load(Path(source))
    mapping = {k.lower(): v for k, v in cfg["objects"].items()}
    if (len(mapping) != len(cfg["objects"]) or set(mapping) != set(package.objects)
            or any(not isinstance(v, str) for v in mapping.values())
            or len({v.lower() for v in mapping.values()}) != len(mapping)):
        raise MooError("import requires one distinct managed key for every package object")
    files = discover(store.world.objects_dir).by_key()
    for key, value in mapping.items():
        if value.lower() not in files:
            raise MooError(f"{value}: managed local definition is required before import")
        mapping[key] = files[value.lower()].stem
    for item in store.instances.values():
        occupied = {r["key"].lower() for r in item["objects"].values()} | {k.lower() for k in item["tombstones"]}
        if occupied & {v.lower() for v in mapping.values()}:
            raise MooError("import cannot claim an object owned by another instance")
    proposed = store.prepare_install(instance, source, namespace=namespace, bindings=bindings, mapping=mapping)
    return proposed, mapping


def execute(store, proposed, mapping):
    world = store.world
    name, changes, lock, config, expected = proposed
    remote, receipt = read_remote(world), read_receipt(world, store)
    if receipt["identity"] not in (None, identity(world)):
        raise MooError("import receipt belongs to another world")
    if receipt["epoch"] and (receipt["epoch"] != remote["epoch"] or receipt["world_id"] != remote["world_id"]):
        raise MooError("import requires the current deployment checkout")
    if not receipt["epoch"] and remote["entries"]:
        raise MooError("import cannot reconstruct missing deployment authority")
    refs = world.refs()
    wanted = list(mapping.values())
    if any(refs.registry_key(k) is None or not refs.generation_for(k) for k in wanted):
        raise MooError("import requires verified managed registry bindings; use adopt --verify first")
    live = export.export(world, refs, wanted)
    if any(live.get(refs.registry_key(k)) is None for k in wanted):
        raise MooError("import cannot claim missing live objects")
    entries, records = deepcopy(remote["entries"]), deepcopy(receipt["bindings"])
    item = lock["instances"][name]
    for source_key, key in mapping.items():
        current = entries.get(key.lower())
        if current and current[2] != "local:" + store.lock["world_id"]:
            raise MooError(f"{key}: remote ownership belongs to another instance")
        actual = refs.registry_key(key)
        nonce = refs.generation_for(key)
        entries[key.lower()] = [actual, nonce, item["id"], 1]
        records[key.lower()] = {"key": actual, "owner": item["id"], "instance": name,
            "module": f"{name}/{item['objects'][source_key]['module']}".lower(),
            "object": refs.registry[actual].num, "generation": nonce, "revision": None}
    epoch = uuid4().hex
    intent = {"schema_version": 1, "kind": "import", "world_id": lock["world_id"], "identity": identity(world),
        "old_epoch": remote["epoch"], "new_epoch": epoch, "entries": list(entries.values()), "bindings": records,
        "changes": {str(p.relative_to(world.dir)): v for p, v in changes.items()}, "lock": lock, "config": config,
        "expected": {str(p.relative_to(world.dir)): v.hex() if v is not None else None for p, v in expected.items()}}
    from .storage import transaction
    transaction(world.dir, {}, expected=expected)
    atomic_write(operation_path(world), json_text(intent))
    world.eval(world.helper("tmoo_packages", '"import"', world.transport.serialize(lock["world_id"]),
               world.transport.serialize(remote["epoch"]), world.transport.serialize(epoch), world.transport.serialize(list(entries.values()))))
    return recover(world)


def recover(world):
    path = operation_path(world)
    intent = read_json(path)
    if (not isinstance(intent, dict) or intent.get("schema_version") != 1 or intent.get("kind") != "import"
            or intent.get("identity") != identity(world)):
        raise MooError("invalid import recovery intent")
    remote = read_remote(world)
    if remote["epoch"] == intent["old_epoch"]:
        path.unlink()
        return "import did not claim remote objects; local definitions preserved"
    if remote["epoch"] != intent["new_epoch"] or remote["world_id"] != intent["world_id"]:
        raise MooError("import ownership revision conflicts with recovery intent")
    expected_entries = {row[0].lower(): row for row in intent["entries"]}
    if remote["entries"] != expected_entries:
        raise MooError("import ownership records differ from the journal")
    store = Store(world)
    changes = {safe_path(world.dir, world.dir / relative): text for relative, text in intent["changes"].items()}
    expected = {safe_path(world.dir, world.dir / relative): bytes.fromhex(value) if value is not None else None
                for relative, value in intent["expected"].items()}
    registry = world.read_registry()
    for key, record in intent["bindings"].items():
        actual = next((k for k in registry if k.lower() == key), None)
        if (not actual and not record.get("removed") or actual and (registry[actual].num != record["object"] or registry.generations.get(actual) != record["generation"])):
            raise MooError(f"{key}: registry changed during import recovery")
    if store.lock != intent["lock"]:
        store.commit(changes, intent["lock"], intent["config"], expected=expected)
    from .ownership import receipt_path
    atomic_write(receipt_path(world), json_text({"schema_version": 1, "world_id": intent["world_id"],
        "identity": intent["identity"], "epoch": intent["new_epoch"], "bindings": intent["bindings"]}))
    for name, item in intent["lock"]["instances"].items():
        atomic_write(safe_path(world.dir, world.dir / ".packages" / name / "deployment.json"),
                     json_text({"schema_version": 1, "id": item["id"], "epoch": intent["new_epoch"],
                                "bindings": {k: v for k, v in intent["bindings"].items() if v["owner"] == item["id"]}}))
    path.unlink()
    fsync_dir(path.parent)
    return "imported existing identities; run plan to review retained local definitions"
