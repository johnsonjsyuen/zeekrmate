import os
import pytest
import psycopg2

from fordlogger.config import load_config
from fordlogger import db

TEST_DB_NAME = "fordlogger_test"


@pytest.fixture(scope="session")
def cfg():
    os.environ["FORDLOGGER_DB_HOST"] = os.environ.get("FORDLOGGER_DB_HOST", "localhost")
    config = load_config("config.json")
    config["db_name"] = TEST_DB_NAME
    return config


@pytest.fixture(scope="session")
def conn(cfg):
    assert cfg["db_name"] == TEST_DB_NAME, (
        f"Safety check failed: tests must use '{TEST_DB_NAME}', got '{cfg['db_name']}'"
    )

    # Connect to default DB to create the test database
    admin_conn = psycopg2.connect(
        host=cfg["db_host"],
        port=cfg["db_port"],
        dbname="fordlogger",
        user=cfg["db_user"],
        password=cfg["db_password"],
    )
    admin_conn.autocommit = True
    with admin_conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM pg_database WHERE datname = '{TEST_DB_NAME}'")
        if not cur.fetchone():
            cur.execute(f"CREATE DATABASE {TEST_DB_NAME}")
    admin_conn.close()

    # Connect to the test database
    c = db.connect(cfg)

    # Verify we are actually connected to the test DB
    with c.cursor() as cur:
        cur.execute("SELECT current_database()")
        actual_db = cur.fetchone()[0]
    assert actual_db == TEST_DB_NAME, (
        f"Safety check failed: connected to '{actual_db}', expected '{TEST_DB_NAME}'"
    )

    db.ensure_schema(c)
    return c


@pytest.fixture(autouse=True)
def _clean_tables(conn):
    """Clean test data before each test (keep schema)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM states")
        cur.execute("DELETE FROM charge_sessions")
        cur.execute("DELETE FROM trips")
        cur.execute("DELETE FROM positions")
        cur.execute("DELETE FROM vehicles")
    yield
