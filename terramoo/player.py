"""Selected player fields, three-way drift checks, and recoverable application."""

from copy import deepcopy
from dataclasses import dataclass, field
from uuid import uuid4

from . import moolit, playerdef
from .catalog import safe_path, read_snapshot
from .errors import MooError
from .model import PropDef, VerbDef, normalize
from .moolit import Obj, Ref, walk
from .ownership import identity
from .storage import atomic_write, digest, json_text, read_json, transaction, fsync_dir


def paths(world):
    return tuple(safe_path(world.dir, world.dir / p) for p in
                 ("player.moo", ".player/state.json", ".player/operation.json"))


def literal(value):
    return moolit.serialize(value, dialect=moolit.MOOR)


def unliteral(value):
    return moolit.parse(value, dialect=moolit.MOOR)


def resolve(refs, value):
    try:
        return refs.resolve(value)
    except (KeyError, ValueError) as e:
        raise MooError(f"player reference cannot be resolved: {e}") from None


def call(world, helper, *args):
    world.require_helper_version()
    return world.eval(world.helper(helper, *(world.transport.serialize(a) for a in args)))


def remote(world):
    state = call(world, "tmoo_player", "read")
    if (not isinstance(state, list) or len(state) != 7 or state[0] != 1
            or not isinstance(state[1], str) or type(state[2]) is not int or state[2] < 0
            or not isinstance(state[3], str) or type(state[4]) is not int
            or state[5] not in {"idle", "ready", "running", "done", "failed", "finished"}
            or not isinstance(state[6], list)):
        raise MooError("malformed player deployment state")
    return state


def load(world):
    path = paths(world)[0]
    try:
        return playerdef.parse(path.read_text()) if path.exists() else playerdef.PlayerDef()
    except (ValueError, OSError) as e:
        raise MooError(f"{path}: {e}") from None


def receipt(world):
    data = read_json(paths(world)[1])
    if data is None:
        return {"schema_version": 1, "world_id": uuid4().hex, "revision": 0,
                "identity": identity(world), "fields": {}}
    if (not isinstance(data, dict) or data.get("schema_version") != 1
            or data.get("identity") != identity(world) or not isinstance(data.get("world_id"), str)
            or type(data.get("revision")) is not int or not isinstance(data.get("fields"), dict)):
        raise MooError("invalid player receipt or wrong world/player/toolbox")
    for key, record in data["fields"].items():
        if not isinstance(record, dict) or not isinstance(record.get("snapshot"), str):
            raise MooError(f"invalid player field receipt: {key}")
        try:
            unliteral(record["snapshot"])
        except ValueError as e:
            raise MooError(f"invalid player snapshot: {key}: {e}") from None
    return data


def check_revision(data, state, *, refresh=False):
    if state[3]:
        raise MooError("unfinished remote player operation; run tmoo player recover in its checkout")
    if state[1] and state[1] != data["world_id"]:
        raise MooError("player belongs to another checkout; restore its .player/state.json")
    if not refresh and data["revision"] != state[2]:
        raise MooError("stale player receipt; inspect and pull the live player changes first")


