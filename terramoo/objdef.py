"""The objdef file format: one object per file, readable and diffable.

    object grand_courtyard
      name: "Grand Courtyard"
      parent: $room
      location: @gatehouse          # omitted when nowhere (#-1)
      owner: #2                     # omitted when the player owns it
      flags: "r"                    # subset of rwf; omitted when empty

      property description (flags: "rc") = {"A broad court...", "..."};
      property greeting (flags: "r", owner: #2) = "hello";
      override arrival_msg = "The lions never stop pouring.";

      verb bow (any none none) flags: "rd"
        player:tell("You bow.");
      endverb
    endobject

`property` defines a property on this object; `override` sets a value on
one inherited from an ancestor.  An inherited property with no `override`
line is clear (inherits its value).  Values are MOO literals, plus `$name`
for corified objects and `@name` for objects in the registry (`@me` is the
player).  Verb code is indented four spaces; the MOO's own two-space
unparse indent sits inside that, so a file diff is a code diff.

The shape follows mooR's objdef export so a repo in this format can be
read by either server; the keywords mooR spells differently (`override`
is ours, mooR writes every property the same way) are the only divergence.
"""

from __future__ import annotations

import re

from . import moolit
from .model import ObjectDef, PropDef, VerbDef
from .moolit import Obj, Ref

CODE_INDENT = "    "


class FormatError(ValueError):
    pass


_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_HEAD_RE = re.compile(rf"^object\s+({_IDENT})\s*$")
_FIELD_RE = re.compile(rf"^\s*({_IDENT}):\s*(.*?)\s*$")
_PROP_RE = re.compile(r'^\s*property\s+(?:"([^"]+)"|(\S+))\s*\((.*?)\)\s*=\s*(.*)$', re.S)
_OVERRIDE_RE = re.compile(r'^\s*override\s+(?:"([^"]+)"|(\S+))\s*=\s*(.*)$', re.S)
_VERB_RE = re.compile(r'^\s*verb\s+(?:"([^"]+)"|(\S+))\s*\((\S+)\s+(\S+)\s+(\S+)\)\s*(.*?)\s*$')
_OPT_RE = re.compile(r'(\w+):\s*("(?:[^"\\]|\\.)*"|\S+)')


def _split_options(text: str) -> dict[str, str]:
    return {k: v for k, v in _OPT_RE.findall(text)}


def _parse_ref(text: str):
    text = text.strip()
    if text in ("none", "nothing"):
        return Obj(-1)
    v = moolit.parse(text)
    if not isinstance(v, (Obj, Ref)):
        raise FormatError(f"expected an object reference, got {text!r}")
    return v


def _quoted(text: str) -> str:
    v = moolit.parse(text)
    if not isinstance(v, str):
        raise FormatError(f"expected a string, got {text!r}")
    return v


def _name(quoted: str | None, bare: str | None) -> str:
    return quoted if quoted is not None else bare


