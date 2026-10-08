"""Read-side package identity checks and transactional world exports."""

from . import export, objdef
from .catalog import discover
from .errors import MooError
from .installation import Store
from .model import ordered_like
from .ownership import preflight, read_receipt


def pull(world, refs, keys, *, into=None):
    store = Store(world)
    graph = store.validate(parse=False, allow_missing=True)
    _, receipt = preflight(world, store, graph, refs, {k.lower() for k in keys})
    destinations = {}
    for name, item in store.instances.items():
        removed = {k.lower() for k in item["tombstones"]}
        for key in keys:
            if key.lower() in removed:
                raise MooError(f"{key}: pending removal; pull would restore a removed definition")
        for record in item["objects"].values():
            key = record["key"].lower()
            if key not in {k.lower() for k in keys}:
                continue
            applied = receipt["bindings"].get(key, {})
            if applied.get("revision") != item["revision"]:
                raise MooError(f"{record['key']}: desired package revision is not fully applied; apply before pull")
            if into:
                raise MooError("--into is only for missing standalone files")
            path = world.file_for(record["key"])
            destinations[key] = path if path.exists() else store.object_path(name, record["path"])
    tree = discover(world.objects_dir)
    expected = {p: p.read_bytes() for p in (*tree.objects, *tree.manifests)}
    expected[store.path] = store.path.read_bytes() if store.path.exists() else None
    expected[world.dir / "world.toml"] = (world.dir / "world.toml").read_bytes()
    live = export.export(world, refs, keys)
    changes = {}
    for key in keys:
        obj = live.get(key)
        if obj is None:
            raise MooError(f"{key}: live object is missing; local definition kept")
        path = destinations.get(key.lower()) or world.file_for(key, into=into)
        if key.lower() not in destinations and any(path.is_relative_to(world.objects_dir / "packages" / n) for n in store.instances):
            raise MooError("pull standalone definitions outside package installation directories")
        obj.key = path.stem
        if path.exists():
            try:
                ordered_like(obj, objdef.parse(path.read_text()))
            except ValueError:
                pass
        changes[path] = objdef.render(obj)
    # Export may introduce a new dependency. Preserve the requested live values
    # and report the declaration needed before the next apply.
    for name, item in store.instances.items():
        for record in item["objects"].values():
            if record["key"].lower() in destinations:
                record["path"] = str(destinations[record["key"].lower()].relative_to(world.objects_dir / "packages" / name))
    store.commit(changes, expected=expected, check_graph=False)
    try:
        store.validate()
    except MooError as error:
        print(f"exported definitions need a module declaration or file repair before apply: {error}")
    return len(changes)


def status(world, selected=None):
    store = Store(world)
    receipt = read_receipt(world, store)
    for name, item in sorted(store.instances.items()):
        records = list(item["objects"].values())
        if selected is not None:
            records = [r for r in records if r["key"].lower() in selected]
            if not records and not ({k.lower() for k in item["tombstones"]} & selected):
                continue
        applied = sum(receipt["bindings"].get(r["key"].lower(), {}).get("revision") == item["revision"] for r in records)
        print(f"package {name}: {item['package']} {item['version']} ({item['state']}); "
              f"{applied}/{len(records)} objects at desired revision; {len(item['tombstones'])} pending removals")
