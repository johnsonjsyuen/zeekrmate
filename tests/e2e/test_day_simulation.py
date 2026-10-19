"""End-to-end day simulation: real ZeekrProvider + mapper + Poller + PostgreSQL.

A fake zeekr_ev_api client serves payloads shaped like the ones the mapper
reads (see tests/providers/zeekr/conftest.py) from a simulated car whose state
is integrated over a controllable clock. ``Poller.run_forever`` drives the
cadence: its ``_sleep`` is replaced by a clock advance, so polling intervals,
backoff and timeouts are the daemon's own.

Simulated day (UTC, 2026-10-18):

  00:00-07:30  parked + asleep, frozen snapshot; API times out 03:00-03:40
  07:35-08:05  drive 1, 25 km (process restart at 07:50)
  08:10-09:40  AC 11 kW (restart 08:40, DB drop mid-cycle 09:00, auth expiry 09:20)
  10:30        one poll returns garbage / string numerics
  12:00-12:20  drive 2, 10 km, straight into a charger
  12:20-12:50  AC 22 kW
  13:25-13:45  drive 3, 15 km (DB drop while saving the trip)
  13:48-14:08  DC fast charge
  14:25-14:55  drive 4, 20 km home, then asleep until midnight

No network: the fake client never leaves the process, geocoding is disabled.
"""
import copy
import json
import logging
import math
import re
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest
from zeekr_ev_api.exceptions import AuthException, ZeekrException

from fordlogger import db
from fordlogger import main as main_module
from fordlogger import poller as poller_module
from fordlogger.poller import Poller
from fordlogger.providers.zeekr import mapper as mapper_module
from fordlogger.providers.zeekr import provider as provider_module
from fordlogger.providers.zeekr.provider import ZeekrProvider
from fordlogger.providers.zeekr.session import ZeekrSession
from fordlogger.providers.zeekr.session_store import SessionStore
from fordlogger.providers.zeekr.settings import SECRET_ENV_VARS, ZeekrSettings

UTC = timezone.utc
VIN = "LZEKR7XAU0000E2E1"
DAY = datetime(2026, 10, 18, tzinfo=UTC)
CAPACITY_KWH = 75.0
KWH_PER_KM = 0.15
REPO = Path(__file__).resolve().parents[2]

SECRETS = {kw: f"value-{kw}" for kw in SECRET_ENV_VARS.values()}

HOME = (-33.80, 151.00)
OFFICE = (-33.87, 151.21)
MALL = (-33.92, 151.18)
DC_SITE = (-33.95, 151.05)


def at(hh, mm=0):
    return DAY + timedelta(hours=hh, minutes=mm)


# (start, end, kind, params)
SEGMENTS = [
    (at(7, 35), at(8, 5), "drive", {"km": 25.0, "frm": HOME, "to": OFFICE}),
    (at(8, 10), at(9, 40), "charge", {"kw": [10.9, 11.0, 11.0, 10.8]}),
    (at(12, 0), at(12, 20), "drive", {"km": 10.0, "frm": OFFICE, "to": MALL}),
    (at(12, 20), at(12, 50), "charge", {"kw": [21.6, 21.9, 22.0, 21.8]}),
    (at(13, 25), at(13, 45), "drive", {"km": 15.0, "frm": MALL, "to": DC_SITE}),
    (at(13, 48), at(14, 8), "charge", {"dc": True}),
    (at(14, 25), at(14, 55), "drive", {"km": 20.0, "frm": DC_SITE, "to": HOME}),
]
EXPECTED_TRIPS = [(at(7, 35), 25.0), (at(12, 0), 10.0), (at(13, 25), 15.0), (at(14, 25), 20.0)]
EXPECTED_CHARGES = [(at(8, 10), "AC", 11.0), (at(12, 20), "AC", 22.0), (at(13, 48), "DC", 180.0)]

TIMEOUT_WINDOW = (at(3, 0), at(3, 40))
GARBAGE_AT = at(10, 30)
AUTH_EXPIRES_AT = at(9, 20)
RESTARTS = [(at(7, 50), timedelta(minutes=2)), (at(8, 40), timedelta(minutes=5))]
DB_DROP_INSERT_AT = at(9, 0)
DB_DROP_TRIP_AFTER = at(13, 40)
WAKE_BEFORE = timedelta(minutes=5)
AWAKE_AFTER = timedelta(minutes=30)


class Clock:
    def __init__(self, now):
        self.now = now


