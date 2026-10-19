"""Map Zeekr API payloads to fordlogger.models.Position.

PROVISIONAL — verify against your own 7X capture (scripts/dump_zeekr.py).

No field below has been checked against a real Australian Zeekr 7X. Every
mapping is taken from the evidence cited, which is either the zeekr_ev_api
library (MIT, v0.1.15 @ 4f22bd6) or the companion Home Assistant
integration (zeekr_homeassistant @ 268d6f8, path custom_components/zeekr_ev/).
The library's own tests (tests/test_client.py) only use placeholder payloads
such as {"status": "ok"}, so they provide no field evidence.

  "VS" = get_vehicle_status() data, "CS" = get_vehicle_charging_status() data,
  "CL" = get_vehicle_charging_limit() data, "EV" = VS.additionalVehicleStatus
  .electricVehicleStatus.

  Position field     | Source path                                         | Evidence (file:line)
  -------------------|-----------------------------------------------------|---------------------------------------------
  ts                 | VS.additionalVehicleStatus.climateStatus.updateTime | climate.py:210-217 (ms epoch ->
                     |   (ms epoch); if missing/unparsable: fallback_ts    |   datetime.fromtimestamp(ms/1000, tz=UTC))
                     |   (last valid ts for the VIN), else observed_at     |
  soc_pct            | EV.chargeLevel                 (%)                  | sensor.py:171-174
  range_km           | EV.distanceToEmptyOnBatteryOnly (km)                | sensor.py:185-188
  odometer_km        | VS.additionalVehicleStatus.maintenanceStatus        | sensor.py:199-202
                     |   .odometer                     (km)                |
  lat / lon          | VS.basicVehicleStatus.position.latitude/.longitude  | device_tracker.py:53,64 (float; 0/"" -> None)
  charge_power_kw    | CS.chargePower (kW); fallback VS.chargingStatus     | sensor.py:364; coordinator.py:490 (CS is
  charger_voltage    | CS.chargeVoltage (V)  "                             | sensor.py:340   merged into VS.chargingStatus)
  charger_current_a  | CS.chargeCurrent (A)  "                             | sensor.py:352
  charge_status      | raw chargerState code, EV.chargerState, else        | binary_sensor.py:101-105; switch.py:224-228
                     |   CS.chargerState (stored verbatim, e.g. "2")       |
  is_charging        | chargerState in {1, 2, 15}                          | binary_sensor.py:101-105 ("Charging Status")
                     |   (switch.py:227 says 1/2 charging, 25/26 stopped)  |   HINT ONLY: confirm codes from a capture
  plug_status        | raw EV.statusOfChargerConnection code (verbatim)    | binary_sensor.py:116-120 (int truthy = plugged)
  door_lock_status   | VS.additionalVehicleStatus.drivingSafetyStatus      | lock.py:33, lock.py:104 ("1" -> locked)
                     |   .centralLockingStatus: "1"->LOCKED, "0"->UNLOCKED |

Deliberately NOT mapped (left None; the full payload is kept in raw_json):
  is_driving      no clear definition. sensor.py:731 maps engineStatus
                  "engine-running" to a display label "Driving", but "ready"
                  is separate and an EV can be "on" while parked; needs a
                  capture taken while driving. Meanwhile the core state
                  machine infers driving from odometer/position changes.
  speed_kmh       no instantaneous speed field evidenced (runningStatus.avgSpeed
                  at sensor.py:251-253 is a trip average).
  tire_pressure_* tyreStatusDriver/Passenger/DriverRear/PassengerRear exist
                  (sensor.py:283-285, kPa) but the corner mapping for RHD cars
                  is ambiguous (sensor.py:66-88); verify on the AU 7X first.
  outside_temp_c, bat_temp_c, heading: no evidence. interiorTemp
                  (sensor.py:214-216) is cabin temperature, not outside.
  charging limit  CL.soc is value*10 (number.py:213-221); no Position field.

The mapper is total: it never raises, coerces numeric strings, and returns
None for anything missing or malformed.
"""

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ...models import Position

# HA binary_sensor.py:105. Provisional: confirm with a real capture.
CHARGING_STATES = frozenset({1, 2, 15})
LOCK_STATES = {"1": "LOCKED", "0": "UNLOCKED"}

# Payload timestamps further than this into the future are not trusted.
MAX_FUTURE_SKEW = timedelta(minutes=5)
# Anything below this is not a plausible ms-epoch value (2001-09-09).
MIN_PLAUSIBLE_MS = 1_000_000_000_000


