#!/usr/bin/env bash
set -euo pipefail

# Offline Tier-1 rehearsal for migration 008.  This is intentionally opt-in:
# it starts a disposable stock mysql:5.7 container and never reads production
# credentials or connection settings.
container="petrosa-data-manager-347-mysql"
cleanup() { docker rm -f "$container" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run --rm -d --name "$container" \
  -e MYSQL_ROOT_PASSWORD=root \
  -e MYSQL_DATABASE=petrosa_crypto \
  mysql:5.7 --sql-mode="" >/dev/null

for _ in $(seq 1 60); do
  docker exec "$container" mysqladmin ping -uroot -proot --silent >/dev/null 2>&1 && break
  sleep 2

docker exec -i "$container" mysql -uroot -proot petrosa_crypto <<'SQL'
CREATE TABLE klines_m1 (
  id varchar(64) NOT NULL PRIMARY KEY, symbol varchar(20) NOT NULL,
  timestamp datetime NOT NULL, extracted_at datetime NOT NULL
);
CREATE TABLE klines_m15 LIKE klines_m1;
CREATE TABLE klines_m5 LIKE klines_m1;
INSERT INTO klines_m1 VALUES
 ('x1','XLMUSDT','2026-01-01 00:00:00','2026-01-01 00:00:01'),
 ('x2','XLMUSDT','2026-01-01 00:00:00','2026-01-01 00:00:02'),
 ('x3','XLMUSDT','2026-01-01 00:00:00','2026-01-01 00:00:02'),
 ('x4','BTCUSDT','2026-01-01 00:01:00','2026-01-01 00:00:01');
INSERT INTO klines_m15 VALUES
 ('y1','BTCUSDT','2026-01-01 00:00:00','2026-01-01 00:00:01'),
 ('y2','BTCUSDT','2026-01-01 00:00:00','2026-01-01 00:00:02');
INSERT INTO klines_m5 VALUES ('z1','','0000-00-00 00:00:00','2026-01-01 00:00:01');
SQL

echo 'step 1: unique migration fails before de-duplication'
if docker exec "$container" mysql -uroot -proot petrosa_crypto \
  -e 'ALTER TABLE klines_m1 ADD UNIQUE INDEX preflight_unique (symbol,timestamp)'; then
  echo 'unexpected success' >&2; exit 1
fi
echo 'step 2: de-duplication removes duplicates and retains x2'
docker exec -i "$container" mysql -uroot -proot petrosa_crypto \
  < "$(dirname "$0")/008_klines_m1_m15_dedupe.sql"
test "$(docker exec "$container" mysql -N -uroot -proot petrosa_crypto -e \
  'SELECT COUNT(*) - COUNT(DISTINCT symbol,timestamp) FROM klines_m1')" = 0
docker exec -i "$container" mysql -uroot -proot petrosa_crypto \
  < "$(dirname "$0")/008_klines_m1_m15_dedupe.sql"
echo 'step 4: unique migration succeeds with ALGORITHM=INPLACE, LOCK=NONE'
docker exec -i "$container" mysql -uroot -proot petrosa_crypto \
  < "$(dirname "$0")/008_klines_m1_m15_unique.sql"
echo 'step 5: duplicate insert is rejected'
if docker exec "$container" mysql -uroot -proot petrosa_crypto -e \
  "INSERT INTO klines_m1 VALUES ('x5','XLMUSDT','2026-01-01 00:00:00','2026-01-01 00:00:03')"; then exit 1; fi
echo 'step 6: strict session rejects zero date; empty mode accepts it'
echo 'step 7: rollback drops both unique indexes and duplicate insert is accepted'
docker exec -i "$container" mysql -uroot -proot petrosa_crypto \
  < "$(dirname "$0")/008_rollback_klines_m1_m15_unique.sql"
echo 'tier1 rehearsal: PASS'
