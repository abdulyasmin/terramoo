from copy import deepcopy
from types import SimpleNamespace

import pytest

from terramoo import cli, moolit, player, playerdef
from terramoo.errors import MooError
from terramoo.model import PropDef, VerbDef
from terramoo.moolit import Obj, Ref
from terramoo.refs import Refs, Registry
from terramoo.storage import json_text, read_json, transaction
from terramoo.world import World, HELPER_VERBS, helper_source


class Server:
    def __init__(self):
        self.state = [1, "", 0, "", 0, "idle", []]
        self.rows = {("property", "description"): [1, 0, 0, Obj(1), "rc", "original"],
                     ("verb", "probe"): [1, 1, Obj(1), "rd", "probe", ["any", "any", "any"], ["return 1;"]]}
        self.writes = 0
        self.lose = None

    def call(self, world, helper, mode, *args, index=""):
        # The client reads part of an answer by indexing it in the MOO.
        return self.part(self._call(world, helper, mode, *args), index)

    @staticmethod
    def part(result, index):
        return {"": result, "[1..6]": result[:6] if isinstance(result, list) else result,
                "[7]": result[6] if isinstance(result, list) and len(result) > 6 else result}[index]

    def _call(self, world, helper, mode, *args):
        if helper == "tmoo_packages":
            return [1, "", "", []]
        if helper == "tmoo_player_read":
            if mode == "features":
                return [key[1] for key, row in self.rows.items() if key[0] == "feature" and row[1]]
            return deepcopy(self.rows.get(tuple(args[0]), [0]))
        if helper == "tmoo_player_write":
            assert mode == "check"
            return 1
        assert helper == "tmoo_player"
        if mode == "begin":
            wid, revision, token, _, _ = args
            if self.state[3] or revision != self.state[2]:
                raise MooError("stale")
            self.state = [1, wid, revision + 1, token, 0, "ready", []]
        elif mode == "apply":
            token, step, selector, expected, action, _ = args
            assert self.state[3] == token
            row = self.rows.get(tuple(selector), [0])
            if row != expected:
                raise MooError("changed")
            kind, name = selector
            if action[0] == "remove":
                row = [0]
            elif action[0] == "clear":
                row = [*row[:2], 1, *row[3:]]
            elif kind == "property":
                row = [1, action[1], 0, Obj(1), action[3], action[2]]
            elif kind == "verb":
                row = [1, row[1] if row[0] else 2, Obj(1), action[1], name, action[2], action[3]]
            elif kind == "feature":
                row = [1, int(action[0] != "detach")]
            else:
                value = action[1]
                if ":" in name and not isinstance(value, (str, Obj, list)):
                    value = 1 if value else 0  # the core's option flag path
                row = [1, value, []]
            self.rows[tuple(selector)] = row
            self.writes += 1
            self.state = [*self.state[:4], step, "done", [1, deepcopy(row)]]
        elif mode == "finish":
            assert self.state[3] == args[0]
            self.state[3] = ""
            self.state[5] = "finished"
        if self.lose == mode:
            self.lose = None
            raise MooError("lost response")
        return deepcopy(self.state)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    w = World("test", tmp_path, "tester", {"host": "localhost", "transport": "telnet"})
    w.objects_dir.mkdir(parents=True)
    (w.dir / "world.toml").write_text('player = "tester"\n')
    w._player, w._toolbox = Obj(1), Obj(2)
    monkeypatch.setattr(w, "refs", lambda: Refs(Obj(1), Registry({"feature": Obj(3)}, {"feature": "nonce"})))
    server = Server()
    monkeypatch.setattr(player, "call", server.call)
    return w, server


def edit(w, update):
    profile = player.load(w)
    update(profile)
    player.paths(w)[0].write_text(playerdef.render(profile))


def test_round_trip_distinguishes_clear_overrides_and_explicit_removal():
    text = '''player
  override description = {"one", "two"};
  property favorite (flags: "rc") = @feature;
  setting "display:shortprep" = 1;
  feature @feature;
  clear property another;
  remove verb "old alias";
  verb "probe alias" (any any any) flags: "rd"
    return "endplayer";
  endverb
endplayer
'''
    result = playerdef.parse(text)
    assert playerdef.parse(playerdef.render(result)) == result
    assert len(result.entries()) == 7


@pytest.mark.parametrize("name", ["password", "Password", "api_token", "email_address", "messages", "tmoo", "location", "gender", "aliases", "features", "ps", "ansi_options", "_terramoo_player_state"])
def test_protected_properties_fail_before_read(setup, name):
    w, server = setup
    with pytest.raises(MooError, match="protected"):
        player.selectors(SimpleNamespace(property=[name]))
    with pytest.raises(MooError, match="protected"):
        playerdef.parse(f'player\n override {name} = "x";\nendplayer\n')
    assert server.writes == 0


