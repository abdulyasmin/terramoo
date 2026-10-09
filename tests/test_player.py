from copy import deepcopy
from types import SimpleNamespace

import pytest

from terramoo import cli, player, playerdef
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

    def call(self, world, helper, mode, *args):
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
                row = [1, action[1], []]
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
    def edited(world, helper, mode, *args):
        result = original(world, helper, mode, *args)
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
    def edited(world, helper, mode, *args):
        result = original(world, helper, mode, *args)
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
