"""Polling loop: provider boundary, deduplication, persistence, transitions.

Provider boundary policy (``Poller.normalise``):

* ``vin`` must be a non-empty string, otherwise the reading is skipped.
* ``ts`` must be a timezone-aware ``datetime``. Naive timestamps are
  **rejected** (logged and skipped), not assumed UTC: a naive local time read
  as UTC silently shifts trips by the UTC offset. Aware values are converted
  to UTC. Readings more than ``future_tolerance_s`` ahead of the observation
  time are skipped as well, because they would poison the ordering check.
* ``observed_at`` defaults to the poll's wall-clock time (UTC).
* Numeric fields are coerced to ``float``; strings that do not parse,
  booleans, NaN and infinities become ``None``.
"""
import collections
import dataclasses
import logging
import math
import time
import typing
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import INTERVALS, mask_vin
from .models import Position, Vehicle
from .state_machine import StateMachine, VehicleState, ACTIVE_STATES
from . import db
from . import geocoder
from .trip_detector import finalize_trip
from .charge_detector import finalize_charge_session

log = logging.getLogger("fordlogger")

MAX_BACKOFF_S = 600
MAX_FINALIZE_ATTEMPTS = 10
RECENT_SNAPSHOTS = 8  # snapshot keys remembered per VIN for deduplication


def _float_fields() -> list[str]:
    hints = typing.get_type_hints(Position)
    return [
        name for name, hint in hints.items()
        if name not in ("id",) and float in typing.get_args(hint)
    ]


def _str_fields() -> list[str]:
    hints = typing.get_type_hints(Position)
    return [
        name for name, hint in hints.items()
        if name not in ("vin",) and str in typing.get_args(hint)
    ]


FLOAT_FIELDS = _float_fields()
STR_FIELDS = _str_fields()
BOOL_FIELDS = ("is_driving", "is_charging")


def coerce_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def coerce_bool(value) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
    return None


def _aware(value) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None \
        and value.tzinfo.utcoffset(value) is not None


@dataclass
class PollResult:
    db_ok: bool = True
    providers_ok: int = 0
    providers_failed: int = 0
    positions_stored: int = 0
    position_errors: int = 0

    @property
    def failed_cycle(self) -> bool:
        """True when nothing useful could be done this cycle (drives backoff)."""
        return not self.db_ok or (self.providers_failed > 0 and self.providers_ok == 0)

    @property
    def ok(self) -> bool:
        """Strict success, used by ``--once``: every provider worked and data was stored."""
        return (
            self.db_ok
            and self.providers_failed == 0
            and self.position_errors == 0
            and self.positions_stored > 0
        )


