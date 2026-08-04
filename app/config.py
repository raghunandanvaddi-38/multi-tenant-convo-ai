"""
Central configuration module for the application.

- Loads all environment-based settings (TTS, STT, LLM, audio pipeline)
- Defines default values for speech synthesis and recognition
- Configures AI model settings and conversation limits
- Sets audio processing thresholds and timing controls
- Defines file system paths for data, vectors, and logs
- Provides logging configuration constants

Acts as the single source of truth for system-wide configuration,
making the application flexible, environment-driven, and easy to deploy.
"""


import os


# Main
APP_TITLE   = "Technodysis Voice Agent"
APP_VERSION = "2.4.0"


# Routes
HEALTH_STT_LABEL = os.getenv("HEALTH_STT_LABEL", "Qwen/Qwen3-ASR-1.7B")
HEALTH_VAD_LABEL = "silero"


# Paths
_BASE              = os.path.dirname(os.path.dirname(__file__))
DATA_DIR           = os.path.join(_BASE, "data")
INPUT_DIR          = os.path.join(DATA_DIR, "input")
VECTOR_DB_DIR      = os.path.join(DATA_DIR, "vector_database")
KNOWLEDGE_BASE_DIR = os.path.join(INPUT_DIR, "knowledge_base.txt")
FAISS_INDEX_DIR    = os.path.join(VECTOR_DB_DIR, "company_index.faiss")
CHUNKS_NPY_DIR     = os.path.join(VECTOR_DB_DIR, "chunks.npy")
STATIC_DIR         = os.path.join(_BASE, "static")


# User Interface
UI_FILE_NAME = "index.html"


# STT
STT_MODEL_NAME         = os.environ.get("STT_MODEL_NAME", "Qwen/Qwen3-ASR-1.7B")
STT_MAX_NEW_TOKENS     = int(os.environ.get("STT_MAX_NEW_TOKENS", "256"))
STT_MAX_BATCH_SIZE     = int(os.environ.get("STT_MAX_BATCH_SIZE", "8"))
STT_DEVICE_MAP         = os.environ.get("STT_DEVICE_MAP", "auto")
STT_DTYPE              = os.environ.get("STT_DTYPE", "auto")
STT_USE_FLASH_ATTN     = os.environ.get("STT_FLASH_ATTN", "auto")
STT_ALIGNER_MODEL      = os.environ.get("STT_ALIGNER_MODEL") or None
STT_LANGUAGE           = os.environ.get("STT_LANGUAGE", "English")
STT_REJECT_NON_ENGLISH = os.environ.get("STT_REJECT_NON_ENGLISH", "1") == "1"


# LLM Model
BC_COOLDOWN                = float(os.getenv("BC_COOLDOWN", "5.0"))
MAX_CACHE_SIZE             = int(os.getenv("MAX_CACHE_SIZE", "100"))
SENTENCE_TRANSFORMER_MODEL = os.getenv("SENTENCE_TRANSFORMER_MODEL", "all-MiniLM-L6-v2")
LLM_MODEL                  = os.getenv("LLM_MODEL", "llama3.2:3b")
LLM_API_TIMEOUT            = int(os.getenv("LLM_API_TIMEOUT", "10"))
LLM_WARMUP_TIMEOUT         = int(os.getenv("LLM_WARMUP_TIMEOUT", "15"))
LLM_WARMUP_NUM_PREDICT     = int(os.getenv("LLM_WARMUP_NUM_PREDICT", "5"))
LLM_STREAM_REQUEST_TIMEOUT = int(os.getenv("LLM_STREAM_REQUEST_TIMEOUT", "30"))
LLM_MAX_GENERATION_TOKENS  = int(os.getenv("LLM_MAX_GENERATION_TOKENS", "150"))
LLM_CONTEXT_WINDOW_TOKENS  = int(os.getenv("LLM_CONTEXT_WINDOW_TOKENS", "2048"))
LLM_TEMPERATURE            = float(os.getenv("LLM_TEMPERATURE", "0.1"))
LLM_CHAT_HISTORY_LIMIT     = int(os.getenv("LLM_CHAT_HISTORY_LIMIT", "5"))
CHUNK_SIZE                 = int(os.getenv("CHUNK_SIZE", "150"))
RETRIEVAL_TOP_K            = int(os.getenv("RETRIEVAL_TOP_K", "2"))
FUZZY_MATCH_THRESHOLD      = int(os.getenv("FUZZY_MATCH_THRESHOLD", "80"))
MAX_HISTORY_TURNS          = int(os.getenv("MAX_HISTORY_TURNS", "20"))
ENTITY_CORRECTIONS         = {"technodysis": ["technodysi", "technodsis", "technoysis", "technodsy", "technodys"]}


