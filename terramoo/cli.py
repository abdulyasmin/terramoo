"""`tmoo`: the command line."""

from __future__ import annotations

import argparse
import difflib
import getpass
import sys
from pathlib import Path

from . import apply as apply_mod
from . import export as export_mod
from . import moolit, objdef
from . import plan as plan_mod
from .errors import MooError
from .model import ordered_like
from .moolit import Obj
from .secrets import check_secret, store_secret
from .world import World, find_root

_open: list[World] = []


def _world(args) -> World:
    w = World.load(find_root(), args.world)
    _open.append(w)
    return w


WORLD_TOML = """\
# {name}: {where}, as {player}.
# The password (or MCP token) is in the Keychain via `tmoo secret store {name}`,
# never in this file.
player = "{player}"

[connection]
{connection}
[core]
toolbox_parent = "$thing"

# Runtime-state properties never written to files, beyond the built-in list
# in terramoo/world.py.  `keep_props` re-enables one from that list.
ignore_props = []
keep_props = []
"""


def cmd_init(args):
    root = Path(args.root or ".").resolve()
    d = root / "worlds" / args.name
    if (d / "world.toml").exists():
        raise MooError(f"{d / 'world.toml'} already exists")
    if args.url:
        conn = f'transport = "mcp"\nurl = "{args.url}"\n'
        where = args.url
    else:
        if not args.host:
            raise MooError("give --host (and --port, --tls) for telnet, or --url for mcp")
        conn = f'transport = "telnet"\nhost = "{args.host}"\nport = {args.port}\ntls = {"true" if args.tls else "false"}\n'
        where = f"{args.host}:{args.port}"
    (d / "objects").mkdir(parents=True, exist_ok=True)
    (d / "world.toml").write_text(WORLD_TOML.format(name=args.name, where=where, player=args.player, connection=conn))
    print(f"wrote {d / 'world.toml'}; next: `tmoo secret store {args.name}`, then `tmoo -w {args.name} bootstrap`")


def cmd_secret(args):
    if args.action == "store":
        secret = getpass.getpass(f"password (or MCP token) for world {args.world_name}: ")
        where = store_secret(args.world_name, check_secret(secret))
        print(f"stored in {where}")


def cmd_bootstrap(args):
    w = _world(args)
    tb = w.bootstrap()
    w.save_state(w.read_registry())
    print(f"toolbox {tb} ready; state written to {w.state_path.relative_to(w.root)}")


def cmd_status(args):
    w = _world(args)
    refs = w.refs()
    files = w.load_files()
    owned, version = w.server_info()
    print(f"world {w.name}: {w.describe()} ({version or 'unknown server'}) as {w.player_name} ({refs.player}), toolbox {w.toolbox}")
    print(f"registry: {len(refs.registry)} objects, files: {len(files)}")
    for key, o in sorted(refs.registry.items()):
        mark = "" if key in files else "  (no file)"
        print(f"  {key:32} {o}{mark}")
    for key in sorted(set(files) - set(refs.registry)):
        print(f"  {key:32} (not created yet)")
    if owned is None:
        print("(this core keeps no owned_objects list, so unmanaged objects are not listed)")
        return
    stray = _stray(w, refs, owned)
    if stray:
        print("owned but unmanaged (`tmoo adopt <#n> <key>` or `tmoo adopt --owned`):")
        for o, n in zip(stray, w.names(stray)):
            print(f"  {str(o):>6}  {n}")


def _stray(w: World, refs, owned: list[Obj]) -> list[Obj]:
    known = set(refs.registry.values()) | {refs.player, w.toolbox}
    return [o for o in owned if o not in known]


def _export_all(w: World, refs, keys):
    live = export_mod.export(w, refs, keys)
    written = []
    for key in keys:
        obj = live.get(key)
        if obj is None:
            print(f"  {key}: registry names {refs.registry[key]} but the MOO has no such object", file=sys.stderr)
            continue
        path = w.file_for(key)
        if path.exists():
            try:
                ordered_like(obj, objdef.parse(path.read_text()))
            except (objdef.FormatError, ValueError):
                pass  # an unreadable file is simply replaced
        w.write_file(obj)
        written.append(key)
    return written


def cmd_pull(args):
    w = _world(args)
    refs = w.refs()
    keys = args.keys or sorted(refs.registry)
    missing = [k for k in keys if k not in refs.registry]
    if missing:
        raise MooError(f"not in the registry: {', '.join(missing)}")
    written = _export_all(w, refs, keys)
    w.save_state(refs.registry)
    print(f"wrote {len(written)} file(s) under {w.objects_dir.relative_to(w.root)}")


def cmd_adopt(args):
    w = _world(args)
    refs = w.refs()
    new: list[tuple[str, Obj]] = []
    if args.owned:
        owned = w.owned()
        if owned is None:
            raise MooError("this core keeps no owned_objects list; adopt objects one at a time: tmoo adopt <#n> <key>")
        stray = _stray(w, refs, owned)
        taken = set(refs.registry) | {p.stem for p in w.objects_dir.glob("*.moo")}
        for o, n in zip(stray, w.names(stray)):
            new.append((export_mod.slug(n, taken), o))
    else:
        if not args.object or not args.key:
            raise MooError("usage: tmoo adopt <#n> <key>  |  tmoo adopt --owned")
        o = Obj(int(args.object.lstrip("#")))
        if args.key in refs.registry:
            raise MooError(f"{args.key} is already {refs.registry[args.key]}")
        new.append((args.key, o))
    if not new:
        print("nothing to adopt")
        return
    ops = [["register", k, o] for k, o in new]
    results = w.eval(w.helper("tmoo_apply", moolit.serialize(ops)))
    for (k, o), res in zip(new, results):
        print(f"  {k} = {o}" if res[0] == 1 else f"  {k}: {res[1]} {res[2]}")
    refs.registry = w.read_registry()
    refs.reindex()
    _export_all(w, refs, [k for k, _ in new])
    w.save_state(refs.registry)


