from terramoo import plan
from terramoo.model import ObjectDef, PropDef, VerbDef
from terramoo.moolit import Map, Obj, Ref
from terramoo.refs import Refs

ME = Obj(130)


def refs(**registry):
    return Refs(player=ME, registry={k: Obj(v) for k, v in registry.items()}, sysrefs={"room": Obj(3), "exit": Obj(7), "thing": Obj(5)})


def room(**kw):
    base = dict(key="hall", name="Hall", parent=Ref("$", "room"))
    base.update(kw)
    return ObjectDef(**base)


def kinds(ops):
    return [op[0] for op in ops]


def test_identical_objects_need_nothing():
    r = refs(hall=200)
    want = room(props=[PropDef("description", "x", defined=False)], verbs=[VerbDef("look", ["return 1;"])])
    have = room(props=[PropDef("description", "x", defined=False)], verbs=[VerbDef("look", ["return 1;"])])
    assert plan.diff_object("hall", want, have, r) == []


def test_case_only_object_and_property_spelling_is_one_moo_identity():
    r = refs(Hall=200)
    want = room(key="hall", props=[PropDef("foo", 1)])
    have = room(key="Hall", props=[PropDef("Foo", 1)])

    pending = plan.build({"hall": want}, {"Hall": have}, r)

    assert pending.creates == []
    assert pending.destroys == {}
    assert pending.ops == [
        ("propinfo", Ref("@", "Hall"), "Foo", [ME, "rc", "foo"]),
        ("link", [Ref("@", "Hall")]),
    ]


def test_case_colliding_properties_are_rejected_as_a_plan_problem():
    r = refs(hall=200)
    want = room(props=[PropDef("Foo", 1), PropDef("foo", 2)])

    pending = plan.build({"hall": want}, {"hall": room()}, r)

    assert len(pending.problems) == 1
    assert "properties 'Foo' and 'foo' differ only in case" in pending.problems[0]
    assert pending.ops == []


def test_live_side_may_be_symbolized():
    """Export writes `$room` and `@name` where it can; both sides resolve."""
    r = refs(hall=200, gate=201)
    want = room(location=Ref("@", "gate"), props=[PropDef("dest", Ref("@", "gate"), defined=False)])
    have = room(parent=Obj(3), location=Obj(201), props=[PropDef("dest", Obj(201), defined=False)])
    assert plan.diff_object("hall", want, have, r) == []


def test_every_kind_of_change():
    r = refs(hall=200)
    want = room(
        name="Great Hall",
        flags="r",
        location=Ref("@", "me"),
        props=[
            PropDef("guard", 1, perms="r"),  # perms change
            PropDef("banner", "purple"),  # new defined
            PropDef("description", ["a", "b"], defined=False),  # override value change
        ],
        verbs=[
            VerbDef("look", ["return 2;"]),  # code change
            VerbDef("bow", ["pass();"], args=("any", "none", "none"), perms="rd"),  # new
        ],
    )
    have = room(
        flags="rf",
        props=[
            PropDef("guard", 1, perms="rc"),
            PropDef("old", 0),  # gone from the file
            PropDef("description", "a", defined=False),
            PropDef("arrival_msg", "x", defined=False),  # override the file dropped -> clear
        ],
        verbs=[VerbDef("look", ["return 1;"]), VerbDef("dance", [])],
    )
    ops = plan.diff_object("hall", want, have, r)
    assert kinds(ops) == [
        "name", "move", "flags",
        "propinfo", "addprop", "setprop", "rmprop", "clearprop",
        "verbcode", "addverb", "rmverb",
    ]
    assert ops[0] == ("name", Ref("@", "hall"), "Great Hall")
    assert ops[1] == ("move", Ref("@", "hall"), ME)
    assert ops[2] == ("flags", Ref("@", "hall"), "r")
    assert ops[3] == ("propinfo", Ref("@", "hall"), "guard", [ME, "r"])
    assert ops[4] == ("addprop", Ref("@", "hall"), "banner", "purple", [ME, "rc"])
    assert ops[9] == ("addverb", Ref("@", "hall"), [ME, "rd", "bow"], ["any", "none", "none"], ["pass();"])


def test_duplicate_verbs_are_matched_in_order_and_mutated_by_live_index():
    r = refs(hall=200)
    want = room(verbs=[
        VerbDef("do", ["return 1;"], args=("this", "none", "this")),
        VerbDef("do", ["return 3;"], args=("any", "none", "this")),
    ])
    have = room(verbs=[
        VerbDef("do", ["return 1;"], args=("this", "none", "this"), live_index=1),
        VerbDef("do", ["return 2;"], args=("any", "none", "this"), live_index=2),
        VerbDef("spare do", [], live_index=3),
        VerbDef("other", [], live_index=4),
    ])

    ops = plan.diff_object("hall", want, have, r)

    assert ops == [
        ("verbcode", Ref("@", "hall"), plan.VerbTarget(2, "do#2"), ["return 3;"]),
        ("rmverb", Ref("@", "hall"), plan.VerbTarget(4, "other")),
        ("rmverb", Ref("@", "hall"), plan.VerbTarget(3, "spare")),
    ]
    assert plan.describe(ops[0]) == "verbcode @hall.do#2"


