"""Remote ownership epochs, durable mutation intents, and deployment receipts.

A local receipt alone cannot authorize deletion after a different checkout
has advanced the world. The toolbox keeps the matching epoch and ownership
records, and checks them again for each mutation.
"""

from __future__ import annotations

from copy import deepcopy

from . import objdef
from .catalog import safe_path
from .errors import MooError
from .installation import Store
from .storage import atomic_write, digest, fsync_dir, json_text, read_json
from uuid import uuid4


def operation_path(world):
    return safe_path(world.dir, world.dir / ".packages" / "operation.json")


def receipt_path(world):
    return safe_path(world.dir, world.dir / ".packages" / "deployment.json")


def read_remote(world):
    world.require_helper_version()
    raw = world.eval(world.helper("tmoo_packages", '"read"'))
    if (not isinstance(raw, list) or len(raw) != 4 or raw[0] != 1
            or not isinstance(raw[1], str) or not isinstance(raw[2], str) or not isinstance(raw[3], list)):
        raise MooError("malformed remote package state")
    entries = {}
    for row in raw[3]:
        if (not isinstance(row, list) or len(row) != 4 or any(not isinstance(v, str) or not v for v in row[:3])
                or type(row[3]) is not int or row[3] not in (0, 1) or row[0].lower() in entries):
            raise MooError("malformed remote ownership entry")
        entries[row[0].lower()] = row
    return {"world_id": raw[1], "epoch": raw[2], "entries": entries}


def identity(world):
    return {"player": world.player.num, "toolbox": world.toolbox.num, "endpoint": world.describe()}


def read_receipt(world, store):
    record = read_json(receipt_path(world))
    if record is None:
        return {"schema_version": 1, "world_id": store.lock["world_id"], "epoch": "", "identity": None, "bindings": {}}
    if (not isinstance(record, dict) or record.get("schema_version") != 1
            or record.get("world_id") != store.lock["world_id"] or not isinstance(record.get("epoch"), str)
            or not isinstance(record.get("bindings"), dict)):
        raise MooError("invalid deployment receipt; restore the matching world metadata")
    for key, binding in record["bindings"].items():
        if (not isinstance(binding, dict) or not isinstance(binding.get("key"), str) or binding["key"].lower() != key
                or not isinstance(binding.get("owner"), str) or not binding["owner"]
                or not isinstance(binding.get("generation"), str) or not binding["generation"]
                or not isinstance(binding.get("module"), str)
                or binding.get("object") is not None and type(binding["object"]) not in (int, str)):
            raise MooError("invalid object identity in deployment receipt")
    return record


def owner_for(store, graph, key):
    for instance, item in store.instances.items():
        if item["state"] == "removed":
            continue
        for record in item["objects"].values():
            if record["key"].lower() == key.lower():
                return item["id"], instance, f"{instance}/{record['module']}".lower(), item["revision"]
        for record in item["tombstones"].values():
            if record["key"].lower() == key.lower():
                return item["id"], instance, f"{instance}/{record['module']}".lower(), item["revision"]
    module = graph.membership.get(key.lower())
    if module and module.startswith("local/"):
        return "local:" + store.lock["world_id"], None, module, "local"
    for mid, record in store.lock["local_modules"].items():
        if key.lower() in record["objects"]:
            return "local:" + store.lock["world_id"], None, mid, "local"
    return None


def preflight(world, store, graph, refs, keys):
    if operation_path(world).exists():
        raise MooError("unfinished remote operation; run tmoo package recover")
    remote = read_remote(world)
    receipt = read_receipt(world, store)
    if receipt["identity"] is not None and receipt["identity"] != identity(world):
        raise MooError("deployment receipt belongs to another world/player/toolbox")
    if receipt["epoch"]:
        if remote["world_id"] != receipt["world_id"] or remote["epoch"] != receipt["epoch"]:
            raise MooError("remote deployment revision differs from this checkout; restore matching metadata or recover its recorded operation")
    elif remote["entries"]:
        raise MooError("remote world has owned objects but this checkout has no matching deployment receipt")
    for key in keys:
        owner = owner_for(store, graph, key)
        entry = remote["entries"].get(key.lower())
        actual = refs.registry_key(key)
        known = receipt["bindings"].get(key.lower())
        if entry is not None and (owner is None or entry[2] != owner[0]):
            raise MooError(f"{key}: remote object belongs to another installation")
        pending_recreation = bool(known and entry and actual and known.get("pending_nonce") == entry[1]
            and known.get("object") == refs.registry[actual].num
            and known.get("generation") == refs.generation_for(actual)
            and not world.eval(f"valid({refs.registry[actual]})"))
        if owner and owner[1] is not None and actual is not None:
            if (not isinstance(known, dict) or known.get("owner") != owner[0]
                    or known.get("object") != refs.registry[actual].num
                    or known.get("generation") != refs.generation_for(actual)
                    or entry is None or entry[1] != known["generation"] and not pending_recreation):
                raise MooError(f"{key}: occupied key has no matching installation receipt; use explicit package import")
        if entry and actual and entry[1] != refs.generation_for(actual) and not pending_recreation:
            raise MooError(f"{key}: generation differs from remote ownership")
    return remote, receipt


