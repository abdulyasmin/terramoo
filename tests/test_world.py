from types import SimpleNamespace

import pytest

from terramoo.errors import MooError
from terramoo.model import ObjectDef
from terramoo.moolit import Obj
from terramoo.world import (
    DEFAULT_IGNORE_PROPS,
    HELPER_VERSION_PROP,
    HELPER_VERBS,
    TOOLBOX_NAME,
    World,
    registry_value,
)


def make_world(tmp_path, config):
    d = tmp_path / "worlds" / "test"
    (d / "objects").mkdir(parents=True)
    (d / "world.toml").write_text(config)
    return World.load(tmp_path, None)


def test_load_reads_generated_core_ignore_and_keep_settings(tmp_path):
    w = make_world(
        tmp_path,
        '''player = "alice"

[connection]
host = "moo.example"

[core]
toolbox_parent = "$thing"
ignore_props = ["session_cache"]
keep_props = ["history"]
''',
    )
    assert "session_cache" in w.ignore_props
    assert "history" not in w.ignore_props
    assert w.ignore_props >= DEFAULT_IGNORE_PROPS - {"history"}


@pytest.mark.parametrize("section", ["", "[core]\n"])
def test_mixed_case_keep_props_survives_pull_and_plan(tmp_path, monkeypatch, section):
    from terramoo import cli, export, plan
    from terramoo.model import PropDef
    from terramoo.refs import Refs

    w = make_world(tmp_path, 'player = "alice"\n' + section + '''
ignore_props = ["HISTORY", "Session_Cache", "Temporary"]
keep_props = ["History", "TEMPORARY"]
''')
    player, hall = Obj(1), Obj(10)
    refs = Refs(player=player, registry={"hall": hall})
    record = [hall, "Hall", Obj(2), Obj(-1), player, "", [
        ["History", 1, player, "rc", "42"],
        ["Temporary", 1, player, "rc", "7"],
        ["SESSION_CACHE", 1, player, "rc", "9"],
    ], []]
    monkeypatch.setattr(w, "helper", lambda *args: "export")
    monkeypatch.setattr(w, "eval", lambda expression: [record])
    w._transport = SimpleNamespace()
    w._helper_version_checked = True
    expected = ObjectDef(key="hall", name="Hall", parent=Obj(2),
                         props=[PropDef("History", 42), PropDef("Temporary", 7)])
    w.write_file(expected)

    live = export.export(w, refs, ["hall"])
    assert live == {"hall": expected}
    assert plan.build(w.load_files(), live, refs).empty
    assert cli._write_exports(w, refs, ["hall"]) == 1
    assert w.load_files() == {"hall": expected}
    assert w.ignore_props == (DEFAULT_IGNORE_PROPS | {"session_cache"}) - {"history"}


def test_generation_property_cannot_be_enabled_in_object_files(tmp_path):
    w = make_world(
        tmp_path,
        'player = "alice"\nkeep_props = ["_terramoo_generation"]\n',
    )

    assert "_terramoo_generation" in w.ignore_props


def test_load_files_rejects_a_key_that_does_not_match_its_filename(tmp_path):
    w = make_world(tmp_path, 'player = "alice"\n')
    (w.objects_dir / "hall.moo").write_text(
        'object courtyard\n  name: "Courtyard"\n  parent: $room\nendobject\n'
    )
    with pytest.raises(MooError, match="file is named 'hall' but declares object 'courtyard'"):
        w.load_files()


def test_load_files_rejects_a_non_identifier_filename(tmp_path):
    w = make_world(tmp_path, 'player = "alice"\n')
    (w.objects_dir / "café.moo").write_text(
        'object cafe\n  name: "Café"\n  parent: $room\nendobject\n'
    )

    with pytest.raises(MooError, match="ASCII identifier"):
        w.load_files()


def test_load_files_rejects_case_collisions(tmp_path, monkeypatch):
    w = make_world(tmp_path, 'player = "alice"\n')

    class Source:
        def __init__(self, key):
            self.key = key
            self.stem = key

        def read_text(self):
            return f'object {self.key}\n  name: "Hall"\n  parent: $room\nendobject\n'

        def relative_to(self, root):
            return f"worlds/test/objects/{self.key}.moo"

        def __lt__(self, other):
            return self.key < other.key

    sources = [Source("hall"), Source("Hall")]
    objects_dir = SimpleNamespace(glob=lambda pattern: sources)
    monkeypatch.setattr(World, "objects_dir", property(lambda self: objects_dir))
    with pytest.raises(MooError, match="differ only in case"):
        w.load_files()


def test_load_files_wraps_malformed_object_input_with_its_path(tmp_path):
    w = make_world(tmp_path, 'player = "alice"\n')
    (w.objects_dir / "broken.moo").write_text('object broken\n  name: "Broken"\nendobject\n')

    error = r"worlds/test/objects/broken\.moo: object broken: name and parent are required"
    with pytest.raises(MooError, match=error):
        w.load_files()


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        [["hall"]],
        [["hall", "door"], [Obj(10)]],
        [["hall"], [10]],
        [[1], [Obj(10)]],
        [["hall", "Hall"], [Obj(10), Obj(11)]],
        [["hall", "door"], [Obj(10), Obj(10)]],
    ],
)
def test_registry_value_rejects_malformed_registry_shapes(raw):
    with pytest.raises(MooError, match="malformed registry"):
        registry_value(raw)