def read_field(world, selector):
    if selector[0] == "setting" and ":" in selector[1]:
        prefix, option = playerdef.setting_name(selector[1]).split(":", 1)
        # LambdaCore publishes separate option objects; ToastCore groups them
        # in $options. Keep that dialect-specific lookup out of the helpers.
        try:
            package = world.eval(f"${prefix}_options")
        except MooError:
            package = world.eval(f'$options["{prefix}"]')
        if not isinstance(package, Obj):
            raise MooError("unsupported core option definitions")
        names = world.eval(f"{package}.names")
        if not isinstance(names, list) or option.lower() not in {str(n).lower() for n in names}:
            raise MooError(f"unsupported player option {selector[1]!r}; select a canonical option name")
    row = call(world, "tmoo_player_read", "field", selector)
    kind = selector[0]
    valid = isinstance(row, list) and row == [0]
    if isinstance(row, list) and row and row[0] == 1:
        if kind == "property":
            valid = (len(row) == 6 and row[1] in (0, 1) and row[2] in (0, 1)
                     and isinstance(row[3], Obj) and isinstance(row[4], str))
        elif kind == "verb":
            valid = (len(row) == 7 and type(row[1]) is int and row[1] > 0
                     and isinstance(row[2], Obj) and isinstance(row[3], str)
                     and isinstance(row[4], str) and isinstance(row[5], list) and len(row[5]) == 3
                     and isinstance(row[6], list) and all(isinstance(x, str) for x in row[6]))
        elif kind == "setting":
            valid = len(row) == 3 and isinstance(row[2], list)
        elif kind == "feature":
            valid = len(row) == 2 and row[1] in (0, 1)
    if not valid:
        raise MooError(f"malformed selected player field: {selector}")
    return row


def selectors(args):
    selected = []
    for kind in ("property", "verb", "setting", "feature"):
        for name in getattr(args, kind, ()) or ():
            if kind == "property":
                playerdef.property_name(name)
            elif kind == "setting":
                name = playerdef.setting_name(name.lower())
            elif kind == "feature":
                try:
                    name = playerdef.feature_ref(unliteral(name))
                except ValueError as e:
                    raise MooError(f"invalid feature reference: {e}") from None
            else:
                playerdef.safe_name(name)
            selected.append((kind, name))
    if len({playerdef.slot(k, n) for k, n in selected}) != len(selected):
        raise MooError("duplicate player selection")
    return selected


def forget(profile, kind, name):
    key = playerdef.slot(kind, name)
    profile.props = [p for p in profile.props if playerdef.slot("property", p.name) != key]
    profile.verbs = [v for v in profile.verbs if playerdef.slot("verb", v.names) != key]
    profile.settings = {n: v for n, v in profile.settings.items() if playerdef.slot("setting", n) != key}
    profile.features = [v for v in profile.features if playerdef.slot("feature", v) != key]
    profile.removals.pop(key, None)


def accept(profile, kind, name, row, refs):
    forget(profile, kind, name)
    if row == [0]:
        return
    if kind == "property":
        if row[2]:
            profile.removals[playerdef.slot(kind, name)] = ("clear", kind, name)
        else:
            profile.props.append(PropDef(name, refs.symbolize(row[5]), row[4], defined=bool(row[1])))
    elif kind == "verb":
        profile.verbs.append(VerbDef(row[4], list(row[6]), tuple(row[5]), row[3]))
    elif kind == "setting":
        profile.settings[name] = refs.symbolize(row[1])
    elif row[1]:
        profile.features.append(name)


def write_local(world, profile, data, *, expected=None):
    definition, state, _ = paths(world)
    transaction(world.dir, {definition: playerdef.render(profile), state: json_text(data)}, expected=expected)


def track(world, selected, *, pull=False):
    if not selected and not pull:
        raise MooError("select --property, --verb, --setting, or --feature")
    before = snapshots(world)
    profile, data, state, refs = load(world), receipt(world), remote(world), world.refs()
    check_revision(data, state, refresh=pull)
    if not selected:
        selected = [(k, n) for k, n, _ in profile.entries().values()]
    for kind, name in selected:
        if kind == "property" and name.lower() in world.ignore_props:
            raise MooError(f"player property {name} is excluded by ignore_props")
        key = playerdef.slot(kind, name)
        if key in profile.entries() and not pull:
            raise MooError(f"already tracking {key}; use player pull to accept live changes")
        row = read_field(world, [kind, resolve(refs, name)])
        if not row[0] and not pull:
            raise MooError(f"{key} does not exist directly on the player")
        if kind == "feature" and not row[1] and not pull:
            raise MooError("feature is not active; add a feature declaration to player.moo to activate it")
        accept(profile, kind, name, row, refs)
        data["fields"][key] = {"snapshot": literal(row)}
    data["revision"] = state[2]
    write_local(world, profile, data, expected={p: snap[0] if snap else None for p, snap in before.items()})
    return len(selected)


