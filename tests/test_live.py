"""A round trip against a real MOO, leaving nothing behind.

Skipped unless `TMOO_LIVE=host:port:player:password` names a programmer
account on a MOO you may scribble on (see testbeds/), e.g.

    TMOO_LIVE=127.0.0.1:17001:tester:tester uv run pytest tests/test_live.py
"""

import os

import pytest

from terramoo import cli
from terramoo.catalog import discover
from terramoo.moolit import Obj
from terramoo.world import World

LIVE = os.environ.get("TMOO_LIVE")
pytestmark = pytest.mark.skipif(not LIVE, reason="set TMOO_LIVE=host:port:player:password")

HALL = """\
object tmoo_test_hall
  name: "tmoo test hall"
  parent: $room
  flags: "r"

  property banner (flags: "rc") = {"purple", @tmoo_test_door, $room, 1.5, E_PERM, "q\\"uote"};
  override description = "A hall for testing.";

  verb bow (any none none) flags: "rd"
    player:tell("You bow.");
    return 1;
  endverb
endobject
"""

DOOR = """\
object tmoo_test_door
  name: "tmoo test door"
  parent: $exit
  location: @tmoo_test_hall

  override source = @tmoo_test_hall;
  override dest = @tmoo_test_hall;
endobject
"""

GENERIC = """\
object tmoo_test_generic
  name: "tmoo test generic"
  parent: $thing

  property kind (flags: "rc") = "generic";
endobject
"""

