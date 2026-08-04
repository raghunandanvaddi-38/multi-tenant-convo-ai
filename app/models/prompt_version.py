from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.engine import Base


class PromptVersion(Base):
    """Immutable prompt history. Newest by (workspace_id, version) is authoritative."""
    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "version", name="uq_prompt_ws_version"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    template: Mapped[str] = mapped_column(Text, nullable=False)
    author_user_id: Mapped[str] = mapped_column(String(32), default="")
    note: Mapped[str] = mapped_column(String(500), default="")
