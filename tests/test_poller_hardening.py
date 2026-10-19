"""Poller boundary, dedup, restart and failure-handling behaviour."""
from datetime import datetime, timedelta, timezone

import pytest

import fordlogger.poller as poller_module
from fordlogger import db
from fordlogger.models import Position, Vehicle
from fordlogger.poller import Poller, PollResult, coerce_float
from fordlogger.state_machine import VehicleState

from tests.fakes import FakeProvider

VIN = "TESTVIN0000000001"


def _base():
    return datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=3)


def _pos(ts, **kw):
    kw.setdefault("speed_kmh", 0.0)
    kw.setdefault("charge_power_kw", 0.0)
    return Position(ts=ts, vin=VIN, **kw)


def _count(conn, table="positions"):
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


def _poll(poller, provider, *positions):
    provider.positions = list(positions)
    return poller.poll_once()


@pytest.fixture
def setup(conn, cfg):
    provider = FakeProvider(vehicles=[Vehicle(vin=VIN)])
    return Poller(cfg, conn, provider), provider


class TestBoundary:
    def test_unknown_vin_is_registered_and_stored(self, conn, cfg):
        provider = FakeProvider(vehicle_error=RuntimeError("down"))
        result = _poll(Poller(cfg, conn, provider), provider, _pos(_base(), soc_pct=50.0))
        assert db.get_latest_position(conn, VIN)["soc_pct"] == 50.0
        assert result.providers_failed == 1 and result.positions_stored == 1

    def test_failed_vehicle_list_retried_and_success_not_repeated(self, conn, cfg):
        provider = FakeProvider(vehicle_error=RuntimeError("down"))
        poller = Poller(cfg, conn, provider)
        poller.poll_once()
        provider.vehicle_error = None
        poller.poll_once()
        poller.poll_once()
        assert provider.vehicle_calls == 2

    def test_naive_timestamp_is_rejected(self, setup, conn):
        poller, provider = setup
        _poll(poller, provider, _pos(datetime(2026, 10, 19, 12, 0)))
        assert _count(conn) == 0

    def test_aware_non_utc_timestamp_is_converted(self, setup, conn):
        poller, provider = setup
        aest = timezone(timedelta(hours=10))
        ts = _base().astimezone(aest)
        _poll(poller, provider, _pos(ts))
        stored = db.get_latest_position(conn, VIN)["ts"]
        assert stored == ts and stored.utcoffset() == timedelta(0)

    def test_future_timestamp_is_rejected(self, setup, conn):
        poller, provider = setup
        _poll(poller, provider, _pos(datetime.now(timezone.utc) + timedelta(hours=10)))
        assert _count(conn) == 0

    def test_string_numerics_are_coerced(self, setup, conn):
        poller, provider = setup
        _poll(poller, provider, _pos(_base(), speed_kmh="42.5", soc_pct="bad",
                                     is_driving="true"))
        row = db.get_latest_position(conn, VIN)
        assert row["speed_kmh"] == 42.5 and row["soc_pct"] is None
        assert poller.machines[VIN].state == VehicleState.DRIVING

    @pytest.mark.parametrize("value, expected", [
        ("1.5", 1.5), (" 7 ", 7.0), ("", None), ("x", None), (True, None),
        (float("nan"), None), (float("inf"), None), (3, 3.0), (None, None)])
    def test_coerce_float(self, value, expected):
        assert coerce_float(value) == expected

    def test_observed_at_defaults_to_poll_time(self, setup, conn):
        poller, provider = setup
        before = datetime.now(timezone.utc)
        _poll(poller, provider, _pos(_base()))
        assert db.get_latest_position(conn, VIN)["observed_at"] >= before - timedelta(seconds=1)

    def test_bad_reading_does_not_block_others(self, setup, conn):
        poller, provider = setup
        result = _poll(poller, provider, object(), _pos(_base()))
        assert _count(conn) == 1
        assert result.positions_stored == 1

    def test_provider_reading_object_is_not_mutated(self, setup):
        poller, provider = setup
        p = _pos(_base(), speed_kmh="1")
        _poll(poller, provider, p)
        assert p.speed_kmh == "1" and p.state is None


