"""A world: one MOO, one player, one directory of object files.

    worlds/<name>/world.toml      url, player, ignored properties
    worlds/<name>/objects/*.moo   one objdef file per managed object
    worlds/<name>/state.json      mirror of the in-MOO registry

The toolbox is a `$thing` the player owns, reached as `player.tmoo`, holding
the registry map and the helper verbs (`terramoo/helper/*.moo`).  It is the
only thing `tmoo` creates that the files do not describe.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import moolit, objdef
from .mcp import Client, MooError, token_for
from .model import ObjectDef
from .moolit import Obj, from_json
from .refs import Refs, load_state, save_state

HELPER_DIR = Path(__file__).parent / "helper"
HELPER_VERBS = ("tmoo_export", "tmoo_apply", "tmoo_sysrefs")
TOOLBOX_NAME = "terramoo toolbox"

# Properties that are the MOO's runtime state rather than the object's
# definition.  Exits and entrances are derived: `tmoo apply` links every
# managed exit into its rooms after everything else is in place.
DEFAULT_IGNORE_PROPS = {
    "exits",
    "entrances",
    "residents",
    "blessed_task",
    "blessed_object",
    "owned_objects",
    "last_connect_time",
    "last_disconnect_time",
    "first_connect_time",
    "previous_connection",
    "all_connect_places",
    "current_message",
    "messages",
    "messages_going",
    "mail_options",
    "mail_forward",
    "mail_notify",
    "mail_lists",
    "features",
    "password",
    "email_address",
    "size_quota",
    "ownership_quota",
    "lines",
    "current_folder",
    "gaglist",
    "paranoid",
    "responsible",
    "history",
    "pending",
    "pagelen",
    "linelen",
    "notified",
    "last_move",
    "object_size",
    "tmoo",
}


def find_root(start: Path | None = None) -> Path:
    env = os.environ.get("TMOO_ROOT")
    if env:
        return Path(env)
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / "worlds").is_dir() and (d / "pyproject.toml").exists():
            return d
    return Path(__file__).resolve().parents[1]


@dataclass
class World:
    name: str
    root: Path
    url: str
    player_name: str
    ignore_props: set[str] = field(default_factory=lambda: set(DEFAULT_IGNORE_PROPS))
    _client: Client | None = None
    _player: Obj | None = None
    _toolbox: Obj | None = None

    @classmethod
    def load(cls, root: Path, name: str | None) -> "World":
        worlds = sorted(p.name for p in (root / "worlds").iterdir() if (p / "world.toml").exists())
        if name is None:
            name = os.environ.get("TMOO_WORLD") or (worlds[0] if len(worlds) == 1 else None)
        if name is None:
            raise MooError(f"which world? one of: {', '.join(worlds)} (pass --world or set TMOO_WORLD)")
        cfg_path = root / "worlds" / name / "world.toml"
        if not cfg_path.exists():
            raise MooError(f"no such world {name!r} (looked for {cfg_path})")
        cfg = tomllib.loads(cfg_path.read_text())
        ignore = set(DEFAULT_IGNORE_PROPS) | set(cfg.get("ignore_props", []))
        ignore -= set(cfg.get("keep_props", []))
        return cls(name=name, root=root, url=cfg["url"], player_name=cfg["player"], ignore_props=ignore)

    # ----- paths

    @property
    def dir(self) -> Path:
        return self.root / "worlds" / self.name

    @property
    def objects_dir(self) -> Path:
        return self.dir / "objects"

    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    def file_for(self, key: str) -> Path:
        return self.objects_dir / f"{key}.moo"

    # ----- files

    def load_files(self) -> dict[str, ObjectDef]:
        out = {}
        for path in sorted(self.objects_dir.glob("*.moo")):
            try:
                obj = objdef.parse(path.read_text())
            except (objdef.FormatError, ValueError) as e:
                raise MooError(f"{path.relative_to(self.root)}: {e}") from None
            if obj.key != path.stem:
                raise MooError(f"{path.relative_to(self.root)}: file is named {path.stem!r} but declares object {obj.key!r}")
            out[obj.key] = obj
        return out

    def write_file(self, obj: ObjectDef) -> Path:
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        path = self.file_for(obj.key)
        path.write_text(objdef.render(obj))
        return path

    # ----- the MOO

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = Client(self.url, token_for(self.name))
        return self._client

    def eval(self, expression: str):
        return self.client.eval(expression)

    @property
    def player(self) -> Obj:
        if self._player is None:
            who = from_json(self.eval("{player, player.name}"))
            self._player = who[0]
            if who[1] != self.player_name:
                raise MooError(f"the token belongs to {who[1]} ({who[0]}), but world.toml says player = {self.player_name!r}")
        return self._player

    @property
    def toolbox(self) -> Obj:
        if self._toolbox is None:
            has = self.eval('$object_utils:has_property(player, "tmoo") && valid(player.tmoo)')
            if not has:
                raise MooError("no toolbox on this player yet: run `tmoo bootstrap`")
            self._toolbox = from_json(self.eval("player.tmoo"))
        return self._toolbox

    def bootstrap(self, log=print) -> Obj:
        """Create the toolbox if it is missing and (re)install the helper verbs."""
        player = self.player
        has = self.eval('$object_utils:has_property(player, "tmoo") && valid(player.tmoo)')
        if has:
            tb = from_json(self.eval("player.tmoo"))
            log(f"toolbox is {tb}")
        else:
            tb = self._find_orphan_toolbox()
            if tb is None:
                tb = from_json(self.eval("create($thing)"))
                log(f"created toolbox {tb}")
                self.client.call_tool("set_prop", {"object": str(tb), "prop": "name", "value": moolit.escape(TOOLBOX_NAME)})
            else:
                log(f"adopting toolbox {tb} left by an earlier bootstrap")
            if not self.eval(f'$object_utils:has_property({tb}, "registry")'):
                self.eval(f'add_property({tb}, "registry", [], {{player, "r"}})')
            if self.eval('$object_utils:has_property(player, "tmoo")'):
                self.client.call_tool("set_prop", {"object": str(player), "prop": "tmoo", "value": str(tb)})
            else:
                self.eval(f'add_property(player, "tmoo", {tb}, {{player, "r"}})')
        self._toolbox = tb
        for name in HELPER_VERBS:
            code = (HELPER_DIR / f"{name}.moo").read_text()
            exists = self.eval(f'$object_utils:has_verb({tb}, "{name}")')
            r = self.client.set_verb(str(tb), name, code, create=not exists, permissions="rxd",
                                     dobj="this", prep="none", iobj="this")
            log(f"{'updated' if exists else 'installed'} {tb}:{name}: {r.strip()}")
        return tb

    def _find_orphan_toolbox(self) -> Obj | None:
        """A toolbox a failed bootstrap created but never hooked to the player."""
        owned = [from_json(o) for o in self.eval("player.owned_objects")]
        if not owned:
            return None
        names = self.eval("$list_utils:map_prop({" + ", ".join(map(str, owned)) + '}, "name")')
        for o, n in zip(owned, names):
            if n == TOOLBOX_NAME:
                return o
        return None

    def read_registry(self) -> dict[str, Obj]:
        raw = self.eval(f"{self.toolbox}.registry")
        if not isinstance(raw, dict):
            return {}
        return {k: from_json(v) for k, v in raw.items()}

    def read_sysrefs(self) -> dict[str, Obj]:
        raw = self.eval(f"{self.toolbox}:tmoo_sysrefs()")
        return {name: from_json(obj) for name, obj in raw}

    def refs(self) -> Refs:
        return Refs(player=self.player, registry=self.read_registry(), sysrefs=self.read_sysrefs())

    def save_state(self, registry: dict[str, Obj]) -> None:
        save_state(self.state_path, self.player, registry, self._toolbox)

    def load_state(self):
        return load_state(self.state_path)
