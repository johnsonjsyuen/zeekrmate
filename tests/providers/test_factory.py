import copy

import pytest

from fordlogger.providers import factory


def test_no_configured_providers_returns_empty_list():
    assert factory.create_providers({}) == []


def test_configured_provider_gets_merged_copy_of_config(monkeypatch):
    built = []

    def builder(cfg):
        built.append(cfg)
        return "provider"

    monkeypatch.setitem(factory.PROVIDER_BUILDERS, "fake", builder)
    cfg = {"db_host": "db", "providers": [{"type": "fake", "country_code": "AU"}]}
    original = copy.deepcopy(cfg)

    assert factory.create_providers(cfg) == ["provider"]
    assert built[0]["db_host"] == "db"
    assert built[0]["country_code"] == "AU"
    assert cfg == original


def test_unknown_provider_type_is_rejected():
    with pytest.raises(ValueError, match="nope"):
        factory.create_providers({"providers": [{"type": "nope"}]})
