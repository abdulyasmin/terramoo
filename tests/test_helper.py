"""Execute the real helper offline with the optional LambdaMOO testbed sources.

NP_SINGLE uses stdin/stdout instead of sockets. Build only in pytest's temporary
directory; never start or modify a running testbed or download missing sources.
"""

import gzip
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from terramoo.moolit import parse, serialize

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def offline_moo(tmp_path_factory):
    build = ROOT / "testbeds/lambdamoo/.build"
    source = build / "MOO-1.8.1"
    core = build / "LambdaCore-17May04.db.gz"
    if not (source / "Makefile").exists() or not core.exists():
        pytest.skip("requires the locally built LambdaMOO testbed sources")
    if not shutil.which("make") or not shutil.which("cc"):
        pytest.skip("requires make and cc for the stdin/stdout LambdaMOO build")
    work = tmp_path_factory.mktemp("offline-moo")
    source = Path(shutil.copytree(source, work / "src", ignore=shutil.ignore_patterns("*.o", "moo")))
    options = source / "options.h"
    text, count = re.subn(r"(#define NETWORK_PROTOCOL\s+)NP_TCP", r"\1NP_SINGLE", options.read_text())
    assert count == 1, "refuse to run unless the socket backend was replaced"
    options.write_text(text)
    result = subprocess.run(["make", "moo"], cwd=source, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    database = work / "pristine.db"
    database.write_bytes(gzip.decompress(core.read_bytes()))
    return source / "moo", database


@pytest.mark.parametrize("callback", ["add", "rebind", "remove", "shift"])
@pytest.mark.parametrize("first_op", ["destroy", "recycle"])
def test_destroy_preserves_recycle_callback_registry_changes(offline_moo, tmp_path, callback, first_op):
    binary, database = offline_moo
    helper = (ROOT / "terramoo/helper/tmoo_apply.moo").read_text().splitlines()
    changes = '{ {"register", "survivor", this.survivor} }'
    if callback == "rebind":
        changes = '{{"unregister", "old"}, {"register", "OLD", this.survivor}}'
    elif callback == "remove":
        changes = '{{"unregister", "old"}, {"register", "survivor", this.survivor}}'
    elif callback == "shift":
        changes = '{{"unregister", "before"}, {"register", "survivor", this.survivor}}'
    recycle = [f"return this.toolbox:tmoo_apply({changes});"]
    expected_key = "OLD" if callback == "rebind" else "survivor"
    initial = '{{"before", "old"}, {after, old}}' if callback == "shift" else '{{"old"}, {old}}'
    first = '{"destroy", "OLD", old}' if first_op == "destroy" else '{"recycle", old}'
    script = f""";;
tool = create(#-1);
add_property(tool, "registry", {{{{}}, {{}}}}, {{#2, ""}});
add_verb(tool, {{#2, "xd", "tmoo_apply"}}, {{"this", "none", "this"}});
errors = set_verb_code(tool, "tmoo_apply", {serialize(helper)});
if (errors) raise(E_INVARG, toliteral(errors)); endif
old = create(#-1);
survivor = create(#-1);
after = create(#-1);
add_property(old, "toolbox", tool, {{#2, ""}});
add_property(old, "survivor", survivor, {{#2, ""}});
add_verb(old, {{#2, "xd", "recycle"}}, {{"this", "none", "this"}});
errors = set_verb_code(old, "recycle", {serialize(recycle)});
if (errors) raise(E_INVARG, toliteral(errors)); endif
tool.registry = {initial};
result = tool:tmoo_apply({{{first}, {{"destroy", "old", old}}, {{"register", "after", after}}}});
return {{result, tool.registry == {{{{"{expected_key}", "after"}}, {{survivor, after}}}}, valid(old), valid(survivor), after}};
.
quit
"""
    result = subprocess.run(
        [str(binary), "-e", str(database), str(tmp_path / "out.db")],
        input=script, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    reply = re.search(r"^=> (.+)$", result.stdout, re.MULTILINE)
    assert reply, result.stdout
    ops, registry_matches, old_valid, survivor_valid, after = parse(reply[1])
    assert [registry_matches, old_valid, survivor_valid] == [1, 0, 1], result.stdout
    assert ops[2] == [1, after]
    # Rebinding must survive and a subsequent stale destroy must reject it.
    for index in (0, 1):
        if callback == "rebind" and (index == 1 or first_op == "destroy"):
            assert ops[index][:2] == [0, "E_INVARG"]
        else:
            assert ops[index] == [1, 1]
