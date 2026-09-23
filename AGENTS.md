# terramoo — agent guide

Read `README.md` first: the commands, the file format, the transports.
This file keeps what an agent needs to avoid breaking things.

## Rules

- **Never run `tmoo apply --destroy` against a real world unattended.** It
  recycles every registry object with no file. Plain `apply` only creates
  and updates.
- **Portability is the point.** The helper verbs (`terramoo/helper/*.moo`)
  must stay plain LambdaMOO 1.8: no maps, no `ancestors()`/`isa()`, no
  `$..._utils`, no type constants (mooR spells them `TYPE_OBJ`: compare
  against `typeof(#0)` etc.), no list comprehensions. They run as the
  player, called from eval, so keep them cheap; they may `suspend(0)` only
  when their last argument says so (telnet yes, a hosted MCP gate no).
- **Expressions `tmoo` sends stay single expressions.** No `;`, no
  backquotes, no statements: the MCP gate evaluates one expression. Loops
  and error handling belong in the helpers. (Assignments go through
  `Transport.set_prop`, which the MCP transport sends to its own tool.)
- **Every helper checks `caller_perms()`.** A toolbox verb runs with the
  player's permissions; without the check anybody could call it.
- **Object numbers are not identities.** Keys (file names) are; the
  registry maps them to numbers. A value read from the MOO resolves
  through the registry (`live=True`), a file's value through the pending
  creates first — see `Refs.resolve_ref`.
- **Runtime state stays out of files.** When a property turns out to be
  state the MOO writes on its own, add it to `DEFAULT_IGNORE_PROPS` in
  `terramoo/world.py` (or `ignore_props` in a world), don't teach plan to
  special-case it.
- **Secrets never touch a file in a repo.** Keychain service `terramoo`,
  `$TMOO_SECRET`, or `~/.config/terramoo/<world>.secret`.

## Commands

- `uv run pytest` — offline: format, literals, plan, both transports.
- `TMOO_LIVE=127.0.0.1:1700N:tester:tester uv run pytest tests/test_live.py`
  — the live round trip against a testbed (README, "Testbeds and tests").
  Run it on all three servers after touching a helper, the transport, or
  plan/apply: 17001 ToastStunt, 17002 LambdaMOO 1.8.1, 17003 mooR.
- Only one testbed at a time needs to run; `stop.sh` each when done.

## Layout

    terramoo/moolit.py       MOO literals <-> Python (Obj, Err, Sym, Map, Ref)
    terramoo/objdef.py       the file format
    terramoo/model.py        ObjectDef / PropDef / VerbDef
    terramoo/refs.py         registry + $names, symbolize/resolve
    terramoo/plan.py         the pure diff (tests/test_plan.py)
    terramoo/export.py       MOO -> ObjectDef via tmoo_export
    terramoo/apply.py        ops -> MOO via tmoo_apply
    terramoo/world.py        world.toml, toolbox, bootstrap
    terramoo/transport/      telnet.py (any MOO), mcp.py (hosted gates)
    terramoo/helper/         the verbs installed on the toolbox
    terramoo/secrets.py      password / token lookup
    terramoo/cli.py          `tmoo`
    testbeds/                LambdaMOO, ToastStunt, mooR built from source