# Audio pipeline
SAMPLE_RATE          = 16_000
SPEECH_THRESHOLD     = 0.60
SILENCE_LIMIT        = 1.00
MIN_SPEECH_SECS      = 0.70
BARGE_IN_HOLD        = 0.35
MAX_AUDIO_SECS       = 8
PARTIAL_INTERVAL     = 1.20
TTS_FLUSH_CHARS      = 140
BC_MIN_SPEECH_BEFORE = 1.50
BC_PAUSE_THRESHOLD   = 0.30


# Audio preprocessing
NOISE_CANCEL_ENABLED    = True
NOISE_CANCEL_STRENGTH   = 0.80
NOISE_RMS_FLOOR         = 0.003
NOISE_SAMPLE_SECS       = 0.20
NOISE_FRAME_SIZE        = 512
NOISE_HOP_SIZE          = 256
NOISE_QUIET_FRAME_RATIO = 0.10
NOISE_ALPHA_BASE        = 1.2
NOISE_ALPHA_SCALE       = 1.0
NOISE_NORM_EPS          = 1e-8


# WebSocket
WS_PING_INTERVAL           = 25.0
WS_PING_TIMEOUT            = 10.0
WS_PING_MAX_MISSED         = 2
VAD_FRAME_SIZE             = 512
MIN_SPEAKER_CHECK_SECS     = 0.8
MIN_INTERRUPT_CHECK_SECS   = 0.8
BC_MIN_SPEECH_FALLBACK     = 1.2
BC_PAUSE_FALLBACK          = 0.35
PRIMARY_ENROLLMENT_COUNT   = 3
PRIMARY_UPDATE_WINDOW      = 5
PRIMARY_UPDATE_MIN_SCORE   = 0.55
SESSION_UPDATE_DELAY       = 1.2
SPEECH_SAMPLE_RATE         = 16000
SPEAKER_ENROLLMENT_SAMPLES = 3
SPEECH_SILENCE_THRESHOLD   = float(os.getenv("SPEECH_SILENCE_THRESHOLD", "0.01"))
TARGET_SPEAKER_VERIFICATION = True
if TARGET_SPEAKER_VERIFICATION:
    SPEAKER_THRESHOLD          = float(os.getenv("SPEAKER_THRESHOLD", "0.45"))
else:
    SPEAKER_THRESHOLD          = float(os.getenv("SPEAKER_THRESHOLD", "0.01"))


# TTS
TTS_VOICE  = os.getenv("TTS_VOICE", "en-IN-NeerjaNeural")
TTS_RATE   = os.getenv("TTS_RATE", "+0%")
TTS_VOLUME = os.getenv("TTS_VOLUME", "+0%")
TTS_PITCH  = os.getenv("TTS_PITCH", "+0Hz")
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "kokoro").strip().lower()   # kokoro | edge | auto
TTS_FALLBACK_PROVIDER = os.getenv("TTS_FALLBACK_PROVIDER", "edge").strip().lower()
TTS_DEFAULT_VOICE = os.getenv("TTS_DEFAULT_VOICE", "af_heart")
TTS_DEVICE = os.getenv("TTS_DEVICE", "auto").strip().lower()
TTS_SAMPLE_RATE = int(os.getenv("TTS_SAMPLE_RATE", "24000"))


# Logging
LOGS_DIR         = os.path.join(_BASE, "logs")
LOG_FILE         = os.path.join(LOGS_DIR, "app.log")
LOG_MAX_BYTES    = 5 * 1024 * 1024                    # 5 MB per file
LOG_BACKUP_COUNT = 5                                  # keep last 5 files

