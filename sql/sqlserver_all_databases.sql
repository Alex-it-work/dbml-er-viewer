-- ============================================================================
--  SQL Server: ВСЕ базы инстанса одним запросом  ->  stats.json
--
--  Обходит все доступные пользовательские БД и собирает по каждой таблице
--  строки / размер / активность записи. Имена выходят полными:
--      MyDb.dbo.CustTable
--  Вьювер при сопоставлении отбрасывает префиксы, так что такое имя найдёт
--  таблицу CustTable в схеме. ВНИМАНИЕ: если одинаковые имена таблиц есть в
--  нескольких базах, в диаграмме они схлопнутся в одну - тогда лучше снимать
--  по одной базе (sqlserver_now.sql).
--
--  Нужны права VIEW DATABASE STATE в каждой БД (или VIEW SERVER STATE на
--  инстансе). Базы, куда доступа нет, молча пропускаются.
--
--  Сохрани вернувшуюся ячейку как stats.json, либо сразу в файл:
--     sqlcmd -S SERVER -E -i sqlserver_all_databases.sql -o stats.json -y 0 -h -1
-- ============================================================================
SET NOCOUNT ON;

IF OBJECT_ID('tempdb..#stats') IS NOT NULL DROP TABLE #stats;
CREATE TABLE #stats (
    full_name nvarchar(400),
    [rows]    bigint,
    size_mb   decimal(18,1),
    writes    bigint
);

DECLARE @db sysname, @sql nvarchar(max);

DECLARE db_cur CURSOR LOCAL FAST_FORWARD FOR
    SELECT name FROM sys.databases
    WHERE database_id > 4                 -- пропускаем master/model/msdb/tempdb
      AND state_desc = 'ONLINE'
      AND HAS_DBACCESS(name) = 1
      AND is_read_only = 0;

OPEN db_cur;
FETCH NEXT FROM db_cur INTO @db;
WHILE @@FETCH_STATUS = 0
BEGIN
    SET @sql = N'
      USE ' + QUOTENAME(@db) + N';
      SELECT ' + QUOTENAME(@db, '''') + N' + ''.'' + s.name + ''.'' + t.name,
             SUM(CASE WHEN ps.index_id IN (0,1) THEN ps.row_count ELSE 0 END),
             CAST(SUM(ps.reserved_page_count) * 8.0 / 1024 AS decimal(18,1)),
             COALESCE(MAX(us.user_updates), 0)
      FROM sys.tables t
      JOIN sys.schemas s              ON s.schema_id = t.schema_id
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
        PRINT 'пропущена БД ' + @db + ': ' + ERROR_MESSAGE();   -- нет прав / недоступна
    END CATCH

    FETCH NEXT FROM db_cur INTO @db;
END
CLOSE db_cur;
DEALLOCATE db_cur;

SELECT
    (SELECT full_name AS [table], [rows], size_mb AS [size], writes
     FROM #stats
     ORDER BY writes DESC, [rows] DESC          -- самые активные сверху
     FOR JSON PATH) AS tables,
    CONVERT(varchar(33), SYSDATETIME(), 126)    AS generated_at,
    'накопительно с момента запуска службы'     AS period
FOR JSON PATH, WITHOUT_ARRAY_WRAPPER;

DROP TABLE #stats;
