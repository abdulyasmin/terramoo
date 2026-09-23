"""Where a world's secret lives: a login password or an MCP token.

Looked up, in order, from `$TMOO_SECRET`, the macOS Keychain (service
"terramoo", account = the world name), or `~/.config/terramoo/<world>.secret`.
`tmoo secret store <world>` writes whichever of the last two applies.
Never a file in a world repo.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .errors import MooError

KEYCHAIN_SERVICE = "terramoo"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "terramoo"
ENV_VAR = "TMOO_SECRET"


def _keychain_read(world: str) -> str | None:
    if sys.platform != "darwin":
        return None
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", world, "-w"],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def secret_for(world: str) -> str:
    if os.environ.get(ENV_VAR):
        return os.environ[ENV_VAR]
    found = _keychain_read(world)
    if found:
        return found
    f = CONFIG_DIR / f"{world}.secret"
    if f.exists():
        return f.read_text().strip()
    raise MooError(f"no secret for world {world}: run `tmoo secret store {world}` or set TMOO_SECRET")


def check_secret(secret: str) -> str:
    """The secret as typed, stripped, or a MooError when it can't be one.

    `tmoo secret store` reads a single line, so pasting a token's JSON
    wrapper stores only its first line (`{"token": `). Refuse anything
    that starts like JSON or a quoted string rather than store it.
    """
    secret = secret.strip()
    if not secret:
        raise MooError("empty secret: nothing stored")
    if secret[0] in "{[\"'":
        raise MooError(f"that starts with {secret[0]!r}, like pasted JSON or quotes: paste only the token itself")
    return secret


def store_secret(world: str, secret: str) -> str:
    if sys.platform == "darwin":
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", world, "-w", secret],
            check=True,
            capture_output=True,
        )
        return f"Keychain ({KEYCHAIN_SERVICE}/{world})"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    f = CONFIG_DIR / f"{world}.secret"
    f.write_text(secret + "\n")
    f.chmod(0o600)
    return str(f)
