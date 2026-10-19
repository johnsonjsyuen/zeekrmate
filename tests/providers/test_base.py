from dataclasses import FrozenInstanceError
import importlib
import json
import pathlib
import subprocess
import sys

import pytest


def _load_base_module():
    try:
        return importlib.import_module("zeekrmate.providers.base")
    except ModuleNotFoundError:
        pytest.fail("vehicle provider base abstraction is missing")


def test_default_capabilities_are_false_and_immutable():
    base = _load_base_module()

    capabilities = base.ProviderCapabilities()

    assert capabilities.journey_history is False
    assert capabilities.trip_trackpoints is False
    with pytest.raises(FrozenInstanceError):
        capabilities.journey_history = True


def test_incomplete_provider_cannot_be_instantiated():
    base = _load_base_module()

    class IncompleteProvider(base.VehicleProvider):
        name = "incomplete"

        def get_vehicles(self):
            return []

    with pytest.raises(TypeError):
        IncompleteProvider()


def test_provider_without_name_cannot_be_instantiated():
    base = _load_base_module()

    class NamelessProvider(base.VehicleProvider):
        def get_vehicles(self):
            return []

        def get_telemetry(self):
            return []

    with pytest.raises(TypeError):
        NamelessProvider()


def test_importing_base_does_not_load_ford_api_or_requests():
    script = """
import json
import sys

import zeekrmate.providers.base

loaded = [
    name for name in ("zeekrmate.api", "requests")
    if name in sys.modules
]
print(json.dumps(loaded))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(pathlib.Path(__file__).resolve().parents[2]),
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == []
