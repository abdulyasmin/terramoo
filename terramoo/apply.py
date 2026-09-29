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
from .plan import Plan, describe, diff_object
from .refs import Refs
from .world import World


@dataclass
class Outcome:
    done: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


def _resolve_op(op: tuple, refs: Refs) -> list:
    """Resolve an op and retain its registry identity for helper-side CAS."""
    def binding(ref: Ref) -> list:
        key = ref.name
        obj = refs.resolve_ref(ref)
        nonce = refs.generations.get(key)
        if not nonce:
            raise MooError(
                f"@{key} has an unverified legacy binding; confirm it with "
                f"`tmoo adopt {obj} {key} --verify`"
            )
        return [key, obj, nonce]

    if op[0] == "link":
        return ["link", [binding(r) for r in op[1] if r.name in refs.registry]]
    if not isinstance(op[1], Ref) or op[1].kind != "@":
        raise MooError(f"{op[0]} has no managed-object key")
    key, obj, nonce = binding(op[1])
    return [op[0], key, obj, nonce, *(refs.resolve(v) for v in op[2:])]


def _send(world: World, ops: list[list], labels: list[str], outcome: Outcome, log) -> list[object | None]:
    batch, batch_labels, size = [], [], 0
    received: list[object | None] = []
    limit = world.transport.batch_bytes
    dialect = getattr(world.transport, "literal_dialect", moolit.LAMBDA)

    def flush():
        if not batch:
            return
        results = world.eval(world.helper("tmoo_apply", moolit.serialize(batch, dialect=dialect)))
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
        text = moolit.serialize(op, dialect=dialect)
        if batch and size + len(text) > limit:
            flush()
            size = 0
        batch.append(op)
        batch_labels.append(label)
        size += len(text)
    flush()
    return received


def _replan_created(world: World, plan_ops: list[tuple], created: list[str], files: dict, refs: Refs) -> list[tuple]:
    """Ops for the objects just created, diffed against what they actually
    are now: a core's `initialize` sets properties of its own (LambdaCore's
    `key = 0`, for one) that a plan made before the create cannot know."""
    from .export import export

    live = export(world, refs, created)
    kept = [op for op in plan_ops
            if op[0] == "link" or not (isinstance(op[1], Ref) and op[1].kind == "@" and op[1].name in created)]
    fresh = [op for key in created if live.get(key) is not None
             for op in diff_object(key, files[key], live[key], refs)]
    links = [op for op in kept if op[0] == "link"]
    return [op for op in kept if op[0] != "link"] + fresh + links


def run(world: World, plan: Plan, refs: Refs, *, files: dict, destroy: bool = False, log=print) -> Outcome:
    """Apply `plan`, made from `files`.  The objects it creates are re-read
    once they exist and diffed again."""
    outcome = Outcome()
    if hasattr(world, "require_helper_version"):
        world.require_helper_version()
    plan_ops = list(plan.ops)
    if plan.creates:
        log("creating:")
        create_nonces = {key: uuid4().hex for key, _, _ in plan.creates}
        ops = [
            [
                "create",
                key,
                refs.registry.get(key, Obj(-1)),
                refs.generations.get(key) or "",
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
            if (
                isinstance(returned, Obj)
                and refs.registry.get(key) == returned
                and refs.generations.get(key) == create_nonces[key]
            ):
                created.append(key)
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
        for key in failed_created:
            refs.registry.pop(key, None)
            refs.generations.pop(key, None)
        refs.pending = failed_created
        refs.reindex()
        if created:
            plan_ops = _replan_created(world, plan_ops, created, files, refs)
        # A key whose create failed now fails to resolve, so each op that
        # needs it is skipped instead of sending `@key` to the MOO.
        refs.pending = set()
    if plan_ops:
        log("applying:")
        ops, labels = [], []
        for op in plan_ops:
            try:
                ops.append(_resolve_op(op, refs))
                labels.append(describe(op))
            except (KeyError, MooError) as e:
                outcome.failed.append((describe(op), str(e)))
                log(f"  SKIP {describe(op)}: {e}")
        _send(world, ops, labels, outcome, log)
    refs.pending = set()
    if destroy and plan.destroys and outcome.failed:
        log("skipping recycling: earlier create or apply operations failed; run a clean apply to retry")
    elif destroy and plan.destroys:
        log("recycling:")
        ops, labels = [], []
        # Callbacks may have rebound orphan keys since the plan was built.
        for key, o in plan.destroys.items():
            label = f"recycle {key} ({o})"
            nonce = refs.generations.get(key)
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