CHILD = """\
object tmoo_test_child
  name: "tmoo test child"
  parent: @tmoo_test_generic
endobject
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    host, port, player, password = LIVE.split(":", 3)
    monkeypatch.setenv("TMOO_ROOT", str(tmp_path))
    monkeypatch.setenv("TMOO_SECRET", password)
    monkeypatch.delenv("TMOO_WORLD", raising=False)
    cli.main(["init", "live", "--player", player, "--host", host, "--port", port, "--root", str(tmp_path)])
    cli.main(["bootstrap"])
    w = World.load(tmp_path, "live")
    # Teardown runs `apply --destroy`, which recycles every registry entry
    # without a file: only ever do that to a registry holding test objects.
    others = sorted(k for k in w.read_registry() if not k.startswith("tmoo_test_"))
    if others:
        w.close()
        pytest.skip(f"this player's registry manages real objects ({', '.join(others[:5])}); use a scratch account")
    yield w
    if (w.dir / ".packages/operation.json").exists():
        cli.main(["package", "recover"])
    if (w.dir / "packages.lock.json").exists():
        from terramoo.installation import Store
        from terramoo.updates import remove
        store = Store(w)
        active = [n for n, r in store.instances.items() if r["state"] == "prepared"]
        roots = [w.objects_dir / "packages" / n for n in active]
        tree = discover(w.objects_dir)
        for f in (*tree.objects, *tree.manifests):
            if not any(f.is_relative_to(root) for root in roots):
                f.unlink()
        if active:
            remove(Store(w), active, allow_modified=True)
    for f in discover(w.objects_dir).objects:
        if f.stem.startswith("tmoo_test_"):
            f.unlink()
    cli.main(["apply", "--destroy", "-y"])
    w.close()


def run(capsys, *argv):
    capsys.readouterr()
    try:
        cli.main(list(argv))
    except SystemExit as e:
        out = capsys.readouterr()
        raise AssertionError(f"tmoo {' '.join(argv)} exited {e.code}:\n{out.out}{out.err}") from None
    return capsys.readouterr().out


@pytest.fixture
def player_world(world):
    """Player tests use only the existing scratch-account testbed fixture."""
    from terramoo import player as pm
    original = world.eval('{is_clear_property(player, "description"), player.description}')
    features = world.eval('player.features')
    props = world.eval('properties(player)')
    vrbs = world.eval('verbs(player)')
    assert not any(p.startswith("tmoo_test_player_") for p in props)
    assert not any(v.startswith("tmoo_test_player_") for v in vrbs)
    prior_state = world.eval(f'"_terramoo_player_state" in properties({world.toolbox})')
    assert not prior_state, "scratch player has a preexisting player deployment; use a clean testbed"
    try:
        yield world
    finally:
        state = pm.remote(world)
        if state[3]:
            pm.call(world, "tmoo_player", "finish", state[3], 1)
        for feature in world.eval('player.features'):
            if feature not in features:
                world.eval(f'player:remove_feature({feature})')
        world.transport.set_prop("player", "description", original[1])
        if original[0]:
            world.eval('clear_property(player, "description")')
        names = world.eval('verbs(player)')
        for i in range(len(names), 0, -1):
            if names[i - 1].startswith("tmoo_test_player_"):
                world.eval(f'delete_verb(player, {i})')
        for prop in world.eval('properties(player)'):
            if prop.startswith("tmoo_test_player_"):
                world.eval(f'delete_property(player, "{prop}")')
        if world.eval(f'"_terramoo_player_state" in properties({world.toolbox})'):
            world.eval(f'delete_property({world.toolbox}, "_terramoo_player_state")')
        for path in pm.paths(world):
            path.unlink(missing_ok=True)


def test_player_partial_management_permissions_drift_and_clear(player_world, capsys):
    from terramoo import player as pm, playerdef
    from terramoo.errors import MooError
    from terramoo.model import PropDef, VerbDef
    from terramoo.moolit import Ref
    world = player_world
    assert world.eval('player.wizard') == 0
    run(capsys, "player", "track", "--property", "description")
    profile = pm.load(world)
    pm.forget(profile, "property", "description")
    profile.props += [PropDef("description", "Player test description", defined=False),
                      PropDef("tmoo_test_player_pref", 7)]
    profile.verbs += [VerbDef("tmoo_test_player_probe", ["return 42;"], ("any", "any", "any"), "rd")]
    pm.paths(world)[0].write_text(playerdef.render(profile))
    assert "applied 3 player change" in run(capsys, "player", "apply", "-y")
    assert world.eval('player.description') == "Player test description"
    assert world.eval('verb_info(player, "tmoo_test_player_probe")[2]') == "rd"
    with pytest.raises(MooError, match="already uses this alias"):
        pm.read_field(world, ["verb", "tmoo_test_player_probe extra_alias"])
    assert "no player changes" in run(capsys, "player", "plan")
    # The helper refuses protected reads and direct adoption, even for its owner.
    with pytest.raises(MooError):
        pm.read_field(world, ["property", "password"])
    op = [["register", "tmoo_test_player_identity", world.player, "unsafe"]]
    result = world.eval(world.helper("tmoo_apply", world.transport.serialize(op)))
    assert result[0][0] == 0
    assert "tmoo_test_player_identity" not in world.read_registry()
    # Recheck selected references inside the recycle task, after client preflight.
    target = world.eval('create($thing)')
    result = world.eval(world.helper("tmoo_apply", world.transport.serialize([
        ["register", "tmoo_test_player_guard", target, "test-guard"]])))
    assert result[0][0] == 1
    guard = pm.incoming_check(world, world.refs(), {"tmoo_test_player_guard"})
    world.transport.set_prop("player", "tmoo_test_player_pref", target)
    destroy = [["destroy", "tmoo_test_player_guard", target, "test-guard", guard]]
    result = world.eval(world.helper("tmoo_apply", world.transport.serialize(destroy)))
    assert result[0][0] == 0 and "player field changed" in result[0][2]
    assert world.eval(f'valid({target})')
    world.transport.set_prop("player", "tmoo_test_player_pref", 7)
    assert world.eval(world.helper("tmoo_apply", world.transport.serialize(destroy))) == [[1, 1]]
    world.transport.set_prop("player", "tmoo_test_player_pref", 8)
    assert pm.prepare(world).problems
    run(capsys, "player", "pull", "--property", "tmoo_test_player_pref")
    assert not pm.prepare(world).changes
    run(capsys, "player", "clear", "--property", "description")
    run(capsys, "player", "remove", "--verb", "tmoo_test_player_probe", "--property", "tmoo_test_player_pref")
    run(capsys, "player", "apply", "-y")
    assert world.eval('is_clear_property(player, "description")') == 1
    assert "tmoo_test_player_probe" not in world.eval('verbs(player)')
    assert "tmoo_test_player_pref" not in world.eval('properties(player)')


def test_player_lost_write_response_and_setter_adapter(player_world, capsys, monkeypatch):
    from terramoo import player as pm, playerdef
    from terramoo.errors import MooError
    world = player_world
    run(capsys, "player", "track", "--property", "description", "--setting", "aliases")
    profile = pm.load(world)
    pm.forget(profile, "property", "description")
    from terramoo.model import PropDef
    profile.props.append(PropDef("description", "After lost response", defined=False))
    pm.paths(world)[0].write_text(playerdef.render(profile))
    prepared = pm.prepare(world)
    original_eval = world.eval
    dropped = False
    def lose_response(expression):
        nonlocal dropped
        result = original_eval(expression)
        if ':tmoo_player("apply"' in expression and not dropped:
            dropped = True
            raise MooError("injected lost player response")
        return result
    monkeypatch.setattr(world, "eval", lose_response)
    with pytest.raises(MooError, match="lost player response"):
        pm.apply(prepared)
    monkeypatch.setattr(world, "eval", original_eval)
    assert pm.remote(world)[5] == "done"
    pm.recover(world)
    assert not pm.prepare(world).changes
    assert world.eval('player.description') == "After lost response"
    # A privileged core-owned property is accessed only by its selected adapter.
    assert "aliases" in pm.load(world).settings
    with pytest.raises(MooError, match="protected"):
        pm.read_field(world, ["property", "aliases"])


def test_player_core_settings_setters_as_nonwizard(player_world, capsys):
    from terramoo import player as pm, playerdef
    from terramoo.errors import MooError
    world = player_world
    with pytest.raises(MooError, match="unsupported player option"):
        pm.read_field(world, ["setting", "display:tmoo_nonexistent_option"])
    selected = ["aliases", "gender", "home", "linelen", "pagelen", "edit:local", "display:shortprep",
                "prog:list_no_numbers", "build:bi_create"]
    # Restore the scratch account's settings through the same core adapters.
    original = {name: pm.read_field(world, ["setting", name])[1] for name in selected}
    room = world.eval('create($room)')
    try:
        arguments = [arg for name in selected for arg in ("--setting", name)]
        run(capsys, "player", "track", *arguments)
        profile = pm.load(world)
        profile.settings.update(aliases=[*original["aliases"], "tmoo_test_player_alias"],
                                gender="female" if original["gender"] != "female" else "male",
                                home=room,
                                linelen=81 if original["linelen"] != 81 else 82,
                                pagelen=23 if original["pagelen"] != 23 else 24)
        profile.settings["edit:local"] = int(not original["edit:local"])
        profile.settings["display:shortprep"] = int(not original["display:shortprep"])
        profile.settings["prog:list_no_numbers"] = int(not original["prog:list_no_numbers"])
        profile.settings["build:bi_create"] = int(not original["build:bi_create"])
        pm.paths(world)[0].write_text(playerdef.render(profile))
        run(capsys, "player", "apply", "-y")
        assert not pm.prepare(world).changes
        assert "tmoo_test_player_alias" in world.eval('player.aliases')
        assert world.eval('player.linelen') == profile.settings["linelen"]
        assert world.eval('player.gender') == profile.settings["gender"]
        assert world.eval('player.home') == room
    finally:
        state = pm.remote(world)
        if state[3]:
            pm.call(world, "tmoo_player", "finish", state[3], 1)
        for name, value in original.items():
            if ":" in name:
                prefix, option = name.split(":")
                world.eval(f'player:set_{prefix}_option("{option}", {world.transport.serialize(value)})')
            else:
                setter = {"linelen": "set_linelength", "pagelen": "set_pagelength"}.get(name, "set_" + name)
                extra = ", 1" if name == "linelen" else ""
                world.eval(f'player:{setter}({world.transport.serialize(value)}{extra})')
        world.eval(f'recycle({room})')


@pytest.mark.parametrize("private_callback", [False, True])
def test_player_recovery_refuses_a_still_suspended_callback(player_world, private_callback):
    import time
    from terramoo import player as pm
    from terramoo.errors import MooError
    world = player_world
    admin = None
    if private_callback:
        from terramoo.transport.telnet import Telnet
        host, port, _, _ = LIVE.split(":", 3)
        if host != "127.0.0.1" or port not in {"17001", "17002", "17003"}:
            pytest.skip("private callback fixture requires a local testbed wizard")
        name, password = ("Wizard", "wizard") if port == "17003" else ("wiz", "wiz")
        admin = Telnet(host, int(port), name, password)
    feature = world.eval('create($thing)')
    other = world.eval('create($thing)')
    task = None
    try:
        world.eval(f'add_property({feature}, "feature_ok", 1, {{player, "r"}})')
        world.transport.install_verb(feature, "feature_add", ['suspend(30);', 'return 1;'])
        if admin:
            admin.eval(f'set_verb_info({feature}, "feature_add", {{player, "rxd", "feature_add"}})')
        state = pm.remote(world)
        pm.call(world, "tmoo_player", "begin", "test-recovery", state[2], "test-token", world.read_registry().revision,
                pm.call(world, "tmoo_packages", "read")[2])
        request = world.helper("tmoo_player", '"apply"', '"test-token"', '1',
            world.transport.serialize(["feature", feature]), '{1, 0}', '{"set"}', '{}')
        world.transport.install_verb(world.player, "tmoo_test_player_background", [
            'fork task (0)', request + ';', 'endfork', 'return task;'])
        task = world.eval('player:tmoo_test_player_background()')
        deadline = time.monotonic() + 5
        while pm.remote(world)[5] != "running" and time.monotonic() < deadline:
            time.sleep(.05)
        assert pm.remote(world)[5] == "running"
        if admin and port != "17003":
            # mooR currently reports the task's original permissions here.
            assert task not in [entry[0] for entry in world.eval('queued_tasks()')]
        with pytest.raises(MooError, match="still running"):
            pm.call(world, "tmoo_player", "finish", "test-token", 1)
        result = world.eval(world.helper("tmoo_apply", world.transport.serialize([
            ["register", "tmoo_test_player_blocked", other, "nonce"]])))
        assert result[0][0] == 0
        assert "tmoo_test_player_blocked" not in world.read_registry()
        (admin or world).eval(f'kill_task({task})')
        task = None
        pm.call(world, "tmoo_player", "finish", "test-token", 1)
        assert not pm.remote(world)[3]
    finally:
        if task is not None:
            (admin or world).eval(f'kill_task({task})')
        state = pm.remote(world)
        if state[3]:
            pm.call(world, "tmoo_player", "finish", state[3], 1)
        if feature in world.eval('player.features'):
            world.eval(f'player:remove_feature({feature})')
        world.eval(f'recycle({feature})')
        world.eval(f'recycle({other})')
        if admin:
            admin.close()


def test_player_features_from_multiple_instances_and_namespace_migration(player_world, tmp_path, capsys):
    from terramoo import player as pm, playerdef
    from terramoo.errors import MooError
    from terramoo.moolit import Ref
    from terramoo.storage import toml_text
    world = player_world
    source = tmp_path / "features"
    module = source / "commands"
    module.mkdir(parents=True)
    (source / "package.toml").write_text(toml_text({"schema_version": 1, "name": "features", "version": "1", "modules": ["commands"]}))
    (module / "module.toml").write_text('schema_version = 1\nname = "commands"\n')
    (module / "feature.moo").write_text('''object feature
  name: "Test player feature"
  parent: $thing
  property feature_ok (flags: "r") = 1;
  verb feature_add (this none this) flags: "rxd"
    player.tmoo_test_player_hooks = player.tmoo_test_player_hooks + 1;
    return 1;
  endverb
endobject
''')
    world.eval('add_property(player, "tmoo_test_player_hooks", 0, {player, ""})')
    original_features = world.eval('player.features')
    for instance in ("tmoo_test_a", "tmoo_test_b"):
        run(capsys, "package", "install", str(source), "--as", instance)
        run(capsys, "apply", "--package", instance, "-y")
    profile = playerdef.PlayerDef(features=[Ref("@", "tmoo_test_a__feature"), Ref("@", "tmoo_test_b__feature")])
    pm.paths(world)[0].write_text(playerdef.render(profile))
    run(capsys, "player", "apply", "-y")
    assert world.eval('player.tmoo_test_player_hooks') == 2
    run(capsys, "player", "apply", "-y")
    assert world.eval('player.tmoo_test_player_hooks') == 2
    refs = world.refs()
    with pytest.raises(MooError, match="still refers"):
        pm.incoming_check(world, refs, {"tmoo_test_a__feature"})
    # Removing a package's files first must still allow detaching its live feature.
    run(capsys, "package", "remove", "tmoo_test_a", "-y")
    run(capsys, "player", "detach", "--feature", "@tmoo_test_a__feature")
    run(capsys, "player", "apply", "-y")
    run(capsys, "apply", "--package", "tmoo_test_a", "--destroy", "-y")
    run(capsys, "package", "migrate", "tmoo_test_b", "--namespace", "tmoo_test_c", "-y")
    assert pm.load(world).features == [Ref("@", "tmoo_test_c__feature")]
    assert not pm.prepare(world).changes
    active = world.eval('player.features')
    assert all(f in active for f in original_features)
    assert refs.registry["tmoo_test_b__feature"] in active
    run(capsys, "player", "detach", "--feature", "@tmoo_test_c__feature")
    run(capsys, "player", "apply", "-y")



def test_package_instances_update_and_remove_independently(world, tmp_path, capsys, monkeypatch):
    from types import SimpleNamespace
    from terramoo.deployment import prepare
    from terramoo.errors import MooError
    from terramoo.ownership import recover
    from terramoo.storage import toml_text
    source = tmp_path / "package"
    module = source / "rooms"
    module.mkdir(parents=True)
    (source / "package.toml").write_text(toml_text({"schema_version": 1, "name": "fixture", "version": "1.0.0", "modules": ["rooms"]}))
    (module / "module.toml").write_text('schema_version = 1\nname = "rooms"\n')
    (module / "generic.moo").write_text('object generic\n  name: "Generic"\n  parent: $thing\nendobject\n')
    definition = 'object item\n  name: "Original"\n  parent: @generic\nendobject\n'
    (module / "item.moo").write_text(definition)
    for instance in ("tmoo_test_north", "tmoo_test_south"):
        run(capsys, "package", "install", str(source), "--as", instance)
        run(capsys, "apply", "--package", instance, "-y")
    before = world.read_registry()
    assert before["tmoo_test_north__item"] != before["tmoo_test_south__item"]
    assert "no changes" in run(capsys, "plan")
    (module / "item.moo").write_text(definition.replace('"Original"', '"Updated"'))
    run(capsys, "package", "update", "tmoo_test_north")
    run(capsys, "apply", "--package", "tmoo_test_north", "-y")
    assert world.eval(f'{before["tmoo_test_north__item"]}.name') == "Updated"
    assert world.eval(f'{before["tmoo_test_south__item"]}.name') == "Original"
    run(capsys, "package", "remove", "tmoo_test_north", "-y")
    proposal = prepare(world, SimpleNamespace(module=[], package=["tmoo_test_north"]))
    original_eval = world.eval
    dropped = False

    def lose_response(expression):
        nonlocal dropped
        result = original_eval(expression)
        if '"destroy"' in expression and not dropped:
            dropped = True
            raise MooError("injected lost deletion response")
        return result

    monkeypatch.setattr(world, "eval", lose_response)
    with pytest.raises(MooError, match="lost deletion response"):
        proposal.run(destroy=True)
    monkeypatch.setattr(world, "eval", original_eval)
    recover(world)
    run(capsys, "apply", "--package", "tmoo_test_north", "--destroy", "-y")
    after = world.read_registry()
    assert "tmoo_test_north__item" not in after
    assert after["tmoo_test_south__item"] == before["tmoo_test_south__item"]
    assert "no changes" in run(capsys, "plan", "--package", "tmoo_test_south")
    run(capsys, "package", "install", str(source), "--as", "tmoo_test_north")
    run(capsys, "apply", "--package", "tmoo_test_north", "-y")
    replaced = world.read_registry()
    assert replaced.generations["tmoo_test_north__item"] != before.generations["tmoo_test_north__item"]
    assert replaced["tmoo_test_south__item"] == before["tmoo_test_south__item"]
    run(capsys, "package", "rename", "tmoo_test_south", "tmoo_test_east")
    assert "no changes" in run(capsys, "plan", "--package", "tmoo_test_east")


def test_import_migrate_and_guard_pending_pull(world, tmp_path, capsys, monkeypatch):
    from terramoo.errors import MooError
    from terramoo.installation import Store
    from terramoo import imports, migrations
    from terramoo.ownership import recover
    from terramoo.storage import toml_text
    source = tmp_path / "source"
    module = source / "rooms"
    module.mkdir(parents=True)
    (source / "package.toml").write_text(toml_text({"schema_version": 1, "name": "fixture", "version": "1", "modules": ["rooms"]}))
    (module / "module.toml").write_text('schema_version = 1\nname = "rooms"\n')
    text = 'object item\n  name: "Imported"\n  parent: $thing\nendobject\n'
    (module / "item.moo").write_text(text)
    directory = world.objects_dir / "legacy"
    directory.mkdir()
    (directory / "module.toml").write_text('schema_version = 1\nname = "legacy"\n')
    existing = directory / "tmoo_test_existing.moo"
    existing.write_text(text.replace("object item", "object tmoo_test_existing"))
    run(capsys, "apply", "-y")
    original = world.read_registry()["tmoo_test_existing"]
    mapping = tmp_path / "mapping.toml"
    mapping.write_text('schema_version = 1\n[objects]\nitem = "tmoo_test_existing"\n')
    store = Store(world)
    proposed, mapping_values = imports.prepare(store, source, "tmoo_test_imported", mapping)
    original_eval = world.eval

    def lose_import_response(expression):
        result = original_eval(expression)
        if 'tmoo_packages("import"' in expression:
            raise MooError("injected lost import response")
        return result

    monkeypatch.setattr(world, "eval", lose_import_response)
    with pytest.raises(MooError, match="lost import response"):
        imports.execute(store, proposed, mapping_values)
    monkeypatch.setattr(world, "eval", original_eval)
    recover(world)
    assert world.read_registry()["tmoo_test_existing"] == original
    assert not existing.exists()
    run(capsys, "apply", "--package", "tmoo_test_imported", "-y")
    store = Store(world)
    proposal = migrations.prepare(store, {"tmoo_test_existing": "tmoo_test_renamed"})
    original_eval = world.eval
    dropped = False

    def lose_response(expression):
        nonlocal dropped
        result = original_eval(expression)
        if '"rename"' in expression and not dropped:
            dropped = True
            raise MooError("injected lost rename response")
        return result

    monkeypatch.setattr(world, "eval", lose_response)
    with pytest.raises(MooError, match="lost rename response"):
        migrations.execute(store, proposal)
    monkeypatch.setattr(world, "eval", original_eval)
    recover(world)
    assert world.read_registry()["tmoo_test_renamed"] == original
    run(capsys, "package", "update", "tmoo_test_imported")
    assert "tmoo_test_renamed" in world.load_files()
    run(capsys, "apply", "--package", "tmoo_test_imported", "-y")
    path = world.file_for("tmoo_test_renamed")
    path.unlink()
    run(capsys, "pull", "--package", "tmoo_test_imported")
    assert path.exists()
    (module / "item.moo").write_text(text.replace('"Imported"', '"Updated"'))
    run(capsys, "package", "update", "tmoo_test_imported")
    with pytest.raises(SystemExit):
        cli.main(["pull", "--package", "tmoo_test_imported"])
    assert world.load_files()["tmoo_test_renamed"].name == "Updated"
    run(capsys, "apply", "--package", "tmoo_test_imported", "-y")
    run(capsys, "package", "migrate", "tmoo_test_imported", "--namespace", "tmoo_test_final", "-y")
    assert world.read_registry()["tmoo_test_final__item"] == original
    run(capsys, "apply", "--package", "tmoo_test_imported", "-y")
    assert "no changes" in run(capsys, "plan")
    run(capsys, "rename-key", "tmoo_test_final__item", "tmoo_test_final__Item")
    assert world.read_registry()["tmoo_test_final__Item"] == original
    run(capsys, "apply", "--package", "tmoo_test_imported", "-y")
    assert world.file_for("tmoo_test_final__Item").name == "tmoo_test_final__Item.moo"


def test_module_cycle_recovers_created_identities_and_checks_incoming_removal(world, capsys, monkeypatch):
    from types import SimpleNamespace
    from terramoo.deployment import prepare
    from terramoo.errors import MooError
    from terramoo.ownership import recover
    for module, other in (("one", "two"), ("two", "one")):
        directory = world.objects_dir / module / "nested"
        directory.mkdir(parents=True)
        (directory.parent / "module.toml").write_text(f'schema_version = 1\nname = "{module}"\nreferences = ["{other}"]\n')
        (directory / f"tmoo_test_{module}.moo").write_text(
            f'object tmoo_test_{module}\n name: "{module}"\n parent: $thing\n'
            f' property peer (flags: "rc") = @tmoo_test_{other};\nendobject\n')
    proposal = prepare(world, SimpleNamespace(module=["one"], package=[]))
    original_eval = world.eval
    dropped = False

    def lose_response(expression):
        nonlocal dropped
        result = original_eval(expression)
        if '"create"' in expression and not dropped:
            dropped = True
            raise MooError("injected lost create response")
        return result

    monkeypatch.setattr(world, "eval", lose_response)
    with pytest.raises(MooError, match="lost create response"):
        proposal.run()
    monkeypatch.setattr(world, "eval", original_eval)
    before = world.read_registry()
    recover(world)
    run(capsys, "apply", "--module", "one", "-y")
    after = world.read_registry()
    assert before == after
    assert world.eval(f'{after["tmoo_test_one"]}.peer') == after["tmoo_test_two"]
    assert world.eval(f'{after["tmoo_test_two"]}.peer') == after["tmoo_test_one"]
    for key in ("tmoo_test_one", "tmoo_test_two"):
        world.file_for(key).unlink()
    with pytest.raises(SystemExit):
        cli.main(["apply", "--module", "one", "--destroy", "-y"])
    assert world.read_registry() == after
    run(capsys, "package", "recover")
    run(capsys, "apply", "--module", "one", "--module", "two", "--destroy", "-y")
    assert not world.read_registry()

def stamp(world, obj):
    """The object's own generation stamp as the helper reads it."""
    return world.eval(f'{world.toolbox}:tmoo_generation("read", {obj})')


