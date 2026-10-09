# terramoo

A MOO player's objects as files, on any MOO. `worlds/<world>/objects/**/*.moo`
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
tmoo adopt '#123' key --verify  # confirm/stamp a legacy binding after inspecting it
tmoo rename-key OLD NEW  # migrate a legacy registry key to an ASCII identifier
tmoo pull [key...]       # files <- MOO
tmoo plan                # what apply would do
tmoo apply [--destroy]   # MOO <- files (asks first; -y skips; --destroy recycles orphans)
tmoo diff [key...]       # unified diff, live rendering against the files
```

`tmoo rename-key --recover` is an explicit operator recovery command. An
interrupted or uncertain rename preserves
`worlds/<world>/state.json.rename-recovery.json`; do not edit files under the
world's `objects/` directory while recovery runs. File-writing commands use
`worlds/<world>/.cache/write.lock`; another such command for that world fails
rather than waiting.

Worlds using modules or packages recover key migrations with
`tmoo package recover`. The command also recovers interrupted installation,
import, deployment, and removal transactions.

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
#   dialect = "moor"       when the gate fronts mooR rather than LambdaMOO/ToastStunt
#   eval_tool = "eval"    set_verb_tool = "set_verb"    set_prop_tool = "set_prop"

[core]
toolbox_parent = "$thing"

ignore_props = []       # runtime state never written to files, beyond the defaults
keep_props = []         # re-enable an exclusion (names are case-insensitive)
```

The secret (a password, or an MCP token) is read from `$TMOO_SECRET`, the
macOS Keychain (service `terramoo`, account = the world name), or
`~/.config/terramoo/<world>.secret`. It never goes in a world file.

## Folders and modules

Object files can be nested to any depth. Their filename and `object` header
remain their identity, so moving a file between ordinary folders does not
rename its live object. Keys must be unique throughout a world, including
case differences. Symlinked files and directories are refused.

A `module.toml` groups the files below it, stopping at any nested module
manifest. For example, `objects/local/rooms/module.toml` can contain:

```toml
schema_version = 1
name = "rooms"
references = ["local/arrival"]
depends_on = ["local/core"]
```

Standalone modules have addresses such as `local/rooms`; bare `rooms` means
the same thing in standalone manifests and command selectors. Package module
addresses use their instance name, such as `north_town/rooms`.

Declare every cross-module structured reference directly. `references`
permits ordinary references and cycles. `depends_on` also requires that the
provider finish applying before the consumer starts, including initialization.
Parent cycles and contradictory initialization dependencies fail validation.
Files without a manifest remain ungrouped. Give an ungrouped provider a module
before referencing it from a named module.

```sh
tmoo check                             # offline file and module validation
tmoo modules                           # offline membership and dependencies
tmoo plan --module rooms
tmoo apply --module rooms -y
tmoo status --module rooms
tmoo diff --module rooms --with-deps
tmoo pull --module rooms
tmoo pull hall --into local/rooms       # destination for a missing standalone file
```

Repeat `--module` and `--package` to select a union. Plan and apply include
reference and initialization dependencies and display the resulting scope.
Status, diff, pull, and export include dependencies only with `--with-deps`.
Removal is limited to explicitly selected scopes; adding dependencies to an
apply does not authorize removing their objects. With no selectors, commands
operate on the whole world.

## Package instances

A package is a local directory with `package.toml`, module manifests, and
portable object definitions. It can be installed zero or more times in each
world. Each named instance has its own namespace, source snapshot, bindings,
editable files, and deployment history. See [the town example](examples/packages/town/package.toml).

```sh
tmoo package check examples/packages/town
tmoo package install examples/packages/town --as north_town
tmoo package install examples/packages/town --as south_town
tmoo plan --package north_town
tmoo apply --package north_town -y
```

Install prepares files locally; apply creates or changes live objects. The
example creates keys such as `north_town__square` and `south_town__square`.
`--namespace NAME` overrides the default prefix. Installation refuses local
collisions; plan and apply also reject occupied live keys without a matching
installation receipt. Use import to claim existing objects explicitly.

```toml
# package.toml in a reusable source directory
schema_version = 1
name = "town"
version = "1.0.0"
modules = ["core", "rooms"]

[inputs.entry_room]
type = "object"
required = true
```

List every module root. Source objects use package-local `@keys`; compilation
rewrites parsed references and headers for each instance. Strings and verb
code stay literal. Positive object numbers and UUID objects in structured
source values are refused. Use object inputs bound to managed `@keys`, `$names`,
`@me`, `#0`, or negative sentinels. A consuming module declares
`uses_inputs = ["entry_room"]`; add `depends_on_inputs` when initialization
needs the provider to be fully applied.