class SimDatetime:
    """Stand-in for fordlogger.poller.datetime whose now() follows the Clock."""

    def __init__(self, clock):
        self._clock = clock

    def now(self, tz=None):
        return self._clock.now if tz is None else self._clock.now.astimezone(tz)

    def __getattr__(self, name):
        return getattr(datetime, name)

    def __instancecheck__(self, obj):
        return isinstance(obj, datetime)


class SimCar:
    """Integrates the car's state over SEGMENTS and renders API payloads."""

    def __init__(self, clock, segments=SEGMENTS):
        self.clock = clock
        self.segments = segments
        self.t = DAY - timedelta(hours=1)
        self.soc = 40.0
        self.odo = 12000.0
        self.pos = HOME
        self.charger_state = "0"
        self.power = 0.0
        self.engine = "engine-off"
        self.update_ms = int((DAY - timedelta(minutes=50)).timestamp() * 1000)
        self.garbage_served = False
        # (start, end, period): the API only refreshes its snapshot every period.
        self.coarse_windows = []
        self._cache = {}

    def _coarse(self, name, render):
        now = self.clock.now
        for start, end, period in self.coarse_windows:
            if start <= now < end:
                slot = start + period * int((now - start) / period)
                key = (name, slot)
                if key not in self._cache:
                    self._cache[key] = render()
                return copy.deepcopy(self._cache[key])
        return render()

    def _segment(self, t):
        for seg in self.segments:
            if seg[0] <= t < seg[1]:
                return seg
        return None

    def awake(self, t):
        return any(s - WAKE_BEFORE <= t < e + AWAKE_AFTER for s, e, _, _ in self.segments)

    def advance(self):
        step = timedelta(seconds=10)
        target = self.clock.now
        while self.t < target:
            t = self.t
            seg = self._segment(t)
            dt_h = step.total_seconds() / 3600
            if seg and seg[2] == "drive":
                s, e, _, p = seg
                frac = (t + step - s) / (e - s)
                km = p["km"] / ((e - s).total_seconds() / 3600) * dt_h
                self.odo += km
                self.soc = max(0.0, self.soc - km * KWH_PER_KM / CAPACITY_KWH * 100)
                (a, b), (c, d) = p["frm"], p["to"]
                self.pos = (a + (c - a) * min(frac, 1), b + (d - b) * min(frac, 1))
                self.engine, self.charger_state, self.power = "engine-running", "0", 0.0
            elif seg and seg[2] == "charge":
                s, e, _, p = seg
                n = int((t - s).total_seconds() // 60)
                if p.get("dc"):
                    self.power = 180.0 if self.soc < 75 else max(30.0, 180.0 - (self.soc - 75) * 6)
                else:
                    self.power = p["kw"][n % len(p["kw"])]
                self.soc = min(100.0, self.soc + self.power * dt_h / CAPACITY_KWH * 100)
                self.engine, self.charger_state = "engine-off", "2"
            else:
                self.engine, self.power = "engine-off", 0.0
                if self.charger_state == "2":
                    self.charger_state = "25"  # charge finished / stopped
            if self.awake(t):
                self.update_ms = int((t - timedelta(seconds=15)).timestamp() * 1000)
            self.t = t + step

    def status(self):
        return self._coarse("vs", self._status)

    def charging_status(self):
        return self._coarse("cs", self._charging_status)

    def _status(self):
        self.advance()
        now = self.clock.now
        vs = {
            "basicVehicleStatus": {
                "usageMode": "1",
                "engineStatus": self.engine,
                "position": {"latitude": f"{self.pos[0]:.6f}", "longitude": f"{self.pos[1]:.6f}"},
            },
            "additionalVehicleStatus": {
                "electricVehicleStatus": {
                    "chargeLevel": str(round(self.soc)),
                    "distanceToEmptyOnBatteryOnly": str(round(self.soc * 4.5)),
                    "chargerState": self.charger_state,
                    "statusOfChargerConnection": "1" if self.charger_state in ("2", "25") else "0",
                },
                "maintenanceStatus": {"odometer": f"{self.odo:.1f}"},
                "climateStatus": {"interiorTemp": "21.5", "updateTime": str(self.update_ms)},
                "drivingSafetyStatus": {"centralLockingStatus": "0" if self.engine == "engine-running" else "1"},
            },
        }
        if not self.garbage_served and now >= GARBAGE_AT:
            self.garbage_served = True
            ev = vs["additionalVehicleStatus"]["electricVehicleStatus"]
            ev.update(chargeLevel="N/A", distanceToEmptyOnBatteryOnly="--", chargerState={"x": 1})
            vs["additionalVehicleStatus"]["maintenanceStatus"]["odometer"] = "NaN"
            vs["additionalVehicleStatus"]["climateStatus"]["updateTime"] = "garbage"
            vs["basicVehicleStatus"]["position"] = {"latitude": "abc", "longitude": ""}
        return vs

    def _charging_status(self):
        self.advance()
        if self.clock.now >= GARBAGE_AT and self.clock.now < GARBAGE_AT + timedelta(minutes=2):
            return {"chargerState": "?", "chargePower": "", "chargeVoltage": "inf"}
        on = self.charger_state == "2"
        return {
            "chargerState": self.charger_state,
            "chargePower": f"{self.power:.1f}",
            "chargeVoltage": "400" if self.power > 22 else ("230" if on else "0"),
            "chargeCurrent": f"{(self.power * 1000 / 400) if self.power > 22 else (self.power * 1000 / 690):.1f}",
        }


class FakeVehicle:
    def __init__(self, vin):
        self.vin = vin
        self.data = {"vin": vin, "name": "7X"}


class Backend:
    """Server side shared by all client instances: tokens and call log."""

    def __init__(self, clock, car):
        self.clock = clock
        self.car = car
        self.token_seq = 0
        self.valid_token = None
        self.logins = 0
        self.auth_errors = 0
        self.timeouts = 0
        self.calls = []

    def issue(self):
        self.token_seq += 1
        self.logins += 1
        self.valid_token = f"tok-{self.token_seq}"
        return self.valid_token


class SimClient:
    def __init__(self, backend):
        self.b = backend
        self.token = None

    def login(self, relogin=False):
        self.token = self.b.issue()

    def load_session(self, data):
        self.token = data.get("bearer_token")

    def export_session(self):
        return {"bearer_token": self.token, "password": "S3cret"} if self.token else {}

    def _check(self, method):
        b = self.b
        b.calls.append((b.clock.now, method))
        now = b.clock.now
        if b.valid_token == "tok-1" and now >= AUTH_EXPIRES_AT:
            b.valid_token = "expired"
        if self.token != b.valid_token:
            b.auth_errors += 1
            raise AuthException("token expired")
        if TIMEOUT_WINDOW[0] <= now < TIMEOUT_WINDOW[1] and method != "get_vehicle_list":
            b.timeouts += 1
            raise ZeekrException("HTTPSConnectionPool: Read timed out. (read timeout=30)")

    def get_vehicle_list(self):
        self._check("get_vehicle_list")
        return [FakeVehicle(VIN)]

    def get_vehicle_status(self, vin):
        self._check("get_vehicle_status")
        return self.b.car.status()

    def get_vehicle_charging_status(self, vin):
        self._check("get_vehicle_charging_status")
        return self.b.car.charging_status()

    def get_vehicle_charging_limit(self, vin):
        self._check("get_vehicle_charging_limit")
        return {"soc": "800"}


def driving_signal_shim(monkeypatch):
    """Test-only explicit driving indicator.

    The real mapper leaves is_driving None (no capture yet) and trips come
    from the core's motion inference. This shim derives is_driving from
    engineStatus (here only) to exercise the explicit-flag path as well.
    """
    real = provider_module.map_position

    def mapped(vin, vs, *args, **kwargs):
        p = real(vin, vs, *args, **kwargs)
        engine = ((vs or {}).get("basicVehicleStatus") or {}).get("engineStatus")
        if engine in ("engine-running", "engine-off"):
            p.is_driving = engine == "engine-running"
        return p

    monkeypatch.setattr(provider_module, "map_position", mapped)


class Simulation:
    def __init__(self, cfg, tmp_path, monkeypatch, admin):
        self.cfg = dict(cfg, geocoding=False)
        self.clock = Clock(DAY)
        self.car = SimCar(self.clock)
        self.backend = Backend(self.clock, self.car)
        self.store_path = tmp_path / "state" / "zeekr_session.json"
        self.admin = admin
        self.monkeypatch = monkeypatch
        self.restarts = list(RESTARTS)
        self.restarts_done = 0
        self.pollers = []
        self.cycles = 0
        self.failed_cycles = 0
        clock = self.clock

        monkeypatch.setattr(poller_module, "datetime", SimDatetime(clock))
        monkeypatch.setattr(poller_module, "time", types.SimpleNamespace(
            time=lambda: clock.now.timestamp(),
            monotonic=lambda: clock.now.timestamp(),
            sleep=lambda s: None,
        ))
        self._install_db_faults()

    def _kill_backend(self, conn):
        with self.admin.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(%s)", (conn.get_backend_pid(),))

    def _install_db_faults(self):
        real_insert_position = db.insert_position
        real_insert_trip = db.insert_trip
        state = {"pos": False, "trip": False}
        self.db_faults = state

        def insert_position(conn, p):
            if not state["pos"] and self.clock.now >= DB_DROP_INSERT_AT:
                state["pos"] = True
                self._kill_backend(conn)
            return real_insert_position(conn, p)

        def insert_trip(conn, t):
            if not state["trip"] and self.clock.now >= DB_DROP_TRIP_AFTER:
                state["trip"] = True
                self._kill_backend(conn)
            return real_insert_trip(conn, t)

        self.monkeypatch.setattr(db, "insert_position", insert_position)
        self.monkeypatch.setattr(db, "insert_trip", insert_trip)

    def new_poller(self):
        settings = ZeekrSettings(**SECRETS)
        session = ZeekrSession(
            settings, SessionStore(self.store_path),
            client_factory=lambda _s, _l: SimClient(self.backend),
            auth_exceptions=(AuthException,), api_exceptions=(ZeekrException,),
        )
        provider = ZeekrProvider(session, clock=lambda: self.clock.now)
        conn = db.connect(self.cfg)
        db.ensure_schema(conn)
        poller = Poller(self.cfg, conn, [provider])
        self.pollers.append(poller)
        return poller

    def run(self, until=DAY + timedelta(hours=24)):
        while self.clock.now < until:
            poller = self.new_poller()
            real_poll = poller.poll_once

            def counted_poll(real_poll=real_poll):
                r = real_poll()
                self.cycles += 1
                self.failed_cycles += r.failed_cycle
                return r

            def sim_sleep(seconds, poller=poller):
                self.clock.now += timedelta(seconds=seconds)
                if self.clock.now >= until:
                    poller.stop()
                    return
                if self.restarts_done < len(self.restarts):
                    when, downtime = self.restarts[self.restarts_done]
                    if self.clock.now >= when:
                        self.restarts_done += 1
                        self.clock.now += downtime
                        poller.stop()

            poller.poll_once = counted_poll
            poller._sleep = sim_sleep
            poller.run_forever()
            try:
                poller.conn.close()
            except Exception:
                pass


def rows(conn, sql, *args):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, args)
        return cur.fetchall()


