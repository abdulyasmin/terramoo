import pytest

from terramoo import moolit, objdef
from terramoo.model import ObjectDef, PropDef, VerbDef, ordered_like
from terramoo.moolit import Err, Map, Obj, Ref, Sym


@pytest.mark.parametrize(
    "text,value",
    [
        ("1", 1),
        ("-7", -7),
        ("1.5", 1.5),
        ('"a \\"q\\" \\\\ b"', 'a "q" \\ b'),
        ("#130", Obj(130)),
        ("#-1", Obj(-1)),
        ("E_PERM", Err("E_PERM")),
        ("#048D05-1234567890", Obj("048D05-1234567890")),
        ("'sym", Sym("sym")),
        ("true", True),
        ("{}", []),
        ('{1, "two", #3, {4}}', [1, "two", Obj(3), [4]]),
        ('["k" -> 1, 2 -> "v"]', Map({"k": 1, 2: "v"})),
        ("$room", Ref("$", "room")),
        ("@grand_courtyard", Ref("@", "grand_courtyard")),
    ],
)
def test_literal_round_trip(text, value):
    assert moolit.parse(text) == value
    assert moolit.parse(moolit.serialize(value)) == value


def test_literal_rejects_trailing():
    with pytest.raises(moolit.LiteralError):
        moolit.parse("1 2")


def test_float_serialization_keeps_a_point():
    assert moolit.serialize(2.0) == "2.0"
    assert moolit.parse(moolit.serialize(1e20)) == 1e20


SAMPLE = '''object grand_courtyard
  name: "Grand Courtyard"
  parent: $room
  location: @gatehouse
  flags: "r"

  property guard_class (flags: "rc") = @generic_guard;
  property "odd name" (flags: "r", owner: #2) = 1;
  property description (flags: "rc") = {
    "A broad court of fitted flagstones.",
    "Wide steps rise north; the fountain says \\"drink\\"."
  };
  override arrival_msg = "The lions never stop pouring.";

  verb bow (any none none) flags: "rd"
    "Bow to the crown.";
    if (player.location == this)
      player:tell("You bow.");
    endif
  endverb

  verb "look_self look*ing" (this none this) flags: "rxd" owner: #2
    return pass(@args);
  endverb
endobject
'''


def test_objdef_round_trip():
    obj = objdef.parse(SAMPLE)
    assert obj.key == "grand_courtyard"
    assert obj.name == "Grand Courtyard"
    assert obj.parent == Ref("$", "room")
    assert obj.location == Ref("@", "gatehouse")
    assert obj.flags == "r"
    assert [p.name for p in obj.props] == ["guard_class", "odd name", "description", "arrival_msg"]
    _, odd, description, arrival = obj.props
    assert odd.owner == Obj(2)
    assert description.value[1] == 'Wide steps rise north; the fountain says "drink".'
    assert arrival.defined is False
    bow, look = obj.verbs
    assert bow.args == ("any", "none", "none")
    assert bow.code[1] == "if (player.location == this)"
    assert look.names == "look_self look*ing"
    assert look.owner == Obj(2)
    assert objdef.render(obj) == SAMPLE


def test_objdef_minimal():
    obj = ObjectDef(key="thing", name="a thing", parent=Ref("$", "thing"))
    text = objdef.render(obj)
    assert text == 'object thing\n  name: "a thing"\n  parent: $thing\nendobject\n'
    assert objdef.parse(text) == obj


def test_objdef_errors():
    with pytest.raises(objdef.FormatError):
        objdef.parse("object x\n  name: \"x\"\nendobject\n")  # no parent
    with pytest.raises(objdef.FormatError):
        objdef.parse('object x\n  name: "x"\n  parent: $thing\n  verb v (this none this)\n  return 1;\n  endverb\nendobject\n')


def test_verb_code_blank_lines_survive():
    obj = ObjectDef(
        key="t", name="t", parent=Obj(1), verbs=[VerbDef(names="v", code=["a = 1;", "", "return a;"])]
    )
    assert objdef.parse(objdef.render(obj)).verbs[0].code == ["a = 1;", "", "return a;"]


def test_prop_value_with_semicolon_in_string():
    obj = ObjectDef(key="t", name="t", parent=Obj(1), props=[PropDef(name="p", value="a; b")])
    assert objdef.parse(objdef.render(obj)).props[0].value == "a; b"


def test_ordered_like_keeps_the_file_order_and_appends_new_ones():
    live = ObjectDef(key="x", name="x", parent=Obj(1),
                     props=[PropDef("c", 1), PropDef("new", 2), PropDef("a", 3)],
                     verbs=[VerbDef("z", []), VerbDef("y", [])])
    file = ObjectDef(key="x", name="x", parent=Obj(1), props=[PropDef("a", 0), PropDef("c", 0)],
                     verbs=[VerbDef("y", []), VerbDef("z", [])])
    ordered_like(live, file)
    assert [p.name for p in live.props] == ["a", "c", "new"]
    assert [v.key for v in live.verbs] == ["y", "z"]
