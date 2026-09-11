-- ============================================================================
--  MySQL / MariaDB: срез "прямо сейчас" -> stats.json
--  Сохрани вернувшуюся ячейку как stats.json и перетащи на диаграмму.
--  writes берётся из performance_schema (накопительно с рестарта сервера).
-- ============================================================================
SELECT JSON_OBJECT(
  'generated_at', NOW(),
  'period',       'накопительно с рестарта сервера',
  'tables', JSON_OBJECTAGG(t.TABLE_NAME, JSON_OBJECT(
      'rows',   t.TABLE_ROWS,                                        -- оценка
      'size',   ROUND((t.DATA_LENGTH + t.INDEX_LENGTH) / 1048576, 1),-- МБ
      'writes', COALESCE(io.w, 0)
  )))
FROM information_schema.TABLES t
LEFT JOIN (
  SELECT OBJECT_NAME, COUNT_INSERT + COUNT_UPDATE + COUNT_DELETE AS w
  FROM performance_schema.table_io_waits_summary_by_table
  WHERE OBJECT_SCHEMA = DATABASE()
) io ON io.OBJECT_NAME = t.TABLE_NAME
WHERE t.TABLE_SCHEMA = DATABASE() AND t.TABLE_TYPE = 'BASE TABLE';
