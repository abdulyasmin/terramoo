"""Durable local replacement transactions and small configuration serializers."""

from __future__ import annotations

import base64
from datetime import date, datetime, time
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from .catalog import safe_path
from .errors import MooError


def digest(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def json_text(data) -> str:
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def read_json(path: Path, *, missing=None):
    if not path.exists():
        return missing
    safe_path(path.parent, path)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise MooError(f"cannot read {path}: {e}") from None


def fsync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def canonical_name(path: Path):
    """Make the directory entry's spelling match a case-only destination."""
    siblings = list(path.parent.iterdir())
    if any(p.name == path.name for p in siblings):
        return
    for sibling in siblings:
        if sibling.name.casefold() == path.name.casefold() and sibling.samefile(path):
            sibling.rename(path)
            fsync_dir(path.parent)
            return


def case_alias(source: Path, destination: Path):
    if source == destination or source.name.casefold() != destination.name.casefold():
        return False
    if not source.parent.samefile(destination.parent) or not source.samefile(destination):
        return False
    names = {p.name for p in source.parent.iterdir()}
    return not {source.name, destination.name} <= names


def atomic_write(path: Path, data: bytes | str):
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing):
        fsync_dir(directory.parent)
    safe_path(path.parent, path)
    temporary = path.with_name(f".{path.name}.tmoo-{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data.encode() if isinstance(data, str) else data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        canonical_name(path)
        fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def toml_text(data: dict) -> str:
    """Serialize TOML data while retaining every unrelated configuration value."""
    def value(v):
        if isinstance(v, str):
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v).lower()
        if isinstance(v, (datetime, date, time)):
            return v.isoformat()
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        if isinstance(v, dict):
            return "{ " + ", ".join(f"{value(k)} = {value(x)}" for k, x in v.items()) + " }"
        raise MooError(f"unsupported TOML configuration value: {type(v).__name__}")

    lines = []

    def table(mapping, path):
        if path:
            lines.extend(["", "[" + ".".join(value(k) for k in path) + "]"])
        for key, item in mapping.items():
            if not isinstance(item, dict):
                lines.append(f"{value(key)} = {value(item)}")
        for key, item in mapping.items():
            if isinstance(item, dict):
                table(item, [*path, key])

    table(data, [])
    return "\n".join(lines).lstrip("\n") + "\n"


def journal_path(world_dir: Path) -> Path:
    return world_dir / ".packages" / "transaction.json"


def _target(world_dir: Path, relative: str) -> Path:
    if not isinstance(relative, str):
        raise MooError("invalid transaction path")
    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise MooError(f"unsafe transaction path: {relative}")
    if path.parts[0] not in {"objects", ".packages", "world.toml", "packages.lock.json", "state.json"}:
        raise MooError(f"unexpected transaction path: {relative}")
    if path == Path(".packages/transaction.json"):
        raise MooError("transaction cannot replace its journal")
    return safe_path(world_dir, world_dir / path)


def _decode(value):
    if value is None:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (TypeError, ValueError):
        raise MooError("invalid transaction content") from None


def recover(world_dir: Path):
    journal = journal_path(world_dir)
    record = read_json(journal)
    if not isinstance(record, dict) or record.get("schema_version") != 1 or not isinstance(record.get("changes"), list):
        raise MooError(f"invalid or unsupported transaction journal: {journal}")
    changes = []
    seen = set()
    for entry in record["changes"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "before", "after"}:
            raise MooError("invalid transaction change")
        target = _target(world_dir, entry["path"])
        if target in seen:
            raise MooError(f"duplicate transaction target: {target}")
        seen.add(target)
        before, after = _decode(entry["before"]), _decode(entry["after"])
        current = target.read_bytes() if target.exists() else None
        if current not in (before, after):
            raise MooError(f"{target} changed during transaction; preserve the edit and reconcile before package recover")
        changes.append((target, before, after))
    # Preflight every target before replacing any of them.
    for target, before, after in changes:
        safe_path(world_dir, target)
        current = target.read_bytes() if target.exists() else None
        if current == after:
            if after is not None:
                canonical_name(target)
            continue
        if current != before:
            raise MooError(f"{target} changed during transaction; recovery journal retained")
        if after is None:
            target.unlink()
            fsync_dir(target.parent)
        else:
            atomic_write(target, after)
    journal.unlink()
    fsync_dir(journal.parent)


def transaction(world_dir: Path, changes: dict[Path, bytes | str | None], *, expected: dict[Path, bytes | None] | None = None):
    journal = journal_path(world_dir)
    if journal.exists():
        raise MooError("unfinished local transaction; run tmoo package recover")
    safe_path(world_dir, journal)
    if expected:
        for path, content in expected.items():
            safe_path(world_dir, path)
            current = path.read_bytes() if path.exists() else None
            if current != content:
                raise MooError(f"{path} changed while preparing the transaction")
    entries = []
    encode = lambda data: base64.b64encode(data).decode() if data is not None else None
    replacements = [path for path, value in changes.items() if value is not None and path.exists()]
    for path, after in sorted(changes.items(), key=lambda pair: str(pair[0])):
        relative = str(path.relative_to(world_dir))
        path = _target(world_dir, relative)
        if after is None and path.exists() and any(case_alias(path, other) for other in replacements):
            # A case-only rename can name the same directory entry twice.
            # Replacing its destination performs both changes; deleting the
            # source afterward would delete the newly written file as well.
            continue
        before = path.read_bytes() if path.exists() else None
        if isinstance(after, str):
            after = after.encode()
        if before != after:
            entries.append({"path": relative, "before": encode(before), "after": encode(after)})
    if not entries:
        return
    atomic_write(journal, json_text({"schema_version": 1, "id": uuid4().hex, "changes": entries}))
    recover(world_dir)