@pytest.fixture
def admin(cfg, conn):
    c = psycopg2.connect(host=cfg["db_host"], port=cfg["db_port"], dbname="fordlogger",
                         user=cfg["db_user"], password=cfg["db_password"])
    c.autocommit = True
    yield c
    c.close()


def dump(conn, label=""):
    """Human-readable summary of what the day produced (shown with -s)."""
    print(f"\n==== {label}")
    for t in rows(conn, "SELECT * FROM trips ORDER BY start_ts"):
        print("TRIP  ", t["id"], t["start_ts"].strftime("%H:%M:%S"), t["end_ts"].strftime("%H:%M:%S"),
              t["duration_s"], t["distance_km"], t["start_soc_pct"], t["end_soc_pct"], t["soc_used_pct"],
              t["energy_used_kwh"], t["consumption_kwh_per_100km"])
    for c in rows(conn, "SELECT * FROM charge_sessions ORDER BY start_ts"):
        print("CHARGE", c["id"], c["start_ts"].strftime("%H:%M:%S"), c["end_ts"].strftime("%H:%M:%S"),
              c["duration_s"], c["start_soc_pct"], c["end_soc_pct"], c["soc_added_pct"],
              c["energy_added_kwh"], c["max_power_kw"], c["avg_power_kw"], c["charge_type"])
    for s in rows(conn, "SELECT * FROM states ORDER BY ts, id"):
        print("STATE ", s["ts"].strftime("%H:%M:%S"), s["prev_state"], "->", s["state"])
    print("positions:", rows(conn, "SELECT count(*) AS n FROM positions")[0]["n"])


