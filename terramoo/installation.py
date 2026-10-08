"""Per-world package instances, retained source, and staged local lifecycles."""

from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import tempfile
import tomllib
from uuid import uuid4

from .catalog import discover, safe_path
from .errors import MooError
from .modules import Modules, identifier
from .packages import Package
from .storage import digest, journal_path, json_text, read_json, toml_text, transaction


SCHEMA = 1


def _uuid(value):
    return isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def read_config(world):
    try:
        cfg = tomllib.loads((world.dir / "world.toml").read_text())
    except (OSError, ValueError) as e:
        raise MooError(f"cannot read world configuration: {e}") from None
    if type(cfg.get("format_version", 1)) is not int or cfg.get("format_version", 1) != 1:
        raise MooError("unsupported world format_version")
    specs = cfg.get("packages", {})
    if not isinstance(specs, dict) or len({k.lower() for k in specs}) != len(specs):
        raise MooError("packages must be a table with distinct instance names")
    for name, spec in specs.items():
        identifier(name, "instance name")
        if name.lower() == "local" or not isinstance(spec, dict):
            raise MooError(f"invalid package instance {name!r}")
        if set(spec) - {"source", "namespace", "bindings", "dependencies"}:
            raise MooError(f"{name}: unknown installation configuration fields")
    return cfg


def spec_for(cfg, name):
    actual = next((n for n in cfg.get("packages", {}) if n.lower() == name.lower()), None)
    return actual, cfg.get("packages", {}).get(actual, {})


def normalized_spec(name, spec):
    if not isinstance(spec, dict):
        raise MooError(f"{name}: invalid installation specification")
    source = spec.get("source")
    if not isinstance(source, str) or not source or "://" in source:
        raise MooError(f"{name}: source must be a local directory")
    namespace = identifier(spec.get("namespace", name), "namespace")
    bindings = spec.get("bindings", {})
    dependencies = spec.get("dependencies", {})
    if not isinstance(bindings, dict) or not isinstance(dependencies, dict):
        raise MooError(f"{name}: bindings and dependencies must be tables")
    for mapping in (bindings, dependencies):
        if len({k.lower() for k in mapping}) != len(mapping) or any(not isinstance(v, str) for v in mapping.values()):
            raise MooError(f"{name}: bindings/dependencies require distinct names and string values")
    return {"source": source, "namespace": namespace,
            "bindings": {k.lower(): v for k, v in bindings.items()},
            "dependencies": {k.lower(): v.lower() for k, v in dependencies.items()}}


