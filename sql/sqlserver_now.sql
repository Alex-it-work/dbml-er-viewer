-- ============================================================================
--  SQL Server: срез "прямо сейчас" для ТЕКУЩЕЙ базы  ->  stats.json
--  Работает на SQL Server 2005 и новее (без FOR JSON).
--
--  Результат - по одной строке JSON на таблицу (так не упираемся в лимит
--  65 535 символов на ячейку в гриде SSMS - у AX таблиц тысячи).
--
--  Как получить файл:
--   * проще всего:
--       sqlcmd -S SERVER -d BASE -E -i sqlserver_now.sql -o stats.json -h -1 -W -r1
--   * или в SSMS: выполнить, кликнуть по заголовку колонки "line" (выделится
--     вся колонка), Ctrl+C, вставить в Блокнот, сохранить как stats.json
--     (кодировка UTF-8).
--
--  writes = user_updates: накопительно С МОМЕНТА ЗАПУСКА СЛУЖБЫ SQL Server.
--  Для периода - sqlserver_period.sql.
-- ============================================================================
SET NOCOUNT ON;

WITH s AS (
    SELECT t.name                                                         AS name,
           -- строки только по куче/кластерному индексу, иначе каждый
           -- некластерный индекс добавит свою копию строк
           SUM(CASE WHEN ps.index_id IN (0,1) THEN ps.row_count ELSE 0 END) AS [rows],
           CAST(SUM(ps.reserved_page_count) * 8.0 / 1024 AS decimal(18,1))   AS size_mb,
           MAX(ISNULL(us.user_updates, 0))                                    AS writes
    FROM sys.tables t
    JOIN sys.dm_db_partition_stats ps
          ON ps.object_id = t.object_id
    LEFT JOIN sys.dm_db_index_usage_stats us
          ON us.object_id = t.object_id
         AND us.database_id = DB_ID()
         AND us.index_id IN (0,1)
    WHERE t.is_ms_shipped = 0
    GROUP BY t.name
), r AS (
    SELECT *, ROW_NUMBER() OVER (ORDER BY writes DESC, [rows] DESC) AS rn FROM s
)
SELECT line FROM (
    SELECT 0 AS part, 0 AS rn,
           CAST(N'{"generated_at":"' + CONVERT(nvarchar(30), GETDATE(), 126)
              + N'","period":"since SQL Server service start","tables":{' AS nvarchar(4000)) AS line
    UNION ALL
    SELECT 1, rn,
           CAST(CASE WHEN rn = 1 THEN N'' ELSE N',' END
              + N'"' + REPLACE(REPLACE(name, N'\', N'\'), N'"', N'\"') + N'":{'
              + N'"rows":'    + CAST([rows]  AS nvarchar(20))
              + N',"size":'   + CAST(size_mb AS nvarchar(30))
              + N',"writes":' + CAST(writes  AS nvarchar(20)) + N'}' AS nvarchar(4000))
    FROM r
    UNION ALL
    SELECT 2, 0, N'}}'
) q
ORDER BY part, rn;
