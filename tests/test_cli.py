import json
from types import SimpleNamespace

import pytest

from terramoo import cli, moolit, objdef
from terramoo.apply import Outcome
from terramoo.cli import parse_object_arg
from terramoo.errors import MooError
from terramoo.model import ObjectDef, PropDef, VerbDef
from terramoo.moolit import Map, Obj, Ref
from terramoo.plan import Plan
from terramoo.refs import Refs
from terramoo.world import World, registry_value


class AdoptWorld:
    def __init__(self, registry=None, reply=None):
        registry = registry or {}
        self.raw_registry = [
            list(registry),
            list(registry.values()),
            [f"existing-{key}" for key in registry],
        ]
        self.reply = reply
        self.sent = []
        self.saved = []
        self.transport = SimpleNamespace(serialize=moolit.serialize)

    def refs(self):
        return Refs(player=Obj(1), registry=registry_value(self.raw_registry))

    def helper(self, verb, arg):
        return verb, arg

    def eval(self, call):
        self.sent.append(call)
        return self.reply(call) if callable(self.reply) else self.reply

    def read_registry(self):
        return registry_value(self.raw_registry)

    def save_state(self, registry):
        self.saved.append(dict(registry))


@pytest.mark.parametrize("text, obj", [
    ("#123", Obj(123)),
    ("123", Obj(123)),
    ("#048D05-1234567890", Obj("048D05-1234567890")),
])
def test_object_argument(text, obj):
    assert parse_object_arg(text) == obj


@pytest.mark.parametrize("text", ["hall", "#", "#12x", "$room"])
def test_bad_object_argument_is_a_moo_error(text):
    with pytest.raises(MooError):
        parse_object_arg(text)


def test_apply_confirmation_refusal_sends_nothing(monkeypatch, capsys):
    world = SimpleNamespace()
    refs = Refs(player=Obj(1), registry={"hall": Obj(2)})
    pending = Plan(ops=[("name", Ref("@", "hall"), "Great Hall")])
    monkeypatch.setattr(cli, "_world", lambda args: world)
    monkeypatch.setattr(cli, "_plan", lambda w: (refs, {}, pending))
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    monkeypatch.setattr(cli.apply_mod, "run", lambda *args, **kwargs: pytest.fail("apply ran"))

    cli.cmd_apply(SimpleNamespace(yes=False, destroy=True))
    assert capsys.readouterr().out.endswith("aborted\n")


def test_confirmed_apply_forwards_the_destroy_flag(monkeypatch, capsys):
    world = SimpleNamespace()
    refs = Refs(player=Obj(1), registry={"hall": Obj(2)})
    pending = Plan(ops=[("name", Ref("@", "hall"), "Great Hall")], destroys={"old": Obj(3)})
    seen = {}
    monkeypatch.setattr(cli, "_world", lambda args: world)
    monkeypatch.setattr(cli, "_plan", lambda w: (refs, {"hall": object()}, pending))

    def run(w, p, r, *, files, destroy):
        seen.update(world=w, plan=p, refs=r, files=files, destroy=destroy)
        return Outcome(done=["name @hall Great Hall", "recycle old"])

    monkeypatch.setattr(cli.apply_mod, "run", run)
    cli.cmd_apply(SimpleNamespace(yes=True, destroy=True))

    assert seen["destroy"] is True
    assert seen["plan"] is pending
    assert capsys.readouterr().out.endswith("applied 2 op(s)\n")


def test_apply_problems_abort_before_confirmation_or_mutation(monkeypatch, capsys):
    world = SimpleNamespace(save_state=lambda registry: pytest.fail("state was saved"))
    refs = Refs(player=Obj(1), registry={"hall": Obj(2)})
    unsafe = Plan(
        destroys={"old": Obj(3)},
        problems=["hall: refers to @missing, which has no file"],
    )
    monkeypatch.setattr(cli, "_world", lambda args: world)
    monkeypatch.setattr(cli, "_plan", lambda w: (refs, {}, unsafe))
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked for confirmation"))
    monkeypatch.setattr(cli.apply_mod, "run", lambda *args, **kwargs: pytest.fail("apply ran"))

    with pytest.raises(MooError, match="fix the problems above first"):
        cli.cmd_apply(SimpleNamespace(yes=True, destroy=True))

    assert "! hall: refers to @missing, which has no file" in capsys.readouterr().out


