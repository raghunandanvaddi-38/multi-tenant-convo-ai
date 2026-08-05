"""
Two-pass natural-language-over-database.

  1. Prompt the LLM with the workspace's allowed schema + the user question,
     asking for a JSON payload: {"sql": "...", "explanation": "...", "cannot_answer": bool}.
  2. Validate & execute the SQL through QueryService (SELECT-only, allowlist,
     LIMIT injection, timeout).
  3. Feed the (question, rows) back into the LLM to produce a natural answer.

The LLM never sees credentials, connection strings, or hidden tables. Only
the whitelisted table+column names and their customer-authored descriptions.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db_query.service import QueryService
from app.db_query.validator import ValidationError
from app.models import DBConnection, DBRelationship, DBTable, ConnectionStatus
from app.gateway.ai_gateway import get_gateway
from app.workspaces.context import WorkspaceContext


log = logging.getLogger("db_query.t2s")

_MAX_TABLES_IN_PROMPT = 40
_MAX_COLS_PER_TABLE = 25
_ANSWER_ROW_CAP = 25   # rows shown to the LLM when generating the answer


def _describe_schema(tables: list[DBTable], relationships: list[DBRelationship], dialect: str) -> str:
    if not tables:
        return "(no tables have been approved for this workspace)"
    lines: list[str] = []
    lines.append(f"Dialect: {dialect.upper()}")
    lines.append("")
    for t in tables[:_MAX_TABLES_IN_PROMPT]:
        lines.append(f"Table: {t.qualified_name}")
        if t.description:
            lines.append(f"  # {t.description}")
        for col in sorted(t.columns, key=lambda c: c.ordinal_position)[:_MAX_COLS_PER_TABLE]:
            flag = " PK" if col.is_primary_key else ""
            nullable = "" if col.is_nullable else " NOT NULL"
            desc = f"  -- {col.description}" if col.description else ""
            lines.append(f"  {col.column_name} {col.data_type}{flag}{nullable}{desc}")
        lines.append("")
    if relationships:
        lines.append("Foreign keys:")
        for r in relationships[:_MAX_TABLES_IN_PROMPT * 2]:
            lines.append(f"  {r.from_table_id[:8]}.{r.from_column} -> {r.to_table_id[:8]}.{r.to_column}")
    return "\n".join(lines)


def _extract_json(text: str) -> Optional[dict]:
    """
    Best-effort extraction of a JSON object from LLM text. Handles fenced
    ```json``` blocks and stray prose around a single JSON object.
    """
    if not text: return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    payload = fence.group(1) if fence else None
    if not payload:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        payload = m.group(0) if m else None
    if not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def _rows_as_markdown(cols: list[str], rows: list[tuple], cap: int) -> str:
    if not rows: return "(no rows)"
    shown = rows[:cap]
    head = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = "\n".join("| " + " | ".join(_render_cell(v) for v in r) + " |" for r in shown)
    trailer = "" if len(rows) <= cap else f"\n… and {len(rows) - cap} more row(s)."
    return f"{head}\n{sep}\n{body}{trailer}"


def _render_cell(v: Any) -> str:
    if v is None: return ""
    s = str(v)
    if len(s) > 120: s = s[:117] + "..."
    return s.replace("|", "\\|")


class TextToSQLService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _pick_connection(self, workspace_id: str) -> Optional[DBConnection]:
        # Pick the most recently refreshed active connection with ≥1 allowed table.
        rows = (
            await self.session.execute(
                select(DBConnection).where(
                    DBConnection.workspace_id == workspace_id,
                    DBConnection.status == ConnectionStatus.active,
                ).order_by(DBConnection.last_refreshed_at.desc().nullslast())
            )
        ).scalars().all()
        for c in rows:
            allowed = (
                await self.session.execute(
                    select(DBTable).where(DBTable.connection_id == c.id, DBTable.allowed == True).limit(1)
                )
            ).scalar_one_or_none()
            if allowed is not None:
                return c
        return None

    async def _allowed_tables(self, connection_id: str) -> list[DBTable]:
        return list((
            await self.session.execute(
                select(DBTable).where(DBTable.connection_id == connection_id, DBTable.allowed == True)
                .options(selectinload(DBTable.columns))
                .order_by(DBTable.table_name)
            )
        ).scalars().all())

    async def _relationships(self, connection_id: str) -> list[DBRelationship]:
        return list((
            await self.session.execute(
                select(DBRelationship).where(DBRelationship.connection_id == connection_id)
            )
        ).scalars().all())

    async def answer(self, ctx: WorkspaceContext, question: str) -> Optional[dict]:
        """
        Try to answer `question` from a workspace database.
        Returns None if the workspace has no usable connection — caller should
        fall through to the standard document-RAG path.

        Return shape on success:
          { "answer": str, "sql": str, "columns": [...], "rows": [...],
            "row_count": int, "connection_id": str, "execution_time_ms": int }
        """
        conn = await self._pick_connection(ctx.workspace_id)
        if conn is None:
            return None

        tables = await self._allowed_tables(conn.id)
        rels = await self._relationships(conn.id)
        schema_text = _describe_schema(tables, rels, conn.db_type.value)

        plan_prompt = (
            "You are a careful SQL analyst. Given the schema below and the user's question, "
            "reply with a single JSON object (no prose):\n"
            '  {"sql": "<SELECT ...>", "explanation": "<one line>", "cannot_answer": false}\n'
            'If the question cannot be answered from these tables, use {"cannot_answer": true, "explanation":"..."}.\n'
            "Rules:\n"
            "  * SELECT only. No INSERT/UPDATE/DELETE/DDL. No comments (`--` or `/* */`).\n"
            "  * Use only the tables and columns listed. Do not invent names.\n"
            "  * Prefer explicit column lists over SELECT *.\n"
            "  * If aggregating, GROUP BY appropriately.\n"
            "  * Return at most 100 rows.\n\n"
            f"Schema:\n{schema_text}\n\n"
            f"Question: {question}\n\n"
            "JSON:"
        )

        gateway = get_gateway()
        plan_text = ""
        try:
            async for tok in gateway.generate_stream(ctx, plan_prompt):
                plan_text += tok
        except Exception as e:
            log.exception("[t2s] plan generation failed")
            return {"answer": f"I couldn't reach the model to plan the query: {e}", "sql": None, "columns": [], "rows": [], "row_count": 0, "connection_id": conn.id, "execution_time_ms": 0}

        plan = _extract_json(plan_text) or {}
        if plan.get("cannot_answer") or not plan.get("sql"):
            return {
                "answer": plan.get("explanation") or "I don't have enough information in the connected database to answer that.",
                "sql": None, "columns": [], "rows": [], "row_count": 0,
                "connection_id": conn.id, "execution_time_ms": 0,
            }

        service = QueryService(self.session)
        try:
            result = await service.execute(
                workspace_id=ctx.workspace_id, connection_id=conn.id,
                sql=plan["sql"], user_context=question[:500],
            )
        except ValidationError as e:
            return {
                "answer": f"I generated a query but it wasn't safe to run: {e}",
                "sql": plan["sql"], "columns": [], "rows": [], "row_count": 0,
                "connection_id": conn.id, "execution_time_ms": 0,
            }
        except Exception as e:
            return {
                "answer": f"The database returned an error: {e}",
                "sql": plan["sql"], "columns": [], "rows": [], "row_count": 0,
                "connection_id": conn.id, "execution_time_ms": 0,
            }

        answer_prompt = (
            "You are a helpful assistant. The user asked a question; I ran a SQL query on their "
            "database and got these rows. Answer the question in 1–3 short sentences using the data. "
            "If the rows are empty, say so plainly. Do not repeat the SQL.\n\n"
            f"Question: {question}\n\n"
            f"Rows:\n{_rows_as_markdown(result.columns, result.rows, _ANSWER_ROW_CAP)}\n\n"
            "Answer:"
        )
        answer_text = ""
        try:
            async for tok in gateway.generate_stream(ctx, answer_prompt):
                answer_text += tok
        except Exception as e:
            answer_text = f"I ran the query but couldn't format the answer: {e}"

        return {
            "answer": answer_text.strip() or "(no answer generated)",
            "sql": result and plan["sql"],
            "columns": result.columns,
            "rows": result.rows[:_ANSWER_ROW_CAP],
            "row_count": result.row_count,
            "truncated": result.truncated,
            "execution_time_ms": result.execution_time_ms,
            "connection_id": conn.id,
        }