def _dig(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def to_float(value: Any) -> Optional[float]:
    """Coerce int/float/numeric string to a finite float, else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            f = float(value)
        elif isinstance(value, str):
            s = value.strip()
            if not s:
                return None
            f = float(s)
        else:
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def to_int(value: Any) -> Optional[int]:
    f = to_float(value)
    if f is None or f != int(f):
        return None
    return int(f)


def _code(value: Any) -> Optional[str]:
    """Raw status code as a short string ("2"), or None."""
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    i = to_int(value)
    if i is not None:
        return str(i)
    s = str(value).strip()
    return s[:64] or None


def ms_epoch_to_utc(value: Any, now: Optional[datetime] = None) -> Optional[datetime]:
    """Zeekr ms-epoch timestamp -> tz-aware UTC datetime (None if implausible)."""
    ms = to_float(value)
    if ms is None or ms < MIN_PLAUSIBLE_MS:
        return None
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    now = now or datetime.now(timezone.utc)
    if dt - now > MAX_FUTURE_SKEW:
        return None
    return dt


def _coord(value: Any, limit: float) -> Optional[float]:
    f = to_float(value)
    if f is None or f == 0.0 or abs(f) > limit:
        return None
    return f


def _non_negative(value: Any) -> Optional[float]:
    f = to_float(value)
    return f if f is not None and f >= 0 else None


def jsonable(obj: Any) -> Any:
    """Deep copy that is guaranteed JSON-serialisable (no NaN/Infinity)."""
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return str(obj)


def snapshot_key(vin: str, raw: dict) -> str:
    canonical = json.dumps({"vin": vin, **raw}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _charger_state(vehicle_status: Any, charging_status: Any) -> Any:
    v = _dig(vehicle_status, "additionalVehicleStatus", "electricVehicleStatus", "chargerState")
    if v is None:
        v = _dig(charging_status, "chargerState")
    return v


def _charging_field(name: str, vehicle_status: Any, charging_status: Any) -> Optional[float]:
    v = to_float(_dig(charging_status, name))
    if v is None:
        v = to_float(_dig(vehicle_status, "chargingStatus", name))
    return v


def _map_fields(vin: str, vs: Any, cs: Any, observed_at: datetime) -> dict:
    ev = _dig(vs, "additionalVehicleStatus", "electricVehicleStatus")
    fields: dict = {}

    def put(name, fn):
        try:
            fields[name] = fn()
        except Exception:
            fields[name] = None

    put("ts", lambda: ms_epoch_to_utc(
        _dig(vs, "additionalVehicleStatus", "climateStatus", "updateTime"), observed_at))
    put("soc_pct", lambda: (lambda f: f if f is not None and 0 <= f <= 100 else None)(
        to_float(_dig(ev, "chargeLevel"))))
    put("range_km", lambda: _non_negative(_dig(ev, "distanceToEmptyOnBatteryOnly")))
    put("odometer_km", lambda: _non_negative(
        _dig(vs, "additionalVehicleStatus", "maintenanceStatus", "odometer")))
    put("lat", lambda: _coord(_dig(vs, "basicVehicleStatus", "position", "latitude"), 90.0))
    put("lon", lambda: _coord(_dig(vs, "basicVehicleStatus", "position", "longitude"), 180.0))
    put("charge_power_kw", lambda: _charging_field("chargePower", vs, cs))
    put("charger_voltage", lambda: _charging_field("chargeVoltage", vs, cs))
    put("charger_current_a", lambda: _charging_field("chargeCurrent", vs, cs))
    put("charge_status", lambda: _code(_charger_state(vs, cs)))
    put("is_charging", lambda: (lambda c: None if c is None else c in CHARGING_STATES)(
        to_int(_charger_state(vs, cs))))
    put("plug_status", lambda: _code(_dig(ev, "statusOfChargerConnection")))
    put("door_lock_status", lambda: LOCK_STATES.get(_code(
        _dig(vs, "additionalVehicleStatus", "drivingSafetyStatus", "centralLockingStatus")) or ""))
    if fields.get("lat") is None or fields.get("lon") is None:
        fields["lat"] = fields["lon"] = None
    return fields


def map_position(
    vin: Any,
    vehicle_status: Any,
    charging_status: Any = None,
    charging_limit: Any = None,
    observed_at: Optional[datetime] = None,
    fallback_ts: Optional[datetime] = None,
) -> Position:
    """Build one Position from the three read-only payloads. Never raises.

    ``ts`` is the payload's updateTime. When that is missing or unparsable,
    ``fallback_ts`` (the provider passes the last valid ts for the VIN) is
    used, and only without one the observation time: a poll-time ts would
    be newer than the next valid snapshot, which would then be skipped as
    "older than the last reading".
    """
    try:
        observed = observed_at if (observed_at and observed_at.tzinfo) else datetime.now(timezone.utc)
    except Exception:
        observed = datetime.now(timezone.utc)
    vin_s = "" if vin is None else str(vin)
    try:
        raw = {
            "vehicle_status": jsonable(vehicle_status),
            "charging_status": jsonable(charging_status),
            "charging_limit": jsonable(charging_limit),
        }
    except Exception:
        raw = {"vehicle_status": None, "charging_status": None, "charging_limit": None}
    try:
        key = snapshot_key(vin_s, raw)
    except Exception:
        key = None
    try:
        fields = _map_fields(vin_s, vehicle_status, charging_status, observed)
    except Exception:
        fields = {}
    if not (isinstance(fallback_ts, datetime) and fallback_ts.tzinfo is not None):
        fallback_ts = None
    ts = fields.pop("ts", None) or fallback_ts or observed
    return Position(
        ts=ts,
        vin=vin_s,
        observed_at=observed,
        snapshot_key=key,
        is_driving=None,
        raw_json=raw,
        **fields,
    )
