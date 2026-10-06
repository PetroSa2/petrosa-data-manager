-- Read-only pre/post measurement for migration 018.
-- Run before and after the operator migration; no DDL is included.

SELECT
    table_name,
    index_name,
    SUM(stat_value) AS index_pages,
    @@innodb_page_size AS page_size_bytes,
    SUM(stat_value) * @@innodb_page_size AS index_size_bytes
FROM mysql.innodb_index_stats
WHERE database_name = DATABASE()
  AND stat_name = 'size'
  AND (
      (table_name = 'klines_m5' AND index_name = 'uniq_klines_m5_symbol_timestamp')
      OR (table_name = 'klines_m15' AND index_name IN ('uniq_klines_m15_symbol_timestamp', 'idx_klines_15m_symbol_timestamp'))
      OR (table_name = 'klines_m30' AND index_name = 'uniq_klines_m30_symbol_timestamp')
      OR (table_name = 'klines_h1' AND index_name = 'uniq_klines_h1_symbol_timestamp')
      OR (table_name = 'klines_d1' AND index_name = 'uniq_klines_d1_symbol_timestamp')
  )
GROUP BY table_name, index_name
ORDER BY table_name, index_name;
