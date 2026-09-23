"""Probe what the testbed offers a non-wizard programmer (tester).

Run with the server started: `python3 probe.py`. Prints each `;` line sent
and the raw lines the server printed back. Creates two objects and
recycles them again at the end.
"""

from moo_client import Moo

PROBES = [
    # basics
    "server_version()",
    "player",
    "{player.programmer, player.wizard}",
    # toliteral
    "toliteral(#1)",
    "toliteral(E_PERM)",
    'toliteral(["a" -> 1, 2 -> {3, "x"}])',
    'toliteral("say \\"hi\\" \\\\ ok")',
    "toliteral({1, 2.5, #-1, E_NONE, true})",
    # maps, ancestry
    '["a" -> 1, "b" -> 2]["b"]',
    "ancestors(player)",
    "isa(player, $player)",
    "{valid($thing), valid($exit), $thing, $exit}",
    # objects owned by tester
    ";o = create($thing); o.name = \"probe thing\"; player.probe = o; return o;",
    "player.probe",
    'add_property(player.probe, "colour", "red", {player, "rc"})',
    'property_info(player.probe, "colour")',
    ";c = create(player.probe); player.probe2 = c; return c;",
    'is_clear_property(player.probe2, "colour")',
    'player.probe2.colour = "blue"',
    'is_clear_property(player.probe2, "colour")',
    'clear_property(player.probe2, "colour")',
    '{is_clear_property(player.probe2, "colour"), player.probe2.colour}',
    'add_verb(player.probe, {player, "rxd", "greet"}, {"this", "none", "this"})',
    'set_verb_code(player.probe, "greet", {"return \\"hello \\" + tostr(args);"})',
    'set_verb_code(player.probe, "greet", {"return 1 +;"})',
    'verb_code(player.probe, "greet")',
    'verb_info(player.probe, "greet")',
    'verb_args(player.probe, "greet")',
    "player.probe:greet(1, 2)",
    "player.owned_objects",
    # utils
    "{$object_utils, $list_utils, $string_utils}",
    "$object_utils:isa(player, $prog)",
    '$list_utils:setremove_all({1, 2, 1, 3}, 1)',
    '$string_utils:print({"a", 1})',
    # control flow and task limits
    ";try raise(E_PERM); except e (ANY) return {\"caught\", e}; endtry",
    "`1 / 0 ! ANY => \"backquote ok\"'",
    "{ticks_left(), seconds_left()}",
    ";suspend(0); return {\"after suspend\", ticks_left(), seconds_left()};",
    # output
    'notify(player, "x-notify")',
    'player:tell("x-tell")',
    "1 +",
    "undefined_variable_zz",
    "#2.password",
    # cleanup
    ";recycle(player.probe2); recycle(player.probe); return {valid(player.probe), valid(player.probe2)};",
    ";delete_property(player, \"probe\"); delete_property(player, \"probe2\"); return 1;",
]


def main():
    m = Moo()
    m.login("tester", "tester")
    # scratch properties on tester so objects survive between one-line evals
    for p in ("probe", "probe2"):
        m.eval(f'`add_property(player, "{p}", #-1, {{player, "rc"}}) ! ANY\'')
    for line in PROBES:
        print(f";{line}")
        for out in m.eval(line):
            print(f"    | {out}")
    m.close()


if __name__ == "__main__":
    main()
