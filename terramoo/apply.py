"""Running a plan through the toolbox's `tmoo_apply` verb.

Two phases: the creates go first (one call, parents before children), the
registry is re-read so the new numbers are known, then every other op is
resolved to numbers and sent in batches.  Each batch is one eval whose
result is one entry per op; a failed op is reported and the rest carry on,
since each op is independent once its object exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from . import moolit
from .errors import MooError
from .moolit import Obj, Ref
from .plan import Plan, VerbTarget, _exit_classes, describe, diff_object
from .refs import Refs
from .world import World


@dataclass
class Outcome:
    done: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


def _resolve_op(op: tuple, refs: Refs) -> list:
    """Resolve an op and retain its registry identity for helper-side CAS."""
    def binding(ref: Ref) -> list:
        key = refs.registry_key(ref.name) or ref.name
        obj = refs.resolve_ref(ref)
        nonce = refs.generation_for(key)
        if not nonce:
            raise MooError(
                f"@{key} has an unverified legacy binding; confirm it with "
                f"`tmoo adopt {obj} {key} --verify`"
            )
        return [key, obj, nonce]

    if op[0] == "link":
        return ["link", [binding(r) for r in op[1] if refs.registry_key(r.name) is not None]]
    if not isinstance(op[1], Ref) or op[1].kind != "@":
        raise MooError(f"{op[0]} has no managed-object key")
    key, obj, nonce = binding(op[1])
    return [
        op[0], key, obj, nonce,
        *(refs.resolve(v.index if isinstance(v, VerbTarget) else v) for v in op[2:]),
    ]


def _send(world: World, ops: list[list], labels: list[str], outcome: Outcome, log) -> list[object | None]:
    batch, batch_labels, size = [], [], 0
    received: list[object | None] = []
    limit = world.transport.batch_bytes
    dialect = getattr(world.transport, "literal_dialect", moolit.LAMBDA)

    def flush():
        if not batch:
            return
        session = getattr(world, "_package_session", None)
        outgoing = session.before_batch(batch) if session else batch
        results = world.eval(world.helper("tmoo_apply", moolit.serialize(outgoing, dialect=dialect)))
        if session:
            session.after_batch(batch, results)
        if not isinstance(results, list):
            why = f"malformed helper result list: {results!r}"
            for label in batch_labels:
                outcome.failed.append((label, why))
                log(f"  FAIL {label}: {why}")
                received.append(None)
            batch.clear()
            batch_labels.clear()
            return
        for label, res in zip(batch_labels, results):
            received.append(res)
            if isinstance(res, list) and len(res) >= 2 and res[0] == 1:
                outcome.done.append(label)
                log(f"  ok   {label}")
            elif isinstance(res, list) and len(res) >= 3 and res[0] == 0:
                outcome.failed.append((label, f"{res[1]}: {res[2]}"))
                log(f"  FAIL {label}: {res[1]}: {res[2]}")
            else:
                why = f"malformed helper result: {res!r}"
                outcome.failed.append((label, why))
                log(f"  FAIL {label}: {why}")
        if len(results) != len(batch):
            outcome.failed.append(("batch", f"{len(batch)} ops sent, {len(results)} results"))
            received.extend([None] * max(0, len(batch) - len(results)))
        batch.clear()
        batch_labels.clear()

    for op, label in zip(ops, labels):
        session = getattr(world, "_package_session", None)
        text = moolit.serialize(["owned", session.epoch, op] if session else op, dialect=dialect)
        length = len(text.encode()) + 2
        if batch and size + length > limit:
            flush()
            size = 0
        batch.append(op)
        batch_labels.append(label)
        size += length
    flush()
    return received


def _depends_on_unlink(unlink: tuple, following: tuple) -> bool:
    if unlink[0] != "unlink" or following[0] not in ("setprop", "rmprop", "clearprop"):
        return False
    if unlink[1] != following[1]:
        return False
    expected = "source" if unlink[2] == "exit" else "dest" if unlink[2] == "entrance" else None
    return expected is not None and str(following[2]).lower() == expected


def _apply_ops(world: World, plan_ops: list[tuple], refs: Refs, outcome: Outcome, log) -> None:
    """Resolve and batch normal ops, but gate endpoint changes on unlink.

    A failed unlink followed by a successful `setprop source`/`dest` would
    strand the exit in its old room, and the ignored membership properties
    could not reveal that drift on the next plan.
    """
    batch: list[list] = []
    labels: list[str] = []

    def flush() -> None:
        if batch:
            _send(world, batch, labels, outcome, log)
            batch.clear()
            labels.clear()

    i = 0
    while i < len(plan_ops):
        op = plan_ops[i]
        label = describe(op)
        if op[0] != "unlink":
            try:
                batch.append(_resolve_op(op, refs))
                labels.append(label)
            except (KeyError, MooError) as e:
                outcome.failed.append((label, str(e)))
                log(f"  SKIP {label}: {e}")
            i += 1
            continue

        flush()
        succeeded = False
        try:
            result = _send(world, [_resolve_op(op, refs)], [label], outcome, log)
            succeeded = bool(
                result
                and isinstance(result[0], list)
                and len(result[0]) >= 2
                and result[0][0] == 1
            )
        except (KeyError, MooError) as e:
            outcome.failed.append((label, str(e)))
            log(f"  SKIP {label}: {e}")

        if i + 1 < len(plan_ops) and _depends_on_unlink(op, plan_ops[i + 1]):
            if not succeeded:
                dependent = describe(plan_ops[i + 1])
                why = f"prerequisite {label} failed"
                outcome.failed.append((dependent, why))
                log(f"  SKIP {dependent}: {why}")
                i += 1
        i += 1
    flush()


def _replan_created(world: World, plan_ops: list[tuple], created: list[str], files: dict, refs: Refs,
                    *, full_context=False) -> list[tuple]:
    """Ops for the objects just created, diffed against what they actually
    are now: a core's `initialize` sets properties of its own (LambdaCore's
    `key = 0`, for one) that a plan made before the create cannot know."""
    from .export import export

    context = [k for key in files if (k := refs.registry_key(key)) is not None] if full_context else created
    live = export(world, refs, context)
    files_by_name = {key.lower(): value for key, value in files.items()}
    created_folded = {key.lower() for key in created}
    kept = [op for op in plan_ops
            if op[0] == "link" or not (
                isinstance(op[1], Ref) and op[1].kind == "@" and op[1].name.lower() in created_folded
            )]
    wanted_exits = _exit_classes(files, refs, live=False)
    current_exits = _exit_classes(
        {key: obj for key, obj in live.items() if obj is not None}, refs, live=True
    )
    fresh = [
        op
        for key in created if live.get(key) is not None
        for op in diff_object(
            key,
            files_by_name[key.lower()],
            live[key],
            refs,
            was_exit=current_exits.get(key.lower(), wanted_exits.get(key.lower(), False)),
            will_exit=wanted_exits.get(key.lower(), False),
        )
    ]
    links = [op for op in kept if op[0] == "link"]
    return [op for op in kept if op[0] != "link"] + fresh + links


def run(world: World, plan: Plan, refs: Refs, *, files: dict, destroy: bool = False,
        stop_on_create_failure: bool = False, replan_keys: set[str] | None = None, log=print) -> Outcome:
    """Apply `plan`, made from `files`.  The objects it creates are re-read
    once they exist and diffed again."""
    outcome = Outcome()
    if hasattr(world, "require_helper_version"):
        world.require_helper_version()
    plan_ops = list(plan.ops)
    if plan.creates:
        log("creating:")
        session = getattr(world, "_package_session", None)
        if session and any(key.lower() not in session.nonces for key, _, _ in plan.creates):
            raise MooError("live changes require additional creates; replan before applying")
        create_nonces = {key: session.nonces[key.lower()] if session else uuid4().hex for key, _, _ in plan.creates}
        ops = [
            [
                "create",
                key,
                refs.registry[refs.registry_key(key)] if refs.registry_key(key) is not None else Obj(-1),
                refs.generation_for(key) or "",
                refs.resolve(parent),
                name,
                create_nonces[key],
            ]
            for key, parent, name in plan.creates
        ]
        labels = [f"create {k} ({n})" for k, _, n in plan.creates]
        results = _send(world, ops, labels, outcome, log)
        refs.replace_registry(world.read_registry())
        created = []
        for (key, _, _), label, result in zip(plan.creates, labels, results):
            returned = result[1] if isinstance(result, list) and len(result) >= 2 and result[0] == 1 else None
            actual_key = refs.registry_key(key)
            if (
                isinstance(returned, Obj)
                and actual_key is not None
                and refs.registry[actual_key] == returned
                and refs.generation_for(actual_key) == create_nonces[key]
            ):
                created.append(actual_key)
                continue
            if returned is not None:
                why = f"create returned {returned}, but the verified registry binding is different"
                try:
                    outcome.done.remove(label)
                except ValueError:
                    pass
                outcome.failed.append((label, why))
                log(f"  FAIL {label}: {why}")
        failed_created = {key for key, _, _ in plan.creates} - set(created)
        if failed_created and stop_on_create_failure:
            refs.pending = set()
            world.save_state(world.read_registry())
            return outcome
        for key in failed_created:
            actual_key = refs.registry_key(key)
            if actual_key is not None:
                refs.registry.pop(actual_key, None)
                refs.generations.pop(actual_key, None)
        refs.pending = failed_created
        refs.reindex()
        if created:
            recheck = created if replan_keys is None else [
                key for name in replan_keys if (key := refs.registry_key(name)) is not None
            ]
            plan_ops = _replan_created(world, plan_ops, recheck, files, refs, full_context=replan_keys is not None)
        # A key whose create failed now fails to resolve, so each op that
        # needs it is skipped instead of sending `@key` to the MOO.
        refs.pending = set()
    if plan_ops:
        log("applying:")
        _apply_ops(world, plan_ops, refs, outcome, log)
    refs.pending = set()
    if destroy and plan.destroys and outcome.failed:
        log("skipping recycling: earlier create or apply operations failed; run a clean apply to retry")
    elif destroy and plan.destroys:
        log("recycling:")
        ops, labels = [], []
        # Callbacks may have rebound orphan keys since the plan was built.
        for key, o in plan.destroys.items():
            label = f"recycle {key} ({o})"
            nonce = refs.generation_for(key)
            if not nonce:
                why = (
                    f"@{key} has an unverified legacy binding; confirm it with "
                    f"`tmoo adopt {o} {key} --verify`"
                )
                outcome.failed.append((label, why))
                log(f"  SKIP {label}: {why}")
                continue
            ops.append(["destroy", key, o, nonce])
            labels.append(label)
        _send(world, ops, labels, outcome, log)
    refs.replace_registry(world.read_registry())
    world.save_state(refs.snapshot())
    return outcome
