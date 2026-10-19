"""Audit regression (T1): config tests must not leak environment changes."""
import os

import pytest

import tests.test_config as _config_tests


def test_env_override_restores_caller_db_port(tmp_path, monkeypatch):
    # Register both variables so the outer monkeypatch restores them afterwards.
    monkeypatch.setenv(
        "ZEEKRMATE_DB_HOST", os.environ.get("ZEEKRMATE_DB_HOST", "localhost")
    )
    monkeypatch.setenv("ZEEKRMATE_DB_PORT", "5434")

    with pytest.MonkeyPatch.context() as inner:
        _config_tests.test_env_override(tmp_path, inner)

    assert os.environ.get("ZEEKRMATE_DB_PORT") == "5434"
