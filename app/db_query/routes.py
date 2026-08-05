"""
REST routes for the database connector module.

Two auth flavors:
  - Human (dashboard): JWT + org admin role — everything under /v1/db/*
    that mutates uses this.
  - App (SDK/widget): API key with `chat` scope — only /v1/db/query.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.deps import AuthedAPIKey, current_user, require_org_role, require_scope
from app.database import get_session
from app.db_connectors import ConnectionSpec, encrypt_secret, get_connector
from app.db_connectors.pool import engine_pool, spec_key
from app.db_query.discovery import persist_schema
from app.db_query.service import QueryService
from app.db_query.text_to_sql import TextToSQLService
from app.db_query.validator import ValidationError
from app.workspaces.context import WorkspaceContext
from app.models import (
    ConnectionStatus, DBColumn, DBConnection, DBTable, DBType, Role, User, Workspace,
)


log = logging.getLogger("db_query.routes")
router = APIRouter(prefix="/v1/db", tags=["database"])


# ---------- Pydantic schemas -----------------------------------------------

class ConnectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    db_type: str = Field(pattern="^(postgres|mysql)$")
    host: str
    port: int = Field(ge=1, le=65535)
    database_name: str
    username: str
    password: str = Field(min_length=0)      # empty allowed if user tests then edits
    ssl_enabled: bool = False


class ConnectionTestIn(ConnectionIn):
    workspace_id: str


class TablePatch(BaseModel):
    allowed: Optional[bool] = None
    description: Optional[str] = None


class ColumnPatch(BaseModel):
    description: Optional[str] = None


class QueryIn(BaseModel):
    connection_id: str
    sql: str
    max_rows: Optional[int] = None
    timeout_s: Optional[int] = None
    user_context: Optional[str] = None


# ---------- helpers --------------------------------------------------------

def _conn_out(c: DBConnection) -> dict[str, Any]:
    return {
        "id": c.id, "workspace_id": c.workspace_id, "name": c.name,
        "db_type": c.db_type.value if hasattr(c.db_type, "value") else str(c.db_type),
        "host": c.host, "port": c.port, "database_name": c.database_name,
        "username": c.username, "ssl_enabled": c.ssl_enabled,
        "status": c.status.value if hasattr(c.status, "value") else str(c.status),
        "error_message": c.error_message,
        "last_tested_at": c.last_tested_at.isoformat() if c.last_tested_at else None,
        "last_refreshed_at": c.last_refreshed_at.isoformat() if c.last_refreshed_at else None,
        "created_at": c.created_at.isoformat(),
    }


def _tbl_out(t: DBTable) -> dict[str, Any]:
    return {
        "id": t.id, "schema_name": t.schema_name, "table_name": t.table_name,
        "qualified_name": t.qualified_name, "table_type": t.table_type,
        "allowed": t.allowed, "description": t.description,
        "columns": [
            {
                "id": c.id, "column_name": c.column_name, "data_type": c.data_type,
                "is_nullable": c.is_nullable, "is_primary_key": c.is_primary_key,
                "ordinal_position": c.ordinal_position, "description": c.description,
            }
            for c in sorted(t.columns, key=lambda c: c.ordinal_position)
        ],
    }


async def _get_conn_for_user(
    session: AsyncSession, user: User, connection_id: str, minimum: Role = Role.member,
) -> DBConnection:
    c = await session.get(DBConnection, connection_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    ws = await session.get(Workspace, c.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    await require_org_role(ws.organization_id, minimum, user, session)
    return c


# ---------- test connection (no save) --------------------------------------

@router.post("/connections/test")
async def test_connection(
    body: ConnectionTestIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Try to connect with the provided creds; return success or a descriptive error."""
    ws = await session.get(Workspace, body.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    await require_org_role(ws.organization_id, Role.admin, user, session)

    spec = ConnectionSpec(
        db_type=body.db_type, host=body.host, port=body.port,
        database=body.database_name, username=body.username,
        password=body.password, ssl_enabled=body.ssl_enabled,
    )
    connector = get_connector(body.db_type)
    result = await connector.test(spec)
    if not result.ok:
        return {"ok": False, "message": result.message}
    return {"ok": True, "message": result.message, "version": result.version}


# ---------- create / list / get / delete -----------------------------------

@router.post("/connections")
async def create_connection(
    body: ConnectionTestIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    Create a connection. Tests credentials first; only saves if the test passes.
    Then runs schema discovery and persists tables/columns/relationships.
    """
    ws = await session.get(Workspace, body.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    await require_org_role(ws.organization_id, Role.admin, user, session)

    spec = ConnectionSpec(
        db_type=body.db_type, host=body.host, port=body.port,
        database=body.database_name, username=body.username,
        password=body.password, ssl_enabled=body.ssl_enabled,
    )
    connector = get_connector(body.db_type)

    test = await connector.test(spec)
    if not test.ok:
        raise HTTPException(status_code=400, detail=f"Connection test failed: {test.message}")

    row = DBConnection(
        workspace_id=body.workspace_id, name=body.name,
        db_type=DBType(body.db_type), host=body.host, port=body.port,
        database_name=body.database_name, username=body.username,
        encrypted_password=encrypt_secret(body.password), ssl_enabled=body.ssl_enabled,
        status=ConnectionStatus.active, last_tested_at=datetime.now(timezone.utc),
        created_by=user.id,
    )
    session.add(row)
    await session.flush()

    try:
        discovered = await connector.discover(spec)
        summary = await persist_schema(session, row, discovered)
    except Exception as e:
        row.status = ConnectionStatus.error
        row.error_message = f"Schema discovery failed: {e}"[:1000]
        await session.commit()
        raise HTTPException(status_code=500, detail=row.error_message)

    return {**_conn_out(row), "discovery": summary}


@router.get("/connections")
async def list_connections(
    workspace_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    ws = await session.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    await require_org_role(ws.organization_id, Role.member, user, session)
    rows = (
        await session.execute(
            select(DBConnection).where(DBConnection.workspace_id == workspace_id)
            .order_by(DBConnection.created_at.desc())
        )
    ).scalars().all()
    return [_conn_out(r) for r in rows]


@router.get("/connections/{connection_id}")
async def get_connection(
    connection_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    c = await _get_conn_for_user(session, user, connection_id)
    return _conn_out(c)


@router.delete("/connections/{connection_id}")
async def delete_connection(
    connection_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    c = await _get_conn_for_user(session, user, connection_id, minimum=Role.admin)
    # Best-effort pool teardown
    try:
        await engine_pool().invalidate(spec_key(ConnectionSpec(
            db_type=c.db_type.value, host=c.host, port=c.port,
            database=c.database_name, username=c.username, password="",
        )))
    except Exception as e:
        log.warning(f"[db_query] pool invalidate on delete failed: {e}")

    await session.delete(c)
    await session.commit()
    return {"ok": True}


# ---------- refresh schema -------------------------------------------------

@router.post("/connections/{connection_id}/refresh")
async def refresh_schema(
    connection_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    from app.db_connectors import decrypt_secret
    c = await _get_conn_for_user(session, user, connection_id, minimum=Role.admin)
    spec = ConnectionSpec(
        db_type=c.db_type.value, host=c.host, port=c.port,
        database=c.database_name, username=c.username,
        password=decrypt_secret(c.encrypted_password), ssl_enabled=c.ssl_enabled,
    )
    connector = get_connector(spec.db_type)
    try:
        discovered = await connector.discover(spec)
    except Exception as e:
        c.status = ConnectionStatus.error
        c.error_message = f"Schema refresh failed: {e}"[:1000]
        await session.commit()
        raise HTTPException(status_code=500, detail=c.error_message)
    summary = await persist_schema(session, c, discovered)
    c.status = ConnectionStatus.active
    c.error_message = None
    await session.commit()
    return {"ok": True, "summary": summary}


# ---------- tables / columns -----------------------------------------------

@router.get("/connections/{connection_id}/tables")
async def list_tables(
    connection_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    c = await _get_conn_for_user(session, user, connection_id)
    rows = (
        await session.execute(
            select(DBTable).where(DBTable.connection_id == c.id)
            .options(selectinload(DBTable.columns))
            .order_by(DBTable.schema_name, DBTable.table_name)
        )
    ).scalars().all()
    return [_tbl_out(t) for t in rows]


@router.patch("/connections/{connection_id}/tables/{table_id}")
async def patch_table(
    connection_id: str, table_id: str, body: TablePatch,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    c = await _get_conn_for_user(session, user, connection_id, minimum=Role.admin)
    t = await session.get(DBTable, table_id)
    if t is None or t.connection_id != c.id:
        raise HTTPException(status_code=404, detail="Table not found")
    if body.allowed is not None:
        t.allowed = body.allowed
    if body.description is not None:
        t.description = body.description
    await session.commit()
    # Reload columns for the response
    t2 = (
        await session.execute(
            select(DBTable).where(DBTable.id == t.id).options(selectinload(DBTable.columns))
        )
    ).scalar_one()
    return _tbl_out(t2)


@router.patch("/connections/{connection_id}/columns/{column_id}")
async def patch_column(
    connection_id: str, column_id: str, body: ColumnPatch,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    c = await _get_conn_for_user(session, user, connection_id, minimum=Role.admin)
    col = await session.get(DBColumn, column_id)
    if col is None:
        raise HTTPException(status_code=404, detail="Column not found")
    tbl = await session.get(DBTable, col.table_id)
    if tbl is None or tbl.connection_id != c.id:
        raise HTTPException(status_code=404, detail="Column not found")
    if body.description is not None:
        col.description = body.description
    await session.commit()
    return {
        "id": col.id, "column_name": col.column_name, "data_type": col.data_type,
        "is_nullable": col.is_nullable, "is_primary_key": col.is_primary_key,
        "ordinal_position": col.ordinal_position, "description": col.description,
    }


# ---------- Query (API-key auth path) --------------------------------------

@router.post("/query")
async def query(
    body: QueryIn,
    authed: AuthedAPIKey = Depends(require_scope("chat")),
    session: AsyncSession = Depends(get_session),
):
    """Execute a validated SELECT. The connection must belong to the authed workspace."""
    conn = await session.get(DBConnection, body.connection_id)
    if conn is None or conn.workspace_id != authed.workspace.id:
        raise HTTPException(status_code=404, detail="Connection not found for this workspace")
    if conn.status != ConnectionStatus.active:
        raise HTTPException(status_code=409, detail=f"Connection is not active (status={conn.status.value})")

    service = QueryService(session)
    try:
        result = await service.execute(
            workspace_id=authed.workspace.id, connection_id=conn.id,
            sql=body.sql, max_rows=body.max_rows, timeout_s=body.timeout_s,
            user_context=body.user_context,
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:800])

    return {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "execution_time_ms": result.execution_time_ms,
        "truncated": result.truncated,
    }


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: Optional[str] = None


@router.post("/ask")
async def ask_database(
    body: AskIn,
    authed: AuthedAPIKey = Depends(require_scope("chat")),
    session: AsyncSession = Depends(get_session),
):
    """
    Natural-language question → SQL (LLM) → validated + executed → NL answer (LLM).
    Returns the answer, the SQL that ran, and up to 25 preview rows.
    """
    ctx = WorkspaceContext.from_workspace(
        authed.workspace,
        conversation_id=body.conversation_id or "db-ask",
    )
    result = await TextToSQLService(session).answer(ctx, body.question)
    if result is None:
        raise HTTPException(status_code=409, detail="No active database connection with approved tables.")
    return result
