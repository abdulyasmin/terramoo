"""apply.run against a fake world that runs tmoo_apply and tmoo_export in memory."""

from types import SimpleNamespace

import pytest

from terramoo import apply, moolit, plan
from terramoo.errors import MooError
from terramoo.model import ObjectDef, PropDef
from terramoo.moolit import Obj, Ref
from terramoo.refs import Refs

ME = Obj(130)


class FakeWorld:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.registry: dict[str, Obj] = {}
        self.names: dict[Obj, tuple[str, Obj]] = {}
        self.sent: list = []
        self.batches: list[list] = []
        self.transport = SimpleNamespace(batch_bytes=24_000)
        self.ignore_props: set[str] = set()

    def helper(self, verb, arg):
        return verb, arg

    def eval(self, call):
        verb, text = call
        value = moolit.parse(text, dialect=getattr(self.transport, "literal_dialect", moolit.LAMBDA))

        def no_refs(v):
            if isinstance(v, Ref):  # the MOO has no @key syntax
                raise MooError("the expression did not compile")
            return v

        moolit.walk(value, no_refs)
        if verb == "tmoo_export":
            return [[o, *self.names[o], Obj(-1), ME, "", [], []] for o in value]
        self.batches.append(value)
        return [self._op(op) for op in value]

    def _op(self, op):
        self.sent.append(op)
        if op[0] == "destroy":
            self.registry.pop(op[1], None)
            return [1, 1]
        if op[0] != "create":
            return [1, 1]
        key, parent, name = op[1:]
        if key in self.fail:
            return [0, "E_QUOTA", "no quota"]
        o = Obj(200 + len(self.registry))
        self.registry[key] = o
        self.names[o] = (name, self.registry.get(parent, parent))
        return [1, o]

    def read_registry(self):
        return dict(self.registry)

    def save_state(self, registry):
        self.saved = dict(registry)


FILES = {
    "hall": ObjectDef(key="hall", name="Hall", parent=Ref("$", "room"),
                      props=[PropDef("guard", Ref("@", "guard"))]),
    "guard": ObjectDef(key="guard", name="Guard", parent=Ref("$", "thing"), location=Ref("@", "hall")),
}


def run(world):
    refs = Refs(player=ME, sysrefs={"room": Obj(3), "thing": Obj(5)})
    p = plan.build(FILES, {}, refs)
    return apply.run(world, p, refs, files=FILES, log=lambda _: None)


def test_creates_then_applies_with_new_numbers():
    w = FakeWorld()
    outcome = run(w)
    assert not outcome.failed
    hall, guard = w.registry["hall"], w.registry["guard"]
    assert ["addprop", hall, "guard", guard, [ME, "rc"]] in w.sent
    assert ["move", guard, hall] in w.sent
    assert w.sent[-1] == ["link", [hall, guard]]
    assert w.saved == w.registry


def test_a_failed_create_skips_only_the_ops_that_need_it():
    w = FakeWorld(fail={"guard"})
    outcome = run(w)
    failed = {label for label, _ in outcome.failed}
    assert failed == {"create guard (Guard)", "addprop @hall.guard", "move @guard @hall"}
    assert w.sent[-1] == ["link", [w.registry["hall"]]]
    assert w.saved == {"hall": w.registry["hall"]}


def test_orphans_are_only_recycled_when_destroy_is_explicit():
    old = Obj(201)
    p = plan.Plan(destroys=["old"])

    kept = FakeWorld()
    kept.registry = {"old": old}
    kept_refs = Refs(player=ME, registry={"old": old})
    apply.run(kept, p, kept_refs, files={}, log=lambda _: None)
    assert kept.sent == []
    assert kept.saved == {"old": old}

    destroyed = FakeWorld()
    destroyed.registry = {"old": old}
    destroyed_refs = Refs(player=ME, registry={"old": old})
    outcome = apply.run(destroyed, p, destroyed_refs, files={}, destroy=True, log=lambda _: None)
    assert outcome.failed == []
    assert destroyed.sent == [["destroy", "old", old]]
    assert destroyed_refs.registry == {}
    assert destroyed.saved == {}


