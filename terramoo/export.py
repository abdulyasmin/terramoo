"""Reading managed objects out of the live MOO into `ObjectDef`s."""

from __future__ import annotations

from . import moolit
from .errors import MooError
from .model import ObjectDef, PropDef, VerbDef, normalize
from .moolit import Obj
from .refs import Refs
from .world import World

CHUNK = 8  # objects per helper call; well inside a hosted gate's 8 s budget


def _one_line(value: str, *, nonempty: bool = False) -> bool:
    return (not nonempty or bool(value)) and "\r" not in value and "\n" not in value


def _alphabet(value: str, allowed: str) -> bool:
    return all(c in allowed for c in value)


def _verb_args(args: list[str]) -> bool:
    return (
        args[0] in ("none", "any", "this")
        and args[2] in ("none", "any", "this")
        and all(_one_line(arg, nonempty=True) and arg == arg.strip() for arg in args)
    )


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
            dialect = getattr(getattr(world, "transport", None), "literal_dialect", moolit.LAMBDA)
            out[key] = None if len(rec) == 1 else _to_def(key, rec, refs, world.ignore_props, dialect)
        missing = [obj for obj in batch if obj not in seen]
        if missing:
            raise MooError(f"tmoo_export returned no record for {missing[0]}")
    return out


def _owner(o: Obj, refs: Refs):
    return None if o == refs.player else refs.symbolize_obj(o)


def _to_def(key: str, rec: list, refs: Refs, ignore: set[str], dialect: str = moolit.LAMBDA) -> ObjectDef:
    _, name, parent, location, owner, flags, props, verbs = rec
    if not (
        isinstance(name, str)
        and isinstance(parent, Obj)
        and isinstance(location, Obj)
        and isinstance(owner, Obj)
        and isinstance(flags, str)
        and _one_line(name)
        and _one_line(flags)
        and isinstance(props, list)
        and isinstance(verbs, list)
    ):
        raise MooError(f"tmoo_export returned a malformed record for {key}")
    obj = ObjectDef(
        key=key,
        name=name,
        parent=refs.symbolize_obj(parent),
        location=refs.symbolize_obj(location),
        owner=_owner(owner, refs),
        flags=normalize(flags, "rwf"),
    )
    for prop in props:
        if not (
            isinstance(prop, list)
            and len(prop) == 5
            and isinstance(prop[0], str)
            and _one_line(prop[0], nonempty=True)
        ):
            raise MooError(f"tmoo_export returned a malformed property record for {key}")
        pname = prop[0]
        if pname in ignore:
            continue
        if not (
            type(prop[1]) is int
            and prop[1] in (0, 1)
            and isinstance(prop[2], Obj)
            and isinstance(prop[3], str)
            and isinstance(prop[4], str)
            and _alphabet(prop[3], "rwc")
        ):
            raise MooError(f"tmoo_export returned a malformed property record for {key}")
        pname, defined, powner, perms, literal = prop
        try:
            value = moolit.parse(literal, dialect=dialect)
        except moolit.LiteralError as e:
            raise MooError(f"tmoo_export returned {key} property {pname!r} with a malformed literal: {e}") from None
        obj.props.append(
            PropDef(
                name=pname,
                value=refs.symbolize(value),
                perms=perms,
                owner=_owner(powner, refs),
                defined=bool(defined),
            )
        )
    for verb in verbs:
        if not (
            isinstance(verb, list)
            and len(verb) == 5
            and isinstance(verb[0], str)
            and isinstance(verb[1], Obj)
            and isinstance(verb[2], str)
            and isinstance(verb[3], list)
            and len(verb[3]) == 3
            and all(isinstance(arg, str) for arg in verb[3])
            and _one_line(verb[0], nonempty=True)
            and bool(verb[0].split())
            and _alphabet(verb[2], "rwxd")
            and _verb_args(verb[3])
            and isinstance(verb[4], list)
            and all(isinstance(line, str) for line in verb[4])
            and all(_one_line(line) for line in verb[4])
        ):
            name = verb[0] if isinstance(verb, list) and verb and isinstance(verb[0], str) else "?"
            raise MooError(f"tmoo_export returned {key} verb {name!r} with malformed fields")
        names, vowner, perms, args, code = verb
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
