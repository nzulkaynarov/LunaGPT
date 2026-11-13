#!/usr/bin/env bash
# Nightly backup of every LunaGPT database: family data (lunagpt), gateway keys and spend (litellm),
# traces (langfuse) and Open WebUI. Each dump is checked, the family database is restored into a
# scratch database to prove the copy works, and the newest set is copied to the Raspberry Pi so a
# dead disk on this PC does not take the backups with it.
# Run by deploy/pc/lunagpt-db-backup.timer; restore steps are in docs/operations.md.
set -euo pipefail
umask 077

CONTAINER=${LUNAGPT_PG_CONTAINER:-lunagpt-postgres}
BACKUP_DIR=${LUNAGPT_BACKUP_DIR:-/data/backups/lunagpt}
KEEP=${LUNAGPT_BACKUP_KEEP:-14}
PI_HOST=${LUNAGPT_BACKUP_PI_HOST-znz@192.168.88.2}
PI_DIR=${LUNAGPT_BACKUP_PI_DIR:-backups/lunagpt}
PI_KEEP=${LUNAGPT_BACKUP_PI_KEEP:-7}
DATABASES=${LUNAGPT_BACKUP_DATABASES:-lunagpt litellm langfuse openwebui}
CHECK_DB=lunagpt_restore_check

pg() { docker exec -i "$CONTAINER" sh -c "$1"; }

partial=""
cleanup() {
  local status=$?
  [ -z "$partial" ] || rm -f "$partial"
  pg "dropdb -U \"\$POSTGRES_USER\" --if-exists $CHECK_DB" >/dev/null 2>&1 || true
  [ "$status" -eq 0 ] || echo "error: backup failed with status $status" >&2
}
trap cleanup EXIT

mkdir -p "$BACKUP_DIR"
stamp=$(date +%Y%m%d-%H%M%S)
saved=()

for db in $DATABASES; do
  partial=$(mktemp "$BACKUP_DIR/.partial.XXXXXX")
  pg "pg_dump -U \"\$POSTGRES_USER\" -Fc $db" > "$partial"
  entries=$(pg "pg_restore --list" < "$partial" | grep -vc '^;' || true)
  if [ "$entries" -lt 5 ]; then
    echo "error: dump of $db has only $entries entries" >&2
    exit 1
  fi
  target="$BACKUP_DIR/$db-$stamp.dump"
  mv "$partial" "$target"
  partial=""
  saved+=("$target")
  find "$BACKUP_DIR" -maxdepth 1 -name "$db-*.dump" | sort | head -n "-$KEEP" | xargs -r rm --
  echo "saved $target ($(stat -c %s "$target") bytes, $entries entries)"
done

# Prove the family database restores: load it into a scratch database and compare row counts.
pg "dropdb -U \"\$POSTGRES_USER\" --if-exists $CHECK_DB && createdb -U \"\$POSTGRES_USER\" $CHECK_DB"
pg "pg_restore -U \"\$POSTGRES_USER\" -d $CHECK_DB --no-owner --exit-on-error" < "$BACKUP_DIR/lunagpt-$stamp.dump"
count() { pg "psql -U \"\$POSTGRES_USER\" -d $1 -Atc 'select count(*) from assistant_members'"; }
live=$(count lunagpt)
restored=$(count "$CHECK_DB")
# A member who joined during the dump is the only acceptable difference.
if [ "$restored" -eq 0 ] && [ "$live" -ne 0 ] || [ "$restored" -gt "$live" ]; then
  echo "error: restored copy has $restored members, live database has $live" >&2
  exit 1
fi
echo "restore check passed: $restored members"

if [ -n "$PI_HOST" ]; then
  ssh -o BatchMode=yes -o ConnectTimeout=15 "$PI_HOST" "umask 077 && mkdir -p $PI_DIR"
  scp -q -o BatchMode=yes -o ConnectTimeout=15 "${saved[@]}" "$PI_HOST:$PI_DIR/"
  for db in $DATABASES; do
    ssh -o BatchMode=yes "$PI_HOST" \
      "ls -1 $PI_DIR/$db-*.dump 2>/dev/null | sort | head -n -$PI_KEEP | xargs -r rm --"
  done
  echo "copied ${#saved[@]} dumps to the Pi"
fi
