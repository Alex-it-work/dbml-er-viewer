# dbml-er-viewer

Turn a [DBML](https://dbml.dbdiagram.io/) schema into an **untangled, interactive ER diagram** — a single self-contained HTML file you open in any browser.

No grid of alphabetically-ordered tables with relationship lines crossing the whole page. A force-directed layout pulls linked tables side-by-side and aligns them by the row their key sits on, so the connections stay **short and mostly horizontal**, with as few crossings as possible.

![Untangled diagram](docs/preview.png)

## Why

Auto-generated ER diagrams usually drop tables onto a grid in name order. With a few hub tables (a `users`, an `orders`, a giant central table) the relationship lines turn into spaghetti and you can't tell where a link actually goes.

This tool re-lays-out the schema from the source and ships a viewer where you can **click a table to light up only its relationships** — everything else dims, and a side panel lists exactly which columns link to which table.

![Highlight a table](docs/highlight.png)

Need a closer look at one table? Double-click it — what points at it on the left, what it points to on the right, everything else out of the way. `Esc` brings the schema back.

![Focus on a table](docs/focus.png)

## Features

- **Untangled layout** — force-directed placement; multiple random seeds are tried and the one with the fewest line crossings wins. Hub tables drift to the center, satellites cluster around them, isolated tables get their own tidy row.
- **Click to highlight** — select a table and only its links stay lit; the rest fades. A side panel shows linked tables split into *References (N→1)* and *Referenced by (1→N)*, each with the exact `column → table.column`.
- **Focus one table** — double-click a table (or select it and hit **Focus**) to see it on its own, laid out for reading: the table in full in the middle, everything that *points at it* on the left, everything it *points to* on the right, all other tables and lines hidden. Neighbours are drawn as **compact cards showing only the linking column**, and they are packed into as many columns as it takes to keep the view screen-shaped — a hub with 45 links stays a readable 2880×1800 instead of a 16000px strip. **Click a link (or the card) to open that table in full** and click again to fold it back — the view stays put while you look. Double-click a neighbour (or click it in the side panel) to make it the new centre. **Back to schema** or **Esc** returns to the full diagram exactly as you left it — positions, manual placement, selection and zoom/pan. Exporting while focused writes just that neighbourhood.
- **Heatmap from a stats.json** - colour the tables by **rows**, **write activity** or **size**: a strip under each header plus the value, on a logarithmic scale (a 10-row lookup and a 100M-row log have to stay distinguishable). Drop a `stats.json` next to the schema - no server, no credentials, nothing leaves the machine. Table names are matched loosely (`public.orders`, `"Orders"`, `ORDERS` all find `orders`) and you get a report: *matched 62/78 - 16 without data - 2 unknown names*. Ready-made queries that emit the JSON straight out of the database live in [`sql/`](sql/).
- **Search** any table by name and jump to it.
- **Navigation** — zoom with the mouse wheel (centred on the cursor), pan by dragging the canvas (left button on empty space, or middle button anywhere) — the view follows the cursor like a hand tool. No scrollbars; `+` / `−` / `Fit` buttons too.
- **Merge several .dbml files** — drop more files (or **+ Add .dbml**) to grow the diagram: new tables are drawn in, tables that already exist are *updated, not duplicated*, and duplicate relationships are dropped. **The later file wins**, so re-exporting a changed schema on top of the current one just refreshes it. Existing tables keep their position — only the newcomers get placed, next to whatever they link to.
- **Edit mode** — drag tables around; the relationship lines re-route live. A **Snap** step (10/20/26/50 px, with a faint grid) keeps manual placement aligned without pixel-fiddling.
- **Re-layout** — recompute the whole arrangement from scratch when you want a fresh untangle.
- **Export SVG** — save the current (possibly edited) diagram as a static `.svg`.
- **Cardinality markers** — crow's-foot for *many*, bar for *one*; supports `>`, `<`, `-` (one-to-one) and `<>`.
- **PK / FK badges** — primary keys get a gold `PK`; columns used in a relationship get a 🔗.

## Two ways to use it

### 1. In the browser — no install, no Python

Open **[`index.html`](index.html)** in any browser and **drag a `.dbml` file onto it** (or click *Open .dbml*). Parsing, layout and rendering all happen client-side in JavaScript — nothing is uploaded anywhere, nothing to install. It loads a sample on first open so you can try it immediately.

> Tip: you can host `index.html` on GitHub Pages and share a link, or just keep the file locally and double-click it.

### 2. From the command line — Python

For batch use / CI. **Python 3.8+**, standard library only, nothing to `pip install`.

```bash
python dbml_to_er.py schema.dbml
```

Several files are merged into one diagram, later file wins on duplicates:

```bash
python dbml_to_er.py core.dbml extras.dbml newer.dbml
```

writes next to the first input:

| file | what it is |
|------|------------|
| `schema.html` | interactive viewer (open in a browser) |
| `schema.svg`  | static untangled diagram |

Options:

```text
-o, --out NAME    output basename (default: input name)
    --seeds N     layout attempts; more = fewer crossings, slower (default 10)
    --no-svg      skip the static .svg
    --no-html     skip the interactive .html
-q, --quiet       no progress output
```

Try it on the bundled example:

```bash
python dbml_to_er.py examples/sample.dbml --seeds 16
```

Both paths share the same DBML parser and force-directed layout — the Python script and `index.html` are direct ports of each other.

## Straight from a database: `er_collect.py`

No DBML at hand? Read the schema, the foreign keys and the load numbers right
out of the database. You get a plain `.dbml` and a `.stats.json` - open the
first in `index.html` (or pass it to `dbml_to_er.py`), drop the second on top:

```bash
python er_collect.py sqlserver -S SERVER -d BASE                # Windows login
python er_collect.py postgres  -S host:5432 -d shop -U me
python er_collect.py mysql     -S host:3306 -d shop -U me       # MariaDB too
python er_collect.py oracle    -S host:1521/SERVICE -U me --schema OWNER
python er_collect.py sqlite    shop.db
python er_collect.py odbc      --dsn "DSN=Legacy;UID=me" --count  # anything with an ODBC driver
```

| engine | driver (`pip install ...`) | load numbers |
|---|---|---|
| SQL Server 2005+ | `pyodbc` + Microsoft ODBC Driver | rows, size, writes, reads since service start |
| PostgreSQL 10+ | `psycopg[binary]` | rows, size, writes, reads since start / stats reset |
| MySQL 5.6+ / MariaDB | `pymysql` | rows, size, writes, reads since start (performance_schema) |
| Oracle 10g+ | `oracledb` (thin, no client) | rows, size, writes/reads since instance start, or since last stats gathering |
| SQLite | built in | rows, size |
| any ODBC source | `pyodbc` | rows with `--count` |

Only the driver for the engine you use is needed.

**Signing in.** SQL Server without `-U` uses Windows authentication - your own
Windows account, no password anywhere. Everything else (and SQL Server with
`-U`) takes the password from the `ER_PASSWORD` environment variable or asks
for it, hidden; it never goes on the command line, where it would stay in the
shell history, and is never written to any file. The script only reads the
system catalogs. A read-only login is enough; what it may not see
(`VIEW SERVER STATE`, `pg_read_all_stats`, `DBA_SEGMENTS`, `V$` views...) is
skipped with a warning - the schema and relationships are still written.

## Relationships the database does not know about

Some systems keep their relationships outside the database - Dynamics AX
declares them in its own metadata and creates no foreign keys at all. Export
them as DBML (the AX job does), read the tables and the load with
`er_collect.py`, and merge the two in the viewer: table names match ignoring
case (`INVENTTRANS` = `InventTrans`), AX's 30-character SQL names (a longer
name is cut and the table id appended) find their table, and a link follows
the column spelling that exists.

