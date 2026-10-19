import logging
from .models import ChargeSession
from . import db
from . import geocoder
from .config import mask_vin
from .trip_detector import _first, _last, _non_negative

log = logging.getLogger("fordlogger")

DEFAULT_DC_THRESHOLD_KW = 22.0  # the AU Zeekr 7X has a 22 kW AC on-board charger


def finalize_charge_session(conn, vin: str, charge_start_ts, charge_end_ts,
                            geocoding: bool = False,
                            dc_threshold_kw: float = DEFAULT_DC_THRESHOLD_KW) -> ChargeSession | None:
    """Create a charge session summary from positions between start and end timestamps.

    Sessions whose peak power exceeds ``dc_threshold_kw`` are classed DC,
    otherwise AC. A negative SoC/energy gain is stored as NULL.
    """
    positions = db.get_positions_since(conn, vin, charge_start_ts)
    charge_positions = [p for p in positions if p["ts"] <= charge_end_ts]

    if len(charge_positions) < 2:
        log.warning("Too few positions for charge session: %d", len(charge_positions))
        return None

    first = charge_positions[0]
    last = charge_positions[-1]

    duration_s = int((last["ts"] - first["ts"]).total_seconds())
    if duration_s < 60:
        log.debug("Charge session too short (%ds), skipping", duration_s)
        return None

    start_soc = _first(charge_positions, "soc_pct")
    end_soc = _last(charge_positions, "soc_pct")
    soc_added = None
    if start_soc is not None and end_soc is not None:
        soc_added = _non_negative(end_soc - start_soc, 1)

    energy_start = _first(charge_positions, "energy_remaining_kwh")
    energy_end = _last(charge_positions, "energy_remaining_kwh")
    energy_added = None
    if energy_start is not None and energy_end is not None:
        energy_added = _non_negative(energy_end - energy_start, 3)

    powers = [p["charge_power_kw"] for p in charge_positions if p.get("charge_power_kw") is not None and p["charge_power_kw"] > 0]
    max_power = round(max(powers), 2) if powers else None
    avg_power = round(sum(powers) / len(powers), 2) if powers else None

    # Determine charge type from peak power (heuristic until providers report it)
    charge_type = None
    if max_power is not None:
        charge_type = "DC" if max_power > dc_threshold_kw else "AC"

    temps = [p["outside_temp_c"] for p in charge_positions if p.get("outside_temp_c") is not None]
    outside_temp = round(sum(temps) / len(temps), 1) if temps else None

    cs = ChargeSession(
        vin=vin,
        start_ts=first["ts"],
        end_ts=last["ts"],
        duration_s=duration_s,
        start_pos_id=first.get("id"),
        end_pos_id=last.get("id"),
        lat=first.get("lat"),
        lon=first.get("lon"),
        start_soc_pct=start_soc,
        end_soc_pct=end_soc,
        soc_added_pct=soc_added,
        energy_added_kwh=energy_added,
        max_power_kw=max_power,
        avg_power_kw=avg_power,
        charge_type=charge_type,
        outside_temp_c=outside_temp,
    )

    existing = db.overlapping_row(conn, "charge_sessions", vin, cs.start_ts, cs.end_ts)
    if existing is not None:
        log.warning("Charge session for %s overlaps session #%d; not recorded twice",
                    mask_vin(vin), existing)
        return None

    cs_id = db.insert_charge_session(conn, cs)
    cs.id = cs_id
    if geocoding:
        try:
            geocoder.geocode_charge_session(conn, cs)
        except Exception as e:
            log.warning("Geocoding failed for charge session: %s", e)
    log.info(
        "Charge session saved #%d for %s: %.1f%% -> %.1f%% (+%.1f kWh), %dmin, max %.1f kW",
        cs_id,
        mask_vin(vin),
        start_soc or 0,
        end_soc or 0,
        energy_added or 0,
        duration_s // 60,
        max_power or 0,
    )
    return cs
