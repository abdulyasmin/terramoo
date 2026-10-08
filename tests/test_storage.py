import json

import pytest

from terramoo.errors import MooError
from terramoo import storage


def test_interrupted_transaction_recovers_forward(tmp_path, monkeypatch):
    objects = tmp_path / "objects"
    objects.mkdir()
    a, b = objects / "a.moo", objects / "b.moo"
    a.write_text("old a")
    b.write_text("old b")
    real_write = storage.atomic_write

    def fail(path, data):
        if path == b:
            raise OSError("disk full")
        real_write(path, data)

    monkeypatch.setattr(storage, "atomic_write", fail)
    with pytest.raises(OSError):
        storage.transaction(tmp_path, {a: "new a", b: "new b"})
    assert a.read_text() == "new a" and b.read_text() == "old b"
    assert storage.journal_path(tmp_path).exists()
    monkeypatch.setattr(storage, "atomic_write", real_write)
    storage.recover(tmp_path)
    assert b.read_text() == "new b" and not storage.journal_path(tmp_path).exists()


def test_recovery_refuses_concurrent_edits_before_replacing_other_files(tmp_path, monkeypatch):
    path = tmp_path / "objects/a.moo"
    path.parent.mkdir()
    path.write_text("old")
    real_recover = storage.recover
    monkeypatch.setattr(storage, "recover", lambda *args: None)
    storage.transaction(tmp_path, {path: "new"})
    path.write_text("user edit")
    with pytest.raises(MooError, match="changed during transaction"):
        real_recover(tmp_path)
    assert path.read_text() == "user edit"


def test_corrupt_recovery_path_cannot_escape_world(tmp_path):
    journal = storage.journal_path(tmp_path)
    journal.parent.mkdir()
    journal.write_text(json.dumps({"schema_version": 1, "changes": [
        {"path": "../elsewhere", "before": None, "after": "b29wcw=="}]}))
    with pytest.raises(MooError, match="unsafe transaction path"):
        storage.recover(tmp_path)


def test_case_alias_transaction_does_not_delete_replaced_file(tmp_path):
    path = tmp_path / "objects" / "room.moo"
    path.parent.mkdir()
    path.write_text("old")
    destination = path.with_name("ROOM.moo")
    if not destination.exists():
        pytest.skip("requires a case-insensitive filesystem")
    storage.transaction(tmp_path, {path: None, destination: "new"})
    assert destination.read_text() == "new"
    assert [p.name for p in path.parent.iterdir()] == ["ROOM.moo"]


def test_case_rename_recovers_after_content_write_before_spelling_change(tmp_path, monkeypatch):
    path = tmp_path / "objects" / "room.moo"
    path.parent.mkdir()
    path.write_text("old")
    destination = path.with_name("ROOM.moo")
    if not destination.exists():
        pytest.skip("requires a case-insensitive filesystem")
    rename = storage.canonical_name

    def interrupt(target):
        if target == destination:
            raise OSError("interrupted after replacement")
        rename(target)

    monkeypatch.setattr(storage, "canonical_name", interrupt)
    with pytest.raises(OSError):
        storage.transaction(tmp_path, {path: None, destination: "new"})
    monkeypatch.setattr(storage, "canonical_name", rename)
    storage.recover(tmp_path)
    assert destination.read_text() == "new"
    assert [p.name for p in path.parent.iterdir()] == ["ROOM.moo"]
