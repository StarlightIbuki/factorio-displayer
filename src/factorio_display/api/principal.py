"""Principal resolution — who owns what.

Every upload, job and artifact belongs to a *principal* (an opaque string).
In the current (pre-OIDC) phase:

- if ``--api-token`` is configured, a request must carry it (``Authorization:
  Bearer <token>`` or ``X-API-Token``); each distinct token maps to a stable
  principal, so different token holders are fully isolated from each other;
- if no token is configured, everything belongs to a single ``"anonymous"``
  bucket.

When OIDC lands (final phase) the Google identity simply resolves to a
principal string — the storage/job isolation model below is unchanged.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, Request

from .settings import Settings


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            return token
    token = request.headers.get("x-api-token")
    return token or None


def resolve_principal(request: Request, settings: Settings) -> str:
    """Return the principal id for *request*, enforcing the ``--api-token`` gate.

    Raises ``HTTPException(401)`` when a token is configured but missing/wrong.
    """
    if settings.api_token:
        token = _extract_token(request)
        if token is None or not hmac.compare_digest(token, settings.api_token):
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "unauthorized",
                    "message": "Invalid or missing API token. Send it as "
                    "'Authorization: Bearer <token>' or 'X-API-Token: <token>'.",
                },
            )
        digest = hashlib.sha256(settings.api_token.encode("utf-8")).hexdigest()[:16]
        return f"tk_{digest}"
    return "anonymous"


def get_principal(request: Request) -> str:
    """FastAPI dependency: resolve the caller's principal (401 when gated)."""
    settings: Settings = request.app.state.settings
    return resolve_principal(request, settings)
