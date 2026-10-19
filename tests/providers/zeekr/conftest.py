"""Fake zeekr_ev_api client and sample payloads. No network access, ever.

Payload shapes follow the paths the Home Assistant integration reads
(custom_components/zeekr_ev/sensor.py, binary_sensor.py, device_tracker.py,
lock.py, climate.py) and the library's tests (vehicle list {"vin", "name"}).
Values are invented.
"""

import copy
import logging

import pytest
from zeekr_ev_api.exceptions import AuthException, ZeekrException

from fordlogger.providers.zeekr.session import ZeekrSession
from fordlogger.providers.zeekr.session_store import SessionStore
from fordlogger.providers.zeekr.settings import ZeekrSettings
from fordlogger.providers.zeekr.provider import ZeekrProvider

VIN_A = "LZEKR7XAU00000001"
VIN_B = "LZEKR7XAU00000002"
UPDATE_MS = 1760000000000  # 2025-10-09T08:53:20Z

SECRETS = {
    "username": "owner@example.com",
    "password": "S3cret-Passw0rd!",
    "hmac_access_key": "HMACACCESSKEYVALUE0001",
    "hmac_secret_key": "HMACSECRETKEYVALUE0002",
    "password_public_key": "PUBLICKEYVALUE0003",
    "prod_secret": "PRODSECRETVALUE0004",
    "vin_key": "VINKEYVALUE0005",
    "vin_iv": "VINIVVALUE0006",
}
TOKEN = "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJlLXRva2Vu"
AUTH_TOKEN = "authtoken-0123456789abcdef0123456789abcdef"

VEHICLE_STATUS = {
    "basicVehicleStatus": {
        "usageMode": "1",
        "engineStatus": "engine-off",
        "position": {"latitude": "-33.8688", "longitude": "151.2093"},
    },
    "additionalVehicleStatus": {
        "electricVehicleStatus": {
            "chargeLevel": "78",
            "distanceToEmptyOnBatteryOnly": "412",
            "chargerState": "2",
            "statusOfChargerConnection": "1",
            "timeToFullyCharged": "95",
        },
        "maintenanceStatus": {
            "odometer": "12345.6",
            "tyreStatusDriver": "250",
        },
        "climateStatus": {"interiorTemp": "21.5", "updateTime": str(UPDATE_MS)},
        "drivingSafetyStatus": {"centralLockingStatus": "1"},
    },
}
CHARGING_STATUS = {"chargerState": "2", "chargePower": "7.2", "chargeVoltage": "240", "chargeCurrent": "30"}
CHARGING_LIMIT = {"soc": "800"}


class FakeVehicle:
    def __init__(self, vin, data):
        self.vin = vin
        self.data = data


class ForbiddenCall(AssertionError):
    pass


class FakeClient:
    """Implements the public read-only surface of ZeekrClient."""

    instances: list = []

    def __init__(self, vins=(VIN_A,), statuses=None, fail=None, auth_fail_times=0,
                 login_fails=False, logger=None):
        self.vins = list(vins)
        self.statuses = statuses or {}
        self.fail = fail or {}            # (method, vin) -> exception
        # a list is shared between successive clients built by one factory
        self.auth_budget = auth_fail_times if isinstance(auth_fail_times, list) else [auth_fail_times]
        self.login_fails = login_fails
        self.logger = logger or logging.getLogger("zeekr_ev_api.client")
        self.calls: list = []
        self.logged_in = False
        self.session_data = None
        FakeClient.instances.append(self)

    # auth / session
    def login(self, relogin=False):
        self.calls.append(("login",))
        self.logger.debug("login headers authorization=%s", TOKEN)
        if self.login_fails:
            raise AuthException('Login failed: {"token": "%s"}' % TOKEN)
        self.logged_in = True
        self.session_data = {"bearer_token": TOKEN, "auth_token": AUTH_TOKEN}

    def load_session(self, data):
        self.calls.append(("load_session",))
        self.session_data = dict(data)
        self.logged_in = bool(data.get("bearer_token"))

    def export_session(self):
        if not self.logged_in:
            return {}
        return {
            "username": SECRETS["username"],
            "password": SECRETS["password"],
            "country_code": "AU",
            **(self.session_data or {}),
        }

    def _maybe_fail(self, method, vin=None):
        self.calls.append((method, vin))
        self.logger.debug("request %s headers=%s", method, {"authorization": TOKEN})
        if self.auth_budget[0] > 0:
            self.auth_budget[0] -= 1
            raise AuthException("Token expired")
        exc = self.fail.get((method, vin))
        if exc is not None:
            raise exc

    # reads
    def get_vehicle_list(self):
        self._maybe_fail("get_vehicle_list")
        return [FakeVehicle(v, {"vin": v, "name": "Car"}) for v in self.vins]

    def get_vehicle_status(self, vin):
        self._maybe_fail("get_vehicle_status", vin)
        return copy.deepcopy(self.statuses.get(vin, VEHICLE_STATUS))

    def get_vehicle_charging_status(self, vin):
        self._maybe_fail("get_vehicle_charging_status", vin)
        return dict(CHARGING_STATUS)

    def get_vehicle_charging_limit(self, vin):
        self._maybe_fail("get_vehicle_charging_limit", vin)
        return dict(CHARGING_LIMIT)

    def get_journey_log(self, vin, page_size=10, current_page=1, last_id=-1, days_back=30, end_time=0):
        self._maybe_fail("get_journey_log", vin)
        return {"total": 1, "data": [{"tripId": 4242, "reportTime": UPDATE_MS,
                                      "startTime": UPDATE_MS - 600000, "endTime": UPDATE_MS}]}

    def get_trip_trackpoints(self, vin, trip_report_time, trip_id):
        self._maybe_fail("get_trip_trackpoints", vin)
        return {"trackPoints": [{"latitude": -33.86881, "longitude": 151.20931}]}

    # writes: must never be called
    def do_remote_control(self, *a, **k):
        raise ForbiddenCall("do_remote_control called")

    def set_charge_plan(self, *a, **k):
        raise ForbiddenCall("set_charge_plan called")

    def set_travel_plan(self, *a, **k):
        raise ForbiddenCall("set_travel_plan called")


@pytest.fixture
def settings():
    return ZeekrSettings(**SECRETS)


@pytest.fixture
def make_provider(tmp_path, settings):
    FakeClient.instances.clear()

    def _make(**client_kwargs):
        def factory(_settings, logger):
            return FakeClient(logger=logger, **client_kwargs)

        store = SessionStore(tmp_path / "state" / "zeekr_session.json")
        session = ZeekrSession(settings, store, client_factory=factory)
        return ZeekrProvider(session), session, store

    return _make
