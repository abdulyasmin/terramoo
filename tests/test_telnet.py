"""The telnet transport against a scripted fake MOO: framing, errors, noise."""

import re
import socket
import threading

import pytest

from terramoo.errors import MooError
from terramoo.moolit import Obj
from terramoo.transport.telnet import IAC, WILL, Telnet, _Wire

TAG = re.compile(r"(~[A-Za-z0-9]{10}~)")


class FakeMoo:
    """Answers each `;;` request with whatever `reply(tag, line)` returns."""

    def __init__(self, reply, banner=b"Welcome!\r\n"):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.reply, self.banner = reply, banner
        self.received: list[str] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self.srv.accept()
        conn.sendall(self.banner)
        buf = b""
        while True:
            try:
                data = conn.recv(65536)
            except OSError:
                return
            if not data:
                return
            buf += data
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                text = line.decode()
                self.received.append(text)
                m = TAG.search(text)
                out = self.reply(m.group(1) if m else None, text)
                if out:
                    conn.sendall("".join(x + "\r\n" for x in out).encode())


def is_sentinel(tag, line):
    return line == f';;notify(player, "{tag}Z")'


def answer(value_literal, chunk=4, noise=()):
    def reply(tag, line):
        if tag is None:
            return ["*** Connected ***"]
        if is_sentinel(tag, line):
            return [tag + "Z"]
        parts = [value_literal[i:i + chunk] for i in range(0, len(value_literal), chunk)]
        return [tag + "S", *noise, tag + "B" + str(len(value_literal)), *[tag + "D" + p for p in parts], tag + "E", "=> 0"]
    return reply


def client(fake, **kw):
    return Telnet("127.0.0.1", fake.port, "alice", "pw", timeout=3, connect_timeout=3, **kw)


def test_value_is_reassembled_from_chunks_amid_noise():
    fake = FakeMoo(answer('{#12, "a \\"b\\"", {1, 2.5}}', noise=["Bob says, \"hi\""]))
    t = client(fake)
    assert t.eval("anything") == [Obj(12), 'a "b"', [1, 2.5]]
    assert fake.received[0] == "connect alice pw"
    assert "Bob says, \"hi\"" in t.noise


def test_raised_error_becomes_moo_error():
    def reply(tag, line):
        if tag is None:
            return []
        if is_sentinel(tag, line):
            return [tag + "Z"]
        if "_r = (player)" in line:
            return [tag + "S", tag + "B2", tag + "D#1", tag + "E"]
        return [tag + "S", tag + 'X{E_PERM, "Permission denied"}']
    t = client(FakeMoo(reply))
    with pytest.raises(MooError, match="E_PERM: Permission denied"):
        t.eval("secret")


def test_compile_error_quotes_what_the_core_printed():
    def reply(tag, line):
        if tag is None:
            return []
        if is_sentinel(tag, line):
            return [tag + "Z"]
        if "_r = (player)" in line:
            return [tag + "S", tag + "B2", tag + "D#1", tag + "E"]
        return ["Line 1:  syntax error", "1 error."]
    t = client(FakeMoo(reply))
    with pytest.raises(MooError, match="did not compile:\n  Line 1:  syntax error"):
        t.eval("bad(")


def test_login_failure_is_reported_at_once():
    def reply(tag, line):
        if tag is None:
            return ["Either that player does not exist, or has a different password."]
        return ["I don't understand that."]
    with pytest.raises(MooError, match="could not log in .*does not exist"):
        client(FakeMoo(reply)).eval("1")


def test_program_is_one_line_with_the_configured_prefix_and_tell():
    t = Telnet("h", 1, "p", "x", eval_prefix=";", tell="player:tell({})", chunk=10)
    line = t.program("~abcdefghij~", "1 + 1")
    assert "\n" not in line
    assert line.startswith(';player:tell("~abcdefghij~S"); try _r = (1 + 1);')
    assert "_v[_i * 10 + 1..min((_i + 1) * 10, length(_v))]" in line


def test_wire_strips_and_refuses_telnet_negotiation():
    a, b = socket.socketpair()
    wire = _Wire(a)
    # IAC WILL 70 split across two reads, an escaped IAC, CRLF endings.
    wire._feed(bytes([IAC]))
    wire._feed(bytes([WILL, 70]) + b"hello\r\nwor")
    wire._feed(b"ld " + bytes([IAC, IAC]) + b"\r\n")
    assert list(wire.lines) == ["hello", "world �"]
    assert b.recv(16) == bytes([IAC, 254, 70])  # DONT 70
