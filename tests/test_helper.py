"""Execute the real helper offline with the optional LambdaMOO testbed sources.

NP_SINGLE uses stdin/stdout instead of sockets. Build only in pytest's temporary
directory; never start or modify a running testbed or download missing sources.
"""

import gzip
from pathlib import Path
import platform
import re
import shutil
import subprocess

import pytest

from terramoo import plan
from terramoo.moolit import parse, serialize
from terramoo.model import ObjectDef, PropDef
from terramoo.moolit import Obj, Ref
from terramoo.refs import Refs
from terramoo.world import HELPER_VERBS, registry_value

ROOT = Path(__file__).resolve().parents[1]


def test_exit_callbacks_are_all_wrapped_by_registry_reconciliation():
    helper = (ROOT / "terramoo/helper/tmoo_apply.moo").read_text()
    callbacks = re.findall(
        r"(?<!tmoo_callback\()\b(?:src|dst|new_room|old_room|room):"
        r"(?:add_exit|add_entrance|remove_exit|remove_entrance)\(o\)",
        helper,
    )
    assert callbacks == []


def test_registry_reconcile_keeps_the_unprotected_revision_floor():
    helper = (ROOT / "terramoo/helper/tmoo_registry.moo").read_text()
    fallback = helper.split('elseif (mode == "reconcile")', 1)[1].split(
        'protected_revision = this._terramoo_registry_revision;', 1
    )[0]

    assert "before = args[2];" in fallback
    assert (
        "revision = registry_revision > before[4] ? registry_revision | before[4];"
        in fallback
    )
    assert "reg[4] = revision;" in fallback


def _helper_install_source():
    statements = []
    for name in HELPER_VERBS:
        code = (ROOT / f"terramoo/helper/{name}.moo").read_text().splitlines()
        statements.extend([
            f'add_verb(tool, {{#2, "xd", "{name}"}}, {{"this", "none", "this"}});',
            f'errors = set_verb_code(tool, "{name}", {serialize(code)});',
            'if (errors) raise(E_INVARG, toliteral(errors)); endif',
        ])
    return "\n".join(statements)


