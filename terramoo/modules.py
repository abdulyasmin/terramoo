"""Module membership, declared reference graphs, and execution groups."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib

from . import objdef
from .catalog import Discovery
from .errors import MooError
from .model import ObjectDef
from .moolit import Obj, Ref, walk
from .refs import Refs


def identifier(value, label="name") -> str:
    try:
        return objdef.validate_identifier(value, label)
    except (TypeError, ValueError) as e:
        raise MooError(str(e)) from None


def string_list(value, label) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise MooError(f"{label} must be a list of strings")
    if len({x.lower() for x in value}) != len(value):
        raise MooError(f"{label} contains duplicates")
    return value


def read_manifest(path: Path) -> dict:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise MooError(f"{path}: {e}") from None
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise MooError(f"{path}: module schema_version must be 1")
    identifier(data.get("name"), "module name")
    unknown = set(data) - {"schema_version", "name", "instance", "references", "depends_on", "uses_inputs", "depends_on_inputs"}
    if unknown:
        raise MooError(f"{path}: unknown module fields: {', '.join(sorted(unknown))}")
    for key in ("references", "depends_on", "uses_inputs", "depends_on_inputs"):
        data[key] = string_list(data.get(key, []), f"{path}: {key}")
    if not {x.lower() for x in data["depends_on_inputs"]} <= {x.lower() for x in data["uses_inputs"]}:
        raise MooError(f"{path}: depends_on_inputs must be declared in uses_inputs")
    if "instance" in data:
        identifier(data["instance"], "instance name")
    return data


def object_values(obj: ObjectDef):
    return (obj.parent, obj.location, obj.owner, *(p.value for p in obj.props),
            *(p.owner for p in obj.props), *(v.owner for v in obj.verbs))


def references(obj: ObjectDef, refs: Refs | None = None) -> set[str]:
    found = set()
    by_obj = {v: k for k, v in refs.registry.items()} if refs else {}

    def leaf(value):
        if isinstance(value, Ref):
            if value.kind == "@" and value.name.lower() != "me":
                found.add(value.name.lower())
            elif value.kind == "$" and refs:
                try:
                    resolved = refs.resolve_ref(value)
                except KeyError as error:
                    raise MooError(str(error)) from None
                if resolved in by_obj:
                    found.add(by_obj[resolved].lower())
        elif isinstance(value, Obj) and value in by_obj:
            found.add(by_obj[value].lower())
        return value

    for value in object_values(obj):
        walk(value, leaf)
    return found


def components(edges: dict[str, set[str]]) -> list[tuple[str, ...]]:
    """Tarjan components, returned with providers before consumers."""
    indices, low, stack, active, result = {}, {}, [], set(), []

    def visit(node):
        indices[node] = low[node] = len(indices)
        stack.append(node)
        active.add(node)
        for provider in sorted(edges[node]):
            if provider not in edges:
                raise MooError(f"{node}: unknown dependency {provider}")
            if provider not in indices:
                visit(provider)
                low[node] = min(low[node], low[provider])
            elif provider in active:
                low[node] = min(low[node], indices[provider])
        if low[node] == indices[node]:
            group = []
            while True:
                member = stack.pop()
                active.remove(member)
                group.append(member)
                if member == node:
                    break
            result.append(tuple(sorted(group)))

    for node in sorted(edges):
        if node not in indices:
            visit(node)
    return result


@dataclass
class Module:
    id: str
    path: Path
    instance: str | None
    keys: set[str] = field(default_factory=set)
    references: set[str] = field(default_factory=set)
    depends_on: set[str] = field(default_factory=set)


@dataclass
class Modules:
    modules: dict[str, Module]
    membership: dict[str, str | None]
    groups: list[tuple[str, ...]]

    @classmethod
    def build(cls, tree: Discovery, files: dict[str, ObjectDef], *, refs: Refs | None = None,
              instances: set[str] | None = None, validate_graph: bool = True) -> Modules:
        modules, roots = {}, {}
        known_instances = {x.lower() for x in instances or ()}
        for path in tree.manifests:
            manifest = read_manifest(path)
            instance = manifest.get("instance")
            if instance and instance.lower() not in known_instances:
                raise MooError(f"{path}: unrecorded package instance {instance!r}")
            if manifest["uses_inputs"]:
                raise MooError(f"{path}: world manifests must have compiled input references")
            module_id = f"{instance or 'local'}/{manifest['name']}".lower()
            if module_id in modules:
                raise MooError(f"duplicate module {module_id}: {modules[module_id].path} and {path}")

            def qualify(name):
                parts = name.split("/")
                if len(parts) not in (1, 2):
                    raise MooError(f"{path}: invalid module address {name!r}")
                for part in parts:
                    identifier(part, "module address")
                return (f"{instance or 'local'}/{name}" if len(parts) == 1 else name).lower()

            module = Module(module_id, path, instance,
                            references={qualify(x) for x in manifest["references"]},
                            depends_on={qualify(x) for x in manifest["depends_on"]})
            if module_id in module.depends_on:
                raise MooError(f"{path}: initialization self-dependency {module_id}")
            module.references.discard(module_id)
            modules[module_id] = module
            roots[path.parent] = module_id
        membership = {}
        for path in tree.objects:
            module = next((roots[d] for d in path.parents if d in roots), None)
            membership[path.stem.lower()] = module
            if module:
                modules[module].keys.add(path.stem)
        for key, obj in (files.items() if validate_graph else ()):
            owner = membership.get(key.lower())
            for target in references(obj, refs):
                if target not in membership:
                    raise MooError(f"{key}: refers to @{target}, which has no file")
                provider = membership[target]
                if owner and provider != owner:
                    if provider is None:
                        raise MooError(f"{key}: @{target} needs a module before {owner} can reference it")
                    module = modules[owner]
                    if provider not in module.references | module.depends_on:
                        raise MooError(f"{module.path}: {key} refers to {provider}; declare references or depends_on")
        edges = {m.id: m.references | m.depends_on for m in modules.values()}
        if not validate_graph:
            return cls(modules, membership, [])
        groups = components(edges)
        for group in groups:
            members = set(group)
            for member in group:
                conflicts = modules[member].depends_on & members
                if conflicts:
                    paths = ', '.join(str(modules[m].path) for m in group)
                    raise MooError(f"unsatisfiable initialization cycle: {member} depends_on {sorted(conflicts)} inside reference group {group} ({paths})")
        # Parent loops are invalid even if all their objects already exist.
        by_key = {k.lower(): obj for k, obj in files.items()}
        parents = {k: set() for k in by_key}
        for key, obj in by_key.items():
            parent = obj.parent
            if refs and isinstance(parent, Obj):
                parent = refs.symbolize_obj(parent)
            if isinstance(parent, Ref) and parent.kind == "@" and parent.name.lower() in by_key:
                parents[key].add(parent.name.lower())
        for group in components(parents):
            if len(group) > 1 or group[0] in parents[group[0]]:
                raise MooError(f"parent chain loops back to itself: {', '.join(group)}")
        return cls(modules, membership, groups)

    def select(self, module_names=(), instance_names=(), *, with_deps=False) -> set[str]:
        selected = set()
        for name in module_names:
            name = (name if "/" in name else f"local/{name}").lower()
            if name not in self.modules:
                raise MooError(f"unknown module {name!r}")
            selected.add(name)
        for instance in instance_names:
            found = {m.id for m in self.modules.values() if m.instance and m.instance.lower() == instance.lower()}
            if not found:
                raise MooError(f"unknown package instance {instance!r}")
            selected |= found
        if not module_names and not instance_names:
            selected = set(self.modules)
        if with_deps:
            queue = list(selected)
            while queue:
                module = self.modules[queue.pop()]
                for provider in module.references | module.depends_on:
                    if provider not in selected:
                        selected.add(provider)
                        queue.append(provider)
        return selected

    def keys(self, selected: set[str], *, include_ungrouped=False) -> set[str]:
        return {key for key, module in self.membership.items()
                if module in selected or (module is None and include_ungrouped)}
