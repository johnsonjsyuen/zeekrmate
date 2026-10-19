import importlib.util
import json
import stat
from pathlib import Path

import pytest

from .conftest import AUTH_TOKEN, SECRETS, TOKEN, VIN_A, VIN_B, FakeClient

ROOT = Path(__file__).resolve().parents[3]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dump = _load("dump_zeekr")
sanitise = _load("sanitise_zeekr_capture")

ENV_NAMES = ("ZEEKR_USERNAME", "ZEEKR_PASSWORD", "ZEEKR_HMAC_ACCESS_KEY", "ZEEKR_HMAC_SECRET_KEY",
             "ZEEKR_PASSWORD_PUBLIC_KEY", "ZEEKR_PROD_SECRET", "ZEEKR_VIN_KEY", "ZEEKR_VIN_IV")


@pytest.fixture
def env(tmp_path):
    keys = ("username", "password", "hmac_access_key", "hmac_secret_key", "password_public_key",
            "prod_secret", "vin_key", "vin_iv")
    e = {name: SECRETS[k] for name, k in zip(ENV_NAMES, keys)}
    e["ZEEKRMATE_STATE_DIR"] = str(tmp_path / "state")
    return e


def _run(argv, env, **client_kwargs):
    FakeClient.instances.clear()
    lines = []

    def factory(settings, logger):
        return FakeClient(logger=logger, **client_kwargs)

    code = dump.main(argv, environ=env, client_factory=factory, out=lines.append)
    return code, "\n".join(lines)


def _assert_no_secrets(text):
    assert TOKEN not in text and AUTH_TOKEN not in text and "eyJ" not in text
    for value in SECRETS.values():
        assert value not in text


def test_check_ok_masks_vins(env):
    code, text = _run(["--check"], env, vins=(VIN_A, VIN_B))
    assert code == 0
    assert "OK" in text and "***0001" in text and "***0002" in text
    assert VIN_A not in text
    _assert_no_secrets(text)
    calls = {c[0] for c in FakeClient.instances[0].calls}
    assert calls == {"login", "get_vehicle_list"}


def test_check_missing_env_lists_names(env):
    del env["ZEEKR_VIN_IV"]
    code, text = _run(["--check"], env)
    assert code == dump.EXIT_CONFIG
    assert "ZEEKR_VIN_IV" in text
    _assert_no_secrets(text)


def test_check_auth_failure(env):
    code, text = _run(["--check"], env, login_fails=True)
    assert code == dump.EXIT_AUTH
    assert "AUTH FAILED" in text
    _assert_no_secrets(text)


def test_check_no_vehicles(env):
    code, _ = _run(["--check"], env, vins=())
    assert code == dump.EXIT_NO_VEHICLES


def test_capture_writes_files_with_private_modes(env, tmp_path):
    out = tmp_path / "captures"
    code, text = _run(["--out", str(out)], env)
    assert code == 0, text
    (cap,) = list(out.iterdir())
    assert stat.S_IMODE(cap.stat().st_mode) == 0o700
    names = {p.name for p in cap.iterdir()}
    assert names == {"vehicles.json", "vehicle_status.json", "charging_status.json",
                     "charging_limit.json", "journeys.json", "trip_trackpoints.json", "manifest.json"}
    for p in cap.iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        json.loads(p.read_text())
    status = json.loads((cap / "vehicle_status.json").read_text())
    assert status["additionalVehicleStatus"]["electricVehicleStatus"]["chargeLevel"] == "78"
    calls = [c for c in FakeClient.instances[0].calls]
    assert ("get_trip_trackpoints", VIN_A) in calls
    _assert_no_secrets(text)
    # session store reused / written without password
    sess = Path(env["ZEEKRMATE_STATE_DIR"]) / "zeekr_session.json"
    assert sess.exists() and SECRETS["password"] not in sess.read_text()


def test_capture_only_read_calls(env, tmp_path):
    from zeekrmate.providers.zeekr.session import READ_ONLY_METHODS

    _run(["--out", str(tmp_path / "c")], env)
    for client in FakeClient.instances:
        assert {c[0] for c in client.calls} <= READ_ONLY_METHODS