def test_register_helper_stamps_a_parent_whose_child_is_managed(world):
    # A hierarchy defines a property once: registering the parent after the
    # child must take over the child's definition without losing its stamp.
    parent = world.eval("create($thing)")
    grandchild = Obj(-1)
    try:
        create = [["create", "tmoo_test_child", Obj(-1), "", parent, "child of unmanaged", "child-generation"]]
        result = world.eval(world.helper("tmoo_apply", world.transport.serialize(create)))
        assert result[0][0] == 1
        child = result[0][1]
        grandchild = world.eval(f"create({child})")
        register = [["register", "tmoo_test_generic", parent, "parent-generation"]]
        result = world.eval(world.helper("tmoo_apply", world.transport.serialize(register)))
        assert result == [[1, parent]]
        assert stamp(world, parent) == "parent-generation"
        assert stamp(world, child) == "child-generation"
        assert stamp(world, grandchild) == ""
        assert world.eval(f'"_terramoo_generation" in properties({parent})')
        assert not world.eval(f'"_terramoo_generation" in properties({child})')
        # Both bindings still verify, so a mutation on each goes through.
        ops = [
            ["name", "tmoo_test_generic", parent, "parent-generation", "generic renamed"],
            ["name", "tmoo_test_child", child, "child-generation", "child renamed"],
        ]
        result = world.eval(world.helper("tmoo_apply", world.transport.serialize(ops)))
        assert result == [[1, 1], [1, 1]]
    finally:
        world.eval(f"valid({grandchild}) ? recycle({grandchild}) | 0")


