# Modules and packages implementation plan

Status: accepted design implemented and verified in the working tree.

Review date: 2026-10-08. Code reviewed: `56f3b4d`.

Consistency review and decision update: 2026-10-08. Lifecycle corrections and
engineering requirements are recorded at the end. The original review describes
the pre-implementation code at `56f3b4d`. The 2026-10-09 implementation is mapped
in [REVIEW.md](REVIEW.md), with commands and migrations in [README.md](README.md).

Support recursive folders under a world's `objects/` directory, named modules
with dependencies and scoped commands, and packages containing modules that
can be installed in zero or more worlds, including multiple named instances
of the same package in one world. Each instance has independent object
identities, configuration, local edits, and deployment history.

This document consolidates the repository review and the proposed module and
package design. The user accepted the recommended behavior with multiple
instances per world replacing the original single-instance limit. Exact
manifest fields, command names, and storage schemas below remain engineering
proposals. The accepted requirements are:

- Objects can be grouped in arbitrary subfolders under `objects/`.
- A module is a named group of objects with dependencies and scoped commands.
- A package is a collection of modules installable in zero to many worlds.
- Package source can exist independently of any world installation.
- Optional manifests define module boundaries; ordinary folders do not.
- A world can install the same package multiple times under distinct names.
- Ordinary references may be reciprocal across modules and instances;
  creation and initialization ordering must remain satisfiable.
- Installed objects are editable; updates preserve local changes or report
  file-level conflicts. New world-specific objects belong in local modules.
- The first release includes identity-preserving import of existing managed
  objects into an instance.

The first release uses local package sources, exact versions, and content
digests. Instances in the same world or different worlds may use different
versions and bindings. Dependencies can share an explicitly selected compatible
instance; incompatible requirements for that instance produce an error.
Remote registries, version-range solving, and executable installation hooks
are outside this first release.

The current implementation assumes a flat, complete set of world files:

| Area | Current behavior | Required change |
|---|---|---|
| [world.py](terramoo/world.py), `load_files` | Reads `objects/*.moo` and returns definitions without source paths. | Recursive discovery with a shared key-to-path catalogue. |
| [world.py](terramoo/world.py), `file_for` and `write_file` | Constructs `objects/<key>.moo`. | Separate locating an existing file from choosing a new destination. |
| [cli.py](terramoo/cli.py), pull and adopt | Independently scan the top level and write there. | Use the shared catalogue and preserve nested destinations. |
| [cli.py](terramoo/cli.py), rename and recovery | Reconstruct flat paths; snapshot checks scan the top level; temporary-file validation accepts only top-level parents. | Journal actual nested paths and validate the entire affected file tree. |
| [plan.py](terramoo/plan.py), `build` | Treats its input files as the entire world; missing registry keys become orphans. | Separate full-world context, selected mutations, and explicit destruction candidates. |
| [plan.py](terramoo/plan.py), `_at_refs` and `_exit_classes` | Walks structured references and classifies exits through managed ancestry. | Share reference analysis and retain ancestry context across scope boundaries. |
| [apply.py](terramoo/apply.py) | Creates, refreshes the registry, replans initialized objects, applies operations, and optionally destroys orphans. | Add dependency execution and installation outcomes while preserving identity checks. |
| [refs.py](terramoo/refs.py) | Resolves global keys and writes a registry snapshot to `state.json`; that snapshot is not loaded as desired state. | Keep live identity separate from package provenance and desired configuration. |
| [tests/test_live.py](tests/test_live.py) | Creates and cleans up top-level test files. | Exercise nested installations and clean up nested test files. |

The review reproduced these behaviors without changing repository code:

- A nested `objects/town/rooms/hall.moo` is not discovered.
- `file_for("hall")` chooses `objects/hall.moo` regardless of nested files.
- Supplying only one module's files to the current planner makes unrelated
  registry objects orphan candidates and rejects references to omitted files.
- Recreating a missing object can require updating references on an otherwise
  unchanged consumer in another module.

[REVIEW.md](REVIEW.md) contains older architectural details and metrics. Use
current source when implementing this proposal, then update that document.

The proposed source and installation layout is:

```text
packages/
  town/
    package.toml
    modules/
      core/
        module.toml
        generic_room.moo
      rooms/
        module.toml
        square.moo
        buildings/
          tavern.moo

worlds/
  alpha/
    world.toml
    packages.lock.json
    state.json
    objects/
      local/
        module.toml
        arrival_room.moo
      packages/
        north_town/
          core/
            module.toml
            north_town__generic_room.moo
          rooms/
            module.toml
            north_town__square.moo
            buildings/
              north_town__tavern.moo
        south_town/
          core/
            module.toml
            south_town__generic_room.moo
          rooms/
            module.toml
            south_town__square.moo
            buildings/
              south_town__tavern.moo
    .packages/
      north_town/
        base/
        deployment.json
      south_town/
        base/
        deployment.json
```

Only the world's `objects/` tree supplies its object definitions. Package
sources and retained baselines are outside that tree so recursive discovery
cannot accidentally deploy them. A package with no installations causes no
world operations.

Ordinary folders have no deployment meaning. A `module.toml` explicitly
creates a module; the nearest enclosing manifest determines membership. A
nested manifest begins a separate module, whose objects are excluded from
the enclosing module. Nesting does not imply a dependency or execution order.
An optional manifest at `objects/module.toml` can group otherwise loose files.

```toml
# packages/town/modules/rooms/module.toml
schema_version = 1
name = "rooms"
depends_on = ["core"]
uses_inputs = ["entry_room"]
```

Standalone module names are unique within the world. Package module names are
unique within the package and have instance-qualified command identifiers such
as `north_town/rooms` and `south_town/rooms`. Use `local/<name>` as the canonical
identifier of a standalone module, reserving `local` as an instance name.
Bare command selectors name standalone modules; a bare dependency inside package source names a
module of that package. Installed manifests use qualified references and
dependencies. Package, instance, namespace, and module names are ASCII
identifiers, compared without case and displayed with their declared spelling.
Slashes separate qualified module identifiers and never become part of an object key. Module identity
comes from its manifest, not its directory name.

In the layout above, `objects/local/module.toml` declares a standalone module
named `arrival`. The directory name and module name need not match. Compilation
turns the north instance's explicit dependency into `north_town/core` and its
input reference into `local/arrival`. `uses_inputs` declares the source-level
need for `entry_room`, whose provider is chosen by the world binding. It adds
a reference edge, without requiring the provider module to finish before
creation starts. Require it for every input used in structured values and
allow it for inputs needed only by runtime code. An optional
`depends_on_inputs` list, restricted to declared `uses_inputs`, requires those
providers to complete before the consuming module starts. An input bound
within the consuming module adds no reference self-edge; an explicit hard
self-dependency is an error. Source declarations and binding origins remain
in installation metadata; installed manifests contain the resolved graphs.

