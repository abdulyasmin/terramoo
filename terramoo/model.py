"""The object model both the files and the live MOO are read into.

An `ObjectDef` is one managed object.  Its `key` is the registry name (the
file name), never an object number: numbers are the MOO's business and are
kept in the registry.  References inside the model (`parent`, `location`,
property values) are `Obj` when read from the MOO and `Ref` when read from a
file; `refs.resolve` / `refs.symbolize` convert between the two.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .moolit import Obj, Ref


@dataclass
class PropDef:
    name: str
    value: object
    perms: str = "rc"
    owner: Obj | Ref | None = None  # None: the player
    defined: bool = True  # False: an override of an inherited property


@dataclass
class VerbDef:
    names: str  # the full spec, e.g. "look_self look*ing"
    code: list[str]
    args: tuple[str, str, str] = ("this", "none", "this")  # dobj, prep, iobj
    perms: str = "rxd"
    owner: Obj | Ref | None = None
    # Present only on live exports.  MOO verb names are not unique; mutations
    # must use the numeric descriptor returned by verbs().
    live_index: int | None = field(default=None, compare=False)

    @property
    def key(self) -> str:
        return self.names.split()[0]


@dataclass
class ObjectDef:
    key: str
    name: str
    parent: Obj | Ref
    location: Obj | Ref = Obj(-1)
    owner: Obj | Ref | None = None
    flags: str = ""  # subset of "rwf", in that order
    props: list[PropDef] = field(default_factory=list)
    verbs: list[VerbDef] = field(default_factory=list)


def ordered_like(obj: ObjectDef, template: ObjectDef) -> ObjectDef:
    """`obj` with its properties and verbs in `template`'s order, new ones
    after, in their own order.  Servers list inherited properties in
    different orders; a pull should not reshuffle a file for that."""
    def rank(names: list[str]):
        positions: dict[str, list[int]] = {}
        for i, name in enumerate(names):
            positions.setdefault(name.lower(), []).append(i)
        used: dict[str, int] = {}

        def item_rank(item_name: str):
            folded = item_name.lower()
            occurrence = used.get(folded, 0)
            used[folded] = occurrence + 1
            matches = positions.get(folded, [])
            return matches[occurrence] if occurrence < len(matches) else len(names)

        return item_rank

    prop_rank = rank([p.name for p in template.props])
    verb_rank = rank([v.names for v in template.verbs])
    obj.props.sort(key=lambda p: prop_rank(p.name))  # stable: new ones keep their order
    obj.verbs.sort(key=lambda v: verb_rank(v.names))
    return obj


def normalize(chars: str, alphabet: str) -> str:
    """`chars` in `alphabet` order, anything else dropped: "rwf" for object
    flags, "rwc" for property perms, "rwxd" for verb perms."""
    return "".join(c for c in alphabet if c in chars)
