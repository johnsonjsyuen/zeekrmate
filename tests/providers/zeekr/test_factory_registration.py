import json
import subprocess
import sys

import pytest

from fordlogger.providers import factory
from fordlogger.providers.zeekr.settings import ZeekrConfigError


def test_zeekr_is_registered():
    assert "zeekr" in factory.PROVIDER_BUILDERS


def test_importing_providers_does_not_load_zeekr_library():
    script = """
import json, sys
import fordlogger.providers
import fordlogger.providers.factory
print(json.dumps([m for m in ("zeekr_ev_api", "requests", "Crypto",
                              "fordlogger.providers.zeekr.provider") if m in sys.modules]))
"""
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == []


def test_factory_reports_missing_env_by_name(monkeypatch):
    for name in ("ZEEKR_USERNAME", "ZEEKR_PASSWORD", "ZEEKR_HMAC_ACCESS_KEY", "ZEEKR_HMAC_SECRET_KEY",
                 "ZEEKR_PASSWORD_PUBLIC_KEY", "ZEEKR_PROD_SECRET", "ZEEKR_VIN_KEY", "ZEEKR_VIN_IV"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ZeekrConfigError, match="ZEEKR_USERNAME"):
        factory.create_providers({"providers": [{"type": "zeekr"}]})


def test_factory_builds_zeekr_without_network(monkeypatch, tmp_path):
    env = {"ZEEKR_USERNAME": "u", "ZEEKR_PASSWORD": "p", "ZEEKR_HMAC_ACCESS_KEY": "a",
           "ZEEKR_HMAC_SECRET_KEY": "b", "ZEEKR_PASSWORD_PUBLIC_KEY": "c", "ZEEKR_PROD_SECRET": "d",
           "ZEEKR_VIN_KEY": "e", "ZEEKR_VIN_IV": "f", "FORDLOGGER_STATE_DIR": str(tmp_path)}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    (provider,) = factory.create_providers({"providers": [{"type": "zeekr"}]})
    assert provider.name == "zeekr"
    assert not (tmp_path / "zeekr_session.json").exists()
