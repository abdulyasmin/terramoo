"""A world: one MOO, one player, one directory of object files.

    worlds/<name>/world.toml      connection, player, core settings
    worlds/<name>/objects/*.moo   one objdef file per managed object
    worlds/<name>/state.json      mirror of the in-MOO registry

`world.toml`:

    player = "alice"

    [connection]
    transport = "telnet"          # or "mcp"
    host = "moo.example.org"
    port = 7777
    tls = false
    # login, eval_prefix, tell, chunk, timeout, batch_bytes: see
    # terramoo/transport/telnet.py

    [core]
    toolbox_parent = "$thing"     # what the toolbox is created from

    ignore_props = []             # beyond DEFAULT_IGNORE_PROPS
    keep_props = []               # re-enable one of those

A top-level `url` (the pre-terramoo shape) is read as an mcp connection.

The toolbox is an object the player owns, reached as `player.tmoo`,
holding the registry and the helper verbs (`terramoo/helper/*.moo`).  It
is the only thing `tmoo` creates that the files do not describe.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import objdef
from .errors import MooError
from .model import ObjectDef
from .moolit import Err, Map, Obj, serialize
from .refs import Refs, load_state, save_state
from .secrets import secret_for
from .transport import Transport, connect

HELPER_DIR = Path(__file__).parent / "helper"
HELPER_VERBS = ("tmoo_export", "tmoo_apply", "tmoo_sysrefs", "tmoo_info")
SUSPENDING_HELPERS = ("tmoo_export", "tmoo_apply")
LEGACY_VERBS = ("tmoo_export", "tmoo_apply", "tmoo_sysrefs")
TOOLBOX_NAME = "terramoo toolbox"
TOOLBOX_PROP = "tmoo"
LEGACY_TOOLBOX_PROP = "tmoo"
LEGACY_TOOLBOX_NAME = "terramoo toolbox"

# Properties that are the MOO's runtime state rather than the object's
# definition, on LambdaCore and its descendants.  Exits and entrances are
# derived: `tmoo apply` links every managed exit into its rooms after
# everything else is in place.
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
    TOOLBOX_PROP,
    LEGACY_TOOLBOX_PROP,
}


def find_root(start: Path | None = None) -> Path:
    """The nearest directory with a `worlds/` in it, or `$TMOO_ROOT`."""
    env = os.environ.get("TMOO_ROOT") or os.environ.get("TMOO_ROOT")
    if env:
        return Path(env)
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / "worlds").is_dir():
            return d
    raise MooError("no worlds/ directory here or above (run `tmoo init <world>` to start one, or set TMOO_ROOT)")


def registry_value(raw) -> dict[str, Obj]:
    """The registry as `tmoo_apply` keeps it ({keys, objects}), or as the
    terramoo toolbox did (a map)."""
    if isinstance(raw, Map):
        return {str(k): v for k, v in raw.items()}
    if isinstance(raw, list) and len(raw) == 2 and all(isinstance(x, list) for x in raw):
        return dict(zip(raw[0], raw[1]))
    return {}


@dataclass
class World:
    name: str
    root: Path
    player_name: str
    connection: dict
    core: dict = field(default_factory=dict)
    ignore_props: set[str] = field(default_factory=lambda: set(DEFAULT_IGNORE_PROPS))
    _transport: Transport | None = None
    _player: Obj | None = None
    _toolbox: Obj | None = None

    @classmethod
    def load(cls, root: Path, name: str | None) -> "World":
        worlds_dir = root / "worlds"
        worlds = sorted(p.name for p in worlds_dir.iterdir() if (p / "world.toml").exists()) if worlds_dir.is_dir() else []
        if name is None:
            name = os.environ.get("TMOO_WORLD") or os.environ.get("TMOO_WORLD") or (worlds[0] if len(worlds) == 1 else None)
        if name is None:
            raise MooError(f"which world? one of: {', '.join(worlds) or '(none)'} (pass --world or set TMOO_WORLD)")
        cfg_path = worlds_dir / name / "world.toml"
        if not cfg_path.exists():
            raise MooError(f"no such world {name!r} (looked for {cfg_path})")
        cfg = tomllib.loads(cfg_path.read_text())
        conn = dict(cfg.get("connection", {}))
        if "url" in cfg and not conn:
            conn = {"transport": "mcp", "url": cfg["url"]}
        if "player" not in cfg:
            raise MooError(f"{cfg_path}: `player` is required")
        ignore = set(DEFAULT_IGNORE_PROPS) | set(cfg.get("ignore_props", []))
        ignore -= set(cfg.get("keep_props", []))
        return cls(name=name, root=root, player_name=cfg["player"], connection=conn,
                   core=dict(cfg.get("core", {})), ignore_props=ignore)

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

    def describe(self) -> str:
        c = self.connection
        if c.get("transport", "telnet") == "mcp":
            return c.get("url", "?")
        return f"{'tls' if c.get('tls') else 'telnet'}://{c.get('host')}:{c.get('port', 7777)}"

    # ----- files

    def load_files(self) -> dict[str, ObjectDef]:
        out = {}
        folded: dict[str, str] = {}
        for path in sorted(self.objects_dir.glob("*.moo")):
            try:
                obj = objdef.parse(path.read_text())
            except (objdef.FormatError, ValueError) as e:
                raise MooError(f"{path.relative_to(self.root)}: {e}") from None
            if obj.key != path.stem:
                raise MooError(f"{path.relative_to(self.root)}: file is named {path.stem!r} but declares object {obj.key!r}")
            # MOO string comparison ignores case, and so does the registry.
            if obj.key.lower() in folded:
                raise MooError(f"keys {folded[obj.key.lower()]!r} and {obj.key!r} differ only in case")
            folded[obj.key.lower()] = obj.key
            out[obj.key] = obj
        return out

    def write_file(self, obj: ObjectDef) -> Path:
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        path = self.file_for(obj.key)
        path.write_text(objdef.render(obj))
        return path

    # ----- the MOO

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            self._transport = connect(self.connection, self.player_name, secret_for(self.name))
        return self._transport

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def eval(self, expression: str):
        return self.transport.eval(expression)

    def helper(self, verb: str, *args: str) -> str:
        """The expression that calls a toolbox helper with MOO-source `args`."""
        args = list(args)
        if verb in SUSPENDING_HELPERS:
            args.append("1" if self.transport.can_suspend else "0")
        return f"{self.toolbox}:{verb}({', '.join(args)})"

    @property
    def player(self) -> Obj:
        if self._player is None:
            who, name = self.eval("{player, player.name}")
            self._player = who
            if name.lower() != self.player_name.lower():
                raise MooError(f"logged in as {name} ({who}), but world.toml says player = {self.player_name!r}")
        return self._player

    def _toolbox_via(self, prop: str) -> Obj | None:
        if self.eval(f'"{prop}" in properties(player) && valid(player.{prop})'):
            return self.eval(f"player.{prop}")
        return None

    @property
    def toolbox(self) -> Obj:
        if self._toolbox is None:
            tb = self._toolbox_via(TOOLBOX_PROP)
            if tb is None:
                raise MooError("no toolbox on this player yet: run `tmoo bootstrap`")
            self._toolbox = tb
        return self._toolbox

    def server_info(self) -> tuple[list[Obj] | None, str]:
        """What the player owns, when the core keeps a list (LambdaCore's
        `owned_objects`; None when it does not), and `server_version()`."""
        owned, version = self.eval(self.helper("tmoo_info"))
        return (None if isinstance(owned, Err) else list(owned)), version

    def owned(self) -> list[Obj] | None:
        return self.server_info()[0]

    def names(self, objs: list[Obj]) -> list[str]:
        if not objs:
            return []
        return [name for _, _, name in self.eval(self.helper("tmoo_info", serialize(objs)))]

    def bootstrap(self, log=print) -> Obj:
        """Find or create the toolbox and (re)install the helper verbs."""
        self.player
        tb = self._toolbox_via(TOOLBOX_PROP)
        if tb is not None:
            log(f"toolbox is {tb}")
        else:
            tb = self._toolbox_via(LEGACY_TOOLBOX_PROP) or self._find_orphan_toolbox()
            if tb is not None:
                log(f"adopting toolbox {tb} left by an earlier bootstrap")
            else:
                parent = self.core.get("toolbox_parent", "$thing")
                tb = self.eval(f"create({parent})")
                log(f"created toolbox {tb} from {parent}")
            if TOOLBOX_PROP in self.eval("properties(player)"):
                self.transport.set_prop("player", TOOLBOX_PROP, tb)
            else:
                self.eval(f'add_property(player, "{TOOLBOX_PROP}", {tb}, {{player, "r"}})')
        self._toolbox = tb
        if "registry" not in self.eval(f"properties({tb})"):
            self.eval(f'add_property({tb}, "registry", {{{{}}, {{}}}}, {{player, "r"}})')
        for name in HELPER_VERBS:
            code = (HELPER_DIR / f"{name}.moo").read_text().splitlines()
            r = self.transport.install_verb(tb, name, code)
            log(f"{tb}:{name} {r}")
        existing = self.eval(f"verbs({tb})")
        for name in LEGACY_VERBS:
            if name in existing:
                self.eval(f'delete_verb({tb}, "{name}")')
                log(f"removed {tb}:{name}")
        # The registry as tmoo_apply keeps it: a terramoo map is converted once.
        raw = self.eval(f"{tb}.registry")
        if not (isinstance(raw, list) and len(raw) == 2):
            reg = registry_value(raw)
            self.transport.set_prop(tb, "registry", [list(reg), list(reg.values())])
            log(f"converted the registry ({len(reg)} entries) to lists")
        if self.eval(f"{tb}.name") != TOOLBOX_NAME:
            self.transport.set_prop(tb, "name", TOOLBOX_NAME)
        return tb

    def _find_orphan_toolbox(self) -> Obj | None:
        """A toolbox a failed bootstrap created but never hooked to the player.
        Only findable where the core keeps `owned_objects`; the helpers are
        not installed yet, so this asks directly."""
        try:
            owned = self.eval("player.owned_objects")
        except MooError:
            return None
        for o in owned if isinstance(owned, list) else []:
            try:
                if self.eval(f"{o}.name") in (TOOLBOX_NAME, LEGACY_TOOLBOX_NAME):
                    return o
            except MooError:
                continue
        return None

    def read_registry(self) -> dict[str, Obj]:
        return registry_value(self.eval(f"{self.toolbox}.registry"))

    def read_sysrefs(self) -> dict[str, Obj]:
        return {name: obj for name, obj in self.eval(self.helper("tmoo_sysrefs"))}

    def refs(self) -> Refs:
        return Refs(player=self.player, registry=self.read_registry(), sysrefs=self.read_sysrefs())

    def save_state(self, registry: dict[str, Obj]) -> None:
        save_state(self.state_path, self.player, registry, self._toolbox)

    def load_state(self):
        return load_state(self.state_path)
