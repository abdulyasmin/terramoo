"""What would change: the files against the live MOO, as a list of ops.

Pure over plain data, so it is where the tests are.  An op is a tuple whose
first element names a `tmoo_apply` case; object arguments are `Ref("@", key)`
until apply resolves them, so a plan can name objects it is about to create.
Values are compared after resolving references on the file side, so
`@gatehouse` in a file equals `#171` in the MOO when the registry says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import ObjectDef, PropDef, VerbDef, normalize_flags, normalize_perms
from .moolit import Obj, Ref
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


def _me(refs: Refs, owner):
    return refs.player if owner is None else refs.resolve_ref(owner)


def _key_ref(key: str) -> Ref:
    return Ref("@", key)


def build(files: dict[str, ObjectDef], live: dict[str, ObjectDef | None], refs: Refs) -> Plan:
    plan = Plan()
    broken: set[str] = set()

    # Creates, parents first.  A parent that is itself a new object is
    # passed by registry name; the helper resolves it from what it just made.
    new_keys = [k for k in files if k not in refs.registry or live.get(k) is None]
    plan.gone = {k: refs.registry[k] for k in new_keys if k in refs.registry}
    refs.pending = set(new_keys)
    ordered = _topo(new_keys, files)
    for key in ordered:
        obj = files[key]
        parent = obj.parent
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
        current = live.get(key) if key in refs.registry else None
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
        plan.ops.append(("link", [_key_ref(k) for k in files]))
    return plan


def _topo(keys: list[str], files: dict[str, ObjectDef]) -> list[str]:
    out, seen = [], set()

    def visit(k, stack):
        if k in seen:
            return
        if k in stack:
            raise ValueError(f"parent cycle through {k}")
        parent = files[k].parent
        if isinstance(parent, Ref) and parent.kind == "@" and parent.name in files and parent.name in keys:
            visit(parent.name, stack | {k})
        seen.add(k)
        out.append(k)

    for k in keys:
        visit(k, frozenset())
    return out


def diff_object(key: str, want: ObjectDef, have: ObjectDef | None, refs: Refs) -> list[tuple]:
    """Ops that turn `have` (live) into `want` (file).  Both may carry
    `Ref`s (export symbolizes what it can), so every comparison resolves
    both sides to numbers first.
    `have` is None for an object that does not exist yet: every attribute
    is then set, on the object the create op registers under `key`."""
    me = _key_ref(key)
    ops: list[tuple] = []
    r = refs.resolve_ref

    if have is not None and want.name != have.name:
        ops.append(("name", me, want.name))
    if have is not None and r(want.parent) != r(have.parent):
        ops.append(("chparent", me, r(want.parent)))
    if r(want.location) != (r(have.location) if have else Obj(-1)):
        ops.append(("move", me, r(want.location)))
    want_flags = normalize_flags(want.flags)
    if want_flags != (normalize_flags(have.flags) if have else ""):
        ops.append(("flags", me, want_flags))

    have_props = {p.name: p for p in (have.props if have else [])}
    for p in want.props:
        cur = have_props.pop(p.name, None)
        value = refs.resolve(p.value)
        if p.defined:
            owner = _me(refs, p.owner)
            perms = normalize_perms(p.perms, "rwc")
            if cur is None:
                ops.append(("addprop", me, p.name, value, [owner, perms]))
            elif not cur.defined:
                raise UnresolvedRef(f"property {p.name} is inherited on the MOO but `property` (defined) in the file")
            else:
                if (owner, perms) != (_me(refs, cur.owner), normalize_perms(cur.perms, "rwc")):
                    ops.append(("propinfo", me, p.name, [owner, perms]))
                if value != refs.resolve(cur.value):
                    ops.append(("setprop", me, p.name, value))
        else:
            if cur is not None and cur.defined:
                raise UnresolvedRef(f"property {p.name} is defined on the MOO but `override` in the file")
            if cur is None or value != refs.resolve(cur.value):
                ops.append(("setprop", me, p.name, value))
    for name, cur in have_props.items():
        ops.append(("rmprop", me, name) if cur.defined else ("clearprop", me, name))

    have_verbs = {v.key: v for v in (have.verbs if have else [])}
    for v in want.verbs:
        cur = have_verbs.pop(v.key, None)
        owner = _me(refs, v.owner)
        perms = normalize_perms(v.perms, "rwxd")
        if cur is None:
            ops.append(("addverb", me, [owner, perms, v.names], list(v.args), list(v.code)))
            continue
        if (owner, perms, v.names) != (_me(refs, cur.owner), normalize_perms(cur.perms, "rwxd"), cur.names):
            ops.append(("verbinfo", me, v.key, [owner, perms, v.names]))
        if tuple(v.args) != tuple(cur.args):
            ops.append(("verbargs", me, v.key, list(v.args)))
        if list(v.code) != list(cur.code):
            ops.append(("verbcode", me, v.key, list(v.code)))
    for name in have_verbs:
        ops.append(("rmverb", me, name))
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
