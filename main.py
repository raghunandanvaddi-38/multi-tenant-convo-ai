"""
Main file that starts the Voice Agent application.

- Creates FastAPI server and sets up logging
- Loads API routes and WebSocket connection
- Serves frontend files

- At startup:
  • checks if TTS is working
  • loads STT, Agent, and Speaker models

- Makes sure everything is ready before users connect

This file starts and prepares the whole system to run.
"""


import os
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.api.routes import router as rest_router
from app.api.websocket import router as ws_router
from app.admin.routes import router as admin_router
from app.auth.routes import router as auth_router
from app.documents.routes import router as documents_router
from app.workspaces.routes import router as workspaces_router
from app.api.chat_routes import router as chat_router
from app.api.voice_routes import router as voice_router
from app.analytics.routes import router as analytics_router
from app.middleware.rate_limit import APIKeyRateLimitMiddleware
from app.middleware.request_id import RequestIDMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request as _StarletteRequest
from app.core.tts import synthesize_chunks, get_engine_name, get_tts_provider, get_default_tts_voice
from app.core.stt import get_model
from app.core.agent import ensure_system_initialized
from app.core.speaker import get_classifier
from app.database import init_db
from app.config import STATIC_DIR, LOGS_DIR, LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT, APP_VERSION, APP_TITLE
from warnings import filterwarnings


filterwarnings("ignore")

# Logging setup
os.makedirs(LOGS_DIR, exist_ok=True)

# Suppress noisy third-party loggers
logging.getLogger("speechbrain").setLevel(logging.WARNING)
logging.getLogger("speechbrain.utils.fetching").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_fmt = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_console = logging.StreamHandler()
_console.setFormatter(_fmt)

_file = RotatingFileHandler(
    LOG_FILE,
    maxBytes=LOG_MAX_BYTES,
    backupCount=LOG_BACKUP_COUNT,
    encoding="utf-8",
)
_file.setFormatter(_fmt)

logging.getLogger().setLevel(logging.INFO)
logging.getLogger().addHandler(_console)
logging.getLogger().addHandler(_file)

log = logging.getLogger("voice-agent")



# Startup probe to verify TTS is working before accepting requests
async def _probe_tts():

    log.info(f"[TTS] startup probe — provider={get_tts_provider()!r} voice={get_default_tts_voice()!r}")

    try:
        data = await synthesize_chunks("Hello.")
        if data:
            log.info(f"[TTS] OK — engine={get_engine_name()} bytes={len(data)}")
        else:
            log.error("[TTS] 0 bytes returned! Check TTS provider setup.")
    
    except Exception as e:
        log.error(f"[TTS] Startup probe error: {e}")
        raise



async def _preload_core_models():

    log.info("[startup] Preloading STT, Agent, and Speaker models...")

    stt_task = asyncio.to_thread(get_model)
    agent_task = ensure_system_initialized()
    speaker_task = asyncio.to_thread(get_classifier)

    stt_result, agent_result, speaker_result = await asyncio.gather(
        stt_task,
        agent_task,
        speaker_task,
        return_exceptions=True,
    )

    if isinstance(stt_result, Exception):
        raise RuntimeError(f"STT preload failed: {stt_result}")
    log.info("[startup] STT ready")

    if isinstance(agent_result, Exception):
        raise RuntimeError(f"Agent preload failed: {agent_result}")
    log.info("[startup] Agent/RAG/Ollama ready")

    if isinstance(speaker_result, Exception):
        raise RuntimeError(f"Speaker model preload failed: {speaker_result}")
    log.info("[startup] Speaker model ready")



# Lifespan manager for startup/shutdown events
@asynccontextmanager
async def lifespan(app: FastAPI):

    log.info("[startup] Voice agent starting up...")

    try:
        await init_db()
        await _probe_tts()
        await _preload_core_models()
        log.info("[startup] All core systems ready")
    except Exception as e:
        log.exception(f"[startup] Fatal startup error: {e}")
        raise

    yield

    log.info("[shutdown] Voice agent shutting down...")



# FastAPI app initialization with CORS and routes
app = FastAPI(title=APP_TITLE, version=APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files for frontend
try:
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
except RuntimeError as e:
    log.error(f"[startup] Could not mount /static: {e}")

# Include API and WebSocket routes
app.include_router(rest_router)
app.include_router(ws_router)
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(documents_router)
app.include_router(workspaces_router)
app.include_router(chat_router)
app.include_router(voice_router)
app.include_router(analytics_router)

# Middleware (order matters — request-id first so it wraps everything else)
app.add_middleware(APIKeyRateLimitMiddleware)
app.add_middleware(RequestIDMiddleware)


# Log the reason for 422s. Without this, uvicorn only prints the status code
# and you can't tell whether a signup rejection was the email, the password
# length, or a missing content-type.
@app.exception_handler(RequestValidationError)
async def _log_validation_error(request: _StarletteRequest, exc: RequestValidationError):
    reasons = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", []) if p not in ("body",))
        reasons.append(f"{loc}: {err.get('msg')}")
    log.warning(f"[422] {request.method} {request.url.path} — {'; '.join(reasons)}")
    return JSONResponse({"detail": exc.errors()}, status_code=422)

