"""
Voice routes for workspace-scoped clients.

  POST /v1/stt              — WAV file in → {transcript, usable} out
  POST /v1/tts              — {text, voice?} in → audio/wav out
  WS   /v1/ws/voice         — streaming voice conversation (low-latency)

The WS endpoint aims for sub-second perceived latency by doing three things:

  1. Rolling / streaming STT
     After PARTIAL_INTERVAL_S of speech we run STT on the buffer so far and
     emit a `partial_transcript` event. Repeats every PARTIAL_INTERVAL_S while
     the user keeps speaking. On silence, one final STT pass produces the
     committed `transcript` event, then we call the LLM.

  2. Short-phrase TTS flush
     Instead of waiting for `.!?;:` we also flush on `,` or on any word
     boundary once the pending text is ≥ FIRST_CHUNK_CHARS (default 22).
     The first phrase of the answer typically ships in ~200–400 ms after
     the first LLM token arrives.

  3. Raw int16 PCM binary frames
     Server sends binary chunks that are raw little-endian int16 samples
     at TTS_SAMPLE_RATE (24 kHz for Kokoro), mono. No WAV header per chunk,
     no `decodeAudioData()` on the client — playback is a direct
     `AudioBufferSourceNode.start(t)`.

The STT (Qwen ASR), TTS (Kokoro/Edge), and VAD (Silero) models are the SAME
singletons loaded at startup by main.py.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import uuid
import wave
from typing import Optional

import numpy as np
from fastapi import APIRouter, Depends, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.api.chat_routes import _resolve_ws_api_key
from app.api.ws_audio import run_vad
from app.auth.deps import AuthedAPIKey, require_scope
from app.config import (
    MAX_AUDIO_SECS, MIN_SPEECH_SECS, SAMPLE_RATE,
    SILENCE_LIMIT, SPEECH_THRESHOLD, TTS_SAMPLE_RATE,
)
from app.core.stt import is_usable_transcript, transcribe_np
from app.core.tts import synthesize_chunks
from app.services.conversation_service import get_conversation_service
from app.workspaces.context import WorkspaceContext


log = logging.getLogger("voice")
router = APIRouter(prefix="/v1", tags=["voice"])


# ---- tunables (all env-overridable) --------------------------------------

import os
PARTIAL_INTERVAL_S = float(os.getenv("VOICE_PARTIAL_INTERVAL_S", "0.8"))
PARTIAL_MIN_SECS   = float(os.getenv("VOICE_PARTIAL_MIN_SECS",   "0.7"))
END_SILENCE_S      = float(os.getenv("VOICE_END_SILENCE_S", str(SILENCE_LIMIT)))
FIRST_CHUNK_CHARS  = int(os.getenv("VOICE_FIRST_CHUNK_CHARS", "22"))
LATER_CHUNK_CHARS  = int(os.getenv("VOICE_LATER_CHUNK_CHARS", "60"))
_FLUSH_HARD = ".!?;:"
_FLUSH_SOFT = ",–—"


# --------------------------------------------------------------------------
# REST — one-shot STT + TTS
# --------------------------------------------------------------------------

def _decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(data)) as wf:
        n_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        framerate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    if sample_width == 2:
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_width == 4:
        audio = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported WAV sample width: {sample_width} bytes")

    if n_channels > 1:
        audio = audio.reshape(-1, n_channels).mean(axis=1)

    if framerate != SAMPLE_RATE:
        try:
            from scipy.signal import resample as scipy_resample
            target_len = int(len(audio) * SAMPLE_RATE / framerate)
            audio = scipy_resample(audio, target_len).astype(np.float32)
        except Exception as e:
            raise ValueError(f"Resample to {SAMPLE_RATE}Hz failed: {e}")

    return audio, SAMPLE_RATE


@router.post("/stt")
async def stt(
    audio: UploadFile = File(...),
    authed: AuthedAPIKey = Depends(require_scope("chat")),
):
    data = await audio.read()
    if not data:
        return JSONResponse({"error": "Empty audio file"}, status_code=400)
    try:
        wav, sr = _decode_wav(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        text = await asyncio.to_thread(transcribe_np, wav, sr)
    except Exception as e:
        log.exception("STT failed")
        return JSONResponse({"error": f"STT failed: {e}"}, status_code=500)
    return {
        "transcript": text or "",
        "usable": is_usable_transcript(text or ""),
        "workspace_id": authed.workspace.id,
    }


class TTSIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    voice: Optional[str] = None
    provider: Optional[str] = None


@router.post("/tts")
async def tts(body: TTSIn, authed: AuthedAPIKey = Depends(require_scope("chat"))):
    ws_tts = (authed.workspace.settings or {}).get("tts") or {}
    voice = body.voice or ws_tts.get("default_voice")
    provider = body.provider or ws_tts.get("provider")
    try:
        wav = await synthesize_chunks(body.text, voice=voice, provider=provider)
    except Exception as e:
        log.exception("TTS failed")
        return JSONResponse({"error": f"TTS failed: {e}"}, status_code=500)
    if not wav:
        return JSONResponse({"error": "TTS produced no audio"}, status_code=500)
    return Response(
        content=wav, media_type="audio/wav",
        headers={"x-workspace-id": authed.workspace.id, "x-tts-voice": voice or ""},
    )


# --------------------------------------------------------------------------
# WebSocket — streaming voice
# --------------------------------------------------------------------------

def _wav_to_pcm16(wav_bytes: bytes) -> tuple[bytes, int]:
    """Extract raw int16 PCM from a WAV blob. Returns (pcm_bytes, sample_rate)."""
    if not wav_bytes:
        return b"", 0
    with wave.open(io.BytesIO(wav_bytes)) as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    if width != 2:
        # Downcast to int16 in-Python — cheap for TTS output sizes
        import struct
        if width == 4:
            samples = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
        elif width == 1:
            samples = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128) / 128.0
        else:
            return b"", 0
        i16 = np.clip(samples, -1, 1)
        frames = (i16 * 32767).astype(np.int16).tobytes()
    if channels > 1:
        arr = np.frombuffer(frames, dtype=np.int16).reshape(-1, channels).mean(axis=1)
        frames = arr.astype(np.int16).tobytes()
    return frames, rate


def _find_flush_boundary(buf: str, *, min_chars: int) -> int:
    """Pick a split point inside `buf`. Returns -1 for don't-flush-yet."""
    if len(buf) < min_chars:
        # Even below the min, honour hard punctuation to end sentences quickly.
        for i, ch in enumerate(buf):
            if ch in _FLUSH_HARD and i >= 8:
                return i + 1
        return -1
    # Prefer the latest hard punct, else the latest soft, else the latest space.
    for chars in (_FLUSH_HARD, _FLUSH_SOFT):
        idx = -1
        for c in chars:
            j = buf.rfind(c)
            if j > idx:
                idx = j
        if idx >= min_chars // 2:
            return idx + 1
    idx = buf.rfind(" ")
    return idx + 1 if idx >= min_chars // 2 else -1


