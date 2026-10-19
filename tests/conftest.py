"""Shared test fixtures.

* ``cfg`` is built from ``DEFAULT_CONFIG`` plus environment overrides; it never
  reads ``config.json``.
* ``conn`` is opt-in: only tests that request it need PostgreSQL. It refuses to
  run unless ``FORDLOGGER_DB_PORT`` is set explicitly, so the suite can never
  fall back to a default ``localhost:5432`` server; DB tests are skipped
  instead. Tables are emptied before every test that uses it.
* Network access is blocked for every test: geocoding is disabled and outbound
  non-loopback sockets raise.
"""
import os
import socket

import psycopg2
import pytest

from fordlogger.config import default_config
from fordlogger import db

TEST_DB_NAME = "fordlogger_test"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture
def cfg():
    config = default_config()
    config["db_host"] = os.environ.get("FORDLOGGER_DB_HOST") or "localhost"
    config["db_name"] = TEST_DB_NAME
    config["geocoding"] = False
    return config


@pytest.fixture(scope="session")
def _db_session():
    if not os.environ.get("FORDLOGGER_DB_PORT"):
        pytest.skip("set FORDLOGGER_DB_PORT (and FORDLOGGER_DB_HOST) to run DB tests")
    config = default_config()
    config["db_host"] = os.environ.get("FORDLOGGER_DB_HOST") or "localhost"
    config["db_name"] = TEST_DB_NAME
    assert config["db_name"] == TEST_DB_NAME

    admin_conn = psycopg2.connect(
        host=config["db_host"],
        port=config["db_port"],
        dbname="fordlogger",
        user=config["db_user"],
        password=config["db_password"],
    )
    admin_conn.autocommit = True
    # Recreate every session: a test DB left over from an older schema.sql
    # would silently keep its old columns (CREATE TABLE IF NOT EXISTS).
    with admin_conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
        cur.execute(f"CREATE DATABASE {TEST_DB_NAME}")
    admin_conn.close()

    c = db.connect(config)
    with c.cursor() as cur:
        cur.execute("SELECT current_database()")
        actual_db = cur.fetchone()[0]
    assert actual_db == TEST_DB_NAME, (
        f"Safety check failed: connected to '{actual_db}', expected '{TEST_DB_NAME}'"
    )
    db.ensure_schema(c)
    yield c
    c.close()


@pytest.fixture
def conn(_db_session):
    """A clean test database connection (tables emptied, schema kept)."""
    c = _db_session
    with c.cursor() as cur:
        cur.execute("DELETE FROM states")
        cur.execute("DELETE FROM charge_sessions")
        cur.execute("DELETE FROM trips")
        cur.execute("DELETE FROM positions")
        cur.execute("DELETE FROM vehicles")
    return c


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Disable geocoding and any non-loopback network access."""
    import fordlogger.geocoder as geocoder

    monkeypatch.setenv("FORDLOGGER_GEOCODING", "false")

    def _blocked_get(*args, **kwargs):
        raise RuntimeError("network access is disabled in tests")

    monkeypatch.setattr(geocoder.requests, "get", _blocked_get)

    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0]
            if host not in _LOOPBACK:
                raise RuntimeError(f"network access to {host} is disabled in tests")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    yield