Existing files without a manifest continue to work in world-wide commands.
A named module that depends on an ungrouped object should report that the
provider needs a module. This makes dependency selection explicit. Ungrouped
objects retain their existing world-wide reference behavior.

A package explicitly lists the module roots it includes:

```toml
# packages/town/package.toml
schema_version = 1
name = "town"
version = "0.1.0"
modules = ["modules/core", "modules/rooms"]

[inputs]
entry_room = { type = "object", required = true }
```

Validate the complete package inventory. Each entry names the exact directory
containing one module manifest. List nested module manifests explicitly as
well. Their nearest-manifest membership excludes their objects from a listed
parent, so listing both is valid and does not include objects twice. Reject
unlisted modules, objects outside all listed modules, duplicate paths or
identities, and paths that escape the package. A package installation includes
all its modules.

World configuration declares named instances, their sources, namespaces, and
bindings. The table key is the instance name, independently of the package
name in `package.toml`. Source paths are relative to `world.toml`:

```toml
# Fragment of worlds/alpha/world.toml
[packages.north_town]
source = "../../packages/town"
namespace = "north_town"

[packages.north_town.bindings]
entry_room = "@arrival_room"

[packages.south_town]
source = "../../packages/town"
namespace = "south_town"

[packages.south_town.bindings]
entry_room = "@arrival_room"
```

Package source keys are unique across the package's modules. Compilation
maps them to ordinary world-global registry keys:

| Package source | Installed definition |
|---|---|
| `object square` | `object north_town__square` |
| `parent: @generic_room` | `parent: @north_town__generic_room` |
| `location: @entry_room` | `location: @arrival_room` for the binding above |
| `$room`, `$thing`, `@me` | Existing system-reference and player semantics |

The namespace defaults to the instance name. Instance names and namespaces
are unique within a world, including retained removal records. Package names
need not be unique among instances. Each instance gets a distinct key mapping;
the same source `square` becomes `south_town__square` in the south instance.
Bindings may intentionally refer to a shared external object, but two
instances cannot own the same registry object or key. All generated keys must
satisfy the existing ASCII identifier grammar and case-insensitive uniqueness
rules.
Reject the reserved object key `me`; references to it always mean the player.
Detect collisions across local files, mappings, and retained tombstones before
committing a local installation. Offline install cannot check the live registry.

Online plan/apply must separately reject every occupied new package key unless
a deployment receipt or durable pending-create intent proves that it belongs
to this installation. The current planner would otherwise update the existing
object, even if another owner of the key created it. A fresh generation found
under the expected key is not proof of installation ownership. Preserve helper
compare-and-set checks to catch races after preflight. Ordinary folder moves
do not change keys or generations.

Persist the source-key-to-installed-key mapping. Moving a package object
between modules preserves that mapping. A namespace change or source-key
rename requires an explicit migration; do not infer a rename from similar
contents or silently turn it into a delete/create operation. A migration
should preserve the existing live identity wherever possible.

Compilation operates on parsed object definitions and references, including
owners and references nested in property values. A declared input cannot
collide with a package object key or the reserved player reference `@me`.
Undeclared external `@` references are errors. Package source may use `#0`
and negative integer sentinels, but positive object numbers and UUID object
identifiers in structured values are errors; use declared inputs instead.
Initial object-input bindings accept managed `@` references, `$` references,
`@me`, and the same system/sentinel values. Unmanaged-object bindings require
a separate future dependency and liveness contract.

Reuse the structured walker, including its existing map-key collision check.
Two inputs can resolve to the same object; if that collapses distinct keys in
a MOO map, compilation must report an error rather than discard an entry.

Verb code remains literal MOO source. Do not perform string replacement in
code, quoted strings, or arbitrary text. Code using concrete object numbers,
registry-key strings, or dynamic lookups may not be portable. Static checks
cannot discover all such dependencies. Packages should access bound objects
through structured properties and declare remaining runtime dependencies.

Build separate reference and ordering graphs spanning local modules and
instances. A `references` entry declares an ordinary reference to another
module. `depends_on` additionally requires the provider module to finish
before any operation in the consumer module starts, including creation and
its initialize callback. A structured reference into another module requires
one of these direct declarations; an input uses `uses_inputs` and optionally
`depends_on_inputs`. Bindings add edges only for consuming modules. Installed
definitions and manifests, including local edits, determine the effective
graphs. Require updated declarations when pull or local edits introduce a
new cross-module reference. Plan/apply take the transitive closure of both
reference and ordering edges and display all selected modules.

Ordinary reference cycles are valid. A local arrival room can refer to a
north-instance exit while that exit refers back through `entry_room`. Declare
the local reference to `north_town/rooms`; do not convert the input reference
into an initialization dependency. In source manifests, bare module names
address the same instance. The same package installed as `south_town` resolves
its internal references to south modules, never to north modules.

Reject unknown targets and explicit ordering self-dependencies. The ordering
graph and object-parent graph must be acyclic. Also validate their combination
with reference readiness: a module required to finish before another starts
cannot itself need a not-yet-created identity from that later module. Report
the conflicting paths and edge types before mutation. Dynamic initializer
behavior cannot be inferred from verb source; package authors must declare
its readiness requirements explicitly.

For execution, combine the reference and ordering edges, find strongly
connected components, and treat each reference-only component as one group.
An ordering edge inside such a component is an unsatisfiable completion
requirement and is rejected. The condensed graph gives provider-first group
order. Within a group, allocate all required objects in parent-first order,
reconcile their identities, then apply ordinary values and links. Thus a
reference cycle does not require a cyclic creation order. Existing MOO create
still runs initialization immediately; this design does not defer callbacks
or promise that an initializer can read future peer properties. Components
without cycles follow the same contract as single-module groups.

Package dependencies declare aliases in `package.toml`, each with a package
name and exact version. The lockfile additionally pins the selected provider's
content digest per resolved edge; an optional required digest constrains
resolution when supplied. First-release resolution has no version ranges.
A world instance maps every dependency alias to a named provider through its
`dependencies` table. These optional fragments show two town instances sharing
a dependency:

```toml
# Additional fields in packages/town/package.toml
[dependencies.common]
package = "foundation"
version = "1.0.0"
```

```toml
# Additional fields in worlds/alpha/world.toml
[packages.shared_core]
source = "../../packages/foundation"
namespace = "shared_core"

[packages.north_town.dependencies]
common = "shared_core"

[packages.south_town.dependencies]
common = "shared_core"
```

Assuming foundation provides a module named `base`, a source-module reference
or ordering entry `common/base` resolves to `shared_core/base` in each town
instance. A bare `core` in the north instance still means
`north_town/core`. Dependency aliases cannot be `local` or collide with a
source module name. Reject unknown aliases, modules, and incompatible pins.