def untrack(world, selected):
    if not selected:
        raise MooError("select fields to stop tracking")
    before = snapshots(world)
    profile, data = load(world), receipt(world)
    check_revision(data, remote(world))
    for kind, name in selected:
        forget(profile, kind, name)
        data["fields"].pop(playerdef.slot(kind, name), None)
    write_local(world, profile, data, expected={p: snap[0] if snap else None for p, snap in before.items()})


def references(profile, refs=None):
    found = set()
    by_obj = {obj: key.lower() for key, obj in refs.registry.items()} if refs else {}
    def visit(value):
        if isinstance(value, Ref) and value.kind == "@" and value.name.lower() != "me":
            found.add(value.name.lower())
        elif refs:
            obj = resolve(refs, value) if isinstance(value, Ref) else value
            if isinstance(obj, Obj) and obj in by_obj:
                found.add(by_obj[obj])
        return value
    for value in playerdef.values(profile):
        walk(value, visit)
    return found


def check(world):
    profile = load(world)
    files = {k.lower() for k in world.load_files()}
    desired = deepcopy(profile)
    desired.removals = {}
    missing = references(desired) - files
    if missing:
        raise MooError(f"player refers to objects without files: {', '.join(sorted(missing))}")
    for kind, name, _ in profile.entries().values():
        if kind == "property" and name.lower() in world.ignore_props:
            raise MooError(f"player property {name} is excluded by ignore_props")
    return profile


def action_for(kind, value, refs):
    if kind == "property":
        return ["set", int(value.defined), resolve(refs, value.value), normalize(value.perms, "rwc")]
    if kind == "verb":
        return ["set", normalize(value.perms, "rwxd"), list(value.args), list(value.code)]
    if kind == "setting":
        return ["set", resolve(refs, value)]
    return ["set"]


def satisfied(kind, name, row, action):
    op = action[0]
    if op == "remove":
        return row == [0]
    if op == "clear":
        return row[0] and not row[1] and bool(row[2])
    if kind == "feature":
        return bool(row[1]) == (op != "detach")
    if not row[0]:
        return False
    if kind == "property":
        return row[1] == action[1] and not row[2] and row[5] == action[2] and (not row[1] or normalize(row[4], "rwc") == action[3])
    if kind == "verb":
        return row[4] == name and normalize(row[3], "rwxd") == action[1] and row[5:] == action[2:]
    return row[1] == action[1]


@dataclass
class Prepared:
    world: object
    profile: playerdef.PlayerDef
    data: dict
    state: list
    snapshots: dict
    bindings: list
    registry_revision: int
    package_epoch: str
    fields: list = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def changes(self):
        return [item for item in self.fields if item["change"]]

    @property
    def needs_receipt(self):
        return not self.state[1] or any(self.data["fields"].get(f["key"], {}).get("snapshot") != literal(f["before"]) for f in self.fields)


def snapshots(world):
    candidates = [*paths(world)[:2], world.dir / "world.toml", world.dir / "packages.lock.json"]
    return {p: read_snapshot(p) if p.exists() else None for p in candidates}


def verify(world, expected):
    if snapshots(world) != expected:
        raise MooError("player files or world metadata changed after planning; replan")


