-- ============================================================================
--  НАГРУЗКА ЗА ПЕРИОД (например за год)
--
--  PostgreSQL НЕ хранит историю сам: в pg_stat_user_tables лежат только
--  накопительные счётчики с момента сброса. Чтобы получить "за март" или
--  "за год", нужно периодически складывать снимки - и потом вычесть один
--  из другого. Шаги 1-2 настраиваются один раз.
-- ============================================================================

-- ШАГ 1. Таблица для снимков (один раз)
CREATE TABLE IF NOT EXISTS er_stats_snapshot (
  taken_at   timestamptz NOT NULL DEFAULT now(),
  table_name text        NOT NULL,
  rows       bigint,
  writes     bigint,      -- накопительный счётчик на момент снимка
  size_mb    numeric
);
CREATE INDEX IF NOT EXISTS er_stats_snapshot_idx ON er_stats_snapshot (table_name, taken_at);

-- ШАГ 2. Снимок - вешается на расписание (pg_cron, systemd timer, планировщик
--        задач Windows). Раз в сутки достаточно, чтобы иметь историю за год.
INSERT INTO er_stats_snapshot (table_name, rows, writes, size_mb)
SELECT relname,
       n_live_tup,
       n_tup_ins + n_tup_upd + n_tup_del,
       round(pg_total_relation_size(relid) / 1048576.0, 1)
FROM pg_stat_user_tables;

-- ШАГ 3. JSON за произвольный период: правь две даты и сохраняй как stats.json
WITH bounds AS (
  SELECT timestamptz '2025-09-01' AS d_from, timestamptz '2026-09-01' AS d_to
), a AS (   -- последний снимок НЕ ПОЗЖЕ начала периода
  SELECT DISTINCT ON (table_name) table_name, writes
  FROM er_stats_snapshot, bounds
  WHERE taken_at <= bounds.d_from
  ORDER BY table_name, taken_at DESC
), b AS (   -- последний снимок НЕ ПОЗЖЕ конца периода
  SELECT DISTINCT ON (table_name) table_name, rows, writes, size_mb
  FROM er_stats_snapshot, bounds
  WHERE taken_at <= bounds.d_to
  ORDER BY table_name, taken_at DESC
)
SELECT jsonb_pretty(jsonb_build_object(
  'generated_at', now(),
  'period',       (SELECT d_from::date || ' .. ' || d_to::date FROM bounds),
  'tables',       jsonb_object_agg(b.table_name, jsonb_build_object(
      'rows',   b.rows,
      -- GREATEST на случай pg_stat_reset() или перезапуска: счётчик мог
      -- уехать назад, отрицательная "нагрузка" нам не нужна
      'writes', GREATEST(b.writes - COALESCE(a.writes, 0), 0),
      'size',   b.size_mb
  ))
))
FROM b LEFT JOIN a USING (table_name);