Provider source paths and input bindings come from the selected provider's
own world configuration, relative to `world.toml`. Do not guess a provider
by package name when several instances exist or inherit bindings from a
consumer. Prepare required configured providers with an install transaction
when absent; reuse compatible installed providers without resetting their
files or local edits. Uninstalled package check validates declaration syntax
and alias use without needing a world; installation additionally validates
resolved provider modules and pins. Ordinary cross-package object values use
declared inputs bound to the selected world's keys. Source does not embed a
world instance's namespace.

Several consumers may explicitly select the same compatible provider, or
select independent instances with different versions and bindings. An exact
version/digest mismatch on a selected shared provider is an error, not an
implicit upgrade. Updating a shared provider checks all surviving consumers'
requirements and graph declarations before committing. Display and journal
the affected dependency-pin changes in that explicit update; do not silently
rewrite consumers' definitions or baselines. A provider's local edits are
validated through the effective world graph; its source pin
alone does not prove runtime behavior. Shared providers are never removed
automatically with a consumer.

Record requested roots separately from resolved dependency instances and
collect dependency closure by instance identity with a visited set. Package
dependency declarations describe source availability, not initialization
order. Mutual declarations may resolve to explicitly configured instances;
their module graphs decide whether execution is possible. Do not recursively
invent instances to break a cycle. Bind every version to a content digest,
including local sources changed without a version change. Installed instances
keep their pinned snapshots until individually updated. Installing another
instance never silently updates a sibling or shared provider.

Installed files are independent, editable desired state. Pulling from one
world changes only that world's installation. Editing shared source changes
no installation until an explicit update. Package operations do not
automatically deploy to any world, and there is no implicit multi-world apply.

Keep desired configuration, provenance, and live identity separate:

| Record | Responsibility |
|---|---|
| Package source | Authored modules and portable definitions. |
| `world.toml` | Named instances, sources, namespaces, bindings, and dependency-instance selections. |
| `packages.lock.json` | Per-instance versions and digests, module ownership, key mappings, resolved dependency instances, and lifecycle state. |
| World `objects/` | Editable definitions consumed by plan/apply. |
| `.packages/<instance>/base/` | Retained instance baseline for upgrade comparison and recovery. |
| Deployment receipt | Last observed key/object/generation bindings and pending-removal history. |
| Live registry | Authority for current object identity. |

Give each instance an immutable installation identifier distinct from its
world-local instance name, package name, source path, and namespace. The instance
name addresses config, paths, and commands; renaming it requires an explicit
metadata migration and never changes its namespace or live keys implicitly.
Reusing a retired name creates a new installation identifier after all prior
outcomes are resolved. Receipts and pending operations also identify the
world/player/toolbox they observed; copying files to another world must not
copy authority over live objects. A requested-version record, a compiled
baseline, and a deployed revision are different records. Persist a desired
revision digest and record outcomes per object/module. Applying one module
cannot mark the entire instance deployed, and a historic receipt cannot prove
that the live MOO has not drifted since then.

The baseline must be available even after a local source directory changes.
Retain the exact input snapshot and enough compilation configuration to
reproduce the installed baseline. These records belong with world files and
backups; they are not disposable cache entries. Version their schemas and
reject unsupported or malformed records. Keep credentials out of every
manifest, lockfile, baseline, and receipt.

Normal world check, plan, apply, and pull use the pinned snapshot and installed
files, even if the original package directory is unavailable. World lifecycle
commands read source only for explicit install/update; package check reads
its provided source independently. Check for concurrent source edits while
capturing it; never lock mixed contents under one digest. A change to namespace,
bindings, or other compilation settings in `world.toml` requires a staged
package update before apply. Do not recompile silently during a normal plan.
Update only the intended configuration fields and preserve unrelated settings.

The following command interface is proposed. Configure required bindings
before installation; missing bindings produce an error before local or
remote mutation.

| Command | Proposed behavior |
|---|---|
| `tmoo check` | Validate the complete local world layout, manifests, references, and installation metadata offline. |
| `tmoo modules` | List module identities, paths, dependencies, and counts offline. |
| `tmoo package check packages/town` | Validate an uninstalled package without a world or connection. |
| `tmoo -w alpha package install packages/town --as north_town` | Prepare the named instance's configuration, pinned baseline, and materialized files. |
| `tmoo -w alpha package install packages/town --as south_town` | Prepare a second independent instance of the same source in alpha. |
| `tmoo -w alpha package import packages/town --as north_town --mapping import.toml` | Claim explicitly mapped existing managed objects for a new instance while preserving their keys, objects, and generations. |
| `tmoo -w alpha package update north_town` | Prepare an upgrade of this instance using its recorded baseline and local edits. |
| `tmoo -w alpha package remove north_town` | Prepare this instance's removal with retained ownership and recovery records; enable only after the removal design gate passes. |
| `tmoo -w alpha package recover` | Reconcile an interrupted local transaction or remote operation from its durable journal. |
| `tmoo -w alpha package update north_town --resume` | Recheck and commit a resolved upgrade candidate, leaving active files untouched until it is valid. |
| `tmoo -w alpha package update north_town --abort` | Discard this instance's uncommitted candidate; it does not roll back an applied world. |
| `tmoo -w alpha plan --package north_town` | Preview this instance and its reference/ordering closure. |
| `tmoo -w alpha apply --package north_town` | Apply that closure in creation and initialization order, handling reciprocal references as a group. |
| `tmoo -w alpha plan --module north_town/rooms` | Preview this module and its reference/ordering closure. |
| `tmoo -w alpha status --package north_town` | Report this instance's desired and live status, including incomplete operations. |
| `tmoo -w alpha diff --module north_town/rooms` | Compare this module's desired definitions with live objects. |
| `tmoo -w alpha pull --package north_town` | Update this instance's world files without changing siblings, shared source, or its upgrade baseline. |
| `tmoo adopt '#123' hall --into local/rooms` | Adopt into a validated directory relative to `objects/`. |
| `tmoo pull hall --into local/rooms` | Restore a missing standalone definition at an explicit destination. |
| `tmoo rename-key hall great_hall` | Rename in the existing directory and rewrite parsed references throughout the world. |

The `--as` option explicitly names an instance for install/import. Lifecycle
arguments and `--package` always identify an instance, never all installations
of a source package. `--package town` is an error if no instance is named
`town`, even if several instances use the town source. Repeated selectors
explicitly select several instances. Repeating install for an existing name
reports its state or the required update/recovery action; it does not replace
that instance or reset local edits. Sharing a source alone never expands a
command's selection; declared references and ordering dependencies can. Each
command's output names the instance and its source package/version separately.