def test_managed_child_of_a_managed_parent_round_trips(world, capsys):
    (world.objects_dir / "tmoo_test_generic.moo").write_text(GENERIC)
    (world.objects_dir / "tmoo_test_child.moo").write_text(CHILD)
    assert "applied" in run(capsys, "apply", "-y")
    assert run(capsys, "plan").strip() == "no changes"
    reg = world.read_registry()
    generic, child = reg["tmoo_test_generic"], reg["tmoo_test_child"]
    assert world.eval(f"parent({child})") == generic
    assert stamp(world, generic) == reg.generations["tmoo_test_generic"]
    assert stamp(world, child) == reg.generations["tmoo_test_child"]
    assert stamp(world, child) != stamp(world, generic)

    # The inherited stamp stays out of the child's file.
    run(capsys, "pull")
    assert (world.objects_dir / "tmoo_test_generic.moo").read_text() == GENERIC
    assert (world.objects_dir / "tmoo_test_child.moo").read_text() == CHILD

    # Reparenting away from the managed generic drops the inherited property;
    # the child must keep its own stamp, and get it back when it returns.
    thing = world.eval("$thing")
    (world.objects_dir / "tmoo_test_child.moo").write_text(CHILD.replace("@tmoo_test_generic", "$thing"))
    assert "chparent" in run(capsys, "plan")
    run(capsys, "apply", "-y")
    assert run(capsys, "plan").strip() == "no changes"
    assert world.eval(f"parent({child})") == thing
    assert stamp(world, child) == reg.generations["tmoo_test_child"]
    (world.objects_dir / "tmoo_test_child.moo").write_text(CHILD)
    run(capsys, "apply", "-y")
    assert run(capsys, "plan").strip() == "no changes"
    assert world.eval(f"parent({child})") == generic
    assert stamp(world, child) == reg.generations["tmoo_test_child"]
    assert stamp(world, generic) == reg.generations["tmoo_test_generic"]
    assert world.read_registry() == reg

    # A lost child is recreated under its managed parent.
    world.eval(f"recycle({child})")
    assert "is gone from the MOO" in run(capsys, "plan")
    run(capsys, "apply", "-y")
    assert run(capsys, "plan").strip() == "no changes"
    new_child = world.read_registry()["tmoo_test_child"]
    assert world.eval(f"parent({new_child})") == generic
    assert stamp(world, new_child) == world.read_registry().generations["tmoo_test_child"]



