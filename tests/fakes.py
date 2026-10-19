"""Test doubles shared across the suite."""
from fordlogger.providers.base import VehicleProvider


class FakeProvider(VehicleProvider):
    """In-memory VehicleProvider with injectable errors and call counters."""

    def __init__(
        self,
        *,
        name="fake",
        vehicles=None,
        positions=None,
        vehicle_error=None,
        telemetry_error=None,
    ):
        self._name = name
        self.vehicles = list(vehicles or [])
        self.positions = list(positions or [])
        self.vehicle_error = vehicle_error
        self.telemetry_error = telemetry_error
        self.vehicle_calls = 0
        self.telemetry_calls = 0

    @property
    def name(self):
        return self._name

    def get_vehicles(self):
        self.vehicle_calls += 1
        if self.vehicle_error:
            raise self.vehicle_error
        return self.vehicles

    def get_telemetry(self):
        self.telemetry_calls += 1
        if self.telemetry_error:
            raise self.telemetry_error
        return self.positions


class BrokenNameProvider(FakeProvider):
    @property
    def name(self):
        raise RuntimeError("provider name unavailable")
