"""Audit repro: test-infrastructure hazard found while running the suite.

Strict xfail: fails today (proving the defect), XPASSes once fixed.
"""
import os

import pytest

import tests.test_config as _config_tests


@pytest.fixture(autouse=True)
def _clean_tables():
    """This check needs no database."""
    yield


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "AUDIT-T1: tests/test_config.py:43-51 sets FORDLOGGER_DB_PORT, then "
        "pops it in `finally` instead of restoring it. Any later load_config() "
        "in the same session falls back to db_port 5432 (config.py:17); "
        "test_live_api.py:25-40 then connects to localhost:5432/fordlogger "
        "(not the test DB) and runs ensure_schema there."
    ),
)
def test_env_override_restores_caller_db_port(tmp_path, monkeypatch):
    # Register both variables so monkeypatch restores them even though the
    # function under test overwrites them.
    monkeypatch.setenv(
        "FORDLOGGER_DB_HOST", os.environ.get("FORDLOGGER_DB_HOST", "localhost")
    )
    monkeypatch.setenv("FORDLOGGER_DB_PORT", "5434")

    _config_tests.test_env_override(tmp_path)

    assert os.environ.get("FORDLOGGER_DB_PORT") == "5434"