@pytest.mark.parametrize("key", ["tmoo_test_occupied", "TMOO_TEST_OCCUPIED"])
def test_register_helper_refuses_to_rebind_an_occupied_key(world, key):
    create = [["create", "tmoo_test_occupied", Obj(-1), "", world.eval("$thing"), "occupied key test", "occupied-generation"]]
    result = world.eval(world.helper("tmoo_apply", world.transport.serialize(create)))
    assert result[0][0] == 1
    reg = world.read_registry()
    fresh = world.eval("create($thing)")
    try:
        assert fresh not in reg.values()
        register = [["register", key, fresh, "fresh-generation"]]
        result = world.eval(world.helper("tmoo_apply", world.transport.serialize(register)))
        assert result[0][0:2] == [0, "E_INVARG"]
        assert world.read_registry() == reg
        assert world.eval(f"valid({fresh})")
    finally:
        # Restore the original binding even if a broken helper rebound it.
        world.transport.set_prop(
            world.toolbox,
            "registry",
            [list(reg), list(reg.values()), [reg.generations[k] for k in reg]],
        )
        world.eval(f"valid({fresh}) ? recycle({fresh}) | 0")


@pytest.mark.parametrize("already_gone", [False, True])
def test_destroy_helper_is_idempotent(world, already_gone):
    key = "tmoo_test_destroy"
    create = [["create", key, Obj(-1), "", world.eval("$thing"), "destroy test", "destroy-generation"]]
    result = world.eval(world.helper("tmoo_apply", world.transport.serialize(create)))
    assert result[0][0] == 1
    reg = world.read_registry()
    obj = reg.pop(key)
    if already_gone:
        world.eval(f"recycle({obj})")
    destroy = [["destroy", key, obj, "destroy-generation"]]
    for _ in range(2):
        result = world.eval(world.helper("tmoo_apply", world.transport.serialize(destroy)))
        assert result == [[1, 1]]
        assert not world.eval(f"valid({obj})")
        assert world.read_registry() == reg


