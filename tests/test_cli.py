from pathlib import Path
from types import SimpleNamespace

import pytest

from terramoo import cli, moolit, objdef
from terramoo.apply import Outcome
from terramoo.cli import parse_object_arg
from terramoo.errors import MooError
from terramoo.model import ObjectDef, PropDef
from terramoo.moolit import Obj, Ref
from terramoo.plan import Plan
from terramoo.refs import Refs
from terramoo.world import World, registry_value


class AdoptWorld:
    def __init__(self, registry=None, reply=None):
        registry = registry or {}
        self.raw_registry = [list(registry), list(registry.values())]
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
    pending = Plan(ops=[("name", Ref("@", "hall"), "Great Hall")], destroys=["old"])
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
        destroys=["old"],
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


def test_adopt_owned_avoids_case_colliding_registry_keys(tmp_path, monkeypatch):
    world = AdoptWorld({"Hall": Obj(10)})
    world.objects_dir = tmp_path
    world.toolbox = Obj(99)
    world.server_info = lambda: ([Obj(10), Obj(11)], "LambdaMOO")
    world.names = lambda objects: ["Hall"]

    def register(call):
        _, arg = call
        results = []
        for _, key, obj in moolit.parse(arg):
            existing = next((i for i, old in enumerate(world.raw_registry[0]) if old.lower() == key.lower()), None)
            if existing is None:
                world.raw_registry[0].append(key)
                world.raw_registry[1].append(obj)
            else:
                world.raw_registry[1][existing] = obj
            results.append([1, obj])
        return results

    world.reply = register
    exported = []
    monkeypatch.setattr(cli, "_world", lambda args: world)
    monkeypatch.setattr(cli, "_write_exports", lambda w, refs, keys: exported.extend(keys) or len(keys))

    cli.cmd_adopt(SimpleNamespace(owned=True, object=None, key=None))

    assert world.raw_registry == [["Hall", "hall_2"], [Obj(10), Obj(11)]]
    assert exported == ["hall_2"]
    assert world.saved == [{"Hall": Obj(10), "hall_2": Obj(11)}]


def test_register_helper_refuses_to_rebind_an_occupied_key():
    source = (Path(__file__).parents[1] / "terramoo/helper/tmoo_apply.moo").read_text()

    assert 'if (i && reg[2][i] != r)' in source


def test_adopt_failed_registration_exports_nothing_and_raises(monkeypatch):
    world = AdoptWorld()

    def reject(call):
        world.raw_registry = [["Mine"], [Obj(11)]]
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
        world.raw_registry = [["Mine"], [Obj(10)]]
        return [[1, Obj(10)]]

    world.reply = succeed
    exported = []
    monkeypatch.setattr(cli, "_world", lambda args: world)
    monkeypatch.setattr(cli, "_write_exports", lambda w, refs, keys: exported.extend(keys) or len(keys))

    cli.cmd_adopt(SimpleNamespace(owned=False, object="#10", key="mine"))

    assert exported == ["Mine"]
    assert world.saved == [{"Mine": Obj(10)}]


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
