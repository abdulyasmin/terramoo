"""The telnet transport against a scripted fake MOO: framing, errors, noise."""

import re
import socket
import threading
from types import SimpleNamespace

import pytest

from terramoo.errors import MooError
from terramoo.moolit import Obj
from terramoo.transport.telnet import IAC, WILL, Telnet, _Wire

TAG = re.compile(r"(~[A-Za-z0-9]{10}~)")


class FakeMoo:
    """Answers each `;;` request with whatever `reply(tag, line)` returns."""

    def __init__(self, reply, banner=b"Welcome!\r\n"):
        self.client, self.srv = socket.socketpair()
        self.reply, self.banner = reply, banner
        self.received: list[str] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn = self.srv
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
                    try:
                        conn.sendall("".join(x + "\r\n" for x in out).encode())
                    except OSError:
                        return


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
    transport = Telnet("local", 0, "alice", "pw", timeout=3, connect_timeout=3, **kw)

    def open_fake():
        transport._wire = _Wire(fake.client)
        transport._drain(1.0)
        transport._wire.send_line(transport.login_template.format(player=transport.player, password=transport.password))
        try:
            transport._request("player", transport.connect_timeout, login=True)
        except MooError as error:
            transport.close()
            raise MooError(
                f"could not log in to {transport.host}:{transport.port} as {transport.player}: {error}"
            ) from None

    transport._open = open_fake
    return transport


def test_value_is_reassembled_from_chunks_amid_noise():
    fake = FakeMoo(answer('{#12, "a \\"b\\"", {1, 2.5}}', noise=["Bob says, \"hi\""]))
    t = client(fake)
    assert t.eval("anything") == [Obj(12), 'a "b"', [1, 2.5]]
    assert fake.received[0] == "connect alice pw"
    assert "Bob says, \"hi\"" in t.noise


@pytest.mark.parametrize("expression, error_reply, expected", [
    ("secret", lambda tag: [tag + "S", tag + 'X{E_PERM, "Permission denied"}'], "E_PERM: Permission denied"),
    ("bad(", lambda tag: ["Line 1:  syntax error", "1 error."], "did not compile:\n  Line 1:  syntax error"),
])
def test_errors_become_moo_errors(expression, error_reply, expected):
    """A raised error is reported as such; a compile error quotes what the core printed."""
    def reply(tag, line):
        if tag is None:
            return []
        if is_sentinel(tag, line):
            return [tag + "Z"]
        if "_r = (player)" in line:
            return [tag + "S", tag + "B2", tag + "D#1", tag + "E"]
        return error_reply(tag)
    t = client(FakeMoo(reply))
    with pytest.raises(MooError, match=expected):
        t.eval(expression)


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


def test_declared_answer_length_must_match_the_received_chunks():
    def reply(tag, line):
        if tag is None:
            return []
        if is_sentinel(tag, line):
            return [tag + "Z"]
        if "_r = (player)" in line:
            return [tag + "S", tag + "B2", tag + "D#1", tag + "E"]
        return [tag + "S", tag + "B2", tag + "D1", tag + "E"]

    t = client(FakeMoo(reply))
    with pytest.raises(MooError, match="declared 2 characters but received 1"):
        t.eval("anything")


@pytest.mark.parametrize("expression", ["1\n2", "1\r2"])
def test_multiline_expressions_are_rejected_before_they_reach_the_wire(expression):
    t = Telnet("local", 0, "alice", "pw")
    t._wire = SimpleNamespace(send_line=lambda line: pytest.fail("sent malformed expression"))

    with pytest.raises(MooError, match="newline"):
        t._request(expression, 1)


@pytest.mark.parametrize("declared", [3, 4])
def test_declared_length_may_count_characters_or_utf8_bytes(declared):
    # mooR counts '"é"' as 3 characters; LambdaMOO and ToastStunt as 4 bytes.
    def reply(tag, line):
        if tag is None:
            return []
        if is_sentinel(tag, line):
            return [tag + "Z"]
        if "_r = (player)" in line:
            return [tag + "S", tag + "B2", tag + "D#1", tag + "E"]
        return [tag + "S", tag + f"B{declared}", tag + 'D"é"', tag + "E"]

    t = client(FakeMoo(reply))
    assert t.eval("anything") == "é"