class Store:
    def __init__(self, world):
        self.world = world
        self.path = world.dir / "packages.lock.json"
        safe_path(world.dir, self.path)
        if journal_path(world.dir).exists():
            raise MooError("unfinished local transaction; run tmoo package recover")
        self.config = read_config(world)
        self.lock = read_json(self.path)
        if self.lock is None:
            self.lock = {"schema_version": SCHEMA, "world_id": uuid4().hex, "instances": {}, "local_modules": {}}
        if (not isinstance(self.lock, dict) or type(self.lock.get("schema_version")) is not int
                or self.lock["schema_version"] != SCHEMA or not _uuid(self.lock.get("world_id"))
                or not isinstance(self.lock.get("instances"), dict) or not isinstance(self.lock.get("local_modules"), dict)):
            raise MooError("invalid or unsupported packages.lock.json")
        self._validate_records()

    @property
    def instances(self):
        return self.lock["instances"]

    def name(self, requested):
        name = next((n for n in self.instances if n.lower() == requested.lower()), None)
        if name is None:
            raise MooError(f"unknown package instance {requested!r}")
        return name

    def _validate_records(self):
        identities, namespaces, keys, names = set(), set(), {}, set()
        for name, item in self.instances.items():
            identifier(name, "instance name")
            if name.lower() == "local" or name.lower() in names or not isinstance(item, dict):
                raise MooError("invalid or duplicate instance name in lockfile")
            names.add(name.lower())
            required = {"id", "package", "version", "digest", "spec", "objects", "manifests", "tombstones", "state", "revision", "dependencies"}
            if not required <= set(item) or not _uuid(item["id"]) or item["id"] in identities:
                raise MooError(f"{name}: incomplete or duplicate installation identity")
            identities.add(item["id"])
            identifier(item["package"], "package name")
            if not _digest(item["digest"]) or not isinstance(item["version"], str) or not isinstance(item["revision"], str) or not item["revision"]:
                raise MooError(f"{name}: invalid source digest, version, or desired revision")
            if item["state"] not in {"prepared", "removing", "removed"}:
                raise MooError(f"{name}: invalid installation state")
            spec = normalized_spec(name, item["spec"])
            if item["state"] != "removed":
                if spec["namespace"].lower() in namespaces:
                    raise MooError("duplicate installation namespace")
                namespaces.add(spec["namespace"].lower())
            for field in ("objects", "manifests", "tombstones", "dependencies"):
                if not isinstance(item[field], dict):
                    raise MooError(f"{name}: invalid {field} inventory")
            for source, record in item["objects"].items():
                identifier(source, "source object key")
                if not isinstance(record, dict) or not {"key", "path", "module", "baseline"} <= set(record):
                    raise MooError(f"{name}/{source}: invalid object inventory")
                key = identifier(record["key"], "installed object key")
                if record["baseline"] is not None and not isinstance(record["baseline"], str):
                    raise MooError(f"{name}/{source}: invalid baseline")
                identifier(record["module"], "module name")
                if key.lower() == "me":
                    raise MooError("me is reserved for the player")
                path = self.object_path(name, record["path"])
                if path.stem != key or path.suffix != ".moo" or record["module"] not in item["manifests"]:
                    raise MooError(f"{name}/{source}: invalid path or module inventory")
                if item["state"] != "removed":
                    if key.lower() in keys:
                        raise MooError(f"key {key} belongs to both {keys[key.lower()]} and {name}")
                    keys[key.lower()] = name
            for module, record in item["manifests"].items():
                identifier(module, "module name")
                if not isinstance(record, dict) or not {"path", "baseline"} <= set(record):
                    raise MooError(f"{name}: invalid manifest record")
                if self.object_path(name, record["path"]).name != "module.toml":
                    raise MooError(f"{name}: invalid manifest path")
                if record["baseline"] is not None and not isinstance(record["baseline"], str):
                    raise MooError(f"{name}: invalid manifest baseline")
            for key, record in item["tombstones"].items():
                identifier(key, "removed key")
                if not isinstance(record, dict) or record.get("key") != key:
                    raise MooError(f"{name}: invalid removal record")
                if not isinstance(record.get("content"), str) or not isinstance(record.get("module"), str):
                    raise MooError(f"{name}: missing removal definition or membership")
                self.object_path(name, record.get("path", ""))
                if key.lower() in keys:
                    raise MooError(f"key {key} is reserved by a pending removal")
                if item["state"] != "removed":
                    keys[key.lower()] = name
            for alias, pin in item["dependencies"].items():
                identifier(alias, "dependency alias")
                if (not isinstance(pin, dict) or set(pin) != {"instance", "package", "version", "digest"}
                        or not all(isinstance(v, str) and v for v in pin.values()) or not _digest(pin["digest"])):
                    raise MooError(f"{name}: invalid dependency pin")
        for mid, record in self.lock["local_modules"].items():
            if not mid.startswith("local/") or not isinstance(record, dict) or not isinstance(record.get("objects"), dict):
                raise MooError("invalid standalone module history")
            identifier(mid[6:], "module name")
            for key, obj in record["objects"].items():
                if not isinstance(obj, dict) or not isinstance(obj.get("content"), str) or obj.get("key", "").lower() != key:
                    raise MooError(f"{mid}: invalid retained object definition")

    def object_path(self, name, relative):
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise MooError("invalid instance-relative path")
        root = self.world.objects_dir / "packages" / name
        safe_path(self.world.dir, root)
        return safe_path(root, root / relative)

    def source_path(self, name):
        return safe_path(self.world.dir, self.world.dir / ".packages" / name / "base" / "source.json")

    def load_source(self, name):
        data = read_json(self.source_path(name))
        if not isinstance(data, dict) or digest(json_text(data)) != self.instances[name]["digest"]:
            raise MooError(f"{name}: retained source snapshot is missing or corrupt")
        with tempfile.TemporaryDirectory(prefix="tmoo-source-") as temporary:
            root = Path(temporary)
            for relative, text in data.items():
                if not isinstance(text, str):
                    raise MooError(f"{name}: invalid retained source")
                path = safe_path(root, root / relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            return Package.load(root)

    def validate(self, *, allow_uninstalled=False, check_specs=True, parse=True, check_graph=True, allow_missing=False):
        tree = discover(self.world.objects_dir)
        by_key = tree.by_key()
        files = self.world.load_files() if parse else {}
        active = {n for n, r in self.instances.items() if r["state"] == "prepared"}
        graph = Modules.build(tree, files, instances=active, validate_graph=check_graph)
        assigned = set()
        for name, item in self.instances.items():
            if item["state"] == "removed":
                continue
            actual, spec = spec_for(self.config, name)
            if item["state"] == "prepared" and check_specs:
                if actual is None or normalized_spec(name, spec) != item["spec"]:
                    raise MooError(f"{name}: configuration changed; stage it with package update")
            source = self.load_source(name)
            if item["state"] == "prepared":
                if set(item["dependencies"]) != set(source.dependencies):
                    raise MooError(f"{name}: dependency pins differ from retained source")
                for alias, pin in item["dependencies"].items():
                    provider = self.instances.get(pin["instance"])
                    requirement = source.dependencies[alias]
                    if (provider is None or provider["state"] != "prepared"
                            or any(provider[f] != pin[f] for f in ("package", "version", "digest"))
                            or pin["package"].lower() != requirement["package"].lower()
                            or pin["version"] != requirement["version"]
                            or requirement.get("digest", pin["digest"]) != pin["digest"]
                            or item["spec"]["dependencies"].get(alias) != pin["instance"].lower()):
                        raise MooError(f"{name}: dependency {alias} no longer matches its pinned provider")
            if item["state"] == "removing":
                if any(key.lower() in by_key for key in item["tombstones"]):
                    raise MooError(f"{name}: removing instance has unexpected desired files")
                continue
            for module, record in item["manifests"].items():
                path = self.object_path(name, record["path"])
                mid = f"{name}/{module}".lower()
                if path not in tree.manifests or mid not in graph.modules or graph.modules[mid].path != path:
                    raise MooError(f"{name}: missing or moved module manifest {path}; use package update")
            for source, record in item["objects"].items():
                key = record["key"]
                path = by_key.get(key.lower())
                if path is None:
                    if allow_missing:
                        graph.membership[key.lower()] = f"{name}/{record['module']}".lower()
                        graph.modules[graph.membership[key.lower()]].keys.add(key)
                        assigned.add(key.lower())
                        continue
                    raise MooError(f"{name}: missing package file for {key}; restore it or stage package update/remove")
                mid = f"{name}/{record['module']}".lower()
                if graph.membership[key.lower()] != mid:
                    raise MooError(f"{name}: unrecorded module/instance move for {key}")
                if not path.is_relative_to(self.world.objects_dir / "packages" / name):
                    raise MooError(f"{name}: object {key} is outside its installation directory")
                assigned.add(key.lower())
            root = self.world.objects_dir / "packages" / name
            if any(p.is_relative_to(root) and p.stem.lower() not in assigned for p in tree.objects):
                raise MooError(f"{name}: unrecorded object under installation directory")
        for module in graph.modules.values():
            if module.instance:
                for key in module.keys:
                    if key.lower() not in assigned:
                        raise MooError(f"{module.id}: unrecorded added object {key}; use a standalone module")
        if not allow_uninstalled:
            for name in self.config.get("packages", {}):
                if name.lower() not in {n.lower() for n in active}:
                    raise MooError(f"{name}: configured instance is not installed; run package install")
        return graph

    def record_moves(self):
        tree = discover(self.world.objects_dir).by_key()
        for name, record in self.instances.items():
            if record["state"] != "prepared":
                continue
            root = self.world.objects_dir / "packages" / name
            for obj in record["objects"].values():
                obj["path"] = str(tree[obj["key"].lower()].relative_to(root))

    def baseline_files(self, name):
        record = self.instances[name]
        return {**{self.object_path(name, r["path"]): r["baseline"] for r in record["objects"].values()},
                **{self.object_path(name, r["path"]): r["baseline"] for r in record["manifests"].values()}}

    def desired_paths(self):
        tree = discover(self.world.objects_dir)
        return [*tree.objects, *tree.manifests]

    def validate_candidate(self, changes, lock, config, *, check_graph=True):
        """Validate a complete prospective world without replacing active files."""
        from .world import World
        with tempfile.TemporaryDirectory(prefix="tmoo-candidate-") as temporary:
            root = Path(temporary)
            candidate = World(self.world.name, root, self.world.player_name, self.world.connection,
                              ignore_props=self.world.ignore_props)
            candidate.objects_dir.mkdir(parents=True)
            contents = {path: path.read_bytes() for path in self.desired_paths()}
            for name, item in self.instances.items():
                source = self.source_path(name)
                if source.exists():
                    contents[source] = source.read_bytes()
            contents.update(changes)
            contents[self.path] = json_text(lock)
            contents[self.world.dir / "world.toml"] = toml_text(config)
            for path, data in contents.items():
                if data is not None:
                    target = safe_path(candidate.dir, candidate.dir / path.relative_to(self.world.dir))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data.encode() if isinstance(data, str) else data)
            Store(candidate).validate(allow_uninstalled=True, check_graph=check_graph)
            for obj in candidate.load_files().values():
                ignored = {p.name.lower() for p in obj.props} & candidate.ignore_props
                if ignored:
                    raise MooError(f"{obj.key}: desired runtime properties excluded: {sorted(ignored)}")

    def commit(self, changes, lock=None, config=None, *, expected=None, check_graph=True):
        lock = self.lock if lock is None else lock
        config = self.config if config is None else config
        config["format_version"] = 1
        if expected is not None:
            initial = {p for p, data in expected.items() if data is not None and p.is_relative_to(self.world.objects_dir)}
            if set(self.desired_paths()) != initial:
                raise MooError("object inventory changed while preparing the transaction")
            for path in changes:
                if path.is_relative_to(self.world.objects_dir):
                    alias = next((p for p in initial if path.exists() and p.exists() and path.samefile(p)), None)
                    expected.setdefault(path, expected[alias] if alias is not None else None)
        self.validate_candidate(changes, lock, config, check_graph=check_graph)
        changes = {**changes, self.path: json_text(lock), self.world.dir / "world.toml": toml_text(config)}
        transaction(self.world.dir, changes, expected=expected)
        self.lock, self.config = lock, config

    def prepare_install(self, requested, source, *, namespace=None, bindings=None, mapping=None):
        identifier(requested, "instance name")
        if requested.lower() == "local":
            raise MooError("local is reserved for standalone modules")
        prior = next((n for n in self.instances if n.lower() == requested.lower()), None)
        retired = None
        if prior is not None and self.instances[prior]["state"] == "removed":
            retired = (prior, deepcopy(self.instances[prior]), self.source_path(prior).read_text())
            del self.instances[prior]
        elif prior is not None:
            raise MooError(f"instance {prior!r} already has recorded state; use update or recover")
        self.validate(allow_uninstalled=True)
        self.record_moves()
        expected = {p: p.read_bytes() for p in self.desired_paths()}
        expected[self.path] = self.path.read_bytes() if self.path.exists() else None
        expected[self.world.dir / "world.toml"] = (self.world.dir / "world.toml").read_bytes()
        config, lock = deepcopy(self.config), deepcopy(self.lock)
        actual, configured = spec_for(config, requested)
        name = actual or requested
        spec = deepcopy(configured)
        spec["source"] = os.path.relpath(Path(source).absolute(), self.world.dir)
        if namespace is not None:
            spec["namespace"] = namespace
        if bindings:
            spec.setdefault("bindings", {}).update(bindings)
        config.setdefault("packages", {})[name] = spec
        packages, specs, imports = {}, {}, {name: mapping} if mapping else {}

        def resolve(instance):
            if instance in packages or instance in self.instances:
                return
            actual, declaration = spec_for(config, instance)
            if actual is None:
                raise MooError(f"missing configured dependency instance {instance!r}")
            if actual != instance:
                instance = actual
            current = normalized_spec(instance, declaration)
            package = Package.load((self.world.dir / current["source"]).absolute())
            packages[instance], specs[instance] = package, current
            for provider in current["dependencies"].values():
                actual_provider, _ = spec_for(config, provider)
                if actual_provider is None:
                    raise MooError(f"{instance}: dependency instance {provider!r} is not configured")
                resolve(actual_provider)

        resolve(name)
        providers = dict(self.validate(allow_uninstalled=True).membership)
        occupied = discover(self.world.objects_dir).by_key()
        reserved = {key.lower() for r in self.instances.values() for key in r["tombstones"]}
        for instance, package in packages.items():
            if instance in imports and (set(imports[instance]) != set(package.objects)
                    or len({v.lower() for v in imports[instance].values()}) != len(imports[instance])):
                raise MooError("import requires one distinct managed key for every source object")
            mapping_for = package.mapping(specs[instance]["namespace"], imports.get(instance))
            for source_key, key in mapping_for.items():
                if key.lower() in reserved or (key.lower() in occupied and not (instance == name and mapping)):
                    raise MooError(f"{instance}: occupied or reserved key {key}")
                if key.lower() in providers and not (instance == name and mapping):
                    raise MooError(f"{instance}: duplicate installed key {key}")
                providers[key.lower()] = f"{instance}/{package.membership[source_key]}".lower()
        changes = {}
        if retired:
            old_name, old_record, old_source = retired
            archive = safe_path(self.world.dir, self.world.dir / ".packages" / "history" / old_record["id"])
            changes[archive / "installation.json"] = json_text(old_record)
            changes[archive / "source.json"] = old_source
            for filename in ("deployment.json", "removal.json"):
                path = safe_path(self.world.dir, self.world.dir / ".packages" / old_name / filename)
                if path.exists():
                    changes[archive / filename] = path.read_text()
                    changes[path] = None
        for instance, package in packages.items():
            current = specs[instance]
            dependency_records = {}
            for alias, requirement in package.dependencies.items():
                target = current["dependencies"].get(alias)
                actual_target, _ = spec_for(config, target or "")
                target_package = packages.get(actual_target) or (self.load_source(actual_target) if actual_target in self.instances else None)
                if target_package is None or target_package.name.lower() != requirement["package"].lower() or target_package.version != requirement["version"] or requirement.get("digest", target_package.digest) != target_package.digest:
                    raise MooError(f"{instance}: incompatible dependency {alias} -> {target}")
                dependency_records[alias] = {"instance": actual_target, "package": target_package.name,
                                             "version": target_package.version, "digest": target_package.digest}
            compiled = package.compile(instance, current["namespace"], current["bindings"], providers,
                                       current["dependencies"], previous=imports.get(instance))
            objects = {}
            for source_key, text in compiled.objects.items():
                record = {"key": compiled.mapping[source_key], "path": compiled.paths[source_key],
                          "module": compiled.membership[source_key], "baseline": text}
                objects[source_key] = record
                destination = self.object_path(instance, record["path"])
                if mapping and instance == name:
                    origin = occupied[record["key"].lower()]
                    changes[origin] = None
                    changes[destination] = origin.read_text()
                else:
                    changes[destination] = text
            manifests = {module: {"path": str(Path(compiled.module_paths[module]) / "module.toml"), "baseline": text}
                         for module, text in compiled.modules.items()}
            changes.update({self.object_path(instance, r["path"]): r["baseline"] for r in manifests.values()})
            lock["instances"][instance] = {"id": uuid4().hex, "package": package.name, "version": package.version,
                "digest": package.digest, "spec": current, "objects": objects, "manifests": manifests,
                "tombstones": {}, "dependencies": dependency_records, "state": "prepared",
                "revision": digest(json_text({"digest": package.digest, "spec": current, "objects": objects, "manifests": manifests}))}
            changes[self.source_path(instance)] = json_text(package.snapshot)
        if mapping:
            from .imports import rewrite_membership
            rewrite_membership(self, changes, lock, name)
        self.validate_candidate(changes, lock, config)
        return name, changes, lock, config, expected

    def install(self, *args, **kwargs):
        name, changes, lock, config, expected = self.prepare_install(*args, **kwargs)
        self.commit(changes, lock, config, expected=expected)
        return name
