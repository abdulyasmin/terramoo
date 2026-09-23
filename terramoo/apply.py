"""Running a plan through the toolbox's `tmoo_apply` verb.

Two phases: the creates go first (one call, parents before children), the
registry is re-read so the new numbers are known, then every other op is
resolved to numbers and sent in batches.  Each batch is one eval whose
result is one entry per op; a failed op is reported and the rest carry on,
since each op is independent once its object exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import moolit
from .moolit import Obj, Ref, walk
from .plan import Plan, describe
from .refs import Refs
from .world import World


@dataclass
class Outcome:
    done: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def _resolve_op(op: tuple, refs: Refs) -> list:
    def leaf(v):
        if isinstance(v, Ref):
            return refs.resolve_ref(v)
        return v

    kind = op[0]
    if kind == "create":
        key, parent, name = op[1], op[2], op[3]
        # parent may be a str: a key made earlier in this batch
        return ["create", key, parent if isinstance(parent, str) else refs.resolve_ref(parent), name]
    if kind == "link":
        return ["link", [refs.resolve_ref(r) for r in op[1] if r.name in refs.registry]]
    return [kind, *[walk(v, leaf) if not isinstance(v, tuple) else list(v) for v in op[1:]]]


def _send(world: World, ops: list[list], labels: list[str], outcome: Outcome, log) -> None:
    batch, batch_labels, size = [], [], 0
    limit = world.transport.batch_bytes

    def flush():
        if not batch:
            return
        results = world.eval(world.helper("tmoo_apply", moolit.serialize(batch)))
        for label, res in zip(batch_labels, results):
            if res[0] == 1:
                outcome.done.append(label)
                log(f"  ok   {label}")
            else:
                outcome.failed.append((label, f"{res[1]}: {res[2]}"))
                log(f"  FAIL {label}: {res[1]}: {res[2]}")
        if len(results) != len(batch):
            outcome.failed.append(("batch", f"{len(batch)} ops sent, {len(results)} results"))
        batch.clear()
        batch_labels.clear()

    for op, label in zip(ops, labels):
        text = moolit.serialize(op)
        if batch and size + len(text) > limit:
            flush()
            size = 0
        batch.append(op)
        batch_labels.append(label)
        size += len(text)
    flush()


def run(world: World, plan: Plan, refs: Refs, *, destroy: bool = False, log=print) -> Outcome:
    outcome = Outcome()
    if plan.creates:
        log("creating:")
        ops = [_resolve_op(("create", *c), refs) for c in plan.creates]
        labels = [f"create {k} ({n})" for k, _, n in plan.creates]
        _send(world, ops, labels, outcome, log)
        refs.registry = world.read_registry()
        refs.pending = {k for k, _, _ in plan.creates if k not in refs.registry}
        refs.reindex()
    if plan.ops:
        log("applying:")
        ops, labels = [], []
        for op in plan.ops:
            try:
                ops.append(_resolve_op(op, refs))
                labels.append(describe(op))
            except KeyError as e:
                outcome.failed.append((describe(op), str(e)))
                log(f"  SKIP {describe(op)}: {e}")
        _send(world, ops, labels, outcome, log)
    if destroy and plan.destroys:
        log("recycling:")
        ops, labels = [], []
        for key in plan.destroys:
            o = refs.registry[key]
            ops.append(["recycle", o])
            labels.append(f"recycle {key} ({o})")
            ops.append(["unregister", key])
            labels.append(f"unregister {key}")
        _send(world, ops, labels, outcome, log)
        refs.registry = world.read_registry()
        refs.reindex()
    world.save_state(refs.registry)
    return outcome
