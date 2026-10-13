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

Python's `==` calls 1, 1.0 and True equal, but they are three different
MOO values.  Compare values with `same`, never `==`.  For the same reason a
map cannot hold keys that differ only in type, such as 1 and 1.0: a Python
dict would merge them.  The MOO, in turn, keeps only one of two string keys
that differ only in letter case.  `parse` refuses both kinds of map.

A value nests lists and maps at most `MAX_DEPTH` deep, so the recursive code
that handles it stays inside Python's recursion limit.

An answer from the MOO is parsed leniently (`strict_keys=False`,
`max_depth=ANSWER_DEPTH`): it wraps values in a few more lists, and it may
hold a map that terramoo can compare but not reproduce.  Such a map records
the keys it merged, and `unrepresentable` says why a value read that way
cannot be written to a file.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from urllib.parse import quote


class Map(dict):
    """A MOO map.  Distinct from dict so serialization can tell it apart.
    `merged` lists the pairs of keys that a lenient `parse` had to merge."""

    merged: tuple = ()


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


# Lists and maps nested deeper than this are refused in a value.  Real MOO
# values nest a few levels; the bound keeps every recursive walk, render and
# copy of a value well inside Python's default recursion limit of 1000.
MAX_DEPTH = 100
# The bound for text that wraps values: a helper's answer or a journal entry.
# The wrappers add at most a few levels, so a value within MAX_DEPTH always
# fits, and a value read this way is checked with `nesting` before use.
ANSWER_DEPTH = 2 * MAX_DEPTH

_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)

LAMBDA = "lambda"
MOOR = "moor"
_DIALECTS = {LAMBDA, MOOR}


_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<float>-?(?:(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+))
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


def _dialect(value: str) -> str:
    if value not in _DIALECTS:
        raise ValueError(f"unknown MOO literal dialect {value!r}")
    return value


def parse(text: str, *, dialect: str = LAMBDA, max_depth: int = MAX_DEPTH, strict_keys: bool = True):
    """Parse one MOO literal; the whole of `text` must be that literal.
    Lists and maps may nest at most `max_depth` levels deep.  With
    `strict_keys`, a map whose keys Python would merge, or whose string keys
    differ only in letter case, is an error; without it, such a map keeps the
    last value and records the merge in `Map.merged`."""
    dialect = _dialect(dialect)
    try:
        toks = list(_tokens(text))
        value, i = _parse_at(toks, 0, dialect, 0, max_depth, strict_keys)
        if toks[i][0] != "eof":
            raise LiteralError(f"trailing text after literal: {toks[i][1]!r}")
        return value
    except LiteralError:
        raise
    except (TypeError, ValueError, OverflowError) as e:
        raise LiteralError(str(e)) from None
    except RecursionError:
        # A backstop: MAX_DEPTH already refuses deep values, unless this was
        # called from an unusually deep stack.
        raise LiteralError("value is nested too deeply to read") from None


