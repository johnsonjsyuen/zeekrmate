"""Run sql/init-roles/20-grafana-readonly.sh against a scratch database and
check the Grafana role really is SELECT-only (independent security review).

Needs a PostgreSQL superuser connection (the test setup's ``fordlogger``
role), ``bash`` and ``psql``; skipped otherwise. Creates and drops its own
database and role, never touching ``fordlogger_test``.
"""

import os
import shutil
import subprocess
from pathlib import Path

import psycopg2
import pytest

from fordlogger.config import default_config

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "sql" / "init-roles" / "20-grafana-readonly.sh"
DB = "fordlogger_sec_ro_test"
ROLE = "grafana_ro_sec_test"
PASSWORD = "p'a\"ss w0rd;--"


def _admin(cfg, dbname="fordlogger"):
    c = psycopg2.connect(host=cfg["db_host"], port=cfg["db_port"], dbname=dbname,
                         user=cfg["db_user"], password=cfg["db_password"])
    c.autocommit = True
    return c


@pytest.fixture(scope="module")
def ro_conn():
    if not os.environ.get("FORDLOGGER_DB_PORT"):
        pytest.skip("set FORDLOGGER_DB_PORT to run DB tests")
    if not shutil.which("psql") or not shutil.which("bash"):
        pytest.skip("psql/bash not available")
    cfg = default_config()
    cfg["db_host"] = os.environ.get("FORDLOGGER_DB_HOST") or "localhost"
    admin = _admin(cfg)
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        cur.execute(f"DROP ROLE IF EXISTS {ROLE}")
        cur.execute(f"CREATE DATABASE {DB}")
    owner = _admin(cfg, DB)
    with owner.cursor() as cur:
        cur.execute((ROOT / "sql" / "schema.sql").read_text())
        for m in sorted((ROOT / "sql" / "migrations").glob("*.sql")):
            cur.execute(m.read_text())
        cur.execute("INSERT INTO vehicles (vin) VALUES ('LSECTEST000000001')")
    env = {**os.environ, "PGHOST": cfg["db_host"], "PGPORT": str(cfg["db_port"]),
           "PGPASSWORD": cfg["db_password"], "POSTGRES_USER": cfg["db_user"],
           "POSTGRES_DB": DB, "GRAFANA_DB_USER": ROLE, "GRAFANA_DB_PASSWORD": PASSWORD}
    for _ in range(2):  # idempotent
        subprocess.run(["bash", str(SCRIPT)], env=env, check=True, capture_output=True)
    # Tables added after the script ran (future migrations) must be readable too.
    with owner.cursor() as cur:
        cur.execute("CREATE TABLE later_table (x int)")
    ro = psycopg2.connect(host=cfg["db_host"], port=cfg["db_port"], dbname=DB,
                          user=ROLE, password=PASSWORD)
    ro.autocommit = True
    yield ro
    ro.close()
    owner.close()
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        cur.execute(f"DROP ROLE IF EXISTS {ROLE}")
    admin.close()


def _run(conn, *sql):
    with conn.cursor() as cur:
        for s in sql:
            cur.execute(s)
        return cur.fetchall() if cur.description else None


def test_role_can_select(ro_conn):
    assert _run(ro_conn, "SELECT count(*) FROM vehicles") == [(1,)]
    assert _run(ro_conn, "SELECT count(*) FROM later_table") == [(0,)]


def test_role_attributes(ro_conn):
    row = _run(ro_conn, "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
               "rolbypassrls FROM pg_roles WHERE rolname = current_user")
    assert row == [(False, False, False, False, False)]


@pytest.mark.parametrize("stmt", [
    "INSERT INTO vehicles (vin) VALUES ('X')",
    "UPDATE vehicles SET model = 'x'",
    "DELETE FROM vehicles",
    "TRUNCATE vehicles",
    "CREATE TABLE public.evil (x int)",
    "SELECT nextval('positions_id_seq')",
    "SELECT pg_read_file('/etc/hostname')",
    "COPY vehicles TO PROGRAM 'id'",
    "SELECT lo_import('/etc/hostname')",
    "SELECT * FROM pg_authid",
    "ALTER TABLE vehicles ADD COLUMN evil int",
    "CREATE SCHEMA evil",
])
def test_writes_denied_even_after_disabling_read_only(ro_conn, stmt):
    # default_transaction_read_only is user-settable; the GRANTs must hold anyway.
    _run(ro_conn, "SET default_transaction_read_only = off")
    with pytest.raises(psycopg2.Error):
        _run(ro_conn, stmt)
    _run(ro_conn, "RESET default_transaction_read_only")


@pytest.mark.xfail(strict=True, reason="PUBLIC keeps TEMP on the database: the role can "
                   "create temp tables/functions once it turns read-only off (low)")
def test_role_cannot_create_temp_objects(ro_conn):
    _run(ro_conn, "SET default_transaction_read_only = off")
    try:
        with pytest.raises(psycopg2.Error):
            _run(ro_conn, "CREATE TEMP TABLE t_sec (x int)")
    finally:
        _run(ro_conn, "RESET default_transaction_read_only")