class TestLogging:
    def test_logs_never_contain_the_full_vin(self, setup, caplog):
        poller, provider = setup
        t0 = _base()
        with caplog.at_level("DEBUG", logger="fordlogger"):
            _poll(poller, provider, _pos(t0))
            _poll(poller, provider, _pos(t0))  # dedup debug line
            for m in (2, 4, 6):
                _poll(poller, provider, _pos(t0 + timedelta(minutes=m), speed_kmh=50.0))
            for m in (8, 10):
                _poll(poller, provider, _pos(t0 + timedelta(minutes=m)))
        assert "***0001" in caplog.text
        assert VIN not in caplog.text


class TestDedup:
    def test_same_snapshot_key_skipped(self, setup, conn):
        poller, provider = setup
        t = _base()
        _poll(poller, provider, _pos(t, snapshot_key="a"))
        _poll(poller, provider, _pos(t + timedelta(minutes=1), snapshot_key="a"))
        assert _count(conn) == 1

    def test_same_ts_new_snapshot_key_processed(self, setup, conn):
        poller, provider = setup
        t = _base()
        _poll(poller, provider, _pos(t, snapshot_key="a"))
        _poll(poller, provider, _pos(t, snapshot_key="b", charge_power_kw=7.0))
        assert _count(conn) == 2
        assert poller.machines[VIN].state == VehicleState.CHARGING
        assert db.get_latest_position(conn, VIN)["snapshot_key"] in ("a", "b")

    def test_older_reading_ignored(self, setup, conn):
        poller, provider = setup
        t = _base()
        _poll(poller, provider, _pos(t, speed_kmh=50.0))
        _poll(poller, provider, _pos(t - timedelta(minutes=5)))
        assert _count(conn) == 1
        assert poller.machines[VIN].state == VehicleState.DRIVING

    def test_restart_does_not_reinsert_last_snapshot(self, conn, cfg):
        t = _base()
        provider = FakeProvider(positions=[_pos(t, snapshot_key="k")])
        Poller(cfg, conn, provider).poll_once()
        Poller(cfg, conn, provider).poll_once()
        assert _count(conn) == 1


class TestTrips:
    def _drive(self, poller, provider, t0):
        _poll(poller, provider, _pos(t0, odometer_km=1000.0, soc_pct=80.0,
                                     energy_remaining_kwh=40.0))
        _poll(poller, provider, _pos(t0 + timedelta(minutes=2), speed_kmh=60.0,
                                     odometer_km=1001.0, soc_pct=79.0,
                                     energy_remaining_kwh=39.5))
        _poll(poller, provider, _pos(t0 + timedelta(minutes=10), speed_kmh=80.0,
                                     odometer_km=1010.0, soc_pct=77.0,
                                     energy_remaining_kwh=38.0))

    def _trip(self, conn):
        with conn.cursor() as cur:
            cur.execute("SELECT start_ts, end_ts, distance_km, energy_used_kwh, "
                        "consumption_kwh_per_100km FROM trips")
            return cur.fetchall()

    def test_trip_starts_at_last_pre_drive_reading(self, setup, conn):
        poller, provider = setup
        t0 = _base()
        self._drive(poller, provider, t0)
        _poll(poller, provider, _pos(t0 + timedelta(minutes=15), odometer_km=1012.0,
                                     soc_pct=76.0, energy_remaining_kwh=37.5))
        _poll(poller, provider, _pos(t0 + timedelta(minutes=17), odometer_km=1012.0,
                                     soc_pct=76.0, energy_remaining_kwh=37.5))
        [(start, end, distance, energy, _)] = self._trip(conn)
        assert start == t0 and end == t0 + timedelta(minutes=15)
        assert distance == 12.0 and energy == 2.5

    def test_drive_into_charger_has_no_negative_energy(self, setup, conn):
        poller, provider = setup
        t0 = _base()
        self._drive(poller, provider, t0)
        _poll(poller, provider, _pos(t0 + timedelta(minutes=20), odometer_km=1012.0,
                                     soc_pct=85.0, energy_remaining_kwh=44.0,
                                     charge_power_kw=150.0))
        [(start, end, distance, energy, consumption)] = self._trip(conn)
        assert end == t0 + timedelta(minutes=10)
        assert energy is not None and energy >= 0
        assert consumption is None or consumption >= 0

    def test_frozen_moving_snapshot_ends_trip(self, setup, conn, monkeypatch):
        poller, provider = setup
        t0 = _base()
        self._drive(poller, provider, t0)
        assert poller.machines[VIN].state == VehicleState.DRIVING
        # The API keeps returning the last (moving) snapshot; 20 min pass.
        later = datetime.now(timezone.utc) + timedelta(minutes=20)
        monkeypatch.setattr(poller_module, "datetime", _FrozenDatetime(later))
        _poll(poller, provider, _pos(t0 + timedelta(minutes=10), speed_kmh=80.0))
        assert poller.machines[VIN].state == VehicleState.PARKED
        [(start, end, *_)] = self._trip(conn)
        assert end == t0 + timedelta(minutes=10)

    def test_restart_mid_trip_still_finalises(self, conn, cfg):
        t0 = _base()
        provider = FakeProvider()
        first = Poller(cfg, conn, provider)
        self._drive(first, provider, t0)
        # Process restarts while driving.
        second = Poller(cfg, conn, provider)
        for minutes in (12, 14):
            _poll(second, provider, _pos(t0 + timedelta(minutes=minutes),
                                         odometer_km=1011.0, soc_pct=76.0,
                                         energy_remaining_kwh=37.0))
        [(start, end, distance, *_)] = self._trip(conn)
        assert start == t0 and distance == 11.0

    def test_restart_mid_charge_still_finalises(self, conn, cfg):
        t0 = _base()
        provider = FakeProvider()
        first = Poller(cfg, conn, provider)
        _poll(first, provider, _pos(t0, charge_power_kw=7.0, soc_pct=20.0))
        _poll(first, provider, _pos(t0 + timedelta(minutes=30), charge_power_kw=7.0,
                                    soc_pct=40.0))
        second = Poller(cfg, conn, provider)
        assert second._get_machine(VIN).state == VehicleState.CHARGING
        for minutes in (60, 62):
            _poll(second, provider, _pos(t0 + timedelta(minutes=minutes), soc_pct=60.0))
        assert _count(conn, "charge_sessions") == 1

    def test_db_failure_mid_insert_keeps_the_drive(self, setup, conn, monkeypatch):
        poller, provider = setup
        t0 = _base()
        _poll(poller, provider, _pos(t0, odometer_km=1000.0))
        real_insert = db.insert_position
        calls = {"n": 0}

        def flaky(c, p):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("connection lost")
            return real_insert(c, p)

        monkeypatch.setattr(poller_module.db, "insert_position", flaky)
        moving = _pos(t0 + timedelta(minutes=2), speed_kmh=60.0, odometer_km=1001.0)
        result = _poll(poller, provider, moving)
        assert result.position_errors == 1
        assert poller.machines[VIN].state == VehicleState.PARKED  # rolled back
        _poll(poller, provider, moving)  # retried, not deduped
        assert poller.machines[VIN].state == VehicleState.DRIVING
        assert VIN in poller._drive_start