def check_states_coherent(conn):
    states = rows(conn, "SELECT ts, state, prev_state FROM states WHERE vin=%s ORDER BY ts, id", VIN)
    assert states, "no state rows"
    prev = None
    for s in states:
        assert s["state"] != s["prev_state"], s
        if prev is not None:
            assert s["prev_state"] == prev["state"], (prev, s)
            assert s["ts"] >= prev["ts"], (prev, s)
        prev = s
    return states


def check_positions_sane(conn):
    bad = rows(conn, """
        SELECT id, ts, soc_pct, odometer_km, state FROM positions
        WHERE vin=%s AND (state IS NULL OR soc_pct < 0 OR soc_pct > 100 OR odometer_km < 0
                          OR charge_power_kw < 0)""", VIN)
    assert not bad, bad
    dupes = rows(conn, """SELECT snapshot_key, count(*) FROM positions WHERE vin=%s
                          GROUP BY 1 HAVING count(*) > 1""", VIN)
    assert not dupes, dupes


def check_charges(conn):
    charges = rows(conn, "SELECT * FROM charge_sessions WHERE vin=%s ORDER BY start_ts", VIN)
    assert len(charges) == len(EXPECTED_CHARGES), [(c["start_ts"], c["end_ts"]) for c in charges]
    for c, (start, ctype, peak) in zip(charges, EXPECTED_CHARGES):
        # E2E-10: anchored at the last idle reading before the charge, so the
        # start is at most one poll interval (sleeping: 300 s) early.
        assert -330 <= (c["start_ts"] - start).total_seconds() <= 60, (c["start_ts"], start)
        assert c["charge_type"] == ctype, c
        assert c["max_power_kw"] == pytest.approx(peak, abs=0.6), c
        assert 0 < c["duration_s"] <= 2 * 3600, c
        assert c["soc_added_pct"] is not None and c["soc_added_pct"] > 0, c
        assert c["end_soc_pct"] <= 100
    return charges