class Session:
    def __init__(self, world, store, graph, refs, files, keys, creates, destroys=()):
        self.world, self.store, self.graph, self.refs = world, store, graph, refs
        self.files = files
        self.keys = set(keys)
        self.destroy_keys = set(destroys)
        self.remote, self.receipt = preflight(world, store, graph, refs, self.keys | self.destroy_keys)
        self.epoch = uuid4().hex
        self.nonces = {key.lower(): uuid4().hex for key, *_ in creates}
        self.entries = deepcopy(self.remote["entries"])
        self.intent = None

    def start(self):
        bindings = deepcopy(self.receipt["bindings"])
        for key in self.keys | self.destroy_keys:
            owner = owner_for(self.store, self.graph, key)
            if owner is None:
                continue
            actual = self.refs.registry_key(key)
            nonce = self.nonces.get(key.lower()) or self.refs.generation_for(key)
            if nonce is None:
                # Never-created removals have no remote binding to claim.
                if actual is None and key in self.destroy_keys:
                    continue
                raise MooError(f"{key}: cannot claim an unverified registry binding")
            installed_key = actual or next((k for k in self.files if k.lower() == key.lower()), key)
            self.entries[key.lower()] = [installed_key, nonce, owner[0], int(key not in self.destroy_keys)]
            if key.lower() not in bindings or bindings[key.lower()]["owner"] != owner[0]:
                bindings[key.lower()] = {"key": installed_key, "owner": owner[0], "instance": owner[1],
                                        "module": owner[2], "object": None, "generation": nonce, "revision": None}
            bindings[key.lower()].update(instance=owner[1], module=owner[2])
            if key.lower() in self.nonces:
                bindings[key.lower()]["pending_nonce"] = nonce
        self.intent = {"schema_version": 1, "kind": "deployment", "world_id": self.store.lock["world_id"],
            "identity": identity(self.world), "old_epoch": self.remote["epoch"], "new_epoch": self.epoch,
            "entries": list(self.entries.values()), "nonces": self.nonces, "bindings": bindings,
            "phase": "begin", "batch": None, "desired": {k: digest(objdef.render(o)) for k, o in self.files.items() if k.lower() in self.keys}}
        atomic_write(operation_path(self.world), json_text(self.intent))
        self.world.eval(self.world.helper("tmoo_packages", '"begin"', self.world.transport.serialize(self.store.lock["world_id"]),
            self.world.transport.serialize(self.remote["epoch"]), self.world.transport.serialize(self.epoch),
            self.world.transport.serialize(list(self.entries.values()))))
        remote = read_remote(self.world)
        if remote["epoch"] != self.epoch or remote["entries"] != self.entries:
            raise MooError("ownership activation could not be reconciled; run package recover")
        self.receipt.update(epoch=self.epoch, identity=identity(self.world), bindings=bindings)
        self.save_receipt()
        self.intent["phase"] = "active"
        atomic_write(operation_path(self.world), json_text(self.intent))
        self.world._package_session = self

    def save_receipt(self):
        atomic_write(receipt_path(self.world), json_text(self.receipt))
        for name, item in self.store.instances.items():
            subset = {k: v for k, v in self.receipt["bindings"].items() if v["owner"] == item["id"]}
            path = safe_path(self.world.dir, self.world.dir / ".packages" / name / "deployment.json")
            atomic_write(path, json_text({"schema_version": 1, "id": item["id"], "epoch": self.epoch, "bindings": subset}))

    def before_batch(self, ops):
        self.intent["batch"] = ops
        self.intent["phase"] = "sending"
        # MOO values are serialized as literals so UUID objects and maps survive.
        self.intent["batch"] = self.world.transport.serialize(ops)
        atomic_write(operation_path(self.world), json_text(self.intent))
        return [["owned", self.epoch, op] for op in ops]

    def after_batch(self, ops, results):
        if not isinstance(results, list) or len(results) != len(ops) or any(
                not isinstance(r, list) or len(r) < 2 or r[0] not in (0, 1) for r in results):
            raise MooError("remote batch result is ambiguous; run package recover")
        if read_remote(self.world)["epoch"] != self.epoch:
            raise MooError("deployment token changed during a batch; run package recover")
        # Record identities before acknowledging completion of a create batch.
        registry = self.world.read_registry()
        for op, result in zip(ops, results):
            if result[0] != 1:
                continue
            if op[0] == "create":
                key = op[1]
                actual = next((k for k in registry if k.lower() == key.lower()), None)
                if actual and registry.generations.get(actual) == op[6] and registry[actual] == result[1]:
                    record = self.receipt["bindings"].get(key.lower())
                    if record:
                        record.update(object=registry[actual].num, generation=op[6])
                        record.pop("pending_nonce", None)
            elif op[0] == "destroy":
                key = op[1].lower()
                if key in self.receipt["bindings"]:
                    self.receipt["bindings"][key]["removed"] = True
        self.save_receipt()
        self.intent.update(phase="active", batch=None)
        atomic_write(operation_path(self.world), json_text(self.intent))

    def completed(self, keys, refs, *, successful):
        for key in keys:
            owner = owner_for(self.store, self.graph, key)
            actual = refs.registry_key(key)
            if owner is None or actual is None:
                continue
            record = self.receipt["bindings"].get(key.lower())
            if record:
                record.update(object=refs.registry[actual].num, generation=refs.generation_for(key))
                if successful:
                    obj = next(o for k, o in self.files.items() if k.lower() == key.lower())
                    record.update(revision=owner[3], desired_digest=digest(objdef.render(obj)))
        self.save_receipt()

    def finish(self):
        if self.intent["phase"] != "active":
            raise MooError("unresolved remote operation; run package recover")
        if read_remote(self.world)["epoch"] != self.epoch:
            raise MooError("deployment token changed before completion; recovery intent retained")
        self.world._package_session = None
        operation_path(self.world).unlink()
        fsync_dir(operation_path(self.world).parent)


