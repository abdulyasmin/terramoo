"""An explicitly selected part of the authenticated player, without a lifecycle."""

from dataclasses import dataclass, field
import re

from . import moolit, objdef
from .errors import MooError
from .model import ObjectDef, PropDef, VerbDef
from .moolit import Obj, Ref, walk
from .world import DEFAULT_IGNORE_PROPS


# This list is also substituted into the server reader at bootstrap. Selection
# is checked there before property_info, is_clear_property, or a value read.
PROTECTED = DEFAULT_IGNORE_PROPS | {
    "name", "owner", "parent", "location", "contents", "programmer", "wizard",
    "r", "w", "f", "aliases", "gender", "home", "display_options", "edit_options",
    "prog_options", "build_options", "ansi_options", "replace_codes", "key",
    "ps", "psc", "po", "poc", "pp", "ppc", "pq", "pqc", "pr", "prc", "verb_subs",
    "eval_env", "eval_ticks", "eval_subs", "out_of_band_session", "reading_input",
    "last_password_time", "last_connect_attempt", "last_connect_place",
    "linebuffer", "linesleft", "linetask", "_mail_task", "message_keep_date",
    "messages_kept", "inline_editor_options", "_terramoo_player_state",
}
SETTINGS = {"aliases", "gender", "home", "linelen", "pagelen"}
OPTIONS = {"display", "edit", "prog", "build"}


def safe_name(name):
    if not isinstance(name, str) or not name.strip() or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise MooError("player field names must be nonempty strings without control characters")
    return name


def property_name(name):
    safe_name(name)
    if name.lower() in PROTECTED or any(s in name.lower() for s in ("password", "secret", "token", "credential")) or name.lower().startswith("_terramoo_"):
        raise MooError(f"player property {name!r} is protected; use a supported setting where applicable")
    return name


def setting_name(name):
    safe_name(name)
    prefix, sep, option = name.partition(":")
    if name not in SETTINGS and not (sep and prefix in OPTIONS and option):
        raise MooError(f"unsupported player setting {name!r}")
    return name


def feature_ref(value):
    if not isinstance(value, (Ref, Obj)) or isinstance(value, Ref) and value.kind == "@" and value.name.lower() == "me":
        raise MooError("a feature must be an object reference other than @me")
    return value


def slot(kind, name):
    return kind + ":" + str(name).lower()


def player_owner(owner):
    return owner is None or isinstance(owner, Ref) and owner.kind == "@" and owner.name.lower() == "me"


@dataclass
class PlayerDef:
    props: list[PropDef] = field(default_factory=list)
    verbs: list[VerbDef] = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    features: list = field(default_factory=list)
    removals: dict = field(default_factory=dict)  # slot -> (action, kind, name)

    def entries(self):
        entries = {}
        def add(kind, name, value):
            key = slot(kind, name)
            if key in entries:
                raise MooError(f"duplicate player field {key}")
            entries[key] = (kind, name, value)
        for prop in self.props:
            property_name(prop.name)
            if not player_owner(prop.owner) or set(prop.perms) - set("rwc"):
                raise MooError(f"player property {prop.name}: owner must be @me and flags must be rwc")
            add("property", prop.name, prop)
        for verb in self.verbs:
            safe_name(verb.names)
            if not player_owner(verb.owner) or set(verb.perms) - set("rwxd"):
                raise MooError(f"player verb {verb.names}: owner must be @me and flags must be rwxd")
            if verb.args[0] not in {"this", "any", "none"} or verb.args[2] not in {"this", "any", "none"}:
                raise MooError(f"player verb {verb.names}: invalid argument specification")
            add("verb", verb.names, verb)
        for name, value in self.settings.items():
            if name == "aliases" and (not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value)):
                raise MooError("aliases must be a list of nonempty strings")
            if name == "gender" and not isinstance(value, str):
                raise MooError("gender must be a string understood by the core")
            if name == "home" and not isinstance(value, (Obj, Ref)):
                raise MooError("home must be an object reference")
            if name in {"linelen", "pagelen"} and type(value) is not int:
                raise MooError(f"{name} must be an integer")
            add("setting", setting_name(name), value)
        for value in self.features:
            add("feature", feature_ref(value), True)
        for key, (action, kind, name) in self.removals.items():
            if kind == "property":
                property_name(name)
            elif kind == "verb":
                safe_name(name)
            else:
                feature_ref(name)
            add(kind, name, action)
        return entries


