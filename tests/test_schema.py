from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from fordlogger import db
from fordlogger.models import Position, Vehicle


def _columns(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,),
        )
        return {r[0] for r in cur.fetchall()}


def _versions(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations ORDER BY version")
        return [r[0] for r in cur.fetchall()]


class TestEnsureSchema:
    def test_rerun_is_idempotent_and_keeps_data(self, conn):
        db.upsert_vehicle(conn, Vehicle(vin="TEST001"))
        db.insert_position(conn, Position(ts=datetime.now(timezone.utc), vin="TEST001"))
        db.ensure_schema(conn)
        db.ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM positions")
            assert cur.fetchone()[0] == 1
        assert conn.autocommit is True

    def test_baseline_has_ingest_columns(self, conn):
        assert {"observed_at", "snapshot_key"} <= _columns(conn, "positions")
        assert "version" in _columns(conn, "schema_migrations")

    def test_pending_migration_applied_once(self, conn, tmp_path):
        (tmp_path / "0001_add_probe.sql").write_text(
            "ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS probe_col TEXT;")
        try:
            assert db.ensure_schema(conn, tmp_path) == ["0001_add_probe"]
            assert db.ensure_schema(conn, tmp_path) == []
            assert "probe_col" in _columns(conn, "vehicles")
            assert "0001_add_probe" in _versions(conn)
        finally:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE vehicles DROP COLUMN IF EXISTS probe_col")
                cur.execute("DELETE FROM schema_migrations WHERE version = '0001_add_probe'")

    def test_failing_migration_rolls_back_everything(self, conn, tmp_path):
        (tmp_path / "0001_ok.sql").write_text(
            "ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS ok_col TEXT;")
        (tmp_path / "0002_broken.sql").write_text("ALTER TABLE no_such_table ADD COLUMN x INT;")
        with pytest.raises(psycopg2.Error):
            db.ensure_schema(conn, tmp_path)
        assert "ok_col" not in _columns(conn, "vehicles")
        assert "0001_ok" not in _versions(conn)
        assert conn.autocommit is True
        db.upsert_vehicle(conn, Vehicle(vin="TEST001"))  # connection still usable

    def test_duplicate_migration_numbers_rejected(self, tmp_path):
        (tmp_path / "0001_a.sql").write_text("SELECT 1;")
        (tmp_path / "0001_b.sql").write_text("SELECT 1;")
        with pytest.raises(ValueError, match="0001"):
            db.migration_files(tmp_path)

    def test_non_migration_files_ignored(self, tmp_path):
        (tmp_path / "README.md").write_text("x")
        (tmp_path / "1_bad.sql").write_text("x")
        (tmp_path / "0002_b.sql").write_text("SELECT 1;")
        (tmp_path / "0001_a.sql").write_text("SELECT 1;")
        assert [p.name for p in db.migration_files(tmp_path)] == ["0001_a.sql", "0002_b.sql"]


class TestPositionIngestColumns:
    def test_observed_at_and_snapshot_key_round_trip(self, conn):
        db.upsert_vehicle(conn, Vehicle(vin="TEST001"))
        ts = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
        observed = ts + timedelta(seconds=42)
        db.insert_position(conn, Position(ts=ts, vin="TEST001", observed_at=observed,
                                          snapshot_key="abc123"))
        latest = db.get_latest_position(conn, "TEST001")
        assert latest["ts"] == ts
        assert latest["observed_at"] == observed
        assert latest["snapshot_key"] == "abc123"

    def test_observed_at_defaults_to_insert_time(self, conn):
        db.upsert_vehicle(conn, Vehicle(vin="TEST001"))
        before = datetime.now(timezone.utc) - timedelta(seconds=5)
        db.insert_position(conn, Position(ts=datetime(2020, 1, 1, tzinfo=timezone.utc),
                                          vin="TEST001"))
        latest = db.get_latest_position(conn, "TEST001")
        assert latest["observed_at"] >= before
        assert latest["snapshot_key"] is None

    def test_position_new_fields_default_none(self):
        p = Position(ts=datetime.now(timezone.utc), vin="X")
        assert p.observed_at is None and p.snapshot_key is None


class TestEvolveExistingDatabase:
    def test_pre_pr2_positions_table_gets_ingest_columns(self, conn):
        db.upsert_vehicle(conn, Vehicle(vin="TEST001"))
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        db.insert_position(conn, Position(ts=ts, vin="TEST001"))
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE positions DROP COLUMN observed_at, DROP COLUMN snapshot_key")
            cur.execute("DELETE FROM schema_migrations")
        assert db.ensure_schema(conn) == ["0001_positions_ingest_columns"]
        assert {"observed_at", "snapshot_key"} <= _columns(conn, "positions")
        latest = db.get_latest_position(conn, "TEST001")
        assert latest["observed_at"] == ts  # backfilled from ts
        assert db.ensure_schema(conn) == []


class TestRawJson:
    def test_datetime_and_decimal_are_stringified(self, conn):
        from decimal import Decimal
        db.upsert_vehicle(conn, Vehicle(vin="TEST001"))
        now = datetime.now(timezone.utc)
        pid = db.insert_position(conn, Position(
            ts=now, vin="TEST001", raw_json={"at": now, "kw": Decimal("7.4")}))
        with conn.cursor() as cur:
            cur.execute("SELECT raw_json FROM positions WHERE id = %s", (pid,))
            raw = cur.fetchone()[0]
        assert raw == {"at": str(now), "kw": "7.4"}

    def test_nan_stores_marker(self, conn):
        db.upsert_vehicle(conn, Vehicle(vin="TEST001"))
        pid = db.insert_position(conn, Position(
            ts=datetime.now(timezone.utc), vin="TEST001", raw_json={"x": float("nan")}))
        with conn.cursor() as cur:
            cur.execute("SELECT raw_json FROM positions WHERE id = %s", (pid,))
            assert cur.fetchone()[0] == {"_unserialisable": "ValueError"}

    def test_empty_payload_is_stored(self, conn):
        db.upsert_vehicle(conn, Vehicle(vin="TEST001"))
        pid = db.insert_position(conn, Position(
            ts=datetime.now(timezone.utc), vin="TEST001", raw_json={}))
        with conn.cursor() as cur:
            cur.execute("SELECT raw_json FROM positions WHERE id = %s", (pid,))
            assert cur.fetchone()[0] == {}


class TestVehicleUpsert:
    def test_sparse_insert_is_filled_later(self, conn):
        db.ensure_vehicle(conn, "TEST001")
        db.ensure_vehicle(conn, "TEST001")
        db.upsert_vehicle(conn, Vehicle(vin="TEST001", make="Zeekr", model="7X",
                                        model_year=2025, color="Grey", engine_type="BEV"))
        db.upsert_vehicle(conn, Vehicle(vin="TEST001"))
        with conn.cursor() as cur:
            cur.execute("SELECT make, model, model_year, color, engine_type FROM vehicles")
            assert cur.fetchone() == ("Zeekr", "7X", 2025, "Grey", "BEV")


class TestStateHelpers:
    def test_last_state_change(self, conn):
        db.upsert_vehicle(conn, Vehicle(vin="TEST001"))
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        db.insert_state(conn, "TEST001", t0, "driving", "parked")
        db.insert_state(conn, "TEST001", t0 + timedelta(hours=1), "parked", "driving")
        db.insert_state(conn, "TEST001", t0 + timedelta(hours=2), "driving", "parked")
        assert db.get_last_state_change(conn, "TEST001", "driving") == t0 + timedelta(hours=2)
        assert db.get_last_state_change(conn, "TEST001", "charging") is None
