"""
Admin routes — tenant CRUD + API-key rotation.

Auth: every request must include `x-admin-token` matching the ADMIN_TOKEN env
var. If ADMIN_TOKEN is unset the admin surface refuses all requests (safe by
default — you must set the token to enable it). This is intentionally minimal;
real IAM belongs above this layer.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.admin.service import AdminError, TenantAdminService
from app.config import STATIC_DIR


log = logging.getLogger("admin.routes")
router = APIRouter(prefix="/admin", tags=["admin"])


def _expected_token() -> Optional[str]:
    tok = os.getenv("ADMIN_TOKEN")
    return tok.strip() if tok and tok.strip() else None


def require_admin(
    x_admin_token: Optional[str] = Header(default=None, alias="x-admin-token"),
) -> None:
    expected = _expected_token()
    if expected is None:
        raise HTTPException(
            status_code=503,
            detail="Admin disabled — set ADMIN_TOKEN env var to enable.",
        )
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(status_code=401, detail="Invalid admin token")


def _service() -> TenantAdminService:
    return TenantAdminService()


def _err(e: AdminError) -> JSONResponse:
    return JSONResponse({"error": str(e)}, status_code=e.status)


# --- UI page (unauthenticated shell; API calls carry the token) ----------

@router.get("", include_in_schema=False)
async def admin_page():
    path = os.path.join(STATIC_DIR, "admin.html")
    if not os.path.isfile(path):
        return JSONResponse({"error": "admin.html not found"}, status_code=404)
    return FileResponse(path)


# --- API ----------------------------------------------------------------

@router.get("/tenants", dependencies=[Depends(require_admin)])
async def list_tenants():
    return _service().list()


@router.get("/tenants/{tenant_id}", dependencies=[Depends(require_admin)])
async def get_tenant(tenant_id: str):
    try:
        data = _service().get_raw(tenant_id)
        # Never leak stored hash.
        has_key = bool(data.pop("api_key_hash", None))
        data["has_api_key"] = has_key
        return data
    except AdminError as e:
        return _err(e)


@router.post("/tenants", dependencies=[Depends(require_admin)])
async def create_tenant(request: Request):
    body = await request.json()
    try:
        created = _service().create(body)
        created.pop("api_key_hash", None)
        return created
    except AdminError as e:
        return _err(e)


@router.put("/tenants/{tenant_id}", dependencies=[Depends(require_admin)])
async def update_tenant(tenant_id: str, request: Request):
    body = await request.json()
    body.pop("api_key_hash", None)  # rotate via dedicated endpoint
    try:
        updated = _service().update(tenant_id, body)
        updated.pop("api_key_hash", None)
        return updated
    except AdminError as e:
        return _err(e)


@router.delete("/tenants/{tenant_id}", dependencies=[Depends(require_admin)])
async def delete_tenant(tenant_id: str):
    try:
        _service().delete(tenant_id)
        return {"ok": True}
    except AdminError as e:
        return _err(e)


@router.post("/tenants/{tenant_id}/api-key", dependencies=[Depends(require_admin)])
async def rotate_key(tenant_id: str):
    """Returns the plaintext key ONCE. Only the sha256 hash is stored."""
    try:
        plaintext = _service().rotate_api_key(tenant_id)
        return {"tenant_id": tenant_id, "api_key": plaintext}
    except AdminError as e:
        return _err(e)


@router.delete("/tenants/{tenant_id}/api-key", dependencies=[Depends(require_admin)])
async def clear_key(tenant_id: str):
    try:
        _service().clear_api_key(tenant_id)
        return {"ok": True}
    except AdminError as e:
        return _err(e)
