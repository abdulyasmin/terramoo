"""Identity-preserving key migrations with a resumable registry rename sequence."""

from copy import deepcopy
from pathlib import Path
import tomllib
from uuid import uuid4

from . import moolit, objdef
from .catalog import discover, safe_path
from .errors import MooError
from .installation import Store
from .modules import identifier
from .moolit import Obj, Ref
from .ownership import identity, operation_path, preflight, read_remote, receipt_path
from .packages import map_values
from .storage import atomic_write, fsync_dir, json_text, read_json, toml_text


def rewrite(text, mapping):
    if text is None:
        return None
    obj = objdef.parse(text)
    changed = False
    new_key = mapping.get(obj.key.lower(), obj.key)
    if new_key != obj.key:
        obj.key, changed = new_key, True

    def replace(value):
        nonlocal changed
        if isinstance(value, Ref) and value.kind == "@" and value.name.lower() in mapping:
            changed = True
            return Ref("@", mapping[value.name.lower()])
        return value

    map_values(obj, replace)
    return objdef.render(obj) if changed else text


def prepare(store, mapping, *, namespace=None):
    graph = store.validate()
    store.record_moves()
    mapping = {k.lower(): identifier(v, "new key") for k, v in mapping.items()}
    if "me" in {v.lower() for v in mapping.values()} or len(set(v.lower() for v in mapping.values())) != len(mapping):
        raise MooError("migration targets must be distinct and cannot be me")
    files = discover(store.world.objects_dir).by_key()
    for old, new in mapping.items():
        if old not in files:
            raise MooError(f"{old}: migration requires a desired file")
        if new.lower() in files and new.lower() not in mapping:
            raise MooError(f"{new}: migration target already exists")
    expected = {p: p.read_bytes() for p in store.desired_paths()}
    expected[store.path] = store.path.read_bytes() if store.path.exists() else None
    expected[store.world.dir / "world.toml"] = (store.world.dir / "world.toml").read_bytes()
    changes = {}
    for old in mapping:
        changes[files[old]] = None
    for key, path in files.items():
        text = path.read_text()
        output = rewrite(text, mapping)
        destination = path.with_name(mapping[key] + ".moo") if key in mapping else path
        if output != text or destination != path:
            changes[destination] = output
    from .player import paths as player_paths, rewrite_files
    expected.update({p: p.read_bytes() if p.exists() else None for p in player_paths(store.world)[:2]})
    player_changes = rewrite_files(store.world, mapping)
    changes.update(player_changes)
    lock, config = deepcopy(store.lock), deepcopy(store.config)
    for name, item in lock["instances"].items():
        affected = False
        for records in (item["objects"], item["tombstones"], item.get("removed_objects", {})):
            for record in records.values():
                if record["key"].lower() in mapping:
                    key = mapping[record["key"].lower()]
                    record.update(key=key, path=str(Path(record["path"]).with_name(key + ".moo")))
                    affected = True
                for field in ("baseline", "content"):
                    if field in record:
                        value = rewrite(record[field], mapping)
                        affected |= value != record[field]
                        record[field] = value
        for field in ("tombstones", "removed_objects"):
            if field in item:
                item[field] = {r["key"]: r for r in item[field].values()}
        for key, value in item["spec"]["bindings"].items():
            parsed = moolit.parse(value)
            if isinstance(parsed, Ref) and parsed.kind == "@" and parsed.name.lower() in mapping:
                value = "@" + mapping[parsed.name.lower()]
                item["spec"]["bindings"][key] = value
                config["packages"][name].setdefault("bindings", {})[key] = value
                affected = True
        if namespace and name == namespace[0]:
            item["spec"]["namespace"] = namespace[1]
            config["packages"][name]["namespace"] = namespace[1]
            affected = True
        if affected:
            item["revision"] = uuid4().hex
            item.setdefault("key_migrations", []).append(mapping)
    for record in lock["local_modules"].values():
        updated = {}
        for key, obj in record["objects"].items():
            obj["content"] = rewrite(obj["content"], mapping)
            obj["key"] = mapping.get(key, obj["key"])
            updated[obj["key"].lower()] = obj
        record["objects"] = updated
    store.validate_candidate(changes, lock, config)
    return {"mapping": mapping, "changes": changes, "lock": lock, "config": config, "expected": expected, "graph": graph}


