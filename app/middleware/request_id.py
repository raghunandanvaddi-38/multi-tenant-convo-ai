"""Attach a per-request id and structured log record fields."""

from __future__ import annotations

import logging
import uuid
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response
