"""ZeekrProvider: read-only VehicleProvider backed by zeekr_ev_api."""

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from ...models import Position, Vehicle
from ..base import ProviderCapabilities, VehicleProvider
from .mapper import map_position
from .redact import mask_vin
from .session import ClientFactory, ZeekrAuthError, ZeekrProviderError, ZeekrSession
from .session_store import SessionStore
from .settings import ZeekrSettings, load_settings, resolve_state_dir, session_path

log = logging.getLogger("fordlogger")


def _clean_str(value) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()[:100]
    return None


class ZeekrProvider(VehicleProvider):
    def __init__(
        self,
        session: ZeekrSession,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self._session = session
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._vins: list[str] = []
        # Last ts per VIN: fallback when a payload's updateTime is unusable.
        self._last_ts: dict[str, datetime] = {}

    @property
    def name(self) -> str:
        return "zeekr"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(journey_history=True, trip_trackpoints=True)

    # ------------------------------------------------------------------
    def _vehicle_list(self) -> list:
        vehicles = self._session.call("get_vehicle_list") or []
        result = []
        for v in vehicles:
            vin = getattr(v, "vin", None)
            if not isinstance(vin, str) or not vin.strip():
                log.warning("Zeekr: skipping vehicle entry without a VIN")
                continue
            data = getattr(v, "data", None)
            result.append((vin.strip(), data if isinstance(data, dict) else {}))
        self._vins = [vin for vin, _ in result]
        return result

    def get_vehicles(self) -> list[Vehicle]:
        vehicles = []
        for vin, data in self._vehicle_list():
            # Vehicle list fields are not evidenced by the library or the HA
            # integration (library test payload: {"vin", "name"}). Only take
            # the model when a key literally named like a model is a string.
            model = _clean_str(data.get("modelName")) or _clean_str(data.get("model"))
            vehicles.append(Vehicle(vin=vin, make="Zeekr", model=model))
        log.info("Zeekr: %d vehicle(s): %s", len(vehicles),
                 ", ".join(mask_vin(v.vin) for v in vehicles) or "none")
        self._session.persist()
        return vehicles

    def _optional(self, method: str, vin: str):
        try:
            data = self._session.call(method, vin)
        except ZeekrAuthError:
            raise
        except ZeekrProviderError as e:
            log.warning("Zeekr: %s", e)
            return None
        return data if isinstance(data, dict) else None

    def _telemetry_for(self, vin: str) -> Position:
        status = self._session.call("get_vehicle_status", vin)
        if not isinstance(status, dict):
            raise ZeekrProviderError(f"get_vehicle_status [{mask_vin(vin)}] returned no data")
        charging_status = self._optional("get_vehicle_charging_status", vin)
        charging_limit = self._optional("get_vehicle_charging_limit", vin)
        pos = map_position(vin, status, charging_status, charging_limit,
                           observed_at=self._clock(), fallback_ts=self._last_ts.get(vin))
        self._last_ts[vin] = pos.ts
        return pos

    def get_telemetry(self) -> list[Position]:
        if not self._vins:
            self._vehicle_list()
        positions: list[Position] = []
        failures = 0
        for vin in list(self._vins):
            try:
                positions.append(self._telemetry_for(vin))
            except ZeekrAuthError:
                raise  # credentials problem: retrying other VINs cannot help
            except ZeekrProviderError as e:
                failures += 1
                log.warning("Zeekr: telemetry for %s failed: %s", mask_vin(vin), e)
            except Exception as e:  # never let one VIN drop the others
                failures += 1
                log.warning("Zeekr: telemetry for %s failed: %s", mask_vin(vin), type(e).__name__)
        self._session.persist()
        if failures and not positions:
            raise ZeekrProviderError(f"Zeekr telemetry failed for all {failures} vehicle(s)")
        return positions


def build_provider(
    cfg: dict,
    environ=None,
    client_factory: Optional[ClientFactory] = None,
    auth_exceptions: Optional[tuple] = None,
    api_exceptions: Optional[tuple] = None,
) -> ZeekrProvider:
    """Factory entry point. Reads secrets from the environment only.

    Does not touch the network: login happens on the first API call.
    """
    settings: ZeekrSettings = load_settings(environ)
    store = SessionStore(session_path(resolve_state_dir(cfg, environ)))
    session = ZeekrSession(settings, store, client_factory=client_factory,
                           auth_exceptions=auth_exceptions, api_exceptions=api_exceptions)
    return ZeekrProvider(session)
