from types import SimpleNamespace

import pytest

from terramoo.errors import MooError
from terramoo.moolit import Obj
from terramoo.world import DEFAULT_IGNORE_PROPS, HELPER_VERBS, TOOLBOX_NAME, World


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


def test_load_files_rejects_a_key_that_does_not_match_its_filename(tmp_path):
    w = make_world(tmp_path, 'player = "alice"\n')
    (w.objects_dir / "hall.moo").write_text(
        'object courtyard\n  name: "Courtyard"\n  parent: $room\nendobject\n'
    )
    with pytest.raises(MooError, match="file is named 'hall' but declares object 'courtyard'"):
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


def test_bootstrap_adopts_an_orphan_before_creating_another_toolbox(tmp_path):
    transport = BootstrapTransport()
    w = World("test", tmp_path, "alice", {}, _transport=transport, _player=Obj(1))

    assert w.bootstrap(log=lambda _: None) == Obj(9)
    assert not any(expression.startswith("create(") for expression in transport.expressions)
    assert 'add_property(player, "tmoo", #9, {player, "r"})' in transport.expressions
    assert 'add_property(#9, "registry", {{}, {}}, {player, "r"})' in transport.expressions
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
