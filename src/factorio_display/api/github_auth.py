"""GitHub OAuth helpers for the factorio-display API.

The browser is redirected to GitHub, GitHub redirects back to
``/auth/github/callback``, the code is exchanged for an access token, the
user's GitHub login is fetched, and the server signs one of *its own* access
tokens (using ``--token-key``) with ``sub = "github:<login>"``.  That token is
handed to the browser, which stores it and uses it like any other access token
(``X-API-Token`` / ``Authorization: Bearer``).

The GitHub *client secret* never leaves the server.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx

from .settings import Settings

_GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
_GITHUB_USER = "https://api.github.com/user"


class GitHubAuthError(Exception):
    """Raised when GitHub rejects an OAuth code or a user fetch fails."""


def is_configured(settings: Settings) -> bool:
    """True when OAuth can be used (client id+secret set and a token key exists)."""
    return bool(
        settings.github_oauth_client_id
        and settings.github_oauth_client_secret
        and settings.github_oauth_redirect_uri
        and settings.token_key
    )


def build_authorize_url(settings: Settings, state: str) -> str:
    """URL to send the browser to for the GitHub OAuth consent screen."""
    params = {
        "client_id": settings.github_oauth_client_id,
        "redirect_uri": settings.github_oauth_redirect_uri,
        "state": state,
        "scope": settings.github_oauth_scope,
    }
    return f"{_GITHUB_AUTHORIZE}?{urlencode(params)}"


async def exchange_code(settings: Settings, code: str) -> str:
    """Exchange an OAuth ``code`` for a GitHub access token."""
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            _GITHUB_TOKEN,
            data={
                "client_id": settings.github_oauth_client_id,
                "client_secret": settings.github_oauth_client_secret,
                "code": code,
                "redirect_uri": settings.github_oauth_redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
    try:
        data = r.json()
    except ValueError as exc:  # pragma: no cover - GitHub always returns JSON with Accept: application/json
        raise GitHubAuthError(f"GitHub returned non-JSON: {r.status_code}") from exc
    if "access_token" not in data:
        desc = data.get("error_description") or data.get("error") or f"HTTP {r.status_code}"
        raise GitHubAuthError(f"GitHub token exchange failed: {desc}")
    return str(data["access_token"])


async def fetch_user(access_token: str) -> dict:
    """Fetch the authenticated GitHub user profile."""
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            _GITHUB_USER,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if r.status_code != 200:
        raise GitHubAuthError(f"GitHub user fetch failed: HTTP {r.status_code}")
    return r.json()


def user_login(user: dict) -> str:
    """The stable identifier for a GitHub user profile."""
    return str(user.get("login") or user.get("id") or "unknown")