def prepare(world):
    if paths(world)[2].exists():
        raise MooError("unfinished player operation; run tmoo player recover")
    before = snapshots(world)
    profile, data, state, refs = check(world), receipt(world), remote(world), world.refs()
    check_revision(data, state)
    packages = call(world, "tmoo_packages", "read")
    bindings = []
    for key in sorted(references(profile, refs)):
        actual = refs.registry_key(key)
        if actual is None or not refs.generation_for(actual):
            raise MooError(f"player requires deployed, verified @{key}; apply its object/module first")
        bindings.append([actual, refs.registry[actual], refs.generation_for(actual)])
    prepared = Prepared(world, profile, data, state, before, bindings, refs.registry_revision, packages[2])
    selected = set()
    for key, (kind, name, value) in profile.entries().items():
        selector = [kind, resolve(refs, name)]
        resolved_key = literal(selector)
        if resolved_key in selected:
            raise MooError(f"{key}: multiple declarations select the same live player field")
        selected.add(resolved_key)
        row = read_field(world, selector)
        removal = profile.removals.get(key)
        action = [removal[0]] if removal else action_for(kind, value, refs)
        record = data["fields"].get(key)
        baseline = unliteral(record["snapshot"]) if record else None
        matches = satisfied(kind, name, row, action)
        fresh = row == [0] and kind in {"property", "verb"} or kind == "feature" and row == [1, 0]
        if baseline is None and (removal or not fresh):
            prepared.problems.append(f"{key}: track the existing field before managing it")
        elif baseline is not None and row != baseline and not matches:
            prepared.problems.append(f"{key}: live drift conflicts with the file; inspect and pull before editing")
        if not matches:
            try:
                call(world, "tmoo_player_write", "check", selector, row, action)
            except MooError as e:
                prepared.problems.append(f"{key}: {e}")
        prepared.fields.append({"key": key, "kind": kind, "name": name, "selector": selector,
                                "action": action, "before": row, "change": not matches})
    # Deleting local verbs shifts later numeric descriptors. Remove them last,
    # in descending order, so our own operations preserve subsequent snapshots.
    prepared.fields.sort(key=lambda f: (1, -f["before"][1]) if f["kind"] == "verb" and f["action"][0] == "remove" and f["before"][0] else (0, 0))
    verify(world, before)
    return prepared


def describe(prepared):
    out = [f"  ~ {f['action'][0]} player {f['key']}" for f in prepared.changes]
    out += [f"  ! {p}" for p in prepared.problems]
    if not out:
        out.append("no player changes" if not prepared.needs_receipt else "synchronize player receipt")
    return "\n".join(out)


def _encode_field(item):
    return {**{k: v for k, v in item.items() if k not in {"name", "selector", "action", "before"}},
            **{k: literal(item[k]) for k in ("name", "selector", "action", "before")}}


def _record_success(profile, data, item, row):
    kind, name = item["kind"], unliteral(item["name"])
    action = unliteral(item["action"])
    if not satisfied(kind, name, row, action):
        raise MooError(f"{item['key']}: setter did not produce the requested state; player recover requires review")
    item["after"] = literal(row)
    if action[0] in {"remove", "detach"}:
        forget(profile, kind, name)
        data["fields"].pop(item["key"], None)
    else:
        data["fields"][item["key"]] = {"snapshot": literal(row)}


def _save_intent(world, intent):
    atomic_write(paths(world)[2], json_text(intent))


def _finish(world, intent, *, accept_live=False):
    for item in intent["fields"]:
        if "after" not in item:
            continue
        row = read_field(world, unliteral(item["selector"]))
        after = unliteral(item["after"])
        same = row == after
        if item["kind"] == "verb" and row[0] and after[0]:
            same = row[2:] == after[2:]  # Our deletions can shift surviving descriptors.
        if not same:
            raise MooError(f"{item['key']}: changed after its operation; inspect then player recover --accept-live")
        if item["key"] in intent["data"]["fields"]:
            intent["data"]["fields"][item["key"]]["snapshot"] = literal(row)
        item["after"] = literal(row)
    expected = {}
    final = {"player.moo": intent["profile"].encode(), ".player/state.json": json_text(intent["data"]).encode()}
    for relative, checksum in intent.get("local_expected", {}).items():
        path = safe_path(world.dir, world.dir / relative)
        content = path.read_bytes() if path.exists() else None
        if ((digest(content) if content is not None else None) != checksum
                and not (relative in final and content == final[relative])):
            raise MooError(f"{relative} changed during player application; restore it before player recover")
        expected[path] = content
    intent["phase"] = "finishing"
    intent["accept_live"] = accept_live or intent.get("accept_live", False)
    _save_intent(world, intent)
    state = remote(world)
    if state[3]:
        call(world, "tmoo_player", "finish", intent["token"], int(intent["accept_live"]))
    elif state[2] != intent["data"]["revision"]:
        raise MooError("player revision changed during recovery")
    definition = paths(world)[0]
    if (definition.read_text() if definition.exists() else None) not in (intent["original"], intent["profile"]):
        raise MooError("player.moo changed during application; restore it before player recover")
    write_local(world, playerdef.parse(intent["profile"]), intent["data"], expected=expected)
    paths(world)[2].unlink()
    fsync_dir(paths(world)[2].parent)


