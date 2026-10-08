from copy import deepcopy
from types import SimpleNamespace

import pytest

from terramoo import ownership
from terramoo.errors import MooError
from terramoo.moolit import Obj
from terramoo.refs import Refs, Registry


def context(tmp_path, monkeypatch):
    world = SimpleNamespace(dir=tmp_path)
    item = {"id": "instance-id", "state": "prepared", "revision": "desired", "tombstones": {},
            "objects": {"room": {"key": "north__room", "module": "rooms"}}}
    store = SimpleNamespace(instances={"north": item}, lock={"world_id": "world-id", "local_modules": {}})
    graph = SimpleNamespace(membership={"north__room": "north/rooms"})
    refs = Refs(Obj(1), Registry({"north__room": Obj(20)}, {"north__room": "generation"}))
    remote = {"world_id": "world-id", "epoch": "current", "entries": {
        "north__room": ["north__room", "generation", "instance-id", 1]}}
    receipt = {"world_id": "world-id", "epoch": "current", "identity": {"world": "test"}, "bindings": {
        "north__room": {"owner": "instance-id", "object": 20, "generation": "generation"}}}
    monkeypatch.setattr(ownership, "read_remote", lambda w: remote)
    monkeypatch.setattr(ownership, "read_receipt", lambda w, s: receipt)
    monkeypatch.setattr(ownership, "identity", lambda w: {"world": "test"})
    return world, store, graph, refs, remote, receipt


@pytest.mark.parametrize("change, message", [
    ("epoch", "revision differs"),
    ("world", "revision differs"),
    ("missing", "no matching deployment receipt"),
    ("generation", "matching installation receipt"),
    ("owner", "another installation"),
])
def test_stale_or_wrong_identity_cannot_authorize_package_mutation(tmp_path, monkeypatch, change, message):
    world, store, graph, refs, remote, receipt = context(tmp_path, monkeypatch)
    if change == "epoch":
        receipt["epoch"] = "stale"
    elif change == "world":
        remote["world_id"] = "other"
    elif change == "missing":
        receipt["epoch"] = ""
    elif change == "generation":
        refs.generations["north__room"] = "reused-number"
    else:
        remote["entries"]["north__room"][2] = "different-instance"
    before = deepcopy(remote)
    with pytest.raises(MooError, match=message):
        ownership.preflight(world, store, graph, refs, {"north__room"})
    assert remote == before


def test_new_instance_cannot_claim_occupied_unowned_key(tmp_path, monkeypatch):
    world, store, graph, refs, remote, receipt = context(tmp_path, monkeypatch)
    remote["entries"] = {}
    receipt["bindings"] = {}
    with pytest.raises(MooError, match="explicit package import"):
        ownership.preflight(world, store, graph, refs, {"north__room"})


def test_pending_remote_operation_blocks_another_deployment(tmp_path, monkeypatch):
    world, store, graph, refs, _, _ = context(tmp_path, monkeypatch)
    path = ownership.operation_path(world)
    path.parent.mkdir()
    path.write_text("{}")
    with pytest.raises(MooError, match="unfinished remote operation"):
        ownership.preflight(world, store, graph, refs, {"north__room"})
