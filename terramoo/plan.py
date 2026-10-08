"""What would change: the files against the live MOO, as a list of ops.

Pure over plain data, so it is where the tests are.  An op is a tuple whose
first element names a `tmoo_apply` case; object arguments are `Ref("@", key)`
until apply resolves them, so a plan can name objects it is about to create.
Values are compared after resolving references on the file side, so
`@gatehouse` in a file equals `#171` in the MOO when the registry says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import ObjectDef, PropDef, normalize
from .moolit import Obj, Ref, walk
from .refs import Refs, UnresolvedRef


@dataclass
class Plan:
    creates: list[tuple[str, object, str]] = field(default_factory=list)  # (key, parent, name)
    gone: dict[str, Obj] = field(default_factory=dict)  # registry entries the MOO no longer has
    ops: list[tuple] = field(default_factory=list)
    destroys: dict[str, Obj] = field(default_factory=dict)  # orphan bindings at planning time
    warnings: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.creates or self.ops or self.destroys)


@dataclass(frozen=True)
class VerbTarget:
    """A live numeric verb descriptor with its human-facing name."""

    index: int
    label: str

    def __str__(self) -> str:
        return self.label


def _owner(refs: Refs, owner, *, live: bool = False):
    """An owner as a number; None means the player."""
    return refs.player if owner is None else refs.resolve_ref(owner, live=live)


def build(files: dict[str, ObjectDef], live: dict[str, ObjectDef | None], refs: Refs,
          *, selected: set[str] | None = None, destroy_keys: set[str] | None = None) -> Plan:
    plan = Plan()
    broken: set[str] = set()
    file_keys = _folded_names(files, "file keys")
    live_keys = _folded_names(live, "live keys")
    selected_names = {key.lower() for key in selected} if selected is not None else set(file_keys)
    unknown = selected_names - set(file_keys)
    if unknown:
        plan.problems.append(f"selected keys have no definition: {', '.join(sorted(unknown))}")
    wanted = {key: obj for key, obj in files.items() if key.lower() in selected_names}
    for key in sorted(refs.legacy_keys):
        plan.warnings.append(
            f"registry key {key!r} is legacy; rename it with `tmoo rename-key {key!r} NEW`"
        )

    def live_for(key: str):
        live_key = live_keys.get(key.lower())
        return live.get(live_key) if live_key is not None else None

    def target_for(key: str) -> str:
        return refs.registry_key(key) or key

    # A reference to a key with no file would dangle as soon as that key is
    # destroyed (or would never be created): say so before anything runs.
    for key, obj in files.items():
        missing = sorted(n for n in _at_refs(obj) if n.lower() != "me" and n.lower() not in file_keys)
        if missing:
            plan.problems.append(f"{key}: refers to {', '.join('@' + n for n in missing)}, which has no file")
            broken.add(key)

    # Creates, parents first.  A parent that is itself a new object is
    # passed by registry name; the helper resolves it from what it just made.
    new_keys = [k for k in wanted if refs.registry_key(k) is None or live_for(k) is None]
    plan.gone = {
        target_for(k): refs.registry[refs.registry_key(k)]
        for k in new_keys
        if refs.registry_key(k) is not None
    }
    refs.pending = {target_for(k) for k in new_keys}
    ordered, cyclic = _topo(new_keys, files)
    for key in sorted(cyclic):
        plan.problems.append(f"{key}: its parent chain loops back to itself")
        broken.add(key)
    for key in ordered:
        obj = files[key]
        parent = obj.parent
        if (
            isinstance(parent, Ref)
            and parent.kind == "@"
            and parent.name.lower() in {name.lower() for name in broken}
            and key not in broken
        ):
            plan.problems.append(f"{key}: parent @{parent.name} cannot be created")
            broken.add(key)
        if key in broken:
            continue
        if isinstance(parent, Ref) and parent.kind == "@" and parent.name.lower() in {k.lower() for k in new_keys}:
            parent_arg = target_for(file_keys[parent.name.lower()])
        else:
            try:
                parent_arg = refs.symbolize_obj(refs.resolve_ref(parent))
            except UnresolvedRef as e:
                plan.problems.append(f"{key}: parent {e}")
                broken.add(key)
                continue
        plan.creates.append((target_for(key), parent_arg, obj.name))

    wanted_exits = _exit_classes(files, refs, live=False)
    current_exits = _exit_classes(
        {key: obj for key, obj in live.items() if obj is not None}, refs, live=True
    )
    for key in wanted:
        if key in broken:
            continue
        obj = files[key]
        current = live_for(key)
        try:
            ops = diff_object(
                target_for(key), obj, current, refs,
                was_exit=current_exits.get(key.lower(), wanted_exits.get(key.lower(), False)),
                will_exit=wanted_exits.get(key.lower(), False),
            )
        except (UnresolvedRef, ValueError) as e:
            plan.problems.append(f"{key}: {e}")
            continue
        if ops:
            plan.ops.extend(ops)
        elif current is not None:
            plan.unchanged.append(key)

    allowed_destroy = {k.lower() for k in destroy_keys} if destroy_keys is not None else (
        {k.lower() for k in refs.registry} if selected is None else set()
    )
    for key in refs.registry:
        if key.lower() not in file_keys and key.lower() in allowed_destroy:
            plan.destroys[key] = refs.registry[key]
    if plan.ops or plan.creates:
        plan.ops.append(("link", [Ref("@", target_for(k)) for k in wanted]))
    return plan


def _folded_names(values, label: str) -> dict[str, str]:
    folded: dict[str, str] = {}
    for name in values:
        key = name.lower()
        if key in folded:
            raise ValueError(f"{label} {folded[key]!r} and {name!r} differ only in case")
        folded[key] = name
    return folded


def _at_refs(obj: ObjectDef) -> set[str]:
    found: set[str] = set()

    def leaf(v):
        if isinstance(v, Ref) and v.kind == "@":
            found.add(v.name)
        return v

    for v in (obj.parent, obj.location, obj.owner, *(p.value for p in obj.props), *(p.owner for p in obj.props),
              *(v.owner for v in obj.verbs)):
        walk(v, leaf)
    return found


def _exit_classes(objects: dict[str, ObjectDef], refs: Refs, *, live: bool) -> dict[str, bool]:
    """Classify managed objects from their declared parent chains."""
    by_name = {key.lower(): obj for key, obj in objects.items()}
    by_object = {obj: key.lower() for key, obj in refs.registry.items()}
    exit_class = refs.sysrefs.get("exit")
    found: dict[str, bool] = {}

    def classify(key: str, stack: set[str]) -> bool:
        folded = key.lower()
        if folded in found:
            return found[folded]
        if folded in stack or folded not in by_name:
            return False
        parent = by_name[folded].parent
        try:
            resolved = refs.resolve_ref(parent, live=live)
        except UnresolvedRef:
            resolved = None
        if exit_class is not None and resolved == exit_class:
            result = True
        else:
            parent_key = None
            if isinstance(parent, Ref) and parent.kind == "@":
                parent_key = parent.name.lower()
            elif isinstance(resolved, Obj):
                parent_key = by_object.get(resolved)
            result = bool(parent_key and classify(parent_key, stack | {folded}))
        found[folded] = result
        return result

    for key in by_name:
        classify(key, set())
    return found


def _topo(keys: list[str], files: dict[str, ObjectDef]) -> tuple[list[str], set[str]]:
    """`keys` parents first, and the keys whose parent chain is a loop."""
    out, seen, cyclic = [], set(), set()
    by_name = _folded_names(files, "file keys")
    selected = {key.lower() for key in keys}

    def visit(k, stack):
        folded = k.lower()
        if folded in seen:
            return
        folded_stack = [name.lower() for name in stack]
        if folded in folded_stack:
            cyclic.update(stack[folded_stack.index(folded):])
            return
        parent = files[k].parent
        if isinstance(parent, Ref) and parent.kind == "@" and parent.name.lower() in selected:
            visit(by_name[parent.name.lower()], stack + [k])
        seen.add(folded)
        out.append(k)

    for k in keys:
        visit(k, [])
    return out, cyclic


def diff_object(
    key: str,
    want: ObjectDef,
    have: ObjectDef | None,
    refs: Refs,
    *,
    was_exit: bool | None = None,
    will_exit: bool | None = None,
) -> list[tuple]:
    """Ops that turn `have` (live) into `want` (file).  Both may carry
    `Ref`s (export symbolizes what it can), so every comparison resolves
    both sides to numbers first.
    `have` is None for an object that does not exist yet: every attribute
    is then set, on the object the create op registers under `key`."""
    target = Ref("@", key)
    ops: list[tuple] = []
    file_ref = refs.resolve_ref
    exit_class = refs.sysrefs.get("exit")
    if will_exit is None:
        will_exit = exit_class is not None and file_ref(want.parent) == exit_class
    if was_exit is None:
        was_exit = (
            have is not None
            and exit_class is not None
            and refs.resolve_ref(have.parent, live=True) == exit_class
        )

    def live_ref(v):  # a key being recreated is still its old number on the MOO
        return refs.resolve_ref(v, live=True)

    if have is not None and want.name != have.name:
        ops.append(("name", target, want.name))
    if have is not None and file_ref(want.parent) != live_ref(have.parent):
        ops.append(("chparent", target, file_ref(want.parent)))
    if file_ref(want.location) != (live_ref(have.location) if have else Obj(-1)):
        ops.append(("move", target, file_ref(want.location)))
    want_flags = normalize(want.flags, "rwf")
    if want_flags != (normalize(have.flags, "rwf") if have else ""):
        ops.append(("flags", target, want_flags))

    _folded_names((p.name for p in want.props), f"{key} file properties")
    have_props = {
        name: next(p for p in (have.props if have else []) if p.name == original)
        for name, original in _folded_names(
            (p.name for p in (have.props if have else [])), f"{key} live properties"
        ).items()
    }

    def unlink_old(prop: PropDef, destination: list[tuple] | None = None):
        destination = ops if destination is None else destination
        relation = {"source": "exit", "dest": "entrance"}.get(prop.name.lower())
        old = refs.resolve(prop.value, live=True) if relation is not None else None
        # Only an exit's room links need undoing; a `source` holding a string
        # or list on some other object is just a property.
        if isinstance(old, (Obj, Ref)):
            destination.append(("unlink", target, relation, old))

    def reconcile_endpoint(prop: PropDef, prop_name: str, value) -> bool:
        if prop.name.lower() in ("source", "dest"):
            old = refs.resolve(prop.value, live=True)
            ops.append(("endpoint", target, prop_name, old, value, int(was_exit), int(will_exit)))
            return True
        return False

    for p in want.props:
        cur = have_props.pop(p.name.lower(), None)
        value = refs.resolve(p.value)
        if p.defined:
            owner = _owner(refs, p.owner)
            perms = normalize(p.perms, "rwc")
            if cur is None:
                ops.append(("addprop", target, p.name, value, [owner, perms]))
            elif not cur.defined:
                raise UnresolvedRef(f"property {p.name} is inherited on the MOO but `property` (defined) in the file")
            else:
                prop_name = cur.name
                if (owner, perms) != (_owner(refs, cur.owner, live=True), normalize(cur.perms, "rwc")):
                    info = [owner, perms]
                    if p.name != cur.name:
                        info.append(p.name)
                    ops.append(("propinfo", target, cur.name, info))
                    prop_name = p.name
                elif p.name != cur.name:
                    ops.append(("propinfo", target, cur.name, [owner, perms, p.name]))
                    prop_name = p.name
                if (
                    value != refs.resolve(cur.value, live=True)
                    or was_exit != will_exit
                    and cur.name.lower() in ("source", "dest")
                ):
                    if not reconcile_endpoint(cur, prop_name, value):
                        ops.append(("setprop", target, prop_name, value))
        else:
            if cur is not None and cur.defined:
                raise UnresolvedRef(f"property {p.name} is defined on the MOO but `override` in the file")
            if (
                cur is None
                or value != refs.resolve(cur.value, live=True)
                or was_exit != will_exit
                and cur.name.lower() in ("source", "dest")
            ):
                if cur is None or not reconcile_endpoint(cur, p.name, value):
                    ops.append(("setprop", target, p.name, value))
    before_chparent = []
    for cur in have_props.values():
        removal = []
        unlink_old(cur, removal)
        removal.append(
            ("rmprop", target, cur.name)
            if cur.defined
            else ("clearprop", target, cur.name)
        )
        if was_exit and not will_exit and cur.name.lower() in ("source", "dest"):
            before_chparent.extend(removal)
        else:
            ops.extend(removal)
    if before_chparent:
        chparent = next((i for i, op in enumerate(ops) if op[0] == "chparent"), None)
        if chparent is None:
            ops.extend(before_chparent)
        else:
            ops[chparent:chparent] = before_chparent

    have_verbs = list(have.verbs if have else [])
    primary_counts: dict[str, int] = {}
    for verb in have_verbs:
        primary_counts[verb.key.lower()] = primary_counts.get(verb.key.lower(), 0) + 1
    primary_seen: dict[str, int] = {}
    verb_targets: dict[int, VerbTarget] = {}
    for position, verb in enumerate(have_verbs, 1):
        primary = verb.key.lower()
        primary_seen[primary] = primary_seen.get(primary, 0) + 1
        label = verb.key
        if primary_counts[primary] > 1:
            label = f"{label}#{primary_seen[primary]}"
        verb_targets[position] = VerbTarget(verb.live_index or position, label)
    verb_groups: dict[str, list[tuple[int, VerbDef]]] = {}
    for position, verb in enumerate(have_verbs, 1):
        verb_groups.setdefault(verb.key.lower(), []).append((position, verb))
    matched: set[int] = set()
    for v in want.verbs:
        group = verb_groups.get(v.key.lower(), [])
        current = next(((position, verb) for position, verb in group if position not in matched), None)
        owner = _owner(refs, v.owner)
        perms = normalize(v.perms, "rwxd")
        if current is None:
            ops.append(("addverb", target, [owner, perms, v.names], list(v.args), list(v.code)))
            continue
        position, cur = current
        matched.add(position)
        descriptor = verb_targets[position]
        if (owner, perms, v.names) != (_owner(refs, cur.owner, live=True), normalize(cur.perms, "rwxd"), cur.names):
            ops.append(("verbinfo", target, descriptor, [owner, perms, v.names]))
        if tuple(v.args) != tuple(cur.args):
            ops.append(("verbargs", target, descriptor, list(v.args)))
        if list(v.code) != list(cur.code):
            ops.append(("verbcode", target, descriptor, list(v.code)))
    removed = [
        (verb_targets[position], verb)
        for position, verb in enumerate(have_verbs, 1)
        if position not in matched
    ]
    for descriptor, _ in sorted(removed, reverse=True, key=lambda item: item[0].index):
        ops.append(("rmverb", target, descriptor))
    return ops


def describe(op: tuple) -> str:
    kind = op[0]
    if kind == "link":
        return f"link exits among {len(op[1])} objects"
    target = op[1]
    rest = op[2:]
    if kind in ("setprop", "addprop", "verbcode", "addverb"):
        name = rest[0] if kind != "addverb" else rest[0][2]
        return f"{kind} {target}.{name}"
    if kind in ("rmprop", "clearprop", "rmverb", "propinfo", "verbinfo", "verbargs"):
        return f"{kind} {target}.{rest[0]}"
    if kind == "endpoint":
        return f"endpoint {target}.{rest[0]} {rest[1]} -> {rest[2]}"
    return f"{kind} {target} {' '.join(str(x) for x in rest)}"
