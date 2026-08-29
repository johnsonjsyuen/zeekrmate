from ..api import FordAPI
from ..models import Position, Vehicle
from .base import VehicleProvider


class FordProvider(VehicleProvider):
    name = "ford"

    def __init__(
        self,
        cfg: dict | None = None,
        *,
        client: FordAPI | None = None,
    ):
        if client is None:
            if cfg is None:
                raise TypeError("client or cfg is required")
            client = FordAPI(cfg)
        self.client = client

    def get_vehicles(self) -> list[Vehicle]:
        return self.client.get_garage()

    def get_telemetry(self) -> list[Position]:
        return self.client.get_telemetry()
