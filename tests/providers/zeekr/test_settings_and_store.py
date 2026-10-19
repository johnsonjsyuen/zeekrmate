import json
import os
import stat
from pathlib import Path

import pytest

from zeekrmate.providers.zeekr import session_store as store_mod
from zeekrmate.providers.zeekr.session_store import SessionStore
from zeekrmate.providers.zeekr.settings import (
    SECRET_ENV_VARS,
    ZeekrConfigError,
    load_settings,
    resolve_state_dir,
)

from .conftest import SECRETS

FULL_ENV = {name: f"value-for-{kw}-xyz" for name, kw in SECRET_ENV_VARS.items()}


def test_settings_from_env_default_country():
    s = load_settings(FULL_ENV)
    assert s.country_code == "AU"
    assert s.username == "value-for-username-xyz"
    kwargs = s.client_kwargs()
    assert set(kwargs) == set(SECRET_ENV_VARS.values()) | {"country_code"}


def test_settings_country_override():
    assert load_settings({**FULL_ENV, "ZEEKR_COUNTRY_CODE": "nz"}).country_code == "NZ"


def test_missing_vars_listed_by_name_only():
    env = dict(FULL_ENV)
    del env["ZEEKR_PASSWORD"]
    env["ZEEKR_VIN_IV"] = "   "
    with pytest.raises(ZeekrConfigError) as info:
        load_settings(env)
    msg = str(info.value)
    assert "ZEEKR_PASSWORD" in msg and "ZEEKR_VIN_IV" in msg
    assert "ZEEKR_USERNAME" not in msg
    for value in env.values():
        assert value.strip() == "" or value not in msg


def test_settings_repr_hides_secrets(settings):
    text = repr(settings) + str(settings)
    for value in SECRETS.values():
        assert value not in text


def test_state_dir_resolution(tmp_path):
    assert resolve_state_dir({}, {"ZEEKRMATE_STATE_DIR": str(tmp_path)}) == tmp_path
    assert resolve_state_dir({"state_dir": "x"}, {}) == Path("x")
    assert resolve_state_dir({}, {}) == Path("data")


# --- session store ----------------------------------------------------------

def test_save_strips_password_and_sets_0600(tmp_path):
    store = SessionStore(tmp_path / "sub" / "zeekr_session.json")
    store.save({"username": "u", "password": "pw-should-not-persist", "bearer_token": "t"})
    raw = store.path.read_text()
    assert "pw-should-not-persist" not in raw
    assert json.loads(raw) == {"username": "u", "bearer_token": "t"}
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert store.load() == {"username": "u", "bearer_token": "t"}


def test_load_strips_password_from_legacy_file(tmp_path):
    p = tmp_path / "zeekr_session.json"
    p.write_text(json.dumps({"password": "old", "bearer_token": "t"}))
    assert SessionStore(p).load() == {"bearer_token": "t"}


def test_save_is_atomic_and_leaves_no_temp_on_failure(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "zeekr_session.json")
    store.save({"bearer_token": "old"})

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(store_mod.os, "replace", boom)
    with pytest.raises(OSError):
        store.save({"bearer_token": "new"})
    assert store.load() == {"bearer_token": "old"}
    assert sorted(os.listdir(tmp_path)) == ["zeekr_session.json"]


def test_save_uses_fsync_and_replace(tmp_path, monkeypatch):
    calls = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(store_mod.os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(store_mod.os, "replace", lambda a, b: (calls.append("replace"), real_replace(a, b))[1])
    SessionStore(tmp_path / "s.json").save({"bearer_token": "t"})
    assert calls.index("fsync") < calls.index("replace")


def test_unreadable_or_empty_session_is_ignored(tmp_path):
    p = tmp_path / "zeekr_session.json"
    p.write_text("{not json")
    assert SessionStore(p).load() is None
    p.write_text("{}")
    assert SessionStore(p).load() is None
    assert SessionStore(tmp_path / "missing.json").load() is None
    assert SessionStore(p).save({}) is False


def test_provider_session_file_has_no_password(make_provider):
    provider, _, store = make_provider()
    provider.get_telemetry()
    raw = store.path.read_text()
    assert SECRETS["password"] not in raw
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
