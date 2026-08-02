"""Tests for GitHub OAuth login (auth/github/*) and capabilities auth info."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from factorio_display.api.server import create_app, exchange_code, fetch_user
from factorio_display.api.settings import Settings
from factorio_display.api.tokens import TokenError, verify

KEY = "oauth-test-key"


def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        data_dir=tmp_path / "data",
        max_workers=1,
        max_jobs_per_user=5,
        token_key=KEY,
        github_oauth_client_id="client-123",
        github_oauth_client_secret="top-secret",
        github_oauth_redirect_uri="https://factorio.qvq.moe:60012/auth/github/callback",
        frontend_url="https://StarlightIbuki.github.io/factorio-displayer/",
    )
    base.update(overrides)
    return Settings(**base)


def _client(tmp_path, **overrides) -> TestClient:
    return TestClient(create_app(_settings(tmp_path, **overrides)))


def test_oauth_503_when_not_configured(tmp_path) -> None:
    with _client(tmp_path, github_oauth_client_id="", github_oauth_client_secret="") as c:
        assert c.get("/auth/github/login?state=x").status_code == 503
        assert c.get("/auth/github/callback?code=c").status_code == 503


def test_login_redirects_to_github(tmp_path) -> None:
    with _client(tmp_path) as c:
        r = c.get("/auth/github/login?state=abc123", follow_redirects=False)
        assert r.status_code == 307
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["client_id"] == ["client-123"]
        assert q["redirect_uri"] == ["https://factorio.qvq.moe:60012/auth/github/callback"]
        assert q["state"] == ["abc123"]
        assert q["scope"] == ["read:user"]
        assert urlparse(r.headers["location"]).netloc == "github.com"


def test_callback_issues_token_and_redirects(tmp_path, monkeypatch) -> None:
    async def fake_exchange(settings, code):
        assert code == "the-code"
        return "gh-access-token"

    async def fake_fetch(token):
        assert token == "gh-access-token"
        return {"login": "octocat", "id": 12345}

    monkeypatch.setattr("factorio_display.api.server.exchange_code", fake_exchange)
    monkeypatch.setattr("factorio_display.api.server.fetch_user", fake_fetch)

    with _client(tmp_path) as c:
        r = c.get("/auth/github/callback?code=the-code&state=st1", follow_redirects=False)
        assert r.status_code == 307
        loc = r.headers["location"]
        assert loc.startswith("https://StarlightIbuki.github.io/factorio-displayer/")
        q = parse_qs(urlparse(loc).query)
        assert q["state"] == ["st1"]
        token = q["fd_token"][0]
        # the token is verifiable and scoped to the github user
        claims = verify(KEY, token)
        assert claims["sub"] == "github:octocat"
        # and it actually authenticates on a gated endpoint, isolated per user
        r2 = c.get("/api/v1/jobs", headers={"Authorization": f"Bearer {token}"})
        assert r2.status_code == 200
        assert r2.json()["total"] == 0


def test_callback_handles_error(tmp_path) -> None:
    with _client(tmp_path) as c:
        r = c.get("/auth/github/callback?error=access_denied&state=st9", follow_redirects=False)
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["error"] == ["access_denied"]
        assert "fd_token" not in q


def test_callback_handles_github_failure(tmp_path, monkeypatch) -> None:
    async def fake_exchange(settings, code):
        from factorio_display.api.github_auth import GitHubAuthError

        raise GitHubAuthError("bad code")

    monkeypatch.setattr("factorio_display.api.server.exchange_code", fake_exchange)
    with _client(tmp_path) as c:
        r = c.get("/auth/github/callback?code=bad", follow_redirects=False)
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["error"][0].startswith("oauth_failed:")
        assert "fd_token" not in q


def test_capabilities_expose_oauth_but_not_secret(tmp_path) -> None:
    with _client(tmp_path) as c:
        r = c.get("/api/v1/capabilities")
        gh = r.json()["auth"]["github"]
        assert gh["client_id"] == "client-123"
        assert gh["redirect_uri"].endswith("/auth/github/callback")
        assert "frontend_url" in gh
        assert "client_secret" not in gh and "secret" not in str(gh)


def test_capabilities_null_when_not_configured(tmp_path) -> None:
    with _client(tmp_path, github_oauth_client_id="") as c:
        assert c.get("/api/v1/capabilities").json()["auth"]["github"] is None
