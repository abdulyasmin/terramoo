import re

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


def test_moor_string_escapes_round_trip_through_nested_literals():
    value = ["é", Map({"arabic": "م", "control": "\n\t\x01"})]
    literal = r'{"\u00E9", ["arabic" -> "\u0645", "control" -> "\n\t\x01"]}'

    assert moolit.parse(literal, dialect="moor") == value
    assert moolit.serialize(value, dialect="moor") == literal
    assert moolit.parse(r'"\n\u00E9"') == "nu00E9"


@pytest.mark.parametrize("text", ["9" * 5_000, "[{{1}} -> 2]"])
def test_literal_conversion_failures_are_literal_errors(text):
    with pytest.raises(moolit.LiteralError):
        moolit.parse(text)


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


def test_objdef_keeps_unicode_raw_and_escapes_only_controls():
    obj = ObjectDef(
        key="cafe",
        name="Café مقهى",
        parent=Ref("$", "thing"),
        props=[PropDef(name="description", value=["Café", "مقهى\nsecond line"], perms="rc")],
    )
    text = objdef.render(obj)
    assert 'name: "Café مقهى"' in text
    assert '"مقهى\\nsecond line"' in text
    assert "\\u" not in text
    assert objdef.parse(text) == obj


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


@pytest.mark.parametrize(
    "a, b",
    [
        (1, 1.0),
        (1, True),
        (0, False),
        (1.0, True),
        (0.0, -0.0),
        ("Hall", "hall"),
        ([1], [1.0]),
        (Map({1: "x"}), Map({1.0: "x"})),
        (Map({"k": 1}), Map({"k": 1.0})),
        (Obj(1), 1),
        (Err("E_PERM"), "E_PERM"),
        (Sym("a"), "a"),
        (Ref("@", "a"), Obj(1)),
    ],
)
def test_values_of_different_moo_types_are_not_the_same(a, b):
    # Python's == calls most of these pairs equal; the MOO does not.
    assert not moolit.same(a, b)
    assert not moolit.same(b, a)


@pytest.mark.parametrize(
    "value",
    [1, -7, 1.5, 1e20, True, "x", Obj(3), Err("E_PERM"), Sym("s"), [1, [2.0, "a"]], Map({"b": 2, "a": [1]})],
)
def test_a_value_is_the_same_as_its_round_trip(value):
    assert moolit.same(value, moolit.parse(moolit.serialize(value)))


def test_map_entry_order_and_list_key_spelling_do_not_matter():
    assert moolit.same(Map({"a": 1, "b": 2}), Map({"b": 2, "a": 1}))
    assert moolit.same(Map({(1, 2): "pair"}), Map({(1, 2): "pair"}))
    assert moolit.same([1, 2], (1, 2))


def test_nan_is_the_same_as_itself_so_it_cannot_plan_forever():
    assert moolit.same(float("nan"), float("nan"))


def _nested(depth, opener="{"):
    if opener == "{":
        return "{" * depth + "}" * depth
    return "[1 -> " * depth + "1" + "]" * depth


@pytest.mark.parametrize("opener", ["{", "["])
def test_values_nest_up_to_the_limit_and_every_consumer_handles_them(opener):
    import copy
    value = moolit.parse(_nested(moolit.MAX_DEPTH, opener))
    # Each of these recurses once or more per level of nesting.
    assert moolit.parse(moolit.serialize(value)) == value
    assert moolit.walk(value, lambda v: v) == value
    assert copy.deepcopy(value) == value
    assert moolit.same(value, value)
    obj = ObjectDef(key="deep", name="Deep", parent=Obj(1), props=[PropDef("v", value)])
    assert objdef.parse(objdef.render(obj)) == obj


@pytest.mark.parametrize("opener", ["{", "["])
@pytest.mark.parametrize("extra", [1, 50_000])
def test_values_nested_past_the_limit_are_literal_errors(opener, extra):
    with pytest.raises(moolit.LiteralError, match="levels deep"):
        moolit.parse(_nested(moolit.MAX_DEPTH + extra, opener))


def test_an_object_file_with_a_too_deep_value_is_a_format_error():
    text = f"object deep\n  name: \"Deep\"\n  parent: #1\n  property v (flags: \"rc\") = {_nested(moolit.MAX_DEPTH + 1)};\nendobject\n"
    # World.load_files reports every ValueError with the file's path.
    with pytest.raises(ValueError, match="levels deep"):
        objdef.parse(text)


def test_a_recursion_error_while_parsing_is_a_literal_error():
    import inspect
    import sys
    limit = sys.getrecursionlimit()
    # Leave fewer frames than a value within MAX_DEPTH needs.
    sys.setrecursionlimit(len(inspect.stack(0)) + 30)
    try:
        with pytest.raises(moolit.LiteralError, match="too deeply"):
            moolit.parse(_nested(moolit.MAX_DEPTH))
    finally:
        sys.setrecursionlimit(limit)


