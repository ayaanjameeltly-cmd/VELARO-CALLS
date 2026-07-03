"""
database.py — dual SQLite (local) / PostgreSQL (Railway) abstraction.

Usage:
    from database import get_db, ph

    conn = get_db()
    conn.execute(f"SELECT * FROM leads WHERE id = {ph}", (lead_id,))
    conn.commit()
    conn.close()

`ph` is '?' on SQLite and '%s' on PostgreSQL.
All other code stays the same.
"""
import os, re, sqlite3

DATABASE_URL = os.getenv("DATABASE_URL")   # set by Railway automatically
DB_PATH      = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "velaro.db"))

# Placeholder token for parameterised queries
ph = "%s" if DATABASE_URL else "?"


# ── SQL compatibility helpers ──────────────────────────────────

def _pg_sql(sql: str) -> str:
    """Convert SQLite-flavoured SQL to PostgreSQL."""
    # ? → %s (skip inside string literals)
    out, in_str = [], False
    for ch in sql:
        if ch == "'" and not in_str:
            in_str = True; out.append(ch)
        elif ch == "'" and in_str:
            in_str = False; out.append(ch)
        elif ch == '?' and not in_str:
            out.append('%s')
        else:
            out.append(ch)
    sql = ''.join(out)

    # SQLite date arithmetic → PostgreSQL INTERVAL
    # DATE(datetime(col, '+330 minutes')) → DATE((col::timestamp) + INTERVAL '330 minutes')
    sql = re.sub(
        r"DATE\(datetime\(([^,)]+),\s*'([^']+)'\)\)",
        lambda m: f"DATE(({m.group(1).strip()}::timestamp) + INTERVAL '{m.group(2)}')",
        sql
    )
    sql = re.sub(
        r"datetime\(([^,)]+),\s*'([^']+)'\)",
        lambda m: f"(({m.group(1).strip()}::timestamp) + INTERVAL '{m.group(2)}')",
        sql
    )

    # AUTOINCREMENT → handled in schema; strip for raw SQL
    sql = sql.replace("AUTOINCREMENT", "")

    # SQLite PRAGMA → ignore
    if sql.strip().upper().startswith("PRAGMA"):
        return ""

    return sql


def _split_script(sql: str):
    """Split a multi-statement script into individual statements."""
    return [s.strip() for s in sql.split(";") if s.strip()]


# ── PostgreSQL wrappers ────────────────────────────────────────

class _PGRow(dict):
    """psycopg2 RealDictRow wrapper that supports integer indexing like sqlite3.Row."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

    def get(self, key, default=None):
        return super().get(key, default)


class _PGCursor:
    def __init__(self, cur):
        self._c       = cur
        self.rowcount = 0
        self.lastrowid = None

    def execute(self, sql, params=None):
        sql = _pg_sql(sql)
        if not sql:
            return self
        if params is not None:
            self._c.execute(sql, list(params) if not isinstance(params, (list, tuple)) else params)
        else:
            self._c.execute(sql)
        self.rowcount  = self._c.rowcount
        self.lastrowid = self._c.fetchone()[0] if sql.strip().upper().startswith("INSERT") and "RETURNING" in sql.upper() else None
        return self

    def executemany(self, sql, seq):
        sql = _pg_sql(sql)
        self._c.executemany(sql, seq)
        return self

    def fetchone(self):
        row = self._c.fetchone()
        return _PGRow(row) if row else None

    def fetchall(self):
        return [_PGRow(r) for r in (self._c.fetchall() or [])]

    def __iter__(self):
        for row in self._c:
            yield _PGRow(row)


class _PGConn:
    """Wraps a psycopg2 connection to look like sqlite3.Connection."""

    def __init__(self, conn):
        import psycopg2.extras
        self._conn = conn
        self._extras = psycopg2.extras

    def cursor(self):
        return _PGCursor(self._conn.cursor(cursor_factory=self._extras.RealDictCursor))

    def execute(self, sql, params=None):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def executemany(self, sql, seq):
        cur = self.cursor()
        cur.executemany(sql, seq)
        return cur

    def executescript(self, sql):
        """Run a multi-statement block (DDL mostly)."""
        for stmt in _split_script(sql):
            converted = _pg_sql(stmt)
            if not converted:
                continue
            # Convert SQLite DDL to PostgreSQL DDL
            converted = _pg_ddl(converted)
            try:
                self._conn.cursor().execute(converted)
            except Exception as e:
                # Ignore "already exists" errors on CREATE TABLE / ADD COLUMN
                if "already exists" in str(e) or "duplicate column" in str(e):
                    self._conn.rollback()
                else:
                    self._conn.rollback()
                    raise
        self._conn.commit()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._conn.commit()
        self._conn.close()


def _pg_ddl(sql: str) -> str:
    """Convert SQLite DDL to PostgreSQL DDL."""
    # INTEGER PRIMARY KEY AUTOINCREMENT → BIGSERIAL PRIMARY KEY
    sql = re.sub(
        r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
        "BIGSERIAL PRIMARY KEY",
        sql, flags=re.IGNORECASE
    )
    # DEFAULT CURRENT_TIMESTAMP stays the same in PG
    return sql


# ── Public API ─────────────────────────────────────────────────

def get_db():
    """Return a database connection (SQLite locally, PostgreSQL on Railway)."""
    if DATABASE_URL:
        import psycopg2
        raw = psycopg2.connect(DATABASE_URL)
        raw.autocommit = False
        return _PGConn(raw)
    else:
        conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