def test_failed_recycle_is_not_unregistered_while_other_destroys_continue():
    blocked = Obj(201)
    removable = Obj(202)

    class PartlyFailedRecycleWorld(FakeWorld):
        def _op(self, op):
            if op == ["destroy", "blocked", blocked]:
                self.sent.append(op)
                return [0, "E_PERM", "recycle refused"]
            return super()._op(op)

    w = PartlyFailedRecycleWorld()
    w.registry = {"blocked": blocked, "removable": removable}
    refs = Refs(player=ME, registry=dict(w.registry))

    outcome = apply.run(
        w,
        plan.Plan(destroys=["blocked", "removable"]),
        refs,
        files={},
        destroy=True,
        log=lambda _: None,
    )

    assert w.sent == [
        ["destroy", "blocked", blocked],
        ["destroy", "removable", removable],
    ]
    assert outcome.failed == [("recycle blocked (#201)", "E_PERM: recycle refused")]
    assert refs.registry == {"blocked": blocked}
    assert w.saved == {"blocked": blocked}


def test_destroy_recovers_an_already_recycled_orphan_in_one_helper_call():
    old = Obj(201)

    class GoneWorld(FakeWorld):
        def _op(self, op):
            self.sent.append(op)
            if op == ["recycle", old]:
                return [0, "E_INVARG", "invalid object"]
            if op == ["destroy", "old", old]:
                self.registry.pop("old", None)
                return [1, 1]
            pytest.fail(f"unexpected operation: {op}")

    w = GoneWorld()
    w.registry = {"old": old}
    refs = Refs(player=ME, registry=dict(w.registry))
    for _ in range(2):
        pending = plan.build({}, {}, refs)
        outcome = apply.run(w, pending, refs, files={}, destroy=True, log=lambda _: None)
        assert outcome.failed == []
        assert refs.registry == w.saved == {}
    assert w.batches == [[["destroy", "old", old]]]


def test_ops_are_split_before_the_configured_batch_limit():
    hall = Obj(200)
    w = FakeWorld()
    w.registry = {"hall": hall}
    refs = Refs(player=ME, registry={"hall": hall})
    ops = [("name", Ref("@", "hall"), "A"), ("name", Ref("@", "hall"), "B")]
    w.transport.batch_bytes = len(moolit.serialize(["name", hall, "A"]))

    outcome = apply.run(w, plan.Plan(ops=ops), refs, files={}, log=lambda _: None)
    assert outcome.failed == []
    assert w.batches == [[["name", hall, "A"]], [["name", hall, "B"]]]


def test_apply_encodes_strings_for_the_moor_dialect():
    hall = Obj(200)
    w = FakeWorld()
    w.registry = {"hall": hall}
    w.transport.literal_dialect = moolit.MOOR
    refs = Refs(player=ME, registry={"hall": hall})

    outcome = apply.run(
        w,
        plan.Plan(ops=[("name", Ref("@", "hall"), "Hé\n")]),
        refs,
        files={},
        log=lambda _: None,
    )

    assert outcome.failed == []
    assert w.sent == [["name", hall, "Hé\n"]]


def test_a_short_result_list_is_reported_and_state_is_still_saved():
    class ShortReplyWorld(FakeWorld):
        def eval(self, call):
            verb, text = call
            if verb == "tmoo_apply":
                value = moolit.parse(text)
                self.batches.append(value)
                self.sent.extend(value)
                return [[1, 1]]
            return super().eval(call)

    hall = Obj(200)
    w = ShortReplyWorld()
    w.registry = {"hall": hall}
    refs = Refs(player=ME, registry={"hall": hall})
    p = plan.Plan(ops=[("name", Ref("@", "hall"), "A"), ("flags", Ref("@", "hall"), "r")])

    outcome = apply.run(w, p, refs, files={}, log=lambda _: None)
    assert outcome.done == ["name @hall A"]
    assert outcome.failed == [("batch", "2 ops sent, 1 results")]
    assert w.saved == {"hall": hall}


@pytest.mark.parametrize(
    "reply, expected",
    [
        ([[0, "E_PERM"]], "malformed helper result: [0, 'E_PERM']"),
        ([[1]], "malformed helper result: [1]"),
        (7, "malformed helper result list: 7"),
    ],
)
def test_a_malformed_helper_result_is_failed_and_state_is_still_saved(reply, expected):
    class MalformedReplyWorld(FakeWorld):
        def eval(self, call):
            verb, text = call
            if verb == "tmoo_apply":
                value = moolit.parse(text)
                self.batches.append(value)
                self.sent.extend(value)
                return reply
            return super().eval(call)

    hall = Obj(200)
    w = MalformedReplyWorld()
    w.registry = {"hall": hall}
    refs = Refs(player=ME, registry={"hall": hall})
    p = plan.Plan(ops=[("name", Ref("@", "hall"), "A")])

    outcome = apply.run(w, p, refs, files={}, log=lambda _: None)

    assert outcome.failed == [("name @hall A", expected)]
    assert w.saved == {"hall": hall}
