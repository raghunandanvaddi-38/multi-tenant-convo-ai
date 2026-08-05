"""
QueryService — orchestrates: validator → connector → audit log.

Business layers call this instead of touching connectors directly. It's the
single choke-point where every executed SQL is:
  - authorised (workspace_id matches connection)
  - validated (SELECT-only, allowed tables, LIMIT enforced)
  - timed and audited to db_query_logs

Nothing in this module ever prints the credential.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db_connectors import ConnectionSpec, decrypt_secret, get_connector
from app.db_connectors.base import QueryResult
from app.db_query.validator import SQLValidator, ValidatedQuery, ValidationError
from app.models import DBColumn, DBConnection, DBQueryLog, DBTable


log = logging.getLogger("db_query")


class QueryService:
    def __init__(self, session: AsyncSession, *, default_max_rows: int = 500, default_timeout_s: int = 15):
        self.session = session
        self.default_max_rows = default_max_rows
        self.default_timeout_s = default_timeout_s

    async def _load_connection(self, workspace_id: str, connection_id: str) -> DBConnection:
        conn = await self.session.get(DBConnection, connection_id)
        if conn is None or conn.workspace_id != workspace_id:
            raise ValueError("Connection not found for this workspace")
        return conn

    async def _allowed_tables(self, connection_id: str) -> list[DBTable]:
        rows = (
            await self.session.execute(
                select(DBTable).where(DBTable.connection_id == connection_id, DBTable.allowed == True)
                .options(selectinload(DBTable.columns))
            )
        ).scalars().all()
        return list(rows)

    def _spec_from(self, conn: DBConnection) -> ConnectionSpec:
        return ConnectionSpec(
            db_type=conn.db_type.value if hasattr(conn.db_type, "value") else str(conn.db_type),
            host=conn.host,
            port=conn.port,
            database=conn.database_name,
            username=conn.username,
            password=decrypt_secret(conn.encrypted_password),
            ssl_enabled=conn.ssl_enabled,
        )

    async def execute(
        self,
        *,
        workspace_id: str,
        connection_id: str,
        sql: str,
        max_rows: Optional[int] = None,
        timeout_s: Optional[int] = None,
        params: Optional[dict[str, Any]] = None,
        user_context: Optional[str] = None,
    ) -> QueryResult:
        conn = await self._load_connection(workspace_id, connection_id)
        allowed = await self._allowed_tables(connection_id)
        if not allowed:
            raise ValidationError(
                "No tables are allowed for this connection. "
                "Ask a workspace admin to approve tables in the dashboard."
            )

        validator = SQLValidator(
            allowed_tables=[t.table_name for t in allowed],
            max_rows=max_rows or self.default_max_rows,
        )
        try:
            vq: ValidatedQuery = validator.validate(sql)
        except ValidationError as e:
            await self._log(workspace_id, connection_id, sql, 0, 0, error=str(e), user_context=user_context)
            raise

        spec = self._spec_from(conn)
        connector = get_connector(spec.db_type)
        t0 = time.monotonic()
        try:
            result = await connector.execute_read(
                spec, vq.sql, params=params,
                max_rows=vq.max_rows,
                timeout_s=timeout_s or self.default_timeout_s,
            )
        except Exception as e:
            ms = int((time.monotonic() - t0) * 1000)
            await self._log(workspace_id, connection_id, vq.sql, ms, 0, error=str(e)[:1000], user_context=user_context)
            raise

        await self._log(workspace_id, connection_id, vq.sql, result.execution_time_ms,
                        result.row_count, user_context=user_context)
        return result

    async def _log(
        self, workspace_id: str, connection_id: str, sql: str,
        execution_time_ms: int, rows_returned: int,
        error: Optional[str] = None, user_context: Optional[str] = None,
    ) -> None:
        try:
            self.session.add(DBQueryLog(
                workspace_id=workspace_id, connection_id=connection_id,
                sql_text=sql[:10000], execution_time_ms=execution_time_ms,
                rows_returned=rows_returned, error=error,
                user_context=(user_context or "")[:500] or None,
            ))
            await self.session.commit()
        except Exception as e:
            log.warning(f"[db_query] audit write failed: {e}")
