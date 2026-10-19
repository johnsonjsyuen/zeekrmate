import logging
from typing import Callable

from .base import VehicleProvider

log = logging.getLogger("fordlogger")

ProviderBuilder = Callable[[dict], VehicleProvider]

# Provider type -> builder. Provider packages register here (e.g. "zeekr").
PROVIDER_BUILDERS: dict[str, ProviderBuilder] = {}


def create_providers(cfg: dict) -> list[VehicleProvider]:
    """Create one provider per entry in cfg["providers"]."""
    providers: list[VehicleProvider] = []
    for entry in cfg.get("providers", []):
        provider_type = entry.get("type")
        builder = PROVIDER_BUILDERS.get(provider_type)
        if builder is None:
            raise ValueError(f"Unknown provider type: {provider_type!r}")
        providers.append(builder({**cfg, **entry}))
    log.info("Configured %d provider(s)", len(providers))
    return providers