class Poller:
    def __init__(self, cfg: dict, conn, providers):
        self.cfg = cfg
        self.conn = conn
        self.providers = providers if isinstance(providers, list) else [providers]
        self._db_failures = 0
        self.machines: dict[str, StateMachine] = {}
        self._drive_start: dict[str, datetime] = {}
        self._charge_start: dict[str, datetime] = {}
        # Last processed reading per VIN: (ts, snapshot_key).
        self._last_seen: dict[str, tuple[datetime, str | None]] = {}
        # Recently stored snapshot keys per VIN: an A, B, A sequence (e.g. one
        # odd payload between two copies of the same frozen snapshot) does
        # not store A twice.
        self._recent_keys: dict[str, collections.deque] = {}
        self._known_vins: set[str] = set()
        # Last successful vehicle-list refresh per provider index (time.time()).
        self._last_vehicle_refresh: dict[int, float] = {}
        self.garage_interval_s = 3600  # refresh the vehicle list every hour
        polling = cfg.get("polling") or {}
        self.intervals = dict(INTERVALS)
        for state, seconds in polling.items():
            if state in INTERVALS:
                self.intervals[state] = max(1, int(seconds))
            else:
                log.warning("Ignoring unknown polling state %r in config", state)
        self.stale_after_s = float(cfg.get("stale_after_minutes", 15)) * 60
        self.drive_gap_s = float(cfg.get("drive_gap_minutes", 30)) * 60
        self.charge_gap_s = float(cfg.get("charge_gap_minutes", 180)) * 60
        self.exit_debounce = int(cfg.get("state_exit_debounce", 2))
        self.trip_prestart_window_s = float(cfg.get("trip_prestart_window_s", 900))
        self.dc_threshold_kw = float(cfg.get("dc_threshold_kw", 22.0))
        self.future_tolerance_s = 300
        self._pending_finalize: list[dict] = []
        self._geocode_queue: list[tuple[str, object]] = []
        self.consecutive_failures = 0
        self._stop = False

    # ------------------------------------------------------------------ state

    def _get_machine(self, vin: str) -> StateMachine:
        if vin in self.machines:
            return self.machines[vin]
        sm = StateMachine(
            vin,
            sleep_after_minutes=self.cfg.get("sleep_after_minutes", 30),
            exit_debounce=self.exit_debounce,
            drive_gap_s=self.drive_gap_s,
            charge_gap_s=self.charge_gap_s,
        )
        saved_state = db.get_latest_state(self.conn, vin)
        latest = db.get_latest_position(self.conn, vin)
        state = VehicleState.PARKED
        try:
            if saved_state:
                state = VehicleState(saved_state)
        except ValueError:
            log.warning("Ignoring unknown saved state %r for %s", saved_state, mask_vin(vin))
        since = None
        if state in ACTIVE_STATES:
            since = db.get_last_state_change(self.conn, vin, state.value)
            if state == VehicleState.DRIVING and since is not None:
                self._drive_start[vin] = self._trip_start(vin, since)
            elif state == VehicleState.CHARGING and since is not None:
                self._charge_start[vin] = self._charge_start_anchor(vin, since)
        last_ts = latest["ts"] if latest else None
        last_observed = latest.get("observed_at") if latest else None
        if latest:
            self._last_seen[vin] = (latest["ts"], latest.get("snapshot_key"))
            self._remember_key(vin, latest.get("snapshot_key"))
        if saved_state or latest:
            sm.restore(
                state, since_ts=since, last_ts=last_ts, last_observed=last_observed,
                last_odometer=latest.get("odometer_km") if latest else None,
                last_lat=latest.get("lat") if latest else None,
                last_lon=latest.get("lon") if latest else None,
                last_charging=(latest.get("state") == VehicleState.CHARGING.value) if latest else None,
            )
            log.info("Restored state for %s from DB: %s (since %s)", mask_vin(vin), state.value, since)
        self.machines[vin] = sm
        return sm

    def _remember_key(self, vin: str, key):
        if key is not None:
            self._recent_keys.setdefault(vin, collections.deque(maxlen=RECENT_SNAPSHOTS)).append(key)

    def _ensure_db(self):
        """Reconnect to PostgreSQL if the connection is broken."""
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT 1")
            self._db_failures = 0
            return True
        except Exception:
            self._db_failures += 1
            log.warning("DB connection lost; attempting reconnect (%d)", self._db_failures)
            try:
                self.conn.close()
            except Exception:
                pass
            try:
                self.conn = db.connect(self.cfg)
                db.ensure_schema(self.conn)
                log.info("DB reconnected successfully")
                self._db_failures = 0
                return True
            except Exception as e:
                log.error("DB reconnect failed: %s", e)
                return False

    # --------------------------------------------------------------- boundary

    def normalise(self, pos, observed_at: datetime) -> Position | None:
        """Validate and coerce a provider reading; None means skip it."""
        vin = getattr(pos, "vin", None)
        if not isinstance(vin, str) or not vin.strip():
            log.warning("Skipping reading without a valid VIN")
            return None
        vin = vin.strip()
        ts = getattr(pos, "ts", None)
        if not _aware(ts):
            log.warning(
                "Skipping reading for %s: timestamp %r is not timezone-aware", mask_vin(vin), ts
            )
            return None
        ts = ts.astimezone(timezone.utc)
        observed = pos.observed_at if _aware(pos.observed_at) else observed_at
        observed = observed.astimezone(timezone.utc)
        if (ts - observed).total_seconds() > self.future_tolerance_s:
            log.warning(
                "Skipping reading for %s: timestamp %s is ahead of observation time %s",
                mask_vin(vin), ts.isoformat(), observed.isoformat(),
            )
            return None

        changes = {"vin": vin, "ts": ts, "observed_at": observed}
        for name in FLOAT_FIELDS:
            value = getattr(pos, name)
            if value is None or (type(value) is float and math.isfinite(value)):
                continue
            coerced = coerce_float(value)
            if coerced is None:
                log.debug("Dropping non-numeric %s=%r for %s", name, value, mask_vin(vin))
            changes[name] = coerced
        for name in BOOL_FIELDS:
            value = getattr(pos, name)
            if value is not None and not isinstance(value, bool):
                changes[name] = coerce_bool(value)
        for name in STR_FIELDS:
            value = getattr(pos, name)
            if value is not None and not isinstance(value, str):
                changes[name] = str(value)
        return dataclasses.replace(pos, **changes)

    # ------------------------------------------------------------------ cycle

    def poll_once(self) -> PollResult:
        """Execute a single poll cycle for all providers. Never raises for
        provider, data or per-position database errors."""
        result = PollResult()
        if not self._ensure_db():
            result.db_ok = False
            return result

        self._run_pending_finalizations()
        observed = datetime.now(timezone.utc)
        now = time.time()
        fresh: set[str] = set()

        for index, provider in enumerate(self.providers):
            provider_ok = True
            try:
                provider_name = provider.name
            except Exception:
                provider_name = type(provider).__name__
                log.warning("Provider name unavailable; using %s", provider_name)

            last_refresh = self._last_vehicle_refresh.get(index)
            if last_refresh is None or now - last_refresh >= self.garage_interval_s:
                try:
                    vehicles = provider.get_vehicles() or []
                    for v in vehicles:
                        db.upsert_vehicle(self.conn, v)
                        self._known_vins.add(v.vin)
                    if vehicles:
                        log.info("Provider %s: %d vehicle(s)", provider_name, len(vehicles))
                    else:
                        log.warning("Provider %s returned no vehicles", provider_name)
                    self._last_vehicle_refresh[index] = now
                except Exception as e:
                    provider_ok = False
                    log.error("Provider %s vehicle list error: %s", provider_name, e)

            try:
                positions = provider.get_telemetry() or []
            except Exception as e:
                log.error("Provider %s telemetry error: %s", provider_name, e, exc_info=True)
                result.providers_failed += 1
                continue

            for pos in positions:
                try:
                    vin = self._process_position(pos, observed)
                    if vin:
                        fresh.add(vin)
                        result.positions_stored += 1
                except Exception as e:
                    result.position_errors += 1
                    log.error(
                        "Provider %s: failed to process reading for %s: %s",
                        provider_name, mask_vin(getattr(pos, "vin", None)), e, exc_info=True,
                    )

            if provider_ok:
                result.providers_ok += 1
            else:
                result.providers_failed += 1

        for vin in list(self.machines):
            if vin not in fresh:
                try:
                    self._tick(vin, observed)
                except Exception as e:
                    log.error("Timeout evaluation failed for %s: %s", mask_vin(vin), e, exc_info=True)

        self._run_geocoding()
        return result

    def _process_position(self, raw_pos, observed: datetime) -> str | None:
        """Process one reading. Returns the VIN when a row was stored."""
        pos = self.normalise(raw_pos, observed)
        if pos is None:
            return None
        vin = pos.vin
        sm = self._get_machine(vin)

        last = self._last_seen.get(vin)
        if last is not None:
            last_ts, last_key = last
            if pos.snapshot_key is not None:
                if pos.snapshot_key == last_key or pos.snapshot_key in self._recent_keys.get(vin, ()):
                    log.debug("Skipping %s: snapshot unchanged (%s)", mask_vin(vin), pos.snapshot_key)
                    return None
            elif pos.ts == last_ts:
                log.debug("Skipping %s: data unchanged (ts=%s)", mask_vin(vin), pos.ts)
                return None
            if pos.ts < last_ts:
                log.debug("Skipping %s: reading at %s is older than %s", mask_vin(vin), pos.ts, last_ts)
                return None

        if vin not in self._known_vins:
            db.ensure_vehicle(self.conn, vin)
            self._known_vins.add(vin)

        if not self.cfg.get("store_raw_json", True):
            pos.raw_json = None

        # A long gap since the last fresh reading (restart, DB/API outage)
        # ends a drive at the last reading before this one is evaluated.
        gap_old = sm.state
        gap_state, gap_changed = sm.close_gap(pos)
        if gap_changed:
            self._handle_transition(vin, gap_old, gap_state, sm.last_change_ts)

        saved = dict(sm.__dict__)
        old_state = sm.state
        try:
            new_state, changed = sm.transition(pos)
            pos.state = new_state.value
            # The position and its state change are stored atomically: a lost
            # state row would otherwise let a restart restore a finished drive
            # and record it a second time.
            with self.conn:
                pos_id = db.insert_position(self.conn, pos)
                if changed:
                    db.insert_state(self.conn, vin, sm.last_change_ts, new_state.value, old_state.value)
        except Exception:
            # Nothing was persisted: roll the state machine back so the same
            # reading is processed again on the next cycle.
            sm.__dict__.clear()
            sm.__dict__.update(saved)
            raise
        self._last_seen[vin] = (pos.ts, pos.snapshot_key)
        self._remember_key(vin, pos.snapshot_key)

        log.info(
            "Position #%d %s | SoC=%s%% | Range=%skm | Odo=%skm | State=%s",
            pos_id, mask_vin(vin), pos.soc_pct, pos.range_km, pos.odometer_km, new_state.value,
        )
        if changed:
            self._handle_transition(vin, old_state, new_state, sm.last_change_ts,
                                    state_recorded=True)
        return vin

    def _tick(self, vin: str, observed: datetime):
        sm = self.machines[vin]
        old = sm.state
        new, changed = sm.tick(observed, self.stale_after_s)
        if changed:
            self._handle_transition(vin, old, new, sm.last_change_ts)

    # ------------------------------------------------------------ transitions

    def _trip_start(self, vin: str, ts: datetime) -> datetime:
        """C10: anchor the trip at the last pre-drive reading when it is recent."""
        try:
            prev = db.get_position_before(self.conn, vin, ts)
        except Exception as e:
            log.warning("Could not look up pre-drive reading for %s: %s", mask_vin(vin), e)
            return ts
        if prev and (ts - prev["ts"]).total_seconds() <= self.trip_prestart_window_s:
            return prev["ts"]
        return ts

    def _charge_start_anchor(self, vin: str, ts: datetime) -> datetime:
        """E2E-10: like trips, start a charge at the last idle reading before
        it (when recent), so the session's start SoC is the pre-charge value
        and the start is not late by a whole poll interval. Never anchors on
        a driving reading (a drive straight into a charger ends at ``ts``)."""
        try:
            prev = db.get_position_before(self.conn, vin, ts)
        except Exception as e:
            log.warning("Could not look up pre-charge reading for %s: %s", mask_vin(vin), e)
            return ts
        if prev and prev.get("state") in (VehicleState.PARKED.value, VehicleState.SLEEPING.value) \
                and (ts - prev["ts"]).total_seconds() <= self.trip_prestart_window_s:
            return prev["ts"]
        return ts

    def _handle_transition(self, vin: str, old: VehicleState, new: VehicleState, ts: datetime,
                           state_recorded: bool = False):
        # Update in-memory bookkeeping first so a DB error cannot lose a drive.
        if new == VehicleState.DRIVING:
            self._drive_start[vin] = self._trip_start(vin, ts)
        elif old == VehicleState.DRIVING:
            start = self._drive_start.pop(vin, None)
            if start is not None:
                self._pending_finalize.append(
                    {"kind": "trip", "vin": vin, "start": start, "end": ts, "attempts": 0})
            else:
                log.warning("Drive end for %s without a known start; trip not recorded", mask_vin(vin))

        if new == VehicleState.CHARGING:
            self._charge_start[vin] = self._charge_start_anchor(vin, ts)
        elif old == VehicleState.CHARGING:
            start = self._charge_start.pop(vin, None)
            if start is not None:
                self._pending_finalize.append(
                    {"kind": "charge", "vin": vin, "start": start, "end": ts, "attempts": 0})
            else:
                log.warning("Charge end for %s without a known start; session not recorded", mask_vin(vin))

        if not state_recorded:
            # Queued ahead of the trip/charge jobs and retried like them.
            self._pending_finalize.insert(0, {
                "kind": "state", "vin": vin, "ts": ts, "state": new.value,
                "prev": old.value, "attempts": 0})
        self._run_pending_finalizations()

    def _run_pending_finalizations(self):
        remaining = []
        for job in self._pending_finalize:
            try:
                if job["kind"] == "state":
                    db.insert_state(self.conn, job["vin"], job["ts"], job["state"], job["prev"])
                    continue
                if job["kind"] == "trip":
                    obj = finalize_trip(self.conn, job["vin"], job["start"], job["end"])
                else:
                    obj = finalize_charge_session(
                        self.conn, job["vin"], job["start"], job["end"],
                        dc_threshold_kw=self.dc_threshold_kw,
                    )
                if obj is not None and self.cfg.get("geocoding", True):
                    self._geocode_queue.append((job["kind"], obj))
            except Exception as e:
                job["attempts"] += 1
                if job["attempts"] >= MAX_FINALIZE_ATTEMPTS:
                    log.error("Giving up on %s finalisation for %s: %s",
                              job["kind"], mask_vin(job["vin"]), e)
                else:
                    log.error("%s finalisation failed for %s (will retry): %s",
                              job["kind"].capitalize(), mask_vin(job["vin"]), e, exc_info=True)
                    remaining.append(job)
        self._pending_finalize = remaining

    def _run_geocoding(self):
        """Fill addresses after the cycle's data is stored; failures are non-fatal."""
        queue, self._geocode_queue = self._geocode_queue, []
        for kind, obj in queue:
            try:
                if kind == "trip":
                    geocoder.geocode_trip(self.conn, obj)
                else:
                    geocoder.geocode_charge_session(self.conn, obj)
            except Exception as e:
                log.warning("Geocoding %s #%s failed: %s", kind, getattr(obj, "id", "?"), e)

    # ------------------------------------------------------------------- loop

    def get_interval(self) -> int:
        """Return the shortest polling interval across all vehicles."""
        if not self.machines:
            return self.intervals["parked"]
        return min(self.intervals[sm.state.value] for sm in self.machines.values())

    def next_interval(self) -> int:
        """Polling interval, with exponential backoff after failed cycles."""
        try:
            base = self.get_interval()
        except Exception:
            base = self.intervals["parked"]
        if self.consecutive_failures <= 0:
            return base
        backoff = base * (2 ** min(self.consecutive_failures, 16))
        return int(min(backoff, max(MAX_BACKOFF_S, base)))

    def stop(self):
        """Ask run_forever to exit after the current cycle/sleep."""
        self._stop = True

    def _sleep(self, seconds: float):
        deadline = time.monotonic() + seconds
        while not self._stop:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(1.0, remaining))

    def run_forever(self):
        """Main polling loop with adaptive intervals; one bad cycle never ends it."""
        log.info("Polling daemon started")
        while not self._stop:
            try:
                failed = self.poll_once().failed_cycle
            except Exception as e:
                log.error("Poll cycle failed: %s", e, exc_info=True)
                failed = True
            self.consecutive_failures = self.consecutive_failures + 1 if failed else 0
            if self._stop:
                break
            interval = self.next_interval()
            if failed:
                log.warning("Poll cycle failed (%d in a row); next poll in %ds",
                            self.consecutive_failures, interval)
            else:
                log.info("Next poll in %ds", interval)
            self._sleep(interval)
        log.info("Polling daemon stopped")