def parse(text):
    lines = text.strip().splitlines()
    if not lines or lines[0].strip() != "player" or lines[-1].strip() != "endplayer":
        raise MooError("player.moo must start with player and end with endplayer")
    body = []
    result = PlayerDef()
    i = 1
    while i < len(lines) - 1:
        line = lines[i].strip()
        if not line:
            i += 1
        elif line.startswith("verb "):
            body.append(lines[i])
            i += 1
            while i < len(lines) - 1 and lines[i].strip() != "endverb":
                body.append(lines[i])
                i += 1
            if i >= len(lines) - 1:
                raise MooError("player verb has no endverb")
            body.append(lines[i])
            i += 1
        elif line.startswith(("property ", "override ")):
            start = i
            _, i = objdef._read_statement(lines, i)
            body.extend(lines[start:i])
        elif line.startswith("setting "):
            statement, i = objdef._read_statement(lines, i)
            match = re.fullmatch(r'\s*setting\s+("(?:[^"\\]|\\.)*"|\S+)\s*=\s*(.*)', statement, re.S)
            if not match:
                raise MooError("expected setting NAME = VALUE;")
            name = objdef._parse_ident(match[1]).lower()
            if name in result.settings:
                raise MooError(f"duplicate player setting {name}")
            result.settings[name] = moolit.parse(match[2], dialect=moolit.MOOR)
        elif line.startswith(("feature ", "detach ", "remove ", "clear ")):
            statement, i = objdef._read_statement(lines, i)
            action, _, rest = statement.strip().partition(" ")
            if action in ("feature", "detach"):
                name = moolit.parse(rest.strip(), dialect=moolit.MOOR)
                kind = "feature"
            else:
                kind, _, rest = rest.strip().partition(" ")
                if kind not in ("property", "verb") or action == "clear" and kind != "property":
                    raise MooError("use remove property, remove verb, clear property, or detach")
                name = objdef._parse_ident(rest.strip())
            if action == "feature":
                result.features.append(name)
            else:
                key = slot(kind, name)
                if key in result.removals:
                    raise MooError(f"duplicate player removal {key}")
                result.removals[key] = (action, kind, name)
        else:
            raise MooError(f"unsupported player declaration: {line}")
    obj = objdef.parse('object player\n  name: ""\n  parent: #-1\n' + "\n".join(body) + '\nendobject\n')
    result.props, result.verbs = obj.props, obj.verbs
    result.entries()
    return result


def render(profile):
    profile.entries()
    obj = ObjectDef("player", "", Obj(-1), props=profile.props, verbs=profile.verbs)
    body = objdef.render(obj).splitlines()[3:-1]
    out = ["player", *body]
    for name, value in profile.settings.items():
        out.append(f"  setting {objdef._ident_or_quoted(name)} = {objdef._render_value(value)};")
    for feature in profile.features:
        out.append(f"  feature {feature};")
    for action, kind, name in profile.removals.values():
        out.append(f"  detach {name};" if kind == "feature" else f"  {action} {kind} {objdef._ident_or_quoted(name)};")
    return "\n".join([*out, "endplayer", ""])


def values(profile):
    return [*(p.value for p in profile.props), *profile.settings.values(), *profile.features,
            *(name for _, kind, name in profile.removals.values() if kind == "feature")]


def map_refs(profile, transform):
    for prop in profile.props:
        prop.value = walk(prop.value, transform)
    profile.settings = {k: walk(v, transform) for k, v in profile.settings.items()}
    profile.features = [walk(v, transform) for v in profile.features]
    profile.removals = {slot(kind, name): (action, kind, name) for action, kind, name in (
        (a, k, walk(n, transform) if k == "feature" else n) for a, k, n in profile.removals.values())}
    return profile
