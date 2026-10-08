"""apply.run against a fake world that runs tmoo_apply and tmoo_export in memory."""

from types import SimpleNamespace

import pytest

from terramoo import apply, moolit, plan
from terramoo.errors import MooError
from terramoo.model import ObjectDef, PropDef
from terramoo.moolit import Obj, Ref
from terramoo.refs import Refs, Registry

ME = Obj(130)


def verify_bindings(world, registry):
    world.registry = dict(registry)
    world.generations = {key: f"generation-{key}" for key in registry}
    return Refs(player=ME, registry=Registry(registry, world.generations))


class FakeWorld:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.registry: dict[str, Obj] = {}
        self.generations: dict[str, str] = {}
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
            return [[binding[1], *self.names[binding[1]], Obj(-1), ME, "", [], []] for binding in value]
        self.batches.append(value)
        return [self._op(op) for op in value]

    def _op(self, op):
        self.sent.append(op)
        if op[0] == "destroy":
            self.registry.pop(op[1], None)
            self.generations.pop(op[1], None)
            return [1, 1]
        if op[0] != "create":
            return [1, 1]
        key, _, _, parent, name, nonce = op[1:]
        if key in self.fail:
            return [0, "E_QUOTA", "no quota"]
        o = Obj(200 + len(self.registry))
        self.registry[key] = o
        self.generations[key] = nonce
        self.names[o] = (name, self.registry.get(parent, parent))
        return [1, o]

    def read_registry(self):
        return Registry(self.registry, self.generations)

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
    assert ["addprop", "hall", hall, w.generations["hall"], "guard", guard, [ME, "rc"]] in w.sent
    assert ["move", "guard", guard, w.generations["guard"], hall] in w.sent
    assert w.sent[-1] == [
        "link",
        [["hall", hall, w.generations["hall"]], ["guard", guard, w.generations["guard"]]],
    ]
    assert w.saved == w.registry


def test_a_failed_create_skips_only_the_ops_that_need_it():
    w = FakeWorld(fail={"guard"})
    outcome = run(w)
    failed = {label for label, _ in outcome.failed}
    assert failed == {"create guard (Guard)", "addprop @hall.guard", "move @guard @hall"}
    assert w.sent[-1] == ["link", [["hall", w.registry["hall"], w.generations["hall"]]]]
    assert w.saved == {"hall": w.registry["hall"]}


def test_failed_create_does_not_adopt_an_unrelated_post_create_binding():
    unrelated = Obj(40)

    class FailedCreateWorld(FakeWorld):
        def _op(self, op):
            if op[0] == "create":
                self.sent.append(op)
                self.registry[op[1]] = unrelated
                self.names[unrelated] = ("Unrelated", Obj(5))
                return [0, "E_QUOTA", "no quota"]
            return super()._op(op)

    w = FailedCreateWorld()
    refs = Refs(player=ME, sysrefs={"thing": Obj(5)})
    files = {"child": ObjectDef(key="child", name="Child", parent=Ref("$", "thing"))}
    pending = plan.build(files, {}, refs)

    outcome = apply.run(w, pending, refs, files=files, log=lambda _: None)

    assert outcome.failed == [("create child (Child)", "E_QUOTA: no quota")]
    assert [op[0] for op in w.sent] == ["create", "link"]
    assert w.sent[-1] == ["link", []]


def test_mutation_is_rejected_when_the_key_was_rebound_after_planning():
    planned, rebound = Obj(10), Obj(11)
    mutated = []

    class ReboundWorld(FakeWorld):
        def _op(self, op):
            self.sent.append(op)
            if len(op) >= 4 and op[0] == "name" and isinstance(op[1], str):
                _, key, expected, nonce, *_ = op
                if self.registry.get(key) != expected or self.generations.get(key) != nonce:
                    return [0, "E_INVARG", f"key {key} was rebound"]
                mutated.append(expected)
                return [1, 1]
            if op[0] == "name":
                mutated.append(op[1])
                return [1, 1]
            return super()._op(op)

    w = ReboundWorld()
    w.registry = {"hall": rebound}
    w.generations = {"hall": "new-generation"}
    refs = Refs(player=ME, registry={"hall": planned})
    refs.generations = {"hall": "planned-generation"}

    outcome = apply.run(
        w,
        plan.Plan(ops=[("name", Ref("@", "hall"), "New Hall")]),
        refs,
        files={},
        log=lambda _: None,
    )

    assert mutated == []
    assert outcome.failed == [("name @hall New Hall", "E_INVARG: key hall was rebound")]


