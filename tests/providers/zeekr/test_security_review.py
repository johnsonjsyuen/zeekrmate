"""Adversarial security tests (independent review, phase 1).

Tests marked ``xfail(strict=True)`` document CONFIRMED weaknesses that are
reported but not fixed here: they fail today (so the suite stays green) and
will start XPASSing - turning the suite red - once the weakness is fixed, at
which point the marker should be removed.

No network access: the real zeekr_ev_api is only used for its plain
``Vehicle`` class and exception types.
"""

import json
import logging
import os
import stat

import pytest
from zeekr_ev_api.client import Vehicle as LibVehicle

from fordlogger.providers.zeekr.redact import quiet_library_loggers, safe_error, scrub
from fordlogger.providers.zeekr.session import ReadOnlyClient, ReadOnlyViolation, ZeekrSession
from fordlogger.providers.zeekr.session_store import SessionStore
from scripts.sanitise_zeekr_capture import Sanitiser

from .conftest import AUTH_TOKEN, SECRETS, TOKEN, VIN_A, FakeClient, ForbiddenCall


class LibVehicleClient(FakeClient):
    """Fake client whose vehicle list returns the library's real Vehicle
    objects, which keep a reference to the (raw) client that created them."""

    def get_vehicle_list(self):
        self._maybe_fail("get_vehicle_list")
        return [LibVehicle(self, v, {"vin": v, "name": "Car"}) for v in self.vins]


def _session(tmp_path, settings, cls=LibVehicleClient, **kw):
    store = SessionStore(tmp_path / "state" / "zeekr_session.json")
    return ZeekrSession(settings, store, client_factory=lambda _s, lg: cls(logger=lg, **kw))


# ---------------------------------------------------------------- read-only


@pytest.mark.parametrize("method,args", [
    ("do_remote_control", ("start", "RHL", {})),
    ("set_charge_plan", ("01:00", "06:00")),
    ("set_travel_plan", ()),
])
def test_vehicle_objects_cannot_reach_write_commands(tmp_path, settings, method, args):
    """Vehicle._client is the raw ZeekrClient; session.call must not hand
    those objects out (fixed in session._detach)."""
    session = _session(tmp_path, settings)
    vehicles = session.call("get_vehicle_list")
    assert [v.vin for v in vehicles] == [VIN_A]
    v = vehicles[0]
    assert not hasattr(v, "_client")
    assert not hasattr(v, method)
    for value in vars(v).values():
        assert not isinstance(value, (FakeClient, LibVehicle))


def test_provider_and_dump_still_work_with_library_vehicle_objects(tmp_path, settings):
    from fordlogger.providers.zeekr.provider import ZeekrProvider

    provider = ZeekrProvider(_session(tmp_path, settings))
    assert [v.vin for v in provider.get_vehicles()] == [VIN_A]
    assert len(provider.get_telemetry()) == 1


def test_proxy_blocks_attribute_tricks(tmp_path, settings):
    session = _session(tmp_path, settings)
    client = session.connect()
    assert isinstance(client, ReadOnlyClient)
    for name in ("do_remote_control", "set_charge_plan", "set_travel_plan",
                 "get_remote_control_state", "get_charge_plan", "vehicles", "session",
                 "__dict__"):
        with pytest.raises((ReadOnlyViolation, AttributeError)):
            getattr(client, name)
    with pytest.raises(ReadOnlyViolation):
        session.call("do_remote_control", VIN_A, "start", "RHL", {})


@pytest.mark.xfail(strict=True, reason="defence in depth: ReadOnlyClient.__slots__ makes "
                   "_client a real attribute, so proxy._client is the raw client")
def test_proxy_does_not_expose_raw_client_attribute(tmp_path, settings):
    client = _session(tmp_path, settings).connect()
    with pytest.raises(AttributeError):
        client._client  # noqa: B018


