"""
Defines FastAPI routes that handle all incoming HTTP requests.

- Returns the frontend UI file (index.html) when user opens the app
- Gives a health API to check if Whisper, VAD, and TTS are working
- Provides API to get session details using session_id
- Provides API to get past conversation history of a session
- Allows changing the TTS voice for a session after validation
- Connects these APIs with agent logic and voice/TTS modules

This file is the main connection between frontend requests and backend logic.
"""


import io
import wave
import logging
import numpy as np
from fastapi import APIRouter, UploadFile, File, Depends
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from app.core.tts import get_engine_name, get_default_tts_voice, synthesize_chunks
from app.core.stt import transcribe_np, is_usable_transcript
from app.core.agent import get_session_state, get_conversation_history, update_session_tts
from app.core.voices import is_supported_voice
from app.tenant.context import TenantContext
from app.tenant.middleware import tenant_dependency
from app.tenant.registry import get_registry
from app.config import STATIC_DIR, UI_FILE_NAME, HEALTH_STT_LABEL, HEALTH_VAD_LABEL, APP_VERSION, SAMPLE_RATE


log = logging.getLogger("routes")
router = APIRouter()


class VoiceRequest(BaseModel):
    voice: str = Field(..., min_length=1)


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1)
    voice: str = Field(default="")


@router.get("/")
async def root():
    try:
        return FileResponse(f"{STATIC_DIR}/{UI_FILE_NAME}")
    except Exception as e:
        log.error(f"[/] Failed to serve {UI_FILE_NAME}: {e}")
        return JSONResponse({"error": "Frontend not found."}, status_code=404)


@router.get("/health")
async def health():
    try:
        return {
            "status": "ok",
            "whisper": HEALTH_STT_LABEL,
            "vad": HEALTH_VAD_LABEL,
            "tts": get_engine_name(),
            "voice": get_default_tts_voice(),
            "version": APP_VERSION,
            "tenants": get_registry().list_tenants(),
        }
    except Exception as e:
        log.error(f"[/health] Error: {e}")
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


@router.get("/tenants/me")
async def tenant_me(ctx: TenantContext = Depends(tenant_dependency)):
    """Introspection endpoint — echoes the tenant resolved from request headers."""
    cfg = ctx.config
    return {
        "tenant_id": cfg.tenant_id,
        "display_name": cfg.display_name,
        "llm_provider": cfg.llm.provider,
        "llm_model": cfg.llm.model,
        "features": cfg.features,
    }


@router.get("/session/{session_id}")
async def session_info(session_id: str):
    try:
        return JSONResponse(get_session_state(session_id))
    except Exception as e:
        log.error(f"[/session/{session_id}] Error: {e}")
        return JSONResponse({"error": "Failed to retrieve session state."}, status_code=500)


@router.get("/session/{session_id}/history")
async def session_history(session_id: str):
    try:
        return JSONResponse({
            "session_id": session_id,
            "history": get_conversation_history(session_id),
        })
    except Exception as e:
        log.error(f"[/session/{session_id}/history] Error: {e}")
        return JSONResponse({"error": "Failed to retrieve history."}, status_code=500)


@router.post("/session/{session_id}/voice")
async def session_set_voice(session_id: str, body: VoiceRequest):
    try:
        voice = body.voice.strip()

        if not is_supported_voice(voice):
            return JSONResponse({"error": "Invalid voice"}, status_code=400)

        update_session_tts(session_id, voice)
        return JSONResponse({"session_id": session_id, "voice": voice})

    except Exception as e:
        log.error(f"[/session/{session_id}/voice] Error: {e}")
        return JSONResponse({"error": "Failed to set voice."}, status_code=500)


@router.post("/stt")
async def speech_to_text(audio: UploadFile = File(...)):
    """
    Accept a WAV audio file and return the transcript.

    - Supports mono or stereo WAV (any sample rate — auto-resampled to 16 kHz).
    - Returns {"transcript": str, "usable": bool}.
    """
    try:
        data = await audio.read()
        if not data:
            return JSONResponse({"error": "Empty audio file."}, status_code=400)

        try:
            with wave.open(io.BytesIO(data)) as wf:
                n_channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                framerate = wf.getframerate()
                raw_frames = wf.readframes(wf.getnframes())
        except wave.Error as e:
            return JSONResponse({"error": f"Invalid WAV file: {e}"}, status_code=400)

        # Decode PCM bytes → float32 numpy array
        if sample_width == 2:
            audio_np = np.frombuffer(raw_frames, dtype=np.int16).astype(np.float32) / 32768.0
        elif sample_width == 4:
            audio_np = np.frombuffer(raw_frames, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            return JSONResponse({"error": f"Unsupported sample width: {sample_width} bytes."}, status_code=400)

        # Downmix stereo/multi-channel to mono
        if n_channels > 1:
            audio_np = audio_np.reshape(-1, n_channels).mean(axis=1)

        # Resample to 16 kHz if needed
        if framerate != SAMPLE_RATE:
            try:
                from scipy.signal import resample as scipy_resample
                target_len = int(len(audio_np) * SAMPLE_RATE / framerate)
                audio_np = scipy_resample(audio_np, target_len).astype(np.float32)
            except Exception as e:
                return JSONResponse({"error": f"Resampling failed: {e}"}, status_code=500)

        transcript = transcribe_np(audio_np, SAMPLE_RATE)
        usable = is_usable_transcript(transcript)

        log.info("[/stt] transcript=%r usable=%s", transcript[:80] if transcript else "", usable)
        return JSONResponse({"transcript": transcript, "usable": usable})

    except Exception as e:
        log.error(f"[/stt] Error: {e}")
        return JSONResponse({"error": "STT processing failed."}, status_code=500)


@router.post("/tts")
async def text_to_speech(body: TTSRequest):
    """
    Accept text and return synthesized WAV audio.

    Body: {"text": "Hello world", "voice": "af_heart"}  (voice is optional)
    Supported voices: af_heart, af_bella, am_adam, am_michael
    Returns: audio/wav binary
    """
    try:
        voice = body.voice.strip() or None

        if voice and not is_supported_voice(voice):
            return JSONResponse(
                {"error": f"Invalid voice '{voice}'. Use /health to see available voices."},
                status_code=400,
            )

        wav_bytes = await synthesize_chunks(body.text, voice=voice)

        if not wav_bytes:
            return JSONResponse({"error": "TTS produced no audio."}, status_code=500)

        log.info("[/tts] voice=%s text_len=%d audio_bytes=%d", voice, len(body.text), len(wav_bytes))
        return Response(content=wav_bytes, media_type="audio/wav")

    except Exception as e:
        log.error(f"[/tts] Error: {e}")
        return JSONResponse({"error": "TTS processing failed."}, status_code=500)
