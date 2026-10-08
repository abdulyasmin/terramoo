import pytest

from terramoo import objdef
from terramoo.errors import MooError
from terramoo.moolit import Obj, Ref
from terramoo.packages import Package
from terramoo.storage import toml_text
from terramoo.installation import Store
from terramoo.world import World
from terramoo.updates import update, remove, candidate_path


def source(tmp_path):
    root = tmp_path / "town"
    rooms = root / "modules" / "rooms"
    rooms.mkdir(parents=True)
    (root / "package.toml").write_text(toml_text({"schema_version": 1, "name": "town", "version": "1.0.0",
        "modules": ["modules/rooms"], "inputs": {"entry": {"type": "object", "required": True}}}))
    (rooms / "module.toml").write_text(toml_text({"schema_version": 1, "name": "rooms", "uses_inputs": ["entry"]}))
    (rooms / "square.moo").write_text('''object square
  name: "Town square"
  parent: #0
  property entry (flags: "rc") = @entry;
  property peer (flags: "rc") = @inn;
  property literal (flags: "rc") = "@inn";
  verb look (this none this) flags: "rxd"
    return "@inn";
  endverb
endobject
''')
    (rooms / "inn.moo").write_text('object inn\n  name: "Inn"\n  parent: #0\nendobject\n')
    return root


def test_instances_compile_independent_keys_and_leave_code_literal(tmp_path):
    package = Package.load(source(tmp_path))
    north = package.compile("north", "north", {"entry": "@arrival"}, {"arrival": "local/arrival"}, {})
    south = package.compile("south", "south", {"entry": "#-1"}, {}, {})
    n = objdef.parse(north.objects["square"])
    s = objdef.parse(south.objects["square"])
    assert (n.key, s.key) == ("north__square", "south__square")
    assert n.props[0].value == Ref("@", "arrival") and s.props[0].value == Obj(-1)
    assert n.props[1].value == Ref("@", "north__inn")
    assert n.props[2].value == "@inn" and n.verbs[0].code == ['return "@inn";']
    assert "local/arrival" in north.modules["rooms"] and "local/arrival" not in south.modules["rooms"]


def test_import_mappings_survive_compilation(tmp_path):
    package = Package.load(source(tmp_path))
    compiled = package.compile("north", "north", {"entry": "#-1"}, {}, {}, previous={"square": "old_square", "inn": "old_inn"})
    assert objdef.parse(compiled.objects["square"]).key == "old_square"
    assert objdef.parse(compiled.objects["square"]).props[1].value == Ref("@", "old_inn")


@pytest.mark.parametrize("value", ["#123", "#048D05-1234567890", "@missing"])
def test_world_specific_or_undeclared_source_refs_fail(tmp_path, value):
    root = source(tmp_path)
    path = root / "modules/rooms/inn.moo"
    path.write_text(path.read_text().replace("parent: #0", f"parent: {value}"))
    with pytest.raises(MooError):
        Package.load(root)


def test_source_inventory_cannot_omit_nested_modules(tmp_path):
    root = source(tmp_path)
    nested = root / "modules/rooms/nested"
    nested.mkdir()
    (nested / "module.toml").write_text('schema_version = 1\nname = "nested"\n')
    with pytest.raises(MooError, match="every module manifest"):
        Package.load(root)


def test_missing_input_has_no_partial_compilation(tmp_path):
    package = Package.load(source(tmp_path))
    with pytest.raises(MooError, match="missing required binding"):
        package.compile("north", "north", {}, {}, {})


def make_world(tmp_path, name="alpha"):
    w = World(name, tmp_path, "alice", {})
    w.objects_dir.mkdir(parents=True)
    (w.dir / "world.toml").write_text('player = "alice"\n[connection]\nhost = "localhost"\n')
    return w