Package install/update/remove stage desired changes; apply performs their
remote mutations. Import additionally verifies and claims existing identities
through the ownership protocol below, without changing object definitions.
Existing bootstrap, adopt, and rename-key retain their remote behavior.
Recovery may reconcile a previous remote operation but must
not start an unrelated deployment. Output distinguishes prepared, last applied,
conflicted candidate, removing, partially applied, and unknown remote outcome.
A removal remains selectable through retained manifests and tombstone records,
even after its desired files are gone.

These states have separate completion conditions:

| Transition | Local result | Remote completion rule |
|---|---|---|
| Install | New pinned baseline, desired files, and mapping; no deployed claims. | Each selected object is created or matched to a recorded create intent, reconciled, and recorded. |
| Import | Record a complete explicit mapping, source baseline, retained local definitions, and original paths through a journaled transaction. | Verify and claim the existing exact identities; record live observations without claiming unapplied desired changes are deployed. |
| Update | A validated candidate replaces desired files and baseline through a journaled commit; old deployment receipts remain until refreshed. | Record the desired digest actually reconciled for each selected object/module. |
| Conflicting update | Active files, lockfile, and baseline remain unchanged; candidate and conflicts are retained separately. | No mutation comes from the candidate until resume validates and commits it. |
| Partial or uncertain apply | Keep desired files and exact operation intents per instance and execution group. | Reconcile known identities, report unknown outcomes, and skip dependent groups until their prerequisites succeed. |
| Remove | Archive affected local content and retain mappings, old graph, and removal intents. | Complete only when selected bindings are confirmed removed or absent without rebinding conflicts. |

An apply without `--destroy` leaves removals pending and says so; it cannot
report the installation removed. A no-op object plan still needs metadata
reconciliation. Do not infer installation success merely from `Plan.empty`
or from a batch returning some successful operations.

Repeated selectors form a union. Combining explicit keys with selectors
must have a documented rule; the recommended initial behavior is to reject
the combination rather than silently intersect or broaden it. Plan/apply
always include the reference and ordering closure. Status, diff, and pull
select only the requested membership unless `--with-deps` is supplied, which
adds the same closure. The `export` alias follows pull.

Separate update scope from destruction scope. Dependency closure expands
creates/updates only. With `--destroy`, eligible removals belong only to the
explicitly selected instances/modules; a dependency's pending removals require
an explicit selector of their own. World-wide commands inspect the complete
world but still apply package ownership and removal checks. Unknown selectors
are errors; a valid empty module is reported as empty. No selector falls back
to the whole world merely because it matched no objects.

Only desired modules contribute to the update dependency closure. Retained
manifests for removals supply historical ordering and ownership; they cannot
recreate a removed module or trigger dependency updates merely to delete its
former consumer.

Without selectors, commands retain world-wide behavior. Full pull preserves
existing paths. For missing package files, use recorded installation paths;
for unknown standalone paths, write at the root unless given `--into`. Do not
infer membership from key prefixes. Do not resurrect files explicitly marked
for removal during an ordinary pull; diagnose the pending removal instead.

Before any package pull writes, verify its live identities against installation
records and validate the complete selected export. A pending or partly applied
revision of the selected objects could otherwise be replaced by the older
live definitions. Refuse the selected write batch in that state and explain
whether apply or recovery is needed.

New, never-created objects remain local and are reported without export. Once
the relevant desired revision was fully applied, pull retains its usual
meaning of replacing selected local definitions with live ones; it updates
neither the upstream baseline nor the package source. If the pulled values
require a new dependency, write/report the desired data and require fixing the
manifest before the next apply. This is not proof of a valid dependency graph.

Ordinary adopt into an instance-owned directory is refused; use standalone
world modules for new world-specific extensions. Converting existing managed
objects into an instance uses the explicit import command. Refuse unrecorded
moves across installation boundaries and explain the required ownership
migration. Moves within the same recorded module can update the path index
without changing object identity.

Import is required in the first release. Its mapping file maps every source
object key to a distinct existing managed world key; partial imports with
implicit creation are not supported initially. Existing keys can differ from
the instance namespace. Persist these overrides and compile internal refs,
inputs, and the baseline through them on every later update. New upstream
objects use the instance namespace, with normal collision checks. Never
infer ownership from a prefix or claim an object already owned by another
instance. Its namespace remains the default for future additions.

Under the world lock, validate the complete mapping, local files and manifests,
source pin, bindings, and both effective graphs. Read and verify live registry
objects and generations; refuse unknown or pending outcomes. A registry entry
without its managed local definition must first be pulled or adopted through
the existing workflow. Present the ownership/path changes and any desired/live
differences before committing the import. Preserve local object contents as
instance edits against the compiled source baseline; do not overwrite them
with source or imply that they have already been applied.

Journal ownership claims, original paths/membership, file moves, configuration,
and receipts. When taking objects from standalone modules, retain residual
modules and explicitly account for changed reference/ordering declarations
throughout the world. Do not silently convert a completion dependency into
a reference declaration. The resulting graphs must validate before commit.
Other instances are unchanged except for necessary, displayed declarations
that target the imported objects' new module identities. Keys, object numbers,
generations, and structured bindings remain unchanged; import sends no create,
recycle, or implicit registry rename operations. Follow the same generation
checks and protected ownership protocol required for subsequent removal. A
crash between remote claim and local commit must resume or undo that claim
from the journal without recycling the imported objects. Source-key or namespace
renames are separate explicit migrations after import.

Track installed membership independently of path discovery. A missing package
manifest must not silently transfer its objects into an enclosing module.
Initially reject unrecorded added/deleted package files and cross-module moves;
stage upstream changes through update and keep world-specific additions in
standalone modules. Permit a move within the same recorded module after
collision checks, and journal the path-index update with the next mutating
command. Check/plan can report such a move without writing metadata. Updates
retain local paths unless an explicitly reviewed migration changes them.

Introduce a shared catalogue containing definition paths, key spelling,
module membership, package provenance, and filesystem snapshots. Keep this
metadata outside `ObjectDef`; filesystem location must not affect comparison
with a live export. Split discovery from definition parsing so pull can still
repair malformed files and `adopt --verify` can preserve arbitrary local bytes.

Use deterministic recursive traversal. Reject duplicate keys with both paths
in the diagnostic. Report unreadable or incomplete directories rather than
treating them as empty. The recommended initial policy rejects symlinked
object files, manifests, and traversed directories. Specify treatment of
hidden directories and temporary files centrally: include ordinary hidden
directories in traversal, ignore exact tool-owned journal/staging paths outside
`objects/`, and treat only regular `*.moo` files and exact `module.toml` files
as object-tree inputs. Do not apply Git ignore rules. Hidden `*.moo` files with
invalid keys are errors. A missing or unreadable required root is an error;
an existing empty standalone objects directory remains valid. Never let
different commands discover different desired object sets.

Replace every independent glob in loading, pull, adopt, rename, snapshot
validation, and live-test cleanup. New destinations must stay within their
allowed roots and be checked before remote registration. Preserve existing
filename spelling and property/verb ordering. `--into` chooses destinations
for new standalone files and does not silently move existing ones.