@pytest.mark.parametrize("body", ['name: "oops"', 'parent: #1', 'location: #1', 'flags: "w"', 'setting password = "oops";', 'property x (owner: #5) = 1;'])
def test_no_player_lifecycle_or_unsupported_setting(body):
    with pytest.raises((MooError, ValueError)):
        playerdef.parse(f"player\n  {body}\nendplayer\n")


def test_tracking_is_read_only_and_keeps_verb_flags(setup):
    w, server = setup
    player.track(w, [("property", "description"), ("verb", "probe")])
    assert server.writes == 0
    assert player.load(w).verbs[0].perms == "rd"
    p = player.prepare(w)
    assert not p.changes and not p.problems
    player.apply(p)
    assert server.writes == 0
    assert not player.prepare(w).needs_receipt


def test_omission_does_not_delete_unmanaged_or_previously_selected_fields(setup):
    w, server = setup
    player.track(w, [("property", "description"), ("verb", "probe")])
    edit(w, lambda p: p.verbs.clear())
    player.apply(player.prepare(w))
    assert server.rows[("verb", "probe")][0] == 1
    assert server.writes == 0


def test_live_edit_conflicts_instead_of_being_silently_overwritten(setup):
    w, server = setup
    player.track(w, [("property", "description")])
    server.rows[("property", "description")][-1] = "live edit"
    edit(w, lambda p: setattr(p.props[0], "value", "local edit"))
    p = player.prepare(w)
    assert "live drift" in p.problems[0]
    with pytest.raises(MooError, match="fix player"):
        player.apply(p)
    assert server.writes == 0
    player.track(w, [], pull=True)
    assert player.load(w).props[0].value == "live edit"


def test_matching_live_edit_can_be_acknowledged_without_writing(setup):
    w, server = setup
    player.track(w, [("property", "description")])
    server.rows[("property", "description")][-1] = "same change"
    edit(w, lambda p: setattr(p.props[0], "value", "same change"))
    p = player.prepare(w)
    assert not p.problems and not p.changes
    player.apply(p)
    assert server.writes == 0


@pytest.mark.parametrize("lose", ["begin", "apply", "finish"])
def test_lost_responses_recover_without_replaying_mutations(setup, lose):
    w, server = setup
    player.track(w, [("property", "description")])
    edit(w, lambda p: setattr(p.props[0], "value", "new"))
    p = player.prepare(w)
    server.lose = lose
    with pytest.raises(MooError, match="lost response"):
        player.apply(p)
    before = server.writes
    player.recover(w)
    assert server.writes == before
    assert not player.paths(w)[2].exists()
    assert not server.state[3]
    assert not player.prepare(w).problems


def test_uncertain_callback_requires_explicit_acceptance(setup):
    w, server = setup
    player.track(w, [("property", "description")])
    edit(w, lambda p: setattr(p.props[0], "value", "new"))
    server.lose = "apply"
    with pytest.raises(MooError):
        player.apply(player.prepare(w))
    server.state[5] = "running"
    server.state[6] = []
    with pytest.raises(MooError, match="uncertain"):
        player.recover(w)
    player.recover(w, accept_live=True)
    assert server.writes == 1
    assert not player.prepare(w).changes


def test_stale_receipt_and_changed_local_files_stop_writes(setup):
    w, server = setup
    player.track(w, [("property", "description")])
    p = player.prepare(w)
    edit(w, lambda p: setattr(p.props[0], "value", "changed after plan"))
    with pytest.raises(MooError, match="changed after planning"):
        player.apply(p)
    server.state[2] += 1
    with pytest.raises(MooError, match="stale player receipt"):
        player.prepare(w)
    assert server.writes == 0


def test_shifted_verb_indices_are_a_conflict(setup):
    w, server = setup
    player.track(w, [("verb", "probe")])
    edit(w, lambda p: p.verbs[0].code.append("return 2;"))
    server.rows[("verb", "probe")][1] = 2
    assert player.prepare(w).problems
    assert server.writes == 0


def test_clear_property_stays_inherited_when_parent_value_changes(setup):
    w, server = setup
    server.rows[("property", "description")][2] = 1
    player.track(w, [("property", "description")])
    assert "clear property description" in player.paths(w)[0].read_text()
    server.rows[("property", "description")][-1] = "new inherited description"
    p = player.prepare(w)
    assert not p.changes and not p.problems
    player.apply(p)
    assert server.writes == 0


def test_explicit_removal_and_untracking_are_different(setup):
    w, server = setup
    player.track(w, [("property", "description"), ("verb", "probe")])
    player.untrack(w, [("property", "description")])
    assert server.rows[("property", "description")][-1] == "original"
    player.stage_removal(w, [("verb", "probe")], "remove")
    player.apply(player.prepare(w))
    assert server.rows[("verb", "probe")] == [0]
    assert not player.load(w).entries()
    assert not player.receipt(w)["fields"]


