"""
TenantContext — the request-scoped identity carried through every layer.

A TenantContext is built at the API/WebSocket boundary and passed explicitly
to services. Nothing below the boundary reads global tenant state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.tenant.registry import TenantConfig


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    user_id: str
    conversation_id: str
    config: "TenantConfig"

    @property
    def memory_key(self) -> str:
        return f"{self.tenant_id}:{self.conversation_id}"

    def log_fields(self) -> dict:
        return {
            "tenant": self.tenant_id,
            "user": self.user_id,
            "conversation": self.conversation_id,
        }
