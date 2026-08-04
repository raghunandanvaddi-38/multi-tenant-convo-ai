"""
Provider protocol — the surface every LLM backend must implement.

Adding a new provider means creating a class that implements this Protocol
and registering it in app/gateway/router.py. Business logic depends only on
LLMProvider, never on a concrete implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class LLMGenerationParams:
    model: str
    temperature: float
    max_tokens: int
    context_window: int
    request_timeout: int


@runtime_checkable
class LLMProvider(Protocol):
    """Streaming text generation. Yields tokens as they arrive."""

    name: str

    async def generate_stream(
        self,
        prompt: str,
        params: LLMGenerationParams,
    ) -> AsyncIterator[str]:
        ...

    async def warmup(self, params: LLMGenerationParams) -> None:
        """Best-effort keep-warm; must not raise."""
        ...