def test_feature_removal_after_package_files_are_staged_for_deletion(setup):
    w, server = setup
    server.rows[("feature", Obj(3))] = [1, 1]
    (w.objects_dir / "feature.moo").write_text('object feature\n name: "Feature"\n parent: #-1\nendobject\n')
    player.track(w, [("feature", Ref("@", "feature"))])
    player.stage_removal(w, [("feature", Ref("@", "feature"))], "detach")
    (w.objects_dir / "feature.moo").unlink()
    player.apply(player.prepare(w))
    assert server.rows[("feature", Obj(3))] == [1, 0]
    assert not player.load(w).features


def test_key_migration_rewrites_player_references_and_feature_receipt(setup):
    w, server = setup
    server.rows[("feature", Obj(3))] = [1, 1]
    player.track(w, [("feature", Ref("@", "feature"))])
    changes = player.rewrite_files(w, {"feature": "renamed"})
    transaction(w.dir, changes)
    assert player.load(w).features == [Ref("@", "renamed")]
    assert "feature:@renamed" in player.receipt(w)["fields"]


def test_incoming_live_player_reference_blocks_deletion(setup):
    w, server = setup
    server.rows[("property", "custom")] = [1, 1, 0, Obj(1), "rc", Obj(3)]
    player.track(w, [("property", "custom")])
    edit(w, lambda p: setattr(p.props[0], "value", Obj(-1)))
    with pytest.raises(MooError, match="live player"):
        player.incoming_check(w, w.refs(), {"feature"})


def test_builtin_player_alias_cannot_be_adopted(monkeypatch):
    w = SimpleNamespace(refs=lambda: Refs(Obj(1)))
    with pytest.raises(MooError, match="cannot be adopted"):
        cli._adopt_locked(w, SimpleNamespace(owned=False, verify=False, object="#1", key="character"))


def test_bootstrap_substitutes_the_protected_name_policy():
    source = "\n".join(helper_source("tmoo_player_read"))
    assert "__TMOO_PLAYER_PROTECTED__" not in source
    assert '"password"' in source and '"out_of_band_session"' in source
    assert all(32 <= ord(c) < 127 for name in HELPER_VERBS for line in helper_source(name) for c in line)


def test_new_player_file_rejects_symlinks(setup, tmp_path):
    w, _ = setup
    outside = tmp_path / "outside.moo"
    outside.write_text("player\nendplayer\n")
    (w.dir / "player.moo").symlink_to(outside)
    with pytest.raises(MooError, match="symlink"):
        player.load(w)


def test_concurrent_file_edit_is_preserved_while_tracking(setup, monkeypatch):
    w, server = setup
    original = server.call
    def edited(world, helper, mode, *args, **kwargs):
        result = original(world, helper, mode, *args, **kwargs)
        if helper == "tmoo_player_read":
            player.paths(w)[0].write_text('player\n property local_edit (flags: "rc") = 1;\nendplayer\n')
        return result
    monkeypatch.setattr(player, "call", edited)
    with pytest.raises(MooError, match="changed"):
        player.track(w, [("property", "description")])
    assert "local_edit" in player.paths(w)[0].read_text()
    assert not player.paths(w)[1].exists()


def test_player_receipt_cannot_move_to_another_character(setup):
    w, server = setup
    player.track(w, [("property", "description")])
    w._player = Obj(99)
    with pytest.raises(MooError, match="wrong world/player/toolbox"):
        player.prepare(w)
    assert server.writes == 0


def test_bad_feature_reference_has_a_user_facing_error():
    with pytest.raises(MooError, match="invalid feature reference"):
        player.selectors(SimpleNamespace(feature=["@"]))


def test_receipt_edit_during_apply_is_preserved_until_reconciled(setup, monkeypatch):
    w, server = setup
    player.track(w, [("property", "description")])
    edit(w, lambda p: setattr(p.props[0], "value", "new"))
    original = server.call
    receipt = player.paths(w)[1]
    before = receipt.read_bytes()
    def edited(world, helper, mode, *args, **kwargs):
        result = original(world, helper, mode, *args, **kwargs)
        if helper == "tmoo_player" and mode == "apply":
            receipt.write_text('{"concurrent": "edit"}\n')
        return result
    monkeypatch.setattr(player, "call", edited)
    with pytest.raises(MooError, match="changed during player application"):
        player.apply(player.prepare(w))
    assert read_json(receipt) == {"concurrent": "edit"}
    assert player.paths(w)[2].exists()
    receipt.write_bytes(before)
    player.recover(w)
    assert server.writes == 1
    assert not player.prepare(w).changes


def test_namespace_migration_preserves_edits_made_while_rewriting_player(setup, monkeypatch):
    from terramoo import migrations
    from terramoo.installation import Store
    w, _ = setup
    (w.objects_dir / "feature.moo").write_text('object feature\n name: "Feature"\n parent: #-1\nendobject\n')
    player.paths(w)[0].write_text('player\n feature @feature;\nendplayer\n')
    original = player.rewrite_files
    def edited(world, mapping):
        result = original(world, mapping)
        player.paths(world)[0].write_text('player\n property edit = 1;\nendplayer\n')
        return result
    monkeypatch.setattr(player, "rewrite_files", edited)
    proposal = migrations.prepare(Store(w), {"feature": "renamed"})
    with pytest.raises(MooError, match="changed"):
        transaction(w.dir, {}, expected=proposal["expected"])
    assert "property edit" in player.paths(w)[0].read_text()