Planning needs the full catalogue and live registry separately from the
selected mutation keys. Compute creates, updates, and pending references for
the selection; retain wider context for ancestry, dependency validation,
incoming references, and ownership checks. Do not scope the existing planner
by simply deleting input definitions or filtering its finished operations.

Keep stage-specific `Refs.pending` isolated: the current builder mutates it,
and apply clears it. Preplanning several execution groups against one mutable
`Refs` would lose pending-create information. Use a full desired context plus
a fresh per-stage resolution state. Within a reciprocal-reference group, track
all pending identities together until they have been allocated and refreshed.
Preserve planned old/new binding relationships when dependencies recreate
objects. Validate all structural catalogue conflicts globally; scope live diffs
and readiness checks to selected
objects and the additional context needed for ancestry and impact analysis.

A scoped recreation can change the object number referenced by unselected
consumers. Detect known consumers before mutation and stop with the modules
that must also be selected, or require a whole-world apply. Do not silently
broaden the operation. This analysis covers known structured references;
also normalize raw object-number values through the live registry when they
identify managed objects in existing world definitions. `_at_refs` alone
does not see them. If old identity evidence is insufficient after an object
has already vanished, require explicit symbolic references or operator
reconciliation instead of assuming no consumers. A larger selection alone
does not make an ambiguous raw object number a reliable reference.
Dynamic verb behavior and unmanaged live objects remain evidence limits.

Only selected objects belong in the final exit-link operation. Preserve full
ancestry context during planning and post-create reconciliation. Scope states
which definitions terramoo applies; it cannot guarantee that MOO callbacks,
inheritance, movement, or room memberships have no effects elsewhere. Show
known effects, including an exit's old and new endpoint rooms, in the preview.

Complete provider groups before dependent groups using the validated graphs.
For each reference-only group, create missing objects across its modules in
parent-first order, refreshing the registry and preserving generation checks.
After all identities are available, re-export and replan the group's selected
objects against the complete desired context, then apply definitions and link
its selected exits. Creation still invokes core initialization. Refresh before
each dependent group because earlier changes can alter inherited properties
and numeric verb descriptors. A failed allocation stops that group before its
ordinary-value/link phase; record successful creates for recovery. Runtime
replanning must stay within the reviewed selection and desired definitions;
it cannot authorize additional objects or destruction. Stop if refreshed state
requires a wider operation.

Freeze the desired files, manifests, mappings, and selection used for preview,
then recheck their snapshots before mutation and between stages. Unexpected
local changes require a new plan. Earlier remote stages may already have
succeeded; preserve their receipts. Check known dialect restrictions and
resolve required `$` references before sending mutations. Offline check cannot
prove live system-object availability or successful MOO verb compilation.
Reject desired properties excluded by the world's effective runtime-property
policy; do not silently strip them or repeatedly try to apply values that
export will omit.

If a group fails, skip its dependent groups and return failure. Independent
selected groups may continue. Record successful per-object outcomes without
declaring a reciprocal-reference group complete after partial application.
Retain endpoint compensation, batch limits,
per-operation diagnostics, and the rule that earlier failures prevent
destruction. Apply ungrouped objects after named modules in a whole-world
run. Worlds without manifests retain their existing execution path.

Package compilation and dependency resolution should be local Python work.
Non-destructive grouping and installation should not require changes to the
object-file grammar, MOO registry shape, or transport APIs. Preserve the
single-expression transport contract and the plain LambdaMOO 1.8 helper
constraints in [AGENTS.md](AGENTS.md).

For updates, compare the previous compiled baseline `B`, current installed
files `L`, and newly compiled source `N`. Apply these ordered rules, treating
absence as a value:

| Condition | Result |
|---|---|
| `L == N` | Keep the common result, including identical edits or deletions. |
| `L == B` | Take the upstream result `N`. |
| `N == B` | Preserve the local result `L`. |
| Otherwise | Record a conflict; do not change the active installation. |

Use conservative file-level comparison initially. Compare files by stable
source identity and mapping, not just by path. Merge installed module
manifests as well as objects, then validate the complete candidate graph and
inventory. A conflict-free text merge can still create an invalid dependency
or a key collision and must not be committed. Rebinding inputs follows the
same comparison process. Keep the new upstream baseline separate from the
merged local result so later updates still recognize retained local edits.

Absence comparisons apply to validated inventories and recorded removals.
An unexplained missing active package file is an error before merging; this
comparison table does not turn accidental file loss into deletion intent.

Retain a conflicted candidate outside `objects/`, with old/local/new content
and conflict records. The user can edit the candidate and run update with
`--resume`; recheck the active snapshots before committing. `--abort` removes
only that uncommitted candidate. Active changes since staging invalidate resume
until the candidate is rebuilt. Later source edits do not alter the captured
candidate; start a new update to include them. Do not permit arbitrary partial
replacement of the active installation during conflict resolution.

Track additions, deletions, folder moves, module moves, and key migrations
separately. A move preserves identity. A key migration must also update
installation mappings, references, and recovery metadata. Package source is
not rewritten by pull, update, or rename; publishing world edits back into
source is a separate future workflow.

Installed key renames must update every affected installation mapping,
structured world binding, receipt, and tombstone in the same recovery
transaction as object references and the remote registry rename. Recompute
comparison baselines under the recorded identity migration; preserve immutable
source snapshots. Otherwise an unchanged upstream update would undo the
rename. Namespace or source-key migrations involving multiple registry renames
need their own reviewed recovery protocol; do not extend the single-key
journal by assuming a series of renames is atomic. Key migration is separate
from a registry-preserving folder move.

All local mutations use one world write lock. Modules and packages share a
registry and login identity, so narrower locks would permit conflicting
commands. This lock coordinates one checkout; it is not a distributed lock
across machines or independent checkouts.

Stage file, configuration, lockfile, baseline, and receipt changes before
replacing active data. Journal intent and affected paths, fsync temporary
files and affected directories, and retain enough information for recovery.
Check snapshots for additions, removals, moves, and edits throughout the
affected tree. Do not overwrite concurrent user edits during recovery.

A multi-file replacement is not an atomic filesystem transaction. The durable
journal makes intermediate states detectable and recoverable. Check/plan must
refuse a mixed installation while such a journal exists; status may report
the journal without parsing mixed desired files. Coordinate read-only commands
with writer commits using a shared world lock or validated read snapshots.
The current exclusive writer lock alone does not protect readers. Exclude a
conflicted but uncommitted candidate from active discovery; it need not prevent
read-only inspection of the intact active installation.

Extend rename recovery to record actual relative source and destination
paths, version its journal, and read existing flat-layout journals. Validate
each recorded temporary path before cleanup. Do not broadly permit deletion
of arbitrary temporary files under a world. While an unresolved journal can
leave files or ownership inconsistent, block other mutating commands and
provide a specific recovery command. Read-only diagnostics should identify
the interrupted operation.