def test_pull_rejects_unknown_keys_before_export_or_state_write(monkeypatch):
    world = SimpleNamespace(
        refs=lambda: Refs(player=Obj(1), registry={"hall": Obj(2)}),
        save_state=lambda registry: pytest.fail("state was saved"),
    )
    monkeypatch.setattr(cli, "_world", lambda args: world)
    monkeypatch.setattr(cli, "_write_exports", lambda *args: pytest.fail("export ran"))

    with pytest.raises(MooError, match="not in the registry: missing"):
        cli.cmd_pull(SimpleNamespace(keys=["hall", "missing"]))


def test_plan_exports_the_case_matching_registry_binding(monkeypatch):
    refs = Refs(player=Obj(1), registry={"Hall": Obj(10)})
    file_obj = ObjectDef(key="hall", name="Hall", parent=Obj(2))
    live_obj = ObjectDef(key="Hall", name="Hall", parent=Obj(2))
    world = SimpleNamespace(refs=lambda: refs, load_files=lambda: {"hall": file_obj})

    def fake_export(actual_world, actual_refs, keys):
        assert (actual_world, actual_refs, keys) == (world, refs, ["Hall"])
        return {"Hall": live_obj}

    monkeypatch.setattr(cli.export_mod, "export", fake_export)

    _, _, pending = cli._plan(world)

    assert pending.creates == []
    assert pending.destroys == {}
    assert pending.unchanged == ["hall"]


def test_status_reports_each_legacy_registry_key_with_rename_command(monkeypatch, capsys):
    refs = Refs(
        player=Obj(1),
        registry=registry_value([["bad-key", "مرحبا"], [Obj(10), Obj(11)]])
    )
    world = SimpleNamespace(
        name="test",
        describe=lambda: "fake://test",
        player_name="alice",
        toolbox=Obj(99),
        refs=lambda: refs,
        load_files=lambda: {},
        server_info=lambda: ([], "FakeMOO"),
    )
    monkeypatch.setattr(cli, "_world", lambda args: world)

    cli.cmd_status(SimpleNamespace())

    output = capsys.readouterr().out
    assert "bad-key" in output and "tmoo rename-key 'bad-key' NEW" in output
    assert "مرحبا" in output and "tmoo rename-key 'مرحبا' NEW" in output


def test_rename_key_atomically_moves_a_legacy_binding(tmp_path, monkeypatch):
    world, raw_registry, sent = _rename_world(tmp_path, monkeypatch, old="bad-key")
    monkeypatch.setattr(cli, "_world", lambda args: world)

    cli.cmd_rename_key(SimpleNamespace(old="bad-key", new="good_key"))

    assert moolit.parse(sent[0]) == [
        ["rename", 1, 0, Obj(10), "generation-10", "good_key"]
    ]
    assert raw_registry[0] == ["good_key"]
    assert json.loads(world.state_path.read_text())["registry"] == {"good_key": 10}


def test_rename_key_renames_an_existing_local_object_file(tmp_path, monkeypatch):
    world, raw_registry, sent = _rename_world(tmp_path, monkeypatch)
    world.write_file(ObjectDef(key="old_key", name="Old", parent=Obj(2)))
    monkeypatch.setattr(cli, "_world", lambda args: world)

    cli.cmd_rename_key(SimpleNamespace(old="old_key", new="new_key"))

    assert moolit.parse(sent[0]) == [
        ["rename", 1, 0, Obj(10), "generation-10", "new_key"]
    ]
    assert not world.file_for("old_key").exists()
    assert objdef.parse(world.file_for("new_key").read_text()).key == "new_key"
    assert json.loads(world.state_path.read_text())["registry"] == {"new_key": 10}


