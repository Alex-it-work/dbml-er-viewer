-- ============================================================================
--  SQL Server: ВСЕ базы инстанса одним запросом  ->  stats.json
--  Работает на SQL Server 2005 и новее (без FOR JSON).
--
--  Обходит все доступные пользовательские БД, имена выходят полными:
--      AX2009.dbo.CUSTTABLE
--  Вьювер при сопоставлении отбрасывает префиксы и регистр, так что такое
--  имя найдёт CustTable в схеме. ВНИМАНИЕ: одинаковые имена таблиц в разных
--  базах в диаграмме схлопнутся в одну - тогда снимай по одной базе
--  (sqlserver_now.sql).
--
--  Нужны права VIEW DATABASE STATE в каждой БД (или VIEW SERVER STATE).
--  Базы без доступа пропускаются с сообщением, а не роняют весь прогон.
--
--  Получить файл:
--     sqlcmd -S SERVER -E -i sqlserver_all_databases.sql -o stats.json -h -1 -W -r1
--  или в SSMS: выделить колонку "line" целиком, Ctrl+C, вставить в Блокнот,
--  сохранить как stats.json (UTF-8).
-- ============================================================================
SET NOCOUNT ON;

-- когда запущена служба: tempdb пересоздаётся при каждом старте (работает на 2005+)
DECLARE @started datetime;
SELECT @started = create_date FROM sys.databases WHERE name = 'tempdb';

IF OBJECT_ID('tempdb..#stats') IS NOT NULL DROP TABLE #stats;
CREATE TABLE #stats (
    full_name nvarchar(400),
    [rows]    bigint,
    size_mb   decimal(18,1),
    writes    bigint
);

DECLARE @db sysname, @sql nvarchar(4000);

DECLARE db_cur CURSOR LOCAL FAST_FORWARD FOR
    SELECT name FROM sys.databases
    WHERE database_id > 4                 -- без master/model/msdb/tempdb
      AND state_desc = 'ONLINE'
      AND HAS_DBACCESS(name) = 1;

OPEN db_cur;
FETCH NEXT FROM db_cur INTO @db;
WHILE @@FETCH_STATUS = 0
BEGIN
    SET @sql = N'USE ' + QUOTENAME(@db) + N';
      SELECT ' + QUOTENAME(@db, '''') + N' + ''.'' + s.name + ''.'' + t.name,
             SUM(CASE WHEN ps.index_id IN (0,1) THEN ps.row_count ELSE 0 END),
             CAST(SUM(ps.reserved_page_count) * 8.0 / 1024 AS decimal(18,1)),
             MAX(ISNULL(us.user_updates, 0))
      FROM sys.tables t
      JOIN sys.schemas s                ON s.schema_id = t.schema_id
      JOIN sys.dm_db_partition_stats ps ON ps.object_id = t.object_id
      LEFT JOIN sys.dm_db_index_usage_stats us
             ON us.object_id = t.object_id
            AND us.database_id = DB_ID()
            AND us.index_id IN (0,1)
      WHERE t.is_ms_shipped = 0
      GROUP BY s.name, t.name;';

    BEGIN TRY
        INSERT INTO #stats (full_name, [rows], size_mb, writes) EXEC sp_executesql @sql;
    END TRY
    BEGIN CATCH
        PRINT 'skipped database ' + @db + ': ' + ERROR_MESSAGE();
    END CATCH

    FETCH NEXT FROM db_cur INTO @db;
END
CLOSE db_cur;
DEALLOCATE db_cur;

WITH r AS (
    SELECT *, ROW_NUMBER() OVER (ORDER BY writes DESC, [rows] DESC) AS rn FROM #stats
)
SELECT line FROM (
    SELECT 0 AS part, 0 AS rn,
           CAST(N'{"generated_at":"' + CONVERT(nvarchar(30), GETDATE(), 126)
              + N'","server_started":"' + CONVERT(nvarchar(30), @started, 126)
              + N'","uptime_days":' + CAST(CAST(DATEDIFF(minute, @started, GETDATE()) / 1440.0 AS decimal(10,1)) AS nvarchar(20))
              + N',"period":"writes since SQL Server start ' + CONVERT(nvarchar(16), @started, 120)
              + N'","tables":{' AS nvarchar(4000)) AS line
    UNION ALL
    SELECT 1, rn,
           CAST(CASE WHEN rn = 1 THEN N'' ELSE N',' END
              + N'"' + REPLACE(REPLACE(full_name, N'\', N'\'), N'"', N'\"') + N'":{'
              + N'"rows":'    + CAST([rows]  AS nvarchar(20))
              + N',"size":'   + CAST(size_mb AS nvarchar(30))
              + N',"writes":' + CAST(writes  AS nvarchar(20)) + N'}' AS nvarchar(4000))
    FROM r
    UNION ALL
    SELECT 2, 0, N'}}'
) q
ORDER BY part, rn;

DROP TABLE #stats;
