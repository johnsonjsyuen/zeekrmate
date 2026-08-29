import logging
import os
from pathlib import Path

from .base import VehicleProvider
from .ford import FordProvider

log = logging.getLogger("fordlogger")


def create_providers(cfg: dict) -> list[VehicleProvider]:
    """Create Ford providers for discovered per-VIN or legacy token files."""
    token_dir = Path(os.environ.get("FORDLOGGER_TOKEN_DIR", "."))
    vin_files = sorted(token_dir.glob("tokens_*.json"))
    if vin_files:
        providers = [
            FordProvider({**cfg, "token_file": str(token_file)})
            for token_file in vin_files
        ]
        log.info(
            "Found %d token file(s): %s",
            len(providers),
            [str(token_file) for token_file in vin_files],
        )
        return providers

    legacy = Path(cfg.get("token_file", "tokens.json"))
    if legacy.exists():
        log.info("Using legacy token file: %s", legacy)
        return [FordProvider(cfg)]
    return []
