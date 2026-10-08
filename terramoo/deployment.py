"""Whole-world context with explicit module mutation scopes."""

from __future__ import annotations

from dataclasses import dataclass

from . import apply, export, plan, objdef
from .catalog import discover, read_snapshot
from .errors import MooError
from .modules import Modules, object_values, references
from .moolit import Obj, Ref, walk
from .refs import Refs
from .installation import Store
from .ownership import Session, owner_for, preflight
from .storage import atomic_write, json_text


def has_selectors(args) -> bool:
    return bool(getattr(args, "module", ()) or getattr(args, "package", ()))


def module_graph(world, files, refs=None):
    tree = discover(world.objects_dir)
    instances = set()
    if (world.dir / "packages.lock.json").exists():
        from .installation import Store
        store = Store(world)
        store.validate(parse=bool(files))
        instances = {n for n, r in store.instances.items() if r["state"] == "prepared"}
    return Modules.build(tree, files, refs=refs, instances=instances)


def selected_keys(world, args, files=None, *, with_deps=False, allow_missing=False):
    if getattr(args, "keys", None) and has_selectors(args):
        raise MooError("explicit keys cannot be combined with --module or --package")
    store = Store(world)
    graph = store.validate(parse=False, allow_missing=True) if allow_missing else module_graph(world, {} if files is None else files)
    selected, explicit = scopes(graph, store, args, with_deps=with_deps)
    keys = graph.keys(selected, include_ungrouped=not has_selectors(args))
    keys |= removal_candidates(store, files if files is not None else {k: None for k in graph.membership}, explicit)
    return graph, keys


def scopes(graph, store, args, *, with_deps):
    historic = set(store.lock["local_modules"])
    for name, item in store.instances.items():
        historic |= {f"{name}/{module}".lower() for module in set(item["manifests"]) | set(item.get("removed_manifests", {}))}
    explicit = set()
    for name in getattr(args, "module", ()):
        mid = (name if "/" in name else f"local/{name}").lower()
        if mid not in graph.modules and mid not in historic:
            raise MooError(f"unknown module {name!r}")
        explicit.add(mid)
    for requested in getattr(args, "package", ()):
        name = store.name(requested)
        explicit |= {mid for mid in set(graph.modules) | historic if mid.startswith(name.lower() + "/")}
    if not has_selectors(args):
        explicit = set(graph.modules) | historic
    selected = explicit & set(graph.modules)
    if with_deps:
        queue = list(selected)
        while queue:
            module = graph.modules[queue.pop()]
            for dep in module.references | module.depends_on:
                if dep not in selected:
                    selected.add(dep)
                    queue.append(dep)
    return selected, explicit


def removal_candidates(store, files, explicit):
    present = {k.lower() for k in files}
    candidates = set()
    for name, item in store.instances.items():
        for key, obj in item["tombstones"].items():
            if f"{name}/{obj['module']}".lower() in explicit and key.lower() not in present:
                candidates.add(key.lower())
    for module, record in store.lock["local_modules"].items():
        if module in explicit:
            candidates |= set(record["objects"]) - present
    return candidates


def validate_properties(world, files):
    for key, obj in files.items():
        ignored = [p.name for p in obj.props if p.name.lower() in world.ignore_props]
        if ignored:
            raise MooError(f"{key}: desired runtime properties are excluded by ignore_props: {', '.join(ignored)}")


def snapshot(world):
    tree = discover(world.objects_dir)
    paths = [*tree.objects, *tree.manifests]
    paths.extend(p for p in (world.dir / "world.toml", world.dir / "packages.lock.json") if p.exists())
    return {path: read_snapshot(path) for path in paths}


def verify_snapshot(world, expected):
    if snapshot(world) != expected:
        raise MooError("world files changed after planning; replan before continuing")


def live_definitions(world, refs, files):
    return export.export(world, refs, [k for key in files if (k := refs.registry_key(key)) is not None])


