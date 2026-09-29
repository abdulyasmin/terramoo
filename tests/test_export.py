"""Decoding and batching live object records returned by the MOO helper."""

from types import SimpleNamespace

import pytest

from terramoo import cli, export as export_mod, moolit, objdef, plan
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

    with pytest.raises(MooError, match=r"hall property 'bad'.*unexpected"):
        export_mod.export(world, refs, ["hall"])


def test_export_rejects_an_oversized_integer_property_literal():
    obj = Obj(10)
    record = [obj, "Hall", Obj(0), Obj(-1), Obj(1), "", [["bad", 1, Obj(1), "rc", "9" * 5_000]], []]
    world = ExportWorld({obj: record})
    refs = Refs(player=Obj(1), registry={"hall": obj})

    with pytest.raises(MooError, match=r"hall property 'bad'.*malformed literal"):
        export_mod.export(world, refs, ["hall"])


@pytest.mark.parametrize(
    "record, expected",
    [
        ([Obj(10), "Hall\nshutdown", Obj(0), Obj(-1), Obj(1), "", [], []], "malformed record"),
        ([Obj(10), "Hall", Obj(0), Obj(-1), Obj(1), "r\n", [], []], "malformed record"),
        ([Obj(10), "Hall", Obj(0), Obj(-1), Obj(1), "", [["bad\rname", 1, Obj(1), "rc", "1"]], []], "malformed property"),
        ([Obj(10), "Hall", Obj(0), Obj(-1), Obj(1), "", [["bad", 1, Obj(1), "rz", "1"]], []], "malformed property"),
        ([Obj(10), "Hall", Obj(0), Obj(-1), Obj(1), "", [], [[" \t", Obj(1), "rd", ["this", "none", "this"], []]]], "malformed fields"),
        ([Obj(10), "Hall", Obj(0), Obj(-1), Obj(1), "", [], [["look", Obj(1), "rz", ["this", "none", "this"], []]]], "malformed fields"),
        ([Obj(10), "Hall", Obj(0), Obj(-1), Obj(1), "", [], [["look", Obj(1), "rd", ["", "none", "this"], []]]], "malformed fields"),
        ([Obj(10), "Hall", Obj(0), Obj(-1), Obj(1), "", [], [["look", Obj(1), "rd", ["other", "none", "this"], []]]], "malformed fields"),
        ([Obj(10), "Hall", Obj(0), Obj(-1), Obj(1), "", [], [["look", Obj(1), "rd", ["this", "none", "this"], ["return 1;\nshutdown();"]]]], "malformed fields"),
    ],
)
def test_export_rejects_fields_that_cannot_be_rendered_as_lines(record, expected):
    obj = Obj(10)
    world = ExportWorld({obj: record})
    refs = Refs(player=Obj(1), registry={"hall": obj})

    with pytest.raises(MooError, match=expected):
        export_mod.export(world, refs, ["hall"])


def test_valid_export_record_renders_and_parses_round_trip():
    obj = Obj(10)
    record = [
        obj,
        "Hé",
        Obj(0),
        Obj(-1),
        Obj(1),
        "rf",
        [["greeting", 1, Obj(1), "rc", r'"hello\nworld"']],
        [["look inspect", Obj(1), "rd", ["this", "none", "this"], ["return 1;"]]],
    ]
    world = ExportWorld({obj: record})
    world.transport = SimpleNamespace(literal_dialect=moolit.MOOR)
    refs = Refs(player=Obj(1), registry={"hall": obj})

    exported = export_mod.export(world, refs, ["hall"])["hall"]

    assert exported.props[0].value == "hello\nworld"
    assert objdef.parse(objdef.render(exported)) == exported


@pytest.mark.parametrize("dialect", [moolit.LAMBDA, moolit.MOOR])
def test_export_render_parse_plan_is_empty_for_escaped_names(dialect):
    obj = Obj(10)
    odd_name = 'name\t"quoted"\\café'
    record = [
        obj,
        "Hall",
        Obj(0),
        Obj(-1),
        Obj(1),
        "",
        [[odd_name, 1, Obj(1), "rc", "1"]],
        [[odd_name, Obj(1), "rd", ["this", "none", "this"], ["return 1;"]]],
    ]
    world = ExportWorld({obj: record})
    world.transport = SimpleNamespace(literal_dialect=dialect)
    refs = Refs(player=Obj(1), registry={"hall": obj})

    exported = export_mod.export(world, refs, ["hall"])["hall"]
    reloaded = objdef.parse(objdef.render(exported))
    result = plan.build({"hall": reloaded}, {"hall": exported}, refs)

    assert reloaded == exported
    assert result.empty
    assert result.unchanged == ["hall"]


@pytest.mark.parametrize("dialect", [moolit.LAMBDA, moolit.MOOR])
def test_export_render_parse_plan_is_empty_for_canonical_multiword_preposition(dialect):
    obj = Obj(10)
    record = [
        obj,
        "Hall",
        Obj(0),
        Obj(-1),
        Obj(1),
        "",
        [],
        [["put", Obj(1), "rd", ["any", "on top of/on/onto/upon", "this"], ["return 1;"]]],
    ]
    world = ExportWorld({obj: record})
    world.transport = SimpleNamespace(literal_dialect=dialect)
    refs = Refs(player=Obj(1), registry={"hall": obj})

    exported = export_mod.export(world, refs, ["hall"])["hall"]
    reloaded = objdef.parse(objdef.render(exported))
    result = plan.build({"hall": reloaded}, {"hall": exported}, refs)

    assert reloaded == exported
    assert result.empty
    assert result.unchanged == ["hall"]


def test_export_skips_ignored_private_property_before_validating_unreadable_fields():
    obj = Obj(10)
    record = [
        obj,
        "Hall",
        Obj(0),
        Obj(-1),
        Obj(1),
        "",
        [["password", 1, Obj(-1), "?", "E_PERM"]],
        [],
    ]
    world = ExportWorld({obj: record}, ignore_props={"password"})
    refs = Refs(player=Obj(1), registry={"hall": obj})

    exported = export_mod.export(world, refs, ["hall"])["hall"]

    assert exported.props == []


def test_malformed_nested_verb_code_never_reaches_the_writer():
    obj = Obj(10)
    record = [
        obj, "Hall", Obj(0), Obj(-1), Obj(1), "", [],
        [["look", Obj(1), "rd", ["this", "none", "this"], "return 1;"]],
    ]
    world = ExportWorld({obj: record})
    world.file_for = lambda key: SimpleNamespace(exists=lambda: False)
    world.write_file = lambda value: pytest.fail("malformed export reached the writer")
    refs = Refs(player=Obj(1), registry={"hall": obj})

    with pytest.raises(MooError, match=r"hall verb 'look'.*malformed"):
        cli._write_exports(world, refs, ["hall"])


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
