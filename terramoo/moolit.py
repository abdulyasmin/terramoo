"""MOO literals: the text `toliteral()` prints, parsed to Python and back.

Python side of the mapping:

    int / float / str          themselves
    #123                       Obj(123)
    #048D05-1234567890         Obj("048D05-1234567890")  (mooR's UUID objects)
    'name                      Sym("name")  (mooR's symbols)
    E_PERM                     Err("E_PERM")
    {1, "a"}                   list
    ["k" -> v]                 Map (an insertion-ordered dict; MOO sorts keys
                               on its side, so order never round-trips exactly)
    true / false               bool (ToastStunt)
    $room / @grand_courtyard   Ref("$", "room") / Ref("@", "grand_courtyard")

`Ref` is this repo's, not MOO's: `$name` is `#0.name`, `@name` a registry
key.  Refs are resolved before anything reaches the MOO (`refs.resolve`)
and produced on export, so files avoid object numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class Map(dict):
    """A MOO map.  Distinct from dict so serialization can tell it apart."""


@dataclass(frozen=True)
class Obj:
    num: int | str  # str: a mooR UUID object id

    def __str__(self) -> str:
        return f"#{self.num}"


@dataclass(frozen=True)
class Sym:
    name: str

    def __str__(self) -> str:
        return f"'{self.name}"


@dataclass(frozen=True, order=True)
class Err:
    name: str

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True, order=True)
class Ref:
    kind: str  # "$" or "@"
    name: str

    def __str__(self) -> str:
        return f"{self.kind}{self.name}"


class LiteralError(ValueError):
    pass


_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<float>-?\d+\.\d+(?:[eE][+-]?\d+)?|-?\d+[eE][+-]?\d+)
  | (?P<int>-?\d+)
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<obj>\#[0-9A-Fa-f]{6}-[0-9A-Fa-f]{10}|\#-?\d+)
  | (?P<sym>'[A-Za-z_][A-Za-z0-9_]*)
  | (?P<err>E_[A-Z_]+)
  | (?P<bool>true|false)(?![A-Za-z0-9_])
  | (?P<sysref>\$[A-Za-z_][A-Za-z0-9_]*)
  | (?P<regref>@[A-Za-z_][A-Za-z0-9_]*)
  | (?P<arrow>->)
  | (?P<punct>[{}\[\],])
    """,
    re.VERBOSE,
)


def _tokens(text: str):
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise LiteralError(f"bad literal at offset {pos}: {text[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind != "ws":
            yield kind, m.group(kind)
    yield "eof", ""


def parse(text: str):
    """Parse one MOO literal; the whole of `text` must be that literal."""
    toks = list(_tokens(text))
    value, i = _parse_at(toks, 0)
    if toks[i][0] != "eof":
        raise LiteralError(f"trailing text after literal: {toks[i][1]!r}")
    return value


def _unescape(s: str) -> str:
    """A string token's contents: a backslash quotes the next character."""
    return re.sub(r"\\(.)", r"\1", s[1:-1])


def _parse_at(toks, i):
    kind, text = toks[i]
    if kind == "int":
        return int(text), i + 1
    if kind == "float":
        return float(text), i + 1
    if kind == "str":
        return _unescape(text), i + 1
    if kind == "obj":
        ident = text[1:]
        return Obj(ident if "-" in ident[1:] else int(ident)), i + 1
    if kind == "sym":
        return Sym(text[1:]), i + 1
    if kind == "err":
        return Err(text), i + 1
    if kind == "bool":
        return text == "true", i + 1
    if kind == "sysref":
        return Ref("$", text[1:]), i + 1
    if kind == "regref":
        return Ref("@", text[1:]), i + 1
    if kind == "punct" and text == "{":
        items = []
        i += 1
        if toks[i] == ("punct", "}"):
            return items, i + 1
        while True:
            v, i = _parse_at(toks, i)
            items.append(v)
            if toks[i] == ("punct", ","):
                i += 1
                continue
            if toks[i] == ("punct", "}"):
                return items, i + 1
            raise LiteralError(f"expected , or }} in list, got {toks[i][1]!r}")
    if kind == "punct" and text == "[":
        m = Map()
        i += 1
        if toks[i] == ("punct", "]"):
            return m, i + 1
        while True:
            k, i = _parse_at(toks, i)
            if toks[i][0] != "arrow":
                raise LiteralError(f"expected -> in map, got {toks[i][1]!r}")
            v, i = _parse_at(toks, i + 1)
            m[tuple(k) if isinstance(k, list) else k] = v
            if toks[i] == ("punct", ","):
                i += 1
                continue
            if toks[i] == ("punct", "]"):
                return m, i + 1
            raise LiteralError(f"expected , or ] in map, got {toks[i][1]!r}")
    raise LiteralError(f"unexpected {text!r}")


def escape(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def serialize(value) -> str:
    """Render a value as MOO source text.  `Ref`s are rendered as spelled;
    resolve them first (`refs.resolve`) if the text is going to the MOO."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)  # always has a point or an exponent, as MOO needs
    if isinstance(value, str):
        return escape(value)
    if isinstance(value, (Obj, Err, Ref, Sym)):
        return str(value)
    if isinstance(value, Map):
        return "[" + ", ".join(f"{serialize(k)} -> {serialize(v)}" for k, v in value.items()) + "]"
    if isinstance(value, (list, tuple)):
        return "{" + ", ".join(serialize(v) for v in value) + "}"
    raise LiteralError(f"cannot serialize {type(value).__name__}")


def walk(value, fn):
    """Rebuild `value` with `fn` applied to every leaf (lists and maps recursed)."""
    if isinstance(value, Map):
        return Map((k, walk(v, fn)) for k, v in value.items())
    if isinstance(value, list):
        return [walk(v, fn) for v in value]
    return fn(value)
