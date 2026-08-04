"""
Handles speech-to-text (STT) conversion using a Qwen ASR model.

- Loads Qwen ASR once and reuses it
- Converts audio input into text
- Cleans audio before processing:
  • applies noise reduction
  • skips noise-only audio
- Enforces English output when configured
- Validates final transcript before using it

This file keeps the same public STT interface used by the rest of the app.
"""

import torch
import logging
import numpy as np
from app.core.audio_preprocessing import denoise_chunk, is_noise_only
from app.config import (SAMPLE_RATE, STT_MODEL_NAME, STT_MAX_NEW_TOKENS, STT_MAX_BATCH_SIZE, STT_DEVICE_MAP,
                        STT_DTYPE, STT_USE_FLASH_ATTN, STT_ALIGNER_MODEL, STT_LANGUAGE, STT_REJECT_NON_ENGLISH)

log = logging.getLogger("stt")


# Singleton model holder
_model = None

# Unicode script ranges that indicate non-Latin scripts.
_NON_LATIN_SCRIPTS: tuple[tuple[int, int], ...] = (
    (0x0900, 0x097F),  # Devanagari
    (0x0A00, 0x0A7F),  # Gurmukhi
    (0x0B00, 0x0B7F),  # Oriya
    (0x0C00, 0x0C7F),  # Telugu
    (0x0D00, 0x0D7F),  # Malayalam
    (0x0E00, 0x0E7F),  # Thai
    (0x0F00, 0x0FFF),  # Tibetan
    (0x1000, 0x109F),  # Myanmar/Burmese
    (0x3000, 0x9FFF),  # CJK, Hiragana, Katakana, etc.
    (0xAC00, 0xD7AF),  # Hangul syllables
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3040, 0x30FF),  # Hiragana + Katakana
    (0x0600, 0x06FF),  # Arabic
    (0x0590, 0x05FF),  # Hebrew
    (0x10A0, 0x10FF),  # Georgian
    (0x0400, 0x04FF),  # Cyrillic
    (0x0370, 0x03FF),  # Greek
)

_NOISE_TOKENS: frozenset[str] = frozenset({
    ".", "..", "...", "!", "?", ",", "-", "--",
    "you", "the", "a", "i", "oh", "ah", "uh", "um", "hmm", "hm", "mm",
    "ok", "okay", "yeah", "yes", "no", "hi", "hey", "bye", "bye.",
    "thanks for watching", "thanks for watching.",
    "please subscribe", "don't forget to subscribe",
    "like and subscribe", "hit the bell",
    "see you in the next video", "see you next time",
    "[music]", "[ music ]", "[applause]", "[ applause ]",
    "(music)", "(applause)", "[silence]", "[ silence ]",
    "[laughter]", "[ laughter ]", "(laughter)",
    "[background noise]", "[noise]", "[inaudible]",
    "subtitles by", "captions by", "transcribed by",
    "♪", "♫",
})


def _contains_non_latin(text: str) -> bool:
    for ch in text:
        cp = ord(ch)
        for lo, hi in _NON_LATIN_SCRIPTS:
            if lo <= cp <= hi:
                return True
    return False



def _enforce_english(text: str, source: str = "") -> str:

    if not text:
        return ""

    if _contains_non_latin(text):
        log.warning(
            "[STT] Rejected non-English transcript%s: %r",
            f" ({source})" if source else "",
            text[:80],
        )
        return ""

    alpha_chars = [c for c in text if c.isalpha()]
    if alpha_chars:
        ascii_alpha = sum(1 for c in alpha_chars if ord(c) < 128)
        ratio = ascii_alpha / len(alpha_chars)
        if ratio < 0.5:
            log.warning(
                "[STT] Low ASCII-alpha ratio (%.0f%%) — likely non-English, rejecting: %r",
                ratio * 100,
                text[:80],
            )
            return ""

    return text



def _resolve_device_and_dtype():

    device_map = (STT_DEVICE_MAP or "auto").strip().lower()
    dtype_name = (STT_DTYPE or "auto").strip().lower()
    flash_attn = (STT_USE_FLASH_ATTN or "auto")
    flash_attn = str(flash_attn).strip().lower()

    # device
    if device_map != "auto":
        resolved_device = device_map
    else:
        if torch.cuda.is_available():
            resolved_device = "cuda:0"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            resolved_device = "mps"
        else:
            resolved_device = "cpu"

    # dtype
    if dtype_name != "auto":
        resolved_dtype_name = dtype_name
    else:
        if resolved_device.startswith("cuda"):
            try:
                major, _ = torch.cuda.get_device_capability(0)
                # bf16 is safer on newer GPUs, fp16 on older supported GPUs
                resolved_dtype_name = "bfloat16" if major >= 8 else "float16"
            except Exception:
                resolved_dtype_name = "float16"
        elif resolved_device == "mps":
            resolved_dtype_name = "float16"
        else:
            resolved_dtype_name = "float32"

    # flash attention
    if flash_attn in {"1", "true", "yes"}:
        use_flash_attn = True
    elif flash_attn in {"0", "false", "no"}:
        use_flash_attn = False
    else:
        # auto
        use_flash_attn = False
        if resolved_device.startswith("cuda"):
            try:
                major, minor = torch.cuda.get_device_capability(0)
                use_flash_attn = major >= 8
            except Exception:
                use_flash_attn = False

    log.info(
        "[STT] Resolved device/dtype/flash_attn: device=%s dtype=%s flash_attn=%s",
        resolved_device,
        resolved_dtype_name,
        use_flash_attn,
    )
    return resolved_device, resolved_dtype_name, use_flash_attn