What plain DBML has no field for rides on three conventions. They are valid
DBML, so other DBML tools still read the file - they just see ordinary refs
and comments:

```dbml
// where the link is declared: the ref name prefix  fk_ / edt_ / table_ / manual_ / inferred_
Ref edt_InventTrans_ItemId: InventTrans.ItemId > InventTable.ItemId

// a composite key is one ref, every column pair is kept
Ref table_Lines_Order: Lines.(OrderId, LineNum) > Orders.(Id, LineNum)

// fixed-value conditions go into a comment after the ref
Ref table_Trans_Origin: Trans.OriginId > Origin.RecId // where to.Category = 2
```

The side panel shows the kind, all key pairs and the conditions of each link.

### One file instead of two: `.er.json`

`er_collect.py --format er` writes schema, relationships and load into a single
`.er.json`; the viewer opens it like a `.dbml` and merges it with other files.

```json
{
  "format": "er/1",
  "server_started": "2026-09-12T08:30:00", "uptime_days": 45.0,
  "tables": {
    "orders": { "columns": [ { "name": "id", "type": "int", "pk": true },
                             { "name": "user_id", "type": "int" } ],
                "stats": { "rows": 1450000, "size": 820, "writes": 12034567, "reads": 991000 } }
  },
  "relations": [
    { "from": "orders", "to": "users", "on": [["user_id", "id"]],
      "kind": "fk", "card": "N:1", "where": { "from.status": 1 } }
  ]
}
```

