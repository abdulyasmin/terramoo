# terramoo

A MOO player's objects as files, on any MOO. `worlds/<world>/objects/*.moo`
is the source of truth for what one player owns: `tmoo apply` makes the MOO
match the files, and `tmoo pull` brings live edits back. Everything runs as
that player, who must be a programmer but needn't be a wizard.

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

Install with `uv tool install git+<this repo's URL>`, or add it as a
dependency of a repo that holds your worlds. `tmoo` uses the nearest
`worlds/` directory at or above the current one (`$TMOO_ROOT` overrides) and
the only world in it, or `--world` / `$TMOO_WORLD`.

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
# connect_timeout, chunk, batch_bytes    see terramoo/transport/telnet.py

# [connection] for a hosted MCP gate:
#   transport = "mcp"
#   url = "https://moo.example.org/mcp"
#   eval_tool = "eval"    set_verb_tool = "set_verb"    set_prop_tool = "set_prop"

[core]
toolbox_parent = "$thing"

ignore_props = []       # runtime state never written to files, beyond the defaults
keep_props = []         # re-enable one of the defaults
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
- Runtime state is never written (`exits`, `entrances`, `object_size`,
  the mail and connection properties, and others): see `DEFAULT_IGNORE_PROPS`
  in `terramoo/world.py`. Exits are linked into their rooms by `apply`, using
  their `source` and `dest`.

The shape is mooR's objdef export, so the same files could feed a mooR
world; `override` is the one keyword of ours.

## How it works

- `tmoo bootstrap` creates a *toolbox*, an object the player owns reached as
  `player.tmoo`, and installs four helper verbs on it from
  `terramoo/helper/`. They are plain LambdaMOO 1.8, so one copy runs on
  every server, and they refuse any caller but their owner.
- The toolbox holds the *registry*, `key -> #nnn`. It lives in the MOO, so a
  rollback rolls it back too; `worlds/<world>/state.json` is a local copy.
- `plan` diffs the files against the live objects. A `@ref` to a key with no
  file is reported before anything runs.
- `apply` creates new objects parents first (recreating any the MOO has
  lost), re-reads them to correct what the core's `initialize` set, then
  sends the rest in batches. A failed op is reported and the rest carry on.
  Nothing is recycled without `--destroy`.
- Both transports send one expression per request and parse its
  `toliteral()` text. Telnet tags its answer so other players' chatter is
  ignored (see `terramoo/transport/telnet.py`).

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

To review or change the code, start with `REVIEW.md`: the architecture, a
reading order and the traps.

## License

GNU Affero General Public License v3.0 or later; see `LICENSE`.