def _unescape(s: str, dialect: str) -> str:
    """Decode a string token using the selected server's escape rules."""
    if dialect == LAMBDA:
        return re.sub(r"\\(.)", r"\1", s[1:-1])

    out = []
    body = s[1:-1]
    i = 0
    simple = {'"': '"', "'": "'", "\\": "\\", "n": "\n", "r": "\r", "t": "\t", "0": "\0"}
    while i < len(body):
        c = body[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= len(body):
            raise LiteralError("unexpected end of string escape")
        escape = body[i]
        i += 1
        if escape in simple:
            out.append(simple[escape])
            continue
        digits = 2 if escape == "x" else 4 if escape == "u" else 0
        if not digits:
            out.append(escape)  # mooR keeps LambdaMOO's unknown-escape behavior
            continue
        encoded = body[i:i + digits]
        if len(encoded) != digits or not all(c in "0123456789abcdefABCDEF" for c in encoded):
            raise LiteralError(f"invalid \\{escape} escape")
        codepoint = int(encoded, 16)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise LiteralError(f"invalid \\{escape} escape")
        out.append(chr(codepoint))
        i += digits
    return "".join(out)


def _parse_at(toks, i, dialect, depth, max_depth, strict_keys):
    kind, text = toks[i]
    if kind == "int":
        return int(text), i + 1
    if kind == "float":
        return float(text), i + 1
    if kind == "str":
        return _unescape(text, dialect), i + 1
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
    if kind == "punct" and text in ("{", "[") and depth >= max_depth:
        raise LiteralError(f"value nests lists or maps more than {max_depth} levels deep")
    if kind == "punct" and text == "{":
        items = []
        i += 1
        if toks[i] == ("punct", "}"):
            return items, i + 1
        while True:
            v, i = _parse_at(toks, i, dialect, depth + 1, max_depth, strict_keys)
            items.append(v)
            if toks[i] == ("punct", ","):
                i += 1
                continue
            if toks[i] == ("punct", "}"):
                return items, i + 1
            raise LiteralError(f"expected , or }} in list, got {toks[i][1]!r}")
    if kind == "punct" and text == "[":
        m = Map()
        folded = set()
        i += 1
        if toks[i] == ("punct", "]"):
            return m, i + 1
        while True:
            k, i = _parse_at(toks, i, dialect, depth + 1, max_depth, strict_keys)
            if toks[i][0] != "arrow":
                raise LiteralError(f"expected -> in map, got {toks[i][1]!r}")
            v, i = _parse_at(toks, i + 1, dialect, depth + 1, max_depth, strict_keys)
            key = tuple(k) if isinstance(k, list) else k
            if key in m:
                # A repeated key is the MOO's own last-one-wins; a key that
                # Python merely calls equal (1 and 1.0) would be lost.
                existing = next(stored for stored in m if stored == key)
                if not same(existing, key):
                    if strict_keys:
                        raise LiteralError(
                            f"map keys {serialize(existing, dialect=dialect)} and {serialize(key, dialect=dialect)} "
                            "are different MOO values that terramoo cannot keep apart"
                        )
                    m.merged = (*m.merged, (existing, key))
            elif strict_keys and _folded(key) in folded:
                existing = next(stored for stored in m if _folded(stored) == _folded(key))
                raise LiteralError(
                    f"map keys {serialize(existing, dialect=dialect)} and {serialize(key, dialect=dialect)} "
                    "differ only in letter case, and the MOO keeps only one of them"
                )
            if _folded(key) is not None:
                folded.add(_folded(key))
            m[key] = v
            if toks[i] == ("punct", ","):
                i += 1
                continue
            if toks[i] == ("punct", "]"):
                return m, i + 1
            raise LiteralError(f"expected , or ] in map, got {toks[i][1]!r}")
    raise LiteralError(f"unexpected {text!r}")


def escape(s: str, *, dialect: str = LAMBDA, raw_unicode: bool = False) -> str:
    """Quote `s` as a string literal.  `raw_unicode` keeps printable non-ASCII
    characters as themselves in mooR literals (object files are UTF-8; only
    text bound for the server needs `\\uNNNN`)."""
    dialect = _dialect(dialect)
    if dialect == LAMBDA:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    out = ['"']
    special = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\0": "\\0"}
    for c in s:
        if c in special:
            out.append(special[c])
        elif ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F:
            out.append(f"\\x{ord(c):02X}")
        elif not raw_unicode and not c.isascii() and ord(c) <= 0xFFFF:
            out.append(f"\\u{ord(c):04X}")
        else:
            out.append(c)
    out.append('"')
    return "".join(out)


def serialize(value, *, dialect: str = LAMBDA, raw_unicode: bool = False) -> str:
    """Render a value as MOO source text.  `Ref`s are rendered as spelled;
    resolve them first (`refs.resolve`) if the text is going to the MOO."""
    dialect = _dialect(dialect)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)  # always has a point or an exponent, as MOO needs
    if isinstance(value, str):
        if dialect == MOOR and not raw_unicode and any(ord(c) > 0xFFFF for c in value):
            # mooR accepts only four hex digits in a \u escape.  Build strings
            # containing larger code points from an ASCII-only UTF-8 percent
            # encoding instead, so telnet never has to send the raw character.
            return f"urldecode({escape(quote(value, safe=''), dialect=MOOR)})"
        return escape(value, dialect=dialect, raw_unicode=raw_unicode)
    if isinstance(value, (Obj, Err, Ref, Sym)):
        return str(value)
    if isinstance(value, Map):
        return "[" + ", ".join(
            f"{serialize(k, dialect=dialect, raw_unicode=raw_unicode)} -> "
            f"{serialize(v, dialect=dialect, raw_unicode=raw_unicode)}"
            for k, v in value.items()
        ) + "]"
    if isinstance(value, (list, tuple)):
        return "{" + ", ".join(serialize(v, dialect=dialect, raw_unicode=raw_unicode) for v in value) + "}"
    raise LiteralError(f"cannot serialize {type(value).__name__}")