def _rename_world(tmp_path, monkeypatch, old="old_key", *, fail_reverse=False, after_rename=None):
    world = World("test", tmp_path, "alice", {})
    world.objects_dir.mkdir(parents=True)
    world._player = Obj(1)
    world._toolbox = Obj(99)
    world._transport = SimpleNamespace(serialize=moolit.serialize)
    raw_registry = [[old], [Obj(10)], ["generation-10"], 0]
    sent = []
    monkeypatch.setattr(world, "refs", lambda: Refs(player=Obj(1), registry=registry_value(raw_registry)))
    monkeypatch.setattr(world, "require_helper_version", lambda: None)
    monkeypatch.setattr(world, "helper", lambda verb, arg: (verb, arg))

    def rename(call):
        text = call[1]
        sent.append(text)
        op = moolit.parse(text)[0]
        if len(op) == 5:
            selector, expected, nonce, new = op[1:]
            expected_revision = None
        else:
            selector, expected_revision, expected, nonce, new = op[1:]
        if isinstance(selector, int):
            index = selector - 1
        else:  # the pre-fix operation names the old key directly
            index = next(i for i, key in enumerate(raw_registry[0]) if key.lower() == selector.lower())
        if fail_reverse and len(sent) > 1:
            return [[0, "E_PERM", "reverse refused"]]
        assert raw_registry[1][index] == expected
        assert raw_registry[2][index] == nonce
        if expected_revision is not None and expected_revision != raw_registry[3]:
            return [[0, "E_INVARG", "registry key changed before rename; replan"]]
        raw_registry[0][index] = new
        raw_registry[3] += 1
        if after_rename is not None:
            after_rename(world, raw_registry, len(sent))
        return [[1, expected]]

    monkeypatch.setattr(world, "eval", rename)
    monkeypatch.setattr(world, "read_registry", lambda: registry_value(raw_registry))
    return world, raw_registry, sent


def test_rename_key_rewrites_every_parsed_reference(tmp_path, monkeypatch):
    world, raw_registry, _ = _rename_world(tmp_path, monkeypatch)
    world.write_file(ObjectDef(key="old_key", name="Old", parent=Obj(2)))
    world.write_file(ObjectDef(
        key="hall",
        name="Hall",
        parent=Ref("@", "OLD_KEY"),
        location=Ref("@", "old_key"),
        owner=Ref("@", "old_key"),
        props=[
            PropDef(
                "links",
                Map({Ref("@", "old_key"): [Ref("@", "OLD_KEY")]}),
                owner=Ref("@", "old_key"),
            ),
        ],
        verbs=[VerbDef("look", ["return 1;"], owner=Ref("@", "old_key"))],
    ))
    monkeypatch.setattr(cli, "_world", lambda args: world)

    cli.cmd_rename_key(SimpleNamespace(old="old_key", new="new_key"))

    assert raw_registry[0] == ["new_key"]
    assert not world.file_for("old_key").exists()
    renamed = objdef.parse(world.file_for("new_key").read_text())
    hall = objdef.parse(world.file_for("hall").read_text())
    assert renamed.key == "new_key"
    assert hall.parent == Ref("@", "new_key")
    assert hall.location == Ref("@", "new_key")
    assert hall.owner == Ref("@", "new_key")
    assert hall.props[0].owner == Ref("@", "new_key")
    assert hall.props[0].value == Map({Ref("@", "new_key"): [Ref("@", "new_key")]})
    assert hall.verbs[0].owner == Ref("@", "new_key")


def test_rename_key_reverses_remote_change_when_local_finalization_fails(tmp_path, monkeypatch):
    world, raw_registry, sent = _rename_world(tmp_path, monkeypatch)
    world.write_file(ObjectDef(key="old_key", name="Old", parent=Obj(2)))
    world.state_path.write_text("old state\n")
    original_replace = type(world.state_path).replace

    def fail_state_replace(path, target):
        if target == world.state_path and ".new." in path.name:
            raise OSError("disk full")
        return original_replace(path, target)

    monkeypatch.setattr(type(world.state_path), "replace", fail_state_replace)
    monkeypatch.setattr(cli, "_world", lambda args: world)

    with pytest.raises(MooError, match="local rename failed.*remote rename was reversed"):
        cli.cmd_rename_key(SimpleNamespace(old="old_key", new="new_key"))

    assert len(sent) == 2
    assert raw_registry[0] == ["old_key"]
    assert world.file_for("old_key").exists()
    assert not world.file_for("new_key").exists()
    assert world.state_path.read_text() == "old state\n"