def test_capture_records_sanitised_error_and_continues(env, tmp_path):
    from zeekr_ev_api.exceptions import ZeekrException

    exc = ZeekrException('Failed to get vehicle charging status: {"t": "%s"}' % TOKEN)
    code, _ = _run(["--out", str(tmp_path / "c")], env, fail={("get_vehicle_charging_status", VIN_A): exc})
    assert code == 0
    (cap,) = list((tmp_path / "c").iterdir())
    err = json.loads((cap / "charging_status.json").read_text())
    assert "_error" in err and TOKEN not in json.dumps(err)
    assert json.loads((cap / "manifest.json").read_text())["results"]["charging_status"] == "error"


def test_capture_uses_env_capture_dir(env, tmp_path):
    env["ZEEKR_CAPTURE_DIR"] = str(tmp_path / "from-env")
    code, _ = _run([], env)
    assert code == 0
    assert len(list((tmp_path / "from-env").iterdir())) == 1


# --- sanitiser -------------------------------------------------------------

def _capture(tmp_path):
    cap = tmp_path / "2026-10-19-0930"
    cap.mkdir()
    files = {
        "vehicles.json": [{"vin": VIN_A, "data": {"vin": VIN_A, "name": "Johnson's 7X",
                                                  "plateNo": "ABC123", "modelName": "7X"}}],
        "vehicle_status.json": {
            "basicVehicleStatus": {"position": {"latitude": "-33.868812", "longitude": 151.209312}},
            "additionalVehicleStatus": {"electricVehicleStatus": {"chargeLevel": "78"},
                                        "climateStatus": {"updateTime": "1760000000000"}},
            "userId": 987654, "ownerPhone": "+61400111222", "contactEmail": "me@example.com",
            "accessToken": "short", "note": f"car {VIN_A} token {TOKEN}",
            "signalStrength": "4", "satelliteCount": 9, "velocity": "0",
        },
        "journeys.json": {"data": [{"tripId": 4242, "startLatitude": -33.861234, "startLongitude": 151.2,
                                    "traveledDistance": 12.3}]},
        "trip_trackpoints.json": {"tripId": 4242, "trackPoints": [{"latitude": -33.861234,
                                                                   "longitude": 151.205678}]},
        "notes.txt": "not json",
    }
    for name, data in files.items():
        (cap / name).write_text(data if isinstance(data, str) else json.dumps(data))
    return cap


def test_sanitiser_replaces_identifiers_and_rounds_coords(tmp_path):
    cap = _capture(tmp_path)
    assert sanitise.main([str(cap)]) == 0
    out = tmp_path / "2026-10-19-0930-sanitised"
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert not (out / "notes.txt").exists()
    text = "".join(p.read_text() for p in out.iterdir())
    for secret in (VIN_A, "Johnson", "ABC123", "987654", "+61400111222", "me@example.com",
                   TOKEN, "eyJ", "-33.868812", "151.209312", "151.205678"):
        assert secret not in text, secret
    vehicles = json.loads((out / "vehicles.json").read_text())
    status = json.loads((out / "vehicle_status.json").read_text())
    journeys = json.loads((out / "journeys.json").read_text())
    tps = json.loads((out / "trip_trackpoints.json").read_text())
    vin_ph = vehicles[0]["vin"]
    assert len(vin_ph) == 17 and vehicles[0]["data"]["vin"] == vin_ph
    assert vin_ph in status["note"]  # stable across files and inside strings
    assert vehicles[0]["data"]["modelName"] == "7X"
    assert status["basicVehicleStatus"]["position"] == {"latitude": "-33.87", "longitude": 151.21}
    assert status["additionalVehicleStatus"]["electricVehicleStatus"]["chargeLevel"] == "78"
    assert status["additionalVehicleStatus"]["climateStatus"]["updateTime"] == "1760000000000"
    assert isinstance(status["userId"], int) and status["userId"] != 987654
    assert status["accessToken"] != "short"
    assert status["signalStrength"] == "4" and status["satelliteCount"] == 9 and status["velocity"] == "0"
    assert journeys["data"][0]["tripId"] == tps["tripId"] != 4242
    assert journeys["data"][0]["traveledDistance"] == 12.3
    for p in out.iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    # the sanitised fixture still maps through the provider mapper
    from zeekrmate.providers.zeekr.mapper import map_position
    pos = map_position(vin_ph, status)
    assert pos.soc_pct == 78.0 and pos.lat == -33.87


def test_sanitiser_refuses_to_overwrite(tmp_path):
    cap = _capture(tmp_path)
    assert sanitise.main([str(cap)]) == 0
    assert sanitise.main([str(cap)]) == 1
    assert sanitise.main([str(cap), "--force"]) == 0
