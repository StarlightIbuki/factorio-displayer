"""End-to-end tests for the factorio-display FastAPI (api.server)."""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from factorio_display.api.server import create_app
from factorio_display.api.settings import Settings
from factorio_display.api.store import Store


def _tiny_png_bytes(size: tuple[int, int] = (4, 4), color: tuple[int, int, int] = (180, 60, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _make_client(tmp_path, **overrides) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data",
        max_workers=1,
        max_jobs_per_user=5,
        **overrides,
    )
    app = create_app(settings)
    return TestClient(app)


@pytest.fixture()
def client(tmp_path) -> TestClient:
    with _make_client(tmp_path) as c:
        yield c


def test_health(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["workers"]["max"] == 1


def test_capabilities(client: TestClient) -> None:
    r = client.get("/api/v1/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert "video" in body["input_extensions"]
    assert "piano" in body["instruments"]
    assert body["result_formats"] == ["blueprint", "toml", "yaml", "json"]


def test_sync_display_builder(client: TestClient) -> None:
    r = client.post("/api/v1/blueprints/display", json={"name": "D", "width": 8, "height": 8})
    assert r.status_code == 200
    body = r.json()
    assert body["blueprint"].startswith("0eN")
    assert body["entity_count"] == 64


def test_sync_audio_decoder_builder(client: TestClient) -> None:
    r = client.post("/api/v1/blueprints/audio-decoder", json={"instruments": ["piano"]})
    assert r.status_code == 200
    body = r.json()
    assert body["blueprint"].startswith("0eN")


def test_sync_logical_builder(client: TestClient) -> None:
    r = client.post("/api/v1/blueprints/logical", json={"instrument": "piano"})
    assert r.status_code == 200
    body = r.json()
    assert body["format"] == "toml"
    assert "[[entity]]" in body["text"]


def test_sync_decode_builder(client: TestClient) -> None:
    disp = client.post("/api/v1/blueprints/display", json={"width": 4, "height": 4}).json()
    r = client.post("/api/v1/blueprints/decode", json={"blueprint": disp["blueprint"]})
    assert r.status_code == 200
    assert r.json()["format"] == "yaml"


def test_pastebin_requires_server_key(client: TestClient) -> None:
    """Without a configured PASTEBIN_DEV_KEY the proxy must refuse cleanly."""
    r = client.post("/api/v1/pastebin", json={"text": "0eN test", "name": "bp"})
    assert r.status_code == 400
    body = r.json()
    assert body["detail"]["error"]["code"] == "no_pastebin_key"

    # missing required text → pydantic 422
    r = client.post("/api/v1/pastebin", json={})
    assert r.status_code == 422


def test_cors_allows_github_pages_by_default(client: TestClient) -> None:
    """The GitHub Pages origin is allowed cross-origin out of the box."""
    r = client.options(
        "/api/v1/health",
        headers={
            "Origin": "https://StarlightIbuki.github.io",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://StarlightIbuki.github.io"
    assert "GET" in (r.headers.get("access-control-allow-methods") or "")


def test_cors_rejects_unknown_origin(client: TestClient) -> None:
    """Origins outside the allow-list are refused on preflight (Starlette → 400)."""
    r = client.options(
        "/api/v1/health",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert r.status_code == 400
    assert r.headers.get("access-control-allow-origin") is None


def test_upload_rejects_too_large(tmp_path) -> None:
    """Uploads above Settings.max_upload_bytes are rejected with 413."""
    with _make_client(tmp_path, max_upload_bytes=100) as c:
        big = c.post("/api/v1/uploads", files={"files": ("big.bin", b"x" * 101, "application/octet-stream")})
        assert big.status_code == 413
        assert big.json()["detail"]["error"]["code"] == "too_large"
        small = c.post("/api/v1/uploads", files={"files": ("small.bin", b"x" * 50, "application/octet-stream")})
        assert small.status_code == 201


def test_upload_roundtrip(client: TestClient) -> None:
    r = client.post("/api/v1/uploads", files=[("files", ("tiny.png", _tiny_png_bytes(), "image/png"))])
    assert r.status_code == 201
    uploads = r.json()
    assert len(uploads) == 1
    up = uploads[0]
    assert up["media_type"] == "image"
    assert up["size_bytes"] > 0

    r = client.get(f"/api/v1/uploads/{up['upload_id']}")
    assert r.status_code == 200
    assert r.json()["name"] == "tiny.png"

    r = client.delete(f"/api/v1/uploads/{up['upload_id']}")
    assert r.status_code == 204
    assert client.get(f"/api/v1/uploads/{up['upload_id']}").status_code == 404


def test_encode_job_end_to_end(client: TestClient) -> None:
    """Upload a tiny PNG and encode it through the async subprocess job path."""
    r = client.post("/api/v1/uploads", files=[("files", ("tiny.png", _tiny_png_bytes(), "image/png"))])
    upload_id = r.json()[0]["upload_id"]

    r = client.post(
        "/api/v1/jobs",
        json={
            "type": "encode",
            "inputs": [upload_id],
            "options": {"name": "Tiny", "power": "none", "use_cache": False},
        },
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    status = "queued"
    deadline = time.time() + 180
    while time.time() < deadline:
        resp = client.get(f"/api/v1/jobs/{job_id}")
        assert resp.status_code == 200
        status = resp.json()["status"]
        if status in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.5)

    assert status == "succeeded", client.get(f"/api/v1/jobs/{job_id}").json().get("error")

    r = client.get(f"/api/v1/jobs/{job_id}/result?format=blueprint")
    assert r.status_code == 200
    assert r.text.startswith("0eN")

    r = client.get(f"/api/v1/jobs/{job_id}/result?format=json")
    assert r.status_code == 200
    body = r.json()
    assert body["blueprint"].startswith("0eN")
    assert body["entity_count"] is not None


def test_job_result_while_running_is_409(tmp_path) -> None:
    # Seed a "running" job AFTER app creation (startup recovery would mark it failed).
    settings = Settings(data_dir=tmp_path / "data", max_workers=1)
    app = create_app(settings)
    store = app.state.store
    job_id = "j_seed"
    store.save_job(
        "anonymous",
        job_id,
        {"job_id": job_id, "owner": "anonymous", "type": "encode", "name": "x", "status": "running",
         "progress": {"phase": "running"}, "created_at": 1.0, "started_at": 1.0, "finished_at": None,
         "error": None, "result": None, "inputs": [], "callback_url": None, "config": {}},
    )
    with TestClient(app) as c:
        assert c.get(f"/api/v1/jobs/{job_id}").json()["status"] == "running"
        assert c.get(f"/api/v1/jobs/{job_id}/result").status_code == 409


def test_principal_isolation_same_datadir(tmp_path) -> None:
    """Two principals sharing one data dir must not see each other's data."""
    data = tmp_path / "data"
    # Seed a job + upload for the anonymous principal directly in the store.
    seed_store = Store(Settings(data_dir=data))
    up = seed_store.save_upload("anonymous", "a.mp4", b"media-bytes")
    seed_store.save_job(
        "anonymous",
        "j_seed",
        {"job_id": "j_seed", "owner": "anonymous", "type": "encode", "name": "seed", "status": "succeeded",
         "progress": {}, "created_at": 1.0, "started_at": None, "finished_at": None, "error": None,
         "result": None, "inputs": [], "callback_url": None, "config": {}},
    )

    # Anonymous app sees its own data.
    with _make_client(tmp_path) as anon:
        assert anon.get("/api/v1/jobs/j_seed").status_code == 200
        assert anon.get(f"/api/v1/uploads/{up.upload_id}").status_code == 200

    # A token-gated app (different principal) sharing the same data dir cannot.
    with _make_client(tmp_path, api_token="topsecret") as tok:
        headers = {"Authorization": "Bearer topsecret"}
        assert tok.get("/api/v1/jobs/j_seed", headers=headers).status_code == 404
        assert tok.get(f"/api/v1/uploads/{up.upload_id}", headers=headers).status_code == 404
        assert tok.get("/api/v1/jobs", headers=headers).json()["total"] == 0


def test_token_gate(tmp_path) -> None:
    with _make_client(tmp_path, api_token="s3cret") as c:
        # Public endpoints are open.
        assert c.get("/api/v1/health").status_code == 200
        # Protected endpoints require the token.
        assert c.get("/api/v1/jobs").status_code == 401
        r = c.post("/api/v1/uploads", files=[("files", ("a.bin", b"x", "application/octet-stream"))])
        assert r.status_code == 401
        # Correct token works.
        assert c.get("/api/v1/jobs", headers={"X-API-Token": "s3cret"}).status_code == 200
        assert (
            c.post(
                "/api/v1/uploads",
                files=[("files", ("a.bin", b"x", "application/octet-stream"))],
                headers={"Authorization": "Bearer s3cret"},
            ).status_code
            == 201
        )


def test_compression_gzip(tmp_path) -> None:
    with _make_client(tmp_path, compress_min_size=64) as c:
        r = c.post(
            "/api/v1/blueprints/display",
            json={"name": "Big", "width": 16, "height": 16},
            headers={"Accept-Encoding": "gzip"},
        )
        assert r.status_code == 200
        assert r.headers.get("content-encoding") == "gzip"
        assert r.json()["entity_count"] == 256


def test_compression_brotli(tmp_path) -> None:
    with _make_client(tmp_path, compress_min_size=64) as c:
        r = c.post(
            "/api/v1/blueprints/display",
            json={"name": "Big", "width": 16, "height": 16},
            headers={"Accept-Encoding": "br"},
        )
        assert r.status_code == 200
        assert r.headers.get("content-encoding") == "br"


def test_static_web_app_served(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "factorio-display" in r.text
    assert "btn-first" in r.text
    assert "view-create" in r.text
    for asset in ("/style.css", "/app.js", "/compress.js"):
        assert client.get(asset).status_code == 200


def test_relative_data_dir_is_resolved(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(data_dir="server_data")
    settings.ensure()
    assert settings.data_dir.is_absolute()


def test_encode_job_with_relative_data_dir(tmp_path, monkeypatch) -> None:
    """Upload + encode must work even when --data-dir is a relative path.

    Regression: the encode subprocess runs with cwd=job dir, so a relative
    data dir used to produce unresolvable input paths.
    """
    monkeypatch.chdir(tmp_path)
    settings = Settings(data_dir="srvdata", max_workers=1, max_jobs_per_user=5)
    app = create_app(settings)
    with TestClient(app) as c:
        r = c.post("/api/v1/uploads", files=[("files", ("tiny.png", _tiny_png_bytes(), "image/png"))])
        assert r.status_code == 201
        upload = r.json()[0]
        assert Path(upload["path"]).is_absolute()

        r = c.post("/api/v1/jobs", json={"type": "encode", "inputs": [upload["upload_id"]], "options": {"power": "none", "use_cache": False}})
        assert r.status_code == 202
        job_id = r.json()["job_id"]

        status = "queued"
        deadline = time.time() + 120
        while time.time() < deadline:
            job = c.get(f"/api/v1/jobs/{job_id}").json()
            status = job["status"]
            if status in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(0.5)
        assert status == "succeeded", c.get(f"/api/v1/jobs/{job_id}").json().get("error")
