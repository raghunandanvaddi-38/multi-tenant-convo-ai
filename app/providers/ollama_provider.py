"""
OllamaProvider — wraps the local Ollama HTTP API.

Replaces the direct httpx call previously in app.core.agent.
The base URL comes from the tenant's LLMSettings; nothing here is hardcoded.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx
import requests

from app.providers.base import LLMGenerationParams


log = logging.getLogger("provider.ollama")


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str, api_timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.api_timeout = api_timeout

    async def generate_stream(
        self,
        prompt: str,
        params: LLMGenerationParams,
    ) -> AsyncIterator[str]:
        url = f"{self.base_url}/api/generate"
        payload = {
            "model": params.model,
            "prompt": prompt,
            "stream": True,
            "keep_alive": -1,
            "options": {
                "temperature": params.temperature,
                "num_predict": params.max_tokens,
                "num_ctx": params.context_window,
            },
        }

        async with httpx.AsyncClient(timeout=params.request_timeout) as client:
            async with client.stream("POST", url, json=payload) as response:
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        log.warning("[ollama] skipping malformed stream line")
                        continue

                    token = data.get("response", "")
                    if token:
                        yield token
                    if data.get("done"):
                        break

    async def warmup(self, params: LLMGenerationParams) -> None:
        try:
            requests.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": params.model,
                    "prompt": "hi",
                    "keep_alive": -1,
                    "options": {"num_predict": 5},
                },
                timeout=params.request_timeout,
            )
        except Exception as e:
            log.warning(f"[ollama] warmup failed: {e}")
