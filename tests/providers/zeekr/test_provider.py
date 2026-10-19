import copy
import json
import logging
from datetime import datetime, timezone

import pytest
from zeekr_ev_api.exceptions import ZeekrException

from fordlogger.models import Position, Vehicle
from fordlogger.providers.zeekr.session import (
    READ_ONLY_METHODS,
    ReadOnlyClient,
    ReadOnlyViolation,
    ZeekrAuthError,
    ZeekrProviderError,
)

from .conftest import (
    AUTH_TOKEN,
    CHARGING_LIMIT,
    CHARGING_STATUS,
    SECRETS,
    TOKEN,
    UPDATE_MS,
    VEHICLE_STATUS,
    VIN_A,
    VIN_B,
    FakeClient,
)


def test_name_and_capabilities(make_provider):
    provider, _, _ = make_provider()
    assert provider.name == "zeekr"
    caps = provider.capabilities()
    assert caps.journey_history is True
    assert caps.trip_trackpoints is True


def test_get_vehicles_maps_list(make_provider):
    provider, _, _ = make_provider(vins=(VIN_A, VIN_B))
    vehicles = provider.get_vehicles()
    assert [v.vin for v in vehicles] == [VIN_A, VIN_B]
    assert all(isinstance(v, Vehicle) and v.make == "Zeekr" for v in vehicles)
    # "name" in the list payload is not evidence of a model
    assert all(v.model is None for v in vehicles)


def test_get_telemetry_builds_one_position_per_vin(make_provider):
    provider, _, _ = make_provider(vins=(VIN_A, VIN_B))
    positions = provider.get_telemetry()
    assert [p.vin for p in positions] == [VIN_A, VIN_B]
    p = positions[0]
    assert isinstance(p, Position)
    assert p.ts.tzinfo is not None and p.ts.utcoffset().total_seconds() == 0
    assert p.ts == datetime.fromtimestamp(UPDATE_MS / 1000, tz=timezone.utc)
    assert p.observed_at.tzinfo is not None
    assert p.soc_pct == 78.0
    assert p.range_km == 412.0
    assert p.odometer_km == 12345.6
    assert (p.lat, p.lon) == (-33.8688, 151.2093)
    assert p.charge_power_kw == 7.2
    assert p.charger_voltage == 240.0
    assert p.charger_current_a == 30.0
    assert p.charge_status == "2"
    assert p.plug_status == "1"
    assert p.is_charging is True
    assert p.is_driving is None
    assert p.door_lock_status == "LOCKED"
    assert p.raw_json == {
        "vehicle_status": VEHICLE_STATUS,
        "charging_status": CHARGING_STATUS,
        "charging_limit": CHARGING_LIMIT,
    }
    json.dumps(p.raw_json, allow_nan=False)
    assert len(p.snapshot_key) == 64


def test_read_calls_only(make_provider):
    provider, _, _ = make_provider()
    provider.get_vehicles()
    provider.get_telemetry()
    client = FakeClient.instances[-1]
    methods = {c[0] for c in client.calls}
    assert methods <= READ_ONLY_METHODS
    assert {"get_vehicle_status", "get_vehicle_charging_status",
            "get_vehicle_charging_limit"} <= methods


def test_missing_fields_are_none(make_provider):
    provider, _, _ = make_provider(statuses={VIN_A: {}})
    FakeClient.instances.clear()
    p = provider.get_telemetry()[0]
    for field in ("soc_pct", "range_km", "odometer_km", "lat", "lon", "plug_status",
                  "door_lock_status", "speed_kmh", "outside_temp_c", "bat_temp_c",
                  "tire_pressure_fl", "heading"):
        assert getattr(p, field) is None, field
    # charging data still comes from the charging endpoint
    assert p.charge_power_kw == 7.2
    # no payload timestamp -> observed time
    assert p.ts == p.observed_at
    assert p.raw_json["vehicle_status"] == {}


def test_one_vin_failing_does_not_drop_others(make_provider, caplog):
    body = ZeekrException('Failed to get vehicle status: {"token": "%s"}' % TOKEN)
    provider, _, _ = make_provider(vins=(VIN_A, VIN_B), fail={("get_vehicle_status", VIN_A): body})
    with caplog.at_level(logging.DEBUG):
        positions = provider.get_telemetry()
    assert [p.vin for p in positions] == [VIN_B]
    assert "***0001" in caplog.text
    assert TOKEN not in caplog.text and "eyJ" not in caplog.text
    assert VIN_A not in caplog.text


def test_secondary_endpoint_failure_keeps_position(make_provider):
    provider, _, _ = make_provider(fail={("get_vehicle_charging_status", VIN_A): ZeekrException("boom: {}")})
    (p,) = provider.get_telemetry()
    assert p.raw_json["charging_status"] is None
    assert p.soc_pct == 78.0
    assert p.charge_power_kw is None


