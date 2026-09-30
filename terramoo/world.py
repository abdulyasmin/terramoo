"""A world: one MOO, one player, one directory, `worlds/<name>/`, holding
`world.toml` (see README), `objects/*.moo` and `state.json`, a copy of the
in-MOO registry.

The toolbox is an object the player owns, reached as `player.tmoo`,
holding the registry and the helper verbs.  It is the only thing `tmoo`
creates that the files do not describe.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import objdef
from .errors import MooError
from .model import ObjectDef
from .moolit import Err, Obj
from .refs import Refs, Registry, save_state
from .secrets import secret_for
from .transport import Transport, connect

HELPER_DIR = Path(__file__).parent / "helper"
HELPER_VERBS = (
    "tmoo_registry",
    "tmoo_callback",
    "tmoo_generation",
    "tmoo_export",
    "tmoo_apply",
    "tmoo_sysrefs",
    "tmoo_info",
)
SUSPENDING_HELPERS = ("tmoo_export", "tmoo_apply")
TOOLBOX_NAME = "terramoo toolbox"
TOOLBOX_PROP = "tmoo"
GENERATION_PROP = "_terramoo_generation"
HELPER_VERSION = 12
HELPER_VERSION_PROP = "_terramoo_helper_version"
REGISTRY_STATE_PROP = "_terramoo_registry_state"
REGISTRY_REVISION_PROP = "_terramoo_registry_revision"

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
    GENERATION_PROP,
    TOOLBOX_PROP,
}


def find_root() -> Path:
    """The nearest directory with a `worlds/` in it, or `$TMOO_ROOT`."""
    env = os.environ.get("TMOO_ROOT")
    if env:
        return Path(env)
    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        if (d / "worlds").is_dir():
            return d
    raise MooError("no worlds/ directory here or above (run `tmoo init <world>` to start one, or set TMOO_ROOT)")


def registry_value(raw) -> Registry:
    """The registry as `tmoo_apply` keeps it: {keys, objects, nonces, revision}.

    The old two- and three-list shapes remain readable so status can explain
    legacy bindings and bootstrap can migrate them on the next mutation.
    """
    if (
        isinstance(raw, list)
        and len(raw) in (2, 3, 4)
        and all(isinstance(x, list) for x in raw[:3])
    ):
        keys, objects = raw[:2]
        nonces = raw[2] if len(raw) == 3 else [None] * len(keys)
        if len(raw) == 4:
            nonces = raw[2]
        revision = raw[3] if len(raw) == 4 else 0
        valid_keys = all(isinstance(k, str) for k in keys)
        valid_objects = all(isinstance(o, Obj) for o in objects)
        valid_nonces = all(n is None or isinstance(n, str) for n in nonces)
        unique_keys = valid_keys and len({k.lower() for k in keys}) == len(keys)
        unique_objects = valid_objects and len(set(objects)) == len(objects)
        present_nonces = [n for n in nonces if n]
        unique_nonces = len(set(present_nonces)) == len(present_nonces)
        if (
            len(keys) == len(objects) == len(nonces)
            and valid_keys
            and valid_objects
            and valid_nonces
            and unique_keys
            and unique_objects
            and unique_nonces
            and type(revision) is int
            and revision >= 0
        ):
            normalized_nonces = [n or None for n in nonces]
            return Registry(
                zip(keys, objects), dict(zip(keys, normalized_nonces)), revision
            )
    raise MooError("toolbox has a malformed registry")


def validate_key(value: object) -> str:
    """Return an object/registry key, or reject it before it reaches a path or MOO."""
    try:
        return objdef.validate_identifier(value, "registry key")
    except objdef.FormatError as e:
        raise MooError(str(e)) from None


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
    _helper_version_checked: bool = False

    @classmethod
    def load(cls, root: Path, name: str | None) -> "World":
        worlds_dir = root / "worlds"
        worlds = sorted(p.name for p in worlds_dir.iterdir() if (p / "world.toml").exists()) if worlds_dir.is_dir() else []
        if name is None:
            name = os.environ.get("TMOO_WORLD") or (worlds[0] if len(worlds) == 1 else None)
        if name is None:
            raise MooError(f"which world? one of: {', '.join(worlds) or '(none)'} (pass --world or set TMOO_WORLD)")
        cfg_path = worlds_dir / name / "world.toml"
        if not cfg_path.exists():
            raise MooError(f"no such world {name!r} (looked for {cfg_path})")
        cfg = tomllib.loads(cfg_path.read_text())
        conn = dict(cfg.get("connection", {}))
        if "player" not in cfg:
            raise MooError(f"{cfg_path}: `player` is required")
        core = dict(cfg.get("core", {}))
        ignore = {n.lower() for n in (
            *DEFAULT_IGNORE_PROPS,
            *cfg.get("ignore_props", []),
            *core.get("ignore_props", []),
        )}
        ignore -= {n.lower() for n in (
            *cfg.get("keep_props", []), *core.get("keep_props", []),
        )}
        ignore.add(GENERATION_PROP)
        return cls(name=name, root=root, player_name=cfg["player"], connection=conn,
                   core=core, ignore_props=ignore)

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
        key = validate_key(key)
        path = self.objects_dir / f"{key}.moo"
        self._check_object_path(path)
        return path

    def _check_object_path(self, path: Path) -> None:
        try:
            path.resolve().relative_to(self.objects_dir.resolve())
        except ValueError:
            raise MooError(f"refusing to write {path}: path is outside the objects directory") from None

    def describe(self) -> str:
        c = self.connection
        if c.get("transport", "telnet") == "mcp":
            return c.get("url", "?")
        return f"{'tls' if c.get('tls') else 'telnet'}://{c.get('host')}:{c.get('port', 7777)}"

    # ----- files

    def load_files(
        self,
        snapshots: dict[Path, tuple[bytes, tuple[int, int, int, int]]] | None = None,
        *,
        allowed_case_pair: tuple[str, str] | None = None,
    ) -> dict[str, ObjectDef]:
        out = {}
        folded: dict[str, list[str]] = {}
        allowed_folded_pair = None
        if (
            allowed_case_pair is not None
            and allowed_case_pair[0] != allowed_case_pair[1]
            and allowed_case_pair[0].lower() == allowed_case_pair[1].lower()
        ):
            allowed_folded_pair = frozenset(allowed_case_pair)
        for path in sorted(self.objects_dir.glob("*.moo")):
            try:
                validate_key(path.stem)
            except MooError as e:
                raise MooError(f"{path.relative_to(self.root)}: {e}") from None
            try:
                if snapshots is None:
                    obj = objdef.parse(path.read_text())
                else:
                    with path.open("rb") as stream:
                        before = os.fstat(stream.fileno())
                        data = stream.read()
                        after = os.fstat(stream.fileno())
                    current = path.stat()
                    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    if identity != (
                        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
                    ) or identity != (
                        current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns
                    ):
                        raise MooError(f"{path.relative_to(self.root)} changed while it was being read")
                    obj = objdef.parse(data.decode("utf-8"))
            except ValueError as e:  # FormatError and LiteralError are ValueErrors
                raise MooError(f"{path.relative_to(self.root)}: {e}") from None
            if snapshots is not None:
                snapshots[path] = (data, identity)
            if obj.key != path.stem:
                raise MooError(f"{path.relative_to(self.root)}: file is named {path.stem!r} but declares object {obj.key!r}")
            # MOO string comparison ignores case, and so does the registry.
            same_fold = [*folded.get(obj.key.lower(), []), obj.key]
            if len(same_fold) > 1 and not (
                len(same_fold) == 2
                and frozenset(same_fold) == allowed_folded_pair
            ):
                raise MooError(
                    f"keys {same_fold[0]!r} and {obj.key!r} differ only in case"
                )
            folded[obj.key.lower()] = same_fold
            out[obj.key] = obj
        return out

    def write_file(self, obj: ObjectDef, *, exclusive: bool = False) -> Path:
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        path = self.file_for(obj.key)
        self._check_object_path(path)
        text = objdef.render(obj)
        with path.open("x" if exclusive else "w") as stream:
            stream.write(text)
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

    def require_helper_version(self) -> None:
        if self._helper_version_checked:
            return
        props = self.eval(f"properties({self.toolbox})")
        version = self.eval(f"{self.toolbox}.{HELPER_VERSION_PROP}") if HELPER_VERSION_PROP in props else None
        if version != HELPER_VERSION:
            raise MooError(
                f"toolbox helpers are outdated (need version {HELPER_VERSION}); run `tmoo bootstrap`"
            )
        self._helper_version_checked = True

    @property
    def player(self) -> Obj:
        if self._player is None:
            who, name = self.eval("{player, player.name}")
            if name.lower() != self.player_name.lower():
                raise MooError(f"logged in as {name} ({who}), but world.toml says player = {self.player_name!r}")
            self._player = who
        return self._player

    def _find_toolbox(self) -> Obj | None:
        if self.eval(f'"{TOOLBOX_PROP}" in properties(player) && valid(player.{TOOLBOX_PROP})'):
            return self.eval(f"player.{TOOLBOX_PROP}")
        return None

    @property
    def toolbox(self) -> Obj:
        if self._toolbox is None:
            tb = self._find_toolbox()
            if tb is None:
                raise MooError("no toolbox on this player yet: run `tmoo bootstrap`")
            self._toolbox = tb
        return self._toolbox

    def server_info(self) -> tuple[list[Obj] | None, str]:
        """What the player owns, when the core keeps a list (LambdaCore's
        `owned_objects`; None when it does not), and `server_version()`."""
        owned, version = self.eval(self.helper("tmoo_info"))
        return (None if isinstance(owned, Err) else list(owned)), version

    def names(self, objs: list[Obj]) -> list[str]:
        if not objs:
            return []
        return [name for _, _, name in self.eval(self.helper("tmoo_info", self.transport.serialize(objs)))]

    def bootstrap(self, log=print) -> Obj:
        """Find or create the toolbox and (re)install the helper verbs."""
        self.player  # checks the login matches world.toml
        tb = self._find_toolbox()
        if tb is not None:
            log(f"toolbox is {tb}")
        else:
            tb = self._find_orphan_toolbox()
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
        props = self.eval(f"properties({tb})")
        if "registry" not in props:
            self.eval(f'add_property({tb}, "registry", {{{{}}, {{}}, {{}}, 0}}, {{player, "r"}})')
        for name in HELPER_VERBS:
            code = (HELPER_DIR / f"{name}.moo").read_text().splitlines()
            r = self.transport.install_verb(tb, name, code)
            log(f"{tb}:{name} {r}")
        # One non-suspending MOO task rereads and migrates all registry state.
        # No MOO task can interleave between that read and the three writes.
        self.eval(f'{tb}:tmoo_registry("bootstrap")')
        if HELPER_VERSION_PROP in props:
            self.transport.set_prop(tb, HELPER_VERSION_PROP, HELPER_VERSION)
        else:
            self.eval(
                f'add_property({tb}, "{HELPER_VERSION_PROP}", {HELPER_VERSION}, {{player, "r"}})'
            )
        self._helper_version_checked = True
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
                if self.eval(f"{o}.name") == TOOLBOX_NAME:
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