def check_trips(conn):
    trips = rows(conn, "SELECT * FROM trips WHERE vin=%s ORDER BY start_ts", VIN)
    assert len(trips) == len(EXPECTED_TRIPS), [(t["start_ts"], t["end_ts"], t["distance_km"]) for t in trips]
    for t, (start, km) in zip(trips, EXPECTED_TRIPS):
        # C10 anchors the trip at the last pre-drive reading (up to 15 min early).
        assert -900 <= (t["start_ts"] - start).total_seconds() <= 180, (t["start_ts"], start)
        assert t["distance_km"] == pytest.approx(km, abs=1.5), t
        assert 0 < t["duration_s"] <= 3600, t
        assert t["soc_used_pct"] is None or t["soc_used_pct"] >= 0, t
        assert t["consumption_kwh_per_100km"] is None or t["consumption_kwh_per_100km"] >= 0
    return trips


@pytest.mark.parametrize("signal", ["inferred", "explicit"])
def test_full_day(cfg, conn, tmp_path, monkeypatch, admin, caplog, signal):
    """Full day with every stress event.

    ``inferred``: the unmodified Zeekr mapper (is_driving and speed_kmh are
    always None); trips come only from the core's generic motion inference
    (odometer/position change between fresh readings). ``explicit``: a
    test-only engineStatus -> is_driving shim exercises the explicit path.
    Both must produce the same trips, sessions and states.
    """
    if signal == "explicit":
        driving_signal_shim(monkeypatch)
    else:
        assert provider_module.map_position is mapper_module.map_position
    sim = Simulation(cfg, tmp_path, monkeypatch, admin)
    with caplog.at_level(logging.INFO, logger="fordlogger"):
        sim.run()
    c = sim.pollers[-1].conn
    if c.closed:
        c = db.connect(sim.cfg)

    # Daemon survived everything; faults actually happened.
    assert sim.restarts_done == len(RESTARTS)
    assert sim.db_faults == {"pos": True, "trip": True}
    assert sim.backend.timeouts > 0 and sim.backend.auth_errors >= 1
    assert sim.backend.logins >= 2  # initial + re-login after expiry
    assert "Poll cycle failed:" not in caplog.text  # no unexpected exception escaped poll_once
    assert "Giving up" not in caplog.text

    dump(c, f"full day ({signal} driving signal)")
    print("cycles", sim.cycles, "failed", sim.failed_cycles, "logins", sim.backend.logins,
          "auth_errors", sim.backend.auth_errors, "timeouts", sim.backend.timeouts,
          "pollers", len(sim.pollers))
    check_positions_sane(c)
    trips = check_trips(c)
    charges = check_charges(c)
    states = check_states_coherent(c)
    # Drive straight into a charger: trip 2 ends where AC 22 starts.
    assert trips[1]["end_ts"] <= charges[1]["start_ts"]
    # Ends asleep overnight.
    assert states[-1]["state"] == "sleeping"
    # Garbage poll was stored with numerics dropped, not as zeros.
    g = rows(c, "SELECT soc_pct, odometer_km, lat FROM positions WHERE raw_json->'vehicle_status'"
                "->'additionalVehicleStatus'->'climateStatus'->>'updateTime' = 'garbage'")
    assert g and g[0]["soc_pct"] is None and g[0]["odometer_km"] is None and g[0]["lat"] is None


