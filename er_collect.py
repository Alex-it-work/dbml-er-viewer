#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
er_collect.py - read tables, columns, keys, relationships and load statistics
straight from a database and write them for the viewer:
  <database>.dbml        schema + foreign keys, plain DBML (default)
  <database>.stats.json  rows / size / writes / reads
or, with --format er, both in one <database>.er.json.

  python er_collect.py sqlserver -S SERVER -d BASE                  (Windows login)
  python er_collect.py sqlserver -S host,1433 -d BASE -U me         (SQL login)
  python er_collect.py postgres  -S host:5432 -d shop -U me
  python er_collect.py mysql     -S host:3306 -d shop -U me         (MariaDB too)
  python er_collect.py oracle    -S host:1521/SERVICE -U me [--schema OWNER]
  python er_collect.py sqlite    shop.db
  python er_collect.py odbc      --dsn "DSN=Legacy;UID=me"          (any ODBC driver)

Passwords are never taken from the command line (they would end up in the
shell history): set the ER_PASSWORD environment variable or type it when asked.

Every engine needs its own driver; only the one you use has to be installed:
  sqlserver, odbc   pip install pyodbc   (+ Microsoft ODBC Driver for SQL Server)
  postgres          pip install "psycopg[binary]"
  mysql             pip install pymysql
  oracle            pip install oracledb (thin mode, no Oracle client needed)
  sqlite            built into Python

Load numbers and where they come from:
  rows    current row count (Oracle: from the last statistics gathering)
  size    MB on disk, data + all indexes (+ LOBs where the engine reports them)
  writes  insert/update/delete activity since `counters_since`
  reads   seeks/scans/lookups (or rows read) since `counters_since`