def execute(store, proposal):
    world = store.world
    refs = world.refs()
    mapping = proposal["mapping"]
    remote, receipt = preflight(world, store, proposal["graph"], refs, set(mapping))
    for new in mapping.values():
        occupied = refs.registry_key(new)
        if occupied and occupied.lower() not in mapping:
            raise MooError(f"{new}: migration target is occupied in the registry")
        if new.lower() in remote["entries"] and new.lower() not in mapping:
            raise MooError(f"{new}: migration target is reserved by another deployment")
    token = uuid4().hex
    steps, final_steps = [], []
    for index, (old, new) in enumerate(mapping.items()):
        actual = refs.registry_key(old)
        if actual is None:
            continue  # A prepared definition has no remote identity to rename yet.
        nonce = refs.generation_for(actual)
        if not nonce:
            raise MooError(f"{actual}: adopt --verify before migrating")
        temporary = f"tmoo_migration_{token}_{index}"
        if refs.registry_key(temporary) or temporary in remote["entries"]:
            raise MooError("migration temporary key is occupied")
        obj = refs.registry[actual].num
        steps.append({"old": actual, "new": temporary, "object": obj, "generation": nonce})
        final_steps.append({"old": temporary, "new": new, "object": obj, "generation": nonce})
    intent = {"schema_version": 1, "kind": "migration", "world_id": store.lock["world_id"],
        "identity": identity(world), "old_epoch": remote["epoch"], "new_epoch": token,
        "entries": remote["entries"], "receipt": receipt, "steps": steps + final_steps, "completed": 0,
        "mapping": mapping, "lock": proposal["lock"], "config": proposal["config"],
        "changes": {str(p.relative_to(world.dir)): v for p, v in proposal["changes"].items()},
        "expected": {str(p.relative_to(world.dir)): v.hex() if v is not None else None for p, v in proposal["expected"].items()}}
    # Check every local byte before taking remote ownership.
    from .storage import transaction
    transaction(world.dir, {}, expected=proposal["expected"])
    atomic_write(operation_path(world), json_text(intent))
    return recover(world)


