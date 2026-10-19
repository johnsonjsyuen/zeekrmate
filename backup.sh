#!/usr/bin/env bash
# Back up the zeekrmate PostgreSQL database.
#
# Usage: ./backup.sh [output_dir]        (default: ./backups next to this script)
#
# Writes <output_dir>/zeekrmate_<UTC timestamp>.dump in pg_dump custom format
# (compressed). Restore it with ./restore.sh.
#
# The dump is taken through `docker compose exec`, so it works whatever the
# compose project (and therefore container) is called. The stack's db service
# must be running. The script exits non-zero and leaves no file behind if
# anything fails. Backups contain your full location history: keep them private.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${1:-$SCRIPT_DIR/backups}"
compose() { docker compose -f "$SCRIPT_DIR/docker-compose.yml" "$@"; }
die() { echo "backup.sh: ERROR: $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker is not installed or not on PATH"
[ -n "$(compose ps --status running -q db 2>/dev/null)" ] \
  || die "the db service is not running (start it with: docker compose up -d db)"

umask 077
mkdir -p "$OUTPUT_DIR"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FINAL="$OUTPUT_DIR/zeekrmate_${TIMESTAMP}.dump"
TMP="$(mktemp "$OUTPUT_DIR/.zeekrmate_${TIMESTAMP}.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

# POSTGRES_USER/POSTGRES_DB are expanded inside the container, so the script
# follows whatever docker-compose.yml configures.
compose exec -T db sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$TMP" \
  || die "pg_dump failed"

[ -s "$TMP" ] || die "pg_dump produced an empty file"
[ "$(head -c 5 "$TMP")" = "PGDMP" ] || die "output is not a pg_dump custom-format archive"
# Make sure the archive is readable end to end (decodes it without restoring).
compose exec -T db pg_restore --file=/dev/null < "$TMP" \
  || die "pg_restore cannot read the archive; backup is unusable"

mv "$TMP" "$FINAL"
trap - EXIT
echo "Backup saved to: $FINAL ($(wc -c < "$FINAL") bytes)"
echo "Restore with:    $SCRIPT_DIR/restore.sh \"$FINAL\""
