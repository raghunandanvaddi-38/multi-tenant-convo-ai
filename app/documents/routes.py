"""
Document routes — REST surface for the dashboard AND for SDK/API-key clients.

Two auth flavors are supported:
  - API key with `admin` scope (server-to-server + SDK ingestion)
  - JWT + membership check (dashboard uploads) — added in the dashboard phase

For now the routes accept an API key that owns the target workspace.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_scope
from app.database import get_session
from app.documents.extractors import supported_extensions
from app.documents.service import DocumentError, DocumentService, process_document


log = logging.getLogger("documents.routes")
router = APIRouter(prefix="/v1/documents", tags=["documents"])


def _err(e: DocumentError):
    return JSONResponse({"error": str(e)}, status_code=e.status)


def _doc_out(doc):
    return {
        "id": doc.id,
        "filename": doc.filename,
        "content_type": doc.content_type,
        "size_bytes": doc.size_bytes,
        "status": doc.status.value if hasattr(doc.status, "value") else str(doc.status),
        "chunk_count": doc.chunk_count,
        "error": doc.error,
        "created_at": doc.created_at.isoformat(),
        "updated_at": doc.updated_at.isoformat(),
    }


@router.get("/supported-types")
async def supported_types():
    """Public — a client can query supported file types before uploading."""
    return {"extensions": supported_extensions()}


@router.post("")
async def upload_document(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    authed = Depends(require_scope("admin")),
    session: AsyncSession = Depends(get_session),
):
    """Upload a document to the authenticated workspace and start indexing."""
    data = await file.read()
    try:
        doc = await DocumentService(session).upload(
            workspace_id=authed.workspace.id,
            filename=file.filename or "unnamed",
            content_type=file.content_type or "",
            data=data,
        )
    except DocumentError as e:
        return _err(e)

    background.add_task(process_document, doc.id)
    return _doc_out(doc)


@router.get("")
async def list_documents(
    authed = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
):
    docs = await DocumentService(session).list(authed.workspace.id)
    return [_doc_out(d) for d in docs]


@router.get("/{document_id}")
async def get_document(
    document_id: str,
    authed = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
):
    try:
        doc = await DocumentService(session).get(authed.workspace.id, document_id)
    except DocumentError as e:
        return _err(e)
    return _doc_out(doc)


@router.delete("/{document_id}")
async def delete_document(
    document_id: str,
    authed = Depends(require_scope("admin")),
    session: AsyncSession = Depends(get_session),
):
    try:
        await DocumentService(session).delete(authed.workspace.id, document_id)
    except DocumentError as e:
        return _err(e)
    return {"ok": True}


@router.post("/{document_id}/reindex")
async def reindex_document(
    document_id: str,
    background: BackgroundTasks,
    authed = Depends(require_scope("admin")),
    session: AsyncSession = Depends(get_session),
):
    """Force re-processing of a single document (e.g. after a failed run)."""
    try:
        doc = await DocumentService(session).get(authed.workspace.id, document_id)
    except DocumentError as e:
        return _err(e)
    background.add_task(process_document, doc.id)
    return {"ok": True, "document_id": doc.id}
