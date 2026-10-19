import pytest

import fordlogger.geocoder as geocoder


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"address": {"road": "Main St", "house_number": "1",
                            "city": "Sydney", "country_code": "au"}}


@pytest.fixture
def fake_get(monkeypatch):
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append({"params": params, "headers": headers, "timeout": timeout})
        return _Resp()

    geocoder.clear_cache()
    monkeypatch.setattr(geocoder.requests, "get", get)
    monkeypatch.setattr(geocoder.time, "sleep", lambda s: None)
    yield calls
    geocoder.clear_cache()


def test_result_is_cached_by_rounded_coordinates(fake_get):
    assert geocoder.reverse_geocode(-33.868801, 151.209301) == "Main St 1, Sydney, AU"
    assert geocoder.reverse_geocode(-33.868849, 151.209349) == "Main St 1, Sydney, AU"
    assert len(fake_get) == 1


def test_identifies_zeekrmate_with_short_timeout(fake_get):
    geocoder.reverse_geocode(1.0, 2.0)
    assert "zeekrmate" in fake_get[0]["headers"]["User-Agent"]
    assert fake_get[0]["timeout"] <= 5


def test_failure_returns_none_updates_rate_limit_and_is_not_cached(monkeypatch):
    geocoder.clear_cache()
    monkeypatch.setattr(geocoder.time, "sleep", lambda s: None)
    monkeypatch.setattr(geocoder, "_last_request_time", 0.0)

    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(geocoder.requests, "get", boom)
    assert geocoder.reverse_geocode(1.0, 2.0) is None
    assert geocoder._last_request_time > 0
    assert (1.0, 2.0) not in geocoder._cache


def test_none_coordinates():
    assert geocoder.reverse_geocode(None, 1.0) is None
