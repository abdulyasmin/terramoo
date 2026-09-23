"""What would change: the files against the live MOO, as a list of ops.

Pure over plain data, so it is where the tests are.  An op is a tuple whose
first element names a `tmoo_apply` case; object arguments are `Ref("@", key)`
until apply resolves them, so a plan can name objects it is about to create.
Values are compared after resolving references on the file side, so
`@gatehouse` in a file equals `#171` in the MOO when the registry says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import ObjectDef, normalize
from .moolit import Obj, Ref, walk
from .refs import Refs, UnresolvedRef


@dataclass
class Plan:
    creates: list[tuple[str, object, str]] = field(default_factory=list)  # (key, parent, name)
    gone: dict[str, Obj] = field(default_factory=dict)  # registry entries the MOO no longer has
    ops: list[tuple] = field(default_factory=list)
    destroys: list[str] = field(default_factory=list)  # registry keys with no file
    problems: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.creates or self.ops or self.destroys)


def _owner(refs: Refs, owner, *, live: bool = False):
    """An owner as a number; None means the player."""
    return refs.player if owner is None else refs.resolve_ref(owner, live=live)


def build(files: dict[str, ObjectDef], live: dict[str, ObjectDef | None], refs: Refs) -> Plan:
    plan = Plan()
    broken: set[str] = set()

    # A reference to a key with no file would dangle as soon as that key is
    # destroyed (or would never be created): say so before anything runs.
    for key, obj in files.items():
        missing = sorted(n for n in _at_refs(obj) if n != "me" and n not in files)
        if missing:
            plan.problems.append(f"{key}: refers to {', '.join('@' + n for n in missing)}, which has no file")
            broken.add(key)

    # Creates, parents first.  A parent that is itself a new object is
    # passed by registry name; the helper resolves it from what it just made.
    new_keys = [k for k in files if k not in refs.registry or live.get(k) is None]
    plan.gone = {k: refs.registry[k] for k in new_keys if k in refs.registry}
    refs.pending = set(new_keys)
    ordered, cyclic = _topo(new_keys, files)
    for key in sorted(cyclic):
        plan.problems.append(f"{key}: its parent chain loops back to itself")
        broken.add(key)
    for key in ordered:
        obj = files[key]
        parent = obj.parent
        if isinstance(parent, Ref) and parent.kind == "@" and parent.name in broken and key not in broken:
            plan.problems.append(f"{key}: parent @{parent.name} cannot be created")
            broken.add(key)
        if key in broken:
            continue
        if isinstance(parent, Ref) and parent.kind == "@" and parent.name in new_keys:
            parent_arg = parent.name
        else:
            try:
                parent_arg = refs.symbolize_obj(refs.resolve_ref(parent))
            except UnresolvedRef as e:
                plan.problems.append(f"{key}: parent {e}")
                broken.add(key)
                continue
        plan.creates.append((key, parent_arg, obj.name))

    for key in files:
        if key in broken:
            continue
        obj = files[key]
        current = live.get(key)
        try:
            ops = diff_object(key, obj, current, refs)
        except UnresolvedRef as e:
            plan.problems.append(f"{key}: {e}")
            continue
        if ops:
            plan.ops.extend(ops)
        elif current is not None:
            plan.unchanged.append(key)

    for key in refs.registry:
        if key not in files:
            plan.destroys.append(key)
    if plan.ops or plan.creates:
        plan.ops.append(("link", [Ref("@", k) for k in files]))
    return plan


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


def _topo(keys: list[str], files: dict[str, ObjectDef]) -> tuple[list[str], set[str]]:
    """`keys` parents first, and the keys whose parent chain is a loop."""
    out, seen, cyclic = [], set(), set()

    def visit(k, stack):
        if k in seen:
            return
        if k in stack:
            cyclic.update(stack[stack.index(k):])
            return
        parent = files[k].parent
        if isinstance(parent, Ref) and parent.kind == "@" and parent.name in keys:
            visit(parent.name, stack + [k])
        seen.add(k)
        out.append(k)

    for k in keys:
        visit(k, [])
    return out, cyclic


def diff_object(key: str, want: ObjectDef, have: ObjectDef | None, refs: Refs) -> list[tuple]:
    """Ops that turn `have` (live) into `want` (file).  Both may carry
    `Ref`s (export symbolizes what it can), so every comparison resolves
    both sides to numbers first.
    `have` is None for an object that does not exist yet: every attribute
    is then set, on the object the create op registers under `key`."""
    target = Ref("@", key)
    ops: list[tuple] = []
    file_ref = refs.resolve_ref

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

    have_props = {p.name: p for p in (have.props if have else [])}
    for p in want.props:
        cur = have_props.pop(p.name, None)
        value = refs.resolve(p.value)
        if p.defined:
            owner = _owner(refs, p.owner)
            perms = normalize(p.perms, "rwc")
            if cur is None:
                ops.append(("addprop", target, p.name, value, [owner, perms]))
            elif not cur.defined:
                raise UnresolvedRef(f"property {p.name} is inherited on the MOO but `property` (defined) in the file")
            else:
                if (owner, perms) != (_owner(refs, cur.owner, live=True), normalize(cur.perms, "rwc")):
                    ops.append(("propinfo", target, p.name, [owner, perms]))
                if value != refs.resolve(cur.value, live=True):
                    ops.append(("setprop", target, p.name, value))
        else:
            if cur is not None and cur.defined:
                raise UnresolvedRef(f"property {p.name} is defined on the MOO but `override` in the file")
            if cur is None or value != refs.resolve(cur.value, live=True):
                ops.append(("setprop", target, p.name, value))
    for name, cur in have_props.items():
        ops.append(("rmprop", target, name) if cur.defined else ("clearprop", target, name))

    have_verbs = {v.key: v for v in (have.verbs if have else [])}
    for v in want.verbs:
        cur = have_verbs.pop(v.key, None)
        owner = _owner(refs, v.owner)
        perms = normalize(v.perms, "rwxd")
        if cur is None:
            ops.append(("addverb", target, [owner, perms, v.names], list(v.args), list(v.code)))
            continue
        if (owner, perms, v.names) != (_owner(refs, cur.owner, live=True), normalize(cur.perms, "rwxd"), cur.names):
            ops.append(("verbinfo", target, v.key, [owner, perms, v.names]))
        if tuple(v.args) != tuple(cur.args):
            ops.append(("verbargs", target, v.key, list(v.args)))
        if list(v.code) != list(cur.code):
            ops.append(("verbcode", target, v.key, list(v.code)))
    for name in have_verbs:
        ops.append(("rmverb", target, name))
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
    return f"{kind} {target} {' '.join(str(x) for x in rest)}"
