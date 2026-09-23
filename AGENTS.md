# terramoo — agent guide

`README.md` is the user view and `REVIEW.md` the implementation map. This
file is what an agent needs to avoid breaking things.

## Rules

- **Never run `tmoo apply --destroy` against a real world unattended.** It
  recycles every registry object with no file.
- **Helpers stay plain LambdaMOO 1.8.** `terramoo/helper/*.moo` must not use
  maps, `ancestors()`/`isa()`, `$..._utils`, type constants (mooR spells them
  `TYPE_OBJ`; compare against `typeof(#0)`) or list comprehensions. They may
  `suspend(0)` only when their last argument says so (telnet yes, a hosted
  MCP gate no).
- **Every helper checks `caller_perms()`.** It runs with the player's
  permissions, so without the check anybody could call it.
- **Expressions `tmoo` sends are single expressions**: no `;`, backquotes or
  statements, since the MCP gate evaluates one expression. Loops and error
  handling go in the helpers; assignments go through `Transport.set_prop`.
- **Keys, not object numbers, are identities.** A value read from the MOO
  resolves through the registry (`live=True`), a file's value through the
  pending creates first: see `Refs.resolve_ref`.
- **Runtime state stays out of files.** Add such a property to
  `DEFAULT_IGNORE_PROPS` in `terramoo/world.py` (or a world's
  `ignore_props`); don't special-case it in plan.
- **Secrets never touch a file in a repo.** Keychain service `terramoo`,
  `$TMOO_SECRET`, or `~/.config/terramoo/<world>.secret`.

## Commands

- `uv run pytest`: the offline suite.
- After touching a helper, a transport, or plan/apply, run the live round
  trip on all three testbeds (README, "Testbeds and tests"), one at a time,
  stopping each when done:
  `TMOO_LIVE=127.0.0.1:1700N:tester:tester uv run pytest tests/test_live.py`
  with 17001 ToastStunt, 17002 LambdaMOO 1.8.1, 17003 mooR.

## Layout

    terramoo/moolit.py       MOO literals <-> Python (Obj, Err, Sym, Map, Ref)
    terramoo/objdef.py       the file format
    terramoo/model.py        ObjectDef / PropDef / VerbDef
    terramoo/refs.py         registry + $names, symbolize/resolve
    terramoo/plan.py         the pure diff
    terramoo/export.py       MOO -> ObjectDef via tmoo_export
    terramoo/apply.py        ops -> MOO via tmoo_apply
    terramoo/world.py        world.toml, toolbox, bootstrap
    terramoo/transport/      telnet.py (any MOO), mcp.py (hosted gates)
    terramoo/helper/         the verbs installed on the toolbox
    terramoo/secrets.py      password / token lookup
    terramoo/cli.py          `tmoo`
    testbeds/                LambdaMOO, ToastStunt, mooR built from source
