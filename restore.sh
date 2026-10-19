#!/usr/bin/env bash
# Restore a zeekrmate database backup made by ./backup.sh.
#
# Usage: ./restore.sh <backup file> [--yes]
#
#   *.dump            pg_dump custom format (what backup.sh writes)
#   *.sql / *.sql.gz  plain SQL dump; must be restored into an EMPTY database
#
# What it does:
#   1. stops the logger and Grafana so nothing writes or holds locks,
#   2. REPLACES the current database contents with the backup, in a single
#      transaction that stops at the first error (pg_restore --exit-on-error,
#      psql ON_ERROR_STOP=1), so a failed restore changes nothing,
#   3. re-applies Grafana's read-only role grants,
#   4. starts the services it stopped again (also after a failure).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGGER_SERVICE="${LOGGER_SERVICE:-zeekrmate}"
compose() { docker compose -f "$SCRIPT_DIR/docker-compose.yml" "$@"; }
die() { echo "restore.sh: ERROR: $*" >&2; exit 1; }

FILE="${1:-}"
[ -n "$FILE" ] || die "usage: ./restore.sh <backup file> [--yes]"
[ -f "$FILE" ] || die "no such file: $FILE"
[ -s "$FILE" ] || die "backup file is empty: $FILE"

case "$FILE" in
  *.dump)   KIND=custom
            [ "$(head -c 5 "$FILE")" = "PGDMP" ] || die "$FILE is not a pg_dump custom-format archive" ;;
  *.sql.gz) KIND=sqlgz; gzip -t "$FILE" || die "$FILE is not a valid gzip file" ;;
  *.sql)    KIND=sql ;;
  *)        die "unrecognised backup type (expected .dump, .sql or .sql.gz): $FILE" ;;
esac

command -v docker >/dev/null 2>&1 || die "docker is not installed or not on PATH"
[ -n "$(compose ps --status running -q db 2>/dev/null)" ] \
  || die "the db service is not running (start it with: docker compose up -d db)"

if [ "${2:-}" != "--yes" ]; then
  echo "This REPLACES everything in the zeekrmate database with: $FILE"
  read -r -p "Type 'restore' to continue: " answer
  [ "$answer" = "restore" ] || die "aborted"
fi

# Stop writers/readers; remember which were running so we restart only those.
STOPPED=()
for svc in "$LOGGER_SERVICE" grafana; do
  if [ -n "$(compose ps --status running -q "$svc" 2>/dev/null)" ]; then
    compose stop "$svc"
    STOPPED+=("$svc")
  fi
done
restart_stopped() {
  if [ "${#STOPPED[@]}" -gt 0 ]; then
    compose start "${STOPPED[@]}" || echo "restore.sh: WARNING: could not restart ${STOPPED[*]}" >&2
  fi
}
trap restart_stopped EXIT

case "$KIND" in
  custom)
    # --no-owner/--no-acl: ownership and grants are re-applied below, so the
    # restore does not depend on which roles existed when the dump was taken.
    compose exec -T db sh -c 'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        --clean --if-exists --no-owner --no-acl --single-transaction --exit-on-error' < "$FILE" \
      || die "pg_restore failed; the transaction was rolled back and the database is unchanged" ;;
  sqlgz|sql)
    reader=(cat "$FILE"); [ "$KIND" = sqlgz ] && reader=(gunzip -c "$FILE")
    "${reader[@]}" | compose exec -T db sh -c 'exec psql -X -q -v ON_ERROR_STOP=1 --single-transaction \
        -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
      || die "psql restore failed; the transaction was rolled back. Plain SQL dumps need an empty database (fresh pgdata volume)" ;;
esac

compose exec -T db bash /docker-entrypoint-initdb.d/20-grafana-readonly.sh \
  || die "restore succeeded but re-granting Grafana's read-only role failed"

echo "Restore complete from: $FILE"
