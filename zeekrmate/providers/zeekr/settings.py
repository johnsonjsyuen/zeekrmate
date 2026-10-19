"""Zeekr credentials and key material, read ONLY from environment variables.

Values never appear in repr(), logs or error messages; errors list variable
NAMES only. Obtaining the key values is the user's responsibility (see the
zeekr_ev_api README); this project does not document or provide them.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

# Env var -> ZeekrClient constructor keyword.
SECRET_ENV_VARS: dict[str, str] = {
    "ZEEKR_USERNAME": "username",
    "ZEEKR_PASSWORD": "password",
    "ZEEKR_HMAC_ACCESS_KEY": "hmac_access_key",
    "ZEEKR_HMAC_SECRET_KEY": "hmac_secret_key",
    "ZEEKR_PASSWORD_PUBLIC_KEY": "password_public_key",
    "ZEEKR_PROD_SECRET": "prod_secret",
    "ZEEKR_VIN_KEY": "vin_key",
    "ZEEKR_VIN_IV": "vin_iv",
}
COUNTRY_ENV_VAR = "ZEEKR_COUNTRY_CODE"
DEFAULT_COUNTRY_CODE = "AU"
STATE_DIR_ENV_VAR = "ZEEKRMATE_STATE_DIR"
DEFAULT_STATE_DIR = "data"
SESSION_FILENAME = "zeekr_session.json"


class ZeekrConfigError(ValueError):
    """Configuration problem. The message never contains secret values."""


@dataclass(frozen=True)
class ZeekrSettings:
    username: str = field(repr=False)
    password: str = field(repr=False)
    hmac_access_key: str = field(repr=False)
    hmac_secret_key: str = field(repr=False)
    password_public_key: str = field(repr=False)
    prod_secret: str = field(repr=False)
    vin_key: str = field(repr=False)
    vin_iv: str = field(repr=False)
    country_code: str = DEFAULT_COUNTRY_CODE

    def client_kwargs(self) -> dict:
        """Keyword arguments for ZeekrClient (contains secrets: never log)."""
        kwargs = {kw: getattr(self, kw) for kw in SECRET_ENV_VARS.values()}
        kwargs["country_code"] = self.country_code
        return kwargs

    @property
    def secret_values(self) -> list[str]:
        return [getattr(self, kw) for kw in SECRET_ENV_VARS.values()]


def load_settings(environ: Optional[Mapping[str, str]] = None) -> ZeekrSettings:
    env = os.environ if environ is None else environ
    missing = [name for name in SECRET_ENV_VARS if not (env.get(name) or "").strip()]
    if missing:
        raise ZeekrConfigError(
            "Missing required Zeekr environment variable(s): "
            + ", ".join(missing)
            + " (set them in .env; see .env.example)"
        )
    country = (env.get(COUNTRY_ENV_VAR) or DEFAULT_COUNTRY_CODE).strip().upper()
    values = {kw: env[name].strip() for name, kw in SECRET_ENV_VARS.items()}
    # Passwords may legitimately have surrounding spaces; keep it verbatim.
    values["password"] = env["ZEEKR_PASSWORD"]
    return ZeekrSettings(country_code=country, **values)


def resolve_state_dir(cfg: Optional[Mapping] = None,
                      environ: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    raw = env.get(STATE_DIR_ENV_VAR) or (cfg or {}).get("state_dir") or DEFAULT_STATE_DIR
    return Path(raw)


def session_path(state_dir: Path) -> Path:
    return Path(state_dir) / SESSION_FILENAME
