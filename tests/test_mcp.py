"""The MCP transport's HTTP protocol and answer handling."""

import io
import json
import urllib.error

import pytest

from terramoo import moolit
from terramoo.errors import MooError
from terramoo.moolit import Err, Map, Obj
from terramoo.transport import mcp
from terramoo.transport.mcp import McpTransport


def transport(answer):
    t = McpTransport("https://moo.example/mcp", "token")
    t.sent = []

    def call_tool(name, arguments):
        t.sent.append((name, arguments))
        return answer

    t.call_tool = call_tool
    return t


def test_expression_is_wrapped_in_toliteral_and_the_literal_parsed():
    t = transport('"{#12, E_PERM, [\\"k\\" -> 1]}"')
    assert t.eval("x") == [Obj(12), Err("E_PERM"), Map({"k": 1})]
    assert t.sent == [("eval", {"expression": "toliteral(x)"})]


def test_a_tagged_answer_is_untagged():
    assert transport('"#5"  (STR)').eval("x") == Obj(5)


def test_a_non_string_answer_is_refused():
    with pytest.raises(MooError, match="not a toliteral"):
        transport("12").eval("x")


def test_install_verb_uses_the_set_verb_tool_and_creates_when_missing():
    t = transport('"0"')
    t.install_verb(Obj(9), "tmoo_info", ["return 1;"])
    name, args = t.sent[-1]
    assert name == "set_verb"
    assert args["create"] is True and args["code"] == "return 1;" and args["object"] == "#9"


class Response:
    def __init__(self, body, content_type="application/json"):
        self.body = body.encode()
        self.headers = {"Content-Type": content_type}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_rpc_sends_bearer_json_and_increments_request_ids(monkeypatch):
    requests = []

    def urlopen(request, timeout):
        requests.append((request, timeout))
        request_id = json.loads(request.data)["id"]
        return Response(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {"ok": request_id}}))

    monkeypatch.setattr(mcp.urllib.request, "urlopen", urlopen)
    t = McpTransport("https://moo.example/mcp", "secret-token", timeout=12)

    assert t.rpc("one", {"x": 1}) == {"ok": 1}
    assert t.rpc("two", {}) == {"ok": 2}
    first, timeout = requests[0]
    assert timeout == 12
    assert first.get_header("Authorization") == "Bearer secret-token"
    assert first.get_header("Content-type") == "application/json"
    assert json.loads(first.data) == {"jsonrpc": "2.0", "id": 1, "method": "one", "params": {"x": 1}}


def test_rpc_uses_the_last_sse_data_event(monkeypatch):
    body = 'data: {"ignored": true}\n\ndata: {"result": {"value": 3}}\n'
    monkeypatch.setattr(
        mcp.urllib.request,
        "urlopen",
        lambda request, timeout: Response(body, "text/event-stream; charset=utf-8"),
    )
    assert McpTransport("https://moo.example/mcp", "token").rpc("tools/list", {}) == {"value": 3}


def test_rpc_turns_http_and_network_errors_into_moo_errors(monkeypatch):
    error = urllib.error.HTTPError("https://moo.example/mcp", 403, "Forbidden", {}, io.BytesIO(b"denied"))
    monkeypatch.setattr(mcp.urllib.request, "urlopen", lambda request, timeout: (_ for _ in ()).throw(error))
    with pytest.raises(MooError, match="HTTP 403.*denied"):
        McpTransport("https://moo.example/mcp", "token").rpc("tools/list", {})

    monkeypatch.setattr(
        mcp.urllib.request,
        "urlopen",
        lambda request, timeout: (_ for _ in ()).throw(urllib.error.URLError("offline")),
    )
    with pytest.raises(MooError, match="cannot reach .*offline"):
        McpTransport("https://moo.example/mcp", "token").rpc("tools/list", {})


def test_rpc_rejects_invalid_json_as_a_transport_error(monkeypatch):
    monkeypatch.setattr(mcp.urllib.request, "urlopen", lambda request, timeout: Response("not json"))
    with pytest.raises(MooError, match="invalid JSON-RPC response"):
        McpTransport("https://moo.example/mcp", "token").rpc("tools/list", {})


@pytest.mark.parametrize(
    "body, expected",
    [
        ("[]", "expected an object"),
        ('{"jsonrpc": "2.0", "id": 1}', "missing result"),
        ('{"error": "permission denied"}', "permission denied"),
    ],
)
def test_rpc_rejects_malformed_json_rpc_envelopes(monkeypatch, body, expected):
    monkeypatch.setattr(mcp.urllib.request, "urlopen", lambda request, timeout: Response(body))

    with pytest.raises(MooError, match=expected):
        McpTransport("https://moo.example/mcp", "token").rpc("tools/list", {})


@pytest.mark.parametrize(
    "result",
    [
        ["not", "an", "object"],
        {"content": "not a list"},
        {"content": [None]},
        {"content": [{"type": "text", "text": 7}]},
    ],
)
def test_call_tool_rejects_a_malformed_tool_result(result):
    t = McpTransport("https://moo.example/mcp", "token")
    t.rpc = lambda method, params: result

    with pytest.raises(MooError, match="malformed result from eval"):
        t.call_tool("eval", {})


def test_tool_errors_and_set_prop_payloads_are_preserved():
    t = McpTransport("https://moo.example/mcp", "token")
    t.rpc = lambda method, params: {"isError": True, "content": [{"type": "text", "text": "denied"}]}
    with pytest.raises(MooError, match="denied"):
        t.call_tool("set_prop", {})

    sent = []
    t.call_tool = lambda name, arguments: sent.append((name, arguments)) or "ok"
    t.set_prop(Obj(9), 'odd "name', [Obj(2), "value"])
    assert sent == [
        (
            "set_prop",
            {"object": "#9", "prop": 'odd "name', "value": moolit.serialize([Obj(2), "value"])},
        )
    ]


def test_moor_dialect_is_used_for_mcp_set_prop_literals():
    sent = []
    t = McpTransport("https://moo.example/mcp", "token", dialect="moor")
    t.call_tool = lambda name, arguments: sent.append((name, arguments)) or "ok"

    t.set_prop(Obj(9), "greeting", "Hé\n")

    assert sent[-1][1]["value"] == r'"H\u00E9\n"'