class TestChargeType:
    def test_22kw_ac_is_ac(self, setup, conn):
        poller, provider = setup
        t0 = _base()
        _poll(poller, provider, _pos(t0, charge_power_kw=21.5, soc_pct=20.0))
        _poll(poller, provider, _pos(t0 + timedelta(minutes=30), charge_power_kw=22.0,
                                     soc_pct=40.0))
        for m in (31, 32):
            _poll(poller, provider, _pos(t0 + timedelta(minutes=m), soc_pct=40.0))
        with conn.cursor() as cur:
            cur.execute("SELECT charge_type FROM charge_sessions")
            assert cur.fetchone()[0] == "AC"


class TestGeocoding:
    def test_addresses_filled_after_insert_and_failures_non_fatal(self, conn, cfg, monkeypatch):
        cfg["geocoding"] = True
        calls = []

        def fake_reverse(lat, lon):
            calls.append((lat, lon))
            if len(calls) == 1:
                raise RuntimeError("nominatim down")
            return "Somewhere"

        monkeypatch.setattr(poller_module.geocoder, "reverse_geocode", fake_reverse)
        provider = FakeProvider()
        poller = Poller(cfg, conn, provider)
        t0 = _base()
        _poll(poller, provider, _pos(t0, charge_power_kw=7.0, lat=1.0, lon=2.0))
        _poll(poller, provider, _pos(t0 + timedelta(minutes=30), charge_power_kw=7.0,
                                     lat=1.0, lon=2.0))
        for m in (31, 32):
            _poll(poller, provider, _pos(t0 + timedelta(minutes=m), lat=1.0, lon=2.0))
        assert _count(conn, "charge_sessions") == 1  # stored despite geocode failure
        assert calls


