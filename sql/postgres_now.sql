-- ============================================================================
--  СРЕЗ "ПРЯМО СЕЙЧАС"  ->  stats.json
--  Запусти в psql / pgAdmin / DBeaver, сохрани единственную вернувшуюся
--  ячейку как stats.json и перетащи файл на диаграмму. Сервер не нужен.
--
--  ВАЖНО: writes здесь - это НАКОПИТЕЛЬНЫЙ счётчик с момента последнего
--  pg_stat_reset() (а не "за период"). Для периода см. postgres_period.sql.
--  rows (n_live_tup) - оценка планировщика, она нулевая пока не отработал
--  autovacuum/ANALYZE на свежей базе.
-- ============================================================================
SELECT jsonb_pretty(jsonb_build_object(
  'generated_at', now(),
  'period',       'накопительно с момента сброса статистики',
  'tables',       jsonb_object_agg(relname, jsonb_build_object(
      'rows',   n_live_tup,
      'writes', n_tup_ins + n_tup_upd + n_tup_del,
      'size',   round(pg_total_relation_size(relid) / 1048576.0, 1)   -- МБ
  ))
))
FROM pg_stat_user_tables;
