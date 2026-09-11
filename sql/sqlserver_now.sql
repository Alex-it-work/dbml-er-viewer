-- ============================================================================
--  SQL Server: срез "прямо сейчас"  ->  stats.json        (нужен SQL Server 2016+)
--
--  Запусти в SSMS / Azure Data Studio. Результат - ОДНА ячейка с JSON:
--  в SSMS она отображается как ссылка, клик открывает полный текст во вкладке.
--  Сохрани как stats.json и перетащи на диаграмму.
--
--  Чтобы сразу получить файл, без копирования из грида:
--     sqlcmd -S SERVER -d BASE -E -i sqlserver_now.sql -o stats.json -y 0 -h -1
--  (-y 0 = не обрезать длинные значения, -h -1 = без заголовков)
--
--  ВАЖНО про writes: user_updates - НАКОПИТЕЛЬНЫЙ счётчик, который обнуляется
--  при перезапуске службы SQL Server (в Azure SQL - при failover). Это "сколько
--  всего с момента старта", а не "за период". Для периода - sqlserver_period.sql.
--  rows из dm_db_partition_stats - точное значение, не оценка.
-- ============================================================================
SELECT
    (SELECT
         t.name                                            AS [table],
         -- строки считаем только по куче/кластерному индексу (index_id 0 или 1),
         -- иначе каждый некластерный индекс добавит свою копию строк
         SUM(CASE WHEN ps.index_id IN (0,1) THEN ps.row_count ELSE 0 END)          AS [rows],
         -- размер = таблица + все её индексы, страница 8 КБ -> МБ
         CAST(SUM(ps.reserved_page_count) * 8.0 / 1024 AS decimal(18,1))           AS [size],
         COALESCE(MAX(us.user_updates), 0)                                        AS [writes]
     FROM sys.tables t
     JOIN sys.dm_db_partition_stats ps
           ON ps.object_id = t.object_id
     LEFT JOIN sys.dm_db_index_usage_stats us
           ON us.object_id = t.object_id
          AND us.database_id = DB_ID()
          AND us.index_id IN (0,1)
     WHERE t.is_ms_shipped = 0
     GROUP BY t.name
     -- если имена таблиц повторяются в разных схемах, замени t.name выше на:
     --     s.name + '.' + t.name   (и добавь JOIN sys.schemas s ON s.schema_id = t.schema_id)
     -- вьювер сам отбросит префикс схемы при сопоставлении
     FOR JSON PATH) AS tables,
    CONVERT(varchar(33), SYSDATETIME(), 126)               AS generated_at,
    'накопительно с момента запуска службы'                AS period
FOR JSON PATH, WITHOUT_ARRAY_WRAPPER;
