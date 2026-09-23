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
    have = room(props=[PropDef("description", "x", defined=False)], verbs=[VerbDef("look", ["return 1;"])], obj=Obj(200))
    assert plan.diff_object("hall", want, have, r) == []


def test_live_side_may_be_symbolized():
    """Export writes `$room` and `@name` where it can; both sides resolve."""
    r = refs(hall=200, gate=201)
    want = room(location=Ref("@", "gate"), props=[PropDef("dest", Ref("@", "gate"), defined=False)])
    have = room(parent=Obj(3), location=Obj(201), props=[PropDef("dest", Obj(201), defined=False)], obj=Obj(200))
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
        obj=Obj(200),
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
    p = plan.build(files, {"hall": room(obj=Obj(200))}, r)
    assert p.destroys == ["attic"]
    assert p.problems and "castle" in p.problems[0]
    assert p.creates == []


def test_defined_versus_override_mismatch_is_a_problem():
    r = refs(hall=200)
    want = room(props=[PropDef("description", "x", defined=True)])
    have = room(props=[PropDef("description", "x", defined=False)], obj=Obj(200))
    p = plan.build({"hall": want}, {"hall": have}, r)
    assert p.problems and "inherited" in p.problems[0]


def test_map_values_compare_after_resolution():
    r = refs(hall=200, gate=201)
    want = room(props=[PropDef("links", Map({"north": Ref("@", "gate")}), defined=False)])
    have = room(props=[PropDef("links", Map({"north": Obj(201)}), defined=False)], obj=Obj(200))
    assert plan.diff_object("hall", want, have, r) == []


def test_reference_to_a_key_with_no_file_is_a_problem():
    r = refs(hall=200, door=201)
    files = {"hall": room(props=[PropDef("exit_to", Ref("@", "door"))])}
    p = plan.build(files, {"hall": room(obj=Obj(200))}, r)
    assert p.problems == ["hall: refers to @door, which has no file"]
    assert not p.creates and not p.ops
    assert p.destroys == ["door"]


def test_reference_to_a_recreated_object_waits_for_its_new_number():
    """door is in the registry but gone from the MOO: hall's reference to it
    must not resolve to the recycled number."""
    r = refs(hall=200, door=201)
    files = {"hall": room(props=[PropDef("to", Ref("@", "door"))]),
             "door": ObjectDef(key="door", name="door", parent=Ref("$", "exit"))}
    live = {"hall": room(props=[PropDef("to", Obj(201))], obj=Obj(200)), "door": None}
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