def test_real_mapper_wakes_for_morning_drive(cfg, conn, tmp_path, monkeypatch, admin):
    sim = Simulation(cfg, tmp_path, monkeypatch, admin)
    sim.run(until=at(8, 6))
    c = db.connect(sim.cfg)
    try:
        moving = rows(c, "SELECT state FROM positions WHERE ts BETWEEN %s AND %s",
                      at(7, 40), at(8, 0))
        assert moving and all(r["state"] != "sleeping" for r in moving), moving
    finally:
        c.close()


def test_22kw_ac_with_realistic_jitter_is_ac(cfg, conn, tmp_path, monkeypatch, admin):
    driving_signal_shim(monkeypatch)
    sim = Simulation(cfg, tmp_path, monkeypatch, admin)
    segs = list(SEGMENTS)
    segs[3] = (segs[3][0], segs[3][1], "charge", {"kw": [21.8, 22.1, 22.2, 21.9]})
    sim.car.segments = segs
    sim.run(until=at(13, 24))
    c = db.connect(sim.cfg)
    try:
        types_ = [r["charge_type"] for r in rows(c, "SELECT charge_type FROM charge_sessions ORDER BY start_ts")]
        assert types_ == ["AC", "AC"], types_
    finally:
        c.close()


@pytest.mark.parametrize("signal", ["inferred", "explicit"])
def test_long_outage_mid_trip_does_not_stretch_trip(cfg, conn, tmp_path, monkeypatch, admin, signal):
    """E2E-4: a gap longer than drive_gap_minutes ends the drive at the last
    reading before the gap instead of stretching it over the outage."""
    if signal == "explicit":
        driving_signal_shim(monkeypatch)
    sim = Simulation(cfg, tmp_path, monkeypatch, admin)
    sim.restarts = [(at(7, 55), timedelta(hours=3, minutes=30))]  # crash, back at 11:25
    sim.db_faults.update(pos=True, trip=True)  # no DB faults in this scenario
    sim.car.garbage_served = True
    sim.run(until=at(11, 55))
    c = db.connect(sim.cfg)
    try:
        dump(c, "long outage mid-trip")
        trips = rows(c, "SELECT * FROM trips ORDER BY start_ts")
        assert len(trips) == 1
        assert trips[0]["duration_s"] <= 45 * 60, trips[0]
    finally:
        c.close()


@pytest.mark.parametrize("signal", ["inferred", "explicit"])
def test_lost_state_row_does_not_duplicate_trip(cfg, conn, tmp_path, monkeypatch, admin, signal):
    """E2E-5: the position and its state row are written in one transaction;
    a failed state insert re-processes the reading instead of losing the row."""
    if signal == "explicit":
        driving_signal_shim(monkeypatch)
    sim = Simulation(cfg, tmp_path, monkeypatch, admin)
    sim.db_faults.update(pos=True, trip=True)
    sim.restarts = [(at(8, 8), timedelta(minutes=1))]  # after drive end, before 08:10 charge
    real_insert_state = db.insert_state
    failed = []

    def insert_state(c, vin, ts, state, prev_state=None):
        if not failed and prev_state == "driving":
            failed.append(ts)
            raise psycopg2.OperationalError("server closed the connection unexpectedly")
        return real_insert_state(c, vin, ts, state, prev_state)

    monkeypatch.setattr(db, "insert_state", insert_state)
    sim.run(until=at(9, 0))
    c = db.connect(sim.cfg)
    try:
        dump(c, "lost state row + restart")
        assert failed
        trips = rows(c, "SELECT start_ts, end_ts FROM trips ORDER BY start_ts")
        assert len(trips) == 1, trips
    finally:
        c.close()


