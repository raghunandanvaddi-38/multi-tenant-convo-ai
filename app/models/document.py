from __future__ import annotations

import enum
from typing import Optional

from sqlalchemy import Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.engine import Base


class DocumentStatus(str, enum.Enum):
    queued = "queued"
    processing = "processing"
    ready = "ready"
    failed = "failed"


class Document(Base):
    """
    A source document uploaded to a workspace. The extracted chunks live inside
    the workspace's FAISS index; the DB row is metadata + status.
    """
    __tablename__ = "documents"

    workspace_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), default="")
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)  # relative to StorageBackend
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus), default=DocumentStatus.queued, nullable=False, index=True
    )
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    workspace = relationship("Workspace", back_populates="documents")