def apply(prepared):
    world = prepared.world
    if prepared.problems:
        raise MooError("fix player problems before applying")
    verify(world, prepared.snapshots)
    if not prepared.changes and not prepared.needs_receipt:
        return 0
    profile, data = deepcopy(prepared.profile), deepcopy(prepared.data)
    data["revision"] = prepared.state[2] + 1
    token = uuid4().hex
    intent = {"schema_version": 1, "identity": identity(world), "token": token, "phase": "begin",
              "old_revision": prepared.state[2], "data": data, "profile": playerdef.render(profile),
              "original": paths(world)[0].read_text() if paths(world)[0].exists() else None,
              "local_expected": {str(p.relative_to(world.dir)): digest(snap[0]) if snap else None for p, snap in prepared.snapshots.items()},
              "fields": [_encode_field(f) for f in prepared.fields], "completed": 0, "sending": None}
    _save_intent(world, intent)
    call(world, "tmoo_player", "begin", data["world_id"], prepared.state[2], token,
         prepared.registry_revision, prepared.package_epoch)
    intent["phase"] = "active"
    _save_intent(world, intent)
    step = 0
    for i, item in enumerate(intent["fields"]):
        verify(world, prepared.snapshots)
        selector, action, expected = (unliteral(item[k]) for k in ("selector", "action", "before"))
        row = read_field(world, selector)
        if row != expected:
            raise MooError(f"{item['key']}: changed after planning; run player recover")
        if item["change"]:
            step += 1
            intent.update(sending={"index": i, "step": step}, phase="sending")
            _save_intent(world, intent)
            state = call(world, "tmoo_player", "apply", token, step, selector, expected, action, prepared.bindings)
            if state[5] != "done" or state[6][0] != 1:
                raise MooError(f"{item['key']}: player operation failed; run player recover: {state[6]}")
            row = state[6][1]
        _record_success(profile, data, item, row)
        intent.update(completed=i + 1, sending=None, phase="active", profile=playerdef.render(profile))
        _save_intent(world, intent)
    _finish(world, intent)
    return step


def stage_removal(world, selected, action):
    before = snapshots(world)
    profile, data = load(world), receipt(world)
    check_revision(data, remote(world))
    if not selected:
        raise MooError("select a tracked player field")
    for kind, name in selected:
        key = playerdef.slot(kind, name)
        if key not in data["fields"]:
            raise MooError(f"{key}: only previously tracked fields can be removed")
        if action == "clear" and kind != "property" or action == "detach" and kind != "feature" or action == "remove" and kind not in ("property", "verb"):
            raise MooError("invalid player removal selection")
        forget(profile, kind, name)
        profile.removals[key] = (action, kind, name)
    write_local(world, profile, data, expected={p: snap[0] if snap else None for p, snap in before.items()})


