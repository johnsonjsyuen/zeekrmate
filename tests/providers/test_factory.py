import copy
import importlib
from unittest.mock import call, patch, sentinel

import pytest


def _load_factory_module():
    try:
        return importlib.import_module("fordlogger.providers.factory")
    except ModuleNotFoundError:
        pytest.fail("vehicle provider factory is missing")


def test_per_vin_discovery_is_sorted_and_does_not_mutate_config(
    tmp_path, monkeypatch
):
    factory = _load_factory_module()
    token_dir = tmp_path / "configured-token-dir"
    token_dir.mkdir()
    (token_dir / "tokens_Z.json").touch()
    (token_dir / "tokens_A.json").touch()
    (token_dir / "unrelated.json").touch()
    empty_cwd = tmp_path / "empty-cwd"
    empty_cwd.mkdir()
    monkeypatch.chdir(empty_cwd)
    monkeypatch.setenv("FORDLOGGER_TOKEN_DIR", str(token_dir))
    cfg = {"api_base": "https://example.invalid", "token_file": "original.json"}
    original = copy.deepcopy(cfg)

    with patch.object(
        factory,
        "FordProvider",
        side_effect=[sentinel.first_provider, sentinel.second_provider],
    ) as ford_provider:
        providers = factory.create_providers(cfg)

    assert len(providers) == 2
    expected_configs = [
        {**cfg, "token_file": str(token_dir / "tokens_A.json")},
        {**cfg, "token_file": str(token_dir / "tokens_Z.json")},
    ]
    assert ford_provider.call_args_list == [call(item) for item in expected_configs]
    assert all(args.args[0] is not cfg for args in ford_provider.call_args_list)
    assert providers == [sentinel.first_provider, sentinel.second_provider]
    assert cfg == original


def test_legacy_token_file_uses_original_config(tmp_path, monkeypatch):
    factory = _load_factory_module()
    token_dir = tmp_path / "token-dir"
    token_dir.mkdir()
    legacy = tmp_path / "configured-tokens.json"
    legacy.touch()
    monkeypatch.setenv("FORDLOGGER_TOKEN_DIR", str(token_dir))
    cfg = {"api_base": "https://example.invalid", "token_file": str(legacy)}
    original = copy.deepcopy(cfg)

    with patch.object(
        factory, "FordProvider", return_value=sentinel.legacy_provider
    ) as ford_provider:
        providers = factory.create_providers(cfg)

    assert providers == [sentinel.legacy_provider]
    ford_provider.assert_called_once_with(original)
    assert ford_provider.call_args.args[0] == original
    assert cfg == original


def test_no_token_files_returns_no_providers(tmp_path, monkeypatch):
    factory = _load_factory_module()
    monkeypatch.setenv("FORDLOGGER_TOKEN_DIR", str(tmp_path))
    cfg = {
        "api_base": "https://example.invalid",
        "token_file": str(tmp_path / "missing-tokens.json"),
    }

    with patch.object(factory, "FordProvider") as ford_provider:
        providers = factory.create_providers(cfg)

    assert providers == []
    ford_provider.assert_not_called()
