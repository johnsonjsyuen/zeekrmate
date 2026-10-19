import logging
from .models import Trip
from . import db
from . import geocoder
from .config import mask_vin

log = logging.getLogger("fordlogger")


def _first(rows, key):
    for r in rows:
        if r.get(key) is not None:
            return r[key]
    return None


def _last(rows, key):
    return _first(list(reversed(rows)), key)


def _non_negative(value, ndigits):
    if value is None:
        return None
    value = round(value, ndigits)
    return value if value >= 0 else None


def finalize_trip(conn, vin: str, drive_start_ts, drive_end_ts, geocoding: bool = False) -> Trip | None:
    """Create a trip summary from positions between start and end timestamps.

    A trailing reading that is already charging (drive straight into a
    charger) is not used as the trip end. Start/end values use the first/last
    non-null reading; a negative distance, SoC use or energy use is stored as
    NULL. Addresses are filled separately (geocoding=True does it inline).
    """
    positions = db.get_positions_since(conn, vin, drive_start_ts)
    drive_positions = [p for p in positions if p["ts"] <= drive_end_ts]
    while len(drive_positions) > 2 and drive_positions[-1].get("state") == "charging":
        drive_positions.pop()

    if len(drive_positions) < 2:
        log.warning("Too few positions for trip: %d", len(drive_positions))
        return None

    first = drive_positions[0]
    last = drive_positions[-1]

    duration_s = int((last["ts"] - first["ts"]).total_seconds())
    if duration_s < 60:
        log.debug("Trip too short (%ds), skipping", duration_s)
        return None

    start_odo = _first(drive_positions, "odometer_km")
    end_odo = _last(drive_positions, "odometer_km")
    distance = None
    if start_odo is not None and end_odo is not None:
        distance = _non_negative(end_odo - start_odo, 2)

    start_soc = _first(drive_positions, "soc_pct")
    end_soc = _last(drive_positions, "soc_pct")
    soc_used = None
    if start_soc is not None and end_soc is not None:
        soc_used = _non_negative(start_soc - end_soc, 1)

    energy_rem_start = _first(drive_positions, "energy_remaining_kwh")
    energy_rem_end = _last(drive_positions, "energy_remaining_kwh")
    energy_used = None
    if energy_rem_start is not None and energy_rem_end is not None:
        energy_used = _non_negative(energy_rem_start - energy_rem_end, 3)

    consumption = None
    if energy_used is not None and distance and distance > 0:
        consumption = round(energy_used / distance * 100, 2)

    speeds = [p["speed_kmh"] for p in drive_positions if p.get("speed_kmh") is not None]
    avg_speed = round(sum(speeds) / len(speeds), 1) if speeds else None
    max_speed = round(max(speeds), 1) if speeds else None

    temps = [p["outside_temp_c"] for p in drive_positions if p.get("outside_temp_c") is not None]
    outside_temp = round(sum(temps) / len(temps), 1) if temps else None

    trip = Trip(
        vin=vin,
        start_ts=first["ts"],
        end_ts=last["ts"],
        duration_s=duration_s,
        start_pos_id=first.get("id"),
        end_pos_id=last.get("id"),
        start_lat=first.get("lat"),
        start_lon=first.get("lon"),
        end_lat=last.get("lat"),
        end_lon=last.get("lon"),
        start_odometer_km=start_odo,
        end_odometer_km=end_odo,
        distance_km=distance,
        start_soc_pct=start_soc,
        end_soc_pct=end_soc,
        soc_used_pct=soc_used,
        energy_used_kwh=energy_used,
        consumption_kwh_per_100km=consumption,
        avg_speed_kmh=avg_speed,
        max_speed_kmh=max_speed,
        outside_temp_c=outside_temp,
    )

    trip_id = db.insert_trip(conn, trip)
    trip.id = trip_id
    if geocoding:
        try:
            geocoder.geocode_trip(conn, trip)
        except Exception as e:
            log.warning("Geocoding failed for trip: %s", e)
    log.info(
        "Trip saved #%d for %s: %.1fkm, %dmin, %.1f kWh/100km",
        trip_id,
        mask_vin(vin),
        distance or 0,
        duration_s // 60,
        consumption or 0,
    )
    return trip
