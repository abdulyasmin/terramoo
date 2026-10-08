"""The telnet transport against a scripted fake MOO: framing, errors, noise."""

import re
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from terramoo import export as export_mod, moolit
from terramoo.errors import MooError
from terramoo.moolit import Obj
from terramoo.refs import Refs
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
                        lines = [x.encode() if isinstance(x, str) else x for x in out]
                        conn.sendall(b"\r\n".join(lines) + b"\r\n")
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
        return [tag + "S", *noise, tag + "B" + str(len(value_literal)), tag + "C1",
                *[tag + "D" + p for p in parts], tag + "E", "=> 0"]
    return reply


def client(monkeypatch, fake, **kw):
    monkeypatch.setattr(socket, "create_connection", lambda address, timeout: fake.client)
    options = {"timeout": 3, "connect_timeout": 3, **kw}
    return Telnet("local", 0, "alice", "pw", **options)


def test_value_is_reassembled_from_chunks_amid_noise(monkeypatch):
    fake = FakeMoo(answer('{#12, "a \\"b\\"", {1, 2.5}}', noise=["Bob says, \"hi\""]))
    t = client(monkeypatch, fake)
    assert t.eval("anything") == [Obj(12), 'a "b"', [1, 2.5]]
    assert fake.received[0] == "connect alice pw"
    assert "Bob says, \"hi\"" in t.noise


def test_socketpair_client_runs_production_open(monkeypatch):
    opened = []
    real_open = Telnet._open

    def tracked_open(self):
        opened.append((self.host, self.port))
        return real_open(self)

    monkeypatch.setattr(Telnet, "_open", tracked_open)
    t = client(monkeypatch, FakeMoo(answer("1")))

    assert t.eval("anything") == 1
    assert opened == [("local", 0)]


@pytest.mark.parametrize("expression, error_reply, expected", [
    ("secret", lambda tag: [tag + "S", tag + 'X{E_PERM, "Permission denied"}'], "E_PERM: Permission denied"),
    ("bad(", lambda tag: ["Line 1:  syntax error", "1 error."], "did not compile:\n  Line 1:  syntax error"),
])
def test_errors_become_moo_errors(monkeypatch, expression, error_reply, expected):
    """A raised error is reported as such; a compile error quotes what the core printed."""
    def reply(tag, line):
        if tag is None:
            return []
        if is_sentinel(tag, line):
            return [tag + "Z"]
        if "_r = (player)" in line:
            return [tag + "S", tag + "B2", tag + "D#1", tag + "E"]
        return error_reply(tag)
    t = client(monkeypatch, FakeMoo(reply))
    with pytest.raises(MooError, match=expected):
        t.eval(expression)


def test_login_failure_is_reported_at_once(monkeypatch):
    def reply(tag, line):
        if tag is None:
            return ["Either that player does not exist, or has a different password."]
        return ["I don't understand that."]
    with pytest.raises(MooError, match="could not log in .*does not exist"):
        client(monkeypatch, FakeMoo(reply)).eval("1")


def test_program_is_one_line_with_the_configured_prefix_and_tell():
    t = Telnet("h", 1, "p", "x", eval_prefix=";", tell="player:tell({})", chunk=10)
    line = t.program("~abcdefghij~", "1 + 1")
    assert "\n" not in line
    assert line.isascii()
    assert line.startswith(';player:tell("~abcdefghij~S"); try _r = (1 + 1);')
    assert r'length("\u00E9")' in line
    assert "_v[_i * 10 + 1..min((_i + 1) * 10, length(_v))]" in line


def test_wire_strips_and_refuses_telnet_negotiation():
    a, b = socket.socketpair()
    wire = _Wire(a)
    # IAC WILL 70 split across two reads, an escaped IAC, CRLF endings.
    wire._feed(bytes([IAC]))
    wire._feed(bytes([WILL, 70]) + b"hello\r\nwor")
    wire._feed(b"ld " + bytes([IAC, IAC]) + b"\r\n")
    assert list(wire.lines) == ["hello", "world \udcff"]
    assert b.recv(16) == bytes([IAC, 254, 70])  # DONT 70


