"""Reading managed objects out of the live MOO into `ObjectDef`s."""

from __future__ import annotations

from . import moolit
from .errors import MooError
from .model import ObjectDef, PropDef, VerbDef, normalize
from .moolit import Obj
from .refs import Refs
from .world import World

CHUNK = 8  # objects per helper call; well inside a hosted gate's 8 s budget


def export(world: World, refs: Refs, keys: list[str]) -> dict[str, ObjectDef | None]:
    """Export the registry objects named by `keys`.  Objects the registry
    names but the MOO no longer has come back as `None`."""
    by_obj = {refs.registry[k]: k for k in keys}
    objs = list(by_obj)
    out: dict[str, ObjectDef | None] = {}
    for i in range(0, len(objs), CHUNK):
        batch = objs[i:i + CHUNK]
        records = world.eval(world.helper("tmoo_export", moolit.serialize(batch)))
        if not isinstance(records, list):
            raise MooError("tmoo_export returned a malformed result")
        seen = set()
        for rec in records:
            if (
                not isinstance(rec, list)
                or len(rec) not in (1, 8)
                or not isinstance(rec[0], Obj)
            ):
                raise MooError("tmoo_export returned a malformed record")
            obj = rec[0]
            if obj not in batch:
                raise MooError(f"tmoo_export returned an unexpected object {obj}")
            if obj in seen:
                raise MooError(f"tmoo_export returned {obj} more than once")
            seen.add(obj)
            key = by_obj[obj]
            out[key] = None if len(rec) == 1 else _to_def(key, rec, refs, world.ignore_props)
        missing = [obj for obj in batch if obj not in seen]
        if missing:
            raise MooError(f"tmoo_export returned no record for {missing[0]}")
    return out


def _owner(o: Obj, refs: Refs):
    return None if o == refs.player else refs.symbolize_obj(o)


def _to_def(key: str, rec: list, refs: Refs, ignore: set[str]) -> ObjectDef:
    _, name, parent, location, owner, flags, props, verbs = rec
    obj = ObjectDef(
        key=key,
        name=name,
        parent=refs.symbolize_obj(parent),
        location=refs.symbolize_obj(location),
        owner=_owner(owner, refs),
        flags=normalize(flags, "rwf"),
    )
    for pname, defined, powner, perms, literal in props:
        if pname in ignore:
            continue
        obj.props.append(
            PropDef(
                name=pname,
                value=refs.symbolize(moolit.parse(literal)),
                perms=perms,
                owner=_owner(powner, refs),
                defined=bool(defined),
            )
        )
    for names, vowner, perms, args, code in verbs:
        obj.verbs.append(VerbDef(names=names, code=list(code), args=tuple(args), perms=perms, owner=_owner(vowner, refs)))
    return obj


def slug(name: str, taken: set[str]) -> str:
    base = "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")
    while "__" in base:
        base = base.replace("__", "_")
    if not base or not base[0].isalpha():
        base = "obj_" + base
    key, n = base, 2
    while key in taken:
        key = f"{base}_{n}"
        n += 1
    taken.add(key)
    return key