@pytest.fixture(scope="module")
def offline_moo(tmp_path_factory):
    build = ROOT / "testbeds/lambdamoo/.build"
    source = build / f"{platform.system().lower()}-{platform.machine()}" / "MOO-1.8.1"
    core = build / "LambdaCore-17May04.db.gz"
    if not (source / "Makefile").exists() or not core.exists():
        pytest.skip("requires the locally built LambdaMOO testbed sources")
    if not shutil.which("make") or not shutil.which("cc"):
        pytest.skip("requires make and cc for the stdin/stdout LambdaMOO build")
    work = tmp_path_factory.mktemp("offline-moo")
    source = Path(shutil.copytree(source, work / "src", ignore=shutil.ignore_patterns("*.o", "moo")))
    options = source / "options.h"
    text, count = re.subn(r"(#define NETWORK_PROTOCOL\s+)NP_TCP", r"\1NP_SINGLE", options.read_text())
    assert count == 1, "refuse to run unless the socket backend was replaced"
    options.write_text(text)
    result = subprocess.run(["make", "moo"], cwd=source, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    database = work / "pristine.db"
    database.write_bytes(gzip.decompress(core.read_bytes()))
    return source / "moo", database


@pytest.mark.parametrize("callback_key", ["auto_child", "CHILD"])
def test_create_preserves_initialize_callback_registration(offline_moo, tmp_path, callback_key):
    binary, database = offline_moo
    initialize = [
        f'return this.toolbox:tmoo_apply({{{{"register", "{callback_key}", this, "callback-generation"}}}});'
    ]
    script = f""";;
tool = create(#-1);
add_property(tool, "registry", {{{{}}, {{}}, {{}}}}, {{#2, ""}});
{_helper_install_source()}
p = create(#-1);
add_property(p, "toolbox", tool, {{#2, ""}});
add_verb(p, {{#2, "xd", "initialize"}}, {{"this", "none", "this"}});
errors = set_verb_code(p, "initialize", {serialize(initialize)});
if (errors) raise(E_INVARG, toliteral(errors)); endif
result = tool:tmoo_apply({{{{"create", "child", #-1, "", p, "Child", "create-generation"}}}});
child = children(p)[1];
return {{result, tool.registry, child, valid(child)}};
.
quit
"""
    result = subprocess.run(
        [str(binary), "-e", str(database), str(tmp_path / "out.db")],
        input=script, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    reply = re.search(r"^=> (.+)$", result.stdout, re.MULTILINE)
    assert reply, result.stdout
    ops, registry, child, child_valid = parse(reply[1])
    assert registry_value(registry) == {callback_key: child}
    assert registry_value(registry).generations == {callback_key: "callback-generation"}
    assert child_valid == 1
    assert ops[0][:2] == [0, "E_INVARG"]
    assert "child" in ops[0][2].lower()


@pytest.mark.parametrize("callback", ["add", "nested", "rebind", "remove", "shift"])
def test_destroy_preserves_recycle_callback_registry_changes(offline_moo, tmp_path, callback):
    binary, database = offline_moo
    changes = 'this.toolbox.registry = {{"old", "survivor"}, {this, this.survivor}, {"old-generation", "survivor-generation"}};'
    if callback == "nested":
        # The callback registers through the helper itself, which advances the
        # protected revision; that is not a concurrent writer.
        changes = 'this.toolbox:tmoo_apply({{"register", "survivor", this.survivor, "survivor-generation"}});'
    elif callback == "rebind":
        changes = 'this.toolbox.registry = {{"OLD"}, {this.survivor}, {"survivor-generation"}};'
    elif callback == "remove":
        changes = 'this.toolbox.registry = {{"survivor"}, {this.survivor}, {"survivor-generation"}};'
    elif callback == "shift":
        changes = 'this.toolbox.registry = {{"old", "survivor"}, {this, this.survivor}, {"old-generation", "survivor-generation"}};'
    recycle = [changes, "return 1;"]
    expected_key = "OLD" if callback == "rebind" else "survivor"
    expected_revision = 2 if callback in ("rebind", "remove") else 3
    initial = '{{"before", "old"}, {after, old}, {"before-generation", "old-generation"}}' if callback == "shift" else '{{"old"}, {old}, {"old-generation"}}'
    script = f""";;
tool = create(#-1);
add_property(tool, "registry", {{{{}}, {{}}, {{}}}}, {{#2, ""}});
{_helper_install_source()}
old = create(#-1);
survivor = create(#-1);
after = create(#-1);
add_property(old, "toolbox", tool, {{#2, ""}});
add_property(old, "survivor", survivor, {{#2, ""}});
add_property(old, "_terramoo_generation", "old-generation", {{#2, "r"}});
add_verb(old, {{#2, "xd", "recycle"}}, {{"this", "none", "this"}});
errors = set_verb_code(old, "recycle", {serialize(recycle)});
if (errors) raise(E_INVARG, toliteral(errors)); endif
tool.registry = {initial};
add_property(tool, "_terramoo_registry_revision", 0, {{#2, "r"}});
add_property(tool, "_terramoo_registry_state", tool.registry, {{#2, "r"}});
result = tool:tmoo_apply({{{{"destroy", "old", old, "old-generation"}}, {{"register", "after", after, "after-generation"}}}});
return {{result, tool.registry == {{{{"{expected_key}", "after"}}, {{survivor, after}}, {{"survivor-generation", "after-generation"}}, {expected_revision}}}, valid(old), valid(survivor), after}};
.
quit
"""
    result = subprocess.run(
        [str(binary), "-e", str(database), str(tmp_path / "out.db")],
        input=script, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    reply = re.search(r"^=> (.+)$", result.stdout, re.MULTILINE)
    assert reply, result.stdout
    ops, registry_matches, old_valid, survivor_valid, after = parse(reply[1])
    assert [registry_matches, old_valid, survivor_valid] == [1, 0, 1], result.stdout
    assert ops[1] == [1, after]
    if callback == "rebind":
        assert ops[0][:2] == [0, "E_INVARG"]
    else:
        assert ops[0] == [1, 1]


def _run_helper_script(offline_moo, tmp_path, body):
    binary, database = offline_moo
    script = f""";;
tool = create(#-1);
add_property(tool, "registry", {{{{}}, {{}}, {{}}}}, {{#2, ""}});
{_helper_install_source()}
add_property(#2, "_terramoo_test_tool", tool, {{#2, ""}});
return 1;
.
;;
tool = #2._terramoo_test_tool;
{body}
.
quit
"""
    result = subprocess.run(
        [str(binary), "-e", str(database), str(tmp_path / "out.db")],
        input=script, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    replies = re.findall(r"^=> (.+)$", result.stdout, re.MULTILINE)
    assert replies, result.stdout
    reply = replies[-1]
    assert reply != ">>Unknown value<<", result.stdout
    return parse(reply)


def test_endpoint_refusal_restores_property_and_old_room_membership(offline_moo, tmp_path):
    result = _run_helper_script(offline_moo, tmp_path, """
old = create(#-1);
new = create(#-1);
door = create($exit);
add_property(old, "exits", {door}, {#2, "rw"});
add_property(new, "exits", {}, {#2, "rw"});
add_verb(old, {#2, "xd", "remove_exit"}, {"this", "none", "this"});
set_verb_code(old, "remove_exit", {"this.exits = setremove(this.exits, args[1]);", "return 1;"});
add_verb(old, {#2, "xd", "add_exit"}, {"this", "none", "this"});
set_verb_code(old, "add_exit", {"this.exits = setadd(this.exits, args[1]);", "return 1;"});
add_verb(new, {#2, "xd", "add_exit"}, {"this", "none", "this"});
set_verb_code(new, "add_exit", {"return 0;"});
add_verb(new, {#2, "xd", "remove_exit"}, {"this", "none", "this"});
set_verb_code(new, "remove_exit", {"this.exits = setremove(this.exits, args[1]);", "return 1;"});
door.source = old;
add_property(door, "_terramoo_generation", "door-generation", {#2, "r"});
tool.registry = {{"door"}, {door}, {"door-generation"}};
result = tool:tmoo_apply({{"endpoint", "door", door, "door-generation", "source", old, new}});
return {result, door.source == old, door in old.exits, door in new.exits};
""")
    assert result[0][0][:2] == [0, "E_INVARG"]
    assert result[1:] == [1, 1, 0]


def test_endpoint_remove_failure_compensates_every_completed_step(offline_moo, tmp_path):
    result = _run_helper_script(offline_moo, tmp_path, """
old = create(#-1);
new = create(#-1);
door = create($exit);
add_property(old, "exits", {door}, {#2, "rw"});
add_property(new, "exits", {}, {#2, "rw"});
add_verb(old, {#2, "xd", "remove_exit"}, {"this", "none", "this"});
set_verb_code(old, "remove_exit", {"this.exits = setremove(this.exits, args[1]);", "return 0;"});
add_verb(old, {#2, "xd", "add_exit"}, {"this", "none", "this"});
set_verb_code(old, "add_exit", {"this.exits = setadd(this.exits, args[1]);", "return 1;"});
add_verb(new, {#2, "xd", "add_exit"}, {"this", "none", "this"});
set_verb_code(new, "add_exit", {"this.exits = setadd(this.exits, args[1]);", "return 1;"});
add_verb(new, {#2, "xd", "remove_exit"}, {"this", "none", "this"});
set_verb_code(new, "remove_exit", {"this.exits = setremove(this.exits, args[1]);", "return 1;"});
door.source = old;
add_property(door, "_terramoo_generation", "door-generation", {#2, "r"});
tool.registry = {{"door"}, {door}, {"door-generation"}};
result = tool:tmoo_apply({{"endpoint", "door", door, "door-generation", "source", old, new}});
return {result, door.source == old, door in old.exits, door in new.exits};
""")
    assert result[0][0][:2] == [0, "E_INVARG"]
    assert result[1:] == [1, 1, 0]


def test_endpoint_rollback_restores_property_before_guarded_old_membership(offline_moo, tmp_path):
    result = _run_helper_script(offline_moo, tmp_path, """
old = create(#-1);
new = create(#-1);
door = create($exit);
add_property(old, "exits", {door}, {#2, "rw"});
add_property(new, "exits", {}, {#2, "rw"});
add_verb(old, {#2, "xd", "remove_exit"}, {"this", "none", "this"});
set_verb_code(old, "remove_exit", {"this.exits = setremove(this.exits, args[1]);", "return 0;"});
add_verb(old, {#2, "xd", "add_exit"}, {"this", "none", "this"});
set_verb_code(old, "add_exit", {"if (args[1].source != this) return 0; endif", "this.exits = setadd(this.exits, args[1]);", "return 1;"});
add_verb(new, {#2, "xd", "add_exit"}, {"this", "none", "this"});
set_verb_code(new, "add_exit", {"this.exits = setadd(this.exits, args[1]);", "return 1;"});
add_verb(new, {#2, "xd", "remove_exit"}, {"this", "none", "this"});
set_verb_code(new, "remove_exit", {"this.exits = setremove(this.exits, args[1]);", "return 1;"});
door.source = old;
add_property(door, "_terramoo_generation", "door-generation", {#2, "r"});
tool.registry = {{"door"}, {door}, {"door-generation"}};
result = tool:tmoo_apply({{"endpoint", "door", door, "door-generation", "source", old, new, 1, 1}});
return {result, door.source == old, door in old.exits, door in new.exits};
""")
    assert result[0][0][:2] == [0, "E_INVARG"]
    assert result[1:] == [1, 1, 0]


def test_rename_revision_rejects_a_stale_interleaved_operation(offline_moo, tmp_path):
    result = _run_helper_script(offline_moo, tmp_path, """
o = create(#-1);
add_property(o, "_terramoo_generation", "object-generation", {#2, "r"});
tool.registry = {{"old"}, {o}, {"object-generation"}, 0};
result = tool:tmoo_apply({{"rename", 1, 0, o, "object-generation", "other"}, {"rename", 1, 0, o, "object-generation", "new"}});
return {result, tool.registry[1][1], tool.registry[4]};
""")
    assert result[0][0][0] == 1
    assert result[0][1][:2] == [0, "E_INVARG"]
    assert result[1:] == ["other", 1]


def test_direct_callback_registry_edit_advances_revision_and_rejects_stale_rename(
    offline_moo, tmp_path
):
    callback = serialize([
        "saved = this.toolbox.registry;",
        'this.toolbox.registry = {{"operator_choice", "door"}, saved[2], saved[3], saved[4]};',
        "this.exits = setadd(this.exits, args[1]);",
        "return 1;",
    ])
    body = """
existing = create(#-1);
room = create(#-1);
door = create($exit);
add_property(existing, "_terramoo_generation", "existing-generation", {#2, "r"});
add_property(door, "_terramoo_generation", "door-generation", {#2, "r"});
add_property(room, "toolbox", tool, {#2, ""});
add_property(room, "exits", {}, {#2, "rw"});
add_verb(room, {#2, "xd", "add_exit"}, {"this", "none", "this"});
set_verb_code(room, "add_exit", CALLBACK_CODE);
door.source = room;
door.dest = #-1;
tool.registry = {{"old", "door"}, {existing, door}, {"existing-generation", "door-generation"}, 2};
add_property(tool, "_terramoo_registry_revision", 2, {#2, "r"});
add_property(tool, "_terramoo_registry_state", tool.registry, {#2, "r"});
result = tool:tmoo_apply({{"link", {{"door", door, "door-generation"}}}, {"rename", 1, 2, existing, "existing-generation", "stale"}});
return {result, tool.registry};
""".replace("CALLBACK_CODE", callback)

    result = _run_helper_script(offline_moo, tmp_path, body)

    registry = registry_value(result[1])
    assert result[0][0][0] == 1
    assert result[0][1][:2] == [0, "E_INVARG"]
    assert list(registry) == ["operator_choice", "door"]
    assert registry.revision == 3


def test_nested_non_registry_apply_does_not_bless_a_direct_callback_edit(
    offline_moo, tmp_path
):
    callback = serialize([
        "saved = this.toolbox.registry;",
        'this.toolbox.registry = {{"operator_choice", "door"}, saved[2], saved[3], saved[4]};',
        'this.toolbox:tmoo_apply({{"name", "operator_choice", this.existing, "existing-generation", "Nested name"}});',
        "this.exits = setadd(this.exits, args[1]);",
        "return 1;",
    ])
    body = """
existing = create(#-1);
room = create(#-1);
door = create($exit);
add_property(existing, "_terramoo_generation", "existing-generation", {#2, "r"});
add_property(door, "_terramoo_generation", "door-generation", {#2, "r"});
add_property(room, "toolbox", tool, {#2, ""});
add_property(room, "existing", existing, {#2, ""});
add_property(room, "exits", {}, {#2, "rw"});
add_verb(room, {#2, "xd", "add_exit"}, {"this", "none", "this"});
set_verb_code(room, "add_exit", CALLBACK_CODE);
door.source = room;
door.dest = #-1;
tool.registry = {{"old", "door"}, {existing, door}, {"existing-generation", "door-generation"}, 2};
add_property(tool, "_terramoo_registry_revision", 2, {#2, "r"});
add_property(tool, "_terramoo_registry_state", tool.registry, {#2, "r"});
result = tool:tmoo_apply({{"link", {{"door", door, "door-generation"}}}, {"rename", 1, 2, existing, "existing-generation", "stale"}});
return {result, tool.registry, existing.name};
""".replace("CALLBACK_CODE", callback)

    result = _run_helper_script(offline_moo, tmp_path, body)

    registry = registry_value(result[1])
    assert result[0][0][0] == 1
    assert result[0][1][:2] == [0, "E_INVARG"]
    assert list(registry) == ["operator_choice", "door"]
    assert registry.revision == 3
    assert result[2] == "Nested name"


def test_stale_derived_callback_registry_write_reports_conflict_and_keeps_current(
    offline_moo, tmp_path
):
    callback = serialize([
        "saved = this.toolbox.registry;",
        'this.toolbox:tmoo_apply({{"register", "other", this.other, "other-generation"}});',
        'this.toolbox.registry = {{@saved[1], "callback"}, {@saved[2], this.callback_object}, {@saved[3], "callback-generation"}, saved[4]};',
        "this.exits = setadd(this.exits, args[1]);",
        "return 1;",
    ])
    body = """
existing = create(#-1);
other = create(#-1);
callback_object = create(#-1);
room = create(#-1);
door = create($exit);
add_property(existing, "_terramoo_generation", "existing-generation", {#2, "r"});
add_property(door, "_terramoo_generation", "door-generation", {#2, "r"});
add_property(room, "toolbox", tool, {#2, ""});
add_property(room, "other", other, {#2, ""});
add_property(room, "callback_object", callback_object, {#2, ""});
add_property(room, "exits", {}, {#2, "rw"});
add_verb(room, {#2, "xd", "add_exit"}, {"this", "none", "this"});
set_verb_code(room, "add_exit", CALLBACK_CODE);
door.source = room;
door.dest = #-1;
tool.registry = {{"existing", "door"}, {existing, door}, {"existing-generation", "door-generation"}, 2};
add_property(tool, "_terramoo_registry_revision", 2, {#2, "r"});
add_property(tool, "_terramoo_registry_state", tool.registry, {#2, "r"});
result = tool:tmoo_apply({{"link", {{"door", door, "door-generation"}}}});
return {result, tool.registry};
""".replace("CALLBACK_CODE", callback)

    result = _run_helper_script(offline_moo, tmp_path, body)

    registry = registry_value(result[1])
    assert result[0][0][:2] == [0, "E_INVARG"]
    assert "registry conflict" in result[0][0][2].lower()
    assert "callback" in result[0][0][2]
    assert set(registry) == {"existing", "door", "other"}
    assert registry.revision == 3


def test_malformed_stale_callback_registry_is_restored_before_diagnostics(
    offline_moo, tmp_path
):
    callback = serialize([
        "saved = this.toolbox.registry;",
        'this.toolbox:tmoo_apply({{"register", "other", this.other, "other-generation"}});',
        "this.toolbox.registry = {saved[1], {saved[2][1]}, saved[3], saved[4]};",
        "this.exits = setadd(this.exits, args[1]);",
        "return 1;",
    ])
    body = """
existing = create(#-1);
other = create(#-1);
room = create(#-1);
door = create($exit);
add_property(existing, "_terramoo_generation", "existing-generation", {#2, "r"});
add_property(door, "_terramoo_generation", "door-generation", {#2, "r"});
add_property(room, "toolbox", tool, {#2, ""});
add_property(room, "other", other, {#2, ""});
add_property(room, "exits", {}, {#2, "rw"});
add_verb(room, {#2, "xd", "add_exit"}, {"this", "none", "this"});
set_verb_code(room, "add_exit", CALLBACK_CODE);
door.source = room;
door.dest = #-1;
tool.registry = {{"existing", "door"}, {existing, door}, {"existing-generation", "door-generation"}, 2};
add_property(tool, "_terramoo_registry_revision", 2, {#2, "r"});
add_property(tool, "_terramoo_registry_state", tool.registry, {#2, "r"});
result = tool:tmoo_apply({{"link", {{"door", door, "door-generation"}}}});
return {result, tool.registry, tool._terramoo_registry_state};
""".replace("CALLBACK_CODE", callback)

    result = _run_helper_script(offline_moo, tmp_path, body)

    registry = registry_value(result[1])
    assert result[0][0][:2] == [0, "E_INVARG"]
    assert "registry conflict" in result[0][0][2].lower()
    assert "unknown keys" in result[0][0][2].lower()
    assert set(registry) == {"existing", "door", "other"}
    assert registry.revision == 3
    assert result[1] == result[2]


def test_large_stale_callback_registry_conflict_is_bounded_and_restored(
    offline_moo, tmp_path
):
    keys = [f"key_{i:03}" for i in range(149)] + ["door"]
    key_literal = serialize(keys)
    object_literal = "{" + ", ".join([f"#{1000 + i}" for i in range(149)] + ["door"]) + "}"
    nonce_literal = serialize([f"generation-{i:03}" for i in range(149)] + ["door-generation"])
    callback = serialize([
        "saved = this.toolbox.registry;",
        'this.toolbox:tmoo_apply({{"register", "other", this.other, "other-generation"}});',
        'this.toolbox.registry = {{"callback", @listdelete(saved[1], 1)}, saved[2], saved[3], saved[4]};',
        "this.exits = setadd(this.exits, args[1]);",
        "return 1;",
    ])
    body = f"""
other = create(#-1);
room = create(#-1);
door = create($exit);
add_property(door, "_terramoo_generation", "door-generation", {{#2, "r"}});
add_property(room, "toolbox", tool, {{#2, ""}});
add_property(room, "other", other, {{#2, ""}});
add_property(room, "exits", {{}}, {{#2, "rw"}});
add_verb(room, {{#2, "xd", "add_exit"}}, {{"this", "none", "this"}});
set_verb_code(room, "add_exit", CALLBACK_CODE);
door.source = room;
door.dest = #-1;
tool.registry = {{{key_literal}, {object_literal}, {nonce_literal}, 2}};
add_property(tool, "_terramoo_registry_revision", 2, {{#2, "r"}});
add_property(tool, "_terramoo_registry_state", tool.registry, {{#2, "r"}});
result = tool:tmoo_apply({{{{"link", {{{{"door", door, "door-generation"}}}}}}}});
return {{result, tool.registry, tool._terramoo_registry_state}};
""".replace("CALLBACK_CODE", callback)

    result = _run_helper_script(offline_moo, tmp_path, body)

    registry = registry_value(result[1])
    assert result[0][0][:2] == [0, "E_INVARG"]
    assert "registry conflict" in result[0][0][2].lower()
    assert "many keys" in result[0][0][2].lower()
    assert len(registry) == 151
    assert "other" in registry
    assert registry.revision == 3
    assert result[1] == result[2]


@pytest.mark.parametrize("protected", [False, True], ids=["legacy", "protected"])
def test_rename_revision_survives_a_legacy_registry_from_recycle(
    offline_moo, tmp_path, protected
):
    recycle = serialize([
        'this.toolbox.registry = {{"other", "doomed"}, {this.target, this}, {"object-generation", "doomed-generation"}};',
        "return 1;",
    ])
    protected_setup = (
        """
add_property(tool, "_terramoo_registry_revision", 1, {#2, "r"});
add_property(tool, "_terramoo_registry_state", tool.registry, {#2, "r"});
"""
        if protected
        else ""
    )
    body = """
o = create(#-1);
doomed = create(#-1);
add_property(o, "_terramoo_generation", "object-generation", {#2, "r"});
add_property(doomed, "_terramoo_generation", "doomed-generation", {#2, "r"});
add_property(doomed, "toolbox", tool, {#2, ""});
add_property(doomed, "target", o, {#2, ""});
add_verb(doomed, {#2, "xd", "recycle"}, {"this", "none", "this"});
set_verb_code(doomed, "recycle", RECYCLE_CODE);
tool.registry = {{"old", "doomed"}, {o, doomed}, {"object-generation", "doomed-generation"}, 1};
PROTECTED_SETUP
result = tool:tmoo_apply({{"rename", 1, 1, o, "object-generation", "other"}, {"destroy", "doomed", doomed, "doomed-generation"}, {"rename", 1, 1, o, "object-generation", "stale"}});
return {result, tool.registry[1][1], tool.registry[4]};
""".replace("RECYCLE_CODE", recycle).replace("PROTECTED_SETUP", protected_setup)
    result = _run_helper_script(offline_moo, tmp_path, body)
    assert result[0][0][0] == 1
    assert result[0][1][0] == 1
    assert result[0][2][:2] == [0, "E_INVARG"]
    assert result[1:] == ["other", 4 if protected else 3]


def test_create_keeps_interleaved_registration_and_never_reuses_revision(
    offline_moo, tmp_path
):
    # The nested helper call models task B running while task A's callback is
    # yielded; the callback then resumes and restores its saved registry.
    initialize = serialize([
        "saved = this.toolbox.registry;",
        'this.toolbox:tmoo_apply({{"register", "other", this.other, "other-generation"}});',
        "this.toolbox.interleaved_revision = this.toolbox.registry[4];",
        "this.toolbox.registry = saved;",
        "return 1;",
    ])
    body = """
existing = create(#-1);
other = create(#-1);
parent = create(#-1);
add_property(existing, "_terramoo_generation", "existing-generation", {#2, "r"});
add_property(parent, "toolbox", tool, {#2, ""});
add_property(parent, "other", other, {#2, ""});
add_verb(parent, {#2, "xd", "initialize"}, {"this", "none", "this"});
set_verb_code(parent, "initialize", INITIALIZE_CODE);
tool.registry = {{"existing"}, {existing}, {"existing-generation"}, 2};
add_property(tool, "_terramoo_registry_revision", 2, {#2, "r"});
add_property(tool, "_terramoo_registry_state", tool.registry, {#2, "r"});
add_property(tool, "interleaved_revision", 0, {#2, "r"});
result = tool:tmoo_apply({{"create", "child", #-1, "", parent, "Child", "child-generation"}});
return {result, tool.registry, tool.interleaved_revision, tool._terramoo_registry_revision};
""".replace("INITIALIZE_CODE", initialize)

    result = _run_helper_script(offline_moo, tmp_path, body)

    registry = registry_value(result[1])
    assert set(registry) == {"existing", "other", "child"}
    assert registry.revision == 4
    assert result[2:] == [3, 4]
    assert result[2:] == sorted(set(result[2:]))


def test_link_keeps_interleaved_registration_from_add_exit(offline_moo, tmp_path):
    callback = serialize([
        "saved = this.toolbox.registry;",
        'this.toolbox:tmoo_apply({{"register", "other", this.other, "other-generation"}});',
        "this.toolbox.interleaved_revision = this.toolbox.registry[4];",
        "this.toolbox.registry = saved;",
        "this.exits = setadd(this.exits, args[1]);",
        "return 1;",
    ])
    body = """
existing = create(#-1);
other = create(#-1);
room = create(#-1);
door = create($exit);
add_property(existing, "_terramoo_generation", "existing-generation", {#2, "r"});
add_property(door, "_terramoo_generation", "door-generation", {#2, "r"});
add_property(room, "toolbox", tool, {#2, ""});
add_property(room, "other", other, {#2, ""});
add_property(room, "interleaved_revision", 0, {#2, "r"});
add_property(room, "exits", {}, {#2, "rw"});
add_verb(room, {#2, "xd", "add_exit"}, {"this", "none", "this"});
set_verb_code(room, "add_exit", CALLBACK_CODE);
door.source = room;
door.dest = #-1;
tool.registry = {{"existing", "door"}, {existing, door}, {"existing-generation", "door-generation"}, 2};
add_property(tool, "_terramoo_registry_revision", 2, {#2, "r"});
add_property(tool, "_terramoo_registry_state", tool.registry, {#2, "r"});
add_property(tool, "interleaved_revision", 0, {#2, "r"});
result = tool:tmoo_apply({{"link", {{"door", door, "door-generation"}}}});
return {result, tool.registry, tool.interleaved_revision};
""".replace("CALLBACK_CODE", callback)

    result = _run_helper_script(offline_moo, tmp_path, body)

    registry = registry_value(result[1])
    assert result[0][0][0] == 1
    assert set(registry) == {"existing", "door", "other"}
    assert registry.revision == result[2] == 3


def test_unlink_keeps_interleaved_registration_from_remove_exit(
    offline_moo, tmp_path
):
    callback = serialize([
        "saved = this.toolbox.registry;",
        'this.toolbox:tmoo_apply({{"register", "other", this.other, "other-generation"}});',
        "this.toolbox.interleaved_revision = this.toolbox.registry[4];",
        "this.toolbox.registry = saved;",
        "this.exits = setremove(this.exits, args[1]);",
        "return 1;",
    ])
    body = """
existing = create(#-1);
other = create(#-1);
room = create(#-1);
door = create($exit);
add_property(existing, "_terramoo_generation", "existing-generation", {#2, "r"});
add_property(door, "_terramoo_generation", "door-generation", {#2, "r"});
add_property(room, "toolbox", tool, {#2, ""});
add_property(room, "other", other, {#2, ""});
add_property(room, "exits", {door}, {#2, "rw"});
add_verb(room, {#2, "xd", "remove_exit"}, {"this", "none", "this"});
set_verb_code(room, "remove_exit", CALLBACK_CODE);
door.source = room;
tool.registry = {{"existing", "door"}, {existing, door}, {"existing-generation", "door-generation"}, 2};
add_property(tool, "_terramoo_registry_revision", 2, {#2, "r"});
add_property(tool, "_terramoo_registry_state", tool.registry, {#2, "r"});
add_property(tool, "interleaved_revision", 0, {#2, "r"});
result = tool:tmoo_apply({{"unlink", "door", door, "door-generation", "exit", room}});
return {result, tool.registry, tool.interleaved_revision};
""".replace("CALLBACK_CODE", callback)

    result = _run_helper_script(offline_moo, tmp_path, body)

    registry = registry_value(result[1])
    assert result[0][0][0] == 1
    assert set(registry) == {"existing", "door", "other"}
    assert registry.revision == result[2] == 3


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy-shape", "lower-revision"])
def test_helper_honors_external_registry_content_and_advances_revision(
    offline_moo, tmp_path, legacy
):
    registry = (
        '{{"operator"}, {operator}, {"operator-generation"}}'
        if legacy
        else '{{"operator"}, {operator}, {"operator-generation"}, 2}'
    )
    result = _run_helper_script(offline_moo, tmp_path, f"""
stale = create(#-1);
operator = create(#-1);
after = create(#-1);
add_property(stale, "_terramoo_generation", "stale-generation", {{#2, "r"}});
add_property(operator, "_terramoo_generation", "operator-generation", {{#2, "r"}});
tool.registry = {{{{"stale"}}, {{stale}}, {{"stale-generation"}}, 5}};
add_property(tool, "_terramoo_registry_revision", 5, {{#2, "r"}});
add_property(tool, "_terramoo_registry_state", tool.registry, {{#2, "r"}});
tool.registry = {registry};
result = tool:tmoo_apply({{{{"register", "after", after, "after-generation"}}}});
return {{result, tool.registry, tool._terramoo_registry_revision}};
""")

    registry = registry_value(result[1])
    assert result[0][0][0] == 1
    assert set(registry) == {"operator", "after"}
    assert registry.revision == result[2] == 7


def test_create_rejects_case_only_stale_callback_registry_rewrite(offline_moo, tmp_path):
    initialize = serialize([
        "saved = this.toolbox.registry;",
        'this.toolbox:tmoo_apply({{"rename", 1, 2, this.existing, "existing-generation", "task_b"}});',
        'this.toolbox.registry = {{"EXISTING"}, saved[2], saved[3], saved[4]};',
        "return 1;",
    ])
    body = """
existing = create(#-1);
parent = create(#-1);
add_property(existing, "_terramoo_generation", "existing-generation", {#2, "r"});
add_property(parent, "toolbox", tool, {#2, ""});
add_property(parent, "existing", existing, {#2, ""});
add_verb(parent, {#2, "xd", "initialize"}, {"this", "none", "this"});
set_verb_code(parent, "initialize", INITIALIZE_CODE);
tool.registry = {{"existing"}, {existing}, {"existing-generation"}, 2};
add_property(tool, "_terramoo_registry_revision", 2, {#2, "r"});
add_property(tool, "_terramoo_registry_state", tool.registry, {#2, "r"});
result = tool:tmoo_apply({{"create", "child", #-1, "", parent, "Child", "child-generation"}});
return {result, tool.registry, tool._terramoo_registry_revision};
""".replace("INITIALIZE_CODE", initialize)

    result = _run_helper_script(offline_moo, tmp_path, body)

    registry = registry_value(result[1])
    assert result[0][0][:2] == [0, "E_INVARG"]
    assert "registry conflict" in result[0][0][2].lower()
    assert "existing" in result[0][0][2].lower()
    assert list(registry) == ["task_b"]
    assert registry.revision == result[2] == 3


def test_endpoint_on_non_exit_only_cas_updates_the_property(offline_moo, tmp_path):
    result = _run_helper_script(offline_moo, tmp_path, """
old = create(#-1);
new = create(#-1);
thing = create(#-1);
add_property(old, "exits", {thing}, {#2, "rw"});
add_property(new, "exits", {}, {#2, "rw"});
add_verb(old, {#2, "xd", "remove_exit"}, {"this", "none", "this"});
set_verb_code(old, "remove_exit", {"this.exits = setremove(this.exits, args[1]);", "return 1;"});
add_verb(new, {#2, "xd", "add_exit"}, {"this", "none", "this"});
set_verb_code(new, "add_exit", {"this.exits = setadd(this.exits, args[1]);", "return 1;"});
add_property(thing, "source", old, {#2, "rw"});
add_property(thing, "_terramoo_generation", "thing-generation", {#2, "r"});
tool.registry = {{"thing"}, {thing}, {"thing-generation"}};
result = tool:tmoo_apply({{"endpoint", "thing", thing, "thing-generation", "source", old, new}});
return {result, thing.source == new, thing in old.exits, thing in new.exits};
""")
    assert result == [[[1, 1]], 1, 1, 0]


def test_unlink_on_non_exit_does_not_call_room_callbacks(offline_moo, tmp_path):
    result = _run_helper_script(offline_moo, tmp_path, """
room = create(#-1);
thing = create(#-1);
add_property(room, "exits", {thing}, {#2, "rw"});
add_verb(room, {#2, "xd", "remove_exit"}, {"this", "none", "this"});
set_verb_code(room, "remove_exit", {"this.exits = setremove(this.exits, args[1]);", "return 1;"});
add_property(thing, "_terramoo_generation", "thing-generation", {#2, "r"});
tool.registry = {{"thing"}, {thing}, {"thing-generation"}};
result = tool:tmoo_apply({{"unlink", "thing", thing, "thing-generation", "exit", room}});
return {result, thing in room.exits};
""")
    assert result == [[[1, 1]], 1]


def test_exit_endpoint_to_scalar_removes_old_membership(offline_moo, tmp_path):
    result = _run_helper_script(offline_moo, tmp_path, """
old = create(#-1);
door = create($exit);
add_property(old, "exits", {door}, {#2, "rw"});
add_verb(old, {#2, "xd", "remove_exit"}, {"this", "none", "this"});
set_verb_code(old, "remove_exit", {"this.exits = setremove(this.exits, args[1]);", "return 1;"});
add_verb(old, {#2, "xd", "add_exit"}, {"this", "none", "this"});
set_verb_code(old, "add_exit", {"this.exits = setadd(this.exits, args[1]);", "return 1;"});
door.source = old;
add_property(door, "_terramoo_generation", "door-generation", {#2, "r"});
tool.registry = {{"door"}, {door}, {"door-generation"}};
result = tool:tmoo_apply({{"endpoint", "door", door, "door-generation", "source", old, "nowhere"}});
return {result, door.source, door in old.exits};
""")
    assert result == [[[1, 1]], "nowhere", 0]


@pytest.mark.parametrize(
    "prop, relation, membership",
    [("source", "exit", "exits"), ("dest", "entrance", "entrances")],
)
def test_exit_to_non_exit_removing_endpoint_unlinks_old_room(
    offline_moo, tmp_path, prop, relation, membership
):
    refs = Refs(
        player=Obj(130),
        registry={"door": Obj(200), "room": Obj(201)},
        sysrefs={"exit": Obj(7), "thing": Obj(5)},
    )
    have = ObjectDef(
        key="door",
        name="Door",
        parent=Ref("$", "exit"),
        props=[PropDef(prop, Ref("@", "room"), defined=False)],
    )
    want = ObjectDef(key="door", name="Door", parent=Ref("$", "thing"))
    planned = plan.diff_object("door", want, have, refs)
    expressions = {
        "chparent": '{"chparent", "door", door, "door-generation", ordinary}',
        "unlink": f'{{"unlink", "door", door, "door-generation", "{relation}", room}}',
        "clearprop": f'{{"clearprop", "door", door, "door-generation", "{prop}"}}',
    }
    helper_ops = ", ".join(expressions[op[0]] for op in planned)
    result = _run_helper_script(offline_moo, tmp_path, f"""
room = create(#-1);
ordinary = create(#-1);
door = create($exit);
add_property(room, "{membership}", {{door}}, {{#2, "rw"}});
add_verb(room, {{#2, "xd", "remove_{relation}"}}, {{"this", "none", "this"}});
set_verb_code(room, "remove_{relation}", {{"this.{membership} = setremove(this.{membership}, args[1]);", "return 1;"}});
door.{prop} = room;
add_property(door, "_terramoo_generation", "door-generation", {{#2, "r"}});
tool.registry = {{{{"door"}}, {{door}}, {{"door-generation"}}}};
result = tool:tmoo_apply({{{helper_ops}}});
return {{result, parent(door) == ordinary, door in room.{membership}}};
""")
    assert [op[0] for op in planned] == ["unlink", "clearprop", "chparent"]
    assert all(item[0] == 1 for item in result[0])
    assert result[1:] == [1, 0]
