"""Audit regressions: generic-core hazards that survived Ford removal.

Regression tests for G1-G5 (originally strict xfails on audit/ford
33a0b6c), using a fake VehicleProvider (no network).
"""
from datetime import datetime, timezone

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
def test_telemetry_for_unregistered_vin_does_not_crash_poll(
    conn, cfg, vehicle_error, vehicles
):
    provider = _AuditProvider(
        vehicles=vehicles,
        vehicle_error=vehicle_error,
        positions=[Position(ts=datetime.now(timezone.utc), vin=VIN, soc_pct=50.0)],
    )

    Poller(cfg, conn, provider).poll_once()


def test_failed_vehicle_list_is_retried_next_cycle(conn, cfg):
    provider = _AuditProvider(vehicle_error=RuntimeError("vehicle list down"))
    poller = Poller(cfg, conn, provider)

    poller.poll_once()
    provider.vehicle_error = None
    poller.poll_once()

    assert provider.vehicle_calls == 2


def test_naive_position_timestamp_does_not_crash_poll(conn, cfg):
    db.upsert_vehicle(conn, Vehicle(vin=VIN))
    provider = _AuditProvider(
        positions=[Position(ts=datetime(2026, 10, 19, 12, 0, 0), vin=VIN)]
    )

    Poller(cfg, conn, provider).poll_once()


def test_string_typed_numeric_does_not_crash_poll(conn, cfg):
    db.upsert_vehicle(conn, Vehicle(vin=VIN))
    provider = _AuditProvider(
        positions=[
            Position(ts=datetime.now(timezone.utc), vin=VIN, speed_kmh="12.5")
        ]
    )

    Poller(cfg, conn, provider).poll_once()


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
