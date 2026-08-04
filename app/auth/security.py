"""
Password hashing, JWT, and API-key primitives.

- Passwords: argon2 via passlib (industry standard for new applications).
- JWTs: HS256 signed with SECRET_KEY. Access tokens short-lived, refresh long-lived.
- API keys: `sk_<prefix>_<random>` — plaintext shown once, sha256 stored.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from passlib.context import CryptContext


_pwd = CryptContext(schemes=["argon2"], deprecated="auto")


def _secret() -> str:
    key = os.getenv("SECRET_KEY")
    if key and key.strip():
        return key.strip()
    # Ephemeral dev fallback — logged once, invalidates on restart.
    return "dev-insecure-secret-CHANGE-ME"


JWT_ALG = "HS256"
ACCESS_TTL = timedelta(minutes=int(os.getenv("ACCESS_TOKEN_MINUTES", "60")))
REFRESH_TTL = timedelta(days=int(os.getenv("REFRESH_TOKEN_DAYS", "14")))


# --- passwords ------------------------------------------------------------

def hash_password(plaintext: str) -> str:
    return _pwd.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    try:
        return _pwd.verify(plaintext, hashed)
    except Exception:
        return False


# --- JWT ------------------------------------------------------------------

def _make_token(sub: str, ttl: timedelta, token_type: str, extra: Optional[dict] = None) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": sub,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "type": token_type,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, _secret(), algorithm=JWT_ALG)


def create_access_token(user_id: str, extra: Optional[dict] = None) -> str:
    return _make_token(user_id, ACCESS_TTL, "access", extra)


def create_refresh_token(user_id: str) -> str:
    return _make_token(user_id, REFRESH_TTL, "refresh")


def decode_token(token: str) -> dict:
    """Raises jwt.InvalidTokenError on any failure (expired, wrong sig, malformed)."""
    return jwt.decode(token, _secret(), algorithms=[JWT_ALG])


# --- API keys -------------------------------------------------------------

# Format: sk_<8-char-prefix>_<32-char-random>
# The prefix is stored plaintext (safe for logs); the whole key is sha256'd.

def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext, prefix, sha256_hash). Plaintext is shown to the user ONCE."""
    prefix = secrets.token_hex(4)  # 8 chars
    body = secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:32]
    plaintext = f"sk_{prefix}_{body}"
    return plaintext, prefix, hash_api_key(plaintext)


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def api_key_prefix(plaintext: str) -> str:
    """Extract the safe-to-display prefix from a plaintext key."""
    parts = plaintext.split("_", 2)
    if len(parts) >= 2:
        return parts[1]
    return plaintext[:8]