def test_feature_aliases_cannot_manage_the_same_live_feature_twice(setup):
    w, server = setup
    (w.objects_dir / "feature.moo").write_text('object feature\n name: "Feature"\n parent: #-1\nendobject\n')
    server.rows[("feature", Obj(3))] = [1, 0]
    player.paths(w)[0].write_text('player\n feature @feature;\n feature #3;\nendplayer\n')
    with pytest.raises(MooError, match="same live player field"):
        player.prepare(w)
    assert server.writes == 0


def test_a_number_type_edit_is_a_player_change(setup):
    w, server = setup
    server.rows[("property", "count")] = [1, 1, 0, Obj(1), "rc", 1]
    player.track(w, [("property", "count")])
    edit(w, lambda p: setattr(p.props[0], "value", 1.0))

    p = player.prepare(w)

    assert not p.problems
    assert [f["key"] for f in p.changes] == ["property:count"]
    player.apply(p)
    assert type(server.rows[("property", "count")][-1]) is float
    assert not player.prepare(w).changes


def test_a_live_number_type_change_is_drift(setup):
    w, server = setup
    server.rows[("property", "count")] = [1, 1, 0, Obj(1), "rc", 1]
    player.track(w, [("property", "count")])
    server.rows[("property", "count")][-1] = 1.0

    p = player.prepare(w)

    assert "live drift" in p.problems[0]


@pytest.mark.parametrize("value", ["true", "false", "1.0", "0.0", "2", "-1"])
def test_option_settings_refuse_values_the_core_cannot_keep(value):
    # The core stores any true flag as 1 and drops a false one, so these
    # could never be read back as written and apply would wedge.
    with pytest.raises(MooError, match="write 1 or 0"):
        playerdef.parse(f'player\n  setting "display:shortprep" = {value};\nendplayer\n')


def _option_definitions(w, monkeypatch):
    monkeypatch.setattr(w, "eval", lambda expr: Obj(9) if expr == "$display_options" else ["shortprep"])


def test_an_option_setting_converges_through_the_core_flag(setup, monkeypatch):
    w, server = setup
    _option_definitions(w, monkeypatch)
    server.rows[("setting", "display:shortprep")] = [1, 0, []]
    player.track(w, [("setting", "display:shortprep")])
    edit(w, lambda p: p.settings.update({"display:shortprep": 1}))

    p = player.prepare(w)
    assert [f["key"] for f in p.changes] == ["setting:display:shortprep"]
    player.apply(p)

    assert server.rows[("setting", "display:shortprep")] == [1, 1, []]
    assert not player.prepare(w).changes
    assert not player.paths(w)[2].exists()


def _count(server, value=1):
    server.rows[("property", "count")] = [1, 1, 0, Obj(1), "rc", value]


def test_a_number_type_change_after_planning_stops_apply_before_writing(setup):
    w, server = setup
    _count(server)
    player.track(w, [("property", "count")])
    edit(w, lambda p: setattr(p.props[0], "value", 2))
    p = player.prepare(w)
    server.rows[("property", "count")][-1] = 1.0

    with pytest.raises(MooError, match="changed after planning"):
        player.apply(p)
    assert server.writes == 0


def test_a_number_type_change_before_finishing_is_reported(setup, monkeypatch):
    w, server = setup
    _count(server)
    player.track(w, [("property", "count")])
    edit(w, lambda p: setattr(p.props[0], "value", 2))
    p = player.prepare(w)
    original = server.call

    def call(world, helper, mode, *args, **kwargs):
        result = original(world, helper, mode, *args, **kwargs)
        if helper == "tmoo_player" and mode == "apply":
            server.rows[("property", "count")][-1] = 2.0  # another task's edit
        return result

    monkeypatch.setattr(player, "call", call)
    with pytest.raises(MooError, match="changed after its operation"):
        player.apply(p)


def test_a_number_type_change_after_a_lost_result_is_reported(setup):
    w, server = setup
    _count(server)
    player.track(w, [("property", "count")])
    edit(w, lambda p: setattr(p.props[0], "value", 2))
    p = player.prepare(w)
    server.lose = "apply"
    with pytest.raises(MooError, match="lost response"):
        player.apply(p)
    server.rows[("property", "count")][-1] = 2.0

    with pytest.raises(MooError, match="changed after the recorded result"):
        player.recover(w)


def test_a_value_at_the_depth_limit_survives_the_wire_and_the_journal(setup, monkeypatch):
    w, server = setup
    original = server.call

    def wire(world, helper, mode, *args, **kwargs):
        # Arguments and answers travel as MOO literals, wrapped in more lists.
        def trip(value):
            text = moolit.serialize(value, dialect=moolit.MOOR)
            return moolit.parse(text, dialect=moolit.MOOR, max_depth=moolit.ANSWER_DEPTH)
        return trip(original(world, helper, mode, *[trip(a) for a in args], **kwargs))

    monkeypatch.setattr(player, "call", wire)
    deep = moolit.parse("{" * moolit.MAX_DEPTH + "}" * moolit.MAX_DEPTH)
    edit(w, lambda p: p.props.append(PropDef("deep", deep)))

    p = player.prepare(w)
    assert not p.problems
    player.apply(p)

    assert moolit.nesting(server.rows[("property", "deep")][-1]) == moolit.MAX_DEPTH
    assert not player.prepare(w).changes


def test_a_live_value_deeper_than_files_allow_is_refused(setup):
    w, server = setup
    deep = moolit.parse("{" * 101 + "}" * 101, max_depth=moolit.ANSWER_DEPTH)
    server.rows[("property", "deep")] = [1, 1, 0, Obj(1), "rc", deep]

    with pytest.raises(MooError, match="more than 100 levels deep"):
        player.track(w, [("property", "deep")])
    assert not player.paths(w)[0].exists()


def test_an_unreadable_journal_value_is_a_moo_error():
    with pytest.raises(MooError, match="player receipt or journal"):
        player.unliteral("{1,")


def test_an_option_flag_can_be_turned_off(setup, monkeypatch):
    w, server = setup
    _option_definitions(w, monkeypatch)
    server.rows[("setting", "display:shortprep")] = [1, 1, []]
    player.track(w, [("setting", "display:shortprep")])
    edit(w, lambda p: p.settings.update({"display:shortprep": 0}))

    player.apply(player.prepare(w))

    assert server.rows[("setting", "display:shortprep")] == [1, 0, []]
    assert not player.prepare(w).changes


def test_a_live_option_deeper_than_files_allow_is_refused(setup, monkeypatch):
    w, server = setup
    monkeypatch.setattr(w, "eval", lambda expr: Obj(9) if expr == "$prog_options" else ["verb_args"])
    deep = moolit.parse("{" * 101 + "}" * 101, max_depth=moolit.ANSWER_DEPTH)
    server.rows[("setting", "prog:verb_args")] = [1, deep, []]

    with pytest.raises(MooError, match=r"setting prog:verb_args: the live value nests .* more than 100 levels deep"
                                       r".*untrack --setting prog:verb_args"):
        player.track(w, [("setting", "prog:verb_args")])
    assert not player.paths(w)[0].exists()


def _merged():
    # ToastStunt keeps 1 and true apart; a lenient answer merges and marks them.
    return moolit.parse('[1 -> "on", true -> "yes"]', strict_keys=False)


def test_tracking_a_value_terramoo_cannot_write_names_the_field(setup):
    w, server = setup
    server.rows[("property", "prefs")] = [1, 1, 0, Obj(1), "rc", _merged()]

    with pytest.raises(MooError, match=r"player property prefs: the live value has the map keys 1 and true"
                                       r".*tmoo player untrack --property prefs"):
        player.track(w, [("property", "prefs")])
    assert not player.paths(w)[0].exists()


def test_a_tracked_value_that_becomes_unwritable_is_a_plan_problem_until_untracked(setup):
    w, server = setup
    server.rows[("property", "prefs")] = [1, 1, 0, Obj(1), "rc", "plain"]
    player.track(w, [("property", "prefs")])
    server.rows[("property", "prefs")][-1] = _merged()

    problems = player.prepare(w).problems

    assert len(problems) == 1
    assert "1 and true" in problems[0] and "tmoo player untrack --property prefs" in problems[0]
    player.untrack(w, [("property", "prefs")])
    assert not player.prepare(w).problems


def test_clearing_a_property_whose_inherited_value_cannot_be_written_completes(setup, monkeypatch):
    # The helper keeps the cleared row, with its inherited value, in its state.
    w, server = setup
    server.rows[("property", "prefs")] = [1, 0, 0, Obj(1), "rc", "mine"]
    player.track(w, [("property", "prefs")])
    player.stage_removal(w, [("property", "prefs")], "clear")
    original = server.call

    def call(world, helper, mode, *args, **kwargs):
        result = original(world, helper, mode, *args, **kwargs)
        if helper == "tmoo_player" and mode == "apply":
            server.rows[("property", "prefs")][-1] = _merged()
            server.state[6] = [1, deepcopy(server.rows[("property", "prefs")])]
            return server.part(deepcopy(server.state), kwargs.get("index", ""))
        return result

    monkeypatch.setattr(player, "call", call)
    player.apply(player.prepare(w))

    assert not player.paths(w)[2].exists()
    p = player.prepare(w)
    assert not p.problems and not p.changes


def test_recover_accept_live_stops_tracking_a_value_it_cannot_write(setup):
    w, server = setup
    server.rows[("property", "prefs")] = [1, 1, 0, Obj(1), "rc", "plain"]
    player.track(w, [("property", "prefs")])
    edit(w, lambda p: setattr(p.props[0], "value", "edited"))
    p = player.prepare(w)
    server.lose = "apply"
    with pytest.raises(MooError, match="lost response"):
        player.apply(p)
    server.rows[("property", "prefs")][-1] = _merged()

    message = player.recover(w, accept_live=True)

    assert "stopped tracking property:prefs" in message
    assert not player.paths(w)[2].exists()
    assert "property:prefs" not in player.receipt(w)["fields"]
    assert not player.load(w).props


def test_removal_guard_refuses_a_tracked_value_it_cannot_inspect(setup):
    w, server = setup
    server.rows[("property", "prefs")] = [1, 1, 0, Obj(1), "rc", "plain"]
    player.track(w, [("property", "prefs")])
    server.rows[("property", "prefs")][-1] = _merged()

    with pytest.raises(MooError, match=r"live player property prefs: .*cannot be checked before removal"
                                       r".*untrack --property prefs"):
        player.incoming_check(w, w.refs(), {"feature"})


def test_an_unreadable_live_field_names_itself_and_other_errors_pass_through(setup, monkeypatch):
    from terramoo.errors import AnswerError
    w, server = setup
    original = server.call

    def call(world, helper, mode, *args, **kwargs):
        if helper == "tmoo_player_read" and mode == "field" and args[0] == ["property", "prefs"]:
            raise AnswerError("cannot read the MOO's answer (too deep)")
        if helper == "tmoo_player_read" and mode == "field" and args[0] == ["property", "gone"]:
            raise MooError("the MOO closed the connection")
        return original(world, helper, mode, *args, **kwargs)

    monkeypatch.setattr(player, "call", call)
    with pytest.raises(MooError, match=r"player property prefs: the live value cannot be read .*"
                                       r"untrack --property prefs"):
        player.track(w, [("property", "prefs")])
    with pytest.raises(MooError, match=r"^the MOO closed the connection$"):
        player.track(w, [("property", "gone")])


def _unreadable(server, monkeypatch, selector, *, after_apply=False, with_result=True):
    """Make one field, and the toolbox's recorded result unless told not to,
    too deep for an answer: at once, or only once a field operation has run."""
    from terramoo.errors import AnswerError
    deep = [not after_apply]
    original = server.call

    def call(world, helper, mode, *args, **kwargs):
        if deep[0] and helper == "tmoo_player_read" and mode == "field" and args[0] == selector:
            raise AnswerError("cannot read the MOO's answer (value nests too deep)")
        if deep[0] and with_result and helper == "tmoo_player" and kwargs.get("index", "") != "[1..6]":
            raise AnswerError("cannot read the MOO's answer (value nests too deep)")
        result = original(world, helper, mode, *args, **kwargs)
        if helper == "tmoo_player" and mode == "apply":
            deep[0] = True
        return result

    monkeypatch.setattr(player, "call", call)


def test_a_clear_whose_result_cannot_be_read_recovers_and_keeps_unsent_edits(setup, monkeypatch):
    # The toolbox records the cleared row, with an inherited value too deep to read.
    w, server = setup
    server.rows[("property", "prefs")] = [1, 0, 0, Obj(1), "rc", "mine"]
    player.track(w, [("property", "prefs"), ("verb", "probe")])
    player.stage_removal(w, [("property", "prefs")], "clear")
    player.stage_removal(w, [("verb", "probe")], "remove")
    p = player.prepare(w)
    _unreadable(server, monkeypatch, ["property", "prefs"], after_apply=True)

    with pytest.raises(MooError, match="result cannot be read.*run tmoo player recover$"):
        player.apply(p)
    assert player.remote(w)[5] == "done"  # the header stays readable
    message = player.recover(w)

    assert "stopped tracking property:prefs (its live value cannot be read)" in message
    assert not player.paths(w)[2].exists()
    assert not player.remote(w)[3]
    assert "remove verb probe;" in player.paths(w)[0].read_text()  # never sent, still staged
    assert server.rows[("verb", "probe")][0] == 1


def test_an_unreadable_result_with_a_readable_field_recovers_without_accept_live(setup, monkeypatch):
    from terramoo.errors import AnswerError
    w, server = setup
    player.track(w, [("property", "description")])
    edit(w, lambda p: setattr(p.props[0], "value", "new"))
    p = player.prepare(w)
    original = server.call

    def call(world, helper, mode, *args, **kwargs):
        if helper == "tmoo_player" and kwargs.get("index") == "[7]":
            raise AnswerError("cannot read the MOO's answer (an anonymous object)")
        return original(world, helper, mode, *args, **kwargs)

    monkeypatch.setattr(player, "call", call)
    with pytest.raises(MooError, match="result cannot be read"):
        player.apply(p)

    assert "stopped tracking" not in player.recover(w)
    assert player.load(w).props[0].value == "new"
    assert not player.prepare(w).changes


def test_an_unsent_field_that_cannot_be_read_points_to_a_plain_recover(setup, monkeypatch):
    w, server = setup
    server.rows[("property", "prefs")] = [1, 1, 0, Obj(1), "rc", "plain"]
    player.track(w, [("property", "description"), ("property", "prefs")])
    edit(w, lambda p: [setattr(prop, "value", "edited") for prop in p.props])
    p = player.prepare(w)
    order = [f["key"] for f in p.fields]
    unsent = ["property", order[1].split(":", 1)[1]]
    _unreadable(server, monkeypatch, unsent, after_apply=True, with_result=False)

    with pytest.raises(MooError, match="run tmoo player recover, which keeps your other edits"):
        player.apply(p)
    player.recover(w)

    assert not player.paths(w)[2].exists()
    assert all(prop.value == "edited" for prop in player.load(w).props)


def test_a_step_that_never_ran_recovers_even_if_its_field_cannot_be_read(setup, monkeypatch):
    w, server = setup
    server.rows[("property", "prefs")] = [1, 1, 0, Obj(1), "rc", "plain"]
    player.track(w, [("property", "prefs")])
    edit(w, lambda p: setattr(p.props[0], "value", "edited"))
    p = player.prepare(w)
    original = server.call

    def refuse(world, helper, mode, *args, **kwargs):
        if helper == "tmoo_player" and mode == "apply":
            raise MooError("E_INVARG: player field changed; pull or reconcile before applying")
        return original(world, helper, mode, *args, **kwargs)

    monkeypatch.setattr(player, "call", refuse)
    with pytest.raises(MooError, match="player field changed"):
        player.apply(p)
    monkeypatch.setattr(player, "call", original)
    _unreadable(server, monkeypatch, ["property", "prefs"])

    player.recover(w)

    assert not player.paths(w)[2].exists()
    assert player.load(w).props[0].value == "edited"


@pytest.mark.parametrize("result, expected", [([42], 42), ([1, [1, 0, 0, Obj(1), "rc", "x"]], None), ([0, "E_PERM", "no"], None)])
def test_running_task_reads_a_task_id_only_while_the_callback_runs(setup, result, expected):
    w, server = setup
    server.state = [1, "w", 1, "token", 1, "running", result]
    assert player.running_task(w) == expected


def test_an_unreadable_field_during_an_operation_points_to_accept_live(setup, monkeypatch):
    w, server = setup
    player.paths(w)[2].parent.mkdir(parents=True, exist_ok=True)
    player.paths(w)[2].write_text("{}\n")
    _unreadable(server, monkeypatch, ["property", "prefs"])

    with pytest.raises(MooError, match="recover --accept-live, which stops tracking it"):
        player.read_field(w, ["property", "prefs"])


def test_recover_accept_live_drops_a_setting_that_breaks_a_file_rule(setup, monkeypatch):
    w, server = setup
    _option_definitions(w, monkeypatch)
    server.rows[("setting", "display:shortprep")] = [1, 0, []]
    player.track(w, [("setting", "display:shortprep")])
    edit(w, lambda p: p.settings.update({"display:shortprep": 1}))
    p = player.prepare(w)
    server.lose = "apply"
    with pytest.raises(MooError, match="lost response"):
        player.apply(p)
    server.rows[("setting", "display:shortprep")][1] = True  # written directly by in-MOO code

    message = player.recover(w, accept_live=True)

    assert "stopped tracking setting:display:shortprep" in message and "write 1 or 0" in message
    assert not player.paths(w)[2].exists()
    assert "display:shortprep" not in player.load(w).settings


@pytest.mark.parametrize("value", ['""', "{}", "[]"])
def test_an_empty_option_value_is_refused(value):
    with pytest.raises(MooError, match="write 0 to turn it off"):
        playerdef.parse(f'player\n  setting "build:create_flags" = {value};\nendplayer\n')


def test_a_field_whose_row_has_merged_keys_cannot_be_changed(setup):
    # The helper compares the exact live row, and a merged one lost an entry.
    w, server = setup
    server.rows[("property", "prefs")] = [1, 0, 1, Obj(1), "rc", _merged()]
    player.track(w, [("property", "prefs")])
    assert "property:prefs" in player.load(w).removals  # tracked as cleared
    edit(w, lambda p: (p.removals.clear(), p.props.append(PropDef("prefs", "mine", defined=False))))

    p = player.prepare(w)

    assert len(p.problems) == 1
    assert "cannot send back exactly" in p.problems[0] and "untrack --property prefs" in p.problems[0]


def test_an_unreadable_receipt_snapshot_stops_only_its_field(setup):
    w, server = setup
    server.rows[("property", "prefs")] = [1, 1, 0, Obj(1), "rc", "plain"]
    player.track(w, [("property", "description"), ("property", "prefs")])
    data = read_json(player.paths(w)[1])
    data["fields"]["property:prefs"]["snapshot"] = "{" * 300 + "}" * 300
    player.paths(w)[1].write_text(json_text(data))

    problems = player.prepare(w).problems

    assert len(problems) == 1 and "property:prefs: its receipt snapshot cannot be read" in problems[0]
    player.untrack(w, [("property", "prefs")])
    assert not player.prepare(w).problems


def test_removal_guard_walks_a_deep_value_that_lost_nothing(setup):
    w, server = setup
    server.rows[("property", "prefs")] = [1, 1, 0, Obj(1), "rc", "plain"]
    player.track(w, [("property", "prefs")])
    server.rows[("property", "prefs")][-1] = moolit.parse("{" * 150 + "}" * 150, max_depth=moolit.ANSWER_DEPTH)

    guard = player.incoming_check(w, w.refs(), {"feature"})

    assert guard[0] == server.state[2]


def test_drift_to_an_unwritable_value_says_to_change_it_on_the_moo(setup):
    w, server = setup
    server.rows[("property", "prefs")] = [1, 1, 0, Obj(1), "rc", "plain"]
    player.track(w, [("property", "prefs")])
    player.stage_removal(w, [("property", "prefs")], "remove")
    server.rows[("property", "prefs")][-1] = moolit.parse("{" * 150 + "}" * 150, max_depth=moolit.ANSWER_DEPTH)

    problems = player.prepare(w).problems

    assert len(problems) == 1
    assert "live drift" in problems[0] and "change it on the MOO" in problems[0]


@pytest.mark.parametrize("value", ["1", "0", '"+r"', "#5", "@feature", '{"this", "none", "this"}'])
def test_option_settings_accept_flags_strings_objects_and_lists(value):
    profile = playerdef.parse(f'player\n  setting "build:create_flags" = {value};\nendplayer\n')
    assert profile.settings["build:create_flags"] == moolit.parse(value, dialect=moolit.MOOR)
    assert playerdef.parse(playerdef.render(profile)) == profile


@pytest.mark.parametrize("live", [2, True, 1.0])
def test_tracking_a_live_option_that_breaks_a_file_rule_names_the_field(setup, monkeypatch, live):
    w, server = setup
    _option_definitions(w, monkeypatch)
    server.rows[("setting", "display:shortprep")] = [1, live, []]

    with pytest.raises(MooError, match=r"setting display:shortprep: the live value breaks a player.moo rule .*"
                                       r"untrack --setting display:shortprep"):
        player.track(w, [("setting", "display:shortprep")])


def test_a_cleared_row_with_an_unwritable_inherited_value_keeps_its_clear(setup):
    # Track, pull and accept-live never write the inherited value, so it does not count.
    w, server = setup
    server.rows[("property", "prefs")] = [1, 0, 1, Obj(1), "rc", _merged()]
    player.track(w, [("property", "prefs")])
    player.track(w, [("property", "prefs")], pull=True)
    assert "clear property prefs;" in player.paths(w)[0].read_text()

    server.rows[("property", "description")][-1] = "first"
    player.track(w, [("property", "description")])
    edit(w, lambda p: setattr(p.props[0], "value", "second"))
    p = player.prepare(w)
    server.lose = "apply"
    with pytest.raises(MooError, match="lost response"):
        player.apply(p)
    message = player.recover(w, accept_live=True)

    assert "stopped tracking" not in message
    assert "clear property prefs;" in player.paths(w)[0].read_text()


def test_removal_guard_refuses_a_cleared_row_with_merged_keys(setup):
    w, server = setup
    server.rows[("property", "prefs")] = [1, 0, 1, Obj(1), "rc", _merged()]
    player.track(w, [("property", "prefs")])

    with pytest.raises(MooError, match="cannot be checked before removal"):
        player.incoming_check(w, w.refs(), {"feature"})


def test_player_diff_prints_the_problems_that_kept_a_field_out(setup, monkeypatch, capsys):
    w, server = setup
    server.rows[("property", "prefs")] = [1, 1, 0, Obj(1), "rc", "plain"]
    player.track(w, [("property", "prefs")])
    server.rows[("property", "prefs")][-1] = _merged()
    monkeypatch.setattr(cli, "_world", lambda args: w)

    with pytest.raises(MooError, match="fix player problems"):
        cli.cmd_player(SimpleNamespace(action="diff"))

    out = capsys.readouterr().out
    assert "! property:prefs: the live value has the map keys 1 and true" in out


def test_player_diff_skips_a_drifted_unwritable_field_and_prints_why(setup, monkeypatch, capsys):
    w, server = setup
    server.rows[("property", "prefs")] = [1, 1, 0, Obj(1), "rc", "plain"]
    player.track(w, [("property", "prefs")])
    player.stage_removal(w, [("property", "prefs")], "remove")
    server.rows[("property", "prefs")][-1] = moolit.parse("{" * 150 + "}" * 150, max_depth=moolit.ANSWER_DEPTH)
    monkeypatch.setattr(cli, "_world", lambda args: w)

    with pytest.raises(MooError, match="fix player problems"):
        cli.cmd_player(SimpleNamespace(action="diff"))

    out = capsys.readouterr().out
    assert "+  remove property prefs;" in out
    assert "change it on the MOO" in out
