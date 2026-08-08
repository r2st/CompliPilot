"""Application-layer encryption for the PII columns, and keyed fingerprints.

Section 8.1: "PII fields (PAN, Aadhaar, GST numbers) are encrypted at the
column level using application-layer encryption."

Two primitives, and they are different things:

* :func:`encrypt` / :func:`decrypt` protect the value. AES-256-GCM with a
  random 96-bit nonce per write, so the same PAN encrypts differently every
  time and the ciphertext leaks nothing — not even equality.
* :func:`fingerprint` makes the value *findable*. HMAC-SHA256 under a separate
  key, deterministic, stored in its own column. That is what unique constraints
  and lookups use.

The split is deliberate: deterministic encryption would make lookup easy and
would also turn the GSTIN column into a de-facto plaintext index, since GSTINs
are a small enough space to enumerate. A keyed hash gives lookup without
giving reversibility, and keeping the HMAC key distinct from the encryption key
means compromising one does not hand over the other.

The key is derived from ``JWT_SECRET`` when no dedicated ``ENCRYPTION_KEY`` is
set. That is a convenience for development, and
:func:`app.core.config.validate_startup_config` already refuses to let the
development ``JWT_SECRET`` reach production — so a production deployment that
sets no ``ENCRYPTION_KEY`` still gets a key nobody else knows, and one that
does gets proper separation.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

# Marks a string as produced by this module. Without it, a column migrated
# from a plaintext import is indistinguishable from ciphertext, and
# :func:`decrypt` would return base64 noise for it instead of the value.
_PREFIX = "enc.v1:"

_NONCE_BYTES = 12

# Domain separation, so the encryption key and the fingerprint key derived from
# the same secret are unrelated.
_ENC_INFO = b"complipilot.column-encryption.v1"
_FPR_INFO = b"complipilot.column-fingerprint.v1"


def _derive(info: bytes) -> bytes:
    """A 32-byte key for one purpose."""
    secret = (os.environ.get("ENCRYPTION_KEY") or settings.jwt_secret).encode("utf-8")
    # HKDF-Expand with a fixed empty salt. The input is already a
    # high-entropy secret rather than a password, so a KDF with a work factor
    # would buy nothing and cost a stretch on every request that decrypts.
    return hmac.new(secret, info, hashlib.sha256).digest()


def _enc_key() -> bytes:
    return _derive(_ENC_INFO)


def _fpr_key() -> bytes:
    return _derive(_FPR_INFO)


def encrypt(value: str | None) -> str | None:
    """Encrypt a value for storage. ``None`` and ``""`` pass through unchanged.

    Empty passes through because an empty encrypted string is still a
    ciphertext, and a column holding one would read as "there is a PAN here"
    to anything checking for presence.
    """
    if value is None or value == "":
        return value
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(_enc_key()).encrypt(nonce, value.encode("utf-8"), None)
    return _PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def decrypt(stored: str | None) -> str | None:
    """Recover a value written by :func:`encrypt`.

    A string without the marker is returned as-is: historical rows imported
    before encryption was switched on are readable rather than lost. A string
    *with* the marker that fails to authenticate raises — that is a wrong key
    or a tampered row, and silently returning the ciphertext would put a blob
    of base64 on a filing where a PAN belongs.
    """
    if stored is None or stored == "":
        return stored
    if not stored.startswith(_PREFIX):
        return stored
    raw = base64.b64decode(stored[len(_PREFIX) :])
    nonce, ct = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    return AESGCM(_enc_key()).decrypt(nonce, ct, None).decode("utf-8")


def is_encrypted(stored: str | None) -> bool:
    return bool(stored) and str(stored).startswith(_PREFIX)


def fingerprint(value: str | None) -> str | None:
    """A deterministic, keyed digest for lookup and uniqueness.

    Normalises before hashing — uppercased and stripped of whitespace — because
    ``27aabcu9603r1zm`` and ``27AABCU9603R1ZM`` are the same GSTIN, and a
    uniqueness constraint that does not know it would let both be onboarded.
    """
    if value is None:
        return None
    normalised = "".join(value.split()).upper()
    if not normalised:
        return None
    return hmac.new(_fpr_key(), normalised.encode("utf-8"), hashlib.sha256).hexdigest()