def walk(value, fn):
    """Rebuild `value` with `fn` applied to every leaf.

    Map keys are values too.  A MOO list used as a map key is represented by
    a tuple in Python so it remains hashable while its elements are walked.
    """
    if isinstance(value, Map):
        out = Map()
        for key, item in value.items():
            new_key = walk(key, fn)
            if new_key in out:
                raise LiteralError(f"map key collision after transformation: {new_key!r}")
            out[new_key] = walk(item, fn)
        return out
    if isinstance(value, list):
        return [walk(v, fn) for v in value]
    if isinstance(value, tuple):
        return tuple(walk(v, fn) for v in value)
    return fn(value)


def _folded(key):
    """A string or symbol key as the MOO compares it, or None for others.
    ToastStunt folds ASCII letters only, so non-ASCII letters stay apart."""
    if isinstance(key, str):
        return ("str", key.translate(_ASCII_LOWER))
    if isinstance(key, Sym):
        return ("sym", key.name.lower())
    return None


def _identity(value):
    """A hashable form of `value` that keeps each MOO type distinct.

    Two values have the same identity exactly when they are the same MOO
    literal, except that a map's entries are unordered: the MOO sorts map
    keys itself.  So an integer, a float and a boolean never match, and
    neither do two strings that differ only in case.
    """
    if isinstance(value, bool):  # before int: bool is an int subclass
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        # repr distinguishes -0.0 from 0.0 and makes NaN equal to itself.
        return ("float", repr(value))
    if isinstance(value, dict):  # Map, and any plain dict
        return ("map", frozenset((_identity(k), _identity(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):  # a tuple is a list used as a map key
        return ("list", tuple(_identity(v) for v in value))
    if isinstance(value, str):
        return ("str", value)
    return (type(value).__name__, value)  # Obj, Err, Ref and Sym


def same(a, b) -> bool:
    """Whether `a` and `b` are the same MOO value (see `_identity`)."""
    return _identity(a) == _identity(b)


def unrepresentable(value) -> str | None:
    """Why `value`, read leniently from the MOO, cannot be written to a file
    as the MOO holds it; None when it can."""
    if nesting(value) > MAX_DEPTH:
        return f"nests lists or maps more than {MAX_DEPTH} levels deep"
    merge = merged_keys(value)
    if merge:
        return (f"has the map keys {serialize(merge[0])} and {serialize(merge[1])}, "
                "different MOO values that terramoo cannot keep apart")
    return None


def merged_keys(value):
    """The first pair of map keys a lenient `parse` merged in `value`, or None.
    Such a value cannot be sent back to the MOO as it is there."""
    if isinstance(value, dict):
        if getattr(value, "merged", ()):
            return value.merged[0]
        for k, v in value.items():
            found = merged_keys(k) or merged_keys(v)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for v in value:
            found = merged_keys(v)
            if found:
                return found
    return None


def nesting(value) -> int:
    """How many lists or maps deep `value` nests: 0 for a scalar, 1 for {}."""
    if isinstance(value, dict):
        return 1 + max((max(nesting(k), nesting(v)) for k, v in value.items()), default=0)
    if isinstance(value, (list, tuple)):
        return 1 + max((nesting(v) for v in value), default=0)
    return 0
