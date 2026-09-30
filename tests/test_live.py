"""A round trip against a real MOO, leaving nothing behind.

Skipped unless `TMOO_LIVE=host:port:player:password` names a programmer
account on a MOO you may scribble on (see testbeds/), e.g.

    TMOO_LIVE=127.0.0.1:17001:tester:tester uv run pytest tests/test_live.py
"""

import os

import pytest

from terramoo import cli
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
    for f in w.objects_dir.glob("tmoo_test_*.moo"):
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