def test_alias_change_updates_the_first_duplicate_primary_without_reordering():
    r = refs(hall=200)
    want = room(verbs=[
        VerbDef("do first", ["return 1;"]),
        VerbDef("do", ["return 2;"]),
    ])
    have = room(verbs=[
        VerbDef("do", ["return 1;"], live_index=1),
        VerbDef("do", ["return 2;"], live_index=2),
    ])

    ops = plan.diff_object("hall", want, have, r)

    assert ops == [
        ("verbinfo", Ref("@", "hall"), plan.VerbTarget(1, "do#1"), [ME, "rxd", "do first"]),
    ]
    have.verbs[0].names = "do first"
    assert [(verb.names, verb.code[0]) for verb in have.verbs] == [
        ("do first", "return 1;"),
        ("do", "return 2;"),
    ]
    assert plan.diff_object("hall", want, have, r) == []


def test_unchanged_duplicate_and_overlapping_alias_verbs_need_nothing():
    r = refs(hall=200)
    want = room(verbs=[
        VerbDef("do", ["return 1;"]),
        VerbDef("do", ["return 2;"], args=("any", "none", "this")),
        VerbDef("look do", ["return 3;"]),
    ])
    have = room(verbs=[
        VerbDef("do", ["return 1;"], live_index=1),
        VerbDef("do", ["return 2;"], args=("any", "none", "this"), live_index=2),
        VerbDef("look do", ["return 3;"], live_index=3),
    ])

    assert plan.diff_object("hall", want, have, r) == []


def test_unique_verb_change_describes_the_name_not_numeric_descriptor():
    r = refs(hall=200)
    want = room(verbs=[VerbDef("bow", ["return 2;"])])
    have = room(verbs=[VerbDef("bow", ["return 1;"], live_index=1)])

    ops = plan.diff_object("hall", want, have, r)

    assert ops == [
        ("verbcode", Ref("@", "hall"), plan.VerbTarget(1, "bow"), ["return 2;"]),
    ]
    assert plan.describe(ops[0]) == "verbcode @hall.bow"


def test_verb_code_only_change_emits_no_endpoint_reconciliation_ops():
    r = refs(hall=200, source=201, dest=202)
    endpoints = [
        PropDef("source", Ref("@", "source"), defined=False),
        PropDef("dest", Ref("@", "dest"), defined=False),
    ]
    want = room(props=endpoints, verbs=[VerbDef("bow", ["return 2;"])])
    have = room(
        props=[
            PropDef("source", Ref("@", "source"), defined=False),
            PropDef("dest", Ref("@", "dest"), defined=False),
        ],
        verbs=[VerbDef("bow", ["return 1;"], live_index=1)],
    )

    ops = plan.diff_object("hall", want, have, r)

    assert [op[0] for op in ops] == ["verbcode"]
    assert plan.describe(ops[0]) == "verbcode @hall.bow"


def test_new_object_sets_everything_and_is_created_parents_first():
    r = refs()
    files = {
        "guard": ObjectDef(key="guard", name="Guard", parent=Ref("@", "generic_guard"), location=Ref("@", "hall")),
        "generic_guard": ObjectDef(key="generic_guard", name="Generic Guard", parent=Ref("$", "thing"),
                                   props=[PropDef("greeting", "Halt!")]),
        "hall": room(),
    }
    p = plan.build(files, {}, r)
    assert [c[0] for c in p.creates] == ["generic_guard", "guard", "hall"]
    assert p.creates[0][1] == Ref("$", "thing")  # symbolized for display
    assert p.creates[1][1] == "generic_guard"  # a key: made in the same batch
    assert ("addprop", Ref("@", "generic_guard"), "greeting", "Halt!", [ME, "rc"]) in p.ops
    assert ("move", Ref("@", "guard"), Ref("@", "hall")) in p.ops  # still a Ref: resolved after create
    assert p.ops[-1][0] == "link"
    assert not p.problems


def test_registry_object_that_vanished_is_recreated_and_named():
    r = refs(hall=200)
    p = plan.build({"hall": room()}, {"hall": None}, r)
    assert p.creates == [("hall", Ref("$", "room"), "Hall")]
    assert p.gone == {"hall": Obj(200)}


def test_orphans_and_problems():
    r = refs(hall=200, attic=201)
    files = {"hall": room(parent=Ref("$", "castle"))}
    p = plan.build(files, {"hall": room()}, r)
    assert p.destroys == {"attic": Obj(201)}
    assert p.problems and "castle" in p.problems[0]
    assert p.creates == []


def test_defined_versus_override_mismatch_is_a_problem():
    r = refs(hall=200)
    want = room(props=[PropDef("description", "x", defined=True)])
    have = room(props=[PropDef("description", "x", defined=False)])
    p = plan.build({"hall": want}, {"hall": have}, r)
    assert p.problems and "inherited" in p.problems[0]


