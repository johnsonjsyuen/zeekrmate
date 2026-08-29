import importlib
from unittest.mock import Mock, patch

import pytest

from fordlogger.models import Position, Vehicle


def _load_ford_module():
    try:
        return importlib.import_module("fordlogger.providers.ford")
    except ModuleNotFoundError:
        pytest.fail("Ford vehicle provider adapter is missing")


def test_get_vehicles_delegates_to_existing_client_unchanged():
    ford = _load_ford_module()
    expected = [Vehicle(vin="VIN-1")]
    client = Mock()
    client.get_garage.return_value = expected
    provider = ford.FordProvider(client=client)

    actual = provider.get_vehicles()

    assert actual is expected
    client.get_garage.assert_called_once_with()


def test_get_telemetry_delegates_empty_output_unchanged():
    ford = _load_ford_module()
    expected: list[Position] = []
    client = Mock()
    client.get_telemetry.return_value = expected
    provider = ford.FordProvider(client=client)

    actual = provider.get_telemetry()

    assert actual is expected
    client.get_telemetry.assert_called_once_with()


def test_config_constructs_and_stores_delegating_client():
    ford = _load_ford_module()
    cfg = {"api_base": "https://example.invalid"}
    expected = [Vehicle(vin="VIN-1")]
    created_client = Mock(name="created_ford_client")
    created_client.get_garage.return_value = expected

    with patch.object(ford, "FordAPI", return_value=created_client) as ford_api:
        provider = ford.FordProvider(cfg)

    ford_api.assert_called_once_with(cfg)
    assert provider.client is created_client
    assert provider.get_vehicles() is expected
    created_client.get_garage.assert_called_once_with()
