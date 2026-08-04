"""
Tenant identity extraction for FastAPI + WebSocket entry points.

REST: reads x-tenant-id, x-user-id, x-api-key headers.
WebSocket: reads them from query params, or from the first client frame.

Validates the tenant exists and (if the tenant defines an api_key_hash) that
the provided key matches. This is a lightweight guard; real auth (JWT/OAuth)
is a separate concern to be layered above this.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Optional

from fastapi import Header, HTTPException

from app.tenant.context import TenantContext
from app.tenant.registry import TenantConfig, get_registry


log = logging.getLogger("tenant.middleware")

DEFAULT_TENANT_ID = "technodysis"


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _verify_api_key(cfg: TenantConfig, provided: Optional[str]) -> None:
    if not cfg.api_key_hash:
        return
    if not provided or _hash_key(provided) != cfg.api_key_hash:
        raise HTTPException(status_code=401, detail="Invalid tenant API key")


def build_tenant_context(
    tenant_id: Optional[str],
    user_id: Optional[str],
    conversation_id: Optional[str],
    api_key: Optional[str],
) -> TenantContext:
    """Resolve tenant + validate key. Missing tenant_id falls back to default."""
    tid = (tenant_id or DEFAULT_TENANT_ID).strip()
    registry = get_registry()

    if not registry.has(tid):
        raise HTTPException(status_code=404, detail=f"Unknown tenant: {tid}")

    cfg = registry.get(tid)
    _verify_api_key(cfg, api_key)

    return TenantContext(
        tenant_id=cfg.tenant_id,
        user_id=(user_id or "anonymous").strip() or "anonymous",
        conversation_id=(conversation_id or str(uuid.uuid4())[:8]),
        config=cfg,
    )


async def tenant_dependency(
    x_tenant_id: Optional[str] = Header(default=None, alias="x-tenant-id"),
    x_user_id: Optional[str] = Header(default=None, alias="x-user-id"),
    x_conversation_id: Optional[str] = Header(default=None, alias="x-conversation-id"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
) -> TenantContext:
    """FastAPI dependency — inject into any route that needs tenant identity."""
    return build_tenant_context(x_tenant_id, x_user_id, x_conversation_id, x_api_key)
