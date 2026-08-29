import copy
import importlib
from unittest.mock import call, patch

import pytest


def _load_provider_modules():
    try:
        factory = importlib.import_module("fordlogger.providers.factory")
        ford = importlib.import_module("fordlogger.providers.ford")
        return factory, ford
    except ModuleNotFoundError:
        pytest.fail("vehicle provider factory is missing")


def test_per_vin_discovery_is_sorted_and_does_not_mutate_config(
    tmp_path, monkeypatch
):
    factory, ford = _load_provider_modules()
    (tmp_path / "tokens_Z.json").touch()
    (tmp_path / "tokens_A.json").touch()
    (tmp_path / "unrelated.json").touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FORDLOGGER_TOKEN_DIR", raising=False)
    cfg = {"api_base": "https://example.invalid", "token_file": "original.json"}
    original = copy.deepcopy(cfg)

    with patch.object(ford, "FordAPI") as ford_api:
        providers = factory.create_providers(cfg)

    assert len(providers) == 2
    expected_configs = [
        {**cfg, "token_file": "tokens_A.json"},
        {**cfg, "token_file": "tokens_Z.json"},
    ]
    assert ford_api.call_args_list == [call(item) for item in expected_configs]
    assert all(args.args[0] is not cfg for args in ford_api.call_args_list)
    assert cfg == original


def test_legacy_token_file_uses_original_config(tmp_path, monkeypatch):
    factory, ford = _load_provider_modules()
    token_dir = tmp_path / "token-dir"
    token_dir.mkdir()
    legacy = tmp_path / "configured-tokens.json"
    legacy.touch()
    monkeypatch.setenv("FORDLOGGER_TOKEN_DIR", str(token_dir))
    cfg = {"api_base": "https://example.invalid", "token_file": str(legacy)}
    original = copy.deepcopy(cfg)

    with patch.object(ford, "FordAPI") as ford_api:
        providers = factory.create_providers(cfg)

    assert len(providers) == 1
    ford_api.assert_called_once_with(cfg)
    assert ford_api.call_args.args[0] is cfg
    assert cfg == original


def test_no_token_files_returns_no_providers(tmp_path, monkeypatch):
    factory, ford = _load_provider_modules()
    monkeypatch.setenv("FORDLOGGER_TOKEN_DIR", str(tmp_path))
    cfg = {
        "api_base": "https://example.invalid",
        "token_file": str(tmp_path / "missing-tokens.json"),
    }

    with patch.object(ford, "FordAPI") as ford_api:
        providers = factory.create_providers(cfg)

    assert providers == []
    ford_api.assert_not_called()