@pytest.mark.parametrize("signal", ["inferred", "explicit"])
def test_coarse_snapshots_do_not_split_charge(cfg, conn, tmp_path, monkeypatch, admin, signal):
    """E2E-7: while the last reading still showed charging, a slow snapshot
    refresh (up to charge_gap_minutes) does not end the session."""
    if signal == "explicit":
        driving_signal_shim(monkeypatch)
    sim = Simulation(cfg, tmp_path, monkeypatch, admin)
    sim.db_faults.update(pos=True, trip=True)
    sim.restarts = []
    sim.car.coarse_windows = [(at(8, 10), at(9, 40), timedelta(minutes=20))]
    sim.run(until=at(10, 0))
    c = db.connect(sim.cfg)
    try:
        dump(c, "coarse snapshots while charging")
        sessions = rows(c, "SELECT * FROM charge_sessions")
        assert len(sessions) == 1, sessions
        assert sessions[0]["duration_s"] >= 80 * 60 and sessions[0]["soc_added_pct"] >= 20, sessions
        flaps = rows(c, "SELECT count(*) AS n FROM states WHERE state = 'charging'")[0]["n"]
        assert flaps == 1, flaps
    finally:
        c.close()


# --------------------------------------------------------------- --once codes

def _run_once(monkeypatch, tmp_path, cfg, env, backend=None):
    config = {k: cfg[k] for k in ("db_host", "db_port", "db_name", "db_user", "db_password")}
    config["providers"] = [{"type": "zeekr"}]
    config["geocoding"] = False
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FORDLOGGER_STATE_DIR", str(tmp_path / "state"))
    if backend is not None:
        from fordlogger.providers.zeekr import session as session_module
        monkeypatch.setattr(session_module, "default_client_factory",
                            lambda _s, _l: SimClient(backend))
        monkeypatch.setattr(session_module, "_library_exceptions",
                            lambda: (AuthException, ZeekrException))
    monkeypatch.setattr("sys.argv", ["fordlogger", "--once", "--config", str(path)])
    try:
        main_module.main()
    except SystemExit as e:
        return e.code or 0
    return 0


def test_once_exit_codes(cfg, conn, tmp_path, monkeypatch):
    env = {name: f"v-{name}" for name in SECRET_ENV_VARS}
    clock = Clock(at(10, 0))
    car = SimCar(clock)
    backend = Backend(clock, car)
    monkeypatch.setattr(poller_module, "datetime", SimDatetime(clock))

    # missing ZEEKR_* variables -> 1
    assert _run_once(monkeypatch, tmp_path, cfg, {}) == 1
    # first poll stores a position -> 0
    assert _run_once(monkeypatch, tmp_path, cfg, env, backend) == 0
    # same frozen snapshot again (car asleep) -> 1, documented duplicate case
    assert _run_once(monkeypatch, tmp_path, cfg, env, backend) == 1
    # new snapshot -> 0
    clock.now = at(12, 5)
    assert _run_once(monkeypatch, tmp_path, cfg, env, backend) == 0
    # API timing out -> 1
    clock.now = at(3, 10) + timedelta(days=1)
    car.segments = [(s + timedelta(days=1), e + timedelta(days=1), k, p) for s, e, k, p in SEGMENTS]
    monkeypatch.setitem(globals(), "TIMEOUT_WINDOW",
                        (at(3, 0) + timedelta(days=1), at(3, 40) + timedelta(days=1)))
    assert _run_once(monkeypatch, tmp_path, cfg, env, backend) == 1
    # missing config file -> 1
    monkeypatch.setattr("sys.argv", ["fordlogger", "--once", "--config", str(tmp_path / "nope.json")])
    with pytest.raises(SystemExit) as e:
        main_module.main()
    assert e.value.code == 1


def test_once_db_unreachable_exits_nonzero(cfg, tmp_path, monkeypatch):
    env = {name: f"v-{name}" for name in SECRET_ENV_VARS}
    bad = dict(cfg, db_port=1)  # nothing listens there
    monkeypatch.delenv("FORDLOGGER_DB_PORT", raising=False)
    code = None
    try:
        code = _run_once(monkeypatch, tmp_path, bad, env)
    except psycopg2.OperationalError:
        code = "traceback"
    # Documented: non-zero. Observed: uncaught OperationalError traceback (exit 1).
    assert code in (1, "traceback")


# ------------------------------------------------------------ Grafana panels

