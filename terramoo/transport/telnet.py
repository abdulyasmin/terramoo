"""Logging in on the game port (plain or TLS) and asking with `;;` eval.

A connection is one stream of lines: other players, the core's `=> ...`
echo and late output from suspended tasks all arrive in it.  So the code
sent tags its own output with a tag made up for that request, and every
other line is ignored:

    ~tag~S              the code started (it compiled)
    ~tag~B<length>      the value follows, as toliteral() text of that length
    ~tag~C<unit>        how length() counts UTF-8: 1 character or 2 bytes for é
    ~tag~D<chunk>       ... in chunks, so no line is long enough to be cut
    ~tag~E              the value is complete
    ~tag~X{E_X, "msg"}  the MOO raised this instead
    ~tag~Z              a sentinel sent right after the request

The sentinel is a second command.  Commands on one connection run in
order, and the request says `S` before it does anything else, so a `Z`
that arrives with no `S` before it means the request never ran: a
compile error, which the core printed untagged, and which is what the
error then quotes.  Output is sent with `notify(player, ...)` rather than
`player:tell`, so no core's paging or line wrapping can touch it (`tell`
in `[connection]` changes that).  `;;` is the LambdaCore family's
"statements, not an expression" eval; `eval_prefix` changes it for a core
that spells it otherwise.
"""

from __future__ import annotations

import collections
import random
import re
import socket
import ssl
import string
import time

from .. import moolit
from ..errors import MooError
from . import Transport

_TAG = re.compile(r"~[A-Za-z0-9]{10}~[SBCDEXZ]")
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
LOGIN_FAILED = (
    "either that player does not exist",
    "invalid password",
    "incorrect password",
    "no such player",
    "login failed",
    "that player does not exist",
)


def _readable(line: str) -> str:
    """`line` with the undecodable bytes the wire kept shown as U+FFFD."""
    return line.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


