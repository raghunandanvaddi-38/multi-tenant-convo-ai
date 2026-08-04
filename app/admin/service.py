"""
Admin service — CRUD over tenant YAML files.

Writes are atomic (temp file + rename). After every mutation the runtime
TenantRegistry cache for that tenant is invalidated so the next request
picks up the change without a restart.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import tempfile
from dataclasses import asdict
from typing import Any, Optional

import yaml

from app.tenant.registry import TenantRegistry, get_registry


log = logging.getLogger("admin.service")

_TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class AdminError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _validate_tenant_id(tenant_id: str) -> None:
    if not _TENANT_ID_RE.match(tenant_id or ""):
        raise AdminError(
            "tenant_id must be lowercase [a-z0-9_-], start with alnum, ≤64 chars",
            status=400,
        )


def _hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class TenantAdminService:
    def __init__(self, registry: Optional[TenantRegistry] = None):
        self._registry = registry or get_registry()
        self._dir = self._registry._dir  # single source of truth
        os.makedirs(self._dir, exist_ok=True)

    # --- read ------------------------------------------------------------
    def list(self) -> list[dict]:
        out = []
        for tid in self._registry.list_tenants():
            try:
                cfg = self._registry.get(tid)
            except Exception as e:
                log.warning(f"[admin] skipping unreadable tenant {tid}: {e}")
                continue
            out.append(
                {
                    "tenant_id": cfg.tenant_id,
                    "display_name": cfg.display_name,
                    "llm_provider": cfg.llm.provider,
                    "llm_model": cfg.llm.model,
                    "has_api_key": bool(cfg.api_key_hash),
                }
            )
        return out

    def get_raw(self, tenant_id: str) -> dict:
        _validate_tenant_id(tenant_id)
        path = os.path.join(self._dir, f"{tenant_id}.yaml")
        if not os.path.isfile(path):
            raise AdminError(f"Unknown tenant: {tenant_id}", status=404)
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    # --- write -----------------------------------------------------------
    def _atomic_write(self, tenant_id: str, data: dict) -> None:
        path = os.path.join(self._dir, f"{tenant_id}.yaml")
        # Ensure required top-level structure exists so registry parses cleanly.
        data.setdefault("tenant_id", tenant_id)
        for section in ("llm", "rag", "tts", "stt"):
            data.setdefault(section, {})
        fd, tmp = tempfile.mkstemp(prefix=f".{tenant_id}.", suffix=".yaml", dir=self._dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        self._registry.reload(tenant_id)

    def create(self, data: dict) -> dict:
        tid = (data.get("tenant_id") or "").strip()
        _validate_tenant_id(tid)
        path = os.path.join(self._dir, f"{tid}.yaml")
        if os.path.exists(path):
            raise AdminError(f"Tenant already exists: {tid}", status=409)
        self._atomic_write(tid, data)
        log.info(f"[admin] created tenant {tid!r}")
        return self.get_raw(tid)

    def update(self, tenant_id: str, data: dict) -> dict:
        _validate_tenant_id(tenant_id)
        path = os.path.join(self._dir, f"{tenant_id}.yaml")
        if not os.path.isfile(path):
            raise AdminError(f"Unknown tenant: {tenant_id}", status=404)
        # Preserve api_key_hash unless explicitly touched via rotate/clear.
        existing = self.get_raw(tenant_id)
        if "api_key_hash" not in data:
            data["api_key_hash"] = existing.get("api_key_hash")
        data["tenant_id"] = tenant_id
        self._atomic_write(tenant_id, data)
        log.info(f"[admin] updated tenant {tenant_id!r}")
        return self.get_raw(tenant_id)

    def delete(self, tenant_id: str) -> None:
        _validate_tenant_id(tenant_id)
        path = os.path.join(self._dir, f"{tenant_id}.yaml")
        if not os.path.isfile(path):
            raise AdminError(f"Unknown tenant: {tenant_id}", status=404)
        os.remove(path)
        self._registry.reload(tenant_id)
        log.info(f"[admin] deleted tenant {tenant_id!r}")

    # --- api-key rotation ------------------------------------------------
    def rotate_api_key(self, tenant_id: str) -> str:
        """Generate a new key, store the sha256 hash, return the plaintext ONCE."""
        _validate_tenant_id(tenant_id)
        data = self.get_raw(tenant_id)
        plaintext = secrets.token_urlsafe(32)
        data["api_key_hash"] = _hash_key(plaintext)
        self._atomic_write(tenant_id, data)
        log.info(f"[admin] rotated API key for {tenant_id!r}")
        return plaintext

    def clear_api_key(self, tenant_id: str) -> None:
        data = self.get_raw(tenant_id)
        data["api_key_hash"] = None
        self._atomic_write(tenant_id, data)
        log.info(f"[admin] cleared API key for {tenant_id!r}")