Package lifecycle changes can partly succeed remotely. Save observed outcomes
and keep unresolved removals or creates visible. A local rollback is not a
rollback of the MOO; recovery must reconcile the live registry and exact
object generations before deciding what remains to do.

Before sending any package create, fsync an intent containing the installation
identifier, desired revision, key, expected old binding, and proposed generation
nonce. The current apply code generates nonces only in memory. A crash after
remote success but before receipt storage must be recoverable by matching that
recorded nonce, not by accepting whichever object now has the key. An
initialize callback that registered a different object must remain a conflict.
Use similarly durable intents for rename and removal, persist reconciled
outcomes per stage, and stop on ambiguous identity; never retry an unknown
mutation blindly. Allow only recovery and diagnostics while an unknown remote
outcome remains unresolved.

Installation history provides membership information that folders alone
lose when a definition is deleted. Use it to propose package-scoped deletion,
with tombstones retained until removal succeeds. Every candidate must have
recorded ownership, be absent from the complete desired world, have no known
surviving managed references, and match its expected live key, object, and
generation. Missing ownership or identity evidence blocks that deletion.

Do not require a definition to exist for every registry key merely to plan a
removal. Build the removal graph from retained membership and dependency data,
then check the surviving desired world and export surviving managed definitions
for incoming-reference analysis. If required surviving definitions cannot be
verified or exported, do not claim the deletion is checked. Resolve symbolic,
system, and raw object references against the same identity snapshot. Two
instances explicitly removed together need not block each other as surviving
dependents. Condense ordinary-reference cycles among removal candidates into
groups, destroy consumer groups before provider groups, and preserve reverse
creation/initialization order and children-before-parents within each group.
Use deterministic key order for remaining ties. A cycle consisting entirely
of objects selected for removal is allowed; an incoming surviving reference
blocks it. A failed removal stops the rest of that group and its provider
groups. Earlier removals are not undone, and retained receipts identify the
remaining objects and possible broken references after partial failure.
Recheck references after selected updates and before
deletion, since callbacks may have changed them. If ordering or cleanup cannot
be established, report the conflict and keep the provider.

These checks cover managed definitions, not every reference inside arbitrary
runtime state or unmanaged objects. They also do not create a global MOO
transaction: other tasks can run between exports and mutations. Preserve
per-operation identity checks, and make those limits part of the removal
protocol review rather than promising total referential isolation.

Removing one instance must not remove a sibling instance or its dependencies
automatically. Block removal while surviving modules or instances require it.
Diagnose modified local files before staging their removal and retain
recoverable contents; do not discard local edits silently. Detect malformed metadata or an absent
installation tree before deriving orphans, including in world-wide
`apply --destroy`.

An explicit removing state is the exception to the absent-tree check: its
archived manifests and tombstones provide the expected missing-file inventory.
An installed state with the same missing tree is an error. Never finish a
removal just because the desired directory is absent. If an instance has never
been deployed, still reconcile any create intents and inspect the live keys
before retiring its records. Preserve final removal history and prevent key
reuse while an outcome is unresolved.

Scoped deletion must pass a separate design and test gate. Local receipts
provide history, but their adequacy must be checked against database rollback,
stale checkouts, ownership transfers, and partial deployment. If live ownership
cannot be established safely with the existing protocol, add protected remote
installation metadata and a versioned helper migration. Do not claim that
the current registry contains package ownership. Until this is resolved,
reject package/module-scoped `--destroy` explicitly.

This restriction also applies to removals introduced by upgrades and to
package-owned candidates in world-wide `--destroy`; the latter must not bypass
the gate. Until the gate passes, reject package remove and updates that drop
managed definitions before staging their active files away. Existing
standalone destruction can retain its documented whole-world behavior. The
completed package release must include the removal protocol and recovery
tests; an install-only intermediate milestone is not completion of this plan.

Once that gate passes, package-owned modules can use the same history for
scoped deletion. Standalone modules need equivalent historical membership
before supporting it. Never infer former membership from a key prefix or
from a cache. Never run unattended destruction against a real world during
development or review.

Implement the work in the following sequence. Names of new Python files are
proposed boundaries, not a requirement to introduce every file immediately.

1. Add recursive discovery and path-preserving writes in a shared
   `catalog.py`, integrated with `World` and all CLI readers/writers. Moving
   an unchanged file into a subfolder must produce no MOO changes.
2. Update rename and recovery for nested paths, recursive snapshots, and
   validated journal cleanup. Preserve legacy journal compatibility and
   existing concurrent-edit protection before enabling nested file writes.
   Deliver these first two phases together.
3. Add `modules.py` for manifest parsing, membership, reference analysis,
   reference and ordering graphs, scheduling validation, and selection.
   Add offline check and listing commands. Cover reciprocal references and
   conflicting initialization requirements. Keep flat worlds working without
   manifests.
4. Refactor pure planning to receive complete context plus explicit scope
   and destruction candidates. Test unrelated objects, cross-module ancestry,
   and recreation consumers before exposing scoped apply.
5. Add `packages.py` for package inventory, per-instance bindings and namespaces,
   dependency aliases resolved to named instances, stable key mappings, and
   compilation into world definitions. Validate uninstalled packages without
   contacting a world. Test two installations of the same source in one world
   and independent versions before exposing package commands.
6. Add installation storage and transactions, using `installation.py` or a
   similarly focused component. Implement local install, retained baselines,
   lockfiles, deployment receipts, and crash recovery. Agree and version the
   serialized schemas before persisting installations.
7. Integrate instance/module selectors and group execution with CLI and apply.
   Allocate reciprocal-reference groups before applying their ordinary values,
   preserving nonce, registry revision, callback, and transport behavior.
   Record actual partial outcomes per instance and group.
8. Implement conflict-preserving updates, identity-preserving import, and
   explicit ownership/key migrations. Complete the ownership and recovery
   design gate before enabling import claims or removal with `--destroy`.
9. Update README, REVIEW, examples, and migration guidance. Extend offline
   and live tests, including recursive teardown, sibling instances, imports,
   reciprocal references, and independent worlds.

Verification should cover the following observable behavior:

| Area | Acceptance checks |
|---|---|
| Discovery | Flat and nested layouts; duplicate keys; case collisions; unreadable directories; symlinks; deterministic traversal; filename/header mismatch diagnostics. |
| File writers | Existing paths survive pull; property and verb order is preserved; malformed files can be repaired; adopt validates destinations and collisions; `--verify` preserves bytes and exclusive creation. |
| Module graphs | Chains, diamonds, valid reciprocal references, rejected parent/initialization cycles and mixed unsatisfiable ordering, nested boundaries, unknown targets, ungrouped targets, missing manifests, and direct declarations. |
| Package source | Zero installations have no world effect; module inventories are complete; escaped paths, input/key collisions, and map-key collisions after binding are rejected. |
| Installation | The same package installs multiple times in one world and across worlds, with independent versions, bindings, edits, mappings, and receipts; duplicate names/namespaces and occupied keys are rejected. |
| Dependency instances | Explicit sharing reuses a compatible provider; separate providers may differ; ambiguous selection, incompatible pins, and implicit shared upgrades fail; cyclic availability declarations terminate without inventing instances. |
| Scope | Instance names select exactly the addressed roots; unrelated siblings never become orphan candidates; reference/ordering closure is displayed; failed groups skip consumers; recreation identifies outside consumers. |
| Import | Complete explicit mappings preserve keys, object identities, generations, and local content; another instance's objects cannot be claimed; changed module membership is validated; interrupted ownership claims recover; unchanged source updates preserve imported mappings. |
| Live behavior | Parent/child initialization, changed inheritance, numeric verb descriptors, cross-module exits, callback failures, and endpoint compensation retain their guarantees. |
| Update | Unchanged, local-only, upstream-only, conflicting, deleted, moved, and rebound files behave as specified; conflicts preserve the active installation and baseline. |
| Recovery | Inject failure before and after replacement, registry mutation, and receipt writes; cover lost responses, case-only renames, concurrent edits, corrupt journals, and stale metadata. |
| Removal | Shared dependencies and siblings survive; surviving references block removal; fully selected reference cycles have deterministic teardown; wrong generations and missing records block deletion; retries recover partial groups. |
| Repeatability | Apply, pull, and a second plan produce no changes for an unchanged desired world; installed snapshots remain usable after source changes. |
| Compatibility | Existing flat worlds, legacy bindings, rename recovery, all three MOO servers, and MCP fake coverage remain supported. |

Exercise complete command sequences in addition to component tests:

| Sequence | Required result |
|---|---|
| Check a source package with no `worlds/` directory | Validate portable source only; do not require a selected world or credentials. |
| Install the same source into alpha and beta, then change the source | Both worlds retain their pinned version until individually updated. |
| Install town as north_town and south_town in alpha; edit, update, pull, and remove only north_town | Each instance keeps distinct keys, baselines, and receipts; south_town retains its pinned revision and edits. Declared cross-instance references must be shown if they expand apply scope. |
| Give two consumers a shared provider, then attempt an incompatible update | Reject before changing active files; resolve by coordinated compatible requirements or an explicitly different provider instance. |
| Apply a local arrival room and package exit with reciprocal ordinary references | Preview the complete group; allocate identities before applying reciprocal values; a second plan has no changes. |
| Add a hard initialization edge inside that reciprocal group | Reject the unsatisfiable ordering with the conflicting declarations before mutation. |
| Fail halfway through creating a reciprocal-reference group | Retain exact successful create intents and receipts; do not apply group values or run dependent groups until recovered. |
| Import existing local objects as north_town, then install the same source as south_town | North preserves existing keys and generations; south receives fresh namespaced keys; updating either preserves their distinct mappings. |
| Interrupt import after an ownership claim but before moving files | Recovery uses the durable mapping and original membership; no objects are created, renamed, or recycled. |
| Install while offline, then discover an occupied key during plan | Keep prepared local files; reject online deployment without touching the occupant. |
| Apply core, fail rooms, then inspect status and pull | Core may be last-applied; rooms remains pending; pull of the whole package refuses to replace pending definitions. |
| Lose the create response and restart | Recover ownership only from the fsynced proposed nonce and exact live binding; never use key spelling alone. |
| Stage an update, then pull before applying | Refuse writes that would overwrite the pending revision with old live data. |
| Make the same edit locally and upstream, then update | Accept the common result; do not report a conflict merely because both changed. |
| Resolve an upgrade conflict while active files also change | Resume refuses stale snapshots; active installation remains intact. |
| Rename an installed key referenced by another instance's world binding, then update unchanged source | Update all affected mappings and bindings; the next update keeps the renamed identity, including when both instances share source. |
| Select north_town with a dependency that has its own pending removals | Apply dependency updates as required, but do not delete its objects without an explicit selector. |
| Remove consumer and provider together | Use retained graph and exact bindings; remove consumers first; stop provider deletion if consumer removal fails. |
| Remove both sides of a reciprocal-reference group and inject a recycle failure | Permit the fully selected group, stop its remaining removals and provider groups on failure, retain partial outcomes, and keep sibling instances outside the selection. |
| Delete a manifest or installation tree without a recorded removal | Report incomplete metadata; do not reassign ownership or derive package orphans. |
| Interrupt replacement after one file but before the lockfile | Readers report the journal; recovery restores a coherent desired revision. |
| Run an older flat-only client against nested definitions | Demonstrate the orphan hazard in a local fake; document that this downgrade is unsupported. |

Run the offline suite, then the live suite on each testbed sequentially:

```sh
uv run pytest

# With only the corresponding scratch testbed running:
TMOO_LIVE=127.0.0.1:17001:tester:tester uv run pytest tests/test_live.py
TMOO_LIVE=127.0.0.1:17002:tester:tester uv run pytest tests/test_live.py
TMOO_LIVE=127.0.0.1:17003:tester:tester uv run pytest tests/test_live.py
```

