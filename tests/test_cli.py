from types import SimpleNamespace

import pytest

from terramoo import cli, objdef
from terramoo.apply import Outcome
from terramoo.cli import parse_object_arg
from terramoo.errors import MooError
from terramoo.model import ObjectDef, PropDef
from terramoo.moolit import Obj, Ref
from terramoo.plan import Plan
from terramoo.refs import Refs
from terramoo.world import World, registry_value


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


def test_destroy_rejects_case_colliding_registry_before_any_mutation(monkeypatch):
    world = SimpleNamespace(
        refs=lambda: registry_value([["hall", "Hall"], [Obj(10), Obj(11)]]),
        load_files=lambda: pytest.fail("files loaded after malformed registry"),
    )
    monkeypatch.setattr(cli, "_world", lambda args: world)
    monkeypatch.setattr(cli.apply_mod, "run", lambda *args, **kwargs: pytest.fail("apply mutated the MOO"))

    with pytest.raises(MooError, match="toolbox has a malformed registry"):
        cli.cmd_apply(SimpleNamespace(yes=True, destroy=True))


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