def test_exit_reconciliation_failures_from_the_fake_helper_are_reported():
    hall, old_source, new_source = Obj(10), Obj(11), Obj(12)

    class FailingExitWorld(FakeWorld):
        def _op(self, op):
            self.sent.append(op)
            if op[0] == "endpoint":
                return [0, "E_PERM", "remove_exit refused"]
            if op[0] == "link":
                return [0, "E_INVARG", "exit absent after add_exit"]
            return [1, 1]

    w = FailingExitWorld()
    refs = verify_bindings(w, {"hall": hall, "old_source": old_source, "new_source": new_source})
    want = ObjectDef(
        key="hall", name="Hall", parent=Obj(3),
        props=[PropDef("source", Ref("@", "new_source"), defined=False)],
    )
    have = ObjectDef(
        key="hall", name="Hall", parent=Obj(3),
        props=[PropDef("source", Ref("@", "old_source"), defined=False)],
    )
    ops = plan.diff_object("hall", want, have, refs)
    ops.append(("link", [Ref("@", "hall")]))

    outcome = apply.run(w, plan.Plan(ops=ops), refs, files={"hall": want}, log=lambda _: None)

    assert [op[0] for op in w.sent] == ["endpoint", "link"]
    assert outcome.failed == [
        ("endpoint @hall.source #11 -> #12", "E_PERM: remove_exit refused"),
        ("link exits among 1 objects", "E_INVARG: exit absent after add_exit"),
    ]


def test_successful_exit_unlink_allows_the_endpoint_mutation():
    hall, old_dest, new_dest = Obj(10), Obj(11), Obj(12)
    w = FakeWorld()
    refs = verify_bindings(w, {"hall": hall, "old_dest": old_dest, "new_dest": new_dest})
    want = ObjectDef(
        key="hall", name="Hall", parent=Obj(3),
        props=[PropDef("dest", Ref("@", "new_dest"), defined=False)],
    )
    have = ObjectDef(
        key="hall", name="Hall", parent=Obj(3),
        props=[PropDef("dest", Ref("@", "old_dest"), defined=False)],
    )

    outcome = apply.run(
        w,
        plan.Plan(ops=plan.diff_object("hall", want, have, refs)),
        refs,
        files={"hall": want},
        log=lambda _: None,
    )

    assert outcome.failed == []
    assert [op[0] for op in w.sent] == ["endpoint"]


def test_endpoint_race_fails_without_changing_or_relinking_the_newer_room():
    exit_obj, planned_old, raced_old, desired = Obj(10), Obj(11), Obj(12), Obj(13)

    class RacingEndpointWorld(FakeWorld):
        def __init__(self):
            super().__init__()
            self.endpoints = {exit_obj: raced_old}
            self.members = {planned_old: set(), raced_old: {exit_obj}, desired: set()}

        def _op(self, op):
            self.sent.append(op)
            if op[0] != "endpoint":
                return [1, 1]
            _, _, target, _, prop, expected, new, *_ = op
            assert prop == "source"
            if self.endpoints[target] != expected:
                return [0, "E_INVARG", "source changed before endpoint update; replan"]
            self.members[expected].discard(target)
            self.endpoints[target] = new
            self.members[new].add(target)
            return [1, 1]

    w = RacingEndpointWorld()
    refs = verify_bindings(
        w,
        {"door": exit_obj, "planned_old": planned_old, "raced_old": raced_old, "desired": desired},
    )
    want = ObjectDef(
        key="door", name="Door", parent=Obj(3),
        props=[PropDef("source", Ref("@", "desired"), defined=False)],
    )
    have = ObjectDef(
        key="door", name="Door", parent=Obj(3),
        props=[PropDef("source", Ref("@", "planned_old"), defined=False)],
    )

    outcome = apply.run(
        w,
        plan.Plan(ops=plan.diff_object("door", want, have, refs)),
        refs,
        files={"door": want},
        log=lambda _: None,
    )

    assert [op[0] for op in w.sent] == ["endpoint"]
    assert outcome.failed == [
        ("endpoint @door.source #11 -> #13", "E_INVARG: source changed before endpoint update; replan"),
    ]
    assert w.endpoints[exit_obj] == raced_old
    assert w.members == {planned_old: set(), raced_old: {exit_obj}, desired: set()}


