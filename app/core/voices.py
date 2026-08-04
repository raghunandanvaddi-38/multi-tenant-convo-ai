"""
Defines supported TTS voices and validation.

- Stores list of allowed voice names
- Checks if given voice is valid and supported

This file ensures only approved voices are used in the system.
"""


EDGE_VOICES = {
    "en-US-JennyNeural",
    "en-US-GuyNeural",
    "en-IN-NeerjaNeural",
}


KOKORO_VOICES = {
    "af_heart",
    "af_bella",
    "am_adam",
    "am_michael",
}


def is_supported_edge_voice(voice: str) -> bool:
    return isinstance(voice, str) and voice.strip() in EDGE_VOICES


def is_supported_kokoro_voice(voice: str) -> bool:
    return isinstance(voice, str) and voice.strip() in KOKORO_VOICES


def detect_tts_provider_from_voice(voice: str) -> str | None:
    v = (voice or "").strip()
    if v in KOKORO_VOICES:
        return "kokoro"
    if v in EDGE_VOICES:
        return "edge"
    return None


def is_supported_voice(voice: str) -> bool:
    return detect_tts_provider_from_voice(voice) is not None

