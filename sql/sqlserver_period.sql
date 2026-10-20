-- ============================================================================
--  SQL Server: НАГРУЗКА ЗА ПЕРИОД (например за год)
--  Работает на SQL Server 2005 и новее (без FOR JSON).
--
--  SQL Server историю сам не хранит: user_updates - накопительный счётчик,
--  обнуляемый при перезапуске службы. Чтобы получить "за год", нужно
--  складывать снимки и вычитать один из другого.
--  ШАГ 1 и ШАГ 2 настраиваются один раз; ШАГ 3 - когда нужен отчёт.
--
--  Получить файл (выполнится весь скрипт: снимок + отчёт за период):
--     sqlcmd -S SERVER -d BASE -E -i sqlserver_period.sql -o stats.json -h -1 -W -r1
--  ВНИМАНИЕ: если у базы включён AUTO_CLOSE (бывает на Express), счётчики
--  обнуляются при каждом закрытии базы - снимки тогда бессмысленны.
-- ============================================================================

SET NOCOUNT ON;   -- иначе "(N rows affected)" попадёт прямо в JSON

-- ШАГ 1. Таблица для снимков (один раз)
IF OBJECT_ID('dbo.er_stats_snapshot') IS NULL
BEGIN
    CREATE TABLE dbo.er_stats_snapshot (
        taken_at   datetime      NOT NULL CONSTRAINT DF_er_stats_taken DEFAULT GETDATE(),
        table_name sysname       NOT NULL,
        [rows]     bigint        NULL,
        writes     bigint        NULL,   -- накопительный счётчик на момент снимка
        size_mb    decimal(18,1) NULL
    );
    CREATE INDEX IX_er_stats ON dbo.er_stats_snapshot (table_name, taken_at);
END
GO

-- ШАГ 2. Снимок. Повесь ЭТОТ блок на SQL Server Agent (раз в сутки хватит,
--        чтобы иметь историю за год; растёт примерно на 1 строку на таблицу в день).
INSERT INTO dbo.er_stats_snapshot (table_name, [rows], writes, size_mb)
SELECT t.name,
       SUM(CASE WHEN ps.index_id IN (0,1) THEN ps.row_count ELSE 0 END),
       MAX(ISNULL(us.user_updates, 0)),
       CAST(SUM(ps.reserved_page_count) * 8.0 / 1024 AS decimal(18,1))
FROM sys.tables t
JOIN sys.dm_db_partition_stats ps ON ps.object_id = t.object_id
LEFT JOIN sys.dm_db_index_usage_stats us
       ON us.object_id = t.object_id AND us.database_id = DB_ID() AND us.index_id IN (0,1)
WHERE t.is_ms_shipped = 0
  AND t.name <> 'er_stats_snapshot'
GROUP BY t.name;
GO

-- ШАГ 3. stats.json за произвольный период: поправь две даты.
SET NOCOUNT ON;
DECLARE @from datetime, @to datetime;
SET @from = '20250901';
SET @to   = '20260901';

WITH a AS (   -- последний снимок НЕ ПОЗЖЕ начала периода
    SELECT table_name, writes,
           ROW_NUMBER() OVER (PARTITION BY table_name ORDER BY taken_at DESC) AS k
    FROM dbo.er_stats_snapshot WHERE taken_at <= @from
), b AS (     -- последний снимок НЕ ПОЗЖЕ конца периода
    SELECT table_name, [rows], writes, size_mb,
           ROW_NUMBER() OVER (PARTITION BY table_name ORDER BY taken_at DESC) AS k
    FROM dbo.er_stats_snapshot WHERE taken_at <= @to
), d AS (
    SELECT b.table_name, b.[rows], b.size_mb,
           -- перезапуск службы обнуляет счётчик: отрицательную нагрузку не показываем
           CASE WHEN b.writes - ISNULL(a.writes, 0) > 0
                THEN b.writes - ISNULL(a.writes, 0) ELSE 0 END AS writes
    FROM b LEFT JOIN a ON a.table_name = b.table_name AND a.k = 1
    WHERE b.k = 1
), r AS (
    SELECT *, ROW_NUMBER() OVER (ORDER BY writes DESC, [rows] DESC) AS rn FROM d
)
SELECT line FROM (
    SELECT 0 AS part, 0 AS rn,
           CAST(N'{"period":"' + CONVERT(nvarchar(10), @from, 120) + N' .. '
              + CONVERT(nvarchar(10), @to, 120) + N'","tables":{' AS nvarchar(4000)) AS line
    UNION ALL
    SELECT 1, rn,
           CAST(CASE WHEN rn = 1 THEN N'' ELSE N',' END
              + N'"' + REPLACE(REPLACE(table_name, N'\', N'\'), N'"', N'\"') + N'":{'
              + N'"rows":'    + CAST([rows]  AS nvarchar(20))
              + N',"size":'   + CAST(size_mb AS nvarchar(30))
              + N',"writes":' + CAST(writes  AS nvarchar(20)) + N'}' AS nvarchar(4000))
    FROM r
    UNION ALL
    SELECT 2, 0, N'}}'
) q
ORDER BY part, rn;