def test_exit_reparented_to_non_exit_drops_its_old_room_membership():
    door, old_source, new_source = Obj(10), Obj(11), Obj(12)

    class ReparentingWorld(FakeWorld):
        def __init__(self):
            super().__init__()
            self.is_exit = True
            self.endpoint = old_source
            self.members = {old_source: {door}, new_source: set()}

        def _op(self, op):
            self.sent.append(op)
            if op[0] == "chparent":
                self.is_exit = False
            elif op[0] == "endpoint":
                _, _, target, _, _, expected, new, *classifications = op
                assert target == door and self.endpoint == expected
                was_exit, will_exit = classifications or (self.is_exit, self.is_exit)
                if was_exit:
                    self.members[expected].discard(target)
                self.endpoint = new
                if will_exit:
                    self.members[new].add(target)
            return [1, 1]

    w = ReparentingWorld()
    refs = verify_bindings(w, {"door": door, "old_source": old_source, "new_source": new_source})
    refs.sysrefs = {"exit": Obj(7), "thing": Obj(5)}
    refs.reindex()
    want = ObjectDef(
        key="door", name="Door", parent=Ref("$", "thing"),
        props=[PropDef("source", Ref("@", "new_source"), defined=False)],
    )
    have = ObjectDef(
        key="door", name="Door", parent=Ref("$", "exit"),
        props=[PropDef("source", Ref("@", "old_source"), defined=False)],
    )
    old_room = ObjectDef(key="old_source", name="Old", parent=Ref("$", "thing"))
    new_room = ObjectDef(key="new_source", name="New", parent=Ref("$", "thing"))
    files = {"door": want, "old_source": old_room, "new_source": new_room}
    live = {"door": have, "old_source": old_room, "new_source": new_room}
    pending = plan.build(files, live, refs)

    outcome = apply.run(w, pending, refs, files=files, log=lambda _: None)

    assert outcome.failed == []
    assert [op[0] for op in w.sent] == ["chparent", "endpoint", "link"]
    assert w.endpoint == new_source
    assert w.members == {old_source: set(), new_source: set()}


def test_orphans_are_only_recycled_when_destroy_is_explicit():
    old = Obj(201)
    p = plan.Plan(destroys={"old": old})

    kept = FakeWorld()
    kept_refs = verify_bindings(kept, {"old": old})
    apply.run(kept, p, kept_refs, files={}, log=lambda _: None)
    assert kept.sent == []
    assert kept.saved == {"old": old}

    destroyed = FakeWorld()
    destroyed_refs = verify_bindings(destroyed, {"old": old})
    outcome = apply.run(destroyed, p, destroyed_refs, files={}, destroy=True, log=lambda _: None)
    assert outcome.failed == []
    assert destroyed.sent == [["destroy", "old", old, "generation-old"]]
    assert destroyed_refs.registry == {}
    assert destroyed.saved == {}


