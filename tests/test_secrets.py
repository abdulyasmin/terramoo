import stat

import pytest

from terramoo import secrets
from terramoo.errors import MooError
from terramoo.secrets import check_secret


def test_plain_secret_is_stripped():
    assert check_secret("  abc.DEF-123\n") == "abc.DEF-123"


@pytest.mark.parametrize("pasted", ['{"token": ', '"abc"', "'abc'", "[1]", "", "   "])
def test_json_quotes_and_empty_are_refused(pasted):
    with pytest.raises(MooError):
        check_secret(pasted)


def test_environment_secret_has_priority_and_is_not_stripped(monkeypatch):
    monkeypatch.setenv(secrets.ENV_VAR, "  from-env  ")
    monkeypatch.setattr(secrets, "_keychain_read", lambda world: pytest.fail("keychain read"))
    assert secrets.secret_for("test") == "  from-env  "


def test_keychain_has_priority_over_the_config_file(tmp_path, monkeypatch):
    monkeypatch.delenv(secrets.ENV_VAR, raising=False)
    monkeypatch.setattr(secrets, "CONFIG_DIR", tmp_path)
    (tmp_path / "test.secret").write_text("from-file\n")
    monkeypatch.setattr(secrets, "_keychain_read", lambda world: "from-keychain")
    assert secrets.secret_for("test") == "from-keychain"


def test_config_file_is_the_final_lookup_source(tmp_path, monkeypatch):
    monkeypatch.delenv(secrets.ENV_VAR, raising=False)
    monkeypatch.setattr(secrets, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(secrets, "_keychain_read", lambda world: None)
    (tmp_path / "test.secret").write_text("  from-file\n")
    assert secrets.secret_for("test") == "from-file"


def test_missing_secret_names_the_safe_storage_options(tmp_path, monkeypatch):
    monkeypatch.delenv(secrets.ENV_VAR, raising=False)
    monkeypatch.setattr(secrets, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(secrets, "_keychain_read", lambda world: None)
    with pytest.raises(MooError, match="tmoo secret store test.*TMOO_SECRET"):
        secrets.secret_for("test")


def test_non_macos_store_creates_a_mode_600_file(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets.sys, "platform", "linux")
    monkeypatch.setattr(secrets, "CONFIG_DIR", tmp_path / "terramoo")
    location = secrets.store_secret("test", "secret-value")
    path = tmp_path / "terramoo" / "test.secret"
    assert location == str(path)
    assert path.read_text() == "secret-value\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_macos_store_uses_the_keychain_without_returning_the_secret(monkeypatch):
    calls = []
    monkeypatch.setattr(secrets.sys, "platform", "darwin")
    monkeypatch.setattr(secrets.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    location = secrets.store_secret("test", "secret-value")
    assert location == "Keychain (terramoo/test)"
    assert calls == [
        (
            ["security", "add-generic-password", "-U", "-s", "terramoo", "-a", "test", "-w", "secret-value"],
            {"check": True, "capture_output": True},
        )
    ]
    assert "secret-value" not in location
