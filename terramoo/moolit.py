"""MOO literals: the text `toliteral()` prints, parsed to Python and back.

Python side of the mapping:

    int / float / str          themselves
    #123                       Obj(123)
    E_PERM                     Err("E_PERM")
    {1, "a"}                   list
    ["k" -> v]                 Map (an insertion-ordered dict; MOO sorts keys
                               on its side, so order never round-trips exactly)
    true / false               bool (ToastStunt)
    $room / @grand_courtyard   Ref("$", "room") / Ref("@", "grand_courtyard")

The two `Ref` spellings are this repo's, not MOO's: `$name` is a corified
object (`#0.name`) and `@name` an object the registry names.  They are
resolved to `Obj` before anything reaches the MOO and produced from `Obj`
on export, so a file never depends on an object number it does not have to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class Map(dict):
    """A MOO map.  Distinct from dict so serialization can tell it apart."""


@dataclass(frozen=True, order=True)
class Obj:
    num: int

    def __str__(self) -> str:
        return f"#{self.num}"


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


NOTHING = Obj(-1)


class LiteralError(ValueError):
    pass


_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<float>-?\d+\.\d+(?:[eE][+-]?\d+)?|-?\d+[eE][+-]?\d+)
  | (?P<int>-?\d+)
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<obj>\#-?\d+)
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
    out = []
    i = 1
    end = len(s) - 1
    while i < end:
        c = s[i]
        if c == "\\" and i + 1 < end:
            out.append(s[i + 1])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _parse_at(toks, i):
    kind, text = toks[i]
    if kind == "int":
        return int(text), i + 1
    if kind == "float":
        return float(text), i + 1
    if kind == "str":
        return _unescape(text), i + 1
    if kind == "obj":
        return Obj(int(text[1:])), i + 1
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
            m[_key(k)] = v
            if toks[i] == ("punct", ","):
                i += 1
                continue
            if toks[i] == ("punct", "]"):
                return m, i + 1
            raise LiteralError(f"expected , or ] in map, got {toks[i][1]!r}")
    raise LiteralError(f"unexpected {text!r}")


def _key(k):
    if isinstance(k, list):
        return tuple(k)
    return k


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
        s = repr(value)
        return s if ("." in s or "e" in s or "n" in s) else s + ".0"
    if isinstance(value, str):
        return escape(value)
    if isinstance(value, (Obj, Err, Ref)):
        return str(value)
    if isinstance(value, Map):
        return "[" + ", ".join(f"{serialize(_unkey(k))} -> {serialize(v)}" for k, v in value.items()) + "]"
    if isinstance(value, (list, tuple)):
        return "{" + ", ".join(serialize(v) for v in value) + "}"
    raise LiteralError(f"cannot serialize {type(value).__name__}")


def _unkey(k):
    return list(k) if isinstance(k, tuple) else k


def walk(value, fn):
    """Rebuild `value` with `fn` applied to every leaf (lists and maps recursed)."""
    if isinstance(value, Map):
        return Map((k, walk(v, fn)) for k, v in value.items())
    if isinstance(value, list):
        return [walk(v, fn) for v in value]
    return fn(value)


def from_json(value):
    """Convert a value the hosted MCP returned as native JSON.

    The gate tags non-JSON types: objects come back as "#123" strings and
    errors as "E_PERM" strings.  Ordinary strings that happen to look like
    those are therefore ambiguous, which is why export asks the helper verb
    for `toliteral()` text instead and only uses this for scalars it knows.
    """
    if isinstance(value, str):
        if re.fullmatch(r"#-?\d+", value):
            return Obj(int(value[1:]))
        if re.fullmatch(r"E_[A-Z_]+", value):
            return Err(value)
        return value
    if isinstance(value, list):
        return [from_json(v) for v in value]
    if isinstance(value, dict):
        return Map((k, from_json(v)) for k, v in value.items())
    return value
