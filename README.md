# terramoo

A MOO player's objects as files, on any MOO. `worlds/<world>/objects/*.moo` is
the source of truth for what one player owns; `tmoo apply` makes the MOO match
the files, and `tmoo pull` brings live edits back into them. Every call runs as
that player, so the tool can do exactly what they could do from `;` eval and
nothing more. The player has to be a programmer, and doesn't need to be a wizard.

Tested against LambdaMOO 1.8.1 (LambdaCore), ToastStunt 2.7 (ToastCore) and
mooR 1.0 (lambda-moor), over telnet, or through a hosted MCP gate with an `eval`
tool.

```
tmoo init mymoo --player alice --host moo.example.org --port 7777 [--tls]
tmoo secret store mymoo  # once per machine: the password, into the Keychain
tmoo bootstrap           # once per MOO: creates the toolbox, installs its verbs
tmoo status              # registry vs files vs objects owned but unmanaged
tmoo adopt --owned       # put every owned object under management, writing files
tmoo adopt '#123' key    # or one at a time
tmoo pull [key...]       # files <- MOO
tmoo plan                # what apply would do
tmoo apply [--destroy]   # MOO <- files (asks first; -y skips; --destroy recycles orphans)
tmoo diff [key...]       # unified diff, live rendering against the files
```

Install with `uv tool install git+<this repo's URL>`, or
depend on it from a repo holding your worlds (see that repo's `pyproject.toml`).
`tmoo` looks for the nearest `worlds/` directory above the current one
(`$TMOO_ROOT` overrides), and picks the only world there, or `--world` /
`$TMOO_WORLD`.

## Worlds

```toml
# worlds/mymoo/world.toml
player = "alice"

[connection]
transport = "telnet"    # or "mcp"
host = "moo.example.org"
port = 7777
tls = false
# verify = true                          check the TLS certificate
# login = "connect {player} {password}"  the login command
# eval_prefix = ";;"                     the core's statement eval
# tell = "notify(player, {})"            how answers are printed
# timeout = 120                          seconds of silence before giving up

# [connection] for a hosted MCP gate:
#   transport = "mcp"
#   url = "https://moo.example.org/mcp"
#   eval_tool = "eval"    set_verb_tool = "set_verb"    set_prop_tool = "set_prop"

[core]
toolbox_parent = "$thing"

ignore_props = []       # runtime state never written to files, beyond the defaults
keep_props = []         # re-enable one of the defaults (e.g. "key", the lock)
```

The secret (a password, or an MCP token) is read from `$TMOO_SECRET`, the
macOS Keychain (service `terramoo`, account = the world name), or
`~/.config/terramoo/<world>.secret`. It never goes in a world file.

## The file format

One object per file, named by its *key*: a registry name that stays put
while object numbers come and go.

```
object grand_courtyard
  name: "Grand Courtyard"
  parent: $room
  location: @gatehouse
  flags: "r"

  property guard_class (flags: "rc") = @generic_guard;
  override description = {
    "A broad court of fitted flagstones.",
    "Wide steps rise north."
  };

  verb bow (any none none) flags: "rd"
    player:tell("You bow.");
  endverb
endobject
```

- `property` defines a property on this object; `override` sets the value
  of one inherited from an ancestor. An inherited property with no
  `override` is clear.
- Values are MOO literals. `$name` is a corified object (`#0.name`),
  `@name` an object this world manages, `@me` the player. Anything else the
  MOO refers to by number stays a number. mooR's symbols (`'name`) and UUID
  objects (`#048D05-1234567890`) read and write as themselves.
- `owner:` on the object, a property or a verb is omitted when it is the
  player.
- Verb code is indented four spaces, with the MOO's own two-space unparse
  indent inside that. Keep that style and a save shows up as a code diff,
  not a whole-verb rewrite.
- Runtime state is never written (`exits`, `entrances`, `key`, `object_size`,
  the mail and connection properties, and others): see `DEFAULT_IGNORE_PROPS`
  in `terramoo/world.py`. Exits are linked into their rooms by `apply`, using
  their `source` and `dest`.

The shape is mooR's objdef export, so the same files could feed a mooR
world; `override` is the one keyword of ours.

## How it works

- **Transports** (`terramoo/transport/`). `telnet` logs in on the game port
  (plain or TLS) and runs each request as one `;;` eval line. The code it
  sends tags every line of its answer with a tag made up for that request,
  so other players' chatter, the core's `=>` echo and pages are ignored. A
  sentinel command sent right after the request tells a compile error apart
  from a slow answer. `mcp` calls a hosted gate's `eval` tool. Both return
  `toliteral()` text, which `terramoo/moolit.py` parses.
- **Toolbox.** An object the player owns, reached as `player.tmoo`, carrying
  the registry and four helper verbs from `terramoo/helper/`:
  - `tmoo_export`: whole objects in one call
  - `tmoo_apply`: a batch of ops, one result each
  - `tmoo_sysrefs`: the `$name` table
  - `tmoo_info`: names, owned objects and the server version

  The helpers are written in plain LambdaMOO 1.8, with no maps, no
  `ancestors()` and no core utilities, so one copy runs on every server. They
  refuse any caller but their owner. `tmoo bootstrap` reinstalls them.
- **Registry.** `key -> #nnn` lives in the MOO on the toolbox, as two
  parallel lists, and is mirrored to `worlds/<world>/state.json`. If the MOO
  is rolled back, the registry rolls back with it, and `plan` simply sees
  what is missing.
- **Plan** (`terramoo/plan.py`: pure, tested). The files and the live export
  are both read into `ObjectDef`s, references are resolved to numbers on both
  sides, and the two are diffed into ops. Creates go first, parents before
  children, so a file can name an object another file creates. A `@ref` to a
  key with no file is a problem, reported before anything runs.
- **Apply.** Ops are sent as MOO literals, one eval per batch. A failed op
  is reported and the rest carry on. Nothing is recycled without
  `--destroy`. A registry object the MOO has lost is recreated from its file,
  and the plan labels it as such.

## Portability notes

- Cores in the LambdaCore family (LambdaCore, ToastCore, JHCore,
  lambda-moor) all work unchanged. For a core that spells eval differently,
  set `eval_prefix`. For one without `$thing`, set `toolbox_parent`.
- On a core without `owned_objects`, `status` can't list unmanaged
  objects and `adopt --owned` is unavailable; adopt objects one at a time.
- Exit linking uses LambdaCore's `$exit`, `source`/`dest` and
  `:add_exit`/`:add_entrance`. On a core without `$exit` it links nothing.
- A second login as the same player boots the first on LambdaCore-family
  cores. The telnet transport logs in again when it finds its connection
  gone between requests (never in the middle of one), but two `tmoo`
  processes on one world will keep knocking each other off.
- Strings containing a newline can't be sent over telnet, because MOO
  literals have no newline escape.

## Testbeds and tests

`uv run pytest` runs the offline tests: the format, the literals, the plan,
and both transports against fakes.

`testbeds/{lambdamoo,toaststunt,moor}/` each hold a `setup.sh` that builds
that server from source on macOS with no Docker, plus `start.sh` and
`stop.sh`. They listen on 127.0.0.1:17002, 17001 and 17003, each with a
non-wizard programmer `tester`/`tester`. The live round trip creates,
edits, pulls, recycles and recreates objects, then leaves nothing behind:

```
testbeds/toaststunt/setup.sh && testbeds/toaststunt/start.sh --fresh
TMOO_LIVE=127.0.0.1:17001:tester:tester uv run pytest tests/test_live.py
testbeds/toaststunt/stop.sh
```

It refuses to run against a player whose registry manages anything but
test objects.
