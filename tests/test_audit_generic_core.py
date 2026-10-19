"""Audit repro tests: generic-core hazards that survive Ford removal.

Each test pins a CONFIRMED defect in provider-neutral code (poller /
state machine) as a strict xfail, using a fake VehicleProvider (no Ford
code, no network). Today each test fails with the named exception, which
proves the defect while keeping the suite green. Once a fix lands the test
XPASSes, strict mode turns that into a failure, and the marker should be
removed. Run with ``--runxfail`` to see the raw failures.
"""
from datetime import datetime, timezone

import psycopg2.errors
import pytest

from fordlogger import db
from fordlogger.models import Position, Vehicle
from fordlogger.poller import Poller
from fordlogger.providers.base import VehicleProvider

VIN = "AUDITVIN000000001"


class _AuditProvider(VehicleProvider):
    name = "audit-fake"

    def __init__(self, *, vehicles=None, positions=None, vehicle_error=None):
        self.vehicles = list(vehicles or [])
        self.positions = list(positions or [])
        self.vehicle_error = vehicle_error
        self.vehicle_calls = 0

    def get_vehicles(self):
        self.vehicle_calls += 1
        if self.vehicle_error:
            raise self.vehicle_error
        return self.vehicles

    def get_telemetry(self):
        return self.positions


class _StopLoop(BaseException):
    """Escapes run_forever without being swallowed by `except Exception`."""


@pytest.mark.parametrize(
    "vehicle_error, vehicles",
    [
        (RuntimeError("vehicle list unavailable"), None),
        (None, []),
    ],
    ids=["vehicle-list-raises", "vehicle-list-empty"],
)
@pytest.mark.xfail(
    strict=True,
    raises=psycopg2.errors.ForeignKeyViolation,
    reason=(
        "AUDIT-G1: poller.py:98-99 processes positions outside any try; "
        "db.insert_position hits the positions.vin FK (schema.sql:18) when the "
        "vehicle row was never upserted, and the exception escapes poll_once "
        "and run_forever (poller.py:167-171), killing the daemon."
    ),
)
def test_telemetry_for_unregistered_vin_does_not_crash_poll(
    conn, cfg, vehicle_error, vehicles
):
    provider = _AuditProvider(
        vehicles=vehicles,
        vehicle_error=vehicle_error,
        positions=[Position(ts=datetime.now(timezone.utc), vin=VIN, soc_pct=50.0)],
    )

    Poller(cfg, conn, provider).poll_once()


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "AUDIT-G2: poller.py:101 advances _last_garage_update even when "
        "get_vehicles() failed, so the vehicle list is not retried for "
        "garage_interval_s (3600 s). Upstream b25a9bf moved this assignment "
        "out of the success path; PR1 preserved it."
    ),
)
def test_failed_vehicle_list_is_retried_next_cycle(conn, cfg):
    provider = _AuditProvider(vehicle_error=RuntimeError("vehicle list down"))
    poller = Poller(cfg, conn, provider)

    poller.poll_once()
    provider.vehicle_error = None
    poller.poll_once()

    assert provider.vehicle_calls == 2


@pytest.mark.xfail(
    strict=True,
    raises=TypeError,
    reason=(
        "AUDIT-G3: state_machine.py:54 subtracts pos.ts from an aware "
        "datetime (state_machine.py:26); a provider returning a naive "
        "Position.ts raises TypeError, which escapes poll_once."
    ),
)
def test_naive_position_timestamp_does_not_crash_poll(conn, cfg):
    db.upsert_vehicle(conn, Vehicle(vin=VIN))
    provider = _AuditProvider(
        positions=[Position(ts=datetime(2026, 10, 19, 12, 0, 0), vin=VIN)]
    )

    Poller(cfg, conn, provider).poll_once()


@pytest.mark.xfail(
    strict=True,
    raises=TypeError,
    reason=(
        "AUDIT-G5: Position has no runtime type checks; a provider passing a "
        "numeric string (zeekr_ev_api payloads carry numbers as strings) "
        "reaches `speed > 1.0` at state_machine.py:35-36 and the TypeError "
        "escapes poll_once."
    ),
)
def test_string_typed_numeric_does_not_crash_poll(conn, cfg):
    db.upsert_vehicle(conn, Vehicle(vin=VIN))
    provider = _AuditProvider(
        positions=[
            Position(ts=datetime.now(timezone.utc), vin=VIN, speed_kmh="12.5")
        ]
    )

    Poller(cfg, conn, provider).poll_once()


@pytest.mark.xfail(
    strict=True,
    raises=RuntimeError,
    reason=(
        "AUDIT-G4: run_forever (poller.py:167-171) has no exception guard; "
        "one failed cycle terminates the daemon (Docker restart policy then "
        "turns it into a restart / re-login loop)."
    ),
)
def test_run_forever_survives_a_failed_cycle(conn, cfg, monkeypatch):
    poller = Poller(cfg, conn, _AuditProvider())

    def failing_cycle():
        raise RuntimeError("cycle failed")

    def stop_loop(_seconds):
        raise _StopLoop

    monkeypatch.setattr(poller, "poll_once", failing_cycle)
    monkeypatch.setattr("fordlogger.poller.time.sleep", stop_loop)

    with pytest.raises(_StopLoop):
        poller.run_forever()