def test_destroy_helper_rejects_a_changed_registration(world):
    key = "tmoo_test_changed"
    create = [["create", key, Obj(-1), "", world.eval("$thing"), "changed registration test", "changed-generation"]]
    result = world.eval(world.helper("tmoo_apply", world.transport.serialize(create)))
    assert result[0][0] == 1
    reg = world.read_registry()
    stale = world.eval("create($thing)")
    try:
        destroy = [["destroy", key, stale, "changed-generation"]]
        result = world.eval(world.helper("tmoo_apply", world.transport.serialize(destroy)))
        assert result[0][0:2] == [0, "E_INVARG"]
        assert world.read_registry() == reg
        assert world.eval(f"valid({reg[key]}) && valid({stale})")
    finally:
        world.eval(f"valid({stale}) ? recycle({stale}) | 0")


def test_destroy_preserves_recycle_callback_registry_changes(world):
    key, survivor_key = "tmoo_test_destroy", "tmoo_test_survivor"
    reg = world.read_registry()
    old = world.eval("create($thing)")
    survivor = world.eval("create($thing)")
    after = world.eval("create($thing)")
    try:
        serialize = world.transport.serialize
        callback = [["register", survivor_key, survivor, "survivor-generation"]]
        code = [f"return {world.toolbox}:tmoo_apply({serialize(callback)});"]
        setup = [
            ["register", key, old, "old-generation"],
            ["addverb", key, old, "old-generation", [world.player, "xd", "recycle"], ["this", "none", "this"], code],
        ]
        result = world.eval(world.helper("tmoo_apply", serialize(setup)))
        assert result == [[1, old], [1, 1]]
        ops = [
            ["destroy", key.upper(), old, "old-generation"],
            ["destroy", key, old, "old-generation"],
            ["register", "tmoo_test_after", after, "after-generation"],
        ]
        result = world.eval(world.helper("tmoo_apply", serialize(ops)))
        assert result == [[1, 1], [1, 1], [1, after]]
        assert world.read_registry() == {**reg, survivor_key: survivor, "tmoo_test_after": after}
        assert not world.eval(f"valid({old})")
        assert world.eval(f"valid({survivor})")
    finally:
        # Also clean up objects whose registration was lost by a broken helper.
        world.eval(f"valid({old}) ? recycle({old}) | 0")
        world.eval(f"valid({survivor}) ? recycle({survivor}) | 0")
        world.eval(f"valid({after}) ? recycle({after}) | 0")
        world.transport.set_prop(
            world.toolbox,
            "registry",
            [list(reg), list(reg.values()), [reg.generations[k] for k in reg]],
        )


