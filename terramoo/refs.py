"""Turning `#123` into names and back.

Two tables: the *registry* (name -> object) for the objects this repo
manages, held in the MOO on the player's toolbox and mirrored to
`state.json`, and the *sysrefs* (`$name` -> object) read from `#0`.  Files
speak names; the MOO speaks numbers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .moolit import Obj, Ref, walk


class UnresolvedRef(KeyError):
    pass


@dataclass
class Refs:
    player: Obj
    registry: dict[str, Obj] = field(default_factory=dict)
    sysrefs: dict[str, Obj] = field(default_factory=dict)
    # Keys a plan is about to create: a `@ref` to one stays a Ref until the
    # create phase has run and the registry knows its number.
    pending: set[str] = field(default_factory=set)

    # ----- one direction: files -> MOO

    def resolve_ref(self, ref, *, live: bool = False):
        """`live` resolves a value read from the MOO: a key being recreated
        is still its old, recycled number there.  A file's value resolves
        to the key itself until the create has run."""
        if isinstance(ref, Ref):
            if ref.kind == "@":
                if ref.name == "me":
                    return self.player
                if ref.name in self.registry and (live or ref.name not in self.pending):
                    return self.registry[ref.name]
                if ref.name in self.pending:
                    return ref
                raise UnresolvedRef(f"@{ref.name} is not in the registry")
            if ref.name in self.sysrefs:
                return self.sysrefs[ref.name]
            raise UnresolvedRef(f"${ref.name} is not a corified object on this MOO")
        return ref

    def resolve(self, value, *, live: bool = False):
        return walk(value, lambda v: self.resolve_ref(v, live=live))

    # ----- the other: MOO -> files

    def symbolize_obj(self, value):
        if isinstance(value, Obj):
            if value == self.player:
                return Ref("@", "me")
            name = self._by_obj.get(value)
            if name is not None:
                return Ref("@", name)
            sysname = self._sys_by_obj.get(value)
            if sysname is not None:
                return Ref("$", sysname)
        return value

    def symbolize(self, value):
        return walk(value, self.symbolize_obj)

    def __post_init__(self):
        self.reindex()

    def reindex(self):
        self._by_obj = {o: n for n, o in self.registry.items()}
        # Prefer the shortest $name when several point at one object.
        self._sys_by_obj = {}
        for n, o in sorted(self.sysrefs.items(), key=lambda kv: (len(kv[0]), kv[0])):
            if not isinstance(o.num, int) or o.num >= 0:  # $nothing, $ambiguous_match, $failed_match stay numbers
                self._sys_by_obj.setdefault(o, n)


# ----- state file


def save_state(path: Path, player: Obj, registry: dict[str, Obj], toolbox: Obj | None) -> None:
    data = {
        "player": player.num,
        "toolbox": toolbox.num if toolbox else None,
        "registry": {k: v.num for k, v in sorted(registry.items())},
    }
    path.write_text(json.dumps(data, indent=2) + "\n")
