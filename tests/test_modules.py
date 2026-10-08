from pathlib import Path

import pytest

from terramoo import cli, objdef, plan
from terramoo.catalog import discover
from terramoo.errors import MooError
from terramoo.model import ObjectDef, PropDef
from terramoo.modules import Modules
from terramoo.moolit import Obj, Ref
from terramoo.refs import Refs
from terramoo.storage import toml_text
from terramoo.world import World


def module(w, directory, name, **fields):
    path = w.objects_dir / directory / "module.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(toml_text({"schema_version": 1, "name": name, **fields}))


def graph(w):
    return Modules.build(discover(w.objects_dir), w.load_files())


def test_nearest_manifest_owns_nested_objects(tmp_path):
    w = World("test", tmp_path, "alice", {})
    module(w, "town", "town")
    module(w, "town/shops", "shops")
    w.write_file(ObjectDef("hall", "Hall", Obj(0)), into="town/buildings")
    w.write_file(ObjectDef("store", "Store", Obj(0)), into="town/shops")
    w.write_file(ObjectDef("loose", "Loose", Obj(0)))
    g = graph(w)
    assert g.membership == {"hall": "local/town", "store": "local/shops", "loose": None}
    assert g.select(["town"], with_deps=True) == {"local/town"}


def test_reciprocal_refs_are_a_single_execution_group(tmp_path):
    w = World("test", tmp_path, "alice", {})
    module(w, "a", "a", references=["b"], depends_on=["base"])
    module(w, "b", "b", references=["a"])
    module(w, "base", "base")
    w.write_file(ObjectDef("a", "A", Obj(0), props=[PropDef("peer", Ref("@", "b"))]), into="a")
    w.write_file(ObjectDef("b", "B", Obj(0), props=[PropDef("peer", Ref("@", "a"))]), into="b")
    g = graph(w)
    assert g.groups == [("local/base",), ("local/a", "local/b")]
    assert g.select(["a"], with_deps=True) == {"local/a", "local/b", "local/base"}
    module(w, "b", "b", depends_on=["a"])
    with pytest.raises(MooError, match="unsatisfiable initialization cycle"):
        graph(w)


def test_parent_cycles_are_rejected_even_when_in_one_module(tmp_path):
    w = World("test", tmp_path, "alice", {})
    module(w, "a", "a")
    w.write_file(ObjectDef("a", "A", Ref("@", "b")), into="a")
    w.write_file(ObjectDef("b", "B", Ref("@", "a")), into="a")
    with pytest.raises(MooError, match="parent chain"):
        graph(w)


def test_module_reference_to_loose_file_is_explained(tmp_path):
    w = World("test", tmp_path, "alice", {})
    module(w, "a", "a")
    w.write_file(ObjectDef("a", "A", Ref("@", "b")), into="a")
    w.write_file(ObjectDef("b", "B", Obj(0)))
    with pytest.raises(MooError, match="needs a module"):
        graph(w)


def test_scoped_plan_retains_context_and_cannot_orphan_unrelated_objects():
    files = {"a": ObjectDef("a", "A", Ref("@", "b")), "b": ObjectDef("b", "B", Obj(0))}
    refs = Refs(Obj(1), {"a": Obj(10), "b": Obj(11), "old": Obj(12)})
    live = {**files, "a": ObjectDef("a", "old name", Ref("@", "b"))}
    p = plan.build(files, live, refs, selected={"a"})
    assert not p.problems and not p.destroys
    assert [op for op in p.ops if op[0] == "link"] == [("link", [Ref("@", "a")])]
    assert all(op[1] == Ref("@", "a") for op in p.ops if op[0] != "link")


def test_check_and_modules_do_not_connect(tmp_path, monkeypatch, capsys):
    w = World("test", tmp_path, "alice", {})
    module(w, "a", "a")
    (w.dir / "world.toml").write_text('player = "alice"\n')
    w.write_file(ObjectDef("a", "A", Obj(0)), into="a")
    monkeypatch.setenv("TMOO_ROOT", str(tmp_path))
    monkeypatch.setattr(World, "refs", lambda self: pytest.fail("connected"))
    cli.main(["check"])
    cli.main(["modules"])
    assert "checked 1 objects, 1 modules" in capsys.readouterr().out