def test_wire_closed_drains_complete_lines_before_reporting_eof():
    a, b = socket.socketpair()
    wire = _Wire(a)
    a.settimeout(0.25)
    b.sendall(b"last notice\r\n")
    b.shutdown(socket.SHUT_WR)

    assert wire.closed() is True
    assert list(wire.lines) == ["last notice"]
    assert a.gettimeout() == 0.25


def test_request_gives_both_writes_one_deadline():
    class DeadlineWire:
        def __init__(self):
            self.sent = []
            self.lines = []
            self.sock = SimpleNamespace(close=lambda: None)

        def send_line(self, line, deadline):
            self.sent.append((line, deadline))
            if len(self.sent) == 2:
                tag = TAG.search(self.sent[0][0]).group(1)
                self.lines = [tag + "S", tag + "B1", tag + "C1", tag + "D1", tag + "E", tag + "Z"]

        def read_line(self, deadline):
            return self.lines.pop(0)

    t = Telnet("local", 0, "alice", "pw")
    t._wire = DeadlineWire()
    before = time.monotonic()

    assert t._request("1", 3) == 1
    assert len(t._wire.sent) == 2
    assert t._wire.sent[0][1] == t._wire.sent[1][1]
    assert t._wire.sent[0][1] > before


def test_second_write_failure_is_an_ambiguous_moo_error_and_closes():
    class FailedSocket:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FailedWire:
        def __init__(self):
            self.sock = FailedSocket()
            self.writes = 0

        def send_line(self, line, deadline=None):
            self.writes += 1
            if self.writes == 2:
                raise OSError("peer reset")

    t = Telnet("local", 0, "alice", "pw")
    wire = FailedWire()
    t._wire = wire

    with pytest.raises(MooError, match="outcome is unknown"):
        t._request("potentially_destructive_call()", 3)

    assert wire.writes == 2
    assert wire.sock.closed is True
    assert t._wire is None


def test_connection_close_mid_request_is_reported_without_retry(monkeypatch):
    fake = None

    def reply(tag, line):
        if tag is None:
            return []
        if is_sentinel(tag, line):
            return [tag + "Z"]
        if "_r = (player)" in line:
            return [tag + "S", tag + "B2", tag + "C1", tag + "D#1", tag + "E"]
        fake.srv.shutdown(socket.SHUT_RDWR)
        fake.srv.close()
        return []

    fake = FakeMoo(reply)
    t = client(monkeypatch, fake)

    # The peer may close before the sentinel write or while we read its reply.
    # Either path must report an uncertain outcome without retrying the call.
    with pytest.raises(MooError, match="the MOO closed the connection|request was not retried"):
        t.eval("potentially_destructive_call()")
    assert sum("potentially_destructive_call()" in line for line in fake.received) == 1


def test_completed_answer_survives_a_missing_sentinel(monkeypatch):
    def reply(tag, line):
        if tag is None:
            return []
        if is_sentinel(tag, line):
            return []
        return [tag + "S", tag + "B2", tag + "C1", tag + "D#1", tag + "E"]

    t = client(monkeypatch, FakeMoo(reply), timeout=0.02, connect_timeout=0.02)

    assert t.eval("anything") == Obj(1)


def test_declared_answer_length_must_match_the_received_chunks(monkeypatch):
    def reply(tag, line):
        if tag is None:
            return []
        if is_sentinel(tag, line):
            return [tag + "Z"]
        if "_r = (player)" in line:
            return [tag + "S", tag + "B2", tag + "D#1", tag + "E"]
        return [tag + "S", tag + "B2", tag + "D1", tag + "E"]

    t = client(monkeypatch, FakeMoo(reply))
    with pytest.raises(MooError, match="declared 2 characters but received 1"):
        t.eval("anything")


def test_unicode_answer_split_between_utf8_bytes_is_reassembled(monkeypatch):
    literal = '"' + "a" * 898 + "ébb" + '"'
    payload = literal.encode()
    assert len(payload) == 904

    def reply(tag, line):
        if tag is None:
            return []
        if is_sentinel(tag, line):
            return [tag + "Z"]
        if "_r = (player)" in line:
            return [tag + "S", tag + "B2", tag + "D#1", tag + "E"]
        prefix = tag.encode()
        return [prefix + b"S", prefix + b"B904", prefix + b"C2", prefix + b"D" + payload[:900],
                prefix + b"D" + payload[900:], prefix + b"E"]

    t = client(monkeypatch, FakeMoo(reply))
    assert t.eval("anything") == literal[1:-1]