def test_destroy_phase_is_skipped_after_a_create_failure():
    old = Obj(201)
    w = FakeWorld(fail={"child"})
    w.registry = {"old": old}
    refs = Refs(player=ME, registry={"old": old}, sysrefs={"thing": Obj(5)})
    files = {"child": ObjectDef(key="child", name="Child", parent=Ref("$", "thing"))}
    pending = plan.build(files, {}, refs)
    messages = []

    outcome = apply.run(w, pending, refs, files=files, destroy=True, log=messages.append)

    assert [op[0] for op in w.sent] == ["create", "link"]
    assert not any(op[0] == "destroy" for op in w.sent)
    assert w.registry == {"old": old}
    assert outcome.failed == [("create child (Child)", "E_QUOTA: no quota")]
    assert any("skipping recycling" in message for message in messages)


def test_destroy_refuses_a_reused_object_number_with_the_wrong_generation():
    reused = Obj(201)

    class ReusedNumberWorld(FakeWorld):
        def _op(self, op):
            self.sent.append(op)
            if len(op) == 4 and op[0] == "destroy":
                _, key, expected, nonce = op
                if (
                    self.registry.get(key) != expected
                    or self.generations.get(key) != nonce
                    or self.object_generations.get(expected) != nonce
                ):
                    return [0, "E_INVARG", f"generation mismatch for {key}"]
            if op[0] == "destroy":
                self.registry.pop(op[1], None)
                return [1, 1]
            return super()._op(op)

    w = ReusedNumberWorld()
    w.registry = {"old": reused}
    w.generations = {"old": "old-generation"}
    w.object_generations = {reused: "new-generation"}
    refs = Refs(player=ME, registry={"old": reused})
    refs.generations = {"old": "old-generation"}

    outcome = apply.run(
        w,
        plan.Plan(destroys={"old": reused}),
        refs,
        files={},
        destroy=True,
        log=lambda _: None,
    )

    assert w.registry == {"old": reused}
    assert outcome.failed == [("recycle old (#201)", "E_INVARG: generation mismatch for old")]


def test_failed_recycle_is_not_unregistered_while_other_destroys_continue():
    blocked = Obj(201)
    removable = Obj(202)

    class PartlyFailedRecycleWorld(FakeWorld):
        def _op(self, op):
            if op == ["destroy", "blocked", blocked, "generation-blocked"]:
                self.sent.append(op)
                return [0, "E_PERM", "recycle refused"]
            return super()._op(op)

    w = PartlyFailedRecycleWorld()
    refs = verify_bindings(w, {"blocked": blocked, "removable": removable})

    outcome = apply.run(
        w,
        plan.Plan(destroys={"blocked": blocked, "removable": removable}),
        refs,
        files={},
        destroy=True,
        log=lambda _: None,
    )

    assert w.sent == [
        ["destroy", "blocked", blocked, "generation-blocked"],
        ["destroy", "removable", removable, "generation-removable"],
    ]
    assert outcome.failed == [("recycle blocked (#201)", "E_PERM: recycle refused")]
    assert refs.registry == {"blocked": blocked}
    assert w.saved == {"blocked": blocked}


def test_destroy_is_skipped_when_initialize_rebinds_an_orphan_and_create_fails():
    old, child = Obj(100), Obj(200)
    recycled = []

    class RebindingWorld(FakeWorld):
        def _op(self, op):
            if op[0] == "create":
                self.sent.append(op)
                # initialize unregisters the orphan and registers the new object.
                self.registry.pop("auto_child")
                self.registry["auto_child"] = child
                return [0, "E_INVARG", "object #200 is already registered as auto_child; cannot bind key child"]
            return super()._op(op)

        def read_registry(self):
            return Registry(self.registry, self.generations)

    w = RebindingWorld()
    refs = verify_bindings(w, {"auto_child": old})
    files = {"child": ObjectDef(key="child", name="Child", parent=Obj(5))}
    pending = plan.build(files, {}, refs)
    assert not pending.problems

    outcome = apply.run(w, pending, refs, files=files, destroy=True, log=lambda _: None)

    assert recycled == []
    assert not any(op[0] == "destroy" for op in w.sent)
    assert outcome.failed == [
        ("create child (Child)", "E_INVARG: object #200 is already registered as auto_child; cannot bind key child"),
    ]
    assert refs.registry == w.registry == w.saved == {"auto_child": child}