@dataclass
class Prepared:
    world: object
    refs: Refs
    files: dict
    graph: Modules
    selected: set[str]
    keys: set[str]
    snapshots: dict
    plan: plan.Plan
    scoped: bool
    store: Store
    removals: set[str]
    explicit: set[str]

    def run(self, *, destroy=False, log=print):
        result = apply.Outcome()
        verify_snapshot(self.world, self.snapshots)
        for mid in self.selected:
            module = self.graph.modules[mid]
            if module.instance:
                continue
            record = self.store.lock["local_modules"].setdefault(mid, {"objects": {}})
            record.update(path=str(module.path.relative_to(self.world.objects_dir)),
                          references=sorted(module.references), depends_on=sorted(module.depends_on))
            for key in module.keys:
                for other, previous in self.store.lock["local_modules"].items():
                    if other != mid:
                        previous["objects"].pop(key.lower(), None)
                obj = self.files[key]
                record["objects"][key.lower()] = {"key": key, "content": objdef.render(obj)}
        self.store.record_moves()
        atomic_write(self.store.path, json_text(self.store.lock))
        self.snapshots[self.store.path] = read_snapshot(self.store.path)
        session = Session(self.world, self.store, self.graph, self.refs, self.files, self.keys,
                          self.plan.creates, self.removals if destroy else ())
        session.start()
        failed_modules = set()
        groups = [group for group in self.graph.groups if set(group) & self.selected]
        groups.append(())  # Ungrouped definitions retain their whole-world behavior.
        for group in groups:
            keys = self.graph.keys(set(group), include_ungrouped=not group and not self.scoped) & self.keys
            if not keys:
                continue
            dependencies = set().union(*(self.graph.modules[m].references | self.graph.modules[m].depends_on for m in group))
            if dependencies & failed_modules:
                failed_modules.update(group)
                result.failed.append((", ".join(group), "dependency group failed"))
                continue
            verify_snapshot(self.world, self.snapshots)
            self.refs.replace_registry(self.world.read_registry())
            live = live_definitions(self.world, self.refs, self.files)
            stage_refs = Refs(self.refs.player, self.refs.snapshot(), self.refs.sysrefs)
            stage = plan.build(self.files, live, stage_refs, selected=keys, destroy_keys=set())
            if stage.problems:
                failed_modules.update(group)
                result.failed.extend((", ".join(group), p) for p in stage.problems)
                continue
            log(f"applying modules: {', '.join(group) or '(ungrouped)'}")
            outcome = apply.run(self.world, stage, stage_refs, files=self.files,
                                stop_on_create_failure=True, replan_keys=keys, log=log)
            result.done.extend(outcome.done)
            result.failed.extend(outcome.failed)
            session.completed(keys, stage_refs, successful=not outcome.failed)
            if outcome.failed:
                failed_modules.update(group)
        empty_removals = any(item["state"] == "removing" and not item["tombstones"] and any(
            mid.startswith(name.lower() + "/") for mid in self.explicit) for name, item in self.store.instances.items())
        if destroy and (self.removals or empty_removals) and not result.failed:
            verify_snapshot(self.world, self.snapshots)
            from .removal import run
            outcome = run(self, session, log=log)
            result.done.extend(outcome.done)
            result.failed.extend(outcome.failed)
        session.finish()
        return result


def prepare(world, args) -> Prepared:
    if getattr(args, "keys", None) and has_selectors(args):
        raise MooError("explicit keys cannot be combined with module/package selectors")
    before = snapshot(world)
    files = world.load_files()
    validate_properties(world, files)
    refs = world.refs()
    graph = module_graph(world, files, refs)
    store = Store(world)
    store.validate()
    scoped = has_selectors(args)
    selected, explicit = scopes(graph, store, args, with_deps=True)
    keys = graph.keys(selected, include_ungrouped=not scoped)
    removals = removal_candidates(store, files, explicit)
    if not scoped:
        removals |= {key.lower() for key in refs.registry} - {key.lower() for key in files}
    _, receipt = preflight(world, store, graph, refs, keys | removals)
    live = live_definitions(world, refs, files)
    pending = plan.build(files, live, refs, selected=keys,
                         destroy_keys=removals)
    new = {k.lower() for k, *_ in pending.creates}
    replaced = {refs.registry[k]: k for key in new if (k := refs.registry_key(key)) is not None}
    for key, obj in files.items():
        if key.lower() not in keys and (affected := references(obj, refs) & new):
            pending.problems.append(f"{key}: references recreated/new {sorted(affected)}; also select {graph.membership[key.lower()] or 'the whole world'}")
        def check_identity(value):
            actual = refs.resolve_ref(value) if isinstance(value, Ref) and value.kind == "$" else value
            if isinstance(actual, Obj) and actual in replaced:
                pending.problems.append(f"{key}: {value} names the old identity of @{replaced[actual]}; use the managed @key before recreation")
            return value
        for value in object_values(obj):
            walk(value, check_identity)
    needs_receipts = not store.path.exists() or not receipt["epoch"]
    from .storage import digest
    for key, obj in files.items():
        if key.lower() not in keys:
            continue
        owner = owner_for(store, graph, key)
        record = receipt["bindings"].get(key.lower(), {})
        if owner and (record.get("revision") != owner[3] or record.get("desired_digest") != digest(objdef.render(obj))):
            needs_receipts = True
    if needs_receipts or removals or any(item["state"] == "removing" and any(mid.startswith(name.lower() + "/") for mid in explicit)
                                       for name, item in store.instances.items()):
        pending.warnings.append("synchronize deployment ownership and per-object receipts")
    verify_snapshot(world, before)
    return Prepared(world, refs, files, graph, selected, keys, before, pending, scoped, store, removals, explicit)
