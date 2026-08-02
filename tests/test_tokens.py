"""Tests for HMAC-signed access tokens (api.tokens) and the API auth gate."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from factorio_display.api.server import create_app
from factorio_display.api.settings import Settings
from factorio_display.api.tokens import TokenError, sign, verify

KEY = "unit-test-secret"


def _client(tmp_path, **overrides) -> TestClient:
    settings = Settings(data_dir=tmp_path / "data", max_workers=1, max_jobs_per_user=5, **overrides)
    return TestClient(create_app(settings))


# ── token primitive tests ──────────────────────────────────────────────


def test_sign_verify_roundtrip() -> None:
    token = sign(KEY, "alice", ttl_seconds=3600, scope="*")
    claims = verify(KEY, token)
    assert claims["sub"] == "alice"
    assert claims["scope"] == "*"
    assert claims["exp"] - claims["iat"] == 3600


def test_verify_wrong_key_rejected() -> None:
    token = sign(KEY, "alice")
    with pytest.raises(TokenError):
        verify("other-key", token)


def test_verify_tampered_token_rejected() -> None:
    token = sign(KEY, "alice")
    head, payload, sig = token.split(".")
    tampered = f"{head}.{payload}x.{sig}"
    with pytest.raises(TokenError):
        verify(KEY, tampered)


def test_verify_expired_token_rejected() -> None:
    token = sign(KEY, "alice", ttl_seconds=10, iat=int(time.time()) - 100)
    with pytest.raises(TokenError):
        verify(KEY, token)


def test_verify_malformed_rejected() -> None:
    with pytest.raises(TokenError):
        verify(KEY, "not-a-token")
    with pytest.raises(TokenError):
        verify(KEY, "a.b")


# ── API auth gate ──────────────────────────────────────────────────────


def test_token_key_requires_valid_token(tmp_path) -> None:
    with _client(tmp_path, token_key=KEY) as c:
        # no token → 401 (auth-gated endpoint)
        assert c.get("/api/v1/jobs").status_code == 401
        # wrong token → 401
        assert c.get("/api/v1/jobs", headers={"X-API-Token": "bogus"}).status_code == 401
        # valid token → 200
        token = sign(KEY, "alice")
        r = c.get("/api/v1/jobs", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        # expired token → 401
        expired = sign(KEY, "alice", ttl_seconds=1, iat=int(time.time()) - 100)
        assert c.get("/api/v1/jobs", headers={"X-API-Token": expired}).status_code == 401


def test_token_users_are_isolated(tmp_path) -> None:
    with _client(tmp_path, token_key=KEY) as c:
        alice = sign(KEY, "alice")
        bob = sign(KEY, "bob")
        # Alice uploads, Bob must not see it.
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        up = c.post(
            "/api/v1/uploads",
            files={"files": ("a.png", png, "image/png")},
            headers={"X-API-Token": alice},
        )
        assert up.status_code == 201
        upload_id = up.json()[0]["upload_id"]

        r_bob = c.get(f"/api/v1/uploads/{upload_id}", headers={"X-API-Token": bob})
        assert r_bob.status_code == 404
        r_alice = c.get(f"/api/v1/uploads/{upload_id}", headers={"X-API-Token": alice})
        assert r_alice.status_code == 200
