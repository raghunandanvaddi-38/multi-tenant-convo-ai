"""
AIGateway — the only LLM entry point in the application.

Any code that needs an LLM response calls gateway.generate_stream(ctx, prompt).
The gateway looks up the tenant's LLM config, routes to the right provider,
and yields tokens. No caller ever imports a provider directly.
"""

from __future__ import annotations

import logging
import threading
from typing import AsyncIterator, Optional

from app.gateway.router import ProviderRouter
from app.providers.base import LLMGenerationParams
from app.tenant.context import TenantContext


log = logging.getLogger("gateway")


class AIGateway:
    def __init__(self, router: Optional[ProviderRouter] = None):
        self._router = router or ProviderRouter()

    @property
    def router(self) -> ProviderRouter:
        return self._router

    def _params(self, ctx: TenantContext) -> LLMGenerationParams:
        llm = ctx.config.llm
        return LLMGenerationParams(
            model=llm.model,
            temperature=llm.temperature,
            max_tokens=llm.max_tokens,
            context_window=llm.context_window,
            request_timeout=llm.request_timeout,
        )

    async def generate_stream(
        self,
        ctx: TenantContext,
        prompt: str,
    ) -> AsyncIterator[str]:
        provider = self._router.resolve(ctx.config.llm)
        params = self._params(ctx)
        log.debug(
            "[gateway] tenant=%s provider=%s model=%s",
            ctx.tenant_id, provider.name, params.model,
        )
        async for token in provider.generate_stream(prompt, params):
            yield token

    async def warmup(self, ctx: TenantContext) -> None:
        provider = self._router.resolve(ctx.config.llm)
        params = self._params(ctx)
        await provider.warmup(params)


_gateway: Optional[AIGateway] = None
_gateway_lock = threading.Lock()


def get_gateway() -> AIGateway:
    global _gateway
    if _gateway is not None:
        return _gateway
    with _gateway_lock:
        if _gateway is None:
            _gateway = AIGateway()
        return _gateway
