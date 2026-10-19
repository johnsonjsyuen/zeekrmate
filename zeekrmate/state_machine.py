"""Per-vehicle state machine: driving / charging / parked / sleeping.

Signals, in priority order:

1. explicit provider flags ``Position.is_driving`` / ``Position.is_charging``
   (when not None);
2. generic fallback: ``speed_kmh > 1`` -> driving, ``charge_power_kw > 0`` ->
   charging;
3. generic motion inference (driving only, used when neither ``is_driving``
   nor ``speed_kmh`` is known), between consecutive fresh readings of the same
   VIN no more than ``drive_gap`` apart: the odometer increased by
   at least ``MOTION_ODOMETER_KM`` or the position moved more than
   ``MOTION_DISTANCE_M`` -> driving; both known and unchanged -> not driving.
   This needs no provider-specific driving field, so trips are recorded even
   when the provider only reports odometer/position;
4. nothing known -> the signal is "unknown". An unknown signal never ends an
   active (driving/charging) state on its own.

Driving wins over charging. Any driving/charging signal leaves PARKED or
SLEEPING. Leaving DRIVING/CHARGING for PARKED needs ``exit_debounce``
consecutive non-active readings; the change is dated at the first of them.

Gaps: if more than ``drive_gap`` passed since the last fresh reading (process
restart, DB outage, API outage), an active DRIVING state is ended at the last
fresh reading before the new reading is evaluated (``close_gap``): the drive
cannot be assumed to have continued through the gap. CHARGING is kept across
gaps of up to ``charge_gap`` as long as the new reading still shows charging,
because charge sessions are long and some APIs refresh slowly while charging.

Clocks: ``transition`` only compares provider timestamps (``pos.ts``) with each
other; ``tick`` only compares observation times (``observed_at``) with each
other and projects the result onto the provider clock. The two are never
subtracted from one another.
"""
import logging
import math
from datetime import datetime, timedelta
from enum import Enum

from .config import mask_vin
from .models import Position

log = logging.getLogger("zeekrmate")

DRIVING_SPEED_KMH = 1.0  # GPS noise reports tiny non-zero speeds when parked
MOTION_ODOMETER_KM = 0.1  # odometer increase between readings that means driving
MOTION_DISTANCE_M = 200.0  # position change between readings that means driving
DEFAULT_DRIVE_GAP_S = 30 * 60
DEFAULT_CHARGE_GAP_S = 3 * 3600


class VehicleState(str, Enum):
    DRIVING = "driving"
    CHARGING = "charging"
    PARKED = "parked"
    SLEEPING = "sleeping"


ACTIVE_STATES = (VehicleState.DRIVING, VehicleState.CHARGING)


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def classify(pos: Position) -> tuple[bool | None, bool | None]:
    """Return (driving, charging); each is True/False, or None when unknown."""
    if pos.is_driving is not None:
        driving = bool(pos.is_driving)
    else:
        speed = _num(pos.speed_kmh)
        driving = None if speed is None else speed > DRIVING_SPEED_KMH
    if pos.is_charging is not None:
        charging = bool(pos.is_charging)
    else:
        power = _num(pos.charge_power_kw)
        charging = None if power is None else power > 0
    return driving, charging


def distance_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in metres (haversine)."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


