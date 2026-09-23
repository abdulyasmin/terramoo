"""apply.run against a fake world that runs tmoo_apply and tmoo_export in memory."""

from types import SimpleNamespace

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
        self.transport = SimpleNamespace(batch_bytes=24_000)
        self.ignore_props: set[str] = set()

    def helper(self, verb, arg):
        return verb, arg

    def eval(self, call):
        verb, text = call
        value = moolit.parse(text)

        def no_refs(v):
            if isinstance(v, Ref):  # the MOO has no @key syntax
                raise MooError("the expression did not compile")
            return v

        moolit.walk(value, no_refs)
        if verb == "tmoo_export":
            return [[o, *self.names[o], Obj(-1), ME, "", [], []] for o in value]
        return [self._op(op) for op in value]

    def _op(self, op):
        self.sent.append(op)
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
