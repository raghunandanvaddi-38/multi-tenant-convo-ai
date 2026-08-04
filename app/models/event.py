from __future__ import annotations

import enum
from typing import Any, Optional

from sqlalchemy import Enum, Float, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.engine import Base


class EventKind(str, enum.Enum):
    message = "message"       # a conversation turn
    error = "error"
    document_ready = "document_ready"
    api_call = "api_call"


class Event(Base):
    """
    Analytics event. Kept intentionally schema-lite — most detail lives in `meta`
    JSON so we can add dimensions without a migration.
    """
    __tablename__ = "events"

    workspace_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[EventKind] = mapped_column(Enum(EventKind), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    conversation_id: Mapped[str] = mapped_column(String(120), default="", index=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    query_text: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