Start and stop each testbed separately as documented in
[README.md](README.md#testbeds-and-tests). Preserve the fixture's refusal to
destroy a registry containing non-test objects. Extend its cleanup before
introducing nested test objects. These commands are an implementation
validation plan, not a record of live tests run during this review.

The offline baseline during the 2026-10-08 code review was 342 passed,
7 skipped, and 1 failed. The failure was
`test_connection_close_mid_request_is_reported_without_retry`; it passed on
a focused rerun. Inspection suggests the fake server can close during either
the write or read, while the assertion expects the read-side error. This is
not a clean full-suite pass. No live testbeds were run for the design review.

Migration should preserve identity: first install the new tool, move existing
files into folders, and verify an unchanged plan. Add standalone manifests
and explicit dependencies next. Install new packages without reusing occupied
keys. Converting existing world objects into a package installation needs an
explicit import/mapping operation included in the first release; ordinary
package install must reject those collisions. Use a new named instance for
each additional copy, even when the source and version match. Keep world
files, installation records, baselines, and database backups together when
changing versions or recovering a world.

An older flat-only client will ignore nested files and can propose recycling
their registry objects with `--destroy`. A new minimum-version marker cannot
make an old client enforce a rule it does not implement. Do not describe this
change as safe for mixed client versions. Record a world-format version for
new clients and document a coordinated upgrade; downgrade requires a separately
validated flatten/export procedure or restoration of a compatible world and
database snapshot. Do not test this hazard against a real world.

The 2026-10-08 consistency review found and corrected these gaps in the
proposal. This table records the requirements established by that review:

| ID | Severity | Gap | Correction or remaining limit |
|---|---|---|---|
| R1 | High | Offline install promised live key-collision protection. | Separate local preparation from online ownership preflight; occupied unowned keys are rejected before diffing. |
| R2 | High | Lost create responses could leave no durable ownership evidence. | Persist proposed generation nonces and expected bindings before sending; reconcile exact intents after restart. |
| R3 | High | Dependency closure could widen `--destroy` or delete providers first. | Distinct update/destruction scopes, retained removal graphs, consumer-first ordering, and failure propagation. |
| R4 | High | Pull could overwrite a staged or partly applied upgrade. | Check per-object desired/deployed revisions and refuse the affected write batch. |
| R5 | High | Global names, package inputs, and direct dependency declarations were inconsistent. | Instance-qualified module identities, explicit input use, separate reference/ordering graphs, and allocation groups permit reciprocal data references while rejecting impossible initialization order. |
| R6 | High | Key renames could leave bindings and upgrade baselines stale. | Include installation mappings, world bindings, receipts, and baseline transformations in rename recovery. |
| R7 | High | Missing manifests or an installation tree could become removals or change ownership. | Validate against recorded inventory and distinguish an explicit removing state from accidental absence. |
| R8 | High | Filesystem transactions and remote success were described too broadly. | Readers detect mixed revisions; lifecycle states track partial outcomes; no-op plans do not imply installation completion. |
| R9 | Medium | Every two-sided update was a conflict, including identical edits. | Ordered three-way comparison, graph validation, and explicit candidate resume/abort rules. |
| R10 | High | Reference impact checks using only `_at_refs` miss managed numeric references. | Normalize known structured object identities and block ambiguous references; dynamic code remains outside static coverage. |
| R11 | Medium | Normal operation could depend on changed or unavailable package source. | Pin immutable snapshots and use them until explicit update; never silently recompile changed configuration. |
| R12 | High | Compatibility language omitted destructive old-client behavior. | Document the flat-client downgrade hazard and avoid claiming that a new format marker protects old clients. |

Four isolated checks against the existing Python code supported the review:
an occupied package key produced an update with zero creates; orphan iteration
followed registry insertion order rather than dependency order; `@me` resolved
to the player even with a registry key named `me`; and `_at_refs` did not report
a property containing a raw managed object number. These checks used in-memory
data and no live MOO. The lifecycle sequences above are design walkthroughs
and test requirements at the time of the document-only review. The subsequent
implementation adds executable package tests; verification is recorded below.

The user-facing decisions are settled: nearest-manifest module membership,
multiple named instances of a package per world, reciprocal ordinary references,
editable installations with conservative upgrade conflicts, local sources with
exact pins, and identity-preserving import in the first release. The prior
single-instance limit and blanket cross-module cycle rejection are superseded.
Independent instances may share explicitly bound objects or dependency instances;
sharing never implies shared ownership of an instance's own objects.

The review assigned these implementation requirements without reopening user
decisions:

1. Finalize and validate the serialized dependency-alias declarations and
   per-instance resolution records described above. Ensure source digests,
   selected providers, input bindings, and local edits remain consistent
   during shared-provider updates.
2. Specify the lockfile, retained snapshot, per-instance deployment receipt,
   import mapping, and recovery schemas, including versions and backups.
   Finalize their command diagnostics and transaction boundaries before use.
3. Validate the reference-group execution algorithm against current create
   callbacks, parent-first allocation, post-create replanning, mixed ordering
   conflicts, and failures spanning several instances. Ordinary cycles must
   not be rejected simply to avoid this work.
4. Prove scoped ownership for import and deletion across rollback, stale
   checkouts, transfers, and partial deployment, or implement protected remote
   metadata with a versioned helper migration. Test partial teardown of
   reciprocal-reference groups. Do not enable those ownership-changing paths
   until the protocol is validated.
5. Complete the import and multi-key migration journals, including recovery
   after remote claims or renames, membership changes, sibling isolation,
   baseline transformations, and dependency-instance bookkeeping.

Recursive discovery and nested writer/recovery work can proceed first. The
completed package release must meet these engineering requirements; unresolved
ownership or recovery design is not a reason to silently omit accepted scope.

## Implementation record, 2026-10-09

The working tree implements recursive discovery and path-preserving writers;
nearest-manifest modules and scoped commands; reference groups and initialization
scheduling; portable package validation; independent instances and explicitly
shared exact dependencies; retained snapshots; editable installations and
three-way conflict candidates; identity-preserving import; key, namespace,
source-key, and instance migrations; guarded pull; scoped removal; and recovery.

Persistent formats use schema version 1. Helper version 13 adds an ownership
epoch checked alongside registry generations. Per-object receipts distinguish
pending desired revisions from completed deployment. Source snapshots remain
immutable; identity migrations transform compiled baselines and world bindings.
Installed paths retain source module paths, for example
`objects/packages/north_town/modules/rooms/north_town__square.moo` when the source
module root is `modules/rooms`.

Implementation files and recovery protocols are documented in
[REVIEW.md](REVIEW.md). A runnable source is in
[examples/packages/town](examples/packages/town/package.toml).

Verification completed on 2026-10-09:

- Full offline suite: **397 passed, 12 live tests skipped**.
- Live suite: **all 12 tests passed** on each of LambdaMOO 1.8.1, ToastStunt,
  and mooR. Testbeds ran sequentially and were stopped afterward.
- Expanded lifecycle tests were rerun after the final fixes on all three:
  lost import, create, rename, and deletion responses; independent instances;
  reinstall with a new generation; instance rename; pending-update pull;
  missing-file restoration; and namespace/case-only key migration.
- Filesystem fault tests cover interrupted replacements, concurrent edits,
  unsafe recovery paths, and a case-only rename interrupted between content
  replacement and filename spelling correction on a case-insensitive filesystem.
- The example package passes offline validation. Local documentation links
  resolve, Python compilation succeeds, and `git diff --check` passes.

Review corrections included retaining complete live ancestry during scoped
post-create replanning; preserving deployment tokens across uncertain outcomes;
updating sibling bindings and baselines during identity migration; reserving
`me` for the player; refusing ambiguous simultaneous source-key additions and
removals without an explicit migration or replacement choice; and correcting
case-only filesystem transactions. The remaining limits are the accepted
local-source/exact-version scope and dependencies hidden in literal MOO code,
callbacks, or unmanaged objects, as described in the README and REVIEW.

Sync integration included upstream generation-stamp and testbed portability
fixes. Package ownership uses the shared generation reader so managed children
can retain their own stamp in an inherited property. The package lifecycle test
now covers parent/child objects through independent installation, update,
interrupted removal, and reinstall. Helper version 13 requires a fresh bootstrap.

No real-world deployment was performed.