Pass bindings as `--bind 'entry_room=@arrival'` or edit the instance's
`bindings` table in `world.toml` and run `package update INSTANCE`. Bindings
add module references only for modules that consume them.

Package dependency aliases specify an exact package name and version:

```toml
# in the consumer's package.toml
[dependencies.kit]
package = "kit"
version = "1.0.0"
# digest = "..."  # optional required SHA-256 content digest
```

```toml
# in worlds/example/world.toml
[packages.shared_kit]
source = "../../../packages/kit"

[packages.north_town]
source = "../../../packages/town"

[packages.north_town.dependencies]
kit = "shared_kit"
```

Source paths in world configuration are relative to that world's directory.
Consumer modules refer to a dependency module as `kit/base`. Installing the
consumer prepares its configured dependency closure. Sharing requires an
explicit provider instance; no dependency instance is created implicitly.
Versions and content digests are pinned. An incompatible shared-provider
upgrade requires updating compatible consumers together or selecting another
provider instance.

### Edit, update, and remove

Installed files live under `objects/packages/INSTANCE/`, retaining the source
module paths. Edit them as ordinary world files. Their pinned source and
upgrade baselines remain separate under `.packages/` and in
`packages.lock.json`. Normal checks and deployments do not reread the source
directory. Retain these files, deployment receipts, and `world.toml` when
moving or backing up a world; `state.json` alone cannot establish ownership.

```sh
tmoo package update north_town                  # stage current source and bindings
tmoo package update shared_kit north_town south_town
tmoo package update north_town --resume         # after editing a conflict candidate
tmoo package update north_town --abort          # discard only an uncommitted candidate
tmoo apply --package north_town -y
tmoo pull --package north_town
tmoo package remove north_town                  # stage removal; no live recycling
tmoo plan --package north_town --destroy
tmoo apply --package north_town --destroy       # asks before recycling
```

Updates compare previous compiled source, edited installation, and new compiled
source at file granularity. Independent local edits are retained. Conflicts
leave active files untouched and write a candidate under `.packages/candidates/`.
Edit its files, remove the conflict markers, then resume; delete a candidate
file to choose removal. Changes to active files invalidate the candidate.

Pull refuses selected package objects whose desired revision is pending or
only partly applied. Missing files are restored at recorded paths. Exported
references that need new module declarations are reported and must be fixed
before applying. Pull never changes source snapshots or upgrade baselines.

Removal with local edits requires `package remove --yes`, which archives those
edits. Live teardown checks ownership, generations, and references from surviving
managed objects. Consumers are removed before providers. A failed deletion
stops later deletions and retains the remaining records for recovery. After
completed removal, the same instance name can be installed again with a new
installation identity; the previous history is archived.

### Import and migrate identities

Import requires an existing managed local definition and a verified registry
binding for every source object. Its mapping file is explicit:

```toml
schema_version = 1
[objects]
town_room = "my_room_class"
square = "existing_square"
inn = "existing_inn"
```

```sh
tmoo package import examples/packages/town --as north_town --mapping import.toml
tmoo plan --package north_town
tmoo rename-key existing_square grand_square
tmoo package migrate north_town --namespace northern
tmoo package rename north_town northern_town
```

Import preserves live objects and local definitions, updates module membership,
and records the source-to-world key overrides. Key and namespace migrations
preserve object identities while updating mappings, parsed references, input
bindings, and comparison baselines. An instance rename changes its address and
directories while preserving its namespace and keys.

For an upstream source-key rename, first change the source, then supply a TOML
mapping with `schema_version = 1` and `[objects] old_key = "new_key"`:

```sh
tmoo package migrate northern_town --source-keys source-renames.toml
```

This stages a three-way update that retains installed keys. Use the usual
update resume/abort commands if it conflicts. Namespace and registry-key
migrations keep a durable remote-operation journal; after interruption, use
`tmoo package recover` before another mutation. Recovery verifies the recorded
generations and deployment token. A stale checkout cannot authorize mutations
of another checkout's package objects.

An update that both adds and removes source keys requires an explicit choice:
provide a source-key migration, or use `package update --allow-replacements`
to accept distinct objects. The latter leaves recycling behind the usual
reviewed `apply --destroy` step.

Package sources are local directories in this release. There is no registry,
remote download protocol, version-range solver, or automatic rewriting of MOO
verb code. Dynamic references and callback behavior still require explicit
module declarations from the author.

Upgrade all clients for a world together and run `tmoo bootstrap` to install
helper version 13. Older flat-only clients do not understand nested files or
package ownership; a new world-format marker cannot make them safe. Downgrading
requires a validated export/flattening procedure or a matching world and
database backup.