WRITE_NAMES = {"do_remote_control", "set_charge_plan", "set_travel_plan", "__self__",
               "get_vehicle_vtm_status", "get_remote_control_state"}


def test_no_code_path_names_a_vehicle_command():
    """Static check over fordlogger/ and scripts/: no attribute access to, and
    no string argument naming, a command method (getattr/session.call)."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    hits = []
    for path in list((root / "fordlogger").rglob("*.py")) + list((root / "scripts").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in WRITE_NAMES:
                hits.append(f"{path.name}:{node.lineno} .{node.attr}")
            if isinstance(node, ast.Call):
                for arg in list(node.args) + [k.value for k in node.keywords]:
                    if isinstance(arg, ast.Constant) and arg.value in WRITE_NAMES:
                        hits.append(f"{path.name}:{node.lineno} {arg.value!r}")
    assert hits == []


@pytest.mark.xfail(strict=True, reason="defence in depth: allowed bound methods expose "
                   "the raw client via __self__ (not used by any code path)")
def test_allowed_bound_methods_do_not_expose_raw_client(tmp_path, settings):
    client = _session(tmp_path, settings).connect()
    raw = client.login.__self__
    with pytest.raises(AttributeError):
        raw.do_remote_control  # noqa: B018


# ------------------------------------------------------------ session file


def test_session_file_private_even_with_permissive_umask(tmp_path, settings):
    old = os.umask(0)
    try:
        session = _session(tmp_path, settings)
        session.call("get_vehicle_list")
    finally:
        os.umask(old)
    path = tmp_path / "state" / "zeekr_session.json"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, oct(mode)
    text = path.read_text()
    assert SECRETS["password"] not in text
    assert "password" not in json.loads(text)
    for key in ("hmac_access_key", "hmac_secret_key", "password_public_key",
                "prod_secret", "vin_key", "vin_iv"):
        assert SECRETS[key] not in text
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


# ------------------------------------------------------------------ logging


def test_library_error_with_html_body_keeps_jwt_and_vin_out(caplog):
    caplog.set_level(logging.DEBUG)
    quiet_library_loggers()
    logging.getLogger("fordlogger.zeekr.client").error(
        "Response status: %s, text: %s", 502,
        "<html>gateway %s for %s %s</html>" % (TOKEN, VIN_A, AUTH_TOKEN))
    assert TOKEN not in caplog.text and "eyJ" not in caplog.text
    assert AUTH_TOKEN not in caplog.text
    assert VIN_A not in caplog.text


def test_library_error_with_short_token_in_text_body(caplog):
    caplog.set_level(logging.DEBUG)
    quiet_library_loggers()
    logging.getLogger("fordlogger.zeekr.client").error(
        "Response status: %s, text: %s", 401, "access_token=Zk7Qp2Lm9Xc4Rt8V")
    assert "Zk7Qp2Lm9Xc4Rt8V" not in caplog.text


def test_late_library_child_logger_is_filtered(caplog):
    caplog.set_level(logging.DEBUG)
    quiet_library_loggers()
    logging.getLogger("zeekr_ev_api.late_module_review").warning(
        "refresh failed for %s with %s", VIN_A, "authorization=Zk7Qp2Lm9Xc4Rt8V")
    assert VIN_A not in caplog.text


@pytest.mark.parametrize("text", [
    f"vin={VIN_A}",
    f"/vehicles/{VIN_A}/status",
    f"'{VIN_A}'",
])
def test_scrub_masks_vin_with_separators(text):
    assert VIN_A not in scrub(text)


@pytest.mark.parametrize("text", [f"trip_{VIN_A}", f"{VIN_A}_2025", VIN_A.lower()])
def test_scrub_masks_vin_without_word_boundary(text):
    assert VIN_A.lower() not in scrub(text).lower()


def test_safe_error_never_returns_body_after_colon_or_brace():
    exc = RuntimeError("HTTPSConnectionPool(host='x', port=443): Max retries exceeded "
                       "with url: /x?token=%s" % AUTH_TOKEN)
    assert AUTH_TOKEN not in safe_error(exc)
    exc = RuntimeError("Failed for %s {\"token\": \"%s\"}" % (VIN_A, AUTH_TOKEN))
    out = safe_error(exc)
    assert AUTH_TOKEN not in out and VIN_A not in out


def test_poller_db_error_with_vin_is_masked(conn, cfg, caplog, monkeypatch):
    """psycopg2 errors carry DETAIL lines with row values (e.g. a FK violation
    'Key (vin)=(<full VIN>) is not present'). Such an error must not put a full
    VIN in the log."""
    from datetime import datetime, timezone

    import psycopg2

    from fordlogger import db
    from fordlogger.models import Position
    from fordlogger.poller import Poller
    from tests.fakes import FakeProvider

    # Obtain a real FK-violation error from PostgreSQL.
    with pytest.raises(psycopg2.Error) as real:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO positions (ts, vin, observed_at) VALUES (NOW(), %s, NOW())",
                        (VIN_A,))
    assert VIN_A in str(real.value)  # the DB itself puts the VIN in the message

    def boom(_conn, _pos):
        raise real.value

    monkeypatch.setattr(db, "insert_position", boom)
    pos = Position(ts=datetime.now(timezone.utc), vin=VIN_A, soc_pct=50.0)
    poller = Poller(cfg, conn, FakeProvider(positions=[pos]))
    caplog.set_level(logging.INFO)
    poller.poll_once()
    assert "***0001" in caplog.text
    assert VIN_A not in caplog.text


# ---------------------------------------------------------------- sanitiser


def test_sanitiser_handles_nested_and_embedded_values():
    s = Sanitiser()
    data = {
        "vehicles": [{"data": {"vin": VIN_A, "extra": {"position": {"latitude": "-33.868812",
                                                                     "longitude": 151.209312}}}}],
        "message": f"vehicle {VIN_A} parked",
        "headers": {"Authorization": "short-tok"},
        "note": TOKEN.split(" ", 1)[1],
        "url": f"https://x.invalid/a?vin={VIN_A}&x=1",
        "contact": "owner@example.com",
    }
    out = json.dumps(s.value(data))
    assert VIN_A not in out
    assert "short-tok" not in out and "eyJ" not in out
    assert "owner@example.com" not in out
    assert "-33.868812" not in out and "151.209312" not in out


@pytest.mark.xfail(strict=True, reason="sanitiser misses coordinates not under lat/lon/"
                   "latitude/longitude keys (pairs, 'coordinates' arrays, *Lat/*Lng)")
@pytest.mark.parametrize("data", [
    {"coordinates": [151.209312, -33.868812]},
    {"location": "-33.868812,151.209312"},
    {"startLat": -33.868812, "startLng": 151.209312},
    {"gpsLat": -33.868812},
])
def test_sanitiser_rounds_unusual_coordinate_shapes(data):
    out = json.dumps(Sanitiser().value(data))
    assert "868812" not in out and "209312" not in out


@pytest.mark.xfail(strict=True, reason="VIN regex needs \\b: VIN inside a word-joined "
                   "string such as 'trip_<VIN>' survives sanitising")
def test_sanitiser_vin_joined_with_underscore():
    out = json.dumps(Sanitiser().value({"ref": f"trip_{VIN_A}"}))
    assert VIN_A not in out


@pytest.mark.xfail(strict=True, reason="header lists like [{name: authorization, value: x}] "
                   "keep short token values")
def test_sanitiser_header_list_token():
    out = json.dumps(Sanitiser().value({"headers": [{"name": "authorization", "value": "tok-123"}]}))
    assert "tok-123" not in out


def test_forbidden_methods_still_raise_on_fake():
    with pytest.raises(ForbiddenCall):
        FakeClient().do_remote_control()