def grafana_queries():
    out = []
    for f in sorted((REPO / "grafana" / "dashboards").glob("*.json")):
        d = json.loads(f.read_text())

        def walk(o, title=None):
            if isinstance(o, dict):
                title = o.get("title", title)
                if isinstance(o.get("rawSql"), str):
                    out.append((f.stem, title, o["rawSql"]))
                for v in o.values():
                    walk(v, title)
            elif isinstance(o, list):
                for v in o:
                    walk(v, title)
        walk(d)
        for v in d.get("templating", {}).get("list", []):
            if v.get("type") == "query" and isinstance(v.get("query"), str):
                out.append((f.stem, f"var ${v['name']}", v["query"]))
    return out


def substitute(sql, frm, to, trip_id=0):
    def tf(m):
        return f"({m.group(1)} BETWEEN '{frm.isoformat()}' AND '{to.isoformat()}')"
    sql = re.sub(r"\$__timeFilter\(([^)]+)\)", tf, sql)
    sql = sql.replace("$__timeFrom()", f"'{frm.isoformat()}'").replace("$__timeTo()", f"'{to.isoformat()}'")
    sql = sql.replace("${__timezone}", "Australia/Sydney")
    sql = sql.replace("${rated_capacity_kwh}", "").replace("${rated_range_km}", "")
    sql = sql.replace("$vin", VIN).replace("$trip", str(trip_id))
    return sql


def test_grafana_panels_run_on_simulated_day(cfg, conn, tmp_path, monkeypatch, admin):
    driving_signal_shim(monkeypatch)
    sim = Simulation(cfg, tmp_path, monkeypatch, admin)
    sim.run()
    c = db.connect(sim.cfg)
    frm, to = DAY - timedelta(days=1), DAY + timedelta(days=2)
    trip_id = rows(c, "SELECT min(id) AS id FROM trips")[0]["id"] or 0
    errors = []
    for dash, title, sql in grafana_queries():
        for tid in ((0, trip_id) if "$trip" in sql else (0,)):
            try:
                with c.cursor() as cur:
                    cur.execute(substitute(sql, frm, to, tid))
                    if cur.description:
                        cur.fetchall()
            except Exception as e:
                errors.append(f"{dash} / {title}: {type(e).__name__}: {str(e).splitlines()[0]}")
    c.close()
    assert not errors, "\n".join(errors)


def _panel(dash, title):
    return next(sql for d, t, sql in grafana_queries() if d == dash and t == title)


@pytest.mark.xfail(strict=True, reason="BUG E2E-8: Overview 'Current State' / 'Plug Status' read the "
                   "latest positions row: a tick-driven change (->sleeping, stale drive end) writes "
                   "no position, and Zeekr plug code '1' is not recognised as plugged")
def test_overview_current_state_and_plug(cfg, conn, tmp_path, monkeypatch, admin):
    driving_signal_shim(monkeypatch)
    sim = Simulation(cfg, tmp_path, monkeypatch, admin)
    sim.db_faults.update(pos=True, trip=True)
    sim.restarts = []
    sim.run(until=at(10, 5))  # AC charge finished 09:40; still plugged in
    c = db.connect(sim.cfg)
    frm, to = DAY - timedelta(days=1), DAY + timedelta(days=2)
    try:
        latest = rows(c, "SELECT plug_status, state FROM positions ORDER BY ts DESC LIMIT 1")[0]
        assert latest["plug_status"] == "1"  # plugged, charge complete
        with c.cursor() as cur:
            cur.execute(substitute(_panel("overview", "Plug Status"), frm, to))
            plug = cur.fetchone()[1]
        sim2 = Simulation(cfg, tmp_path, monkeypatch, admin)  # continue: let it fall asleep
        sim2.clock.now = at(10, 5)
        sim2.car = sim.car
        sim2.car.clock = sim2.clock
        sim2.backend = sim.backend
        sim2.backend.clock = sim2.clock
        sim2.db_faults.update(pos=True, trip=True)
        sim2.restarts = []
        sim2.run(until=at(11, 0))
        with c.cursor() as cur:
            cur.execute("SELECT state FROM states ORDER BY ts DESC, id DESC LIMIT 1")
            actual = cur.fetchone()[0]
            cur.execute(substitute(_panel("overview", "Current State"), frm, to))
            shown = cur.fetchone()[1]
        codes = {"parked": 0, "driving": 1, "charging": 2, "sleeping": 3}
        assert (plug, shown) == (1, codes[actual]), (plug, shown, actual)
    finally:
        c.close()
