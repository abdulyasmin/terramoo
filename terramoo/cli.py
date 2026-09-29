"""`tmoo`: the command line."""

from __future__ import annotations

import argparse
import difflib
import getpass
import sys
from pathlib import Path
from uuid import uuid4

from . import apply as apply_mod
from . import export as export_mod
from . import moolit, objdef
from . import plan as plan_mod
from .errors import MooError
from .model import ordered_like
from .moolit import Obj
from .secrets import check_secret, store_secret
from .world import World, find_root, validate_key

_open: list[World] = []


def _registry_key(refs, name: str) -> str | None:
    if hasattr(refs, "registry_key"):
        return refs.registry_key(name)
    return next((key for key in refs.registry if key.lower() == name.lower()), None)


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
    secret = getpass.getpass(f"password (or MCP token) for world {args.world_name}: ")
    print(f"stored in {store_secret(args.world_name, check_secret(secret))}")


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
        notes = []
        if key.lower() not in {name.lower() for name in files}:
            notes.append("no file")
        if not refs.generations.get(key):
            notes.append("unverified; use adopt --verify")
        if key in refs.legacy_keys:
            notes.append(f"legacy key; use tmoo rename-key {key!r} NEW")
        mark = f"  ({'; '.join(notes)})" if notes else ""
        print(f"  {key:32} {o}{mark}")
    for key in sorted(files):
        if refs.registry_key(key) is None:
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


def _write_exports(w: World, refs, keys) -> int:
    """Write the files for `keys` from the live objects; how many were written."""
    safe_keys = []
    for key in keys:
        actual_key = _registry_key(refs, key) or key
        if not objdef.is_identifier(actual_key):
            print(
                f"  {actual_key}: legacy registry key; use `tmoo rename-key {actual_key!r} NEW`",
                file=sys.stderr,
            )
            continue
        safe_keys.append(key)
    keys = safe_keys
    live = export_mod.export(w, refs, keys)
    existing = {path.stem.lower(): path.stem for path in w.objects_dir.glob("*.moo")}
    written = 0
    for key in keys:
        actual_key = _registry_key(refs, key) or key
        obj = live.get(actual_key)
        if obj is None:
            print(
                f"  {actual_key}: registry names {refs.registry[actual_key]} but the MOO has no such object",
                file=sys.stderr,
            )
            continue
        file_key = existing.get(actual_key.lower(), actual_key)
        obj.key = file_key
        path = w.file_for(file_key)
        if path.exists():
            try:
                ordered_like(obj, objdef.parse(path.read_text()))
            except ValueError:
                pass  # an unreadable file is simply replaced
        w.write_file(obj)
        written += 1
    return written


def cmd_pull(args):
    w = _world(args)
    refs = w.refs()
    requested = args.keys or sorted(refs.registry)
    missing = [k for k in requested if refs.registry_key(k) is None]
    if missing:
        raise MooError(f"not in the registry: {', '.join(missing)}")
    keys = [refs.registry_key(key) for key in requested]
    written = _write_exports(w, refs, keys)
    w.save_state(refs.snapshot())
    print(f"wrote {written} file(s) under {w.objects_dir.relative_to(w.root)}")


