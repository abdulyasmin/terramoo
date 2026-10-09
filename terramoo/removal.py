"""Checked teardown of explicitly selected ownership records."""

from __future__ import annotations

from . import apply, export, objdef, plan
from .errors import MooError
from .modules import components, references
from .ownership import owner_for, read_remote, operation_path
from .storage import atomic_write, json_text
from uuid import uuid4


def definitions(prepared):
    found = {}
    for item in prepared.store.instances.values():
        for key, record in item["tombstones"].items():
            found[key.lower()] = objdef.parse(record["content"])
    for item in prepared.store.lock["local_modules"].values():
        for key, record in item["objects"].items():
            found[key] = objdef.parse(record["content"])
    return found


def incoming_check(prepared, candidates):
    refs = prepared.refs
    refs.replace_registry(prepared.world.read_registry())
    from .player import incoming_check as player_incoming
    player_incoming(prepared.world, refs, candidates)
    survivors = [key for key in refs.registry if key.lower() not in candidates]
    live = export.export(prepared.world, refs, survivors)
    for key, obj in [*prepared.files.items(), *((k, v) for k, v in live.items() if v is not None)]:
        if key.lower() not in candidates and (incoming := references(obj, refs) & candidates):
            raise MooError(f"cannot remove {sorted(incoming)}: surviving managed object {key} still refers to it")


def retire(prepared, session, removed):
    world, store = prepared.world, prepared.store
    registry = world.read_registry()
    if any(k.lower() in removed for k in registry):
        raise MooError("a removed key reappeared before ownership retirement")
    entries = {key: row for key, row in session.entries.items() if key not in removed}
    epoch = uuid4().hex
    session.intent.update(old_epoch=session.epoch, new_epoch=epoch, entries=list(entries.values()),
                          bindings=session.receipt["bindings"], phase="begin", batch=None, nonces={})
    atomic_write(operation_path(world), json_text(session.intent))
    world.eval(world.helper("tmoo_packages", '"begin"', world.transport.serialize(store.lock["world_id"]),
               world.transport.serialize(session.epoch), world.transport.serialize(epoch), world.transport.serialize(list(entries.values()))))
    remote = read_remote(world)
    if remote["epoch"] != epoch or remote["entries"] != entries:
        raise MooError("ownership retirement has an unknown outcome; run package recover")
    session.epoch, session.entries = epoch, entries
    session.receipt["epoch"] = epoch
    for key in removed:
        if key in session.receipt["bindings"]:
            session.receipt["bindings"][key]["removed"] = True
    session.save_receipt()
    session.intent.update(phase="active")
    atomic_write(operation_path(world), json_text(session.intent))
    for item in store.instances.values():
        for key in list(item["tombstones"]):
            if key.lower() in removed:
                item.setdefault("removed_objects", {})[key] = item["tombstones"].pop(key)
        if item["state"] == "removing" and not item["tombstones"]:
            item["state"] = "removed"
    for item in store.lock["local_modules"].values():
        for key in set(item["objects"]) & removed:
            item.setdefault("removed_objects", {})[key] = item["objects"].pop(key)
    store.commit({})


def run(prepared, session, *, log=print):
    outcome = apply.Outcome()
    candidates = prepared.removals
    incoming_check(prepared, candidates)
    retained = definitions(prepared)
    edges = {key: references(retained[key], prepared.refs) & candidates if key in retained else set() for key in candidates}
    # Historical initialization edges are enforced between all candidate objects
    # in the corresponding modules, in addition to object-level references.
    by_module = {}
    for key in candidates:
        owner = owner_for(prepared.store, prepared.graph, key)
        if owner:
            by_module.setdefault(owner[2], set()).add(key)
    manifests = {}
    import tomllib
    for name, item in prepared.store.instances.items():
        for module, record in {**item.get("removed_manifests", {}), **item["manifests"]}.items():
            if record["baseline"]:
                manifests[f"{name}/{module}".lower()] = tomllib.loads(record["baseline"])
    manifests.update(prepared.store.lock["local_modules"])
    for mid, keys in by_module.items():
        for dep in manifests.get(mid, {}).get("depends_on", []):
            for key in keys:
                edges[key] |= by_module.get(dep.lower(), set())
    removed = set()
    for group in reversed(components(edges)):
        # Parent chains cannot cycle; reverse parent order within reference groups.
        group_files = {k: retained[k] for k in group if k in retained}
        ordered, cyclic = plan._topo(list(group_files), group_files)
        if cyclic:
            raise MooError(f"cannot order removal parent cycle: {sorted(cyclic)}")
        order = list(reversed(ordered)) + sorted(set(group) - set(ordered))
        for key in order:
            incoming_check(prepared, candidates - removed)
            actual = prepared.refs.registry_key(key)
            if actual is None:
                removed.add(key)
                continue
            obj = prepared.refs.registry[actual]
            owner = owner_for(prepared.store, prepared.graph, key)
            if owner:
                receipt = session.receipt["bindings"].get(key)
                if not receipt or receipt["object"] != obj.num or receipt["generation"] != prepared.refs.generation_for(actual):
                    raise MooError(f"{key}: deletion identity differs from the deployment receipt")
            result = apply.run(prepared.world, plan.Plan(destroys={actual: obj}), prepared.refs,
                               files=prepared.files, destroy=True, log=log)
            outcome.done.extend(result.done)
            outcome.failed.extend(result.failed)
            if result.failed:
                if removed:
                    retire(prepared, session, removed)
                return outcome
            removed.add(key)
    if removed or not candidates:
        retire(prepared, session, removed)
    return outcome