def test_install_same_source_twice_is_offline_and_independent(tmp_path, monkeypatch):
    root = source(tmp_path)
    w = make_world(tmp_path)
    monkeypatch.setattr(w, "refs", lambda: pytest.fail("connected"))
    Store(w).install("north", root, bindings={"entry": "#-1"})
    Store(w).install("south", root, bindings={"entry": "#0"})
    store = Store(w)
    assert set(store.instances) == {"north", "south"}
    assert store.instances["north"]["id"] != store.instances["south"]["id"]
    assert set(w.load_files()) == {"north__square", "north__inn", "south__square", "south__inn"}
    assert set(store.validate().modules) == {"north/rooms", "south/rooms"}
    (root / "modules/rooms/inn.moo").write_text("upstream is now invalid")
    store.validate()
    assert store.load_source("north").objects["inn"].name == "Inn"


def test_install_rejects_collision_without_writing_metadata(tmp_path):
    from terramoo.model import ObjectDef
    root = source(tmp_path)
    w = make_world(tmp_path)
    w.write_file(ObjectDef("north__inn", "Unrelated", Obj(0)))
    with pytest.raises(MooError, match="occupied"):
        Store(w).install("north", root, bindings={"entry": "#-1"})
    assert not (w.dir / "packages.lock.json").exists()
    assert len(w.load_files()) == 1


def test_missing_instance_manifest_cannot_change_membership(tmp_path):
    w = make_world(tmp_path)
    Store(w).install("north", source(tmp_path), bindings={"entry": "#-1"})
    (w.objects_dir / "packages/north/modules/rooms/module.toml").unlink()
    with pytest.raises(MooError, match="missing or moved module manifest"):
        Store(w).validate()


def test_update_preserves_local_edits_and_sibling_snapshot(tmp_path):
    root = source(tmp_path)
    w = make_world(tmp_path)
    Store(w).install("north", root, bindings={"entry": "#-1"})
    Store(w).install("south", root, bindings={"entry": "#-1"})
    square = w.file_for("north__square")
    square.write_text(square.read_text().replace('"Town square"', '"Local square"'))
    inn = root / "modules/rooms/inn.moo"
    inn.write_text(inn.read_text().replace('"Inn"', '"New inn"'))
    update(Store(w), ["north"])
    assert w.load_files()["north__square"].name == "Local square"
    assert w.load_files()["north__inn"].name == "New inn"
    assert w.load_files()["south__inn"].name == "Inn"
    assert Store(w).instances["north"]["digest"] != Store(w).instances["south"]["digest"]


def test_conflicted_update_retains_active_state_and_can_resume(tmp_path):
    import json
    root = source(tmp_path)
    w = make_world(tmp_path)
    Store(w).install("north", root, bindings={"entry": "#-1"})
    path = w.file_for("north__inn")
    path.write_text(path.read_text().replace('"Inn"', '"Local inn"'))
    before = (w.dir / "packages.lock.json").read_bytes()
    upstream = root / "modules/rooms/inn.moo"
    upstream.write_text(upstream.read_text().replace('"Inn"', '"Upstream inn"'))
    with pytest.raises(MooError, match="update conflicts retained"):
        update(Store(w), ["north"])
    assert (w.dir / "packages.lock.json").read_bytes() == before
    assert w.load_files()["north__inn"].name == "Local inn"
    candidate = json.loads(candidate_path(Store(w)).read_text())
    relative = str(path.relative_to(w.dir))
    edit = w.dir / ".packages/candidates" / candidate["id"] / relative
    edit.write_text(path.read_text().replace('"Local inn"', '"Resolved inn"'))
    update(Store(w), ["north"], resume=True)
    assert w.load_files()["north__inn"].name == "Resolved inn"
    assert '"Upstream inn"' in Store(w).instances["north"]["objects"]["inn"]["baseline"]
    assert not candidate_path(Store(w)).exists()


