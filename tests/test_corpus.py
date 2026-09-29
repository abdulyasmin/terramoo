"""Conformance checks against the vendored ToastStunt/AgiMoo corpus."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from terramoo import moolit
from terramoo.cli import parse_object_arg
from terramoo.errors import MooError
from terramoo.transport.mcp import McpTransport
from terramoo.transport.telnet import DONT, IAC, WONT, _Wire


FIXTURES = Path(__file__).parents[1] / "moo-fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


LITERALS = load("literals.json")
GATE = load("gate-contract.json")
MCP21 = load("mcp21.json")
TELNET = load("telnet.json")
MOOR = load("moor.json")


def test_corpus_source_hashes_match():
    lines = (FIXTURES / "SOURCE").read_text().splitlines()
    recorded = dict(line.split(maxsplit=1)[::-1] for line in lines if line.endswith(".json"))
    actual = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in FIXTURES.glob("*.json")
    }
    assert lines[:2] == ["AgiMoo", "commit 5fb0cde"]
    assert recorded == actual


@pytest.mark.parametrize("case", LITERALS["strings"], ids=lambda case: case["id"])
def test_corpus_string_literals(case):
    assert moolit.serialize(case["value"]) == case["literal"]
    assert moolit.parse(case["literal"]) == case["value"]


GENERAL_STRING_LIST_CASES = [
    case for case in LITERALS["stringLists"]
    if case["id"] not in {"reject-mixed-types", "reject-nested-list"}
]


@pytest.mark.parametrize("case", GENERAL_STRING_LIST_CASES, ids=lambda case: case["id"])
def test_corpus_string_lists(case):
    if case["value"] is None:
        with pytest.raises(moolit.LiteralError):
            moolit.parse(case["literal"])
    else:
        assert moolit.parse(case["literal"]) == case["value"]


LITERAL_VALUES = [case for case in LITERALS["values"] if case["status"] == "literal"]


@pytest.mark.parametrize("case", LITERAL_VALUES, ids=lambda case: case["id"])
def test_corpus_typed_literals(case):
    value = moolit.parse(case["literal"])
    if case["kind"] == "ERR":
        assert value == moolit.Err(case["value"])
    else:
        assert value == case["value"]
    assert moolit.serialize(value) == case["canonical"]


class _GateTransport(McpTransport):
    def __init__(self, result):
        super().__init__("https://moo.example/mcp", "token")
        self.result = result

    def rpc(self, method: str, params: dict) -> dict:
        assert method == "tools/call"
        assert params == {"name": "eval", "arguments": {"expression": "x"}}
        return self.result


@pytest.mark.parametrize("case", GATE["formats"], ids=lambda case: case["id"])
def test_corpus_hosted_gate_text_is_preserved(case):
    transport = _GateTransport({"content": [{"type": "text", "text": case["text"]}]})
    assert transport.call_tool("eval", {"expression": "x"}) == case["text"]


@pytest.mark.parametrize("case", GATE["failures"], ids=lambda case: case["id"])
def test_corpus_hosted_gate_errors(case):
    result = {"isError": True, "content": [{"type": "text", "text": case["text"]}]}
    with pytest.raises(MooError, match=case["text"]):
        _GateTransport(result).call_tool("eval", {"expression": "x"})


def test_corpus_exclusions_are_explicit():
    """Fail on corpus drift until every new out-of-scope case is classified."""
    assert [case["id"] for case in LITERALS["stringLists"] if case not in GENERAL_STRING_LIST_CASES] == [
        "reject-mixed-types",
        "reject-nested-list",
    ]
    assert [case["input"] for case in LITERALS["objectReferences"]] == [
        "#0", "#123", "#-1", "#-123", "$room", "$a.b", "$_a.b_2", "-#1",
        "$1room", "$a.2b", "$a..b", "#123\n", "#123.name", "#1; shutdown()",
        "#550e8400-e29b-41d4-a716-446655440000",
        "#[550e8400-e29b-41d4-a716-446655440000]",
    ]
    assert [case["id"] for case in LITERALS["values"] if case["status"] != "literal"] == [
        "negative-leading-dot", "waif", "anonymous", "corified-chain", "uuid-object",
        "uuid-bracketed", "error-with-message", "unknown-error-with-message",
    ]
    assert {key: len(MCP21[key]) for key in ("parse", "serialize", "sessions")} == {
        "parse": 9,
        "serialize": 1,
        "sessions": 1,
    }


class _RecordingSocket:
    def __init__(self):
        self.sent = bytearray()

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)


def filter_telnet(case) -> tuple[bytes, bytes]:
    sock = _RecordingSocket()
    wire = _Wire(sock)
    data = bytearray()
    for chunk in case["chunks"]:
        wire.raw.extend(chunk)
        data.extend(wire._strip_iac())
    return bytes(data), bytes(sock.sent)


@pytest.mark.parametrize("case", TELNET["cases"], ids=lambda case: case["id"])
def test_corpus_telnet_data_framing(case):
    data, _ = filter_telnet(case)
    assert data == bytes(case["data"])


TELNET_REPLY_CASES = [case for case in TELNET["cases"] if case["id"] not in {"echo", "mcp"}]


@pytest.mark.parametrize("case", TELNET_REPLY_CASES, ids=lambda case: case["id"])
def test_corpus_telnet_negotiation_replies(case):
    _, replies = filter_telnet(case)
    assert replies == b"".join(bytes(reply) for reply in case["replies"])


@pytest.mark.parametrize("case", TELNET["lineCases"], ids=lambda case: case["id"])
def test_corpus_telnet_lines(case):
    wire = _Wire(_RecordingSocket())
    for chunk in case["chunks"]:
        wire._feed(bytes(chunk))
    lines = [line.encode("utf-8", "surrogateescape") for line in wire.lines]
    assert lines == [bytes(line) for line in case["lines"]]


@pytest.mark.parametrize("case", MOOR["objectReferences"], ids=lambda case: case["input"])
def test_corpus_moor_object_references(case):
    if case["accepted"]:
        assert isinstance(parse_object_arg(case["input"]), moolit.Obj)
    else:
        with pytest.raises(MooError):
            parse_object_arg(case["input"])


def test_telnet_refusal_policy_for_corpus_echo_and_mcp_cases():
    echo = next(case for case in TELNET["cases"] if case["id"] == "echo")
    mcp = next(case for case in TELNET["cases"] if case["id"] == "mcp")
    assert filter_telnet(echo)[1] == bytes([IAC, DONT, 1])
    assert filter_telnet(mcp)[1] == bytes([IAC, DONT, 70, IAC, WONT, 70])