def test_map_values_compare_after_resolution():
    r = refs(hall=200, gate=201)
    want = room(props=[PropDef("links", Map({"north": Ref("@", "gate")}), defined=False)])
    have = room(props=[PropDef("links", Map({"north": Obj(201)}), defined=False)])
    assert plan.diff_object("hall", want, have, r) == []


def test_exit_endpoint_changes_are_one_atomic_reconciliation_op():
    r = refs(hall=200, old_source=201, new_source=202, old_dest=203, new_dest=204)
    want = room(props=[
        PropDef("source", Ref("@", "new_source"), defined=False),
        PropDef("dest", Ref("@", "new_dest"), defined=False),
    ])
    have = room(props=[
        PropDef("source", Ref("@", "old_source"), defined=False),
        PropDef("dest", Ref("@", "old_dest"), defined=False),
    ])

    assert plan.diff_object("hall", want, have, r) == [
        ("endpoint", Ref("@", "hall"), "source", Obj(201), Obj(202)),
        ("endpoint", Ref("@", "hall"), "dest", Obj(203), Obj(204)),
    ]


def test_plan_reports_legacy_registry_keys_without_blocking_other_changes():
    registry = {"hall": Obj(200), "bad-key": Obj(201), "مرحبا": Obj(202)}
    r = Refs(player=ME, registry=registry, sysrefs={"room": Obj(3)})

    pending = plan.build({"hall": room(name="Great Hall")}, {"hall": room()}, r)

    assert pending.problems == []
    assert pending.warnings == [
        "registry key 'bad-key' is legacy; rename it with `tmoo rename-key 'bad-key' NEW`",
        "registry key 'مرحبا' is legacy; rename it with `tmoo rename-key 'مرحبا' NEW`",
    ]
    assert ("name", Ref("@", "hall"), "Great Hall") in pending.ops


def test_non_object_source_and_dest_values_use_endpoint_compare_and_set():
    r = refs(hall=200)
    want = room(props=[
        PropDef("source", "a new book", defined=False),
        PropDef("dest", ["b"], defined=False),
    ])
    have = room(props=[
        PropDef("source", "an old book", defined=False),
        PropDef("dest", ["a"], defined=False),
    ])

    assert plan.diff_object("hall", want, have, r) == [
        ("endpoint", Ref("@", "hall"), "source", "an old book", "a new book"),
        ("endpoint", Ref("@", "hall"), "dest", ["a"], ["b"]),
    ]


def test_endpoint_change_with_a_non_object_on_either_side_still_reconciles_exits():
    r = refs(hall=200, old_source=201, new_dest=202)
    want = room(props=[
        PropDef("source", "not a room", defined=False),
        PropDef("dest", Ref("@", "new_dest"), defined=False),
    ])
    have = room(props=[
        PropDef("source", Ref("@", "old_source"), defined=False),
        PropDef("dest", "not a room", defined=False),
    ])

    assert plan.diff_object("hall", want, have, r) == [
        ("endpoint", Ref("@", "hall"), "source", Obj(201), "not a room"),
        ("endpoint", Ref("@", "hall"), "dest", "not a room", Obj(202)),
    ]


def test_reference_to_a_key_with_no_file_is_a_problem():
    r = refs(hall=200, door=201)
    files = {"hall": room(props=[PropDef("exit_to", Ref("@", "door"))])}
    p = plan.build(files, {"hall": room()}, r)
    assert p.problems == ["hall: refers to @door, which has no file"]
    assert not p.creates and not p.ops
    assert p.destroys == {"door": Obj(201)}


def test_reference_to_a_recreated_object_waits_for_its_new_number():
    """door is in the registry but gone from the MOO: hall's reference to it
    must not resolve to the recycled number."""
    r = refs(hall=200, door=201)
    files = {"hall": room(props=[PropDef("to", Ref("@", "door"))]),
             "door": ObjectDef(key="door", name="door", parent=Ref("$", "exit"))}
    live = {"hall": room(props=[PropDef("to", Obj(201))]), "door": None}
    p = plan.build(files, live, r)
    assert [c[0] for c in p.creates] == ["door"]
    assert ("setprop", Ref("@", "hall"), "to", Ref("@", "door")) in p.ops


def test_parent_cycle_is_a_problem_not_a_crash():
    r = refs()
    files = {
        "a": ObjectDef(key="a", name="A", parent=Ref("@", "b")),
        "b": ObjectDef(key="b", name="B", parent=Ref("@", "a")),
        "child": ObjectDef(key="child", name="Child", parent=Ref("@", "a")),
        "hall": room(),
    }
    p = plan.build(files, {}, r)
    assert p.problems == [
        "a: its parent chain loops back to itself",
        "b: its parent chain loops back to itself",
        "child: parent @a cannot be created",
    ]
    assert [c[0] for c in p.creates] == ["hall"]
    assert all(op[1] == Ref("@", "hall") for op in p.ops if op[0] != "link")
