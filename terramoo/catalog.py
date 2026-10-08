"""Filesystem discovery shared by object readers, writers, and recovery.

Discovery does not parse definitions: pull can repair malformed files and
adopt --verify must be able to preserve arbitrary existing bytes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import objdef
from .errors import MooError


def safe_path(root: Path, path: Path) -> Path:
    """Check lexical containment and every existing component without following links."""
    root = root.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise MooError(f"refusing {path}: path is outside {root}") from None
    if ".." in relative.parts:
        raise MooError(f"refusing {path}: parent traversal is not allowed")
    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current /= part
        if current.is_symlink():
            raise MooError(f"refusing symlink {current}")
    return path


@dataclass(frozen=True)
class Discovery:
    objects: tuple[Path, ...]
    manifests: tuple[Path, ...]

    def by_key(self) -> dict[str, Path]:
        found = {}
        for path in self.objects:
            try:
                objdef.validate_identifier(path.stem, "object file key")
            except ValueError as e:
                raise MooError(f"{path}: {e}") from None
            key = path.stem.lower()
            if key in found:
                raise MooError(f"duplicate keys (or keys that differ only in case): {found[key]} and {path}")
            found[key] = path
        return found


def discover(root: Path, *, missing_ok: bool = False) -> Discovery:
    objects, manifests = [], []
    safe_path(root, root)
    if not root.exists() and missing_ok:
        return Discovery((), ())

    def visit(directory):
        try:
            with os.scandir(directory) as stream:
                entries = sorted(stream, key=lambda e: e.name)
            for entry in entries:
                path = directory / entry.name
                relevant = entry.name.endswith(".moo") or entry.name == "module.toml"
                if entry.is_symlink():
                    # Directory links are rejected even when broken; otherwise
                    # a broken link could silently omit an entire desired tree.
                    raise MooError(f"refusing symlink {path}")
                if entry.is_dir(follow_symlinks=False):
                    visit(path)
                elif relevant:
                    if not entry.is_file(follow_symlinks=False):
                        raise MooError(f"not a regular file: {path}")
                    (manifests if entry.name == "module.toml" else objects).append(path)
        except OSError as e:
            raise MooError(f"cannot read objects directory {directory}: {e}") from None

    visit(root)
    return Discovery(tuple(objects), tuple(manifests))


def object_paths(root: Path) -> tuple[Path, ...]:
    return discover(root).objects


def read_snapshot(path: Path) -> tuple[bytes, tuple[int, int, int, int]]:
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        data = stream.read()
        after = os.fstat(stream.fileno())
    current = path.stat()
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if identity(before) != identity(after) or identity(before) != identity(current):
        raise MooError(f"{path} changed while it was being read")
    return data, identity(before)