class StateMachine:
    def __init__(self, vin: str, sleep_after_minutes: float = 30, exit_debounce: int = 2,
                 drive_gap_s: float = DEFAULT_DRIVE_GAP_S,
                 charge_gap_s: float = DEFAULT_CHARGE_GAP_S):
        self.vin = vin
        self.state = VehicleState.PARKED
        self.sleep_after_s = sleep_after_minutes * 60
        self.exit_debounce = max(1, int(exit_debounce))
        self.drive_gap_s = float(drive_gap_s)
        self.charge_gap_s = max(float(charge_gap_s), self.drive_gap_s)
        # Provider-clock timestamp of the last state change (None until known).
        self.last_change_ts: datetime | None = None
        # Provider-clock timestamp of the last driving/charging activity.
        self._last_significant_change: datetime | None = None
        self._pending_exit = 0
        self._pending_exit_ts: datetime | None = None
        # Last fresh (non-duplicate) reading: provider ts and observation time.
        self.last_fresh_ts: datetime | None = None
        self.last_fresh_observed: datetime | None = None
        # Odometer/position of the last fresh reading (motion inference) and
        # whether it showed charging (charge gap tolerance).
        self._last_odometer: float | None = None
        self._last_lat: float | None = None
        self._last_lon: float | None = None
        self._last_charging: bool | None = None

    def restore(self, state: VehicleState, since_ts=None, last_ts=None, last_observed=None,
                last_odometer=None, last_lat=None, last_lon=None, last_charging=None):
        """Seed the machine from persisted data after a restart."""
        self.state = state
        self.last_change_ts = since_ts
        self.last_fresh_ts = last_ts
        self.last_fresh_observed = last_observed
        self._last_significant_change = last_ts or since_ts
        self._last_odometer = _num(last_odometer)
        self._last_lat = _num(last_lat)
        self._last_lon = _num(last_lon)
        self._last_charging = last_charging

    # ---------------------------------------------------------------- signals

    def gap_s(self, pos: Position) -> float | None:
        """Time since the last fresh reading (None if there is none).

        The larger of the provider-clock gap and the observation-clock gap:
        each is computed on its own clock, they are never mixed.
        """
        if self.last_fresh_ts is None:
            return None
        gap = (pos.ts - self.last_fresh_ts).total_seconds()
        if pos.observed_at is not None and self.last_fresh_observed is not None:
            gap = max(gap, (pos.observed_at - self.last_fresh_observed).total_seconds())
        return gap

    def _moved(self, pos: Position, gap: float | None) -> bool | None:
        """Motion since the previous fresh reading: True/False, None if unknown."""
        if gap is None or gap > self.drive_gap_s:
            return None  # movement across a long gap says nothing about now
        known = False
        odo = _num(pos.odometer_km)
        if odo is not None and self._last_odometer is not None:
            known = True
            if odo - self._last_odometer >= MOTION_ODOMETER_KM - 1e-9:
                return True
        lat, lon = _num(pos.lat), _num(pos.lon)
        if None not in (lat, lon, self._last_lat, self._last_lon):
            known = True
            if distance_m(self._last_lat, self._last_lon, lat, lon) > MOTION_DISTANCE_M:
                return True
        return False if known else None

    def signals(self, pos: Position, gap: float | None = None) -> tuple[bool | None, bool | None]:
        """(driving, charging) for a fresh reading, including motion inference."""
        driving, charging = classify(pos)
        if driving is None:
            # Only when neither is_driving nor speed is known: speed describes
            # the instant of the reading, motion the interval before it.
            driving = self._moved(pos, gap)
        return driving, charging

    def close_gap(self, pos: Position) -> tuple[VehicleState, bool]:
        """End an active state that cannot have continued through a long gap.

        Call before ``transition`` with the same reading. DRIVING ends at the
        last fresh reading when the gap exceeds ``drive_gap``. CHARGING ends
        there too unless the new reading still shows charging and the gap is
        within ``charge_gap``. Returns (state, changed).
        """
        old = self.state
        gap = self.gap_s(pos)
        if old not in ACTIVE_STATES or gap is None or gap <= self.drive_gap_s:
            return old, False
        if old == VehicleState.CHARGING:
            _, charging = classify(pos)
            if charging and self._last_charging is not False and gap <= self.charge_gap_s:
                return old, False
        end_ts = self._pending_exit_ts or self.last_fresh_ts
        self._clear_pending()
        log.info("Gap of %ds before a fresh reading for %s; ending %s at the last reading",
                 gap, mask_vin(self.vin), old.value)
        return VehicleState.PARKED, self._apply(old, VehicleState.PARKED, end_ts)

    # ------------------------------------------------------------- transition

    def transition(self, pos: Position) -> tuple[VehicleState, bool]:
        """Feed a fresh reading. Returns (new_state, changed)."""
        old = self.state
        now = pos.ts
        if self._last_significant_change is None:
            self._last_significant_change = now

        driving, charging = self.signals(pos, self.gap_s(pos))
        if driving:
            candidate = VehicleState.DRIVING
        elif charging:
            candidate = VehicleState.CHARGING
        elif old == VehicleState.DRIVING and driving is None:
            candidate = old  # speed unknown: keep driving
        elif old == VehicleState.CHARGING and charging is None:
            candidate = old  # power unknown: keep charging
        elif old == VehicleState.SLEEPING:
            candidate = VehicleState.SLEEPING
        else:
            candidate = VehicleState.PARKED

        change_ts = now
        if old in ACTIVE_STATES and candidate not in ACTIVE_STATES:
            if self._pending_exit == 0:
                self._pending_exit_ts = now
            self._pending_exit += 1
            if self._pending_exit < self.exit_debounce:
                candidate = old
            else:
                change_ts = self._pending_exit_ts
                self._clear_pending()
        else:
            self._clear_pending()

        if candidate == VehicleState.PARKED and old == VehicleState.PARKED:
            if (now - self._last_significant_change).total_seconds() > self.sleep_after_s:
                candidate = VehicleState.SLEEPING

        changed = self._apply(old, candidate, change_ts)
        if changed:
            self._last_significant_change = now
        if driving or charging:
            self._last_significant_change = now

        self.last_fresh_ts = now
        self.last_fresh_observed = pos.observed_at or now
        self._last_odometer = _num(pos.odometer_km)
        self._last_lat, self._last_lon = _num(pos.lat), _num(pos.lon)
        self._last_charging = charging
        return candidate, changed

    def tick(self, observed_at: datetime, stale_after_s: float) -> tuple[VehicleState, bool]:
        """Evaluate timeouts when no fresh reading arrived (frozen snapshot).

        * DRIVING/CHARGING with no fresh reading for ``stale_after_s`` ends at
          the last fresh reading (or the first pending non-active one).
          CHARGING whose last fresh reading still showed charging is kept for
          up to ``charge_gap`` instead (slow snapshot refresh while charging).
        * PARKED goes to SLEEPING once ``sleep_after`` has elapsed since the
          last activity, counting the time without fresh readings.
        """
        if self.last_fresh_observed is None or self.last_fresh_ts is None:
            return self.state, False
        idle = (observed_at - self.last_fresh_observed).total_seconds()
        old = self.state
        limit = stale_after_s
        if old == VehicleState.CHARGING and self._last_charging and not self._pending_exit:
            limit = max(stale_after_s, self.charge_gap_s)
        if old in ACTIVE_STATES and idle > limit:
            end_ts = self._pending_exit_ts or self.last_fresh_ts
            self._clear_pending()
            log.info("No fresh data for %s for %ds; ending %s", mask_vin(self.vin), idle, old.value)
            return VehicleState.PARKED, self._apply(old, VehicleState.PARKED, end_ts)
        if old == VehicleState.PARKED and self._last_significant_change is not None:
            quiet = (self.last_fresh_ts - self._last_significant_change).total_seconds() + idle
            if quiet > self.sleep_after_s:
                ts = self.last_fresh_ts + timedelta(seconds=max(idle, 0))
                return VehicleState.SLEEPING, self._apply(old, VehicleState.SLEEPING, ts)
        return old, False

    def _clear_pending(self):
        self._pending_exit = 0
        self._pending_exit_ts = None

    def _apply(self, old: VehicleState, new: VehicleState, ts) -> bool:
        changed = new != old
        if changed:
            log.info("State change %s: %s -> %s", mask_vin(self.vin), old.value, new.value)
            self.last_change_ts = ts
        self.state = new
        return changed
