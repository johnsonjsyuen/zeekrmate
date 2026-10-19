import copy
import json
import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from zeekrmate.models import Position
from zeekrmate.providers.zeekr.mapper import map_position, ms_epoch_to_utc, to_float

from .conftest import CHARGING_LIMIT, CHARGING_STATUS, UPDATE_MS, VEHICLE_STATUS, VIN_A

NOW = datetime(2025, 10, 9, 9, 0, tzinfo=timezone.utc)


def test_ms_epoch_conversion_is_utc_aware():
    dt = ms_epoch_to_utc(UPDATE_MS, now=NOW)
    assert dt == datetime.fromtimestamp(UPDATE_MS / 1000, tz=timezone.utc)
    assert dt == datetime(2025, 10, 9, 8, 53, 20, tzinfo=timezone.utc)
    assert dt.tzinfo is timezone.utc
    assert ms_epoch_to_utc(str(UPDATE_MS), now=NOW) == dt
    assert ms_epoch_to_utc("1760000000123", now=NOW).microsecond == 123000


@pytest.mark.parametrize("bad", [None, "", "abc", 0, -5, 1760000000, True, [], {}, float("nan")])
def test_ms_epoch_rejects_implausible(bad):
    assert ms_epoch_to_utc(bad, now=NOW) is None


def test_future_timestamp_falls_back_to_observed():
    future_ms = int((NOW + timedelta(hours=10)).timestamp() * 1000)
    vs = copy.deepcopy(VEHICLE_STATUS)
    vs["additionalVehicleStatus"]["climateStatus"]["updateTime"] = future_ms
    p = map_position(VIN_A, vs, observed_at=NOW)
    assert p.ts == NOW


def test_string_numerics_coerced():
    p = map_position(VIN_A, VEHICLE_STATUS, CHARGING_STATUS, CHARGING_LIMIT, observed_at=NOW)
    assert isinstance(p.soc_pct, float) and p.soc_pct == 78.0
    assert p.ts == datetime.fromtimestamp(UPDATE_MS / 1000, tz=timezone.utc)
    assert p.observed_at == NOW


@pytest.mark.parametrize("state,expected", [("1", True), ("2", True), (15, True), ("25", False),
                                            ("26", False), ("0", False), ("x", None), (None, None)])
def test_is_charging_from_charger_state(state, expected):
    vs = copy.deepcopy(VEHICLE_STATUS)
    vs["additionalVehicleStatus"]["electricVehicleStatus"]["chargerState"] = state
    p = map_position(VIN_A, vs, {}, observed_at=NOW)
    assert p.is_charging is expected


def test_charger_state_falls_back_to_charging_endpoint():
    vs = copy.deepcopy(VEHICLE_STATUS)
    del vs["additionalVehicleStatus"]["electricVehicleStatus"]["chargerState"]
    p = map_position(VIN_A, vs, {"chargerState": "25"}, observed_at=NOW)
    assert p.charge_status == "25" and p.is_charging is False


def test_charging_fields_fall_back_to_merged_block():
    vs = copy.deepcopy(VEHICLE_STATUS)
    vs["chargingStatus"] = {"chargePower": "11.0"}
    assert map_position(VIN_A, vs, None, observed_at=NOW).charge_power_kw == 11.0


def test_zero_or_out_of_range_coordinates_are_none():
    for lat, lon in [("0", "0"), ("95", "10"), ("", "151"), ("abc", "151")]:
        vs = copy.deepcopy(VEHICLE_STATUS)
        vs["basicVehicleStatus"]["position"] = {"latitude": lat, "longitude": lon}
        p = map_position(VIN_A, vs, observed_at=NOW)
        assert p.lat is None and p.lon is None


def test_is_driving_never_guessed():
    vs = copy.deepcopy(VEHICLE_STATUS)
    vs["basicVehicleStatus"]["engineStatus"] = "engine-running"
    assert map_position(VIN_A, vs, observed_at=NOW).is_driving is None


def test_to_float():
    assert to_float(" 7.5 ") == 7.5
    assert to_float(3) == 3.0
    for bad in (None, "", "nan", "inf", True, [], {}, object()):
        assert to_float(bad) is None


def _garbage(rng, depth=0):
    choices = [None, "", "abc", "12", -1, 0, 1e309, float("nan"), True, [], {}, object(),
               "\x00", 10**40, "1e999", b"bytes", set(), ("t",)]
    if depth < 4:
        choices += ["dict", "list"]
    c = rng.choice(choices)
    if c == "dict":
        keys = ["basicVehicleStatus", "additionalVehicleStatus", "electricVehicleStatus",
                "position", "latitude", "longitude", "chargeLevel", "climateStatus", "updateTime",
                "maintenanceStatus", "odometer", "chargerState", "chargePower", "chargingStatus",
                "drivingSafetyStatus", "centralLockingStatus", 1, None]
        return {rng.choice(keys): _garbage(rng, depth + 1) for _ in range(rng.randint(0, 6))}
    if c == "list":
        return [_garbage(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return c


def test_mapper_is_total_on_garbage():
    rng = random.Random(1234)
    for _ in range(2000):
        p = map_position(_garbage(rng), _garbage(rng), _garbage(rng), _garbage(rng),
                         observed_at=rng.choice([NOW, None]))
        assert isinstance(p, Position)
        assert p.ts.tzinfo is not None
        json.dumps(p.raw_json, allow_nan=False)
        for f in ("soc_pct", "range_km", "odometer_km", "lat", "lon", "charge_power_kw"):
            v = getattr(p, f)
            assert v is None or (isinstance(v, float) and math.isfinite(v))


def test_mapper_survives_cyclic_payload():
    a = {}
    a["self"] = a
    p = map_position(VIN_A, a, a, a, observed_at=NOW)
    assert isinstance(p, Position)


@pytest.mark.parametrize("bad", ["garbage", None, ""])
def test_unparsable_update_time_uses_fallback_ts(bad):
    """E2E-11: not the poll time, which would hide later valid snapshots."""
    vs = copy.deepcopy(VEHICLE_STATUS)
    vs["additionalVehicleStatus"]["climateStatus"]["updateTime"] = bad
    last = NOW - timedelta(minutes=40)
    assert map_position(VIN_A, vs, observed_at=NOW, fallback_ts=last).ts == last
    assert map_position(VIN_A, vs, observed_at=NOW).ts == NOW
    valid = map_position(VIN_A, VEHICLE_STATUS, observed_at=NOW, fallback_ts=last)
    assert valid.ts == datetime.fromtimestamp(UPDATE_MS / 1000, tz=timezone.utc)