## Managing your player

`worlds/<world>/player.moo` manages selected fields on the authenticated
`@me`. Player commands are separate from object and module commands:
`tmoo apply --module ...` changes objects; `tmoo player apply` changes the
player. Ordinary adoption, export and mutation refuse player objects and
the toolbox, including bindings left in an old registry.

After upgrading, run `tmoo bootstrap` to install helper version 14 on each
world. Start by inspecting metadata and selecting the fields you want:

```sh
tmoo player inspect
tmoo player track --verb '@who' --verb probe --property description
tmoo player track --setting aliases --setting 'display:shortprep'
tmoo player check          # offline validation; also included in tmoo check
tmoo player diff
tmoo player plan
tmoo player apply          # asks first; -y skips
tmoo player pull           # accept live changes for the selected fields
```

Use a verb's full local name specification, as printed by `inspect`.
Tracking preserves its owner, flags, arguments and source; inherited verbs
are never edited. A new specification that collides with an existing local
alias is refused; remove the old definition explicitly before replacing
its name specification. Properties must belong to the player. Only explicitly
selected values are read. The reader does not enumerate ancestor
properties, so an unreadable player class does not prevent tracking a
readable field inherited through it.

The file uses the same property and verb syntax as object files, with a
`player` header and no name, parent, location, owner or object flags:

```moo
player
  override description = "A description maintained in Git.";
  property favorite_color (flags: "rc") = "purple";

  verb wave (any none none) flags: "rd"
    player:tell("You wave.");
  endverb

  setting aliases = {"alice", "Alice Example"};
  setting "display:shortprep" = 1;
  feature @tools__commands;
endplayer
```

Track an existing field before editing its definition. New personal
properties and local verbs can be added directly to the file. A new
`feature` declaration can activate a feature that is not already attached.
The referenced object must be deployed first. Keys in property values and
feature declarations participate in object-key and namespace migrations;
MOO source inside verbs is kept verbatim.

The LambdaCore-family settings adapters support `aliases`, `gender`,
`home`, `linelen`, `pagelen`, and individual `display:NAME`, `edit:NAME`,
`prog:NAME`, and `build:NAME` options. Use canonical names from the core's
option definitions; shorthand options that change several fields are
excluded. These adapters use the core's setter verbs and verify the
result. Aliases must include the current player name. Setters
maintain name indexes, pronouns, abode checks and option validation. A
missing setter or an unsupported setting fails explicitly. ANSI settings
and other core-specific extensions require a separate adapter; they cannot
be assigned through the generic property path. Description changes use
`set_description`.

Password, token, email, mail, connection, quota, toolbox and derived
properties are protected before any value is read. `keep_props` cannot
enable them for player management. Select custom properties only when
their values belong in the world repository. Player lifecycle operations,
account renaming and privilege changes are excluded.

An omitted field is left alone. These commands distinguish forgetting a
field from changing it on the MOO:

```sh
tmoo player untrack --verb probe       # leave the live verb alone
tmoo player remove --verb probe        # stage deletion of a tracked local verb
tmoo player remove --property custom   # stage deletion of a tracked local property
tmoo player clear --property description  # restore inheritance; keep tracking clear state
tmoo player detach --feature @tools__commands
tmoo player plan
tmoo player apply
```

The corresponding declarations are `remove verb probe;`,
`remove property custom;`, `clear property description;`, and
`detach @tools__commands;`. Successful deletions and detachments leave the
file; a `clear` declaration remains and follows the inherited value.

Player application compares the file and live field against its last
accepted snapshot in `.player/state.json`. Conflicting live edits stop
application. `player diff` shows the current difference; `player pull`
explicitly accepts live values into the file and receipt, replacing local
edits for those fields. Each write rechecks its expected value, metadata
and verb descriptor. A stale checkout must refresh its receipt before
writing. Keep `.player/` with the world when backing it up or moving it.

An interrupted application retains `.player/operation.json`. Run
`tmoo player recover`; it reconciles recorded results without replaying
setters or feature hooks. If a callback was interrupted or failed, inspect
its effects and use `tmoo player recover --accept-live` to accept the live
values of all selected fields. Core callbacks can have effects beyond
those fields, and recovery does not roll those effects back. Recovery
refuses to finish while the callback task is still suspended. Other
terramoo mutations are blocked while a player operation remains active.

