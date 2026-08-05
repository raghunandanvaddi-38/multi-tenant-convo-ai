"""
Symmetric encryption of stored credentials.

Key derivation: sha256(SECRET_KEY) → 32 bytes → urlsafe-base64 → Fernet key.
Rotating SECRET_KEY invalidates every stored password — customers would need
to re-enter them. That's a deliberate trade-off: no separate key management
system to run, and the platform's SECRET_KEY is already required.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    secret = (os.getenv("SECRET_KEY") or "dev-insecure-secret-CHANGE-ME").encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    if plaintext is None:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("Failed to decrypt stored credential; SECRET_KEY may have changed") from e
