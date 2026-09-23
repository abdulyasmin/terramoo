"""A hosted MCP server with an `eval` tool.

The server is stateless Streamable HTTP: every request is one JSON-RPC
call with the player's Bearer token, and the answer comes back as an SSE
`data:` line or a plain JSON body.  Nothing here holds a session.

Every expression is sent wrapped in `toliteral()`, so the answer is one
string of MOO literal text whatever the gate does to native values, and
it parses the same way the telnet transport's does.  Tool names are
configurable in `[connection]` (`eval_tool`, `set_verb_tool`,
`set_prop_tool`); set the latter two to "" on a server without them and
the builtins are used instead.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from .. import moolit
from ..errors import MooError
from . import Transport

_TAGGED = re.compile(r"(.*\S)\s+\((?:OBJ|ERR|ANON|WAIF|STR)\)", re.S)


class McpTransport(Transport):
    def __init__(self, url: str, token: str, *, eval_tool: str = "eval", set_verb_tool: str | None = "set_verb",
                 set_prop_tool: str | None = "set_prop", timeout: float = 90.0, batch_bytes: int = 24_000):
        self.url = url
        self.token = token
        self.eval_tool = eval_tool
        self.set_verb_tool = set_verb_tool or None  # "" in world.toml: use the builtins
        self.set_prop_tool = set_prop_tool or None
        self.timeout = timeout
        self.batch_bytes = batch_bytes
        self._id = 0

    @classmethod
    def from_config(cls, conn: dict, secret: str) -> "McpTransport":
        if "url" not in conn:
            raise MooError("an mcp connection needs `url`")
        keys = ("eval_tool", "set_verb_tool", "set_prop_tool", "timeout", "batch_bytes")
        return cls(conn["url"], secret, **{k: conn[k] for k in keys if k in conn})

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
        text = self.call_tool(self.eval_tool, {"expression": f"toliteral({expression})"}).strip()
        m = _TAGGED.fullmatch(text)
        if m:
            text = m.group(1)
        try:
            literal = json.loads(text)
        except json.JSONDecodeError:
            literal = text  # a gate that answers with the bare string
        if not isinstance(literal, str):
            raise MooError(f"eval returned something that is not a toliteral() string: {text[:200]}")
        try:
            return moolit.parse(literal)
        except moolit.LiteralError as e:
            raise MooError(f"cannot read the MOO's answer ({e}): {literal[:200]}") from None

    def set_prop(self, obj, name: str, value) -> None:
        if not self.set_prop_tool:
            return super().set_prop(obj, name, value)
        self.call_tool(self.set_prop_tool, {"object": str(obj), "prop": name, "value": moolit.serialize(value)})

    def install_verb(self, obj, name, lines, *, perms="rxd", args=("this", "none", "this")) -> str:
        if not self.set_verb_tool:
            return super().install_verb(obj, name, lines, perms=perms, args=args)
        exists = self.eval(f"{moolit.escape(name)} in verbs({obj})")
        dobj, prep, iobj = args
        call = {"object": str(obj), "verb": name, "code": "\n".join(lines),
                "permissions": perms, "dobj": dobj, "prep": prep, "iobj": iobj}
        if not exists:
            call["create"] = True
        r = self.call_tool(self.set_verb_tool, call)
        return f"{'updated' if exists else 'installed'}: {r.strip()}"
