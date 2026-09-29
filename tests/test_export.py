"""Decoding and batching live object records returned by the MOO helper."""

from types import SimpleNamespace

import pytest

from terramoo import export as export_mod, moolit
from terramoo.errors import MooError
from terramoo.model import PropDef, VerbDef
from terramoo.moolit import Obj, Ref
from terramoo.refs import Refs


class ExportWorld:
    def __init__(self, records, ignore_props=()):
        self.records = records
        self.ignore_props = set(ignore_props)
        self.calls = []

    def helper(self, verb, arg):
        return SimpleNamespace(verb=verb, arg=arg)

    def eval(self, call):
        assert call.verb == "tmoo_export"
        objects = moolit.parse(call.arg)
        self.calls.append(objects)
        return [self.records[obj] for obj in objects]


def test_export_chunks_requests_and_preserves_vanished_objects():
    registry = {f"object_{i}": Obj(i) for i in range(1, 11)}
    records = {
        obj: [obj] if i == 4 else [obj, f"Object {i}", Obj(0), Obj(-1), Obj(1), "", [], []]
        for i, obj in enumerate(registry.values(), 1)
    }
    world = ExportWorld(records)
    refs = Refs(player=Obj(1), registry=registry)

    result = export_mod.export(world, refs, list(registry))

    assert [len(call) for call in world.calls] == [export_mod.CHUNK, 2]
    assert result["object_4"] is None
    assert result["object_10"].name == "Object 10"


def test_export_decodes_refs_owners_flags_properties_and_verbs():
    hall, guard, room, player = Obj(10), Obj(11), Obj(2), Obj(1)
    record = [
        hall,
        "Hall",
        room,
        Obj(-1),
        player,
        "fxwr",
        [
            ["runtime", 1, player, "rc", "99"],
            ["targets", 0, guard, "cr", "{#10, #2, #99}"],
        ],
        [["look inspect", player, "dxr", ["this", "none", "this"], ["return #10;"]]],
    ]
    refs = Refs(
        player=player,
        registry={"hall": hall, "guard": guard},
        sysrefs={"room": room},
    )
    world = ExportWorld({hall: record}, ignore_props={"runtime"})

    obj = export_mod.export(world, refs, ["hall"])["hall"]

    assert obj.parent == Ref("$", "room")
    assert obj.location == Obj(-1)
    assert obj.owner is None
    assert obj.flags == "rwf"
    assert obj.props == [
        PropDef(
            "targets",
            [Ref("@", "hall"), Ref("$", "room"), Obj(99)],
            perms="cr",
            owner=Ref("@", "guard"),
            defined=False,
        )
    ]
    assert obj.verbs == [
        VerbDef(
            "look inspect",
            ["return #10;"],
            args=("this", "none", "this"),
            perms="dxr",
        )
    ]


def test_export_rejects_a_malformed_nested_property_literal():
    obj = Obj(10)
    record = [obj, "Hall", Obj(0), Obj(-1), Obj(1), "", [["bad", 1, Obj(1), "rc", "{1,"]], []]
    world = ExportWorld({obj: record})
    refs = Refs(player=Obj(1), registry={"hall": obj})

    with pytest.raises(moolit.LiteralError, match="unexpected"):
        export_mod.export(world, refs, ["hall"])


@pytest.mark.parametrize(
    "records, expected",
    [
        ([], "returned no record for #10"),
        ([[Obj(99)]], "returned an unexpected object #99"),
        ([[]], "returned a malformed record"),
        ([[Obj(10)], [Obj(10)]], "returned #10 more than once"),
    ],
)
def test_export_rejects_incomplete_and_malformed_helper_records(records, expected):
    obj = Obj(10)
    world = ExportWorld({obj: [obj]})
    world.eval = lambda call: records
    refs = Refs(player=Obj(1), registry={"hall": obj})

    with pytest.raises(MooError, match=expected):
        export_mod.export(world, refs, ["hall"])