def parse(text: str) -> ObjectDef:
    lines = text.split("\n")
    n = len(lines)
    i = 0
    while i < n and not lines[i].strip():
        i += 1
    if i >= n:
        raise FormatError("empty file")
    m = _HEAD_RE.match(lines[i].strip())
    if not m:
        raise FormatError(f"line {i + 1}: expected `object <name>`")
    obj = ObjectDef(key=m.group(1), name="", parent=Obj(-1))
    i += 1
    seen_name = seen_parent = False
    while i < n:
        raw = lines[i]
        line = raw.strip()
        if not line:
            i += 1
            continue
        if line == "endobject":
            i += 1
            break
        if line.startswith("verb "):
            vm = _VERB_RE.match(raw)
            if not vm:
                raise FormatError(f"line {i + 1}: bad verb header")
            names = _name(vm.group(1), vm.group(2))
            opts = _split_options(vm.group(6))
            code = []
            i += 1
            while i < n and lines[i].strip() != "endverb":
                code.append(_strip_indent(lines[i], i))
                i += 1
            if i >= n:
                raise FormatError(f"verb {names!r}: no endverb")
            i += 1
            obj.verbs.append(
                VerbDef(
                    names=names,
                    code=code,
                    args=(vm.group(3), vm.group(4), vm.group(5)),
                    perms=_quoted(opts["flags"]) if "flags" in opts else "rxd",
                    owner=_parse_ref(opts["owner"]) if "owner" in opts else None,
                )
            )
            continue
        if line.startswith("property ") or line.startswith("override "):
            # A value runs to the `;` that ends it, possibly lines later.
            stmt, i = _read_statement(lines, i)
            pm = _PROP_RE.match(stmt)
            if pm:
                opts = _split_options(pm.group(3))
                obj.props.append(
                    PropDef(
                        name=_name(pm.group(1), pm.group(2)),
                        value=moolit.parse(pm.group(4)),
                        perms=_quoted(opts["flags"]) if "flags" in opts else "rc",
                        owner=_parse_ref(opts["owner"]) if "owner" in opts else None,
                        defined=True,
                    )
                )
                continue
            om = _OVERRIDE_RE.match(stmt)
            if om:
                obj.props.append(
                    PropDef(name=_name(om.group(1), om.group(2)), value=moolit.parse(om.group(3)), defined=False)
                )
                continue
            raise FormatError(f"line {i}: bad property line")
        fm = _FIELD_RE.match(raw)
        if fm:
            key, val = fm.group(1), fm.group(2)
            if key == "name":
                obj.name = _quoted(val)
                seen_name = True
            elif key == "parent":
                obj.parent = _parse_ref(val)
                seen_parent = True
            elif key == "location":
                obj.location = _parse_ref(val)
            elif key == "owner":
                obj.owner = _parse_ref(val)
            elif key == "flags":
                obj.flags = _quoted(val)
            else:
                raise FormatError(f"line {i + 1}: unknown field {key!r}")
            i += 1
            continue
        raise FormatError(f"line {i + 1}: cannot parse {line!r}")
    else:
        raise FormatError("no endobject")
    rest = "".join(lines[i:]).strip()
    if rest:
        raise FormatError(f"text after endobject: {rest[:40]!r}")
    if not seen_name or not seen_parent:
        raise FormatError(f"object {obj.key}: name and parent are required")
    return obj


def _strip_indent(line: str, lineno: int) -> str:
    if not line.strip():
        return ""
    if line.startswith(CODE_INDENT):
        return line[len(CODE_INDENT):]
    raise FormatError(f"line {lineno + 1}: verb code must be indented {len(CODE_INDENT)} spaces")


def _read_statement(lines: list[str], i: int) -> tuple[str, int]:
    """Collect from line i to the `;` that closes the value, honouring strings
    and nesting.  Returns the statement text (without the `;`) and the next
    line index."""
    buf = []
    depth = 0
    in_str = False
    n = len(lines)
    while i < n:
        line = lines[i]
        j = 0
        while j < len(line):
            c = line[j]
            if in_str:
                if c == "\\":
                    j += 1
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
            elif c == ";" and depth == 0:
                buf.append(line[:j])
                if line[j + 1:].strip():
                    raise FormatError(f"line {i + 1}: text after `;`")
                return "\n".join(buf), i + 1
            j += 1
        buf.append(line)
        i += 1
    raise FormatError("value never closed with `;`")


# ---------------------------------------------------------------- writing


def _ident_or_quoted(name: str) -> str:
    return name if re.fullmatch(_IDENT, name) else moolit.escape(name)


def _value_lines(value) -> str:
    """Lists of strings (descriptions, help text) go one element per line so
    diffs stay line-sized; everything else stays on one line."""
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        inner = ",\n".join("    " + moolit.escape(v) for v in value)
        return "{\n" + inner + "\n  }"
    return moolit.serialize(value)


def render(obj: ObjectDef) -> str:
    out = [f"object {obj.key}", f"  name: {moolit.escape(obj.name)}", f"  parent: {obj.parent}"]
    if obj.location != Obj(-1):
        out.append(f"  location: {obj.location}")
    if obj.owner is not None:
        out.append(f"  owner: {obj.owner}")
    if obj.flags:
        out.append(f"  flags: {moolit.escape(obj.flags)}")
    if obj.props:
        out.append("")
    for p in obj.props:
        if p.defined:
            opts = f"flags: {moolit.escape(p.perms)}"
            if p.owner is not None:
                opts += f", owner: {p.owner}"
            out.append(f"  property {_ident_or_quoted(p.name)} ({opts}) = {_value_lines(p.value)};")
        else:
            out.append(f"  override {_ident_or_quoted(p.name)} = {_value_lines(p.value)};")
    for v in obj.verbs:
        out.append("")
        head = f"  verb {_ident_or_quoted(v.names)} ({v.args[0]} {v.args[1]} {v.args[2]}) flags: {moolit.escape(v.perms)}"
        if v.owner is not None:
            head += f" owner: {v.owner}"
        out.append(head)
        for line in v.code:
            out.append(CODE_INDENT + line if line else "")
        out.append("  endverb")
    out.append("endobject")
    return "\n".join(out) + "\n"
