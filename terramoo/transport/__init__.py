"""How `tmoo` reaches a MOO: one interface, several wires.

A transport evaluates MOO expressions as the player and hands back the
value as Python (`moolit` types: int, float, str, `Obj`, `Err`, list,
`Map`).  An error raised in the MOO comes back as `MooError`.  Everything
else `tmoo` does is built from `eval`, so a transport only has to get an
expression there and its `toliteral()` back.

    telnet   log in as the player on the game port (plain or TLS) and use
             the core's `;` eval; works on any MOO where the player is a
             programmer (LambdaMOO, ToastStunt, mooR, ...)
    mcp      a hosted MCP server exposing an `eval` tool

Expressions sent through `eval` stay single expressions with no
statements, backquotes or assignments, so they run on either wire; the
work with loops lives in the helper verbs (`terramoo/helper/`).
"""

from __future__ import annotations

from .. import moolit
from ..errors import MooError


class Transport:
    #: Whether a helper verb called through this transport may `suspend()`.
    #: Telnet evals are ordinary tasks; a hosted gate usually forbids it.
    can_suspend = False
    #: Characters of expression text per call (a batch of apply ops).
    batch_bytes = 24_000

    def eval(self, expression: str):
        raise NotImplementedError

    def close(self) -> None:
        pass

    def set_prop(self, obj, name: str, value) -> None:
        """Set a property; an assignment, which is still an expression."""
        self.eval(f"{obj}.({moolit.escape(name)}) = {moolit.serialize(value)}")

    def install_verb(self, obj, name: str, lines: list[str], *, perms: str = "rxd",
                     args: tuple[str, str, str] = ("this", "none", "this")) -> str:
        """Create or replace a verb from source lines; raise on compile errors."""
        exists = self.eval(f"{moolit.escape(name)} in verbs({obj})")
        if not exists:
            self.eval(f"add_verb({obj}, {{player, {moolit.escape(perms)}, {moolit.escape(name)}}}, "
                      f"{moolit.serialize(list(args))})")
        else:
            self.eval(f"set_verb_info({obj}, {moolit.escape(name)}, {{player, {moolit.escape(perms)}, {moolit.escape(name)}}})")
            self.eval(f"set_verb_args({obj}, {moolit.escape(name)}, {moolit.serialize(list(args))})")
        errors = self.eval(f"set_verb_code({obj}, {moolit.escape(name)}, {moolit.serialize(lines)})")
        if errors:
            raise MooError(f"{obj}:{name} did not compile: " + " / ".join(map(str, errors)))
        return "updated" if exists else "installed"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def connect(conn: dict, player: str, secret: str) -> Transport:
    """Open the transport a world's `[connection]` table describes."""
    kind = conn.get("transport", "telnet")
    if kind == "telnet":
        from .telnet import Telnet

        return Telnet.from_config(conn, player, secret)
    if kind == "mcp":
        from .mcp import McpTransport

        return McpTransport.from_config(conn, secret)
    raise MooError(f"unknown transport {kind!r} (telnet or mcp)")
