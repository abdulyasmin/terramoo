import json

import pytest

from terramoo.moolit import Map, Obj, Ref
from terramoo.refs import Refs, Registry, UnresolvedRef, save_state


def test_pending_refs_keep_file_values_symbolic_but_resolve_live_values():
    refs = Refs(player=Obj(1), registry={"door": Obj(10)}, pending={"door"})
    value = [Ref("@", "door"), Map({"destination": Ref("@", "door")})]

    assert refs.resolve(value) == value
    assert refs.resolve(value, live=True) == [Obj(10), Map({"destination": Obj(10)})]


def test_map_keys_are_symbolized_and_resolved_including_list_keys():
    refs = Refs(player=Obj(1), registry={"door": Obj(10)})
    live = Map({Obj(10): "object", (Obj(10), "north"): "list"})
    symbolic = Map({Ref("@", "door"): "object", (Ref("@", "door"), "north"): "list"})

    assert refs.symbolize(live) == symbolic
    assert refs.resolve(symbolic) == live


def test_map_key_transforms_reject_collisions():
    refs = Refs(player=Obj(1), registry={"door": Obj(10)})

    with pytest.raises(ValueError, match="map key collision"):
        refs.resolve(Map({Ref("@", "door"): 1, Obj(10): 2}))


def test_resolve_rejects_unknown_managed_and_system_refs():
    refs = Refs(player=Obj(1))

    with pytest.raises(UnresolvedRef, match="@missing is not in the registry"):
        refs.resolve_ref(Ref("@", "missing"))
    with pytest.raises(UnresolvedRef, match=r"\$missing is not a corified object"):
        refs.resolve_ref(Ref("$", "missing"))


def test_registry_resolution_is_case_insensitive_and_preserves_spelling():
    refs = Refs(player=Obj(1), registry={"Door": Obj(10)})

    assert refs.registry_key("door") == "Door"
    assert refs.resolve_ref(Ref("@", "DOOR")) == Obj(10)
    assert refs.symbolize_obj(Obj(10)) == Ref("@", "Door")


def test_symbolize_prefers_player_then_registry_then_shortest_system_name():
    refs = Refs(
        player=Obj(1),
        registry={"player_alias": Obj(1), "hall": Obj(10)},
        sysrefs={"root_room": Obj(10), "room": Obj(3), "r": Obj(3), "nothing": Obj(-1)},
    )

    assert refs.symbolize_obj(Obj(1)) == Ref("@", "me")
    assert refs.symbolize_obj(Obj(10)) == Ref("@", "hall")
    assert refs.symbolize_obj(Obj(3)) == Ref("$", "r")
    assert refs.symbolize_obj(Obj(-1)) == Obj(-1)


def test_reindex_switches_both_resolution_directions_to_the_refreshed_registry():
    refs = Refs(player=Obj(1), registry={"old": Obj(10)})
    refs.registry = {"new": Obj(11)}
    refs.reindex()

    assert refs.resolve_ref(Ref("@", "new")) == Obj(11)
    assert refs.symbolize_obj(Obj(11)) == Ref("@", "new")
    assert refs.symbolize_obj(Obj(10)) == Obj(10)


def test_save_state_is_deterministic_and_uses_raw_object_identifiers(tmp_path):
    path = tmp_path / "state.json"

    save_state(
        path,
        Obj(1),
        Registry({"z": Obj(30), "a": Obj(20)}, {"z": "gen-z", "a": "gen-a"}),
        Obj(9),
    )

    assert json.loads(path.read_text()) == {
        "player": 1,
        "toolbox": 9,
        "registry": {"a": 20, "z": 30},
        "generations": {"a": "gen-a", "z": "gen-z"},
    }
    assert path.read_text().index('"a"') < path.read_text().index('"z"')
