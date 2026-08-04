"""
Public chat API. Two endpoints, both auth via API key:

  POST /v1/chat         — single-shot JSON: {message} → {reply, conversation_id, latency_ms}
  POST /v1/chat/stream  — Server-Sent Events streaming tokens (text/event-stream)
  WS   /v1/ws/chat      — WebSocket streaming (for browsers that prefer WS)

All resolve WorkspaceContext from the api key and call ConversationService.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AuthedAPIKey, require_scope, workspace_from_api_key
from app.auth.security import hash_api_key
from app.database import get_session, async_session_factory
from app.models import APIKey, Workspace
from app.services.conversation_service import get_conversation_service
from app.workspaces.context import WorkspaceContext


log = logging.getLogger("chat")
router = APIRouter(prefix="/v1", tags=["chat"])


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: Optional[str] = None
    user_id: Optional[str] = None


async def _build_ctx(authed: AuthedAPIKey, conversation_id: Optional[str], user_id: Optional[str]) -> WorkspaceContext:
    conv = conversation_id or uuid.uuid4().hex[:16]
    return WorkspaceContext.from_workspace(
        authed.workspace,
        user_id=user_id or "anonymous",
        conversation_id=conv,
    )


@router.get("/workspace")
async def workspace_public(
    authed: AuthedAPIKey = Depends(workspace_from_api_key),
):
    """Widget bootstrap — returns non-sensitive workspace info (branding, ids)."""
    ws = authed.workspace
    return {
        "workspace_id": ws.id,
        "name": ws.name,
        "branding": (ws.settings or {}).get("branding", {}),
    }


@router.post("/chat")
async def chat(
    body: ChatIn,
    authed: AuthedAPIKey = Depends(require_scope("chat")),
):
    ctx = await _build_ctx(authed, body.conversation_id, body.user_id)
    service = get_conversation_service()

    t0 = time.monotonic()
    tokens: list[str] = []
    async for token in service.stream(ctx, body.message):
        tokens.append(token)
    reply = "".join(tokens).strip()
    latency_ms = int((time.monotonic() - t0) * 1000)

    from app.analytics.service import record_message
    await record_message(ctx, tokens_out=len(tokens), latency_ms=latency_ms, query=body.message)

    return {
        "reply": reply,
        "conversation_id": ctx.conversation_id,
        "workspace_id": ctx.workspace_id,
        "latency_ms": latency_ms,
    }


@router.post("/chat/stream")
async def chat_stream(
    body: ChatIn,
    authed: AuthedAPIKey = Depends(require_scope("chat")),
):
    ctx = await _build_ctx(authed, body.conversation_id, body.user_id)
    service = get_conversation_service()

    async def sse():
        # SSE preamble
        yield f"event: session\ndata: {json.dumps({'conversation_id': ctx.conversation_id})}\n\n"
        t0 = time.monotonic()
        token_count = 0
        try:
            async for token in service.stream(ctx, body.message):
                token_count += 1
                yield f"event: token\ndata: {json.dumps({'text': token})}\n\n"
            latency_ms = int((time.monotonic() - t0) * 1000)
            yield f"event: done\ndata: {json.dumps({'latency_ms': latency_ms})}\n\n"

            from app.analytics.service import record_message
            await record_message(ctx, tokens_out=token_count, latency_ms=latency_ms, query=body.message)
        except Exception as e:
            log.exception("chat stream error")
            yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream", headers={"cache-control": "no-cache"})


# --- WebSocket -------------------------------------------------------------

async def _resolve_ws_api_key(token: Optional[str]) -> Optional[AuthedAPIKey]:
    if not token or not token.startswith("sk_"):
        return None
    key_hash = hash_api_key(token)
    async with async_session_factory() as s:
        api_key = (
            await s.execute(
                select(APIKey).where(APIKey.key_hash == key_hash)
            )
        ).scalar_one_or_none()
        if api_key is None or not api_key.is_active or api_key.revoked_at is not None:
            return None
        ws = await s.get(Workspace, api_key.workspace_id)
        if ws is None:
            return None
        return AuthedAPIKey(api_key=api_key, workspace=ws)


@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    await websocket.accept()
    token = websocket.query_params.get("api_key")
    authed = await _resolve_ws_api_key(token)
    if authed is None:
        await websocket.close(code=4401, reason="Invalid API key")
        return
    if not authed.api_key.has_scope("chat"):
        await websocket.close(code=4403, reason="Missing scope: chat")
        return

    conv_id = websocket.query_params.get("conversation_id") or uuid.uuid4().hex[:16]
    user_id = websocket.query_params.get("user_id") or "anonymous"
    ctx = WorkspaceContext.from_workspace(authed.workspace, user_id=user_id, conversation_id=conv_id)
    service = get_conversation_service()

    await websocket.send_json({
        "type": "session_start",
        "conversation_id": ctx.conversation_id,
        "workspace_id": ctx.workspace_id,
        "branding": ctx.settings.branding,
    })

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "text": "malformed json"})
                continue

            if msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            if msg.get("type") != "user_message":
                continue

            user_text = (msg.get("text") or "").strip()
            if not user_text:
                continue

            t0 = time.monotonic()
            token_count = 0
            try:
                async for token in service.stream(ctx, user_text):
                    token_count += 1
                    await websocket.send_json({"type": "token", "text": token})
                latency_ms = int((time.monotonic() - t0) * 1000)
                await websocket.send_json({"type": "done", "latency_ms": latency_ms})

                from app.analytics.service import record_message
                await record_message(ctx, tokens_out=token_count, latency_ms=latency_ms, query=user_text)
            except Exception as e:
                log.exception("ws chat error")
                await websocket.send_json({"type": "error", "text": str(e)})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception(f"ws_chat unexpected: {e}")
