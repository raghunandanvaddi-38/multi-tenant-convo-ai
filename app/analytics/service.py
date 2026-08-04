"""
Analytics recorder + aggregator.

Writes are best-effort: a failed write must never break a conversation. We use
a fire-and-forget task and swallow exceptions with a log line. In production
this would be batched via Redis/queue — swap the implementation here.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models import Event, EventKind
from app.workspaces.context import WorkspaceContext


log = logging.getLogger("analytics")


async def _write(event: Event) -> None:
    try:
        async with async_session_factory() as session:
            session.add(event)
            await session.commit()
    except Exception as e:
        log.warning(f"[analytics] write failed: {e}")


async def _fire(coro):
    """
    Await the write inline. Analytics writes are single INSERTs, cheap enough
    to keep on the request path — the guarantee it lands is worth the ~ms.
    """
    await coro


async def record_message(
    ctx: WorkspaceContext,
    *,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: int = 0,
    query: str = "",
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> None:
    ev = Event(
        workspace_id=ctx.workspace_id,
        kind=EventKind.message,
        provider=provider or ctx.settings.llm.get("provider", ""),
        model=model or ctx.settings.llm.get("model", ""),
        conversation_id=ctx.conversation_id,
        latency_ms=latency_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        query_text=query[:500] if query else None,
        meta={"user_id": ctx.user_id},
    )
    await _fire(_write(ev))


async def record_error(ctx: WorkspaceContext, message: str) -> None:
    ev = Event(
        workspace_id=ctx.workspace_id, kind=EventKind.error,
        conversation_id=ctx.conversation_id, meta={"message": message[:500]},
    )
    await _fire(_write(ev))


async def summary(session: AsyncSession, workspace_id: str, days: int = 7) -> dict[str, Any]:
    """Aggregate the last N days for a workspace."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    total_messages = (await session.execute(
        select(func.count(Event.id)).where(
            Event.workspace_id == workspace_id,
            Event.kind == EventKind.message,
            Event.created_at >= since,
        )
    )).scalar() or 0

    unique_conversations = (await session.execute(
        select(func.count(func.distinct(Event.conversation_id))).where(
            Event.workspace_id == workspace_id,
            Event.kind == EventKind.message,
            Event.created_at >= since,
        )
    )).scalar() or 0

    total_tokens = (await session.execute(
        select(func.sum(Event.tokens_out)).where(
            Event.workspace_id == workspace_id,
            Event.kind == EventKind.message,
            Event.created_at >= since,
        )
    )).scalar() or 0

    avg_latency = (await session.execute(
        select(func.avg(Event.latency_ms)).where(
            Event.workspace_id == workspace_id,
            Event.kind == EventKind.message,
            Event.created_at >= since,
            Event.latency_ms > 0,
        )
    )).scalar() or 0

    errors = (await session.execute(
        select(func.count(Event.id)).where(
            Event.workspace_id == workspace_id,
            Event.kind == EventKind.error,
            Event.created_at >= since,
        )
    )).scalar() or 0

    # Top 10 recent queries (dedup + first-seen sort)
    recent_queries = (await session.execute(
        select(Event.query_text, func.count(Event.id).label("n"))
        .where(
            Event.workspace_id == workspace_id,
            Event.kind == EventKind.message,
            Event.created_at >= since,
            Event.query_text.isnot(None),
        )
        .group_by(Event.query_text)
        .order_by(func.count(Event.id).desc())
        .limit(10)
    )).all()

    return {
        "days": days,
        "total_messages": int(total_messages),
        "unique_conversations": int(unique_conversations),
        "total_tokens_out": int(total_tokens),
        "avg_latency_ms": int(avg_latency),
        "errors": int(errors),
        "top_queries": [{"query": q, "count": int(n)} for q, n in recent_queries if q],
    }