def test_truncated_unicode_answer_is_rejected(monkeypatch):
    def reply(tag, line):
        if tag is None:
            return []
        if is_sentinel(tag, line):
            return [tag + "Z"]
        if "_r = (player)" in line:
            return [tag + "S", tag + "B2", tag + "D#1", tag + "E"]
        # mooR quote_str emits `"\u00E9a"`; this frame lost the `a`.
        return [tag + "S", tag + "B9", tag + "C1", tag + r'D"\u00E9"', tag + "E"]

    t = client(monkeypatch, FakeMoo(reply))
    with pytest.raises(MooError, match="declared 9"):
        t.eval("anything")


@pytest.mark.parametrize("expression", ["1\n2", "1\r2"])
def test_multiline_expressions_are_rejected_before_they_reach_the_wire(expression):
    t = Telnet("local", 0, "alice", "pw")
    t._wire = SimpleNamespace(send_line=lambda line: pytest.fail("sent malformed expression"))

    with pytest.raises(MooError, match="newline"):
        t._request(expression, 1)


def test_moor_escaped_nested_export_record_uses_moor_dialect(monkeypatch):
    literal = (
        r'{{#10, "H\u00E9", #0, #-1, #1, "", '
        r'{{"greeting", 1, #1, "rc", "\"\\u0645\\n\""}}, {}}}'
    )
    transport = client(monkeypatch, FakeMoo(answer(literal, chunk=13)))
    world = SimpleNamespace(
        transport=transport,
        ignore_props=set(),
        helper=lambda verb, arg: arg,
        eval=transport.eval,
    )

    obj = export_mod.export(world, Refs(player=Obj(1), registry={"hall": Obj(10)}), ["hall"])["hall"]

    assert obj.name == "Hé"
    assert obj.props[0].value == "م\n"


@pytest.mark.parametrize("value", ["Café", "مرحبا", "before\x01after", "before😀after"])
def test_moor_telnet_round_trips_strings_using_ascii_only_commands(monkeypatch, value):
    literal = moolit.serialize(value, dialect=moolit.MOOR)
    assert literal.isascii()
    if value == "before😀after":
        assert literal == 'urldecode("before%F0%9F%98%80after")'
    answer_literal = moolit.escape(value, dialect=moolit.MOOR, raw_unicode=True)

    def reply(tag, line):
        if tag is None:
            return ["*** Connected ***"]
        if is_sentinel(tag, line):
            return [tag + "Z"]
        result = "1" if "_r = (player)" in line or "_r = (1)" in line else answer_literal
        return [tag + "S", tag + "B" + str(len(result)), tag + "C1",
                tag + "D" + result, tag + "E"]

    fake = FakeMoo(reply)
    t = client(monkeypatch, fake)
    assert t.eval("1") == 1
    assert t.literal_dialect == moolit.MOOR

    assert t.eval(t.serialize(value)) == value
    if value == "before😀after":
        assert any(f"_r = ({literal})" in line for line in fake.received)
    assert all(line.isascii() and all(0x20 <= ord(char) <= 0x7E for char in line) for line in fake.received)


@pytest.mark.parametrize("value", ["Café", "مرحبا", "before\x01after", "before😀after"])
def test_lambdamoo_telnet_rejects_string_bytes_the_server_would_drop(monkeypatch, value):
    def reply(tag, line):
        if tag is None:
            return ["*** Connected ***"]
        if is_sentinel(tag, line):
            return [tag + "Z"]
        return [tag + "S", tag + "B1", tag + "C2", tag + "D1", tag + "E"]

    fake = FakeMoo(reply)
    t = client(monkeypatch, fake)
    assert t.eval("1") == 1
    assert t.literal_dialect == moolit.LAMBDA
    before = len(fake.received)

    with pytest.raises(MooError, match="printable ASCII"):
        t.eval(t.serialize(value))

    assert len(fake.received) == before