def test_destroy_recovers_an_already_recycled_orphan_in_one_helper_call():
    old = Obj(201)

    class GoneWorld(FakeWorld):
        def _op(self, op):
            self.sent.append(op)
            if op == ["recycle", old]:
                return [0, "E_INVARG", "invalid object"]
            if op == ["destroy", "old", old, "generation-old"]:
                self.registry.pop("old", None)
                return [1, 1]
            pytest.fail(f"unexpected operation: {op}")

    w = GoneWorld()
    refs = verify_bindings(w, {"old": old})
    for _ in range(2):
        pending = plan.build({}, {}, refs)
        outcome = apply.run(w, pending, refs, files={}, destroy=True, log=lambda _: None)
        assert outcome.failed == []
        assert refs.registry == w.saved == {}
    assert w.batches == [[["destroy", "old", old, "generation-old"]]]


def test_ops_are_split_before_the_configured_batch_limit():
    hall = Obj(200)
    w = FakeWorld()
    refs = verify_bindings(w, {"hall": hall})
    ops = [("name", Ref("@", "hall"), "A"), ("name", Ref("@", "hall"), "B")]
    first = ["name", "hall", hall, "generation-hall", "A"]
    second = ["name", "hall", hall, "generation-hall", "B"]
    w.transport.batch_bytes = len(moolit.serialize(first))

    outcome = apply.run(w, plan.Plan(ops=ops), refs, files={}, log=lambda _: None)
    assert outcome.failed == []
    assert w.batches == [[first], [second]]


def test_scoped_post_create_replan_keeps_live_ancestry_outside_group(monkeypatch):
    from copy import deepcopy
    from terramoo import export
    refs = Refs(ME, Registry({"parent": Obj(200), "door": Obj(201)}, {"parent": "a", "door": "b"}), {"exit": Obj(7)})
    parent = ObjectDef("parent", "Exit class", Ref("$", "exit"))
    desired = ObjectDef("door", "Door", Ref("@", "parent"), props=[PropDef("source", Obj(11), defined=False)])
    current = deepcopy(desired)
    current.props[0].value = Obj(10)
    live = {"parent": parent, "door": current}
    monkeypatch.setattr(export, "export", lambda w, r, keys: {k: live[k] for k in keys})
    ops = apply._replan_created(None, [], ["door"], {"parent": parent, "door": desired}, refs, full_context=True)
    endpoint = next(op for op in ops if op[0] == "endpoint")
    assert endpoint[2:6] == ("source", Obj(10), Obj(11), 1)


def test_verb_name_is_logged_while_numeric_descriptor_is_sent_to_helper():
    hall = Obj(200)
    w = FakeWorld()
    refs = verify_bindings(w, {"hall": hall})
    op = (
        "verbcode",
        Ref("@", "hall"),
        plan.VerbTarget(1, "bow"),
        ["return 2;"],
    )

    outcome = apply.run(w, plan.Plan(ops=[op]), refs, files={}, log=lambda _: None)

    assert outcome.failed == []
    assert outcome.done == ["verbcode @hall.bow"]
    assert w.sent == [["verbcode", "hall", hall, "generation-hall", 1, ["return 2;"]]]


def test_apply_encodes_strings_for_the_moor_dialect():
    hall = Obj(200)
    w = FakeWorld()
    refs = verify_bindings(w, {"hall": hall})
    w.transport.literal_dialect = moolit.MOOR

    outcome = apply.run(
        w,
        plan.Plan(ops=[("name", Ref("@", "hall"), "Hé\n")]),
        refs,
        files={},
        log=lambda _: None,
    )

    assert outcome.failed == []
    assert w.sent == [["name", "hall", hall, "generation-hall", "Hé\n"]]


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
    refs = verify_bindings(w, {"hall": hall})
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
    refs = verify_bindings(w, {"hall": hall})
    p = plan.Plan(ops=[("name", Ref("@", "hall"), "A")])

    outcome = apply.run(w, p, refs, files={}, log=lambda _: None)

    assert outcome.failed == [("name @hall A", expected)]
    assert w.saved == {"hall": hall}
