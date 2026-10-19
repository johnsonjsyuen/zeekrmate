import logging
import time

import requests

log = logging.getLogger("zeekrmate")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "zeekrmate/0.1 (+https://github.com/johnsonjsyuen/zeekrmate)"
REQUEST_TIMEOUT_S = 5
MIN_REQUEST_GAP_S = 1.1  # Nominatim usage policy: at most 1 request/second
CACHE_DECIMALS = 4  # ~11 m; Nominatim policy requires caching results
_CACHE_MAX = 2048

_last_request_time = 0.0
_cache: dict[tuple[float, float], str | None] = {}


def clear_cache():
    _cache.clear()


def reverse_geocode(lat: float, lon: float) -> str | None:
    """Reverse geocode lat/lon to a short address via Nominatim.

    Results (including failures to find an address) are cached in memory by
    coordinates rounded to CACHE_DECIMALS. Network errors are not cached.
    Respects Nominatim's 1 request/second rate limit, also after failures.
    Returns 'Street, City, CC' or None on failure.
    """
    global _last_request_time

    if lat is None or lon is None:
        return None

    key = (round(float(lat), CACHE_DECIMALS), round(float(lon), CACHE_DECIMALS))
    if key in _cache:
        return _cache[key]

    elapsed = time.time() - _last_request_time
    if elapsed < MIN_REQUEST_GAP_S:
        time.sleep(MIN_REQUEST_GAP_S - elapsed)

    try:
        r = requests.get(
            NOMINATIM_URL,
            params={
                "lat": key[0],
                "lon": key[1],
                "format": "json",
                "zoom": 18,
                "addressdetails": 1,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_S,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        # No coordinates and no exception text: the requests error embeds the
        # full-precision request URL (lat=...&lon=...).
        status = getattr(getattr(e, "response", None), "status_code", None)
        log.warning("Geocoding failed (%s%s)", type(e).__name__,
                    f", HTTP {status}" if status else "")
        return None
    finally:
        _last_request_time = time.time()

    address = _format_address(data)
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = address
    return address


def _format_address(data: dict) -> str | None:
    addr = data.get("address", {}) or {}
    road = addr.get("road") or addr.get("pedestrian") or addr.get("footway") or ""
    house = addr.get("house_number") or ""
    city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality") or ""
    country = (addr.get("country_code") or "").upper()

    parts = []
    if road:
        parts.append(f"{road} {house}".strip())
    if city:
        parts.append(city)
    if country:
        parts.append(country)

    return ", ".join(parts) if parts else (data.get("display_name") or "")[:100] or None


def geocode_trip(conn, trip):
    """Fill start/end addresses of a stored trip (after it was inserted)."""
    if trip is None or trip.id is None:
        return
    start = reverse_geocode(trip.start_lat, trip.start_lon)
    end = reverse_geocode(trip.end_lat, trip.end_lon)
    trip.start_address, trip.end_address = start, end
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE trips SET start_address = COALESCE(start_address, %s), "
            "end_address = COALESCE(end_address, %s) WHERE id = %s",
            (start, end, trip.id),
        )


def geocode_charge_session(conn, cs):
    """Fill the address of a stored charge session (after it was inserted)."""
    if cs is None or cs.id is None:
        return
    address = reverse_geocode(cs.lat, cs.lon)
    cs.address = address
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE charge_sessions SET address = COALESCE(address, %s) WHERE id = %s",
            (address, cs.id),
        )


def backfill_addresses(conn):
    """Geocode all trips and charge sessions that have coordinates but no address."""
    with conn.cursor() as cur:
        # Trips without start_address
        cur.execute("""
            SELECT id, start_lat, start_lon, end_lat, end_lon
            FROM trips
            WHERE (start_address IS NULL AND start_lat IS NOT NULL)
               OR (end_address IS NULL AND end_lat IS NOT NULL)
            ORDER BY id
        """)
        trips = cur.fetchall()

    log.info("Backfill: %d trip(s) without address", len(trips))
    for trip_id, start_lat, start_lon, end_lat, end_lon in trips:
        start_addr = reverse_geocode(start_lat, start_lon) if start_lat else None
        end_addr = reverse_geocode(end_lat, end_lon) if end_lat else None
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE trips SET start_address = COALESCE(start_address, %s), end_address = COALESCE(end_address, %s) WHERE id = %s",
                (start_addr, end_addr, trip_id),
            )
        log.info("  Trip #%d: %s -> %s", trip_id, start_addr, end_addr)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, lat, lon
            FROM charge_sessions
            WHERE address IS NULL AND lat IS NOT NULL
            ORDER BY id
        """)
        sessions = cur.fetchall()

    log.info("Backfill: %d charge session(s) without address", len(sessions))
    for cs_id, lat, lon in sessions:
        addr = reverse_geocode(lat, lon)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE charge_sessions SET address = %s WHERE id = %s",
                (addr, cs_id),
            )
        log.info("  Charge session #%d: %s", cs_id, addr)

    log.info("Backfill complete")
