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

OWNER_SELF = Ref("@", "me")  # the player; spelled `@me` in files

ArgSpec = tuple[str, str, str]  # dobj, prep, iobj as MOO spells them


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
    args: ArgSpec = ("this", "none", "this")
    perms: str = "rxd"
    owner: Obj | Ref | None = None

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
    obj: Obj | None = None  # the live number, when known

    def prop(self, name: str) -> PropDef | None:
        for p in self.props:
            if p.name == name:
                return p
        return None

    def verb(self, key: str) -> VerbDef | None:
        for v in self.verbs:
            if v.key == key:
                return v
        return None


def normalize_flags(flags: str) -> str:
    return "".join(c for c in "rwf" if c in flags)


def normalize_perms(perms: str, alphabet: str) -> str:
    return "".join(c for c in alphabet if c in perms)