def test_round_trip(world, capsys):
    (world.objects_dir / "tmoo_test_hall.moo").write_text(HALL)
    (world.objects_dir / "tmoo_test_door.moo").write_text(DOOR)
    assert "applied" in run(capsys, "apply", "-y")
    assert run(capsys, "plan").strip() == "no changes"

    # The exit is linked into its room, and values survive the trip.
    reg = world.read_registry()
    hall, door = reg["tmoo_test_hall"], reg["tmoo_test_door"]
    assert world.eval(f"{door} in {hall}.exits")
    assert world.eval(f"{hall}.banner")[1] == door

    # The helper itself preserves registry uniqueness if a stale client tries
    # to register an object already managed under another key.
    duplicate = [["register", "tmoo_test_duplicate", hall, "duplicate-generation"]]
    result = world.eval(world.helper("tmoo_apply", world.transport.serialize(duplicate)))
    assert result[0][0] == 0
    assert world.read_registry() == reg

    # pull writes back exactly what was applied.
    run(capsys, "pull")
    assert (world.objects_dir / "tmoo_test_hall.moo").read_text() == HALL
    assert (world.objects_dir / "tmoo_test_door.moo").read_text() == DOOR

    # An edit shows up in diff and plan, and applies.
    (world.objects_dir / "tmoo_test_hall.moo").write_text(HALL.replace("You bow.", "You bow low."))
    assert "+    player:tell(\"You bow low.\");" in run(capsys, "diff")
    assert "verbcode @tmoo_test_hall.bow" in run(capsys, "plan")
    run(capsys, "apply", "-y")
    assert world.eval(f"verb_code({hall}, \"bow\")") == ['player:tell("You bow low.");', "return 1;"]

    # A compile error fails that op and leaves the old code.
    (world.objects_dir / "tmoo_test_hall.moo").write_text(HALL.replace('player:tell("You bow.");', "player:tell(;"))
    with pytest.raises(AssertionError, match="compile"):
        run(capsys, "apply", "-y")
    assert world.eval(f"verb_code({hall}, \"bow\")")[0] == 'player:tell("You bow low.");'
    (world.objects_dir / "tmoo_test_hall.moo").write_text(HALL)
    run(capsys, "apply", "-y")

    # An object recycled behind our back is recreated from its file.
    world.eval(f"recycle({door})")
    out = run(capsys, "plan")
    assert "is gone from the MOO" in out
    run(capsys, "apply", "-y")
    assert run(capsys, "plan").strip() == "no changes"