def get_model():

    global _model

    if _model is not None:
        return _model

    log.info("[STT] Loading Qwen ASR model: %s ...", STT_MODEL_NAME)

    try:
        from qwen_asr import Qwen3ASRModel
    except ImportError as exc:
        raise RuntimeError(
            "Qwen ASR requires the 'qwen-asr' package. "
            "Install with: pip install -U qwen-asr"
        ) from exc

    resolved_device, resolved_dtype_name, use_flash_attn = _resolve_device_and_dtype()

    dtype_map = {
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
        "float32": torch.float32,
        "auto": "auto",
    }
    torch_dtype = dtype_map.get(resolved_dtype_name, torch.float32)

    init_kwargs: dict = {
        "dtype": torch_dtype,
        "device_map": resolved_device,
        "max_inference_batch_size": STT_MAX_BATCH_SIZE,
        "max_new_tokens": STT_MAX_NEW_TOKENS,
    }

    if use_flash_attn:
        init_kwargs["attn_implementation"] = "flash_attention_2"

    if STT_ALIGNER_MODEL:
        aligner_kwargs: dict = {
            "dtype": torch_dtype,
            "device_map": resolved_device,
        }
        if use_flash_attn:
            aligner_kwargs["attn_implementation"] = "flash_attention_2"

        init_kwargs["forced_aligner"] = STT_ALIGNER_MODEL
        init_kwargs["forced_aligner_kwargs"] = aligner_kwargs
        log.info("[STT] Forced aligner enabled: %s", STT_ALIGNER_MODEL)

    log.info(
        "[STT] Resolved runtime: device=%s dtype=%s flash_attn=%s",
        resolved_device,
        resolved_dtype_name,
        use_flash_attn,
    )

    try:
        _model = Qwen3ASRModel.from_pretrained(STT_MODEL_NAME, **init_kwargs)
        log.info(
            "[STT] Qwen ASR ready. Forced language=%r STT_REJECT_NON_ENGLISH=%s",
            STT_LANGUAGE,
            STT_REJECT_NON_ENGLISH,
        )
    except Exception as exc:
        log.error("[STT] Failed to load Qwen ASR model: %s", exc)
        raise RuntimeError(f"STT model load failed: {exc}") from exc

    return _model



def transcribe_np(audio_np: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
    
    if audio_np is None or not isinstance(audio_np, np.ndarray):
        log.warning("[STT] Invalid input type: %s", type(audio_np))
        return ""

    if audio_np.size == 0:
        return ""

    if audio_np.ndim == 2:
        audio_np = audio_np.mean(axis=1)

    if audio_np.ndim != 1:
        log.warning("[STT] Unexpected audio shape %s", getattr(audio_np, "shape", None))
        return ""

    if audio_np.dtype != np.float32:
        try:
            audio_np = audio_np.astype(np.float32)
        except Exception as exc:
            log.error("[STT] Failed to cast audio to float32: %s", exc)
            return ""

    if not np.isfinite(audio_np).all():
        log.warning("[STT] Audio has NaN/Inf — skipping transcription.")
        return ""

    peak = float(np.abs(audio_np).max()) if audio_np.size else 0.0
    if peak > 1.0:
        audio_np = audio_np / peak

    try:
        audio_np = denoise_chunk(audio_np, sample_rate)
        if is_noise_only(audio_np, sample_rate):
            return ""
    except Exception as exc:
        log.error("[STT] Preprocessing error: %s", exc)
        return ""

    try:
        model = get_model()
    except Exception as exc:
        log.error("[STT] Model unavailable: %s", exc)
        return ""

    try:
        results = model.transcribe(
            audio=(audio_np, sample_rate),
            language=STT_LANGUAGE,
        )

        if not results:
            return ""

        transcript = (results[0].text or "").strip()
        detected_lang = getattr(results[0], "language", "?")

        if transcript:
            duration_s = len(audio_np) / max(sample_rate, 1)
            log.debug("[STT] (%.1fs | model_lang=%s) → %r", duration_s, detected_lang, transcript)

        if STT_REJECT_NON_ENGLISH:
            transcript = _enforce_english(transcript, source=f"model_lang={detected_lang}")

        return transcript

    except MemoryError:
        log.error("[STT] OOM during transcription — audio too long?")
        return ""
    except Exception as exc:
        log.error("[STT] Transcription error: %s", exc)
        return ""



def is_usable_transcript(text: str) -> bool:
    try:
        if not isinstance(text, str):
            return False

        stripped = text.strip()
        if len(stripped) < 2:
            return False

        cleaned = stripped.lower().rstrip(".!?,;:")
        if cleaned in _NOISE_TOKENS:
            log.debug("[STT] Discarded hallucination: %r", text)
            return False

        if sum(c.isalpha() for c in stripped) < 3:
            return False

        if _contains_non_latin(stripped):
            return False

        return True
    except Exception as exc:
        log.error("[STT] Unexpected error in is_usable_transcript: %s", exc)
        return False