def recover(world):
    path = operation_path(world)
    intent = read_json(path)
    if isinstance(intent, dict) and intent.get("kind") == "import":
        from .imports import recover as recover_import
        return recover_import(world)
    if isinstance(intent, dict) and intent.get("kind") == "migration":
        from .migrations import recover as recover_migration
        return recover_migration(world)
    store = Store(world)
    if (not isinstance(intent, dict) or intent.get("schema_version") != 1 or intent.get("kind") != "deployment"
            or intent.get("world_id") != store.lock["world_id"] or intent.get("identity") != identity(world)):
        raise MooError("invalid remote recovery intent or wrong world identity")
    if (not isinstance(intent.get("entries"), list) or not isinstance(intent.get("bindings"), dict)
            or not isinstance(intent.get("nonces"), dict) or not isinstance(intent.get("desired"), dict)
            or intent.get("phase") not in {"begin", "active", "sending"}
            or not all(isinstance(intent.get(k), str) for k in ("old_epoch", "new_epoch"))):
        raise MooError("incomplete deployment recovery intent")
    remote = read_remote(world)
    if remote["epoch"] == intent["old_epoch"] and intent["phase"] == "begin":
        path.unlink()
        world._package_session = None
        return "ownership activation did not occur; apply may be retried"
    if remote["epoch"] != intent["new_epoch"] or remote["world_id"] != intent["world_id"]:
        raise MooError("remote ownership revision conflicts with recovery intent; restore the matching checkout/database")
    if remote["entries"] != {entry[0].lower(): entry for entry in intent["entries"]}:
        raise MooError("remote ownership differs from the recovery intent")
    receipt = read_receipt(world, store)
    receipt.update(epoch=remote["epoch"], identity=intent["identity"], bindings=intent["bindings"])
    registry = world.read_registry()
    for key, record in receipt["bindings"].items():
        entry = remote["entries"].get(key)
        actual = next((k for k in registry if k.lower() == key), None)
        if actual is not None:
            nonce = registry.generations.get(actual)
            pending_old = (key in intent["nonces"] and nonce != intent["nonces"][key]
                           and nonce == record.get("generation") and registry[actual].num == record.get("object")
                           and not world.eval(f"valid({registry[actual]})"))
            if pending_old and entry and entry[1] == intent["nonces"][key] and record["owner"] == entry[2]:
                record["pending_nonce"] = intent["nonces"][key]
                record["revision"] = None
                continue
            if entry is None or nonce != entry[1] or record["owner"] != entry[2]:
                raise MooError(f"{key}: live binding conflicts with recorded ownership intent")
            # New creates are accepted only with the nonce fsynced before send.
            if key in intent["nonces"] and nonce != intent["nonces"][key]:
                raise MooError(f"{key}: created object has an unexpected generation")
            record.update(key=actual, object=registry[actual].num, generation=nonce)
        elif entry and not entry[3]:
            record["removed"] = True
        if key in {k.lower() for k in intent["desired"]}:
            record["revision"] = None  # Replan selected definitions before claiming completion.
    atomic_write(receipt_path(world), json_text(receipt))
    path.unlink()
    world._package_session = None
    fsync_dir(path.parent)
    return "reconciled remote identities; run plan before applying remaining changes"
