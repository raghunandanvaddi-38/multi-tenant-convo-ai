"""PostgreSQL connector — SQLAlchemy async + asyncpg."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from sqlalchemy import text

from app.db_connectors.base import (
    ConnectionSpec, ConnectionTestResult,
    DatabaseConnector, DiscoveredColumn, DiscoveredRelationship, DiscoveredSchema, DiscoveredTable,
    QueryResult,
)
from app.db_connectors.pool import engine_pool, spec_key


class PostgresConnector:
    name = "postgres"

    async def test(self, spec: ConnectionSpec) -> ConnectionTestResult:
        try:
            eng = await engine_pool().get(spec)
            async with eng.connect() as conn:
                result = await conn.execute(text("SELECT current_database(), version()"))
                dbname, version = result.first()
            return ConnectionTestResult(ok=True, message="Connected", version=str(version))
        except Exception as e:
            # Ensure the next test/rebuild after a fixed password
            await engine_pool().invalidate(spec_key(spec))
            return ConnectionTestResult(ok=False, message=self._friendly_err(e))

    async def discover(self, spec: ConnectionSpec) -> DiscoveredSchema:
        eng = await engine_pool().get(spec)
        async with eng.connect() as conn:
            # Tables + views (exclude system schemas)
            tbl_sql = text("""
                SELECT table_schema, table_name, table_type
                  FROM information_schema.tables
                 WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                   AND table_schema NOT LIKE 'pg_toast%'
                 ORDER BY table_schema, table_name
            """)
            tables_raw = (await conn.execute(tbl_sql)).all()

            # Columns
            col_sql = text("""
                SELECT c.table_schema, c.table_name, c.column_name, c.data_type,
                       (c.is_nullable = 'YES') AS nullable, c.ordinal_position
                  FROM information_schema.columns c
                 WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema')
                 ORDER BY c.table_schema, c.table_name, c.ordinal_position
            """)
            cols_raw = (await conn.execute(col_sql)).all()

            # Primary keys
            pk_sql = text("""
                SELECT tc.table_schema, tc.table_name, kcu.column_name
                  FROM information_schema.table_constraints tc
                  JOIN information_schema.key_column_usage kcu
                    ON kcu.constraint_name = tc.constraint_name
                   AND kcu.table_schema  = tc.table_schema
                 WHERE tc.constraint_type = 'PRIMARY KEY'
            """)
            pks_raw = (await conn.execute(pk_sql)).all()
            pk_set = {(r[0], r[1], r[2]) for r in pks_raw}

            # Foreign keys
            fk_sql = text("""
                SELECT tc.constraint_name,
                       kcu.table_schema  AS from_schema, kcu.table_name  AS from_table, kcu.column_name AS from_col,
                       ccu.table_schema  AS to_schema,   ccu.table_name  AS to_table,   ccu.column_name AS to_col
                  FROM information_schema.table_constraints tc
                  JOIN information_schema.key_column_usage kcu
                    ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
                  JOIN information_schema.constraint_column_usage ccu
                    ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
                 WHERE tc.constraint_type = 'FOREIGN KEY'
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
            # Set a per-transaction read-only + statement timeout on Postgres.
            await conn.execute(text(f"SET LOCAL statement_timeout = {int(timeout_s * 1000)}"))
            await conn.execute(text("SET LOCAL transaction_read_only = on"))
            result = await conn.execute(text(sql), params or {})
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

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _friendly_err(e: Exception) -> str:
        msg = str(e)
        low = msg.lower()
        if "authentication" in low or "password" in low:
            return "Authentication failed — check username or password."
        if "does not exist" in low and "database" in low:
            return "Database does not exist on the server."
        if "connection refused" in low:
            return "Connection refused — verify host, port, and firewall."
        if "ssl" in low:
            return f"TLS error — {msg}"
        if "timeout" in low or "timed out" in low:
            return "Connection timed out — check host reachability."
        return msg[:400]


def _assemble_schema(tables_raw, cols_raw, pk_set, fks_raw) -> DiscoveredSchema:
    """Common assembler shared with MySQL (given equivalent projections)."""
    schema = DiscoveredSchema()
    table_index: dict[tuple[str, str], DiscoveredTable] = {}
    for schema_name, table_name, table_type in tables_raw:
        ttype = "view" if str(table_type).lower().startswith("view") else "table"
        t = DiscoveredTable(schema_name=schema_name or "", table_name=table_name, table_type=ttype)
        table_index[(schema_name, table_name)] = t
        schema.tables.append(t)

    for schema_name, table_name, col_name, data_type, nullable, ordinal in cols_raw:
        key = (schema_name, table_name)
        if key not in table_index:
            continue
        table_index[key].columns.append(DiscoveredColumn(
            name=col_name, data_type=str(data_type), is_nullable=bool(nullable),
            is_primary_key=(schema_name, table_name, col_name) in pk_set,
            ordinal_position=int(ordinal or 0),
        ))

    for constraint_name, from_s, from_t, from_c, to_s, to_t, to_c in fks_raw:
        schema.relationships.append(DiscoveredRelationship(
            constraint_name=str(constraint_name),
            from_schema=from_s or "", from_table=from_t, from_column=from_c,
            to_schema=to_s or "",     to_table=to_t,     to_column=to_c,
        ))
    return schema
