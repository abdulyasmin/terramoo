"""Execute the real helper offline with the optional LambdaMOO testbed sources.

NP_SINGLE uses stdin/stdout instead of sockets. Build only in pytest's temporary
directory; never start or modify a running testbed or download missing sources.
"""

import gzip
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from terramoo import plan
from terramoo.moolit import parse, serialize
from terramoo.model import ObjectDef, PropDef
from terramoo.moolit import Obj, Ref
from terramoo.refs import Refs
from terramoo.world import registry_value

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def offline_moo(tmp_path_factory):
    build = ROOT / "testbeds/lambdamoo/.build"
    source = build / "MOO-1.8.1"
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
    helper = (ROOT / "terramoo/helper/tmoo_apply.moo").read_text().splitlines()
    initialize = [
        f'return this.toolbox:tmoo_apply({{{{"register", "{callback_key}", this, "callback-generation"}}}});'
    ]
    script = f""";;
tool = create(#-1);
add_property(tool, "registry", {{{{}}, {{}}, {{}}}}, {{#2, ""}});
add_verb(tool, {{#2, "xd", "tmoo_apply"}}, {{"this", "none", "this"}});
errors = set_verb_code(tool, "tmoo_apply", {serialize(helper)});
if (errors) raise(E_INVARG, toliteral(errors)); endif
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


@pytest.mark.parametrize("callback", ["add", "rebind", "remove", "shift"])
def test_destroy_preserves_recycle_callback_registry_changes(offline_moo, tmp_path, callback):
    binary, database = offline_moo
    helper = (ROOT / "terramoo/helper/tmoo_apply.moo").read_text().splitlines()
    changes = 'this.toolbox.registry = {{"old", "survivor"}, {this, this.survivor}, {"old-generation", "survivor-generation"}};'
    if callback == "rebind":
        changes = 'this.toolbox.registry = {{"OLD"}, {this.survivor}, {"survivor-generation"}};'
    elif callback == "remove":
        changes = 'this.toolbox.registry = {{"survivor"}, {this.survivor}, {"survivor-generation"}};'
    elif callback == "shift":
        changes = 'this.toolbox.registry = {{"old", "survivor"}, {this, this.survivor}, {"old-generation", "survivor-generation"}};'
    recycle = [changes, "return 1;"]
    expected_key = "OLD" if callback == "rebind" else "survivor"
    expected_revision = 1 if callback in ("rebind", "remove") else 2
    initial = '{{"before", "old"}, {after, old}, {"before-generation", "old-generation"}}' if callback == "shift" else '{{"old"}, {old}, {"old-generation"}}'
    script = f""";;
tool = create(#-1);
add_property(tool, "registry", {{{{}}, {{}}, {{}}}}, {{#2, ""}});
add_verb(tool, {{#2, "xd", "tmoo_apply"}}, {{"this", "none", "this"}});
errors = set_verb_code(tool, "tmoo_apply", {serialize(helper)});
if (errors) raise(E_INVARG, toliteral(errors)); endif
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
    helper = (ROOT / "terramoo/helper/tmoo_apply.moo").read_text().splitlines()
    script = f""";;
tool = create(#-1);
add_property(tool, "registry", {{{{}}, {{}}, {{}}}}, {{#2, ""}});
add_verb(tool, {{#2, "xd", "tmoo_apply"}}, {{"this", "none", "this"}});
errors = set_verb_code(tool, "tmoo_apply", {serialize(helper)});
if (errors) raise(E_INVARG, toliteral(errors)); endif
{body}
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
    assert reply[1] != ">>Unknown value<<", result.stdout
    return parse(reply[1])


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


def test_rename_revision_survives_a_legacy_registry_from_recycle(offline_moo, tmp_path):
    recycle = serialize([
        'this.toolbox.registry = {{"other", "doomed"}, {this.target, this}, {"object-generation", "doomed-generation"}};',
        "return 1;",
    ])
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
result = tool:tmoo_apply({{"rename", 1, 1, o, "object-generation", "other"}, {"destroy", "doomed", doomed, "doomed-generation"}, {"rename", 1, 1, o, "object-generation", "stale"}});
return {result, tool.registry[1][1], tool.registry[4]};
""".replace("RECYCLE_CODE", recycle)
    result = _run_helper_script(offline_moo, tmp_path, body)
    assert result[0][0][0] == 1
    assert result[0][1][0] == 1
    assert result[0][2][:2] == [0, "E_INVARG"]
    assert result[1:] == ["other", 3]


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