def recover(world):
    path = operation_path(world)
    intent = read_json(path)
    if (not isinstance(intent, dict) or intent.get("schema_version") != 1 or intent.get("kind") != "migration"
            or intent.get("identity") != identity(world) or not isinstance(intent.get("steps"), list)):
        raise MooError("invalid migration journal or wrong world")
    expected = {safe_path(world.dir, world.dir / p): bytes.fromhex(v) if v is not None else None for p, v in intent["expected"].items()}
    if Store(world).lock != intent["lock"]:
        from .storage import transaction
        transaction(world.dir, {}, expected=expected)
    remote = read_remote(world)
    if remote["epoch"] == intent["old_epoch"] and intent["completed"] == 0:
        if remote["entries"] != intent["entries"]:
            raise MooError("migration ownership changed before activation")
        world.eval(world.helper("tmoo_packages", '"begin"', world.transport.serialize(intent["world_id"]),
                   world.transport.serialize(intent["old_epoch"]), world.transport.serialize(intent["new_epoch"]),
                   world.transport.serialize(list(intent["entries"].values()))))
        remote = read_remote(world)
    if remote["epoch"] != intent["new_epoch"] or remote["world_id"] != intent["world_id"]:
        raise MooError("migration deployment token is stale; restore matching world metadata")
    expected_entries = deepcopy(intent["entries"])
    for index, step in enumerate(intent["steps"]):
        old, new = step["old"], step["new"]
        after = deepcopy(expected_entries)
        entry = after.pop(old.lower(), None)
        if entry:
            entry[0] = new
            after[new.lower()] = entry
        if index < intent["completed"]:
            expected_entries = after
            continue
        registry = world.read_registry()
        matches = [k for k, obj in registry.items() if obj.num == step["object"] and registry.generations.get(k) == step["generation"]]
        if matches not in ([old], [new]):
            raise MooError(f"migration identity for {old} changed; journal retained")
        remote = read_remote(world)
        if matches == [old]:
            if remote["entries"] != expected_entries:
                raise MooError("ownership differs before migration step")
            op = ["rename", list(registry).index(old) + 1, registry.revision, Obj(step["object"]), step["generation"], new]
            result = world.eval(world.helper("tmoo_apply", world.transport.serialize([["owned", intent["new_epoch"], op]])))
            if result != [[1, Obj(step["object"])]]:
                raise MooError(f"migration rename failed: {result}; use package recover")
        remote = read_remote(world)
        if remote["entries"] != after:
            raise MooError("ownership differs after migration step; journal retained")
        expected_entries = after
        intent["completed"] = index + 1
        atomic_write(path, json_text(intent))
    if read_remote(world)["entries"] != expected_entries:
        raise MooError("migration ownership does not match completed steps")
    # Verify all identities again before replacing any local definitions.
    registry = world.read_registry()
    for step in intent["steps"][len(intent["steps"]) // 2:]:
        if registry.get(step["new"]) != Obj(step["object"]) or registry.generations.get(step["new"]) != step["generation"]:
            raise MooError("migrated registry identity changed before local commit")
    store = Store(world)
    if store.lock != intent["lock"]:
        changes = {safe_path(world.dir, world.dir / p): v for p, v in intent["changes"].items()}
        expected = {safe_path(world.dir, world.dir / p): bytes.fromhex(v) if v is not None else None for p, v in intent["expected"].items()}
        store.commit(changes, intent["lock"], intent["config"], expected=expected)
    receipt = intent["receipt"]
    mapping = intent["mapping"]
    bindings = {}
    for key, record in receipt["bindings"].items():
        if record.get("removed") and key not in mapping and key in {v.lower() for v in mapping.values()}:
            continue  # The retired identity remains in installation history.
        key = mapping.get(key, record["key"])
        record["key"] = key
        # Definitions and input bindings changed; replan before marking applied.
        instance = record.get("instance")
        desired = intent["lock"]["instances"].get(instance)
        if key.lower() in {v.lower() for v in mapping.values()} or desired and record.get("revision") != desired["revision"]:
            record["revision"] = None
        bindings[key.lower()] = record
    receipt.update(epoch=intent["new_epoch"], identity=intent["identity"], bindings=bindings)
    atomic_write(receipt_path(world), json_text(receipt))
    for name, item in intent["lock"]["instances"].items():
        atomic_write(safe_path(world.dir, world.dir / ".packages" / name / "deployment.json"),
                     json_text({"schema_version": 1, "id": item["id"], "epoch": intent["new_epoch"],
                                "bindings": {k: v for k, v in bindings.items() if v["owner"] == item["id"]}}))
    world.save_state(registry)
    path.unlink()
    fsync_dir(path.parent)
    return "migrated keys without recreating objects; run plan to review remaining changes"


def rename_instance(store, requested, new):
    old = store.name(requested)
    identifier(new, "instance name")
    if new.lower() == "local" or new.lower() in {n.lower() for n in store.instances}:
        raise MooError("new instance name is reserved or already recorded")
    store.validate()
    store.record_moves()
    expected = {p: p.read_bytes() for p in store.desired_paths()}
    expected[store.path] = store.path.read_bytes()
    expected[store.world.dir / "world.toml"] = (store.world.dir / "world.toml").read_bytes()
    changes, lock, config = {}, deepcopy(store.lock), deepcopy(store.config)
    item = lock["instances"].pop(old)
    if item["state"] != "prepared":
        raise MooError("only active instances can be renamed")
    lock["instances"][new] = item
    declaration = config["packages"].pop(old)
    declaration["namespace"] = item["spec"]["namespace"]
    config["packages"][new] = declaration

    def address(value):
        return new.lower() + value[len(old):] if value.lower().startswith(old.lower() + "/") else value

    def manifest(text):
        if text is None:
            return None
        data = tomllib.loads(text)
        if data.get("instance", "").lower() == old.lower():
            data["instance"] = new
        for field in ("references", "depends_on"):
            if field in data:
                data[field] = [address(v) for v in data[field]]
        return toml_text(data)

    for name, record in lock["instances"].items():
        for alias, target in record["spec"]["dependencies"].items():
            if target == old.lower():
                record["spec"]["dependencies"][alias] = new.lower()
                config["packages"][name].setdefault("dependencies", {})[alias] = new
                record["dependencies"][alias]["instance"] = new
        for field in ("manifests", "removed_manifests"):
            for entry in record.get(field, {}).values():
                entry["baseline"] = manifest(entry["baseline"])
    for record in lock["local_modules"].values():
        for field in ("references", "depends_on"):
            record[field] = [address(v) for v in record.get(field, [])]
    old_root, new_root = store.world.objects_dir / "packages" / old, store.world.objects_dir / "packages" / new
    for path in store.desired_paths():
        destination = new_root / path.relative_to(old_root) if path.is_relative_to(old_root) else path
        content = manifest(path.read_text()) if path.name == "module.toml" else path.read_text()
        if path != destination:
            changes[path] = None
        if path != destination or content != path.read_text():
            changes[destination] = content
    for relative in ("base/source.json", "removal.json", "deployment.json"):
        path = safe_path(store.world.dir, store.world.dir / ".packages" / old / relative)
        if path.exists():
            expected[path] = path.read_bytes()
            data = path.read_text()
            if relative == "deployment.json":
                record = read_json(path)
                for binding in record["bindings"].values():
                    binding.update(instance=new, module=address(binding["module"]))
                data = json_text(record)
            changes[path] = None
            changes[safe_path(store.world.dir, store.world.dir / ".packages" / new / relative)] = data
    path = receipt_path(store.world)
    if path.exists():
        expected[path] = path.read_bytes()
        receipt = read_json(path)
        for record in receipt["bindings"].values():
            if record.get("instance") == old:
                record.update(instance=new, module=address(record["module"]))
        changes[path] = json_text(receipt)
    store.commit(changes, lock, config, expected=expected)
    return f"renamed instance {old} to {new}; namespace and live keys preserved"
