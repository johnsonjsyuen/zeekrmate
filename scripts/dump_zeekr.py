#!/usr/bin/env python3
"""First real connection to the Zeekr API: check login, or capture raw payloads.

READ-ONLY. Only these library calls are made: login / load_session /
export_session, get_vehicle_list, get_vehicle_status,
get_vehicle_charging_status, get_vehicle_charging_limit, get_journey_log and
get_trip_trackpoints. Commands (remote control, charge/travel plans) are
unreachable: the client is wrapped in an allow-list proxy.

Secrets come only from environment variables (see .env.example). The login
session is reused from $FORDLOGGER_STATE_DIR/zeekr_session.json (same file the
logger uses), so running this does not force a new login every time.

Usage (host, from the repo root, with the ZEEKR_* variables exported):
    python scripts/dump_zeekr.py --check
    python scripts/dump_zeekr.py               # writes captures/YYYY-MM-DD-HHMM/

Usage (Docker; reads .env, writes to ./data/captures on the host):
    docker compose run --rm fordlogger python scripts/dump_zeekr.py --check
    docker compose run --rm fordlogger python scripts/dump_zeekr.py

Exit codes: 0 OK, 2 configuration missing, 3 authentication failed,
4 API/other error, 5 no vehicles on the account.

Capture files contain your VIN, location history and account data. Keep them
private; run scripts/sanitise_zeekr_capture.py before sharing.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fordlogger.providers.zeekr.mapper import jsonable  # noqa: E402
from fordlogger.providers.zeekr.redact import mask_vin  # noqa: E402
from fordlogger.providers.zeekr.session import (  # noqa: E402
    ZeekrAuthError,
    ZeekrProviderError,
    ZeekrSession,
)
from fordlogger.providers.zeekr.session_store import SessionStore  # noqa: E402
from fordlogger.providers.zeekr.settings import (  # noqa: E402
    ZeekrConfigError,
    load_settings,
    resolve_state_dir,
    session_path,
)

EXIT_OK, EXIT_CONFIG, EXIT_AUTH, EXIT_API, EXIT_NO_VEHICLES = 0, 2, 3, 4, 5

log = logging.getLogger("fordlogger")


def _write_json(path: Path, data) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(jsonable(data), fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    os.chmod(path, 0o600)


def _make_capture_dir(base: Path, now: datetime) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d-%H%M")
    out = base / stamp
    n = 1
    while out.exists():
        n += 1
        out = base / f"{stamp}-{n}"
    out.mkdir(mode=0o700)
    os.chmod(out, 0o700)
    return out


def _latest_trip(journeys):
    if not isinstance(journeys, dict):
        return None
    trips = journeys.get("data") or journeys.get("list") or []
    trips = [t for t in trips if isinstance(t, dict) and t.get("tripId") is not None
             and t.get("reportTime") is not None]
    if not trips:
        return None
    return max(trips, key=lambda t: t.get("startTime") or 0)


def build_session(environ=None, client_factory=None) -> ZeekrSession:
    settings = load_settings(environ)
    store = SessionStore(session_path(resolve_state_dir({}, environ)))
    return ZeekrSession(settings, store, client_factory=client_factory)


def run_check(session: ZeekrSession, out=print) -> int:
    vehicles = session.call("get_vehicle_list") or []
    session.persist()
    vins = [getattr(v, "vin", None) for v in vehicles]
    out(f"Login OK. Vehicles on this account: {len(vins)}")
    for vin in vins:
        out(f"  - VIN {mask_vin(vin)}")
    if not vins:
        out("No vehicles found. Is the car shared with this Zeekr account?")
        return EXIT_NO_VEHICLES
    out("OK")
    return EXIT_OK


def run_capture(session: ZeekrSession, base: Path, vin_suffix=None, days_back=30,
                out=print, now=None) -> int:
    now = now or datetime.now(timezone.utc)
    vehicles = session.call("get_vehicle_list") or []
    vehicle_rows = [{"vin": getattr(v, "vin", None), "data": getattr(v, "data", None)}
                    for v in vehicles]
    vins = [r["vin"] for r in vehicle_rows if isinstance(r["vin"], str) and r["vin"]]
    if not vins:
        out("No vehicles found. Is the car shared with this Zeekr account?")
        return EXIT_NO_VEHICLES
    if vin_suffix:
        matches = [v for v in vins if v.endswith(vin_suffix)]
        if len(matches) != 1:
            out(f"--vin {vin_suffix!r} matched {len(matches)} vehicle(s)")
            return EXIT_API
        vin = matches[0]
    else:
        vin = vins[0]

    target = _make_capture_dir(base, now)
    _write_json(target / "vehicles.json", vehicle_rows)
    results = {"vehicles": "ok"}

    def grab(name, method, *args, **kwargs):
        try:
            data = session.call(method, vin, *args, **kwargs)
            results[name] = "ok" if data else "empty"
        except ZeekrAuthError:
            raise
        except ZeekrProviderError as e:
            data = {"_error": str(e)}
            results[name] = "error"
        _write_json(target / f"{name}.json", data)
        return data

    grab("vehicle_status", "get_vehicle_status")
    grab("charging_status", "get_vehicle_charging_status")
    grab("charging_limit", "get_vehicle_charging_limit")
    journeys = grab("journeys", "get_journey_log", page_size=10, days_back=days_back)
    trip = _latest_trip(journeys)
    if trip is not None:
        grab("trip_trackpoints", "get_trip_trackpoints",
             trip_report_time=trip["reportTime"], trip_id=trip["tripId"])
    else:
        results["trip_trackpoints"] = "skipped (no journey in window)"
    session.persist()

    try:
        from importlib.metadata import version
        lib_version = version("zeekr_ev_api")
    except Exception:
        lib_version = None
    _write_json(target / "manifest.json", {
        "captured_at": now.isoformat(),
        "vin_masked": mask_vin(vin),
        "zeekr_ev_api_version": lib_version,
        "journey_days_back": days_back,
        "results": results,
    })

    out(f"Captured vehicle {mask_vin(vin)} into {target}")
    for name, status in results.items():
        out(f"  {name:17s} {status}")
    out("These files contain personal data (VIN, location, account). Keep them private;")
    out("run scripts/sanitise_zeekr_capture.py on the folder before sharing it.")
    failed = [k for k, v in results.items() if v == "error"]
    return EXIT_API if "vehicle_status" in failed else EXIT_OK


def main(argv=None, environ=None, client_factory=None, out=print) -> int:
    parser = argparse.ArgumentParser(description="Zeekr read-only connection check / raw capture")
    parser.add_argument("--check", action="store_true",
                        help="log in and list vehicles (masked VINs) only")
    parser.add_argument("--out", default=None,
                        help="capture base directory (default: $ZEEKR_CAPTURE_DIR or ./captures)")
    parser.add_argument("--vin", default=None, help="last characters of the VIN to capture (default: first)")
    parser.add_argument("--days", type=int, default=30, help="journey log window in days (default 30)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    env = os.environ if environ is None else environ
    try:
        session = build_session(environ, client_factory)
    except ZeekrConfigError as e:
        out(f"CONFIG ERROR: {e}")
        return EXIT_CONFIG
    try:
        if args.check:
            return run_check(session, out)
        base = Path(args.out or env.get("ZEEKR_CAPTURE_DIR") or "captures")
        return run_capture(session, base, args.vin, args.days, out)
    except ZeekrAuthError as e:
        out(f"AUTH FAILED: {e}")
        return EXIT_AUTH
    except ZeekrProviderError as e:
        out(f"API ERROR: {e}")
        return EXIT_API
    except Exception as e:  # never print raw exception text (may contain payloads)
        out(f"UNEXPECTED ERROR: {type(e).__name__}")
        return EXIT_API


if __name__ == "__main__":
    sys.exit(main())