## Where stats.json comes from

Nothing to install: the database can build the JSON itself. Run the query for
your engine, save the single returned cell as `stats.json`, and drop that file
onto the diagram.

| engine | snapshot | over a period |
|---|---|---|
| PostgreSQL | [`sql/postgres_now.sql`](sql/postgres_now.sql) | [`sql/postgres_period.sql`](sql/postgres_period.sql) |
| SQL Server | [`sql/sqlserver_now.sql`](sql/sqlserver_now.sql) | [`sql/sqlserver_period.sql`](sql/sqlserver_period.sql) |
| MySQL / MariaDB | [`sql/mysql_now.sql`](sql/mysql_now.sql) | — |

On SQL Server the result shows up as a link in the SSMS grid — click it to get
the full text. To write the file directly instead:

```text
sqlcmd -S SERVER -d BASE -E -i sql\sqlserver_now.sql -o stats.json -h -1 -W -r1
```

Both the object form (`"tables": { "orders": {...} }`) and the list form that
`FOR JSON PATH` produces (`"tables": [ { "table": "orders", ... } ]`) are
accepted.

```json
{
  "period": "2025-09-01 .. 2026-09-01",
  "tables": {
    "orders":    { "rows": 1450000,  "writes": 12034567, "size": 820 },
    "audit_log": { "rows": 58000000, "writes": 85000000, "size": 3300 }
  }
}
```

`size` is in MB. Key aliases are accepted (`rowCount`, `row_count`, `n_live_tup`,
`total_writes`, `size_mb`, ...), so output from your own query usually just works,
and any metric you leave out simply gets no colour.

**About periods.** A database keeps no history of its own: `pg_stat_user_tables`
holds counters accumulated since the last `pg_stat_reset()`, SQL Server's
`user_updates` resets when the service restarts, and sizes are instantaneous. To get *"activity over the last year"* you have to collect
snapshots and subtract one from another -
[`sql/postgres_period.sql`](sql/postgres_period.sql) sets that up: a snapshot
table, a one-line insert to run on a schedule, and a query that turns any two
dates into a `stats.json`. If you already run monitoring (Zabbix, Prometheus +
postgres_exporter, pgwatch, Performance Insights), export from there instead -
the viewer only cares about the JSON shape above.

## DBML support

Parses the common DBML constructs:

- `Table name { column type [settings] }` — with `schema.table` and `as alias`.
- Column settings: `[pk]` / `[primary key]`, and inline `[ref: > other.table.col]`.
- `Ref:` short form and `Ref { ... }` block form, operators `>`, `<`, `-`, `<>`.
- Composite ref endpoints `Table.(a, b)` - every column pair is kept, the first one anchors the line.
- Composite primary keys in `indexes { (a, b) [pk] }`, quoted names and types (`"timestamp with time zone"`).
- `Project`, `Enum`, `TableGroup`, `Note` blocks are skipped.
- Input is read as UTF-8 / UTF-16 / CP1251 automatically.

## How the layout works

1. Each table becomes a card whose height comes from its column count; relationships are anchored to the exact row of the key column.
2. A force-directed pass runs over the connected tables:
   - **edge attraction** aligns the two anchor rows vertically (links become near-horizontal) and keeps linked tables a fixed gap apart horizontally;
   - **box-aware repulsion** keeps cards from piling up.
3. A hard overlap-removal pass guarantees no two cards touch.
4. Steps 2–3 run for several random seeds; the layout with the fewest straight-line crossings is kept.
5. Isolated tables (no relationships) are packed into a separate row so they don't clutter the core.

On the bundled 17-table example this brings line crossings down to ~1.

## License

MIT — see [LICENSE](LICENSE).
