"""MySQL connector — SQLAlchemy async + aiomysql."""

from __future__ import annotations

import time
from typing import Any, Optional

from sqlalchemy import text

from app.db_connectors.base import (
    ConnectionSpec, ConnectionTestResult,
    DatabaseConnector, DiscoveredSchema, QueryResult,
)
from app.db_connectors.pool import engine_pool, spec_key
from app.db_connectors.postgres_connector import _assemble_schema


class MySQLConnector:
    name = "mysql"

    async def test(self, spec: ConnectionSpec) -> ConnectionTestResult:
        try:
            eng = await engine_pool().get(spec)
            async with eng.connect() as conn:
                result = await conn.execute(text("SELECT DATABASE(), VERSION()"))
                dbname, version = result.first()
            return ConnectionTestResult(ok=True, message="Connected", version=str(version))
        except Exception as e:
            await engine_pool().invalidate(spec_key(spec))
            return ConnectionTestResult(ok=False, message=self._friendly_err(e))

    async def discover(self, spec: ConnectionSpec) -> DiscoveredSchema:
        eng = await engine_pool().get(spec)
        async with eng.connect() as conn:
            # Tables + views scoped to the current database
            tbl_sql = text("""
                SELECT table_schema, table_name, table_type
                  FROM information_schema.tables
                 WHERE table_schema = DATABASE()
                 ORDER BY table_schema, table_name
            """)
            tables_raw = (await conn.execute(tbl_sql)).all()

            col_sql = text("""
                SELECT table_schema, table_name, column_name, data_type,
                       (is_nullable = 'YES') AS nullable, ordinal_position
                  FROM information_schema.columns
                 WHERE table_schema = DATABASE()
                 ORDER BY table_schema, table_name, ordinal_position
            """)
            cols_raw = (await conn.execute(col_sql)).all()

            pk_sql = text("""
                SELECT table_schema, table_name, column_name
                  FROM information_schema.key_column_usage
                 WHERE constraint_name = 'PRIMARY' AND table_schema = DATABASE()
            """)
            pks_raw = (await conn.execute(pk_sql)).all()
            pk_set = {(r[0], r[1], r[2]) for r in pks_raw}

            # Foreign keys — join to get target column name properly
            fk_sql = text("""
                SELECT kcu.constraint_name,
                       kcu.table_schema AS from_schema, kcu.table_name AS from_table, kcu.column_name AS from_col,
                       kcu.referenced_table_schema AS to_schema, kcu.referenced_table_name AS to_table,
                       kcu.referenced_column_name AS to_col
                  FROM information_schema.key_column_usage kcu
                 WHERE kcu.referenced_table_name IS NOT NULL
                   AND kcu.table_schema = DATABASE()
            """)
            fks_raw = (await conn.execute(fk_sql)).all()

        return _assemble_schema(tables_raw, cols_raw, pk_set, fks_raw)

    async def execute_read(
        self,
        spec: ConnectionSpec,
        sql: str,
        params: Optional[dict[str, Any]] = None,
        *,
        max_rows: int = 500,
        timeout_s: int = 15,
    ) -> QueryResult:
        eng = await engine_pool().get(spec)
        t0 = time.monotonic()
        async with eng.connect() as conn:
            # MySQL uses MAX_EXECUTION_TIME hint per query for SELECT.
            await conn.execute(text("SET SESSION TRANSACTION READ ONLY"))
            hinted = _mysql_add_timeout_hint(sql, timeout_s)
            result = await conn.execute(text(hinted), params or {})
            cols = list(result.keys())
            rows = result.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        if truncated:
            rows = rows[:max_rows]
        return QueryResult(
            columns=cols,
            rows=[tuple(r) for r in rows],
            row_count=len(rows),
            execution_time_ms=int((time.monotonic() - t0) * 1000),
            truncated=truncated,
        )

    async def close_pool(self, spec_key_str: str) -> None:
        await engine_pool().invalidate(spec_key_str)

    @staticmethod
    def _friendly_err(e: Exception) -> str:
        msg = str(e); low = msg.lower()
        if "access denied" in low: return "Authentication failed — check username or password."
        if "unknown database" in low: return "Database does not exist on the server."
        if "connection refused" in low or "can't connect" in low:
            return "Connection refused — verify host, port, and firewall."
        if "ssl" in low: return f"TLS error — {msg}"
        if "timeout" in low or "timed out" in low:
            return "Connection timed out — check host reachability."
        return msg[:400]


def _mysql_add_timeout_hint(sql: str, timeout_s: int) -> str:
    """
    Insert MAX_EXECUTION_TIME hint right after the first SELECT keyword.
    MySQL 5.7.4+ and MariaDB support it. Fails silently on older servers.
    """
    ms = int(timeout_s * 1000)
    stripped = sql.lstrip()
    if stripped[:6].upper() != "SELECT":
        return sql
    return f"SELECT /*+ MAX_EXECUTION_TIME({ms}) */" + stripped[6:]