def recover(world, *, accept_live=False):
    intent = read_json(paths(world)[2])
    if (not isinstance(intent, dict) or intent.get("schema_version") != 1
            or intent.get("identity") != identity(world) or not isinstance(intent.get("token"), str)
            or not isinstance(intent.get("fields"), list) or not isinstance(intent.get("data"), dict)):
        raise MooError("missing/invalid player recovery intent or wrong world identity")
    state = remote(world)
    if state[2] == intent["old_revision"] and not state[3] and intent["phase"] == "begin":
        paths(world)[2].unlink()
        return "player deployment did not begin; replan"
    if state[2] != intent["data"]["revision"] or state[1] != intent["data"]["world_id"] or state[3] not in ("", intent["token"]):
        raise MooError("player recovery revision conflicts with the live world")
    if accept_live:
        profile = playerdef.parse(intent["profile"])
        refs = world.refs()
        for item in intent["fields"]:
            row = read_field(world, unliteral(item["selector"]))
            accept(profile, item["kind"], unliteral(item["name"]), row, refs)
            intent["data"]["fields"][item["key"]] = {"snapshot": literal(row)}
            item["after"] = literal(row)
        intent["profile"] = playerdef.render(profile)
        _finish(world, intent, accept_live=True)
        return "accepted inspected player state without replaying callbacks; run player plan"
    if intent["phase"] == "finishing":
        _finish(world, intent, accept_live=accept_live)
        return "finished player receipt transaction"
    profile = playerdef.parse(intent["profile"])
    sending = intent["sending"]
    if state[5] in {"running", "failed"} and not accept_live:
        raise MooError("callback outcome is uncertain; inspect the player, then use player recover --accept-live; callbacks are never replayed")
    if sending:
        item = intent["fields"][sending["index"]]
        row = read_field(world, unliteral(item["selector"]))
        if state[4] == sending["step"] and state[5] == "done":
            if row != state[6][1]:
                raise MooError("player changed after the recorded result; inspect then recover --accept-live")
            _record_success(profile, intent["data"], item, row)
        elif state[4] >= sending["step"]:
            raise MooError("player operation outcome requires explicit inspection")
    intent["profile"] = playerdef.render(profile)
    _finish(world, intent, accept_live=accept_live)
    return "reconciled player operation without replaying callbacks; run player plan"


def incoming_check(world, refs, candidates):
    revision = remote(world)[2]
    selected = []
    profile = load(world)
    incoming = references(profile, refs) & {k.lower() for k in candidates}
    if incoming:
        raise MooError(f"player.moo still refers to {sorted(incoming)}; detach/clear or untrack before removal")
    targets = {refs.registry[actual] for key in candidates if (actual := refs.registry_key(key)) is not None}
    for kind, name, _ in profile.entries().values():
        if kind not in {"property", "setting"}:
            continue
        selector = [kind, resolve(refs, name)]
        row = read_field(world, selector)
        selected.append([selector, row])
        if not row[0]:
            continue
        value = row[5] if kind == "property" else row[1]
        hits = []
        walk(value, lambda v: hits.append(v) or v if isinstance(v, Obj) and v in targets else v)
        if hits:
            raise MooError(f"live player {kind} {name} still refers to an object pending removal; apply player changes first")
    active = call(world, "tmoo_player_read", "features")
    attached = [k for k in candidates if refs.registry.get(refs.registry_key(k)) in active]
    if attached:
        raise MooError(f"detach player features before removal: {', '.join(sorted(attached))}")
    return [revision, selected]


def rewrite_files(world, mapping):
    """Include player references and selector receipts in an object-key transaction."""
    definition, state, _ = paths(world)
    if not definition.exists():
        return {}
    profile = load(world)
    def replace(value):
        if isinstance(value, Ref) and value.kind == "@" and value.name.lower() in mapping:
            return Ref("@", mapping[value.name.lower()])
        return value
    playerdef.map_refs(profile, replace)
    changes = {definition: playerdef.render(profile)}
    data = read_json(state)
    if data:
        def migrated(key):
            old = key[len("feature:@"):] if key.startswith("feature:@") else None
            return playerdef.slot("feature", Ref("@", mapping[old])) if old in mapping else key
        data["fields"] = {migrated(k): v for k, v in data["fields"].items()}
        changes[state] = json_text(data)
    return changes
