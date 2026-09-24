#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CONTAINER="petrosa-data-manager-344-${RANDOM}"
LOG_FILE="${ROOT_DIR}/007_tier1_rehearsal.log"
FIXTURE=$(mktemp)

cleanup() {
  docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
  rm -f "${FIXTURE}"
}
trap cleanup EXIT

cat >"${FIXTURE}" <<'SQL'
CREATE DATABASE IF NOT EXISTS petrosa_crypto;
USE petrosa_crypto;
CREATE TABLE audit_logs (id INT NOT NULL, dataset_id INT, symbol VARCHAR(32), timestamp DATETIME, KEY idx_audit_logs_dataset_timestamp (dataset_id, timestamp), KEY idx_audit_logs_symbol (symbol), KEY idx_audit_logs_symbol_timestamp (symbol, timestamp)) ENGINE=InnoDB;
CREATE TABLE health_metrics (id INT NOT NULL, dataset_id INT, symbol VARCHAR(32), timestamp DATETIME, KEY idx_health_metrics_dataset_timestamp (dataset_id, timestamp), KEY idx_health_metrics_symbol (symbol), KEY idx_health_metrics_symbol_timestamp (symbol, timestamp)) ENGINE=InnoDB;
CREATE TABLE backfill_jobs (id INT NOT NULL, symbol VARCHAR(32), status VARCHAR(32), KEY idx_backfill_jobs_status (status), KEY idx_backfill_jobs_symbol (symbol)) ENGINE=InnoDB;
CREATE TABLE signals (id INT NOT NULL, strategy_id VARCHAR(64), symbol VARCHAR(32), period VARCHAR(16), KEY idx_strategy (strategy_id), KEY idx_symbol_period (symbol, period)) ENGINE=InnoDB;
CREATE TABLE positions (id INT NOT NULL, strategy_id VARCHAR(64), symbol VARCHAR(32), exchange VARCHAR(32), status VARCHAR(16), entry_time DATETIME, KEY idx_entry_time (entry_time), KEY idx_exchange (exchange), KEY idx_positions_status_entry_time (status, entry_time), KEY idx_strategy_id (strategy_id), KEY idx_symbol (symbol), KEY idx_status (status)) ENGINE=InnoDB;
CREATE TABLE klines_m1 (id INT NOT NULL, symbol VARCHAR(32), timestamp DATETIME, open_time DATETIME, KEY idx_klines_m1_open_time (open_time), KEY idx_klines_m1_timestamp (timestamp)) ENGINE=InnoDB;
CREATE TABLE klines_m15 (id INT NOT NULL, symbol VARCHAR(32), timestamp DATETIME, open_time DATETIME, KEY idx_klines_15m_open_time (open_time)) ENGINE=InnoDB;
CREATE TABLE klines_m30 (id INT NOT NULL, symbol VARCHAR(32), timestamp DATETIME, open_time DATETIME, KEY idx_klines_m30_symbol_timestamp (symbol, timestamp), KEY idx_klines_m30_open_time (open_time)) ENGINE=InnoDB;
CREATE TABLE klines_h1 (id INT NOT NULL, symbol VARCHAR(32), timestamp DATETIME, KEY idx_klines_h1_symbol_timestamp (symbol, timestamp)) ENGINE=InnoDB;
CREATE TABLE klines_d1 (id INT NOT NULL, symbol VARCHAR(32), timestamp DATETIME, KEY idx_klines_d1_symbol_timestamp (symbol, timestamp), KEY idx_klines_d1_timestamp (timestamp)) ENGINE=InnoDB;
CREATE TABLE klines_h4 (id INT NOT NULL, symbol VARCHAR(32), timestamp DATETIME, open_time DATETIME, KEY idx_klines_h4_open_time (open_time), KEY idx_klines_h4_timestamp (timestamp)) ENGINE=InnoDB;
CREATE TABLE klines_m5 (id INT NOT NULL, symbol VARCHAR(32), timestamp DATETIME, KEY idx_klines_m5_symbol_timestamp (symbol, timestamp)) ENGINE=InnoDB;
SQL

docker run --privileged --rm tonistiigi/binfmt --install amd64 >/dev/null
docker run --platform linux/amd64 --name "${CONTAINER}" --rm -d \
  -e MYSQL_ROOT_PASSWORD=root -e MYSQL_DATABASE=petrosa_crypto mysql:5.7 >/dev/null
until docker exec "${CONTAINER}" mysql -uroot -proot -e 'SELECT 1' >/dev/null 2>&1; do
  sleep 2
done

: >"${LOG_FILE}"
run_mysql() {
  docker exec -i "${CONTAINER}" mysql -vvv -uroot -proot petrosa_crypto
}

run_mysql <<'SQL' >>"${LOG_FILE}" 2>&1
SELECT VERSION();
SQL
printf 'PHASE: apply\n' | tee -a "${LOG_FILE}"
run_mysql <"${FIXTURE}" >>"${LOG_FILE}" 2>&1
run_mysql <"${ROOT_DIR}/007_drop_duplicate_and_unused_indexes.sql" >>"${LOG_FILE}" 2>&1

printf 'PHASE: re-apply\n' | tee -a "${LOG_FILE}"
run_mysql <"${ROOT_DIR}/007_drop_duplicate_and_unused_indexes.sql" >>"${LOG_FILE}" 2>&1

printf 'PHASE: rollback\n' | tee -a "${LOG_FILE}"
run_mysql <"${ROOT_DIR}/007_rollback_drop_duplicate_and_unused_indexes.sql" >>"${LOG_FILE}" 2>&1

printf 'PHASE: re-apply\n' | tee -a "${LOG_FILE}"
run_mysql <"${ROOT_DIR}/007_drop_duplicate_and_unused_indexes.sql" >>"${LOG_FILE}" 2>&1

printf 'TIER1 REHEARSAL: PASS\n' | tee -a "${LOG_FILE}"