@router.websocket("/ws/voice")
async def ws_voice(websocket: WebSocket):
    await websocket.accept()

    # Auth
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

    # Session preamble — client uses `audio` block to size its playback buffer.
    await websocket.send_json({
        "type": "session_start",
        "conversation_id": ctx.conversation_id,
        "workspace_id": ctx.workspace_id,
        "branding": ctx.settings.branding,
        "audio": {
            "input":  {"sample_rate": SAMPLE_RATE,     "channels": 1, "encoding": "pcm_s16le"},
            "output": {"sample_rate": TTS_SAMPLE_RATE, "channels": 1, "encoding": "pcm_s16le"},
        },
        "streaming": {
            "partial_interval_s": PARTIAL_INTERVAL_S,
            "end_silence_s":      END_SILENCE_S,
        },
    })

    # ---- per-connection state ----
    speech_buf: list[np.ndarray] = []
    pre_buf: list[np.ndarray] = []
    speech_active = False
    silence_time = 0.0
    speech_total = 0.0
    last_partial_at = 0.0
    partial_task: Optional[asyncio.Task] = None
    turn_lock = asyncio.Lock()
    log_prefix = f"[voice/{ctx.workspace_id[:8]}/{ctx.conversation_id[:8]}]"

    async def send_json_safe(data: dict):
        try: await websocket.send_json(data)
        except Exception: pass

    async def send_status(text: str): await send_json_safe({"type": "status", "text": text})
    async def send_error(msg: str):   await send_json_safe({"type": "error",  "text": msg})

    async def run_partial(snapshot: np.ndarray):
        """Best-effort partial STT. Silent on failure — a partial is expendable."""
        try:
            text = await asyncio.to_thread(transcribe_np, snapshot)
            if text and is_usable_transcript(text):
                await send_json_safe({"type": "partial_transcript", "text": text})
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # partials are best-effort

    async def run_turn_from_audio():
        nonlocal speech_buf, partial_task
        # Cancel any in-flight partial so it doesn't overwrite the final.
        if partial_task and not partial_task.done():
            partial_task.cancel()
            try: await partial_task
            except (asyncio.CancelledError, Exception): pass
        partial_task = None

        if not speech_buf: return
        try:
            audio = np.concatenate(speech_buf)
        except ValueError:
            speech_buf = []; return
        speech_buf = []

        dur = len(audio) / SAMPLE_RATE
        if dur < MIN_SPEECH_SECS:
            await send_status("idle"); return
        if dur > MAX_AUDIO_SECS:
            audio = audio[-int(SAMPLE_RATE * MAX_AUDIO_SECS):]

        await send_status("thinking")
        try:
            text = await asyncio.to_thread(transcribe_np, audio)
        except Exception as e:
            log.exception(f"{log_prefix} STT error")
            await send_error(f"STT failed: {e}"); await send_status("idle"); return

        if not text or not is_usable_transcript(text):
            log.info(f"{log_prefix} STT unusable: {text!r}")
            await send_status("idle"); return

        await send_json_safe({"type": "transcript", "text": text})
        await run_turn_from_text(text)

    async def stream_tts(chunk: str):
        """Synthesize a text chunk and stream raw int16 PCM as one or more binary frames."""
        if not chunk.strip(): return
        try:
            wav = await synthesize_chunks(
                chunk,
                voice=(ctx.settings.tts.get("default_voice") if ctx.settings.tts else None),
                provider=(ctx.settings.tts.get("provider") if ctx.settings.tts else None),
            )
        except Exception as e:
            log.warning(f"{log_prefix} TTS synth error: {e}"); return
        if not wav: return
        pcm, rate = _wav_to_pcm16(wav)
        if not pcm: return
        # If the provider ever returns a rate that disagrees with our declared
        # session rate, resample so the client's fixed-rate buffer stays aligned.
        if rate != TTS_SAMPLE_RATE:
            try:
                from scipy.signal import resample as scipy_resample
                arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                arr = scipy_resample(arr, int(len(arr) * TTS_SAMPLE_RATE / rate)).astype(np.float32)
                pcm = (np.clip(arr, -1, 1) * 32767).astype(np.int16).tobytes()
            except Exception as e:
                log.warning(f"{log_prefix} PCM resample failed: {e}")
                return
        try: await websocket.send_bytes(pcm)
        except Exception: pass

    async def run_turn_from_text(user_text: str):
        await send_status("speaking")
        t0 = time.monotonic()
        buf = ""
        token_count = 0
        first_chunk_sent = False

        try:
            async for tok in service.stream(ctx, user_text):
                token_count += 1
                buf += tok
                await send_json_safe({"type": "token", "text": tok})

                # Very aggressive flush on the FIRST chunk (get audio out fast),
                # then a slightly larger chunk for the rest to keep synth calls cheap.
                min_chars = FIRST_CHUNK_CHARS if not first_chunk_sent else LATER_CHUNK_CHARS
                idx = _find_flush_boundary(buf, min_chars=min_chars)
                if idx > 0:
                    chunk, buf = buf[:idx], buf[idx:]
                    await stream_tts(chunk)
                    first_chunk_sent = True
        except Exception as e:
            log.exception(f"{log_prefix} chat error")
            await send_error(str(e)); await send_status("idle"); return

        if buf.strip():
            await stream_tts(buf)

        latency_ms = int((time.monotonic() - t0) * 1000)
        await send_json_safe({"type": "done", "latency_ms": latency_ms})

        from app.analytics.service import record_message
        try: await record_message(ctx, tokens_out=token_count, latency_ms=latency_ms, query=user_text)
        except Exception as e: log.debug(f"{log_prefix} analytics write skipped: {e}")

        await send_status("idle")

    # ---- receive loop ----
    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break

            raw_text = msg.get("text")
            if raw_text is not None:
                try:
                    j = json.loads(raw_text)
                except json.JSONDecodeError:
                    continue
                mt = j.get("type")
                if mt == "ping":
                    await websocket.send_json({"type": "pong"}); continue
                if mt == "reset":
                    if partial_task and not partial_task.done(): partial_task.cancel()
                    speech_buf = []; pre_buf = []; speech_active = False
                    silence_time = 0.0; speech_total = 0.0; last_partial_at = 0.0
                    await websocket.send_json({"type": "reset_ack"}); continue
                if mt == "flush":
                    if turn_lock.locked(): continue
                    asyncio.create_task(_locked(turn_lock, run_turn_from_audio)); continue
                if mt == "user_text":
                    user_text = (j.get("text") or "").strip()
                    if not user_text or turn_lock.locked(): continue
                    asyncio.create_task(_locked(turn_lock, lambda: run_turn_from_text(user_text)))
                    continue
                continue

            raw_bytes = msg.get("bytes")
            if not raw_bytes:
                continue

            try:
                pcm = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            except Exception as e:
                log.warning(f"{log_prefix} audio decode: {e}"); continue

            try:
                prob = run_vad(pcm)
            except Exception as e:
                log.warning(f"{log_prefix} VAD error: {e}"); prob = 0.0

            chunk_secs = len(pcm) / SAMPLE_RATE

            if prob > SPEECH_THRESHOLD:
                if not speech_active:
                    speech_active = True
                    speech_buf = list(pre_buf) + [pcm]; pre_buf = []
                    silence_time = 0.0; speech_total = 0.0; last_partial_at = 0.0
                    await send_status("listening")
                else:
                    speech_buf.append(pcm); silence_time = 0.0
                speech_total += chunk_secs

                # Kick a rolling partial-STT pass at fixed cadence, without blocking
                # the receive loop. Only one partial in flight at a time.
                now = time.monotonic()
                if (
                    speech_total >= PARTIAL_MIN_SECS
                    and (now - last_partial_at) >= PARTIAL_INTERVAL_S
                    and (partial_task is None or partial_task.done())
                ):
                    last_partial_at = now
                    try:
                        snapshot = np.concatenate(speech_buf)
                    except ValueError:
                        snapshot = None
                    if snapshot is not None:
                        partial_task = asyncio.create_task(run_partial(snapshot))
            else:
                if speech_active:
                    speech_buf.append(pcm)
                    silence_time += chunk_secs
                    if silence_time >= END_SILENCE_S and speech_total >= MIN_SPEECH_SECS:
                        speech_active = False
                        if not turn_lock.locked():
                            asyncio.create_task(_locked(turn_lock, run_turn_from_audio))
                else:
                    pre_buf.append(pcm)
                    if len(pre_buf) > 8: pre_buf.pop(0)

    except WebSocketDisconnect:
        log.info(f"{log_prefix} disconnected")
    except Exception as e:
        log.exception(f"{log_prefix} loop error: {e}")
    finally:
        if partial_task and not partial_task.done():
            partial_task.cancel()


async def _locked(lock: asyncio.Lock, coro_fn):
    async with lock:
        try:
            await coro_fn()
        except Exception:
            logging.getLogger("voice").exception("turn error")
