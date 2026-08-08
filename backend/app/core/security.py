"""Authentication primitives: password hashing, JWTs, and TOTP."""
from __future__ import annotations

import secrets
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import pyotp
from jose import JWTError, jwt

from app.core.config import settings
from app.core.crypto import decrypt, encrypt

# bcrypt operates on at most 72 bytes; longer inputs must be truncated first.
_BCRYPT_MAX_BYTES = 72

# A hash of a password nobody knows, compared against when no account matched.
# Built on first use rather than at import: it costs a full bcrypt round, and
# paying that in every worker boot and every test collection to serve a branch
# most processes never take is the wrong trade. Guarded by a lock so two
# concurrent failed logins do not both pay for it.
_absent_hash: str | None = None
_absent_hash_lock = threading.Lock()


def _prepare(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """Return a bcrypt hash for a plaintext password."""
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt()).decode("utf-8")


def _absent_account_hash() -> str:
    """A real bcrypt hash to compare against when there is no account.

    Minted from a random secret, so it is not a hash of anything guessable and
    a comparison against it cannot succeed. Built by :func:`hash_password`, so
    it carries the same cost factor as a stored hash and takes the same time to
    check.
    """
    global _absent_hash

    if _absent_hash is None:
        with _absent_hash_lock:
            if _absent_hash is None:
                _absent_hash = hash_password(secrets.token_urlsafe(32))
    return _absent_hash


def verify_password(plain: str, hashed: str | None) -> bool:
    """Check a plaintext password against a stored bcrypt hash.

    ``hashed`` is ``None`` when the lookup found no account. That case still
    runs a full bcrypt comparison — against :func:`_absent_account_hash` — and
    only then returns ``False``.

    Skipping the work there is the obvious implementation and it is a user
    enumeration oracle. bcrypt is deliberately slow: a login against an account
    that exists costs a few hundred milliseconds, one against an address that
    does not would cost a database round trip. That gap is measurable across
    the internet and hands an attacker the "does this firm have an account"
    answer that the identical 401 body is written to withhold. Taking ``None``
    here, rather than leaving each caller to remember a separate dummy-verify,
    is what keeps the two paths the same length.
    """
    if hashed is None:
        hashed = _absent_account_hash()
    try:
        return bcrypt.checkpw(_prepare(plain), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --- JWT -----------------------------------------------------------------


def create_access_token(
    subject: str | int,
    *,
    org_id: int,
    role: str,
    client_org_id: int | None = None,
    expires_minutes: int | None = None,
) -> str:
    """Mint an access token.

    ``client_org_id`` is section 6.1's cross-organization claim: present when a
    CA firm user has switched into a client context, absent otherwise. It is a
    claim rather than a query parameter so that the tenant a request acts on is
    fixed at authentication time and signed — a request cannot widen its own
    scope by editing a URL.

    ``org_id`` and ``role`` ride along as claims *and* are re-checked against
    the database on every request by :func:`app.core.deps.get_tenant_context`.
    The claims make the common path cheap; the re-check is what makes a
    revoked role take effect before the token expires.
    """
    expire = datetime.now(UTC) + timedelta(
        minutes=expires_minutes or settings.access_token_expire_minutes
    )
    payload: dict[str, Any] = {
        "sub": str(subject),
        "exp": expire,
        "type": "access",
        "org": org_id,
        "role": role,
    }
    if client_org_id is not None:
        payload["client_org_id"] = client_org_id
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(subject: str | int, *, org_id: int) -> str:
    """Mint a refresh token.

    Deliberately carries no ``role`` and no ``client_org_id``. A refresh token
    outlives several role changes and several context switches, so baking
    either into it would let a user refresh their way back into access they no
    longer have. The refresh endpoint reads them from the database instead.
    """
    expire = datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "exp": expire,
        "type": "refresh",
        "org": org_id,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_totp_challenge_token(subject: str | int, *, org_id: int) -> str:
    """A short-lived token proving the password step passed, nothing more.

    Issued when a login needs a second factor. It is a distinct ``type`` so it
    cannot be presented to an ordinary endpoint: a token that got a user past
    the password but not past TOTP must not authenticate anything.
    """
    expire = datetime.now(UTC) + timedelta(minutes=5)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "exp": expire,
        "type": "totp_challenge",
        "org": org_id,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, *, expected_type: str = "access") -> dict[str, Any] | None:
    """Return the claims of a valid token of *expected_type*, else ``None``.

    The type check is not optional and not the caller's job. jose validates the
    signature and expiry; nothing in the JWT standard stops a refresh token
    from being presented as an access token, and only this check does.
    """
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
    if payload.get("type") != expected_type:
        return None
    return payload


# --- TOTP (section 8.2) --------------------------------------------------


def generate_totp_secret() -> str:
    """A fresh base32 secret, ready to be stored encrypted."""
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, *, email: str, issuer: str | None = None) -> str:
    """The ``otpauth://`` URI an authenticator app scans."""
    return pyotp.TOTP(secret).provisioning_uri(
        name=email, issuer_name=issuer or settings.app_name
    )


def verify_totp(secret: str | None, code: str | None) -> bool:
    """Check a six-digit code against a secret.

    ``valid_window=1`` accepts the adjacent 30-second step in each direction.
    Phone clocks drift, and a user who is refused a code they are reading
    correctly will turn the second factor off — which costs more security than
    the one extra step admits.
    """
    if not secret or not code:
        return False
    try:
        return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)
    except Exception:  # noqa: BLE001 - malformed secret or code is simply a failure
        return False


def store_totp_secret(secret: str) -> str:
    """Encrypt a secret for the ``users.totp_secret`` column."""
    encrypted = encrypt(secret)
    # encrypt() only returns None for a None/empty input, which a freshly
    # generated secret never is; the assert documents that for the type checker
    # rather than guarding a reachable branch.
    assert encrypted is not None
    return encrypted


def load_totp_secret(stored: str | None) -> str | None:
    """Decrypt a stored secret."""
    return decrypt(stored)
