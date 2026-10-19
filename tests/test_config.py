import json

import pytest

from fordlogger.config import load_config, default_config, INTERVALS


@pytest.fixture
def _clear_env(monkeypatch):
    for name in (
        "FORDLOGGER_DB_HOST",
        "FORDLOGGER_DB_PORT",
        "FORDLOGGER_STORE_RAW_JSON",
        "FORDLOGGER_GEOCODING",
    ):
        monkeypatch.delenv(name, raising=False)


def test_load_config_valid(tmp_path, _clear_env):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"sleep_after_minutes": 45}))
    cfg = load_config(str(cfg_file))
    assert cfg["sleep_after_minutes"] == 45
    # Defaults should be merged
    assert cfg["db_host"] == "db"
    assert cfg["db_port"] == 5432


def test_load_config_missing_file():
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/config.json")


def test_env_override(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({}))
    monkeypatch.setenv("FORDLOGGER_DB_HOST", "myhost")
    monkeypatch.setenv("FORDLOGGER_DB_PORT", "9999")
    cfg = load_config(str(cfg_file))
    assert cfg["db_host"] == "myhost"
    assert cfg["db_port"] == 9999


def test_config_file_flags_not_overwritten_by_unset_env(tmp_path, _clear_env):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"geocoding": False, "store_raw_json": False}))
    cfg = load_config(str(cfg_file))
    assert cfg["geocoding"] is False
    assert cfg["store_raw_json"] is False


def test_env_flags_override_config_file(tmp_path, _clear_env, monkeypatch):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"geocoding": True}))
    monkeypatch.setenv("FORDLOGGER_GEOCODING", "false")
    assert load_config(str(cfg_file))["geocoding"] is False


def test_default_config_needs_no_file(_clear_env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = default_config()
    assert cfg["db_port"] == 5432
    assert cfg["geocoding"] is True


def test_intervals_defined():
    assert set(INTERVALS.keys()) == {"driving", "charging", "parked", "sleeping"}
    assert INTERVALS["driving"] <= INTERVALS["charging"] <= INTERVALS["parked"] <= INTERVALS["sleeping"]
