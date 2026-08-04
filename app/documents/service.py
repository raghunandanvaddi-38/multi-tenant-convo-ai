"""
DocumentService — orchestrates upload → extract → chunk → embed → index.

Upload path:
  1. Persist raw bytes via StorageBackend at workspaces/<ws_id>/documents/<uuid>_<name>
  2. Insert Document row with status=queued
  3. Kick off background task (`process()`); returns immediately

Processing (background):
  1. Mark processing
  2. Read bytes → extract text → chunk → append to WorkspaceRAGService index
  3. Mark ready + chunk_count
  4. On failure: mark failed with error text
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import async_session_factory
from app.documents.chunker import chunk_text
from app.documents.extractors import extract
from app.models import Document, DocumentStatus, Workspace
from app.rag.workspace_rag import get_workspace_rag
from app.storage import get_storage
from app.workspaces.context import WorkspaceContext


log = logging.getLogger("documents")

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))  # 50 MB


class DocumentError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg); self.status = status


class DocumentService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _get_workspace(self, workspace_id: str) -> Workspace:
        ws = await self.session.get(Workspace, workspace_id)
        if ws is None:
            raise DocumentError("Workspace not found", status=404)
        return ws

    async def upload(
        self,
        *,
        workspace_id: str,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> Document:
        if len(data) > MAX_UPLOAD_BYTES:
            raise DocumentError(f"File exceeds {MAX_UPLOAD_BYTES} bytes", status=413)
        if len(data) == 0:
            raise DocumentError("Empty file", status=400)

        ws = await self._get_workspace(workspace_id)

        # Sanitize filename to keep storage paths safe
        safe = "".join(c if c.isalnum() or c in "-_.() " else "_" for c in filename).strip()
        storage_key = f"workspaces/{ws.id}/documents/{uuid.uuid4().hex}_{safe}"

        storage = get_storage()
        await storage.write(storage_key, data)

        doc = Document(
            workspace_id=ws.id,
            filename=safe,
            content_type=content_type or "",
            storage_path=storage_key,
            size_bytes=len(data),
            status=DocumentStatus.queued,
        )
        self.session.add(doc)
        await self.session.commit()
        log.info(f"[documents] queued ws={ws.id} doc={doc.id} filename={safe!r} size={len(data)}")
        return doc

    async def list(self, workspace_id: str) -> list[Document]:
        return list(
            (
                await self.session.execute(
                    select(Document).where(Document.workspace_id == workspace_id).order_by(Document.created_at.desc())
                )
            ).scalars()
        )

    async def get(self, workspace_id: str, document_id: str) -> Document:
        doc = await self.session.get(Document, document_id)
        if doc is None or doc.workspace_id != workspace_id:
            raise DocumentError("Document not found", status=404)
        return doc

    async def delete(self, workspace_id: str, document_id: str) -> None:
        doc = await self.get(workspace_id, document_id)
        try:
            await get_storage().delete(doc.storage_path)
        except Exception as e:
            log.warning(f"[documents] storage delete failed: {e}")
        await self.session.delete(doc)
        await self.session.commit()
        # Invalidate cached index so it will reload; note this does NOT remove
        # chunks from the FAISS index (rebuild endpoint coming in a later phase).
        get_workspace_rag().invalidate(workspace_id)


# --- Background processing --------------------------------------------------

async def process_document(document_id: str) -> None:
    """
    Background task. Uses its own DB session — the request session is long
    closed by the time this runs.
    """
    async with async_session_factory() as session:
        doc: Optional[Document] = await session.get(Document, document_id)
        if doc is None:
            log.warning(f"[documents] process: doc {document_id} vanished")
            return
        ws = await session.get(Workspace, doc.workspace_id)
        if ws is None:
            doc.status = DocumentStatus.failed
            doc.error = "Workspace deleted before processing"
            await session.commit()
            return

        doc.status = DocumentStatus.processing
        doc.error = None
        await session.commit()

        try:
            data = await get_storage().read(doc.storage_path)
            text = extract(doc.filename, data)
            if not text.strip():
                raise ValueError("Extractor returned empty text")

            chunk_size = int((ws.settings or {}).get("rag", {}).get("chunk_size", 150))
            chunks = chunk_text(text, chunk_size=chunk_size)
            if not chunks:
                raise ValueError("No chunks produced")

            ctx = WorkspaceContext.from_workspace(ws)
            added = await get_workspace_rag().add_chunks(ctx, chunks)

            doc.chunk_count = added
            doc.status = DocumentStatus.ready
            doc.updated_at = datetime.now(timezone.utc)
            await session.commit()
            log.info(f"[documents] ready ws={ws.id} doc={doc.id} chunks={added}")

        except Exception as e:
            log.exception(f"[documents] processing failed: {e}")
            doc.status = DocumentStatus.failed
            doc.error = str(e)[:1000]
            await session.commit()