class _Wire:
    """A socket that yields text lines, with telnet negotiation stripped
    (and politely refused)."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.raw = bytearray()
        self.text = bytearray()
        self.lines: collections.deque[str] = collections.deque()

    def closed(self) -> bool:
        """Whether the MOO has hung up (a second login as the same player
        boots this one, on LambdaCore and its descendants).  Whatever it
        said first is read into `lines` on the way."""
        try:
            self.sock.setblocking(False)
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    return True
                self._feed(chunk)
        except (BlockingIOError, ssl.SSLWantReadError):
            return False
        except OSError:
            return True
        finally:
            try:
                self.sock.setblocking(True)
            except OSError:
                pass

    def send_line(self, text: str) -> None:
        data = text.encode("utf-8").replace(bytes([IAC]), bytes([IAC, IAC]))
        self.sock.sendall(data + b"\r\n")

    def read_line(self, deadline: float) -> str | None:
        """The next line, or None once `deadline` (time.monotonic) passes."""
        while not self.lines:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                return None
            except ssl.SSLWantReadError:
                continue
            if not chunk:
                raise MooError("the MOO closed the connection")
            self._feed(chunk)
        return self.lines.popleft()

    def _feed(self, chunk: bytes) -> None:
        self.raw.extend(chunk)
        self.text.extend(self._strip_iac())
        while (nl := self.text.find(b"\n")) >= 0:
            line = bytes(self.text[:nl])
            del self.text[:nl + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            line = line.replace(b"\r\0", b"\r")  # NVT: CR NUL is a literal CR
            # A byte-counting MOO can split a UTF-8 character between D lines.
            # Surrogate escapes preserve those bytes until _request rejoins them.
            self.lines.append(line.decode("utf-8", errors="surrogateescape"))

    def _strip_iac(self) -> bytearray:
        """Plain bytes out of `self.raw`; a telnet command cut off at the
        end of the chunk stays there for the next one."""
        out = bytearray()
        b = self.raw
        i = 0
        while i < len(b):
            if b[i] != IAC:
                out.append(b[i])
                i += 1
                continue
            if i + 1 >= len(b):
                break
            cmd = b[i + 1]
            if cmd == IAC:
                out.append(IAC)
                i += 2
            elif cmd in (DO, DONT, WILL, WONT):
                if i + 2 >= len(b):
                    break
                if cmd == DO:
                    self.sock.sendall(bytes([IAC, WONT, b[i + 2]]))
                elif cmd == WILL:
                    self.sock.sendall(bytes([IAC, DONT, b[i + 2]]))
                i += 3
            elif cmd == SB:
                end = b.find(bytes([IAC, SE]), i + 2)
                if end < 0:
                    break
                i = end + 2
            else:
                i += 2
        del b[:i]
        return out


class Telnet(Transport):
    can_suspend = True

    def __init__(self, host: str, port: int, player: str, password: str, *, tls: bool = False,
                 verify: bool = True, login: str = "connect {player} {password}", eval_prefix: str = ";;",
                 tell: str = "notify(player, {})", chunk: int = 900, timeout: float = 120.0,
                 connect_timeout: float = 20.0, batch_bytes: int = 16_000):
        self.host, self.port, self.tls, self.verify = host, port, tls, verify
        self.player, self.password = player, password
        self.login_template, self.eval_prefix, self.tell_template = login, eval_prefix, tell
        self.chunk, self.timeout, self.connect_timeout = chunk, timeout, connect_timeout
        self.batch_bytes = batch_bytes
        self.noise: collections.deque[str] = collections.deque(maxlen=40)
        self._wire: _Wire | None = None

    @classmethod
    def from_config(cls, conn: dict, player: str, secret: str) -> "Telnet":
        if "host" not in conn:
            raise MooError("a telnet connection needs `host` (and `port`)")
        keys = ("tls", "verify", "login", "eval_prefix", "tell", "chunk", "timeout", "connect_timeout", "batch_bytes")
        return cls(conn["host"], int(conn.get("port", 7777)), player, secret,
                   **{k: conn[k] for k in keys if k in conn})

    # ----- connection

    def _open(self) -> None:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except OSError as e:
            raise MooError(f"cannot reach {self.host}:{self.port}: {e}") from None
        if self.tls:
            ctx = ssl.create_default_context()
            if not self.verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            try:
                sock = ctx.wrap_socket(sock, server_hostname=self.host)
            except ssl.SSLError as e:
                raise MooError(f"TLS to {self.host}:{self.port} failed: {e}") from None
        self._wire = _Wire(sock)
        # Let the banner arrive, then log in.
        self._drain(1.0)
        self._wire.send_line(self.login_template.format(player=self.player, password=self.password))
        try:
            self._request("player", self.connect_timeout, login=True)
        except MooError as e:
            self.close()
            raise MooError(f"could not log in to {self.host}:{self.port} as {self.player}: {e}") from None

    def _drain(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while (line := self._wire.read_line(deadline)) is not None:
            self.noise.append(_readable(line))

    def close(self) -> None:
        if self._wire is not None:
            try:
                self._wire.sock.close()
            finally:
                self._wire = None

    # ----- asking

    def eval(self, expression: str):
        # Log in first if this is the first request, or again if the MOO hung
        # up in between.  Never mid-request: that could run an op twice.
        if self._wire is not None and self._wire.closed():
            self.close()
        if self._wire is None:
            self._open()
        return self._request(expression, self.timeout)

    def _tell(self, what: str) -> str:
        return self.tell_template.format(what)

    def program(self, tag: str, expression: str) -> str:
        """The one line of MOO that evaluates `expression` and reports it under `tag`."""
        n = self.chunk
        q = moolit.escape
        return (
            f"{self.eval_prefix}{self._tell(q(tag + 'S'))}; "
            f"try _r = ({expression}); _v = toliteral(_r); "
            f"{self._tell(q(tag + 'B') + ' + tostr(length(_v))')}; "
            f"{self._tell(q(tag + 'C') + ' + tostr(length(\"é\"))')}; "
            f"for _i in [0..(length(_v) - 1) / {n}] "
            f"{self._tell(q(tag + 'D') + f' + _v[_i * {n} + 1..min((_i + 1) * {n}, length(_v))]')}; "
            f"if (_i % 16 == 15) suspend(0); endif "
            f"endfor "
            f"{self._tell(q(tag + 'E'))}; "
            f"except _e (ANY) {self._tell(q(tag + 'X') + ' + toliteral({_e[1], _e[2]})')}; "
            f"endtry"
        )

    def _request(self, expression: str, timeout: float, *, login: bool = False):
        if "\n" in expression or "\r" in expression:
            # A command is one line; MOO string literals have no newline escape.
            raise MooError("a value with a newline in it cannot be sent over telnet")
        wire = self._wire
        tag = "~" + "".join(random.choices(string.ascii_letters + string.digits, k=10)) + "~"
        wire.send_line(self.program(tag, expression))
        wire.send_line(f"{self.eval_prefix}{self._tell(moolit.escape(tag + 'Z'))}")
        started = sentinel = False
        chunks: list[bytes] = []
        expected_length: int | None = None
        length_unit: int | None = None
        untagged: list[str] = []
        outcome = None
        deadline = time.monotonic() + timeout
        while True:
            line = wire.read_line(deadline)
            if line is None:
                if outcome is not None:
                    break  # the answer is in; only the sentinel went missing
                what = "log in" if login and not started else "answer"
                raise MooError(f"the MOO did not {what} within {timeout:.0f}s" + self._context(untagged))
            if not line.startswith(tag):
                line = _readable(line)
                self.noise.append(line)
                if not _TAG.match(line):  # a stale tag from an earlier request is not news
                    untagged.append(line)
                if login and any(s in line.lower() for s in LOGIN_FAILED):
                    raise MooError(line.strip())
                continue
            deadline = time.monotonic() + timeout  # still talking: keep waiting
            kind, rest = line[len(tag)], line[len(tag) + 1:]
            if kind == "S":
                started = True
            elif kind == "B":
                chunks = []
                length_unit = None
                try:
                    expected_length = int(rest)
                except ValueError:
                    expected_length = None
            elif kind == "C":
                try:
                    length_unit = int(rest)
                except ValueError:
                    length_unit = None
            elif kind == "D":
                chunks.append(rest.encode("utf-8", errors="surrogateescape"))
            elif kind == "E":
                data = b"".join(chunks)
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    outcome = ("P", "the MOO sent an answer that is not valid UTF-8")
                else:
                    if expected_length is None:
                        outcome = ("P", "the MOO sent an answer without a valid length")
                    else:
                        if length_unit == 2:
                            received, unit = len(data), "bytes"
                        elif length_unit == 1 or len(data) == len(text):
                            received, unit = len(text), "characters"
                        else:
                            received = None
                            outcome = ("P", "the MOO sent a Unicode answer without a valid length convention")
                        if received is not None:
                            if expected_length != received:
                                outcome = ("P", f"the MOO declared {expected_length} {unit} but received {received}")
                            else:
                                outcome = (kind, text)
                if sentinel:
                    break
                # Read on to this request's sentinel so it cannot confuse the next.
                deadline = time.monotonic() + min(timeout, 10)
            elif kind == "X":
                outcome = (kind, _readable(rest))
                if sentinel:
                    break
                # Read on to this request's sentinel so it cannot confuse the next.
                deadline = time.monotonic() + min(timeout, 10)
            elif kind == "Z":
                sentinel = True
                if outcome is not None:
                    break
                if not started:
                    raise MooError("the expression did not compile" + self._context(untagged))
        kind, text = outcome
        if kind == "P":
            raise MooError(text)
        if kind == "X":
            try:
                code, msg = moolit.parse(text)
            except ValueError:
                raise MooError(text) from None
            raise MooError(f"{code}: {msg}")
        try:
            return moolit.parse(text)
        except moolit.LiteralError as e:
            raise MooError(f"cannot read the MOO's answer ({e}): {text[:200]}") from None

    @staticmethod
    def _context(lines: list[str]) -> str:
        lines = [ln for ln in lines if ln.strip()][-8:]
        return (":\n  " + "\n  ".join(lines)) if lines else ""
