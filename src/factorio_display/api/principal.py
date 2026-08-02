"""Principal resolution — who owns what.

Every upload, job and artifact belongs to a *principal* (an opaque string).

Auth modes (checked in this order):

- ``--token-key <key>`` — signed access tokens.  The request must carry a
  token issued by ``factorio-display token issue --key <same key>`` (sent as
  ``Authorization: Bearer <token>`` or ``X-API-Token: <token>``).  The
  principal is derived from the token's ``sub`` claim, so each user is fully
  isolated from the others.
- ``--api-token <secret>`` — legacy shared-secret gate.  Every request must
  carry it; each distinct token maps to a stable principal.
- neither — everything belongs to a single ``"anonymous"`` bucket.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, Request

from .settings import Settings
from .tokens import TokenError, verify


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            return token
    token = request.headers.get("x-api-token")
    return token or None


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={
            "code": "unauthorized",
            "message": message,
        },
    )


def _principal_id(seed: str) -> str:
    return f"tk_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"


def resolve_principal(request: Request, settings: Settings) -> str:
    """Return the principal id for *request*, enforcing auth when configured.

    Raises ``HTTPException(401)`` when a token/key is configured but missing or wrong.
    """
    # Mode 1: signed access tokens (HMAC key on the server).
    if settings.token_key:
        token = _extract_token(request)
        if token is None:
            raise _unauthorized(
                "Missing access token. Send it as 'Authorization: Bearer <token>' "
                "or 'X-API-Token: <token>'. Issue one with "
                "'factorio-display token issue --key <server token-key> --user <name>'."
            )
        try:
            claims = verify(settings.token_key, token)
        except TokenError as exc:
            raise _unauthorized(f"Invalid access token: {exc}") from exc
        sub = str(claims.get("sub") or "")
        return _principal_id(f"{settings.token_key}:{sub}")

    # Mode 2: legacy shared-secret gate.
    if settings.api_token:
        token = _extract_token(request)
        if token is None or not hmac.compare_digest(token, settings.api_token):
            raise _unauthorized(
                "Invalid or missing API token. Send it as "
                "'Authorization: Bearer <token>' or 'X-API-Token: <token>'."
            )
        return _principal_id(settings.api_token)

    return "anonymous"


def get_principal(request: Request) -> str:
    """FastAPI dependency: resolve the caller's principal (401 when gated)."""
    settings: Settings = request.app.state.settings
    return resolve_principal(request, settings)
