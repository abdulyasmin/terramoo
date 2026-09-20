"""A client for a hosted MCP server, enough for eval and set_verb.

The server is stateless Streamable HTTP: every request is one JSON-RPC
call with the player's Bearer token, and the answer comes back as an SSE
`data:` line or a plain JSON body.  Nothing here holds a session.

The token is looked up, in order, from `$TMOO_TOKEN`, the macOS Keychain
(service "terramoo", account = the world name), or `~/.config/terramoo/<world>.token`.
`tmoo token store <world>` writes whichever of the last two applies.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

_TAGGED = re.compile(r"(.*\S)\s+\((?:OBJ|ERR|ANON|WAIF)\)", re.S)
KEYCHAIN_SERVICE = "terramoo"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "terramoo"


class MooError(RuntimeError):
    """The MOO refused: an E_* error, a compile error, or a gateway failure."""


def _keychain_read(world: str) -> str | None:
    if sys.platform != "darwin":
        return None
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", world, "-w"],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _keychain_write(world: str, token: str) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", world, "-w", token],
        check=True,
        capture_output=True,
    )


def token_for(world: str) -> str:
    env = os.environ.get("TMOO_TOKEN")
    if env:
        return env
    kc = _keychain_read(world)
    if kc:
        return kc
    f = CONFIG_DIR / f"{world}.token"
    if f.exists():
        return f.read_text().strip()
    raise MooError(f"no MCP token for {world}: run `tmoo token store {world}` or set TMOO_TOKEN")


def store_token(world: str, token: str) -> str:
    if sys.platform == "darwin":
        _keychain_write(world, token)
        return f"Keychain ({KEYCHAIN_SERVICE}/{world})"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    f = CONFIG_DIR / f"{world}.token"
    f.write_text(token + "\n")
    f.chmod(0o600)
    return str(f)


class Client:
    def __init__(self, url: str, token: str, timeout: float = 90.0):
        self.url = url
        self.token = token
        self.timeout = timeout
        self._id = 0

    def rpc(self, method: str, params: dict) -> dict:
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}).encode()
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ctype = resp.headers.get("Content-Type", "")
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:
            raise MooError(f"HTTP {e.code} from {self.url}: {e.read().decode()[:300]}") from None
        except urllib.error.URLError as e:
            raise MooError(f"cannot reach {self.url}: {e.reason}") from None
        if "text/event-stream" in ctype:
            payloads = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
            if not payloads:
                raise MooError(f"empty event stream from {self.url}")
            msg = json.loads(payloads[-1])
        else:
            msg = json.loads(raw)
        if "error" in msg:
            raise MooError(f"{method}: {msg['error'].get('message', msg['error'])}")
        return msg["result"]

    def call_tool(self, name: str, arguments: dict) -> str:
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            raise MooError(text.strip() or f"{name} failed")
        return text

    def eval(self, expression: str):
        """Evaluate one MOO expression; the value comes back as the gate's
        native JSON (objects and errors as "#123"/"E_PERM" strings)."""
        text = self.call_tool("eval", {"expression": expression})
        # A non-JSON-native scalar is tagged: `"#365"  (OBJ)`, `"E_PERM"  (ERR)`.
        m = _TAGGED.fullmatch(text.strip())
        if m:
            text = m.group(1)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise MooError(f"eval returned something that is not JSON: {text[:200]}") from None

    def set_verb(self, obj: str, verb: str, code: str, *, create: bool = False, permissions: str | None = None,
                 dobj: str | None = None, prep: str | None = None, iobj: str | None = None) -> str:
        args = {"object": obj, "verb": verb, "code": code}
        if create:
            args["create"] = True
        for k, v in (("permissions", permissions), ("dobj", dobj), ("prep", prep), ("iobj", iobj)):
            if v is not None:
                args[k] = v
        return self.call_tool("set_verb", args)
