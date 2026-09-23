"""The MCP transport's answer handling, with the HTTP call stubbed out."""

import pytest

from terramoo.errors import MooError
from terramoo.moolit import Err, Map, Obj
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