Packages contain ordinary objects and may refer to `@me`. They do not own
the player. The world player file selects feature objects from any number
of independent installations. Apply those objects before activating their
features. Unrelated live features remain enabled. Before removing an
installation, detach its features and change any remaining player
references; removal checks both the desired player references and live
selected values, plus the current player's feature list even when it is
untracked. The server rechecks selected fields and the player revision
immediately before recycling. A staged package removal still permits
detaching its live features before `apply --destroy`.

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
- Values use mooR's MOO-literal escapes (including `\n`, `\xNN` and
  `\uNNNN`). `$name` is a corified object (`#0.name`),
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
  `player.tmoo`, and installs eleven helper verbs on it from
  `terramoo/helper/`: `tmoo_registry`, `tmoo_callback`, `tmoo_generation`,
  `tmoo_export`, `tmoo_apply`, `tmoo_sysrefs`, `tmoo_info`, `tmoo_packages`,
  `tmoo_player_read`, `tmoo_player_write`, and `tmoo_player`.
  They are plain LambdaMOO 1.8,
  so one copy runs on every server, and they refuse any caller but their
  owner. Re-run `tmoo bootstrap` after upgrading terramoo to install updated
  helpers.
  Export, adoption and apply check the installed helper version and give that
  bootstrap instruction before sending an incompatible request.
- The toolbox holds the *registry*, `key -> {#nnn, generation nonce}`, plus a
  registry-wide revision used to compare-and-set key renames. The
  same protected nonce is stored on the object and ignored by exports, so a
  recycled and reused object number cannot impersonate the managed object.
  A hierarchy defines a property once, so a managed child of a managed
  parent holds its nonce as its own value of the inherited property;
  `tmoo_generation` keeps every stamp intact across registering a parent
  after its children, reparenting, and recycling a parent. It
  lives in the MOO, so a
  rollback rolls it back too; `worlds/<world>/state.json` is a local copy.
- `plan` diffs the files against the live objects. A `@ref` to a key with no
  file is reported before anything runs.
- `apply` creates new objects parents first (recreating any the MOO has
  lost), re-reads them to correct what the core's `initialize` set, then
  sends the rest in batches. Every mutation compares the key, expected object
  and generation immediately before it runs. A failed op is reported and the
  rest carry on.
  If `initialize` registers a new object under another key, create reports
  a conflict and preserves that registration and object.
  Nothing is recycled without `--destroy`, and recycling is skipped whenever
  a create or normal operation in that run failed.
  Destroy checks that the key still names the expected object generation, recycles it
  if it exists, and unregisters it in one helper call. Retrying also removes
  registrations for objects already gone; a failed recycle keeps the entry.
- Registries created by older terramoo versions have no generation nonces.
  Status marks these bindings unverified; export, mutation, and destruction
  refuse them. Inspect the live object, then run
  `tmoo adopt '#123' key --verify` to stamp the confirmed binding; it keeps
  an existing object file and writes the live definition only when none
  exists. Re-run `tmoo bootstrap` first when terramoo reports outdated
  toolbox helpers.
  Bootstrap migrates the registry in a single non-suspending MOO task.
- Both transports send one expression per request and parse its
  `toliteral()` text. Telnet tags its answer so other players' chatter is
  ignored (see `terramoo/transport/telnet.py`).

## Upgrading from a pre-nonce terramoo

Back up the MOO database together with the world files, then install the
new terramoo and run `tmoo bootstrap`. Inspect each live binding and confirm
it with `tmoo adopt '#123' key --verify`; existing object files are kept.
Run `tmoo plan`, review the changes, then `tmoo apply` and `tmoo plan` again.

Rolling back only the tool is unsafe: an older terramoo reads the migrated
four-element registry as empty, so it plans duplicate creates, reports
"no changes" for `apply --destroy`, and replaces bindings if re-bootstrapped.
A rollback must restore the pre-upgrade database and world files together.

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
- Strings containing a newline can be sent to mooR. LambdaMOO and ToastStunt
  have no newline escape, so their telnet transports reject such values.

## Testbeds and tests

`uv run pytest` runs the offline tests: formats, modules, packages, planning,
transactions, recovery, and both transports against fakes. Helper tests compile
and run the MOO verbs in a local LambdaMOO subprocess.

`testbeds/{lambdamoo,toaststunt,moor}/` each hold a `setup.sh` that builds
that server from source on macOS or Linux with no Docker, plus `start.sh`
and `stop.sh`. Builds land in `.build/<os>-<arch>/`, so a checkout synced
between machines keeps one build per platform; the working databases in
`.run/` are shared. They listen on 127.0.0.1:17002, 17001 and 17003, each with a
non-wizard programmer `tester`/`tester`. The live round trip creates,
edits, pulls, recycles and recreates objects. It also covers package isolation,
imports, migrations, interrupted operations, reciprocal modules, selected
player fields and feature attachments from independent installations, then
removes the test objects:

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

AGPL-3.0-or-later; see [LICENSE](LICENSE).
