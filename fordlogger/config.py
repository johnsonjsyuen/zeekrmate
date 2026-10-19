import json
import logging
import os
from pathlib import Path

log = logging.getLogger("fordlogger")

INTERVALS = {
    "driving": 60,
    "charging": 60,
    "parked": 120,
    "sleeping": 300,
}

# Defaults for config.json keys. Env overrides: see apply_env_overrides().
DEFAULT_CONFIG = {
    "db_host": "db",
    "db_port": 5432,
    "db_name": "fordlogger",
    "db_user": "fordlogger",
    "db_password": "fordlogger",
    "sleep_after_minutes": 30,
    # Poll interval in seconds per vehicle state (shortest across vehicles wins).
    "polling": dict(INTERVALS),
    # End DRIVING/CHARGING when the provider has served no fresh snapshot for
    # this long (frozen data while the car sleeps).
    "stale_after_minutes": 15,
    # Consecutive non-active readings required to leave DRIVING/CHARGING.
    "state_exit_debounce": 2,
    # Anchor a trip at the last reading before driving if it is this recent.
    "trip_prestart_window_s": 900,
    # Peak charge power above this is classed DC (7X: 22 kW AC on-board charger).
    "dc_threshold_kw": 22.0,
    "store_raw_json": True,
    "geocoding": True,
    "providers": [],
}

_TRUE = ("true", "1", "yes")


def _env_bool(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    return value.strip().lower() in _TRUE


def apply_env_overrides(cfg: dict) -> dict:
    """Apply FORDLOGGER_* environment overrides in place (only when set)."""
    if os.environ.get("FORDLOGGER_DB_HOST"):
        cfg["db_host"] = os.environ["FORDLOGGER_DB_HOST"]
    if os.environ.get("FORDLOGGER_DB_PORT"):
        cfg["db_port"] = int(os.environ["FORDLOGGER_DB_PORT"])
    for key, env in (
        ("store_raw_json", "FORDLOGGER_STORE_RAW_JSON"),
        ("geocoding", "FORDLOGGER_GEOCODING"),
    ):
        value = _env_bool(env)
        if value is not None:
            cfg[key] = value
    return cfg


def default_config() -> dict:
    """Defaults plus environment overrides, without reading any file."""
    return apply_env_overrides(json.loads(json.dumps(DEFAULT_CONFIG)))


def load_config(path: str = "config.json") -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p}")
    cfg = {**json.loads(json.dumps(DEFAULT_CONFIG)), **json.loads(p.read_text())}
    return apply_env_overrides(cfg)
