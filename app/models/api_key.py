from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.engine import Base


class APIKeyScope(str, enum.Enum):
    chat = "chat"          # send messages, stream
    read = "read"          # inspect settings, history
    admin = "admin"        # mutate workspace settings, upload docs


class APIKey(Base):
    """
    Workspace-scoped API key. We store only the sha256 hash; the plaintext is
    shown once at creation time. `prefix` is the first 8 chars of the plaintext
    for identification in listings/logs (safe to display).
    """
    __tablename__ = "api_keys"

    workspace_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), default="")
    prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    scopes: Mapped[str] = mapped_column(String(255), default="chat,read")  # comma-separated
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    workspace = relationship("Workspace", back_populates="api_keys")

    def scope_list(self) -> list[str]:
        return [s.strip() for s in (self.scopes or "").split(",") if s.strip()]

    def has_scope(self, needed: str) -> bool:
        scopes = self.scope_list()
        return "admin" in scopes or needed in scopes
