"""Where a world's secret lives: a login password or an MCP token.

Looked up, in order, from the environment (`$TMOO_SECRET`, or the older
`$TMOO_TOKEN`), the macOS Keychain (service "terramoo", account = the
world name; "terramoo" is still read for worlds stored before the rename),
or `~/.config/terramoo/<world>.secret`.  `tmoo secret store <world>`
writes whichever of the last two applies.  Never a file in a world repo.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .errors import MooError

KEYCHAIN_SERVICE = "terramoo"
LEGACY_SERVICES = ("terramoo",)
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "terramoo"
ENV_VARS = ("TMOO_SECRET", "TMOO_TOKEN")


def _keychain_read(service: str, world: str) -> str | None:
    if sys.platform != "darwin":
        return None
    r = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", world, "-w"],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def secret_for(world: str) -> str:
    for var in ENV_VARS:
        if os.environ.get(var):
            return os.environ[var]
    for service in (KEYCHAIN_SERVICE, *LEGACY_SERVICES):
        found = _keychain_read(service, world)
        if found:
            return found
    f = CONFIG_DIR / f"{world}.secret"
    if f.exists():
        return f.read_text().strip()
    raise MooError(f"no secret for world {world}: run `tmoo secret store {world}` or set TMOO_SECRET")


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
