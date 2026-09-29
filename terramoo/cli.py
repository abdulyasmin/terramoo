"""`tmoo`: the command line."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import difflib
import fcntl
import getpass
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from . import apply as apply_mod
from . import export as export_mod
from . import moolit, objdef
from . import plan as plan_mod
from .errors import MooError
from .model import ordered_like
from .moolit import Obj, Ref, walk
from .refs import Registry, save_state as write_state
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
    with _world_write_lock(w):
        _bootstrap_locked(w)


def _bootstrap_locked(w: World) -> None:
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
    with _world_write_lock(w):
        _pull_locked(w, args)


def _pull_locked(w: World, args) -> None:
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
    with _world_write_lock(w):
        _adopt_locked(w, args)


def _adopt_locked(w: World, args) -> None:
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


def _rewrite_object_refs(obj, old: str, new: str) -> bool:
    """Rewrite parsed @refs only; verb code and arbitrary text stay untouched."""
    changed = False

    def rename(value):
        nonlocal changed
        if isinstance(value, Ref) and value.kind == "@" and value.name.lower() == old.lower():
            changed = True
            return Ref("@", new)
        return value

    obj.parent = walk(obj.parent, rename)
    obj.location = walk(obj.location, rename)
    obj.owner = walk(obj.owner, rename)
    for prop in obj.props:
        prop.value = walk(prop.value, rename)
        prop.owner = walk(prop.owner, rename)
    for verb in obj.verbs:
        verb.owner = walk(verb.owner, rename)
    return changed


def _temp_for(path: Path, label: str) -> Path:
    return path.with_name(f".{path.name}.{label}.{uuid4().hex}.tmp")


def _write_fsynced(path: Path, data: str | bytes) -> None:
    mode = "xb" if isinstance(data, bytes) else "x"
    kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8"}
    with path.open(mode, **kwargs) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_result(
    w: World, index: int, revision: int, obj: Obj, nonce: str, new: str
):
    op = ["rename", index, revision, obj, nonce, new]
    results = w.eval(w.helper("tmoo_apply", w.transport.serialize([op])))
    if (
        isinstance(results, list)
        and len(results) == 1
        and isinstance(results[0], list)
        and len(results[0]) == 2
        and results[0][0] == 1
        and results[0][1] == obj
    ):
        return
    if (
        isinstance(results, list)
        and len(results) == 1
        and isinstance(results[0], list)
        and len(results[0]) >= 3
        and results[0][0] == 0
    ):
        raise MooError(f"rename failed: {results[0][1]}: {results[0][2]}")
    raise MooError(f"rename failed: malformed helper result: {results!r}")


def _cleanup(paths) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def _world_write_lock(w: World):
    if not hasattr(w, "dir"):
        yield
        return
    lock_dir = w.dir / ".cache"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "write.lock"
    with lock_path.open("a+b") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MooError(
                f"another file-writing command is already running for world {w.name}"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write_rename_journal(path: Path, record: dict) -> None:
    temporary = _temp_for(path, "new")
    try:
        _write_fsynced(temporary, json.dumps(record, indent=2, ensure_ascii=False) + "\n")
        temporary.replace(path)
        _fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _matching_registry_key(registry: Registry, key: str, obj: Obj, nonce: str) -> str | None:
    return next(
        (
            actual for actual, value in registry.items()
            if actual == key and value == obj and registry.generations.get(actual) == (nonce or None)
        ),
        None,
    )


class _LocalEditError(MooError):
    pass


def _assert_file_snapshot(path: Path, data: bytes, identity: tuple[int, int, int, int]) -> None:
    try:
        with path.open("rb") as stream:
            current_identity = os.fstat(stream.fileno())
            current_data = stream.read()
        named_identity = path.stat()
    except OSError as e:
        raise _LocalEditError(f"{path.name} changed after it was read: {e}") from None
    current = (
        current_identity.st_dev,
        current_identity.st_ino,
        current_identity.st_size,
        current_identity.st_mtime_ns,
    )
    named = (
        named_identity.st_dev,
        named_identity.st_ino,
        named_identity.st_size,
        named_identity.st_mtime_ns,
    )
    if current != identity or named != identity or current_data != data:
        raise _LocalEditError(f"{path.name} changed after it was read")


def _assert_object_snapshots(
    w: World,
    snapshots: dict[Path, tuple[bytes, tuple[int, int, int, int]]],
    object_files: frozenset[Path],
) -> None:
    for path, (data, identity) in snapshots.items():
        _assert_file_snapshot(path, data, identity)
    current = frozenset(w.objects_dir.glob("*.moo"))
    if current != object_files:
        added = sorted(path.name for path in current - object_files)
        removed = sorted(path.name for path in object_files - current)
        detail = []
        if added:
            detail.append(f"appeared: {', '.join(added)}")
        if removed:
            detail.append(f"disappeared: {', '.join(removed)}")
        raise _LocalEditError(
            f"objects directory changed after it was read ({'; '.join(detail)})"
        )


def _recovery_temp_paths(w: World, record: dict) -> list[Path]:
    raw_paths = record.get("preserved_temporary_files")
    if not isinstance(raw_paths, list):
        raise MooError("rename recovery journal has no temporary-file list")
    allowed_parents = {w.objects_dir.resolve(), w.state_path.parent.resolve()}
    paths = []
    for raw in raw_paths:
        if not isinstance(raw, str):
            raise MooError("rename recovery journal has an invalid temporary-file path")
        path = Path(raw)
        if (
            path.parent.resolve() not in allowed_parents
            or not path.name.startswith(".")
            or not path.name.endswith(".tmp")
        ):
            raise MooError(f"rename recovery journal has an unsafe temporary-file path: {raw}")
        paths.append(path)
    return paths


def _finish_rename_recovery(w: World, journal: Path, record: dict) -> None:
    try:
        for path in _recovery_temp_paths(w, record):
            path.unlink(missing_ok=True)
        journal.unlink()
        _fsync_dir(journal.parent)
    except OSError as e:
        raise MooError(
            f"rename recovery succeeded but cleanup failed ({e}); journal preserved at {journal}"
        ) from None


def _read_rename_journal(w: World, journal: Path) -> tuple[dict, str, str, Obj, str]:
    try:
        record = json.loads(journal.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise MooError(f"cannot read rename recovery journal {journal}: {e}") from None
    if not isinstance(record, dict):
        raise MooError("rename recovery journal is not an object")
    old = record.get("old_key")
    new = record.get("new_key")
    generation = record.get("generation")
    index = record.get("registry_index")
    revision = record.get("registry_revision")
    if not isinstance(old, str) or not old:
        raise MooError("rename recovery journal has an invalid old key")
    try:
        new = validate_key(new)
        obj = parse_object_arg(record.get("object"))
    except (AttributeError, MooError, TypeError):
        raise MooError("rename recovery journal has an invalid new key or object") from None
    if (
        not isinstance(generation, str)
        or type(index) is not int
        or index < 1
        or type(revision) is not int
        or revision < 0
    ):
        raise MooError("rename recovery journal has invalid registry identity fields")
    _recovery_temp_paths(w, record)
    return record, old, new, obj, generation


def _recover_local_migration(
    w: World,
    journal: Path,
    record: dict,
    source_name: str,
    destination_name: str,
    registry: Registry,
) -> None:
    snapshots: dict[Path, tuple[bytes, tuple[int, int, int, int]]] = {}
    try:
        files = w.load_files(snapshots)
    except (OSError, ValueError) as e:
        raise MooError(f"cannot prepare rename recovery: {e}") from None
    object_files = frozenset(snapshots)
    source_key = next(
        (key for key in files if key.lower() == source_name.lower()), None
    )
    destination_key = next(
        (key for key in files if key.lower() == destination_name.lower()), None
    )
    changes = []
    remove_source = None
    for key, local in files.items():
        source = w.file_for(key)
        refs_changed = _rewrite_object_refs(local, source_name, destination_name)
        destination = source
        if key == source_key:
            local.key = destination_name
            destination = w.file_for(destination_name)
        if not refs_changed and key != source_key:
            continue
        rendered = objdef.render(local)
        same_path = source == destination
        if not same_path and destination.exists():
            try:
                same_path = source.samefile(destination)
            except OSError:
                same_path = False
        if key == source_key and destination_key is not None and destination_key != source_key:
            existing = snapshots[w.file_for(destination_key)][0]
            if objdef.parse(existing.decode("utf-8")) != local:
                raise MooError(
                    f"cannot recover rename: both {source.name} and {destination.name} exist"
                )
            remove_source = source
            continue
        changes.append((source, destination, same_path, rendered))

    temporary = []
    prepared = []
    state_temp = _temp_for(w.state_path, "recover")
    try:
        for source, destination, same_path, rendered in changes:
            new_temp = _temp_for(destination, "recover")
            _write_fsynced(new_temp, rendered)
            temporary.append(new_temp)
            original, identity = snapshots[source]
            prepared.append((source, destination, same_path, new_temp, original, identity))
        write_state(state_temp, w.player, registry, w.toolbox)
        with state_temp.open("rb") as stream:
            os.fsync(stream.fileno())
        temporary.append(state_temp)
        record.update(
            phase="recovery-local-prepared",
            preserved_temporary_files=[
                str(path)
                for path in dict.fromkeys([*_recovery_temp_paths(w, record), *temporary])
                if path.exists()
            ],
            recovery_commands=["tmoo rename-key --recover"],
        )
        _write_rename_journal(journal, record)
        _assert_object_snapshots(w, snapshots, object_files)
        for source, destination, same_path, new_temp, original, identity in prepared:
            _assert_file_snapshot(source, original, identity)
            if not same_path and destination.exists():
                raise _LocalEditError(
                    f"{destination.name} appeared during rename recovery"
                )
            new_temp.replace(destination)
            if not same_path:
                source.unlink()
            _fsync_dir(destination.parent)
        if remove_source is not None:
            original, identity = snapshots[remove_source]
            _assert_file_snapshot(remove_source, original, identity)
            remove_source.unlink()
            _fsync_dir(remove_source.parent)
        state_temp.replace(w.state_path)
        _fsync_dir(w.state_path.parent)
    except (OSError, MooError) as e:
        record.update(
            phase="recovery-local-incomplete",
            recovery_error=str(e),
            preserved_temporary_files=[
                str(path)
                for path in dict.fromkeys([*_recovery_temp_paths(w, record), *temporary])
                if path.exists()
            ],
            recovery_commands=["tmoo rename-key --recover"],
        )
        _write_rename_journal(journal, record)
        raise MooError(
            f"rename recovery could not finish local files ({e}); recovery journal preserved at {journal}"
        ) from None


def _recover_rename_key_locked(w: World, journal: Path) -> None:
    if not journal.exists():
        raise MooError(f"no unfinished rename recovery journal exists at {journal}")
    record, old, new, obj, nonce = _read_rename_journal(w, journal)
    w.require_helper_version()
    updated = w.read_registry()
    recorded_revision = record["registry_revision"]
    if updated.revision < recorded_revision:
        raise MooError(
            f"remote registry revision {updated.revision} predates recovery journal revision "
            f"{recorded_revision}; journal preserved at {journal}"
        )
    actual = _matching_registry_key(updated, new, obj, nonce)
    still_old = _matching_registry_key(updated, old, obj, nonce)
    if (
        actual is not None
        and still_old is None
        and updated.revision > recorded_revision
    ):
        _recover_local_migration(w, journal, record, old, new, updated)
        outcome = f"finished rename {old} to {actual} ({obj})"
    elif still_old is not None and actual is None:
        _recover_local_migration(w, journal, record, new, old, updated)
        outcome = f"restored rename {new} to {still_old} ({obj})"
    else:
        record.update(
            phase="remote-outcome-conflicted",
            registry_keys=list(updated),
            recovery_commands=["tmoo rename-key --recover"],
        )
        _write_rename_journal(journal, record)
        raise MooError(
            f"rename recovery does not match either {old!r} or {new!r} remotely; journal preserved at {journal}"
        )
    _finish_rename_recovery(w, journal, record)
    print(outcome)


def cmd_rename_key(args):
    """Lock, prepare local migration, CAS-rename, then finalize files."""
    w = _world(args)
    with _world_write_lock(w):
        journal = w.state_path.with_name("state.json.rename-recovery.json")
        if getattr(args, "recover", False):
            if getattr(args, "old", None) is not None or getattr(args, "new", None) is not None:
                raise MooError("rename-key --recover does not take OLD or NEW")
            _recover_rename_key_locked(w, journal)
        else:
            if getattr(args, "old", None) is None or getattr(args, "new", None) is None:
                raise MooError("rename-key needs OLD and NEW (or use --recover)")
            _rename_key_locked(w, args, journal)


def _rename_key_locked(w: World, args, journal: Path) -> None:
    if journal.exists():
        raise MooError(
            f"unfinished rename recovery journal exists at {journal}; inspect it before retrying"
        )
    refs = w.refs()
    old = refs.registry_key(args.old)
    if old is None:
        raise MooError(f"{args.old!r} is not in the registry")
    new = validate_key(args.new)
    occupied = refs.registry_key(new)
    if occupied is not None and occupied != old:
        raise MooError(f"{new} is already {refs.registry[occupied]}")
    obj = refs.registry[old]
    nonce = refs.generation_for(old) or ""
    index = list(refs.registry).index(old) + 1

    changes = []
    snapshots: dict[Path, tuple[bytes, tuple[int, int, int, int]]] = {}
    try:
        files = w.load_files(snapshots)
        object_files = frozenset(snapshots)
        source_key = next((key for key in files if key.lower() == old.lower()), None)
        collision = next(
            (key for key in files if key.lower() == new.lower() and key != source_key),
            None,
        )
        if collision is not None:
            raise MooError(
                f"cannot rename to {new!r}: existing file key {collision!r} differs only in case"
            )
        for key, local in files.items():
            source = w.file_for(key)
            refs_changed = _rewrite_object_refs(local, old, new)
            destination = source
            if key == source_key:
                local.key = new
                destination = w.file_for(new)
            if not refs_changed and key != source_key:
                continue
            same_path = source == destination
            if destination.exists() and not same_path:
                try:
                    same_path = source.samefile(destination)
                except OSError:
                    same_path = False
                if not same_path:
                    raise MooError(
                        f"cannot rename {source.name}: {destination.name} already exists"
                    )
            original, identity = snapshots[source]
            changes.append(
                (source, destination, same_path, objdef.render(local), original, identity)
            )
    except (OSError, ValueError) as e:
        raise MooError(f"cannot prepare rename {old!r} to {new!r}: {e}") from None

    updated_pairs = [(new if key == old else key, value) for key, value in refs.registry.items()]
    updated_generations = {
        new if key == old else key: refs.generations.get(key)
        for key in refs.registry
    }
    expected_registry = Registry(
        updated_pairs, updated_generations, refs.registry_revision + 1
    )
    prepared = []
    temporary = []
    state_temp = _temp_for(w.state_path, "new")
    state_backup = _temp_for(w.state_path, "old") if w.state_path.exists() else None
    try:
        for source, destination, same_path, rendered, original, identity in changes:
            new_temp = _temp_for(destination, "new")
            backup = _temp_for(source, "old")
            temporary.extend((new_temp, backup))
            _write_fsynced(new_temp, rendered)
            _write_fsynced(backup, original)
            prepared.append(
                (source, destination, same_path, new_temp, backup, original, identity)
            )
        write_state(state_temp, refs.player, expected_registry, w.toolbox)
        with state_temp.open("rb") as stream:
            os.fsync(stream.fileno())
        temporary.append(state_temp)
        if state_backup is not None:
            _write_fsynced(state_backup, w.state_path.read_bytes())
            temporary.append(state_backup)
        _fsync_dir(w.objects_dir)
        _fsync_dir(w.state_path.parent)
    except OSError as e:
        _cleanup(temporary + [state_temp] + ([state_backup] if state_backup else []))
        raise MooError(f"cannot prepare rename {old!r} to {new!r}: {e}") from None

    recovery_commands = ["tmoo rename-key --recover"]
    record = {
        "phase": "intent",
        "old_key": old,
        "new_key": new,
        "object": str(obj),
        "generation": nonce,
        "registry_index": index,
        "registry_revision": refs.registry_revision,
        "preserved_temporary_files": [str(path) for path in temporary if path.exists()],
        "recovery_commands": recovery_commands,
    }
    try:
        _write_rename_journal(journal, record)
        w.require_helper_version()
        _rename_result(w, index, refs.registry_revision, obj, nonce, new)
        remote_error = None
    except Exception as e:
        remote_error = e

    try:
        updated = w.read_registry()
    except Exception as e:
        record.update(
            phase="remote-outcome-unknown",
            remote_error=str(remote_error) if remote_error is not None else None,
            reconciliation_error=str(e),
        )
        _write_rename_journal(journal, record)
        raise MooError(
            f"rename outcome is unknown ({remote_error or e}); prepared files and recovery journal preserved at {journal}"
        ) from None

    actual = _matching_registry_key(updated, new, obj, nonce)
    still_old = _matching_registry_key(updated, old, obj, nonce)
    if actual is None:
        if still_old is not None:
            _cleanup(temporary + [journal])
            raise MooError(f"rename failed without changing the registry: {remote_error}") from None
        record.update(
            phase="remote-outcome-conflicted",
            remote_error=str(remote_error) if remote_error is not None else None,
            registry_keys=list(updated),
        )
        _write_rename_journal(journal, record)
        raise MooError(
            f"rename could not be reconciled; prepared files and recovery journal preserved at {journal}; replan"
        ) from None

    finalization_error = None
    finalized = []
    try:
        _assert_object_snapshots(w, snapshots, object_files)
        for source, destination, same_path, new_temp, _, original, identity in prepared:
            _assert_file_snapshot(source, original, identity)
            if not same_path and destination.exists():
                raise _LocalEditError(f"{destination.name} appeared after the rename was prepared")
            new_temp.replace(destination)
            if not same_path:
                source.unlink()
            _fsync_dir(destination.parent)
            finalized.append((source, destination, same_path))
        state_temp.replace(w.state_path)
        _fsync_dir(w.state_path.parent)
    except (OSError, MooError) as e:
        finalization_error = e

    if finalization_error is None:
        refs.replace_registry(updated)
        _cleanup(temporary + [journal])
        print(f"renamed {old} to {actual} ({obj})")
        return

    reverse_error = None
    try:
        _rename_result(w, index, updated.revision, obj, nonce, old)
        restored = w.read_registry()
        restored_key = _matching_registry_key(restored, old, obj, nonce)
        if restored_key is None:
            raise MooError(f"reverse rename returned success, but {old!r} is absent")
    except Exception as e:
        reverse_error = e

    rollback_errors = []
    preserve_edit = isinstance(finalization_error, _LocalEditError)
    if not preserve_edit:
        for source, destination, same_path, _, backup, _, _ in reversed(prepared):
            try:
                backup.replace(source)
                if not same_path:
                    destination.unlink(missing_ok=True)
                _fsync_dir(source.parent)
            except OSError as e:
                rollback_errors.append(f"{source.name}: {e}")
        try:
            if state_backup is None:
                w.state_path.unlink(missing_ok=True)
            else:
                state_backup.replace(w.state_path)
            _fsync_dir(w.state_path.parent)
        except OSError as e:
            rollback_errors.append(f"{w.state_path.name}: {e}")

    if preserve_edit:
        record.update(
            phase="local-edit-detected",
            local_error=str(finalization_error),
            remote_reversed=reverse_error is None,
            reverse_error=str(reverse_error) if reverse_error is not None else None,
            finalized_files=[str(destination) for _, destination, _ in finalized],
        )
        _write_rename_journal(journal, record)
        raise MooError(
            f"{finalization_error}; local files were not overwritten; recovery journal: {journal}"
        ) from None

    if reverse_error is None and not rollback_errors:
        _cleanup(temporary + [journal])
        raise MooError(
            f"local rename failed ({finalization_error}); the remote rename was reversed"
        ) from None

    commands = ["tmoo rename-key --recover"]
    record.update({
        "phase": "recovery-incomplete",
        "local_error": str(finalization_error),
        "reverse_error": str(reverse_error) if reverse_error is not None else None,
        "rollback_errors": rollback_errors,
        "finalized_files": [str(destination) for _, destination, _ in finalized],
        "preserved_temporary_files": [str(path) for path in temporary if path.exists()],
        "recovery_commands": commands,
    })
    try:
        _write_rename_journal(journal, record)
    except OSError as e:
        raise MooError(
            f"local rename failed ({finalization_error}); recovery was incomplete; "
            f"could not write recovery journal: {e}"
        ) from None
    raise MooError(
        f"local rename failed ({finalization_error}); recovery was incomplete; "
        f"recovery journal: {journal}; run: {'; then '.join(commands)}"
    ) from None


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
    with _world_write_lock(w):
        _apply_locked(w, args)


def _apply_locked(w: World, args) -> None:
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
    rk.add_argument("old", nargs="?", help="existing registry key, including a legacy key")
    rk.add_argument("new", nargs="?", help="new ASCII identifier")
    rk.add_argument("--recover", action="store_true", help="finish the recorded interrupted rename")
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