def cmd_adopt(args):
    w = _world(args)
    refs = w.refs()
    new: list[tuple[str, Obj]] = []
    verify = getattr(args, "verify", False)
    if verify and args.owned:
        raise MooError("--verify is only for one existing binding: tmoo adopt <#n> <key> --verify")
    if args.owned:
        owned = w.server_info()[0]
        if owned is None:
            raise MooError("this core keeps no owned_objects list; adopt objects one at a time: tmoo adopt <#n> <key>")
        stray = _stray(w, refs, owned)
        taken = {key.lower() for key in refs.registry} | {p.stem.lower() for p in w.objects_dir.glob("*.moo")}
        for o, n in zip(stray, w.names(stray)):
            new.append((export_mod.slug(n, taken), o))
    else:
        if not args.object or not args.key:
            raise MooError("usage: tmoo adopt <#n> <key>  |  tmoo adopt --owned")
        o = parse_object_arg(args.object)
        requested_key = validate_key(args.key)
        existing_key = next((key for key in refs.registry if key.lower() == requested_key.lower()), None)
        if verify:
            if existing_key is None or refs.registry[existing_key] != o:
                actual = refs.registry.get(existing_key) if existing_key is not None else "not registered"
                raise MooError(f"cannot verify {requested_key} as {o}: current binding is {actual}")
            new.append((existing_key, o))
        elif existing_key is not None:
            raise MooError(f"{requested_key} is already {refs.registry[existing_key]}")
        managed_as = next((key for key, obj in refs.registry.items() if obj == o), None)
        if not verify and managed_as is not None:
            raise MooError(f"{o} is already managed as {managed_as}")
        if not verify:
            new.append((requested_key, o))
    if not new:
        print("nothing to adopt")
        return
    if hasattr(w, "require_helper_version"):
        w.require_helper_version()
    registrations = [(k, o, uuid4().hex) for k, o in new]
    ops = [["register", k, o, nonce] for k, o, nonce in registrations]
    results = w.eval(w.helper("tmoo_apply", w.transport.serialize(ops)))
    successful = []
    problems = []
    if not isinstance(results, list):
        problem = f"malformed helper result list: {results!r}"
        print(f"  {problem}")
        problems.append(problem)
        results = []
    elif len(results) != len(new):
        problem = f"{len(new)} registrations sent, {len(results)} results"
        print(f"  {problem}")
        problems.append(problem)
    for i, (k, o, nonce) in enumerate(registrations):
        if i >= len(results):
            continue
        res = results[i]
        if isinstance(res, list) and len(res) == 2 and type(res[0]) is int and res[0] == 1 and res[1] == o:
            successful.append((k, o, nonce))
        elif (
            isinstance(res, list)
            and len(res) == 3
            and type(res[0]) is int
            and res[0] == 0
            and isinstance(res[1], str)
            and isinstance(res[2], str)
        ):
            problem = f"{k}: {res[1]} {res[2]}"
            print(f"  {problem}")
            problems.append(problem)
        else:
            problem = f"{k}: malformed helper result: {res!r}"
            print(f"  {problem}")
            problems.append(problem)
    refs.replace_registry(w.read_registry())
    export_keys = []
    for requested, obj, nonce in successful:
        actual = next(
            (key for key, registered in refs.registry.items()
             if key.lower() == requested.lower()
             and registered == obj
             and refs.generations.get(key) == nonce),
            None,
        )
        if actual is None:
            problem = f"{requested}: successful registration is absent from the registry"
            print(f"  {problem}")
            problems.append(problem)
            continue
        print(f"  {actual} = {obj}")
        export_keys.append(actual)
    if export_keys:
        _write_exports(w, refs, export_keys)
    w.save_state(refs.snapshot())
    if problems:
        raise MooError(f"adoption failed: {'; '.join(problems)}")


def cmd_rename_key(args):
    """Atomically rename one registry binding, then its local file if any."""
    w = _world(args)
    refs = w.refs()
    old = refs.registry_key(args.old)
    if old is None:
        raise MooError(f"{args.old!r} is not in the registry")
    new = validate_key(args.new)
    occupied = refs.registry_key(new)
    if occupied is not None and occupied != old:
        raise MooError(f"{new} is already {refs.registry[occupied]}")

    source = next(
        (path for path in w.objects_dir.glob("*.moo") if path.stem.lower() == old.lower()),
        None,
    )
    destination = w.file_for(new) if source is not None else None
    rendered = None
    same_path = False
    if source is not None:
        if destination.exists():
            try:
                same_path = source.samefile(destination)
            except OSError:
                same_path = False
            if not same_path:
                raise MooError(f"cannot rename {source.name}: {destination.name} already exists")
        try:
            local = objdef.parse(source.read_text())
        except ValueError as e:
            raise MooError(f"cannot rename {source.name}: {e}") from None
        local.key = new
        rendered = objdef.render(local)

    w.require_helper_version()
    obj = refs.registry[old]
    nonce = refs.generation_for(old) or ""
    op = ["rename", old, obj, nonce, new]
    results = w.eval(w.helper("tmoo_apply", w.transport.serialize([op])))
    if not (
        isinstance(results, list)
        and len(results) == 1
        and isinstance(results[0], list)
        and len(results[0]) == 2
        and results[0][0] == 1
        and results[0][1] == obj
    ):
        if (
            isinstance(results, list)
            and len(results) == 1
            and isinstance(results[0], list)
            and len(results[0]) >= 3
            and results[0][0] == 0
        ):
            raise MooError(f"rename failed: {results[0][1]}: {results[0][2]}")
        raise MooError(f"rename failed: malformed helper result: {results!r}")

    updated = w.read_registry()
    actual = next(
        (
            key for key, value in updated.items()
            if key.lower() == new.lower()
            and value == obj
            and updated.generations.get(key) == (nonce or None)
        ),
        None,
    )
    if actual is None:
        raise MooError(f"rename returned success, but {new!r} is absent from the registry")
    refs.replace_registry(updated)
    w.save_state(refs.snapshot())

    if source is not None and rendered is not None:
        temp = w.objects_dir / f".{new}.{uuid4().hex}.tmp"
        try:
            temp.write_text(rendered)
            temp.replace(destination)
            if not same_path and source.exists():
                source.unlink()
        finally:
            if temp.exists():
                temp.unlink()
    print(f"renamed {old} to {actual} ({obj})")


