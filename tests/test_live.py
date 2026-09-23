"""A round trip against a real MOO, leaving nothing behind.

Skipped unless `TMOO_LIVE=host:port:player:password` names a programmer
account on a MOO you may scribble on (see testbeds/), e.g.

    TMOO_LIVE=127.0.0.1:17001:tester:tester uv run pytest tests/test_live.py
"""

import os

import pytest

from terramoo import cli
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
