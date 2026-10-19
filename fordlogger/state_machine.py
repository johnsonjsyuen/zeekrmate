"""Per-vehicle state machine: driving / charging / parked / sleeping.

Signals, in priority order:

1. explicit provider flags ``Position.is_driving`` / ``Position.is_charging``
   (when not None);
2. generic fallback: ``speed_kmh > 1`` -> driving, ``charge_power_kw > 0`` ->
   charging;
3. nothing known -> the signal is "unknown". An unknown signal never ends an
   active (driving/charging) state on its own.

Driving wins over charging. Leaving DRIVING/CHARGING for PARKED needs
``exit_debounce`` consecutive non-active readings; the change is dated at the
first of them.

Clocks: ``transition`` only compares provider timestamps (``pos.ts``) with each
other; ``tick`` only compares observation times (``observed_at``) with each
other and projects the result onto the provider clock. The two are never
subtracted from one another.
"""
import logging
import math
from datetime import datetime, timedelta
from enum import Enum

from .models import Position

log = logging.getLogger("fordlogger")

DRIVING_SPEED_KMH = 1.0  # GPS noise reports tiny non-zero speeds when parked


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


class StateMachine:
    def __init__(self, vin: str, sleep_after_minutes: float = 30, exit_debounce: int = 2):
        self.vin = vin
        self.state = VehicleState.PARKED
        self.sleep_after_s = sleep_after_minutes * 60
        self.exit_debounce = max(1, int(exit_debounce))
        # Provider-clock timestamp of the last state change (None until known).
        self.last_change_ts: datetime | None = None
        # Provider-clock timestamp of the last driving/charging activity.
        self._last_significant_change: datetime | None = None
        self._pending_exit = 0
        self._pending_exit_ts: datetime | None = None
        # Last fresh (non-duplicate) reading: provider ts and observation time.
        self.last_fresh_ts: datetime | None = None
        self.last_fresh_observed: datetime | None = None

    def restore(self, state: VehicleState, since_ts=None, last_ts=None, last_observed=None):
        """Seed the machine from persisted data after a restart."""
        self.state = state
        self.last_change_ts = since_ts
        self.last_fresh_ts = last_ts
        self.last_fresh_observed = last_observed
        self._last_significant_change = last_ts or since_ts

    def transition(self, pos: Position) -> tuple[VehicleState, bool]:
        """Feed a fresh reading. Returns (new_state, changed)."""
        old = self.state
        now = pos.ts
        if self._last_significant_change is None:
            self._last_significant_change = now

        driving, charging = classify(pos)
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
        return candidate, changed

    def tick(self, observed_at: datetime, stale_after_s: float) -> tuple[VehicleState, bool]:
        """Evaluate timeouts when no fresh reading arrived (frozen snapshot).

        * DRIVING/CHARGING with no fresh reading for ``stale_after_s`` ends at
          the last fresh reading (or the first pending non-active one).
        * PARKED goes to SLEEPING once ``sleep_after`` has elapsed since the
          last activity, counting the time without fresh readings.
        """
        if self.last_fresh_observed is None or self.last_fresh_ts is None:
            return self.state, False
        idle = (observed_at - self.last_fresh_observed).total_seconds()
        old = self.state
        if old in ACTIVE_STATES and idle > stale_after_s:
            end_ts = self._pending_exit_ts or self.last_fresh_ts
            self._clear_pending()
            log.info("No fresh data for %s for %ds; ending %s", self.vin, idle, old.value)
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
            log.info("State change %s: %s -> %s", self.vin, old.value, new.value)
            self.last_change_ts = ts
        self.state = new
        return changed
