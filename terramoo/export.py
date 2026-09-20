"""Reading managed objects out of the live MOO into `ObjectDef`s."""

from __future__ import annotations

from . import moolit
from .model import ObjectDef, PropDef, VerbDef, normalize_flags
from .moolit import Obj, from_json
from .refs import Refs
from .world import World

CHUNK = 8  # objects per helper call; well inside the gate's 8 s budget


def export(world: World, refs: Refs, keys: list[str]) -> dict[str, ObjectDef]:
    """Export the registry objects named by `keys`.  Objects the registry
    names but the MOO no longer has come back as `None`."""
    by_obj = {refs.registry[k]: k for k in keys}
    objs = list(by_obj)
    out: dict[str, ObjectDef | None] = {}
    for i in range(0, len(objs), CHUNK):
        batch = objs[i:i + CHUNK]
        expr = f"{world.toolbox}:tmoo_export({moolit.serialize(batch)})"
        for rec in world.eval(expr):
            o = from_json(rec["obj"])
            key = by_obj[o]
            out[key] = None if "error" in rec else _to_def(key, rec, refs, world.ignore_props)
    return out


def _owner(value, refs: Refs):
    o = from_json(value)
    return None if o == refs.player else refs.symbolize_obj(o)


def _to_def(key: str, rec: dict, refs: Refs, ignore: set[str]) -> ObjectDef:
    obj = ObjectDef(
        key=key,
        name=rec["name"],
        parent=refs.symbolize_obj(from_json(rec["parent"])),
        location=refs.symbolize_obj(from_json(rec["location"])),
        owner=_owner(rec["owner"], refs),
        flags=normalize_flags(rec["flags"]),
        obj=from_json(rec["obj"]),
    )
    for p in rec["props"]:
        if p["name"] in ignore:
            continue
        obj.props.append(
            PropDef(
                name=p["name"],
                value=refs.symbolize(moolit.parse(p["value"])),
                perms=p["perms"],
                owner=_owner(p["owner"], refs),
                defined=bool(p["defined"]),
            )
        )
    for v in rec["verbs"]:
        obj.verbs.append(
            VerbDef(
                names=v["names"],
                code=list(v["code"]),
                args=tuple(v["args"]),
                perms=v["perms"],
                owner=_owner(v["owner"], refs),
            )
        )
    return obj


def slug(name: str, taken: set[str]) -> str:
    base = "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")
    while "__" in base:
        base = base.replace("__", "_")
    if not base or not (base[0].isalpha() or base[0] == "_"):
        base = "obj_" + base
    key, n = base, 2
    while key in taken:
        key = f"{base}_{n}"
        n += 1
    taken.add(key)
    return key