def _plan(w: World):
    refs = w.refs()
    files = w.load_files()
    live = export_mod.export(w, refs, [k for k in files if k in refs.registry])
    return refs, files, live, plan_mod.build(files, live, refs)


def _print_plan(p: plan_mod.Plan, destroy: bool):
    for key, parent, name in p.creates:
        why = f"  [{p.gone[key]} is gone from the MOO; recreating from the file]" if key in p.gone else ""
        print(f"  + create {key} ({name}) as child of {parent}{why}")
    for op in p.ops:
        print(f"  ~ {plan_mod.describe(op)}")
    for key in p.destroys:
        print(f"  - {'recycle' if destroy else 'orphan (no file; --destroy recycles)'} {key}")
    for prob in p.problems:
        print(f"  ! {prob}")
    if p.unchanged:
        print(f"  = {len(p.unchanged)} unchanged")


def cmd_plan(args):
    w = _world(args)
    _, _, _, p = _plan(w)
    if p.empty and not p.problems:
        print("no changes")
        return
    _print_plan(p, args.destroy)
    if p.problems:
        sys.exit(2)


def cmd_apply(args):
    w = _world(args)
    refs, files, _, p = _plan(w)
    if p.problems:
        _print_plan(p, args.destroy)
        raise MooError("fix the problems above first")
    if p.empty:
        print("no changes")
        w.save_state(refs.registry)
        return
    _print_plan(p, args.destroy)
    if not args.yes:
        answer = input("apply? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return
    outcome = apply_mod.run(w, p, refs, files=files, destroy=args.destroy)
    if outcome.failed:
        print(f"{len(outcome.failed)} op(s) failed:")
        for label, why in outcome.failed:
            print(f"  {label}: {why}")
        sys.exit(1)
    print(f"applied {len(outcome.done)} op(s)")


def cmd_diff(args):
    w = _world(args)
    refs = w.refs()
    files = w.load_files()
    keys = args.keys or sorted(set(files) | set(refs.registry))
    live = export_mod.export(w, refs, [k for k in keys if k in refs.registry])
    changed = 0
    for key in keys:
        a = objdef.render(live[key]).splitlines(keepends=True) if live.get(key) else []
        b = objdef.render(files[key]).splitlines(keepends=True) if key in files else []
        if a == b:
            continue
        changed += 1
        sys.stdout.writelines(difflib.unified_diff(a, b, fromfile=f"moo/{key}.moo", tofile=f"files/{key}.moo"))
    if not changed:
        print("no differences")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tmoo", description="a MOO player's objects as files, on any MOO")
    ap.add_argument("--world", "-w", help="world name under worlds/ (default: the only one, or $TMOO_WORLD)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="start a world: worlds/<name>/world.toml")
    i.add_argument("name")
    i.add_argument("--player", required=True)
    i.add_argument("--host")
    i.add_argument("--port", type=int, default=7777)
    i.add_argument("--tls", action="store_true")
    i.add_argument("--url", help="an MCP endpoint instead of telnet")
    i.add_argument("--root", help="the directory to hold worlds/ (default: here)")
    i.set_defaults(fn=cmd_init)

    t = sub.add_parser("secret", help="store the world's password or MCP token")
    t.add_argument("action", choices=["store"])
    t.add_argument("world_name")
    t.set_defaults(fn=cmd_secret)

    sub.add_parser("bootstrap", help="create the toolbox and install the helper verbs").set_defaults(fn=cmd_bootstrap)
    sub.add_parser("status", help="registry, files and unmanaged owned objects").set_defaults(fn=cmd_status)

    p = sub.add_parser("pull", help="write files from the live objects (all, or the given keys)")
    p.add_argument("keys", nargs="*")
    p.set_defaults(fn=cmd_pull)
    sub.add_parser("export", help="alias of pull with no keys").set_defaults(fn=cmd_pull, keys=[])

    a = sub.add_parser("adopt", help="put an existing object under management")
    a.add_argument("object", nargs="?", help="#123")
    a.add_argument("key", nargs="?", help="registry name")
    a.add_argument("--owned", action="store_true", help="adopt every owned object not yet managed")
    a.set_defaults(fn=cmd_adopt)

    pl = sub.add_parser("plan", help="show what apply would do")
    pl.add_argument("--destroy", action="store_true", help="include recycling objects with no file")
    pl.set_defaults(fn=cmd_plan)

    ac = sub.add_parser("apply", help="make the MOO match the files")
    ac.add_argument("--destroy", action="store_true", help="recycle registry objects with no file")
    ac.add_argument("--yes", "-y", action="store_true", help="do not ask")
    ac.set_defaults(fn=cmd_apply)

    d = sub.add_parser("diff", help="unified diff, live rendering against the files")
    d.add_argument("keys", nargs="*")
    d.set_defaults(fn=cmd_diff)

    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except MooError as e:
        print(f"tmoo: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        for w in _open:
            w.close()


if __name__ == "__main__":
    main()