def test_all_vins_failing_raises_sanitised(make_provider):
    exc = ZeekrException('Failed to get vehicle status: {"bearer": "%s"}' % TOKEN)
    provider, _, _ = make_provider(fail={("get_vehicle_status", VIN_A): exc})
    with pytest.raises(ZeekrProviderError) as info:
        provider.get_telemetry()
    assert TOKEN not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None or info.value.__suppress_context__


def test_snapshot_key_changes_with_payload_and_is_stable(make_provider):
    provider, _, _ = make_provider()
    k1 = provider.get_telemetry()[0].snapshot_key
    k2 = provider.get_telemetry()[0].snapshot_key
    assert k1 == k2
    changed = copy.deepcopy(VEHICLE_STATUS)
    changed["additionalVehicleStatus"]["electricVehicleStatus"]["chargeLevel"] = "79"
    provider2, _, _ = make_provider(statuses={VIN_A: changed})
    assert provider2.get_telemetry()[0].snapshot_key != k1


# --- read-only guarantee --------------------------------------------------

def test_provider_never_calls_write_methods(make_provider):
    """FakeClient raises on do_remote_control/set_charge_plan/set_travel_plan."""
    provider, session, _ = make_provider(vins=(VIN_A, VIN_B))
    provider.get_vehicles()
    for _ in range(3):
        provider.get_telemetry()
    for client in FakeClient.instances:
        assert not any(c[0] in {"do_remote_control", "set_charge_plan", "set_travel_plan"}
                       for c in client.calls)


@pytest.mark.parametrize("method", ["do_remote_control", "set_charge_plan", "set_travel_plan",
                                    "get_remote_control_state", "session"])
def test_read_only_proxy_blocks_writes(method, make_provider):
    _, session, _ = make_provider()
    client = session.connect()
    assert isinstance(client, ReadOnlyClient)
    with pytest.raises(ReadOnlyViolation):
        getattr(client, method)
    with pytest.raises(ReadOnlyViolation):
        session.call(method, VIN_A)


# --- auth -----------------------------------------------------------------

def test_auth_error_triggers_one_fresh_login_and_retry(make_provider):
    budget = [1]
    provider, _, store = make_provider(auth_fail_times=budget)
    (p,) = provider.get_telemetry()
    assert p.vin == VIN_A
    logins = sum(1 for c in FakeClient.instances for x in c.calls if x[0] == "login")
    assert logins == 2  # initial login + one fresh login after AuthException
    assert store.load()["bearer_token"] == TOKEN


def test_persistent_auth_error_raises_after_single_retry(make_provider):
    budget = [100]
    provider, _, _ = make_provider(auth_fail_times=budget)
    with pytest.raises(ZeekrAuthError):
        provider.get_telemetry()
    logins = sum(1 for c in FakeClient.instances for x in c.calls if x[0] == "login")
    assert logins == 2


def test_login_failure_is_sanitised(make_provider):
    provider, _, _ = make_provider(login_fails=True)
    with pytest.raises(ZeekrAuthError) as info:
        provider.get_vehicles()
    msg = str(info.value)
    assert TOKEN not in msg and "eyJ" not in msg
    assert "ZEEKR_USERNAME" in msg
    for value in SECRETS.values():
        assert value not in msg


def test_stored_session_is_reused_without_login(make_provider):
    provider, _, store = make_provider()
    provider.get_telemetry()
    assert store.path.exists()
    FakeClient.instances.clear()
    provider2, _, _ = make_provider()
    provider2.get_telemetry()
    calls = [c[0] for c in FakeClient.instances[0].calls]
    assert "load_session" in calls and "login" not in calls


def test_build_provider_makes_no_calls(tmp_path):
    from fordlogger.providers.zeekr.provider import build_provider

    env = {
        "ZEEKR_USERNAME": "u", "ZEEKR_PASSWORD": "p", "ZEEKR_HMAC_ACCESS_KEY": "a",
        "ZEEKR_HMAC_SECRET_KEY": "b", "ZEEKR_PASSWORD_PUBLIC_KEY": "c",
        "ZEEKR_PROD_SECRET": "d", "ZEEKR_VIN_KEY": "e", "ZEEKR_VIN_IV": "f",
        "FORDLOGGER_STATE_DIR": str(tmp_path),
    }
    built = []

    def factory(settings, logger):
        built.append(settings)
        return FakeClient(logger=logger)

    provider = build_provider({}, environ=env, client_factory=factory)
    assert provider.name == "zeekr" and built == []
    provider.get_vehicles()
    assert built and built[0].country_code == "AU"
    assert (tmp_path / "zeekr_session.json").exists()
