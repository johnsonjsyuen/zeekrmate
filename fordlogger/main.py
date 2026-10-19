import argparse
import logging
import signal
import sys

import psycopg2

from .config import load_config
from . import db
from .poller import Poller
from .geocoder import backfill_addresses
from .providers.factory import create_providers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fordlogger")
# zeekr_ev_api logs signed headers and full bodies at DEBUG; keep it at WARNING
# (matches providers.zeekr.redact.MIN_LIBRARY_LEVEL).
logging.getLogger("zeekr_ev_api").setLevel(logging.WARNING)


def _install_signal_handlers(poller):
    """SIGTERM/SIGINT stop the loop after the current cycle (exit code 0)."""
    def handler(signum, _frame):
        log.info("Received signal %d; stopping after the current cycle", signum)
        poller.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # not in the main thread
            pass


PLACEHOLDER_PASSWORD_PREFIX = "change-me"


def _placeholder_password(cfg: dict) -> bool:
    """True if db_password is still a .env.example placeholder (change-me...)."""
    return str(cfg.get("db_password") or "").strip().lower().startswith(PLACEHOLDER_PASSWORD_PREFIX)


def _connect_db(cfg: dict):
    """Connect and migrate; a database that is down is one log line, exit 1."""
    try:
        conn = db.connect(cfg)
        db.ensure_schema(conn)
        return conn
    except psycopg2.Error as e:
        detail = (str(e).strip().splitlines() or [""])[0][:200]
        log.error("Database unavailable at %s:%s (%s): %s",
                  cfg.get("db_host"), cfg.get("db_port"), type(e).__name__, detail)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="zeekrmate: vehicle telemetry logger")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    parser.add_argument("--backfill-addresses", action="store_true", help="Reverse-geocode addresses for existing trips and charge sessions")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        log.error("%s", e)
        sys.exit(1)

    if _placeholder_password(cfg):
        log.error("The database password is still the placeholder from .env.example "
                  "(change-me...). Set a real POSTGRES_PASSWORD in .env (before the "
                  "database volume is first created) and try again.")
        sys.exit(1)

    conn = _connect_db(cfg)

    if args.backfill_addresses:
        backfill_addresses(conn)
        return

    try:
        providers = create_providers(cfg)
    except Exception as e:  # e.g. ZeekrConfigError (a ValueError) for missing ZEEKR_* vars
        log.error("Provider configuration error: %s", e)
        log.debug("Provider configuration error details", exc_info=True)
        sys.exit(1)
    if not providers:
        log.error("No vehicle providers configured; add a \"providers\" entry to config.json")
        sys.exit(1)
    poller = Poller(cfg, conn, providers)

    if args.once:
        result = poller.poll_once()
        if not result.ok:
            log.error(
                "Single poll failed: db_ok=%s providers_ok=%d providers_failed=%d "
                "positions_stored=%d position_errors=%d",
                result.db_ok, result.providers_ok, result.providers_failed,
                result.positions_stored, result.position_errors,
            )
            sys.exit(1)
        return

    # Daemon mode
    _install_signal_handlers(poller)
    poller.run_forever()


if __name__ == "__main__":
    main()