def test_rename_key_journals_a_failed_remote_reverse(tmp_path, monkeypatch):
    world, raw_registry, sent = _rename_world(tmp_path, monkeypatch, fail_reverse=True)
    world.write_file(ObjectDef(key="old_key", name="Old", parent=Obj(2)))
    original_replace = type(world.state_path).replace

    def fail_state_replace(path, target):
        if target == world.state_path:
            raise OSError("disk full")
        return original_replace(path, target)

    monkeypatch.setattr(type(world.state_path), "replace", fail_state_replace)
    monkeypatch.setattr(cli, "_world", lambda args: world)

    with pytest.raises(MooError, match="recovery journal"):
        cli.cmd_rename_key(SimpleNamespace(old="old_key", new="new_key"))

    journal = world.state_path.with_name("state.json.rename-recovery.json")
    assert len(sent) == 2
    assert raw_registry[0] == ["new_key"]
    assert journal.exists()
    assert "tmoo rename-key new_key old_key" in journal.read_text()


def test_rename_key_addresses_non_ascii_legacy_key_by_registry_index(tmp_path, monkeypatch):
    world, raw_registry, sent = _rename_world(tmp_path, monkeypatch, old="مرحبا")
    monkeypatch.setattr(cli, "_world", lambda args: world)

    cli.cmd_rename_key(SimpleNamespace(old="مرحبا", new="hello"))

    assert raw_registry[0] == ["hello"]
    assert sent[0].isascii()
    assert moolit.parse(sent[0]) == [
        ["rename", 1, 0, Obj(10), "generation-10", "hello"],
    ]


def test_rename_key_rejects_an_interleaved_remote_rename(tmp_path, monkeypatch):
    changed = False

    def interleave(_world, raw_registry, call_number):
        nonlocal changed
        if call_number == 1 and not changed:
            changed = True
            raw_registry[0][0] = "other"
            raw_registry[3] += 1

    world, raw_registry, _ = _rename_world(tmp_path, monkeypatch, after_rename=interleave)
    # Simulate process B changing only the key immediately before process A's CAS.
    original_eval = world.eval

    def rename_after_interleave(call):
        if not changed:
            raw_registry[0][0] = "other"
            raw_registry[3] += 1
        return original_eval(call)

    monkeypatch.setattr(world, "eval", rename_after_interleave)
    monkeypatch.setattr(cli, "_world", lambda args: world)

    with pytest.raises(MooError):
        cli.cmd_rename_key(SimpleNamespace(old="old_key", new="new_key"))

    assert raw_registry[0] == ["other"]


def test_rename_key_finalizes_after_a_lost_success_response(tmp_path, monkeypatch):
    journal_was_durable = []

    def lose_response(world, _raw_registry, call_number):
        if call_number == 1:
            journal_was_durable.append(
                world.state_path.with_name("state.json.rename-recovery.json").exists()
            )
            raise MooError("connection lost after write")

    world, raw_registry, _ = _rename_world(tmp_path, monkeypatch, after_rename=lose_response)
    world.write_file(ObjectDef(key="old_key", name="Old", parent=Obj(2)))
    monkeypatch.setattr(cli, "_world", lambda args: world)

    cli.cmd_rename_key(SimpleNamespace(old="old_key", new="new_key"))

    assert raw_registry[0] == ["new_key"]
    assert journal_was_durable == [True]
    assert not world.file_for("old_key").exists()
    assert objdef.parse(world.file_for("new_key").read_text()).key == "new_key"
    assert not world.state_path.with_name("state.json.rename-recovery.json").exists()


