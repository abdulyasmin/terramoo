"""Portable package inventories and compilation for a named world instance."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

from . import moolit, objdef
from .catalog import discover, read_snapshot, safe_path
from .errors import MooError
from .model import ObjectDef
from .modules import components, identifier, object_values, read_manifest, string_list
from .moolit import Obj, Ref, walk
from .storage import digest, json_text, toml_text


def map_values(obj, transform):
    obj.parent = walk(obj.parent, transform)
    obj.location = walk(obj.location, transform)
    obj.owner = walk(obj.owner, transform)
    for prop in obj.props:
        prop.value = walk(prop.value, transform)
        prop.owner = walk(prop.owner, transform)
    for verb in obj.verbs:
        verb.owner = walk(verb.owner, transform)
    return obj


def binding(text: str):
    try:
        value = moolit.parse(text)
    except (ValueError, TypeError):
        raise MooError(f"invalid object input binding {text!r}") from None
    if not (isinstance(value, Ref) or isinstance(value, Obj) and isinstance(value.num, int) and value.num <= 0):
        raise MooError(f"object inputs require @key, $name, @me, #0, or a negative sentinel: {text!r}")
    return value


@dataclass
class Compiled:
    objects: dict[str, str]  # source key -> rendered installed definition
    paths: dict[str, str]  # source key -> relative path within the instance
    modules: dict[str, str]  # module name -> compiled manifest
    module_paths: dict[str, str]
    mapping: dict[str, str]
    membership: dict[str, str]


@dataclass
class Package:
    root: Path
    name: str
    version: str
    digest: str
    manifests: dict[str, dict]
    module_paths: dict[str, str]
    objects: dict[str, ObjectDef]
    paths: dict[str, str]
    membership: dict[str, str]
    inputs: dict
    dependencies: dict
    snapshot: dict[str, str]

    @classmethod
    def load(cls, root: Path):
        root = root.absolute()
        tree = discover(root)
        package_path = safe_path(root, root / "package.toml")
        paths = [package_path, *tree.manifests, *tree.objects]
        try:
            snapshots = {p: read_snapshot(p) for p in paths}
            cfg = tomllib.loads(snapshots[package_path][0].decode())
        except (OSError, ValueError) as e:
            raise MooError(f"{package_path}: {e}") from None
        if type(cfg.get("schema_version")) is not int or cfg["schema_version"] != 1:
            raise MooError(f"{package_path}: package schema_version must be 1")
        unknown = set(cfg) - {"schema_version", "name", "version", "modules", "inputs", "dependencies"}
        if unknown:
            raise MooError(f"{package_path}: unknown package fields: {sorted(unknown)}")
        name = identifier(cfg.get("name"), "package name")
        version = cfg.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", version):
            raise MooError(f"{package_path}: version must be an exact version string")
        declared = string_list(cfg.get("modules"), "package modules")
        roots = [safe_path(root, root / p) for p in declared]
        if len(set(roots)) != len(roots) or any(Path(p).is_absolute() for p in declared):
            raise MooError("package modules contain duplicate or absolute paths")
        if {p / "module.toml" for p in roots} != set(tree.manifests):
            raise MooError("package modules must list every module manifest exactly once")
        manifests, module_paths, by_root = {}, {}, {}
        for directory in roots:
            manifest = read_manifest(directory / "module.toml")
            module = manifest["name"].lower()
            if module in manifests or "instance" in manifest:
                raise MooError(f"{directory}: duplicate module or installed instance field in package source")
            manifests[module] = manifest
            # Include the source module path to keep nested module roots disjoint.
            module_paths[module] = str(directory.relative_to(root))
            by_root[directory] = module
        objects, object_paths, membership = {}, {}, {}
        for path in tree.objects:
            try:
                obj = objdef.parse(snapshots[path][0].decode())
            except ValueError as e:
                raise MooError(f"{path}: {e}") from None
            identifier(path.stem, "object key")
            if obj.key != path.stem or obj.key.lower() == "me":
                raise MooError(f"{path}: filename/header mismatch or reserved object key me")
            key = obj.key.lower()
            if key in objects:
                raise MooError(f"duplicate package object key {key}: {object_paths[key]} and {path}")
            module = next((by_root[p] for p in path.parents if p in by_root), None)
            if module is None:
                raise MooError(f"{path}: object is outside listed modules")
            objects[key] = obj
            object_paths[key] = str(path.relative_to(root))
            membership[key] = module
        inputs = cfg.get("inputs", {})
        dependencies = cfg.get("dependencies", {})
        if not isinstance(inputs, dict) or not isinstance(dependencies, dict):
            raise MooError("inputs and dependencies must be tables")
        for field, values in (("input", inputs), ("dependency", dependencies)):
            if len({x.lower() for x in values}) != len(values):
                raise MooError(f"case-colliding {field} names")
            for key in values:
                identifier(key, f"{field} name")
        inputs = {k.lower(): v for k, v in inputs.items()}
        dependencies = {k.lower(): v for k, v in dependencies.items()}
        for key, spec in inputs.items():
            if key == "me" or key in objects:
                raise MooError(f"input {key} collides with an object key or @me")
            if not isinstance(spec, dict) or spec.get("type") != "object" or set(spec) - {"type", "required", "default"}:
                raise MooError(f"input {key} must declare type = 'object'")
            if type(spec.get("required", False)) is not bool:
                raise MooError(f"input {key}: required must be a boolean")
            if "default" in spec:
                binding(spec["default"])
        for alias, spec in dependencies.items():
            if alias == "local" or alias in manifests:
                raise MooError(f"dependency alias {alias} collides with a module or local")
            if not isinstance(spec, dict) or set(spec) - {"package", "version", "digest"}:
                raise MooError(f"invalid dependency {alias}")
            identifier(spec.get("package"), "dependency package")
            if not isinstance(spec.get("version"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", spec["version"]):
                raise MooError(f"dependency {alias} needs an exact version")
            if "digest" in spec and not re.fullmatch(r"[0-9a-f]{64}", spec["digest"]):
                raise MooError(f"dependency {alias}: digest must be a sha256 hex value")
        for module, manifest in manifests.items():
            uses = {x.lower() for x in manifest["uses_inputs"]}
            if not uses <= set(inputs):
                raise MooError(f"{module}: unknown uses_inputs {uses - set(inputs)}")
            for dep in manifest["references"] + manifest["depends_on"]:
                parts = dep.lower().split("/")
                if not (len(parts) == 1 and parts[0] in manifests or len(parts) == 2 and parts[0] in dependencies):
                    raise MooError(f"{module}: unknown module/dependency address {dep!r}")
                for part in parts:
                    identifier(part, "module address")
        for key, obj in objects.items():
            module = membership[key]
            manifest = manifests[module]
            declared_refs = {x.lower() for x in manifest["references"] + manifest["depends_on"]}
            uses = {x.lower() for x in manifest["uses_inputs"]}

            def validate(value):
                if isinstance(value, Obj) and (not isinstance(value.num, int) or value.num > 0):
                    raise MooError(f"{key}: package source contains a world-specific object {value}; use an input")
                if isinstance(value, Ref) and value.kind == "@" and value.name.lower() != "me":
                    target = value.name.lower()
                    if target in objects:
                        provider = membership[target]
                        if provider != module and provider not in declared_refs:
                            raise MooError(f"{module}: @{target} requires a reference/dependency declaration for {provider}")
                    elif target not in inputs or target not in uses:
                        raise MooError(f"{key}: @{target} must be a declared input with uses_inputs")
                return value

            for value in object_values(obj):
                walk(value, validate)
        edges = {m: {v.lower() for v in x["references"] + x["depends_on"] if "/" not in v} for m, x in manifests.items()}
        for group in components(edges):
            if any(set(v.lower() for v in manifests[m]["depends_on"]) & set(group) for m in group):
                raise MooError(f"unsatisfiable module initialization cycle: {', '.join(group)}")
        parents = {k: {o.parent.name.lower()} if isinstance(o.parent, Ref) and o.parent.kind == "@" and o.parent.name.lower() in objects else set()
                   for k, o in objects.items()}
        if any(len(g) > 1 or g[0] in parents[g[0]] for g in components(parents)):
            raise MooError("package object parent cycle")
        after = discover(root)
        if after != tree or any(read_snapshot(p) != before for p, before in snapshots.items()):
            raise MooError(f"package source changed while capturing {root}")
        snapshot = {str(p.relative_to(root)): data.decode() for p, (data, _) in snapshots.items()}
        return cls(root, name, version, digest(json_text(snapshot)), manifests, module_paths,
                   objects, object_paths, membership, inputs, dependencies, snapshot)

    def mapping(self, namespace: str, previous: dict | None = None) -> dict[str, str]:
        identifier(namespace, "namespace")
        previous = previous or {}
        return {key: identifier(previous.get(key, f"{namespace}__{obj.key}"), "installed key")
                for key, obj in self.objects.items()}

    def compile(self, instance: str, namespace: str, inputs: dict[str, str], providers: dict[str, str | None],
                dependencies: dict[str, str], *, previous: dict | None = None) -> Compiled:
        identifier(instance, "instance name")
        if instance.lower() == "local":
            raise MooError("local is reserved for standalone modules")
        mapping = self.mapping(namespace, previous)
        supplied = {k.lower(): v for k, v in inputs.items()}
        if len(supplied) != len(inputs) or set(supplied) - set(self.inputs):
            raise MooError(f"{instance}: unknown or duplicate input bindings")
        values = {}
        for name, spec in self.inputs.items():
            if name not in supplied and spec.get("required", False) and "default" not in spec:
                raise MooError(f"{instance}: missing required binding {name}")
            values[name] = binding(supplied.get(name, spec.get("default", "#-1")))
        if set(dependencies) != set(self.dependencies):
            raise MooError(f"{instance}: dependency selections must name {sorted(self.dependencies)}")
        compiled_modules, compiled_objects, paths = {}, {}, {}
        for module, manifest in self.manifests.items():
            def qualify(address):
                if "/" in address:
                    alias, target = address.lower().split("/")
                    return f"{dependencies[alias]}/{target}".lower()
                return f"{instance}/{address}".lower()

            data_refs = {qualify(x) for x in manifest["references"]}
            hard = {qualify(x) for x in manifest["depends_on"]}
            hard_inputs = {x.lower() for x in manifest["depends_on_inputs"]}
            for name in manifest["uses_inputs"]:
                value = values[name.lower()]
                if isinstance(value, Ref) and value.kind == "@" and value.name.lower() != "me":
                    provider = providers.get(value.name.lower())
                    if provider is None:
                        raise MooError(f"{instance}/{module}: input {name} requires a managed object in a module")
                    if provider != f"{instance}/{module}".lower():
                        data_refs.add(provider)
                    if name.lower() in hard_inputs:
                        hard.add(provider)
            compiled_modules[module] = toml_text({"schema_version": 1, "name": manifest["name"],
                "instance": instance, "references": sorted(data_refs), "depends_on": sorted(hard)})
        for key, source in self.objects.items():
            obj = deepcopy(source)
            obj.key = mapping[key]

            def substitute(value):
                if isinstance(value, Ref) and value.kind == "@":
                    name = value.name.lower()
                    if name in mapping:
                        return Ref("@", mapping[name])
                    if name in values:
                        return values[name]
                return value

            try:
                compiled_objects[key] = objdef.render(map_values(obj, substitute))
            except ValueError as e:
                raise MooError(f"{instance}/{key}: {e}") from None
            paths[key] = str(Path(self.paths[key]).with_name(mapping[key] + ".moo"))
        return Compiled(compiled_objects, paths, compiled_modules, self.module_paths, mapping, self.membership)
