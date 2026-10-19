import json
import logging
import os
from pathlib import Path

log = logging.getLogger("fordlogger")

DEFAULT_CONFIG = {
    "db_host": "db",
    "db_port": 5432,
    "db_name": "fordlogger",
    "db_user": "fordlogger",
    "db_password": "fordlogger",
    "sleep_after_minutes": 30,
    "store_raw_json": True,
    "geocoding": True,
    "providers": [],
}

INTERVALS = {
    "driving": 60,
    "charging": 60,
    "parked": 120,
    "sleeping": 300,
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