def test_registry_value_accepts_parallel_key_and_object_lists():
    legacy = registry_value([["hall", "door"], [Obj(10), Obj(11)]])
    assert registry_value([[], []]) == {}
    assert legacy == {
        "hall": Obj(10),
        "door": Obj(11),
    }
    assert legacy.generations == {"hall": None, "door": None}

    verified = registry_value([["hall"], [Obj(10)], ["generation-hall"]])
    assert verified == {"hall": Obj(10)}
    assert verified.generations == {"hall": "generation-hall"}
    assert verified.revision == 0

    revised = registry_value([["hall"], [Obj(10)], ["generation-hall"], 7])
    assert revised == {"hall": Obj(10)}
    assert revised.generations == {"hall": "generation-hall"}
    assert revised.revision == 7

    partially_migrated = registry_value(
        [["hall", "door"], [Obj(10), Obj(11)], ["generation-hall", ""]]
    )
    assert partially_migrated.generations == {"hall": "generation-hall", "door": None}


def test_registry_value_accepts_and_marks_legacy_non_identifier_keys():
    registry = registry_value(
        [["hall", "bad-key", "مرحبا"], [Obj(10), Obj(11), Obj(12)]]
    )

    assert registry == {"hall": Obj(10), "bad-key": Obj(11), "مرحبا": Obj(12)}
    assert registry.legacy_keys == {"bad-key", "مرحبا"}


def test_write_file_creates_the_objects_directory_and_round_trips(tmp_path):
    w = World("test", tmp_path, "alice", {})
    obj = ObjectDef(key="hall", name="Hall", parent=Obj(2))

    path = w.write_file(obj)

    assert path == tmp_path / "worlds" / "test" / "objects" / "hall.moo"
    assert w.load_files() == {"hall": obj}


@pytest.mark.parametrize("key", ["../outside", "/absolute", "café", "bad-key"])
def test_file_for_rejects_keys_that_are_not_ascii_identifiers(tmp_path, key):
    w = World("test", tmp_path, "alice", {})

    with pytest.raises(MooError, match="ASCII identifier"):
        w.file_for(key)


def test_write_file_refuses_a_resolved_path_outside_objects_directory(tmp_path, monkeypatch):
    w = World("test", tmp_path, "alice", {})
    outside = tmp_path / "outside.moo"
    monkeypatch.setattr(w, "file_for", lambda key: outside)

    with pytest.raises(MooError, match="outside the objects directory"):
        w.write_file(ObjectDef(key="hall", name="Hall", parent=Obj(2)))

    assert not outside.exists()


class BootstrapTransport:
    can_suspend = True

    def __init__(self):
        self.expressions = []
        self.installed = []

    def eval(self, expression):
        self.expressions.append(expression)
        answers = {
            '"tmoo" in properties(player) && valid(player.tmoo)': False,
            "player.owned_objects": [Obj(9)],
            "#9.name": TOOLBOX_NAME,
            "properties(player)": [],
            "properties(#9)": [],
        }
        return answers.get(expression, 0)

    def install_verb(self, obj, name, lines):
        self.installed.append((obj, name, lines))
        return "installed"


def test_player_mismatch_is_never_cached_as_a_valid_identity(tmp_path):
    class WrongPlayerTransport:
        can_suspend = True

        def __init__(self):
            self.expressions = []

        def eval(self, expression):
            self.expressions.append(expression)
            if expression == "{player, player.name}":
                return [Obj(2), "mallory"]
            pytest.fail(f"bootstrap continued after identity mismatch: {expression}")

    transport = WrongPlayerTransport()
    w = World("test", tmp_path, "alice", {}, _transport=transport)

    with pytest.raises(MooError, match="logged in as mallory"):
        w.bootstrap(log=lambda _: None)
    with pytest.raises(MooError, match="logged in as mallory"):
        _ = w.player

    assert transport.expressions == ["{player, player.name}", "{player, player.name}"]


def test_bootstrap_adopts_an_orphan_before_creating_another_toolbox(tmp_path):
    transport = BootstrapTransport()
    w = World("test", tmp_path, "alice", {}, _transport=transport, _player=Obj(1))

    assert w.bootstrap(log=lambda _: None) == Obj(9)
    assert not any(expression.startswith("create(") for expression in transport.expressions)
    assert 'add_property(player, "tmoo", #9, {player, "r"})' in transport.expressions
    assert 'add_property(#9, "registry", {{}, {}, {}, 0}, {player, "r"})' in transport.expressions
    assert any(HELPER_VERSION_PROP in expression for expression in transport.expressions)
    assert [name for _, name, _ in transport.installed] == list(HELPER_VERBS)
    assert all(lines for _, _, lines in transport.installed)


@pytest.mark.parametrize("can_suspend, suffix", [(True, ", 1)"), (False, ", 0)")])
def test_helper_tells_remote_helpers_whether_the_transport_can_suspend(tmp_path, can_suspend, suffix):
    w = World(
        "test",
        tmp_path,
        "alice",
        {},
        _transport=SimpleNamespace(can_suspend=can_suspend),
        _toolbox=Obj(9),
    )
    assert w.helper("tmoo_export", "{#1}").endswith(suffix)


def test_outdated_helper_is_rejected_with_bootstrap_instruction(tmp_path):
    class OldHelperTransport:
        can_suspend = True

        def eval(self, expression):
            if expression == "properties(#9)":
                return ["registry"]
            pytest.fail(f"unexpected expression: {expression}")

    w = World(
        "test",
        tmp_path,
        "alice",
        {},
        _transport=OldHelperTransport(),
        _toolbox=Obj(9),
    )

    with pytest.raises(MooError, match=r"outdated.*tmoo bootstrap"):
        w.require_helper_version()
