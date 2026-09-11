-- ============================================================================
--  SQL Server: НАГРУЗКА ЗА ПЕРИОД (например за год)
--
--  SQL Server, как и PostgreSQL, историю сам не хранит: user_updates -
--  накопительный счётчик, обнуляемый при перезапуске службы. Чтобы получить
--  "за март" или "за год", нужно складывать снимки и вычитать один из другого.
--  Шаги 1-2 настраиваются один раз.
-- ============================================================================

-- ШАГ 1. Таблица для снимков (один раз)
IF OBJECT_ID('dbo.er_stats_snapshot') IS NULL
CREATE TABLE dbo.er_stats_snapshot (
    taken_at   datetime2    NOT NULL CONSTRAINT DF_er_stats_taken DEFAULT SYSDATETIME(),
    table_name sysname      NOT NULL,
    [rows]     bigint       NULL,
    writes     bigint       NULL,   -- накопительный счётчик на момент снимка
    size_mb    decimal(18,1) NULL,
    INDEX IX_er_stats (table_name, taken_at)
);
GO

-- ШАГ 2. Снимок - на расписание через SQL Server Agent (раз в сутки хватит,
--        чтобы иметь историю за год).
INSERT INTO dbo.er_stats_snapshot (table_name, [rows], writes, size_mb)
SELECT t.name,
       SUM(CASE WHEN ps.index_id IN (0,1) THEN ps.row_count ELSE 0 END),
       COALESCE(MAX(us.user_updates), 0),
       CAST(SUM(ps.reserved_page_count) * 8.0 / 1024 AS decimal(18,1))
FROM sys.tables t
JOIN sys.dm_db_partition_stats ps ON ps.object_id = t.object_id
LEFT JOIN sys.dm_db_index_usage_stats us
       ON us.object_id = t.object_id AND us.database_id = DB_ID() AND us.index_id IN (0,1)
WHERE t.is_ms_shipped = 0
GROUP BY t.name;
GO

-- ШАГ 3. JSON за произвольный период: правь две даты, сохраняй как stats.json
DECLARE @from datetime2 = '2025-09-01';
DECLARE @to   datetime2 = '2026-09-01';

WITH a AS (   -- последний снимок НЕ ПОЗЖЕ начала периода
    SELECT table_name, writes,
           ROW_NUMBER() OVER (PARTITION BY table_name ORDER BY taken_at DESC) rn
    FROM dbo.er_stats_snapshot WHERE taken_at <= @from
), b AS (     -- последний снимок НЕ ПОЗЖЕ конца периода
    SELECT table_name, [rows], writes, size_mb,
           ROW_NUMBER() OVER (PARTITION BY table_name ORDER BY taken_at DESC) rn
    FROM dbo.er_stats_snapshot WHERE taken_at <= @to
)
SELECT
    (SELECT b.table_name AS [table],
            b.[rows]     AS [rows],
            b.size_mb    AS [size],
            -- перезапуск службы обнуляет счётчик и разность уходит в минус:
            -- отрицательную "нагрузку" показывать нельзя
            CASE WHEN b.writes - COALESCE(a.writes, 0) > 0
                 THEN b.writes - COALESCE(a.writes, 0) ELSE 0 END AS [writes]
     FROM b LEFT JOIN a ON a.table_name = b.table_name AND a.rn = 1
     WHERE b.rn = 1
     FOR JSON PATH) AS tables,
    CONVERT(varchar(10), @from, 23) + ' .. ' + CONVERT(varchar(10), @to, 23) AS period
FOR JSON PATH, WITHOUT_ARRAY_WRAPPER;
