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

from . import objdef
from .moolit import Obj, Ref, walk


class UnresolvedRef(KeyError):
    pass


class Registry(dict[str, Obj]):
    """A registry mapping, per-key generation nonces and mutation revision."""

    def __init__(
        self,
        values=(),
        generations: dict[str, str | None] | None = None,
        revision: int = 0,
    ):
        super().__init__(values)
        self.generations = dict(generations or {})
        self.revision = revision

    @property
    def legacy_keys(self) -> set[str]:
        """Keys readable from old registries but unsafe as object-file names."""
        return {key for key in self if not objdef.is_identifier(key) or key.lower() == "me"}


@dataclass
class Refs:
    player: Obj
    registry: dict[str, Obj] = field(default_factory=dict)
    sysrefs: dict[str, Obj] = field(default_factory=dict)
    generations: dict[str, str | None] = field(default_factory=dict)
    registry_revision: int = 0
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
                if ref.name.lower() == "me":
                    return self.player
                key = self.registry_key(ref.name)
                pending = ref.name.lower() in self._pending_folded()
                if key is not None and (live or not pending):
                    return self.registry[key]
                if pending:
                    return ref
                raise UnresolvedRef(f"@{ref.name} is not in the registry")
            sysname = self._sys_by_name.get(ref.name.lower())
            if sysname is not None:
                return self.sysrefs[sysname]
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
        if isinstance(self.registry, Registry):
            self.generations = dict(self.registry.generations)
            self.registry_revision = self.registry.revision
        self.reindex()

    def registry_key(self, name: str) -> str | None:
        """The registry's preserved spelling for MOO's case-insensitive name."""
        return self._registry_by_name.get(name.lower())

    def generation_for(self, name: str) -> str | None:
        key = self.registry_key(name)
        return self.generations.get(key) if key is not None else None

    @property
    def legacy_keys(self) -> set[str]:
        return {key for key in self.registry if not objdef.is_identifier(key)}

    def _pending_folded(self) -> set[str]:
        return {name.lower() for name in self.pending}

    def replace_registry(self, registry: dict[str, Obj]) -> None:
        self.registry = dict(registry)
        self.generations = dict(getattr(registry, "generations", {}))
        self.registry_revision = getattr(registry, "revision", 0)
        self.reindex()

    def snapshot(self) -> Registry:
        return Registry(self.registry, self.generations, self.registry_revision)

    def reindex(self):
        self._registry_by_name = {}
        for name in self.registry:
            folded = name.lower()
            if folded in self._registry_by_name:
                raise ValueError(
                    f"registry keys {self._registry_by_name[folded]!r} and {name!r} differ only in case"
                )
            self._registry_by_name[folded] = name
        self._sys_by_name = {}
        for name in self.sysrefs:
            self._sys_by_name.setdefault(name.lower(), name)
        self._by_obj = {o: n for n, o in self.registry.items()}
        # Prefer the shortest $name when several point at one object.
        self._sys_by_obj = {}
        for n, o in sorted(self.sysrefs.items(), key=lambda kv: (len(kv[0]), kv[0])):
            if not isinstance(o.num, int) or o.num >= 0:  # $nothing, $ambiguous_match, $failed_match stay numbers
                self._sys_by_obj.setdefault(o, n)


# ----- state file


def save_state(path: Path, player: Obj, registry: dict[str, Obj], toolbox: Obj | None) -> None:
    generations = getattr(registry, "generations", {})
    data = {
        "player": player.num,
        "toolbox": toolbox.num if toolbox else None,
        "registry": {k: v.num for k, v in sorted(registry.items())},
        "generations": {k: generations.get(k) for k in sorted(registry)},
        "registry_revision": getattr(registry, "revision", 0),
    }
    path.write_text(json.dumps(data, indent=2) + "\n")
