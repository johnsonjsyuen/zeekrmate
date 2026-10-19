"""No secrets, tokens, headers, payloads or full VINs in logs, even at DEBUG."""

import logging

import pytest
from zeekr_ev_api.exceptions import ZeekrException

from fordlogger.providers.zeekr.redact import mask_vin, quiet_library_loggers, safe_error

from .conftest import AUTH_TOKEN, SECRETS, TOKEN, VIN_A, VIN_B


def _assert_clean(text):
    assert TOKEN not in text
    assert "eyJ" not in text
    assert AUTH_TOKEN not in text
    for value in SECRETS.values():
        assert value not in text, value
    assert VIN_A not in text and VIN_B not in text


def test_mask_vin():
    assert mask_vin(VIN_A) == "***0001"
    assert mask_vin(None) == "***"
    assert mask_vin("ab") == "***"


def test_safe_error_drops_embedded_body():
    exc = ZeekrException('Failed to get vehicle status: {"success": false, "token": "%s"}' % TOKEN)
    assert safe_error(exc) == "ZeekrException: Failed to get vehicle status"


def test_full_cycle_at_root_debug_leaks_nothing(make_provider, caplog):
    caplog.set_level(logging.DEBUG)  # root logger at DEBUG
    logging.getLogger().setLevel(logging.DEBUG)
    provider, _, _ = make_provider(vins=(VIN_A, VIN_B), auth_fail_times=[1])
    provider.get_vehicles()
    provider.get_telemetry()
    # The fake client logs tokens/headers at DEBUG via the injected logger;
    # the library module logger must be quiet too.
    logging.getLogger("zeekr_ev_api").debug("headers %s", {"authorization": TOKEN})
    logging.getLogger("zeekr_ev_api.network").debug("headers %s", {"authorization": TOKEN})
    assert caplog.records, "provider should still log its own INFO lines"
    _assert_clean(caplog.text)
    assert "***0001" in caplog.text


def test_library_loggers_pinned_to_warning():
    logging.getLogger("zeekr_ev_api.network")  # ensure a child exists
    injected = quiet_library_loggers()
    for name in ("zeekr_ev_api", "zeekr_ev_api.network", "zeekr_ev_api.client"):
        lg = logging.getLogger(name)
        assert lg.getEffectiveLevel() >= logging.WARNING
        assert not lg.isEnabledFor(logging.DEBUG)
        assert not lg.isEnabledFor(logging.INFO)
    assert injected.getEffectiveLevel() >= logging.WARNING


def test_library_warning_with_payload_is_redacted(caplog):
    caplog.set_level(logging.DEBUG)
    injected = quiet_library_loggers()
    injected.warning("Request failed: %s", {"authorization": TOKEN, "vin": VIN_A})
    logging.getLogger("zeekr_ev_api").warning("bearer %s for %s", TOKEN, VIN_A)
    _assert_clean(caplog.text)


def test_auth_failure_logs_are_clean(make_provider, caplog):
    caplog.set_level(logging.DEBUG)
    exc = ZeekrException('Failed to get vehicle list: {"token": "%s"}' % TOKEN)
    provider, _, _ = make_provider(fail={("get_vehicle_list", None): exc})
    with pytest.raises(Exception) as info:
        provider.get_vehicles()
    logging.getLogger("fordlogger").error("Provider error: %s", info.value, exc_info=True)
    _assert_clean(caplog.text)
