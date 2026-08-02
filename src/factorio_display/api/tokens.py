"""HMAC-signed access tokens for the factorio-display API (stdlib only).

Format (JWT-style, compact)::

    <b64url(header)>.<b64url(payload)>.<b64url(signature)>

- header  = ``{"alg": "HS256", "typ": "JWT"}``
- payload = ``{"sub": <subject>, "iat": <issued-ts>, "exp": <expiry-ts>, "scope": <scope>}``
- signature = HMAC-SHA256(key, "<header>.<payload>")

Everything is produced/consumed by this module, so there is no dependency on
PyJWT.  The same key is used by the CLI (``factorio-display token ...``) to
issue/verify tokens and by the API server (``--token-key``) to verify them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


class TokenError(Exception):
    """Raised when a token is malformed, expired, or fails its signature check."""


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _b64json(obj: dict[str, Any]) -> str:
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _b64url_encode(raw)


def _signing_input(key: str, header_b64: str, payload_b64: str) -> bytes:
    return f"{header_b64}.{payload_b64}".encode("utf-8")


def _hmac(key: str, data: bytes) -> bytes:
    return hmac.new(key.encode("utf-8"), data, hashlib.sha256).digest()


def sign(
    key: str,
    sub: str,
    ttl_seconds: int = 7 * 24 * 3600,
    scope: str = "*",
    iat: int | None = None,
) -> str:
    """Sign a new access token for *sub* (a username/principal label)."""
    header = _b64json({"alg": "HS256", "typ": "JWT"})
    now = int(time.time() if iat is None else iat)
    payload = _b64json({"sub": str(sub), "iat": now, "exp": now + int(ttl_seconds), "scope": scope})
    sig = _hmac(key, _signing_input(key, header, payload))
    return f"{header}.{payload}.{_b64url_encode(sig)}"


def verify(key: str, token: str, now: int | None = None) -> dict[str, Any]:
    """Verify *token* against *key* and return its payload claims.

    Raises :class:`TokenError` for malformed/expired/invalid-signed tokens.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("malformed token")
    header_b64, payload_b64, sig_b64 = parts
    if not header_b64 or not payload_b64 or not sig_b64:
        raise TokenError("malformed token")

    expected = _hmac(key, _signing_input(key, header_b64, payload_b64))
    try:
        provided = _b64url_decode(sig_b64)
    except (ValueError, TypeError) as exc:
        raise TokenError("malformed signature") from exc
    if not hmac.compare_digest(expected, provided):
        raise TokenError("invalid signature")

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise TokenError("bad payload") from exc
    if not isinstance(payload, dict):
        raise TokenError("bad payload")

    current = int(time.time() if now is None else now)
    exp = int(payload.get("exp", 0))
    if current > exp:
        raise TokenError("token expired")
    return payload