Anything the login is not allowed to see is left out with a warning - the
schema and relationships are still written.
"""
import argparse, datetime, getpass, json, os, re, sys

FORMAT = "er/1"

DRIVERS = {
    "sqlserver": ("pyodbc",   "pip install pyodbc   (and Microsoft ODBC Driver 17/18 for SQL Server)"),
    "postgres":  ("psycopg",  'pip install "psycopg[binary]"'),
    "mysql":     ("pymysql",  "pip install pymysql"),
    "oracle":    ("oracledb", "pip install oracledb"),
    "sqlite":    ("sqlite3",  "built into Python"),
    "odbc":      ("pyodbc",   "pip install pyodbc"),
}
ALIASES = {"mssql": "sqlserver", "pg": "postgres", "postgresql": "postgres",
           "mariadb": "mysql", "ora": "oracle"}

QUIET = False
def log(*a):
    if not QUIET:
        print(*a, file=sys.stderr)

def warn(what, err):
    print("warning: %s skipped - %s" % (what, str(err).strip().splitlines()[0][:200]), file=sys.stderr)

def need(engine):
    mod, how = DRIVERS[engine]
    try:
        return __import__(mod)
    except ImportError:
        sys.exit("%s needs the %s driver: %s" % (engine, mod, how))

def password(a):
    if os.environ.get("ER_PASSWORD") is not None:
        return os.environ["ER_PASSWORD"]
    if not a.user:
        return None
    return getpass.getpass("password for %s: " % a.user)

def host_port(server, default_port):
    """'host', 'host:port' or 'host,port' -> (host, port)."""
    s = server or "localhost"
    for sep in (",", ":"):
        if sep in s and not s.startswith("["):
            h, p = s.rsplit(sep, 1)
            if p.isdigit():
                return h, int(p)
    return s, default_port

def fetch(cur, sql, params=None):
    cur.execute(sql, params) if params is not None else cur.execute(sql)
    return [tuple(r) for r in cur.fetchall()]

def num(v):
    if v is None:
        return None
    v = float(v)
    return int(v) if v.is_integer() else v

def iso(dt):
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.replace(tzinfo=None)                 # keep the server's wall clock
    return dt.replace(microsecond=0).isoformat()

def days(since, now):
    if since is None or now is None:
        return None
    if getattr(since, "tzinfo", None) is not None and getattr(now, "tzinfo", None) is not None:
        d = now - since
    else:
        d = now.replace(tzinfo=None) - since.replace(tzinfo=None)
    return round(d.total_seconds() / 86400.0, 1)

class Raw:
    """What every engine collects; assemble() turns it into the file format."""
    def __init__(self):
        self.tables = []        # (schema, table)
        self.columns = []       # (schema, table, column, type, nullable, ordinal)
        self.pk = []            # (schema, table, column, ordinal)
        self.fk = []            # (name, schema, table, column, ref_schema, ref_table, ref_column, ordinal)
        self.stats = {}         # (schema, table) -> {rows, size, writes, reads}
        self.meta = {}          # engine, server, database, version, server_started, now, counters_since, period

    def stat(self, schema, table, **kv):
        d = self.stats.setdefault((schema, table), {})
        for k, v in kv.items():
            if v is not None:
                d[k] = v

# ----------------------------------------------------------------------------
# SQL Server (2005+)
# ----------------------------------------------------------------------------
def mssql_type(name, max_len, prec, scale):
    n = name.lower()
    if n in ("varchar", "char", "varbinary", "binary"):
        return "%s(%s)" % (n, "max" if max_len == -1 else max_len)
    if n in ("nvarchar", "nchar"):
        return "%s(%s)" % (n, "max" if max_len == -1 else max_len // 2)
    if n in ("decimal", "numeric"):
        return "%s(%d,%d)" % (n, prec, scale)
    return n

def odbc_sqlserver_driver(pyodbc, wanted):
    if wanted:
        return wanted
    have = pyodbc.drivers()
    for d in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server",
              "ODBC Driver 13 for SQL Server", "SQL Server Native Client 11.0", "SQL Server"):
        if d in have:
            return d
    sys.exit("no SQL Server ODBC driver found - install 'Microsoft ODBC Driver 18 for SQL Server'")

def collect_sqlserver(a):
    pyodbc = need("sqlserver")
    drv = odbc_sqlserver_driver(pyodbc, a.odbc_driver)
    server = (a.server or "localhost").replace(":", ",") if not (a.server or "").startswith("(") else a.server
    cs = ["DRIVER={%s}" % drv, "SERVER=%s" % server, "APP=er_collect"]
    if a.database:
        cs.append("DATABASE=%s" % a.database)
    if a.user:
        cs += ["UID=%s" % a.user, "PWD={%s}" % (password(a) or "").replace("}", "}}")]
    else:
        cs.append("Trusted_Connection=yes")
    if a.trust_server_certificate:
        cs.append("TrustServerCertificate=yes")
    try:
        conn = pyodbc.connect(";".join(cs), timeout=a.timeout)
    except pyodbc.Error as e:
        msg = str(e)
        if "certificate" in msg.lower():
            msg += "\n-> the server certificate is not trusted; add --trust-server-certificate if that is expected"
        sys.exit("cannot connect: " + msg)
    cur = conn.cursor()
    r = Raw()
    db = fetch(cur, "SELECT DB_NAME(), CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(64)), @@SERVERNAME")[0]
    r.meta.update(engine="sqlserver", server=db[2] or a.server, database=db[0], version="SQL Server " + db[1])

    flt, params = "", []
    if a.schema:
        flt = " AND s.name IN (%s)" % ",".join("?" * len(a.schema))
        params = list(a.schema)
    base = (" FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id"
            " WHERE t.is_ms_shipped = 0" + flt)
    r.tables = fetch(cur, "SELECT s.name, t.name" + base, params)
    for s, t, c, ty, ml, pr, sc, nl, cid in fetch(cur,
            "SELECT s.name, t.name, c.name, ty.name, c.max_length, c.precision, c.scale, c.is_nullable, c.column_id"
            + base.replace("WHERE", "JOIN sys.columns c ON c.object_id = t.object_id"
                                    " JOIN sys.types ty ON ty.user_type_id = c.user_type_id WHERE", 1), params):
        r.columns.append((s, t, c, mssql_type(ty, ml, pr, sc), bool(nl), cid))
    # primary key; a table without one (Dynamics AX has none) gets its unique clustered index
    keys = fetch(cur,
        "SELECT s.name, t.name, c.name, ic.key_ordinal, i.is_primary_key"
        + base.replace("WHERE",
            "JOIN sys.indexes i ON i.object_id = t.object_id"
            " JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.key_ordinal > 0"
            " JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id WHERE", 1)
        + " AND (i.is_primary_key = 1 OR (i.is_unique = 1 AND i.type = 1))", params)
    has_pk = {(s, t) for s, t, c, o, p in keys if p}
    r.pk = [(s, t, c, o) for s, t, c, o, p in keys if p or (s, t) not in has_pk]
    r.fk = fetch(cur,
        "SELECT fk.name, s.name, t.name, c1.name, s2.name, t2.name, c2.name, fkc.constraint_column_id"
        + base.replace("WHERE",
            "JOIN sys.foreign_keys fk ON fk.parent_object_id = t.object_id"
            " JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id"
            " JOIN sys.columns c1 ON c1.object_id = fkc.parent_object_id AND c1.column_id = fkc.parent_column_id"
            " JOIN sys.tables t2 ON t2.object_id = fkc.referenced_object_id"
            " JOIN sys.schemas s2 ON s2.schema_id = t2.schema_id"
            " JOIN sys.columns c2 ON c2.object_id = fkc.referenced_object_id AND c2.column_id = fkc.referenced_column_id"
            " WHERE", 1), params)

    if a.no_stats:
        return r
    try:   # needs VIEW DATABASE STATE
        for s, t, rows, mb in fetch(cur,
                "SELECT s.name, t.name,"
                " SUM(CASE WHEN ps.index_id IN (0, 1) THEN ps.row_count ELSE 0 END),"
                " SUM(ps.reserved_page_count) * 8 / 1024.0"
                + base.replace("WHERE", "JOIN sys.dm_db_partition_stats ps ON ps.object_id = t.object_id WHERE", 1)
                + " GROUP BY s.name, t.name", params):
            r.stat(s, t, rows=num(rows), size=round(float(mb), 2))
    except pyodbc.Error as e:
        warn("size (needs VIEW DATABASE STATE)", e)
        for s, t, rows in fetch(cur, "SELECT s.name, t.name, SUM(p.rows)"
                + base.replace("WHERE", "JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1) WHERE", 1)
                + " GROUP BY s.name, t.name", params):
            r.stat(s, t, rows=num(rows))
    try:   # needs VIEW SERVER STATE; counted since the service started
        started, now = fetch(cur, "SELECT create_date, GETDATE() FROM sys.databases WHERE name = 'tempdb'")[0]
        r.meta.update(server_started=started, now=now, counters_since=started)
        used = {}
        for s, t, w, rd in fetch(cur,
                "SELECT s.name, t.name, SUM(us.user_updates), SUM(us.user_seeks + us.user_scans + us.user_lookups)"
                + base.replace("WHERE", "JOIN sys.dm_db_index_usage_stats us ON us.object_id = t.object_id"
                                        " AND us.database_id = DB_ID() WHERE", 1)
                + " GROUP BY s.name, t.name", params):
            used[(s, t)] = (w, rd)
        for s, t in r.tables:                 # untouched since the start = 0, not unknown
            w, rd = used.get((s, t), (0, 0))
            r.stat(s, t, writes=num(w), reads=num(rd))
    except pyodbc.Error as e:
        warn("writes/reads (needs VIEW SERVER STATE)", e)
    return r

# ----------------------------------------------------------------------------
# PostgreSQL (10+)
# ----------------------------------------------------------------------------
def collect_postgres(a):
    psycopg = need("postgres")
    host, port = host_port(a.server, 5432)
    try:
        conn = psycopg.connect(host=host, port=port, dbname=a.database or "postgres", user=a.user,
                               password=password(a), connect_timeout=a.timeout, application_name="er_collect")
    except psycopg.Error as e:
        sys.exit("cannot connect: %s" % e)
    conn.autocommit = True
    cur = conn.cursor()
    r = Raw()
    db, ver = fetch(cur, "SELECT current_database(), current_setting('server_version')")[0]
    r.meta.update(engine="postgres", server=a.server or "localhost", database=db, version="PostgreSQL " + ver)

    flt = (" AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
           " AND n.nspname NOT LIKE 'pg\\_toast%%' AND n.nspname NOT LIKE 'pg\\_temp%%'")
    params = []
    if a.schema:
        flt += " AND n.nspname = ANY(%s)"
        params = [list(a.schema)]
    rel = (" FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
           " WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition" + flt)
    r.tables = fetch(cur, "SELECT n.nspname, c.relname" + rel, params)
    for s, t, col, ty, nl, o in fetch(cur,
            "SELECT n.nspname, c.relname, a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, a.attnum"
            + rel.replace("WHERE", "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped WHERE", 1)
            + " ORDER BY 1, 2, 6", params):
        r.columns.append((s, t, col, ty, nl, o))
    for name, kind, s, t, col, s2, t2, col2, o in fetch(cur,
            "SELECT con.conname, con.contype, n.nspname, c.relname, a.attname, n2.nspname, c2.relname, a2.attname, k.ord"
            + rel.replace("WHERE",
                "JOIN pg_constraint con ON con.conrelid = c.oid AND con.contype IN ('p', 'f')"
                " CROSS JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS k(attnum, fattnum, ord)"
                " JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum"
                " LEFT JOIN pg_class c2 ON c2.oid = con.confrelid"
                " LEFT JOIN pg_namespace n2 ON n2.oid = c2.relnamespace"
                " LEFT JOIN pg_attribute a2 ON a2.attrelid = con.confrelid AND a2.attnum = k.fattnum WHERE", 1), params):
        if kind == "p":
            r.pk.append((s, t, col, o))
        else:
            r.fk.append((name, s, t, col, s2, t2, col2, o))

    if a.no_stats:
        return r
    started, now, reset = fetch(cur,
        "SELECT pg_postmaster_start_time(), now(),"
        " (SELECT stats_reset FROM pg_stat_database WHERE datname = current_database())")[0]
    since = reset if reset is not None and reset > started else started
    r.meta.update(server_started=started, now=now, counters_since=since)
    try:
        for s, t, rows, w, rd, mb in fetch(cur,
                "SELECT st.schemaname, st.relname, st.n_live_tup,"
                " st.n_tup_ins + st.n_tup_upd + st.n_tup_del,"
                " COALESCE(st.seq_scan, 0) + COALESCE(st.idx_scan, 0),"
                " pg_total_relation_size(st.relid) / 1048576.0"
                " FROM pg_stat_user_tables st"):
            r.stat(s, t, rows=num(rows), writes=num(w), reads=num(rd), size=round(float(mb), 2))
    except psycopg.Error as e:
        warn("load statistics", e)
    return r

# ----------------------------------------------------------------------------
# MySQL / MariaDB (5.6+)
# ----------------------------------------------------------------------------
def collect_mysql(a):
    pymysql = need("mysql")
    if not a.database:
        sys.exit("mysql: say which database with -d")
    host, port = host_port(a.server, 3306)
    try:
        conn = pymysql.connect(host=host, port=port, user=a.user or getpass.getuser(), password=password(a) or "",
                               database=a.database, connect_timeout=a.timeout, charset="utf8mb4")
    except pymysql.Error as e:
        sys.exit("cannot connect: %s" % e)
    cur = conn.cursor()
    r = Raw()
    ver, = fetch(cur, "SELECT VERSION()")[0]
    r.meta.update(engine="mysql", server=a.server or "localhost", database=a.database,
                  version=("MariaDB " if "mariadb" in ver.lower() else "MySQL ") + ver)
    try:   # MySQL 8 caches TABLE_ROWS / sizes for a day unless told otherwise
        cur.execute("SET SESSION information_schema_stats_expiry = 0")
    except pymysql.Error:
        pass
    db = [a.database]
    for s, t, rows, size in fetch(cur,
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_ROWS, COALESCE(DATA_LENGTH, 0) + COALESCE(INDEX_LENGTH, 0)"
            " FROM information_schema.TABLES WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_SCHEMA = %s", db):
        r.tables.append((s, t))
        if not a.no_stats:
            r.stat(s, t, rows=num(rows), size=round(float(size) / 1048576.0, 2))
    r.columns = [(s, t, c, ty, nl == "YES", o) for s, t, c, ty, nl, o in fetch(cur,
        "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, ORDINAL_POSITION"
        " FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME, ORDINAL_POSITION", db)]
    for name, s, t, c, s2, t2, c2, o in fetch(cur,
            "SELECT CONSTRAINT_NAME, TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME,"
            " REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME, ORDINAL_POSITION"
            " FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = %s"
            " AND (CONSTRAINT_NAME = 'PRIMARY' OR REFERENCED_TABLE_NAME IS NOT NULL)", db):
        if t2 is None:
            r.pk.append((s, t, c, o))
        else:
            r.fk.append((name, s, t, c, s2, t2, c2, o))

    if a.no_stats:
        return r
    now, = fetch(cur, "SELECT NOW()")[0]
    up = fetch(cur, "SHOW GLOBAL STATUS LIKE 'Uptime'")
    if up:
        started = now - datetime.timedelta(seconds=int(up[0][1]))
        r.meta.update(server_started=started, now=now, counters_since=started)
    try:   # performance_schema, counted since the server started
        for s, t, w, rd in fetch(cur,
                "SELECT OBJECT_SCHEMA, OBJECT_NAME, COUNT_WRITE, COUNT_READ"
                " FROM performance_schema.table_io_waits_summary_by_table"
                " WHERE OBJECT_TYPE = 'TABLE' AND OBJECT_SCHEMA = %s", db):
            r.stat(s, t, writes=num(w), reads=num(rd))
    except pymysql.Error as e:
        warn("writes/reads (performance_schema is off or not readable)", e)
    return r

# ----------------------------------------------------------------------------
# Oracle (10g+), python-oracledb thin mode
# ----------------------------------------------------------------------------
def oracle_type(ty, length, prec, scale, chars):
    if ty in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR"):
        return "%s(%s)" % (ty, chars or length)
    if ty == "RAW":
        return "RAW(%s)" % length
    if ty == "NUMBER" and prec is not None:
        return "NUMBER(%d,%d)" % (prec, scale or 0) if scale else "NUMBER(%d)" % prec
    return ty

def collect_oracle(a):
    oracledb = need("oracle")
    if not a.user:
        sys.exit("oracle: give the login with -U")
    try:
        conn = oracledb.connect(user=a.user, password=password(a), dsn=a.server or "localhost/FREEPDB1",
                                tcp_connect_timeout=a.timeout)
    except oracledb.Error as e:
        sys.exit("cannot connect: %s" % e)
    cur = conn.cursor()
    r = Raw()
    owners = [s.upper() for s in a.schema] if a.schema else [a.user.upper()]
    r.meta.update(engine="oracle", server=a.server, database=",".join(owners), version="Oracle " + conn.version,
                  now=fetch(cur, "SELECT SYSDATE FROM dual")[0][0])
    binds = ",".join(":o%d" % i for i in range(len(owners)))
    p = {"o%d" % i: o for i, o in enumerate(owners)}

    rows = fetch(cur,
        "SELECT owner, table_name, num_rows FROM all_tables WHERE owner IN (%s)"
        " AND nested = 'NO' AND secondary = 'N' AND dropped = 'NO'"
        " AND (iot_type IS NULL OR iot_type = 'IOT')" % binds, p)
    r.tables = [(s, t) for s, t, n in rows]
    known = set(r.tables)
    if not a.no_stats:
        for s, t, n in rows:
            r.stat(s, t, rows=num(n))
    for s, t, c, ty, ln, pr, sc, nl, o, ch in fetch(cur,
            "SELECT owner, table_name, column_name, data_type, data_length, data_precision, data_scale,"
            " nullable, column_id, char_length FROM all_tab_columns WHERE owner IN (%s)"
            " ORDER BY owner, table_name, column_id" % binds, p):
        if (s, t) in known:
            r.columns.append((s, t, c, oracle_type(ty, ln, pr, sc, ch), nl == "Y", o))
    for name, kind, s, t, c, o, s2, t2, c2 in fetch(cur,
            "SELECT c.constraint_name, c.constraint_type, c.owner, c.table_name, cc.column_name, cc.position,"
            " r.owner, r.table_name, rc.column_name"
            " FROM all_constraints c"
            " JOIN all_cons_columns cc ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name"
            " LEFT JOIN all_constraints r ON r.owner = c.r_owner AND r.constraint_name = c.r_constraint_name"
            " LEFT JOIN all_cons_columns rc ON rc.owner = r.owner AND rc.constraint_name = r.constraint_name"
            "  AND rc.position = cc.position"
            " WHERE c.owner IN (%s) AND c.constraint_type IN ('P', 'R')" % binds, p):
        if (s, t) not in known:
            continue
        if kind == "P":
            r.pk.append((s, t, c, o or 1))
        else:
            r.fk.append((name, s, t, c, s2, t2, c2, o or 1))

    if a.no_stats:
        return r
    # size: table + its indexes + its LOBs. DBA_SEGMENTS needs a grant; USER_SEGMENTS covers your own schema
    seg = None
    try:
        seg = fetch(cur, "SELECT owner, segment_name, segment_type, SUM(bytes) FROM dba_segments"
                         " WHERE owner IN (%s) GROUP BY owner, segment_name, segment_type" % binds, p)
    except oracledb.Error:
        if owners == [a.user.upper()]:
            seg = [(owners[0], n, ty, b) for n, ty, b in fetch(cur,
                   "SELECT segment_name, segment_type, SUM(bytes) FROM user_segments GROUP BY segment_name, segment_type")]
        else:
            warn("size (needs SELECT on DBA_SEGMENTS)", "no access")
    if seg is not None:
        owner_of = {}
        for s, i, t in fetch(cur, "SELECT owner, index_name, table_name FROM all_indexes WHERE owner IN (%s)" % binds, p):
            owner_of[(s, i)] = t
        for s, sg, ix, t in fetch(cur, "SELECT owner, segment_name, index_name, table_name FROM all_lobs"
                                       " WHERE owner IN (%s)" % binds, p):
            owner_of[(s, sg)] = t
            owner_of[(s, ix)] = t
        mb = {}
        for s, name, ty, b in seg:
            t = name if ty.startswith("TABLE") else owner_of.get((s, name))
            if t and (s, t) in known:
                mb[(s, t)] = mb.get((s, t), 0) + float(b)
        for (s, t), b in mb.items():
            r.stat(s, t, size=round(b / 1048576.0, 2))
    try:   # since the instance started; needs SELECT_CATALOG_ROLE or SELECT ANY DICTIONARY
        started, now = fetch(cur, "SELECT startup_time, SYSDATE FROM v$instance")[0]
        r.meta.update(server_started=started, now=now, counters_since=started)
        for s, t, stat, v in fetch(cur,
                "SELECT owner, object_name, statistic_name, SUM(value) FROM v$segment_statistics"
                " WHERE owner IN (%s) AND object_type LIKE 'TABLE%%'"
                " AND statistic_name IN ('db block changes', 'logical reads')"
                " GROUP BY owner, object_name, statistic_name" % binds, p):
            if (s, t) in known:
                r.stat(s, t, **{"writes" if stat == "db block changes" else "reads": num(v)})
        r.meta["period"] = "writes = db block changes, reads = logical reads, since instance start"
    except oracledb.Error as e:
        warn("instance counters (V$INSTANCE / V$SEGMENT_STATISTICS); using ALL_TAB_MODIFICATIONS instead", e)
        try:
            for s, t, w in fetch(cur, "SELECT table_owner, table_name, SUM(inserts + updates + deletes)"
                                      " FROM all_tab_modifications WHERE table_owner IN (%s) AND partition_name IS NULL"
                                      " GROUP BY table_owner, table_name" % binds, p):
                r.stat(s, t, writes=num(w))
            for s, t in r.tables:
                r.stats.setdefault((s, t), {}).setdefault("writes", 0)
            r.meta["period"] = "writes since each table's last statistics gathering"
        except oracledb.Error as e2:
            warn("writes", e2)
    return r

# ----------------------------------------------------------------------------
# SQLite
# ----------------------------------------------------------------------------
def collect_sqlite(a):
    import sqlite3
    path = a.target or a.database
    if not path or not os.path.exists(path):
        sys.exit("sqlite: give the database file")
    conn = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(path).replace("\\", "/"), uri=True)
    cur = conn.cursor()
    r = Raw()
    r.meta.update(engine="sqlite", server=None, database=os.path.basename(path), version="SQLite " + sqlite3.sqlite_version)
    q = lambda n: '"%s"' % n.replace('"', '""')
    names = [n for n, in fetch(cur, "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for t in names:
        r.tables.append(("main", t))
        info = fetch(cur, "PRAGMA table_info(%s)" % q(t))
        pkcols = [c[1] for c in sorted((c for c in info if c[5]), key=lambda c: c[5])]
        for cid, name, ty, notnull, dflt, pk in info:
            r.columns.append(("main", t, name, ty or "", not notnull, cid + 1))
            if pk:
                r.pk.append(("main", t, name, pk))
        for fid, seq, ref, frm, to, *rest in fetch(cur, "PRAGMA foreign_key_list(%s)" % q(t)):
            if to is None:                           # REFERENCES parent  ->  the parent's primary key
                ppk = [c for c in fetch(cur, "PRAGMA table_info(%s)" % q(ref)) if c[5]]
                ppk.sort(key=lambda c: c[5])
                to = ppk[seq][1] if seq < len(ppk) else frm
            r.fk.append(("fk_%s_%d" % (t, fid), "main", t, frm, "main", ref, to, seq + 1))
        if not a.no_stats and not a.no_count:
            r.stat("main", t, rows=float(fetch(cur, "SELECT COUNT(*) FROM %s" % q(t))[0][0]))
    if not a.no_stats:
        try:   # dbstat is compiled into most builds; size = the table + its indexes
            owner = {n: tb for n, tb in fetch(cur, "SELECT name, tbl_name FROM sqlite_master WHERE type IN ('table', 'index')")}
            for n, b in fetch(cur, "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"):
                t = owner.get(n)
                if t in names:
                    d = r.stats.setdefault(("main", t), {})
                    d["size"] = round(d.get("size", 0) + b / 1048576.0, 3)
        except sqlite3.Error as e:
            warn("size (this SQLite has no dbstat)", e)
    return r

SYSTEM_SCHEMAS = {"sys", "information_schema", "pg_catalog", "mysql", "performance_schema",
                  "sysibm", "syscat", "sysstat", "systools", "sys_", "system", "ctxsys", "mdsys", "xdb"}

# ----------------------------------------------------------------------------
# Any ODBC driver:schema and relationships through the ODBC catalog functions.
# No load counters - there is no portable way to read them - only --count.
# ----------------------------------------------------------------------------
def collect_odbc(a):
    pyodbc = need("odbc")
    cs = a.dsn or a.target
    if not cs:
        sys.exit('odbc: give a connection string, e.g. --dsn "DSN=Legacy;UID=me"')
    if "PWD=" not in cs.upper() and os.environ.get("ER_PASSWORD") is not None:
        cs = cs.rstrip(";") + ";PWD={%s}" % os.environ["ER_PASSWORD"].replace("}", "}}")
    try:
        conn = pyodbc.connect(cs, timeout=a.timeout)
    except pyodbc.Error as e:
        sys.exit("cannot connect: %s" % e)
    cur = conn.cursor()
    r = Raw()
    get = lambda k: (lambda: conn.getinfo(k))
    def info(k, default=None):
        try:
            return get(k)()
        except pyodbc.Error:
            return default
    r.meta.update(engine="odbc", server=info(pyodbc.SQL_SERVER_NAME), database=info(pyodbc.SQL_DATABASE_NAME),
                  version="%s %s" % (info(pyodbc.SQL_DBMS_NAME, "?"), info(pyodbc.SQL_DBMS_VER, "")))
    wanted = set(s.lower() for s in a.schema) if a.schema else None
    for row in cur.tables(tableType="TABLE").fetchall():
        s, t = row.table_schem, row.table_name
        if wanted is None and (s or "").lower() in SYSTEM_SCHEMAS:
            continue
        if wanted is None or (s or "").lower() in wanted:
            r.tables.append((s, t))
    log("odbc: %d tables, reading columns and keys one table at a time..." % len(r.tables))
    for i, (s, t) in enumerate(r.tables):
        for c in cur.columns(table=t, schema=s).fetchall():
            size = getattr(c, "column_size", None)
            ty = c.type_name + ("(%s)" % size if size and "char" in c.type_name.lower() else "")
            r.columns.append((s, t, c.column_name, ty, bool(c.nullable), c.ordinal_position))
        for k in cur.primaryKeys(table=t, schema=s).fetchall():
            r.pk.append((s, t, k.column_name, k.key_seq))
        try:
            for f in cur.foreignKeys(foreignTable=t, foreignSchema=s).fetchall():
                r.fk.append((f.fk_name or "fk", f.fktable_schem, f.fktable_name, f.fkcolumn_name,
                             f.pktable_schem, f.pktable_name, f.pkcolumn_name, f.key_seq))
        except pyodbc.Error as e:
            if i == 0:
                warn("foreign keys (driver does not support SQLForeignKeys)", e)
        if (i + 1) % 200 == 0:
            log("  %d/%d" % (i + 1, len(r.tables)))
    if a.count and not a.no_stats:
        qc = info(pyodbc.SQL_IDENTIFIER_QUOTE_CHAR, '"') or '"'
        q = lambda n: qc + n + qc
        for s, t in r.tables:
            try:
                n = fetch(cur, "SELECT COUNT(*) FROM %s" % ((q(s) + "." if s else "") + q(t)))[0][0]
                r.stat(s, t, rows=float(n))
            except pyodbc.Error as e:
                warn("row count of %s" % t, e)
    return r

COLLECT = {"sqlserver": collect_sqlserver, "postgres": collect_postgres, "mysql": collect_mysql,
           "oracle": collect_oracle, "sqlite": collect_sqlite, "odbc": collect_odbc}

# ----------------------------------------------------------------------------
# assemble the file
# ----------------------------------------------------------------------------
def assemble(r):
    # bare table names unless two schemas share one, then schema.table
    count = {}
    for s, t in r.tables:
        count[t.lower()] = count.get(t.lower(), 0) + 1
    key = {(s, t): (t if count[t.lower()] == 1 else "%s.%s" % (s, t)) for s, t in r.tables}

    cols, pk = {}, {}
    for s, t, c, o in sorted(r.pk, key=lambda x: (str(x[0]), x[1], x[3] or 0)):
        pk.setdefault((s, t), []).append(c)
    for s, t, c, ty, nl, o in sorted(r.columns, key=lambda x: (str(x[0]), x[1], x[5] or 0)):
        if (s, t) in key:
            col = {"name": c, "type": ty}
            if c in pk.get((s, t), ()):
                col["pk"] = True
            if not nl:
                col["null"] = False
            cols.setdefault((s, t), []).append(col)

    tables = {}
    for s, t in sorted(r.tables, key=lambda x: key[x].lower()):
        d = {"schema": s} if s not in (None, "main") else {}
        d["columns"] = cols.get((s, t), [])
        st = {k: v for k, v in r.stats.get((s, t), {}).items() if v is not None}
        if st:
            d["stats"] = st
        tables[key[(s, t)]] = d

    groups = {}
    for name, s, t, c, s2, t2, c2, o in r.fk:
        groups.setdefault((name, s, t), {"to": (s2, t2), "on": []})["on"].append((o or 0, c, c2))
    rels = []
    for (name, s, t), g in sorted(groups.items(), key=lambda x: (key.get((x[0][1], x[0][2]), ""), x[0][0] or "")):
        if (s, t) not in key or g["to"] not in key:
            continue                           # points outside what was collected
        on = [[c, c2] for o, c, c2 in sorted(g["on"])]
        own = pk.get((s, t), [])
        one = bool(own) and sorted(own) == sorted(c for c, c2 in on)
        rels.append({"from": key[(s, t)], "to": key[g["to"]], "on": on,
                     "kind": "fk", "card": "1:1" if one else "N:1", "name": name})

    m = r.meta
    out = {"format": FORMAT,
           "source": {k: m.get(k) for k in ("engine", "server", "database", "version") if m.get(k)},
           "generated_at": iso(m.get("now") or datetime.datetime.now())}
    if m.get("server_started") is not None:
        out["server_started"] = iso(m["server_started"])
        out["uptime_days"] = days(m["server_started"], m.get("now"))
    if m.get("counters_since") is not None:
        out["counters_since"] = iso(m["counters_since"])
        out["counters_days"] = days(m["counters_since"], m.get("now"))
        out["period"] = m.get("period") or "writes/reads since " + iso(m["counters_since"])[:16].replace("T", " ")
    elif m.get("period"):
        out["period"] = m["period"]
    out["tables"] = tables
    out["relations"] = rels
    return out

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

def dq(name):
    """DBML identifier: bare when it can be, "quoted" otherwise (AX has names like _X$)."""
    return name if IDENT.match(name) else '"%s"' % name.replace('"', "'")

def write_dbml(out, path):
    """Schema and relationships as plain DBML. What DBML has no field for rides on
    conventions the viewer reads and other DBML tools simply ignore:
    the ref name prefix is the kind (fk_...), a composite key is Table.(a, b)."""
    src = out["source"]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("// %s, database %s - er_collect.py, %s\n\n" % (
            src.get("version", src.get("engine", "")), src.get("database", "?"), out["generated_at"]))
        for name, t in out["tables"].items():
            f.write("Table %s {\n" % dq(name))
            pk = [c["name"] for c in t["columns"] if c.get("pk")]
            for c in t["columns"]:
                ty = c["type"] or "unknown"
                if re.search(r'[\s"]', ty):
                    ty = '"%s"' % ty.replace('"', "'")
                st = (["pk"] if c.get("pk") and len(pk) == 1 else []) + (["not null"] if c.get("null") is False else [])
                f.write("  %s %s%s\n" % (dq(c["name"]), ty, " [%s]" % ", ".join(st) if st else ""))
            if len(pk) > 1:                    # DBML allows one column [pk]; a composite key is an index
                f.write("  indexes {\n    (%s) [pk]\n  }\n" % ", ".join(dq(c) for c in pk))
            f.write("}\n\n")
        used = set()
        for r in out["relations"]:
            name = re.sub(r"\W", "_", "%s_%s" % (r.get("kind") or "fk", r.get("name") or r["from"]))
            base, k = name, 2
            while name in used:                # ref names must be unique
                name, k = "%s_%d" % (base, k), k + 1
            used.add(name)
            end = lambda t, cols: "%s.%s" % (dq(t), dq(cols[0]) if len(cols) == 1
                                             else "(%s)" % ", ".join(dq(c) for c in cols))
            op = "-" if r.get("card") == "1:1" else ">"
            f.write("Ref %s: %s %s %s\n" % (name, end(r["from"], [p[0] for p in r["on"]]), op,
                                            end(r["to"], [p[1] for p in r["on"]])))

def write_stats(out, path):
    """Load numbers in the stats.json shape the viewer (and the sql/ scripts) use."""
    head = {k: v for k, v in out.items() if k not in ("tables", "relations", "format")}
    rows = [(n, t["stats"]) for n, t in out["tables"].items() if t.get("stats")]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(head, ensure_ascii=False)[:-1] + ',\n"tables": {\n')
        for i, (n, s) in enumerate(rows):
            f.write(" %s: %s%s\n" % (json.dumps(n, ensure_ascii=False), json.dumps(s), "," if i < len(rows) - 1 else ""))
        f.write("}}\n")

def write(out, path):
    # one table / one relationship per line: readable, diffable, and not 50 MB of indentation
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        head = {k: v for k, v in out.items() if k not in ("tables", "relations")}
        f.write(json.dumps(head, ensure_ascii=False)[:-1] + ',\n"tables": {\n')
        items = list(out["tables"].items())
        for i, (k, v) in enumerate(items):
            f.write(" %s: %s%s\n" % (json.dumps(k, ensure_ascii=False), json.dumps(v, ensure_ascii=False),
                                      "," if i < len(items) - 1 else ""))
        f.write('},\n"relations": [\n')
        for i, rel in enumerate(out["relations"]):
            f.write(" %s%s\n" % (json.dumps(rel, ensure_ascii=False), "," if i < len(out["relations"]) - 1 else ""))
        f.write("]}\n")

def main():
    global QUIET
    ap = argparse.ArgumentParser(description="Read schema, relationships and load statistics from a database "
                                             "into one .er.json for the ER viewer.")
    ap.add_argument("engine", help="sqlserver | postgres | mysql (mariadb) | oracle | sqlite | odbc")
    ap.add_argument("target", nargs="?", help="sqlite: the database file; odbc: the connection string")
    ap.add_argument("-S", "--server", help="host, host:port, host,port; oracle: host:port/service")
    ap.add_argument("-d", "--database", help="database name")
    ap.add_argument("-U", "--user", help="login (password from ER_PASSWORD or asked); "
                                         "sqlserver without -U uses Windows authentication")
    ap.add_argument("--schema", action="append", help="only this schema (repeatable; oracle: owner)")
    ap.add_argument("--dsn", help="odbc: full connection string")
    ap.add_argument("--odbc-driver", help="sqlserver: ODBC driver name (default: the newest installed)")
    ap.add_argument("--trust-server-certificate", action="store_true",
                    help="sqlserver: accept a self-signed server certificate")
    ap.add_argument("--no-stats", action="store_true", help="schema and relationships only")
    ap.add_argument("--count", action="store_true", help="odbc: SELECT COUNT(*) every table for row counts")
    ap.add_argument("--no-count", action="store_true", help="sqlite: skip COUNT(*)")
    ap.add_argument("--timeout", type=int, default=15, help="connect timeout, seconds")
    ap.add_argument("--format", choices=("dbml", "er"), default="dbml",
                    help="dbml: <name>.dbml + <name>.stats.json (default); er: one <name>.er.json")
    ap.add_argument("-o", "--out", help="output name without extension (default: the database name)")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args()
    QUIET = a.quiet
    engine = ALIASES.get(a.engine.lower(), a.engine.lower())
    if engine not in COLLECT:
        ap.error("unknown engine %r - one of: %s" % (a.engine, ", ".join(COLLECT)))
    if a.schema:
        a.schema = [x.strip() for s in a.schema for x in s.split(",") if x.strip()]

    t0 = datetime.datetime.now()
    raw = COLLECT[engine](a)
    out = assemble(raw)
    base = a.out or os.path.splitext(os.path.basename(
        str(out["source"].get("database") or engine)))[0].replace(",", "_")
    base = re.sub(r"(\.er\.json|\.dbml|\.stats\.json|\.json)$", "", base, flags=re.I)
    nstat = sum(1 for t in out["tables"].values() if "stats" in t)
    if a.format == "er":
        files = [base + ".er.json"]
        write(out, files[0])
    else:
        files = [base + ".dbml"]
        write_dbml(out, files[0])
        if nstat:
            files.append(base + ".stats.json")
            write_stats(out, files[1])
    log("%s: %d tables, %d columns, %d relationships, load data for %d tables (%.1f s) -> %s" % (
        out["source"].get("version", engine), len(out["tables"]),
        sum(len(t["columns"]) for t in out["tables"].values()), len(out["relations"]), nstat,
        (datetime.datetime.now() - t0).total_seconds(), ", ".join(files)))

if __name__ == "__main__":
    main()