class TestLoop:
    def test_poll_result_flags(self):
        assert PollResult(providers_ok=1, positions_stored=1).ok
        assert not PollResult(providers_ok=1, positions_stored=0).ok
        assert not PollResult(providers_ok=1, providers_failed=1, positions_stored=1).ok
        assert PollResult(providers_failed=1).failed_cycle
        assert PollResult(db_ok=False).failed_cycle
        assert not PollResult(providers_ok=1, providers_failed=1).failed_cycle

    def test_backoff_is_exponential_and_capped(self, cfg):
        poller = Poller(cfg, None, [])
        base = poller.next_interval()
        poller.consecutive_failures = 1
        assert poller.next_interval() == min(base * 2, 600)
        poller.consecutive_failures = 50
        assert poller.next_interval() == max(600, base)

    def test_polling_intervals_configurable(self, cfg):
        cfg["polling"] = {"parked": 180}
        assert Poller(cfg, None, []).get_interval() == 180

    def test_run_forever_backs_off_then_stops(self, cfg, monkeypatch):
        poller = Poller(cfg, None, [])
        sleeps = []
        outcomes = iter([RuntimeError("boom"), PollResult(providers_failed=1)])

        def cycle():
            o = next(outcomes)
            if isinstance(o, Exception):
                raise o
            return o

        def fake_sleep(seconds):
            sleeps.append(poller.consecutive_failures)
            if len(sleeps) >= 2:
                poller.stop()

        monkeypatch.setattr(poller, "poll_once", cycle)
        monkeypatch.setattr(poller, "_sleep", fake_sleep)
        poller.run_forever()  # returns once stopped
        assert sleeps == [1, 2]

    def test_stop_before_start_exits_immediately(self, cfg, monkeypatch):
        poller = Poller(cfg, None, [])
        poller.stop()
        monkeypatch.setattr(poller, "poll_once", lambda: pytest.fail("polled"))
        poller.run_forever()


class _FrozenDatetime:
    """Stand-in for the poller's datetime module attribute with a fixed now()."""

    def __init__(self, now):
        self._now = now

    def now(self, tz=None):
        return self._now

    def __getattr__(self, name):
        return getattr(datetime, name)

    def __instancecheck__(self, obj):
        return isinstance(obj, datetime)


class TestStateRows:
    """E2E-5: state rows are never silently lost."""

    def test_reading_and_state_row_are_atomic(self, setup, conn, monkeypatch):
        poller, provider = setup
        t0 = _base()
        _poll(poller, provider, _pos(t0))
        real = db.insert_state
        calls = []

        def flaky(*a, **kw):
            calls.append(a)
            if len(calls) == 1:
                raise RuntimeError("state insert failed")
            return real(*a, **kw)

        monkeypatch.setattr(db, "insert_state", flaky)
        result = _poll(poller, provider, _pos(t0 + timedelta(minutes=2), speed_kmh=60.0))
        assert result.position_errors == 1 and _count(conn) == 1  # rolled back together
        assert poller.machines[VIN].state == VehicleState.PARKED
        _poll(poller, provider, _pos(t0 + timedelta(minutes=2), speed_kmh=60.0))
        assert _count(conn) == 2 and _count(conn, "states") == 1

    def test_tick_state_row_is_retried(self, setup, conn, monkeypatch):
        poller, provider = setup
        t0 = _base()
        _poll(poller, provider, _pos(t0, speed_kmh=60.0))
        real = db.insert_state
        fail = {"n": 1}

        def flaky(*a, **kw):
            if fail["n"]:
                fail["n"] -= 1
                raise RuntimeError("state insert failed")
            return real(*a, **kw)

        monkeypatch.setattr(db, "insert_state", flaky)
        later = datetime.now(timezone.utc) + timedelta(minutes=20)
        monkeypatch.setattr(poller_module, "datetime", _FrozenDatetime(later))
        _poll(poller, provider)  # no fresh data: tick ends the drive, state insert fails
        assert poller.machines[VIN].state == VehicleState.PARKED
        _poll(poller, provider)  # retried from the pending queue
        with conn.cursor() as cur:
            cur.execute("SELECT prev_state, state FROM states ORDER BY ts, id")
            assert cur.fetchall()[-1] == ("driving", "parked")


def test_recent_snapshot_is_not_stored_again(setup, conn):
    """E2E-11: A, garbage, A stores A once (the garbage reading carries A's ts)."""
    poller, provider = setup
    t = _base()
    _poll(poller, provider, _pos(t, snapshot_key="A"))
    _poll(poller, provider, _pos(t, snapshot_key="G", soc_pct=None))
    _poll(poller, provider, _pos(t, snapshot_key="A"))
    assert _count(conn) == 2