def test_rename_key_preserves_an_edit_made_after_parsing(tmp_path, monkeypatch):
    edited = ObjectDef(key="old_key", name="Edited concurrently", parent=Obj(2))

    def edit_source(world, _raw_registry, call_number):
        if call_number == 1:
            world.file_for("old_key").write_text(objdef.render(edited))

    world, raw_registry, _ = _rename_world(tmp_path, monkeypatch, after_rename=edit_source)
    world.write_file(ObjectDef(key="old_key", name="Original", parent=Obj(2)))
    monkeypatch.setattr(cli, "_world", lambda args: world)

    with pytest.raises(MooError, match="changed after it was read.*recovery journal"):
        cli.cmd_rename_key(SimpleNamespace(old="old_key", new="new_key"))

    assert raw_registry[0] == ["old_key"]
    assert objdef.parse(world.file_for("old_key").read_text()).name == "Edited concurrently"
    assert world.state_path.with_name("state.json.rename-recovery.json").exists()


def test_rename_key_holds_a_per_world_command_lock(tmp_path, monkeypatch):
    world, raw_registry, sent = _rename_world(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_world", lambda args: world)

    with cli._rename_lock(world):
        with pytest.raises(MooError, match="another rename-key command"):
            cli.cmd_rename_key(SimpleNamespace(old="old_key", new="new_key"))

    assert sent == []
    assert raw_registry[0] == ["old_key"]


def test_rename_key_rejects_case_folded_destination_file_collision(tmp_path, monkeypatch):
    world, raw_registry, sent = _rename_world(tmp_path, monkeypatch)
    world.write_file(ObjectDef(key="old_key", name="Old", parent=Obj(2)))
    world.write_file(ObjectDef(key="NEW_KEY", name="Unrelated", parent=Obj(2)))
    original_exists = type(world.state_path).exists

    def case_sensitive_exists(path):
        if path == world.objects_dir / "new_key.moo":
            return False
        return original_exists(path)

    monkeypatch.setattr(type(world.state_path), "exists", case_sensitive_exists)
    monkeypatch.setattr(cli, "_world", lambda args: world)

    with pytest.raises(MooError, match="NEW_KEY.*differs only in case"):
        cli.cmd_rename_key(SimpleNamespace(old="old_key", new="new_key"))

    assert sent == []
    assert raw_registry[0] == ["old_key"]


def test_rename_key_allows_its_source_file_in_a_case_only_rename(tmp_path, monkeypatch):
    world, raw_registry, sent = _rename_world(tmp_path, monkeypatch)
    world.write_file(ObjectDef(key="old_key", name="Old", parent=Obj(2)))
    monkeypatch.setattr(cli, "_world", lambda args: world)

    cli.cmd_rename_key(SimpleNamespace(old="old_key", new="OLD_KEY"))

    assert sent
    assert raw_registry[0] == ["OLD_KEY"]
    assert objdef.parse(world.file_for("OLD_KEY").read_text()).key == "OLD_KEY"


def test_pull_skips_legacy_keys_without_building_a_file_path(monkeypatch, capsys):
    refs = Refs(
        player=Obj(1),
        registry=registry_value([["hall", "../outside"], [Obj(10), Obj(11)]])
    )
    world = SimpleNamespace(
        objects_dir=SimpleNamespace(glob=lambda pattern: []),
        file_for=lambda key: pytest.fail(f"built a path for {key}"),
    )
    monkeypatch.setattr(cli.export_mod, "export", lambda w, r, keys: {})

    assert cli._write_exports(world, refs, ["../outside"]) == 0
    assert "tmoo rename-key '../outside' NEW" in capsys.readouterr().err


def test_destroy_rejects_case_colliding_registry_before_any_mutation(monkeypatch):
    world = SimpleNamespace(
        refs=lambda: registry_value([["hall", "Hall"], [Obj(10), Obj(11)]]),
        load_files=lambda: pytest.fail("files loaded after malformed registry"),
    )
    monkeypatch.setattr(cli, "_world", lambda args: world)
    monkeypatch.setattr(cli.apply_mod, "run", lambda *args, **kwargs: pytest.fail("apply mutated the MOO"))

    with pytest.raises(MooError, match="toolbox has a malformed registry"):
        cli.cmd_apply(SimpleNamespace(yes=True, destroy=True))


def test_adopt_rejects_managed_object_before_sending_or_persisting(monkeypatch):
    class AdoptWorld:
        def __init__(self):
            self.raw_registry = [["hall"], [Obj(10)]]
            self.sent = []
            self.saved = []
            self.transport = SimpleNamespace(serialize=moolit.serialize)

        def refs(self):
            return Refs(player=Obj(1), registry=registry_value(self.raw_registry))

        def helper(self, verb, arg):
            return verb, arg

        def eval(self, call):
            self.sent.append(call)
            _, arg = call
            for _, key, obj in moolit.parse(arg):
                self.raw_registry[0].append(key)
                self.raw_registry[1].append(obj)
            return [[1, Obj(10)]]

        def read_registry(self):
            return registry_value(self.raw_registry)

        def save_state(self, registry):
            self.saved.append(dict(registry))

    world = AdoptWorld()
    monkeypatch.setattr(cli, "_world", lambda args: world)

    with pytest.raises(MooError, match=r"#10 is already managed as hall"):
        cli.cmd_adopt(SimpleNamespace(owned=False, object="#10", key="door"))

    assert world.sent == []
    assert world.raw_registry == [["hall"], [Obj(10)]]
    assert world.saved == []


@pytest.mark.parametrize("key", ["../outside", "/absolute", "café", "bad-key"])
def test_adopt_rejects_non_identifier_keys_before_registration(monkeypatch, key):
    world = AdoptWorld()
    monkeypatch.setattr(cli, "_world", lambda args: world)

    with pytest.raises(MooError, match="ASCII identifier"):
        cli.cmd_adopt(SimpleNamespace(owned=False, object="#10", key=key, verify=False))

    assert world.sent == []
    assert world.saved == []


def test_adopt_owned_avoids_case_colliding_registry_keys(tmp_path, monkeypatch):
    world = AdoptWorld({"Hall": Obj(10)})
    world.objects_dir = tmp_path
    world.toolbox = Obj(99)
    world.server_info = lambda: ([Obj(10), Obj(11)], "LambdaMOO")
    world.names = lambda objects: ["Hall"]

    def register(call):
        _, arg = call
        results = []
        for _, key, obj, nonce in moolit.parse(arg):
            existing = next((i for i, old in enumerate(world.raw_registry[0]) if old.lower() == key.lower()), None)
            if existing is None:
                world.raw_registry[0].append(key)
                world.raw_registry[1].append(obj)
                world.raw_registry[2].append(nonce)
            else:
                world.raw_registry[1][existing] = obj
                world.raw_registry[2][existing] = nonce
            results.append([1, obj])
        return results

    world.reply = register
    exported = []
    monkeypatch.setattr(cli, "_world", lambda args: world)
    monkeypatch.setattr(cli, "_write_exports", lambda w, refs, keys: exported.extend(keys) or len(keys))

    cli.cmd_adopt(SimpleNamespace(owned=True, object=None, key=None))

    assert world.raw_registry[:2] == [["Hall", "hall_2"], [Obj(10), Obj(11)]]
    assert world.raw_registry[2][0] == "existing-Hall"
    assert world.raw_registry[2][1]
    assert exported == ["hall_2"]
    assert world.saved == [{"Hall": Obj(10), "hall_2": Obj(11)}]


@pytest.mark.parametrize("key", ["hall", "HALL"])
def test_adopt_refuses_to_rebind_an_occupied_key(monkeypatch, key):
    world = AdoptWorld({"hall": Obj(10)})
    monkeypatch.setattr(cli, "_world", lambda args: world)
    monkeypatch.setattr(cli, "_write_exports", lambda *args: pytest.fail("occupied key was exported"))

    with pytest.raises(MooError, match=rf"{key} is already #10"):
        cli.cmd_adopt(SimpleNamespace(owned=False, object="#11", key=key))

    assert world.sent == []
    assert world.raw_registry == [["hall"], [Obj(10)], ["existing-hall"]]
    assert world.saved == []


def test_adopt_failed_registration_exports_nothing_and_raises(monkeypatch):
    world = AdoptWorld()

    def reject(call):
        world.raw_registry = [["Mine"], [Obj(11)], ["other-generation"]]
        return [[0, "E_INVARG", "object #10 is already registered as other"]]

    world.reply = reject
    exported = []
    monkeypatch.setattr(cli, "_world", lambda args: world)
    monkeypatch.setattr(cli, "_write_exports", lambda w, refs, keys: exported.extend(keys) or len(keys))

    with pytest.raises(MooError, match="adoption failed"):
        cli.cmd_adopt(SimpleNamespace(owned=False, object="#10", key="mine"))

    assert exported == []
    assert world.saved == [{"Mine": Obj(11)}]


def test_adopt_exports_the_registry_spelling_after_a_success(monkeypatch):
    world = AdoptWorld()

    def succeed(call):
        _, arg = call
        _, _, obj, nonce = moolit.parse(arg)[0]
        world.raw_registry = [["Mine"], [obj], [nonce]]
        return [[1, Obj(10)]]

    world.reply = succeed
    exported = []
    monkeypatch.setattr(cli, "_world", lambda args: world)
    monkeypatch.setattr(cli, "_write_exports", lambda w, refs, keys: exported.extend(keys) or len(keys))

    cli.cmd_adopt(SimpleNamespace(owned=False, object="#10", key="mine"))

    assert exported == ["Mine"]
    assert world.saved == [{"Mine": Obj(10)}]


def test_adopt_verify_stamps_an_existing_legacy_binding(monkeypatch):
    world = AdoptWorld()
    world.raw_registry = [["hall"], [Obj(10)]]

    def verify(call):
        _, arg = call
        _, key, obj, nonce = moolit.parse(arg)[0]
        world.raw_registry = [[key], [obj], [nonce]]
        return [[1, obj]]

    world.reply = verify
    exported = []
    monkeypatch.setattr(cli, "_world", lambda args: world)
    monkeypatch.setattr(cli, "_write_exports", lambda w, refs, keys: exported.extend(keys) or len(keys))

    cli.cmd_adopt(SimpleNamespace(owned=False, object="#10", key="hall", verify=True))

    assert world.raw_registry[0:2] == [["hall"], [Obj(10)]]
    assert world.raw_registry[2][0]
    assert exported == ["hall"]


@pytest.mark.parametrize("reply", [7, [], [[1]]])
def test_adopt_rejects_malformed_helper_results(reply, monkeypatch):
    world = AdoptWorld(reply=reply)
    monkeypatch.setattr(cli, "_world", lambda args: world)
    monkeypatch.setattr(cli, "_write_exports", lambda *args: pytest.fail("malformed registration was exported"))

    with pytest.raises(MooError, match="adoption failed"):
        cli.cmd_adopt(SimpleNamespace(owned=False, object="#10", key="mine"))

    assert world.saved == [{}]


def test_pull_keeps_existing_property_order_when_rewriting_a_file(tmp_path, monkeypatch):
    w = World("test", tmp_path, "alice", {})
    w.objects_dir.mkdir(parents=True)
    existing = ObjectDef(
        key="hall",
        name="Hall",
        parent=Ref("$", "room"),
        props=[PropDef("a", 0), PropDef("c", 0)],
    )
    w.file_for("hall").write_text(objdef.render(existing))
    live = ObjectDef(
        key="hall",
        name="Hall",
        parent=Ref("$", "room"),
        props=[PropDef("c", 3), PropDef("new", 2), PropDef("a", 1)],
    )
    monkeypatch.setattr(cli.export_mod, "export", lambda world, refs, keys: {"hall": live})

    assert cli._write_exports(w, SimpleNamespace(registry={"hall": Obj(2)}), ["hall"]) == 1
    written = objdef.parse(w.file_for("hall").read_text())
    assert [prop.name for prop in written.props] == ["a", "c", "new"]