def test_remove_one_instance_leaves_sibling_and_retains_history(tmp_path):
    w = make_world(tmp_path)
    root = source(tmp_path)
    Store(w).install("north", root, bindings={"entry": "#-1"})
    Store(w).install("south", root, bindings={"entry": "#-1"})
    remove(Store(w), ["north"])
    assert set(w.load_files()) == {"south__square", "south__inn"}
    store = Store(w)
    assert store.instances["north"]["state"] == "removing"
    assert set(store.instances["north"]["tombstones"]) == {"north__inn", "north__square"}
    store.validate()


def test_source_key_migration_keeps_installed_keys_and_sibling(tmp_path):
    from terramoo.updates import migrate_source_keys
    root = source(tmp_path)
    w = make_world(tmp_path)
    for name in ("north", "south"):
        Store(w).install(name, root, bindings={"entry": "#-1"})
    old = root / "modules/rooms/inn.moo"
    old.with_name("hotel.moo").write_text(old.read_text().replace("object inn", "object hotel"))
    old.unlink()
    square = root / "modules/rooms/square.moo"
    square.write_text(square.read_text().replace("= @inn;", "= @hotel;"))
    mapping = tmp_path / "keys.toml"
    mapping.write_text('schema_version = 1\n[objects]\ninn = "hotel"\n')
    with pytest.raises(MooError, match="both added and removed"):
        update(Store(w), ["north"])
    migrate_source_keys(Store(w), "north", mapping)
    store = Store(w)
    assert store.instances["north"]["objects"]["hotel"]["key"] == "north__inn"
    assert "inn" in store.instances["south"]["objects"]
    assert "north__hotel" not in w.load_files()
    update(Store(w), ["north"])
    assert w.load_files()["north__square"].props[1].value == Ref("@", "north__inn")


def test_instance_rename_preserves_default_namespace_and_pinned_source(tmp_path):
    from terramoo.migrations import rename_instance
    root = source(tmp_path)
    w = make_world(tmp_path)
    Store(w).install("north", root, bindings={"entry": "#-1"})
    original = Store(w).instances["north"]["id"]
    rename_instance(Store(w), "north", "east")
    store = Store(w)
    assert set(store.instances) == {"east"}
    assert store.instances["east"]["id"] == original
    assert store.instances["east"]["spec"]["namespace"] == "north"
    assert w.file_for("north__inn").is_relative_to(w.objects_dir / "packages/east")
    update(Store(w), ["east"])
    assert set(w.load_files()) == {"north__inn", "north__square"}


def test_package_parent_and_initialization_cycles_fail_offline(tmp_path):
    root = source(tmp_path)
    inn = root / "modules/rooms/inn.moo"
    inn.write_text(inn.read_text().replace("parent: #0", "parent: @inn"))
    with pytest.raises(MooError, match="parent cycle"):
        Package.load(root)


def test_import_preserves_definition_and_rewrites_consumer_membership(tmp_path):
    from terramoo.imports import prepare
    root = source(tmp_path)
    w = make_world(tmp_path)
    local = w.objects_dir / "old"
    local.mkdir()
    (local / "module.toml").write_text('schema_version = 1\nname = "old"\n')
    for key, text in Package.load(root).compile("north", "north", {"entry": "#-1"}, {}, {}).objects.items():
        (local / f"north__{key}.moo").write_text(text)
    consumer = w.objects_dir / "consumer"
    consumer.mkdir()
    (consumer / "module.toml").write_text('schema_version = 1\nname = "consumer"\nreferences = ["old"]\n')
    (consumer / "consumer.moo").write_text('object consumer\n name: "Consumer"\n parent: @north__inn\nendobject\n')
    mapping = tmp_path / "mapping.toml"
    mapping.write_text('schema_version = 1\n[objects]\ninn = "north__inn"\nsquare = "north__square"\n')
    proposed, _ = prepare(Store(w), root, "north", mapping, bindings={"entry": "#-1"})
    assert 'north/rooms' in proposed[1][consumer / "module.toml"]
    assert proposed[1][local / "north__inn.moo"] is None


