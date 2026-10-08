from types import SimpleNamespace

import pytest

from terramoo import cli, objdef, plan
from terramoo.catalog import discover
from terramoo.errors import MooError
from terramoo.model import ObjectDef
from terramoo.moolit import Obj, Ref
from terramoo.refs import Refs
from terramoo.world import World


def world(tmp_path):
    w = World("test", tmp_path, "alice", {})
    w.objects_dir.mkdir(parents=True)
    return w


def test_folder_move_changes_neither_identity_nor_plan(tmp_path):
    w = world(tmp_path)
    obj = ObjectDef("hall", "Hall", Obj(2))
    original = w.write_file(obj)
    nested = w.objects_dir / "town" / ".buildings" / original.name
    nested.parent.mkdir(parents=True)
    original.rename(nested)
    refs = Refs(Obj(1), {"hall": Obj(10)})
    assert w.file_for("hall") == nested
    assert plan.build(w.load_files(), {"hall": obj}, refs).empty


def test_pull_repairs_a_malformed_nested_file_without_flattening(tmp_path, monkeypatch):
    w = world(tmp_path)
    obj = ObjectDef("hall", "Hall", Ref("$", "room"))
    path = w.write_file(obj, into="town/buildings")
    path.write_text("malformed existing definition\n")
    refs = Refs(Obj(1), {"hall": Obj(10)})
    monkeypatch.setattr(cli.export_mod, "export", lambda *args: {"hall": obj})
    assert cli._write_exports(w, refs, ["hall"]) == 1
    assert objdef.parse(path.read_text()) == obj
    assert not (w.objects_dir / "hall.moo").exists()


def test_discovery_reports_both_duplicate_paths(tmp_path):
    w = world(tmp_path)
    obj = ObjectDef("hall", "Hall", Obj(2))
    first = w.write_file(obj)
    second = w.objects_dir / "nested" / first.name
    second.parent.mkdir()
    second.write_bytes(first.read_bytes())
    with pytest.raises(MooError) as exc:
        w.load_files()
    assert str(first) in str(exc.value) and str(second) in str(exc.value)
    with pytest.raises(MooError, match="duplicate keys"):
        w.file_for("hall")


@pytest.mark.parametrize("kind", ["file", "directory", "broken"])
def test_discovery_rejects_symlinks(tmp_path, kind):
    w = world(tmp_path)
    target = tmp_path / "target"
    if kind == "file":
        target.write_text("data")
    elif kind == "directory":
        target.mkdir()
    (w.objects_dir / "linked").symlink_to(target)
    with pytest.raises(MooError, match="symlink"):
        w.load_files()


def test_missing_objects_root_is_not_an_empty_world(tmp_path):
    w = World("test", tmp_path, "alice", {})
    with pytest.raises(MooError, match="cannot read objects directory"):
        w.load_files()


@pytest.mark.parametrize("into", ["../escape", "/tmp/escape"])
def test_new_destination_must_be_inside_objects(tmp_path, into):
    w = world(tmp_path)
    with pytest.raises(MooError, match="relative to objects"):
        w.write_file(ObjectDef("hall", "Hall", Obj(2)), into=into)
    assert discover(w.objects_dir).objects == ()


def test_adopt_preflights_destination_before_remote_registration(tmp_path, monkeypatch):
    w = world(tmp_path)
    monkeypatch.setattr(w, "refs", lambda: Refs(Obj(1)))
    monkeypatch.setattr(w, "eval", lambda *args: pytest.fail("remote registration attempted"))
    with pytest.raises(MooError, match="relative to objects"):
        cli._adopt_locked(w, SimpleNamespace(owned=False, verify=False, object="#10", key="hall", into="../escape"))


def test_recovery_will_not_clean_up_an_unrelated_temporary_file(tmp_path):
    w = world(tmp_path)
    path = w.objects_dir / ".notes.tmp"
    path.write_text("user content")
    with pytest.raises(MooError, match="unsafe temporary-file path"):
        cli._recovery_temp_paths(w, {"schema_version": 1, "preserved_temporary_files": [str(path)]})
    assert path.read_text() == "user content"