@pytest.mark.parametrize(
    "text, keys",
    [
        ('[1 -> "int", 1.0 -> "float"]', "1 and 1.0"),
        ('[1 -> "int", true -> "bool"]', "1 and true"),
        ('[0 -> "int", false -> "bool"]', "0 and false"),
        ('[{1} -> "int", {1.0} -> "float"]', "{1} and {1.0}"),
    ],
)
def test_a_map_with_keys_python_would_merge_is_refused(text, keys):
    # ToastStunt keeps these keys apart; a Python dict would drop one entry.
    with pytest.raises(moolit.LiteralError, match=f"map keys {re.escape(keys)} are different"):
        moolit.parse(text)


def test_a_repeated_map_key_keeps_the_moo_last_one_wins_rule():
    assert moolit.parse('[1 -> "a", 1 -> "b"]') == Map({1: "b"})


def test_nesting_counts_lists_and_maps_including_keys():
    assert moolit.nesting(1) == 0
    assert moolit.nesting([]) == 1
    assert moolit.nesting([1, [2, [3]]]) == 3
    assert moolit.nesting(Map({(1, (2,)): "v"})) == 3
    assert moolit.nesting(moolit.parse(_nested(moolit.MAX_DEPTH))) == moolit.MAX_DEPTH


def test_answers_have_room_to_wrap_a_value_at_the_limit():
    # A helper answer wraps a value in a few lists, e.g. {1, {1, {..., value}}}.
    value = _nested(moolit.MAX_DEPTH)
    answer = "{1, {1, {1, " + value + "}}}"
    with pytest.raises(moolit.LiteralError):
        moolit.parse(answer)
    parsed = moolit.parse(answer, max_depth=moolit.ANSWER_DEPTH)
    assert moolit.nesting(parsed[1][1][1]) == moolit.MAX_DEPTH


def test_definitions_whose_values_differ_only_in_type_are_not_equal():
    assert PropDef("v", 1) != PropDef("v", 1.0)
    assert PropDef("v", [1, Map({"k": True})]) == PropDef("v", [1, Map({"k": True})])
    one = ObjectDef(key="k", name="n", parent=Obj(1), props=[PropDef("v", True)])
    other = ObjectDef(key="k", name="n", parent=Obj(1), props=[PropDef("v", 1)])
    assert one != other


def test_a_map_with_keys_that_differ_only_in_letter_case_is_refused():
    # ToastStunt and mooR both keep only one of them.
    with pytest.raises(moolit.LiteralError, match='map keys "name" and "Name" differ only in letter case'):
        moolit.parse('["name" -> 1, "Name" -> 2]')
    assert moolit.parse('["name" -> 1, "other" -> 2]') == Map({"name": 1, "other": 2})


@pytest.mark.parametrize("text, keys", [('[1 -> "a", 1.0 -> "b"]', "1 and 1.0"), ('[1 -> "a", true -> "b"]', "1 and true")])
def test_a_lenient_parse_records_merged_keys_so_they_are_never_written(text, keys):
    value = moolit.parse(text, strict_keys=False)
    assert len(value) == 1
    assert moolit.unrepresentable(value) == (
        f"has the map keys {keys}, different MOO values that terramoo cannot keep apart"
    )
    assert moolit.unrepresentable([1, Map({"k": value})]) is not None


def test_unrepresentable_reports_depth_and_accepts_ordinary_values():
    deep = moolit.parse(_nested(moolit.MAX_DEPTH + 1), max_depth=moolit.ANSWER_DEPTH)
    assert moolit.unrepresentable(deep) == f"nests lists or maps more than {moolit.MAX_DEPTH} levels deep"
    assert moolit.unrepresentable(moolit.parse('[1 -> {"a", 1.0}, "b" -> [2 -> 3]]')) is None


@pytest.mark.parametrize(
    "one, other",
    [
        (PropDef("v", 1, perms="rc"), PropDef("v", 1, perms="r")),
        (PropDef("v", 1), PropDef("v", 1, owner=Obj(2))),
        (PropDef("v", 1), PropDef("v", 1, defined=False)),
        (PropDef("v", 1), PropDef("w", 1)),
    ],
)
def test_definitions_that_differ_in_any_property_field_are_not_equal(one, other):
    assert one != other


def test_symbol_keys_that_differ_only_in_case_are_refused():
    # mooR folds symbol case in map keys; only mooR has symbols.
    with pytest.raises(moolit.LiteralError, match="differ only in letter case"):
        moolit.parse("['foo -> 1, 'Foo -> 2]", dialect="moor")


def test_non_ascii_keys_that_differ_in_case_are_kept_as_toaststunt_does():
    assert len(moolit.parse('["\\u00E9" -> 1, "\\u00C9" -> 2]', dialect="moor")) == 2