def test_key_migration_updates_sibling_binding_and_three_way_baselines(tmp_path):
    from terramoo.migrations import prepare
    root = source(tmp_path)
    w = make_world(tmp_path)
    Store(w).install("north", root, bindings={"entry": "#-1"})
    Store(w).install("south", root, bindings={"entry": "@north__inn"})
    store = Store(w)
    proposal = prepare(store, {"north__inn": "grand_inn"})
    assert proposal["lock"]["instances"]["south"]["spec"]["bindings"]["entry"] == "@grand_inn"
    assert "@grand_inn" in proposal["lock"]["instances"]["south"]["objects"]["square"]["baseline"]
    store.commit(proposal["changes"], proposal["lock"], proposal["config"], expected=proposal["expected"])
    update(Store(w), ["north", "south"])
    assert w.load_files()["south__square"].props[0].value == Ref("@", "grand_inn")
    assert "grand_inn" in w.load_files() and "north__inn" not in w.load_files()


def test_shared_dependency_is_installed_once_and_exact_version_changes_are_checked(tmp_path):
    import tomllib
    kit = tmp_path / "kit"
    (kit / "base").mkdir(parents=True)
    (kit / "package.toml").write_text('schema_version = 1\nname = "kit"\nversion = "1"\nmodules = ["base"]\n')
    (kit / "base/module.toml").write_text('schema_version = 1\nname = "base"\n')
    (kit / "base/anchor.moo").write_text('object anchor\n name: "Anchor"\n parent: #0\nendobject\n')
    root = source(tmp_path)
    manifest = root / "package.toml"
    package = tomllib.loads(manifest.read_text())
    package["dependencies"] = {"kit": {"package": "kit", "version": "1"}}
    manifest.write_text(toml_text(package))
    w = make_world(tmp_path)
    config_path = w.dir / "world.toml"
    config = tomllib.loads(config_path.read_text())
    config["packages"] = {"shared": {"source": str(kit)}, "north": {"source": str(root), "dependencies": {"kit": "shared"}},
                          "south": {"source": str(root), "dependencies": {"kit": "shared"}}}
    config_path.write_text(toml_text(config))
    Store(w).install("north", root, bindings={"entry": "@shared__anchor"})
    shared_id = Store(w).instances["shared"]["id"]
    Store(w).install("south", root, bindings={"entry": "@shared__anchor"})
    assert Store(w).instances["shared"]["id"] == shared_id
    assert len(w.load_files()) == 5
    metadata = kit / "package.toml"
    metadata.write_text(metadata.read_text().replace('version = "1"', 'version = "2"'))
    before = (w.dir / "packages.lock.json").read_bytes()
    with pytest.raises(MooError, match="incompatible provider"):
        update(Store(w), ["shared"])
    assert (w.dir / "packages.lock.json").read_bytes() == before
    package["dependencies"]["kit"]["version"] = "2"
    manifest.write_text(toml_text(package))
    update(Store(w), ["shared", "north", "south"])
    Store(w).validate()
    with pytest.raises(MooError, match="still requires"):
        remove(Store(w), ["shared"])


def test_same_source_in_two_worlds_has_separate_installations_and_bindings(tmp_path):
    root = source(tmp_path)
    alpha, beta = make_world(tmp_path, "alpha"), make_world(tmp_path, "beta")
    Store(alpha).install("town", root, bindings={"entry": "#-1"})
    Store(beta).install("town", root, bindings={"entry": "#0"})
    a, b = Store(alpha), Store(beta)
    assert a.lock["world_id"] != b.lock["world_id"]
    assert a.instances["town"]["id"] != b.instances["town"]["id"]
    assert a.instances["town"]["digest"] == b.instances["town"]["digest"]
    assert alpha.load_files()["town__square"].props[0].value == Obj(-1)
    assert beta.load_files()["town__square"].props[0].value == Obj(0)