def parse_object_arg(text: str) -> Obj:
    """`#123`, `123` or a mooR UUID object (`#048D05-1234567890`)."""
    text = text.strip()
    try:
        value = moolit.parse(text if text.startswith("#") else "#" + text)
    except moolit.LiteralError:
        value = None
    if not isinstance(value, Obj):
        raise MooError(f"not an object number: {text!r} (expected #123)")
    return value


def _plan(w: World):
    refs = w.refs()
    files = w.load_files()
    live = export_mod.export(
        w,
        refs,
        [registry_key for key in files if (registry_key := refs.registry_key(key)) is not None],
    )
    return refs, files, plan_mod.build(files, live, refs)


def _print_plan(p: plan_mod.Plan, destroy: bool):
    for key, parent, name in p.creates:
        why = f"  [{p.gone[key]} is gone from the MOO; recreating from the file]" if key in p.gone else ""
        print(f"  + create {key} ({name}) as child of {parent}{why}")
    for op in p.ops:
        print(f"  ~ {plan_mod.describe(op)}")
    for key in p.destroys:
        print(f"  - {'recycle' if destroy else 'orphan (no file; --destroy recycles)'} {key}")
    for warning in p.warnings:
        print(f"  ? {warning}")
    for prob in p.problems:
        print(f"  ! {prob}")
    if p.unchanged:
        print(f"  = {len(p.unchanged)} unchanged")


def cmd_plan(args):
    w = _world(args)
    _, _, p = _plan(w)
    if p.empty and not p.problems and not p.warnings:
        print("no changes")
        return
    _print_plan(p, args.destroy)
    if p.problems:
        sys.exit(2)


def cmd_apply(args):
    w = _world(args)
    refs, files, p = _plan(w)
    if p.problems:
        _print_plan(p, args.destroy)
        raise MooError("fix the problems above first")
    if p.empty:
        print("no changes")
        w.save_state(refs.snapshot())
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
    if args.keys:
        keys = args.keys
    else:
        keys = list(files)
        keys.extend(key for key in refs.registry if key.lower() not in {name.lower() for name in files})
    live_keys = [registry_key for key in keys if (registry_key := refs.registry_key(key)) is not None]
    live = export_mod.export(w, refs, live_keys)
    files_by_name = {key.lower(): key for key in files}
    live_by_name = {key.lower(): key for key in live}
    changed = 0
    for key in keys:
        file_key = files_by_name.get(key.lower())
        live_key = live_by_name.get(key.lower())
        live_obj = live.get(live_key) if live_key is not None else None
        file_obj = files.get(file_key) if file_key is not None else None
        if live_obj is not None and file_obj is not None:
            live_obj.key = file_obj.key
        a = objdef.render(live_obj).splitlines(keepends=True) if live_obj else []
        b = objdef.render(file_obj).splitlines(keepends=True) if file_obj else []
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
    a.add_argument("--verify", action="store_true", help="stamp an existing key/object binding after confirming its identity")
    a.set_defaults(fn=cmd_adopt)

    rk = sub.add_parser("rename-key", help="rename a registry key and its local object file")
    rk.add_argument("old", help="existing registry key, including a legacy key")
    rk.add_argument("new", help="new ASCII identifier")
    rk.set_defaults(fn=cmd_rename_key)

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
