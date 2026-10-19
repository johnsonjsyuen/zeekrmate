from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..models import Position, Vehicle


@dataclass(frozen=True)
class ProviderCapabilities:
    journey_history: bool = False
    trip_trackpoints: bool = False


class VehicleProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Stable provider name used for diagnostics and attribution."""

    @abstractmethod
    def get_vehicles(self) -> list[Vehicle]:
        """Return vehicles available through this provider."""

    @abstractmethod
    def get_telemetry(self) -> list[Position]:
        """Return the latest vehicle telemetry."""

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()
