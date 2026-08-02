"""End-to-end tests for the factorio-display FastAPI (api.server)."""

from __future__ import annotations

import base64
import io
import json
import time
import zlib
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


def test_blueprint_ascii_endpoint(client: TestClient) -> None:
    """The ASCII-art endpoint renders entities + wiring maps for a blueprint."""
    disp = client.post("/api/v1/blueprints/display", json={"width": 4, "height": 4}).json()
    r = client.post("/api/v1/blueprints/ascii", json={"blueprint": disp["blueprint"]})
    assert r.status_code == 200
    body = r.json()
    assert body["format"] == "ascii"
    assert "Blueprint entities" in body["text"]
    assert "Wiring" in body["text"]
    # A 4x4 display is all lamps wired into one red data bus.
    assert "small-lamp" in body["text"]


def test_blueprint_render_endpoint(client: TestClient) -> None:
    """The render endpoint returns ASCII text plus a structured preview model."""
    disp = client.post("/api/v1/blueprints/display", json={"width": 4, "height": 4}).json()
    r = client.post("/api/v1/blueprints/render", json={"blueprint": disp["blueprint"]})
    assert r.status_code == 200
    body = r.json()
    assert "ascii" in body and "Blueprint entities" in body["ascii"]
    model = body["model"]
    assert len(model["entities"]) == 16          # 4x4 lamp grid
    assert model["entities"][0]["kind"] == "one"
    assert model["entities"][0]["letter"] == "L"
    assert len(model["networks"]) == 1           # one red data bus
    assert model["networks"][0]["color"] == "red"
    assert len(model["wires"]) == 15             # 16 lamps daisy-chained
    assert model["ports"][0]["red"]["input"] == 0
    assert model["min_x"] == 0 and model["max_y"] == 3


def test_share_link_lifecycle(client: TestClient) -> None:
    """A finished job can be shared; the public link serves the blueprint with CORS."""
    r = client.post("/api/v1/uploads", files=[("files", ("tiny.png", _tiny_png_bytes(), "image/png"))])
    upload_id = r.json()[0]["upload_id"]
    r = client.post(
        "/api/v1/jobs",
        json={"type": "encode", "inputs": [upload_id], "options": {"name": "Tiny", "power": "none", "use_cache": False}},
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    deadline = time.time() + 180
    while time.time() < deadline:
        status = client.get(f"/api/v1/jobs/{job_id}").json()["status"]
        if status in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.5)
    assert status == "succeeded", client.get(f"/api/v1/jobs/{job_id}").json().get("error")

    r = client.post(f"/api/v1/jobs/{job_id}/share")
    assert r.status_code == 200
    share = r.json()
    assert share["url"].startswith("/api/v1/share/")
    token = share["url"].rsplit("/", 1)[1]

    # Public link is reachable WITHOUT auth, serves a valid blueprint, and is CORS-open.
    r = client.get(f"/api/v1/share/{token}")
    assert r.status_code == 200
    body = json.loads(zlib.decompress(base64.b64decode(r.text[1:])).decode())
    assert "blueprint" in body
    assert r.headers.get("access-control-allow-origin") == "*"

    # Unknown / expired token → 410 Gone.
    assert client.get("/api/v1/share/does-not-exist").status_code == 410


def test_share_requires_finished_job(tmp_path) -> None:
    """Sharing a non-existent or in-flight job is refused."""
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
        assert c.post("/api/v1/jobs/j_seed/share").status_code == 409
        assert c.post("/api/v1/jobs/missing/share").status_code == 404


def test_bug_report_records_job_and_preserves_uploads(client: TestClient) -> None:
    """A finished job can be reported: snapshot blueprint + mark uploads kept."""
    r = client.post("/api/v1/uploads", files=[("files", ("tiny.png", _tiny_png_bytes(), "image/png"))])
    upload_id = r.json()[0]["upload_id"]
    r = client.post(
        "/api/v1/jobs",
        json={"type": "encode", "inputs": [upload_id], "options": {"name": "Tiny", "power": "none", "use_cache": False}},
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    deadline = time.time() + 180
    status = "queued"
    while time.time() < deadline:
        status = client.get(f"/api/v1/jobs/{job_id}").json()["status"]
        if status in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.5)
    assert status == "succeeded", client.get(f"/api/v1/jobs/{job_id}").json().get("error")

    # In-flight job is refused (only finished jobs are reportable).
    assert client.post("/api/v1/jobs/does-not-exist/bug-report").status_code == 404

    # No-body POST still works (comment/contact default empty).
    assert client.post(f"/api/v1/jobs/{job_id}/bug-report").status_code == 200

    r = client.post(
        f"/api/v1/jobs/{job_id}/bug-report",
        json={"comment": "Display stays blank", "contact": "me@example.com"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == job_id
    assert body["status"] == "succeeded"
    assert body["preserved_uploads"] == 1
    assert body["report"].endswith(".json")

    # The upload is marked for long-term preservation.
    store = client.app.state.store
    up = store.load_upload("anonymous", upload_id)
    assert up is not None and up.preserved is True

    # The report is persisted with the generated blueprint (+ FBE-compatible).
    report = Path(body["report"])
    assert report.exists()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["job_id"] == job_id
    assert payload["owner"] == "anonymous"
    assert payload["blueprint"].startswith("0")
    assert payload["blueprint_compatible"].startswith("0")
    assert payload["comment"] == "Display stays blank"
    assert payload["contact"] == "me@example.com"

    # The input file is copied into the report's files dir (self-contained).
    files_dir = store.bug_report_files_dir("anonymous", job_id)
    assert files_dir.exists()
    assert any(p.is_file() for p in files_dir.iterdir())

    # Re-reporting the same job overwrites the report (idempotent).
    assert client.post(f"/api/v1/jobs/{job_id}/bug-report").status_code == 200


def test_ensure_blueprint_icons_injects_missing() -> None:
    """Serve-time icon injection makes old (icon-less) blueprints FBE-compatible."""
    from factorio_display import service
    from factorio_display.api.server import _ensure_blueprint_icons

    bpstr = service.export_display(service.DisplayConfig(name="X", width=2, height=2)).blueprint
    data = json.loads(zlib.decompress(base64.b64decode(bpstr[1:])).decode())
    data["blueprint"].pop("icons", None)
    noicons = "0" + base64.b64encode(zlib.compress(json.dumps(data, separators=(",", ":")).encode())).decode()

    fixed = _ensure_blueprint_icons(noicons)
    out = json.loads(zlib.decompress(base64.b64decode(fixed[1:])).decode())
    assert out["blueprint"]["icons"]
    # Already carrying icons → left untouched.
    assert _ensure_blueprint_icons(bpstr) == bpstr


def test_make_fbe_compatible_strips_quality_and_remaps_unknown_items() -> None:
    """The share path strips `quality` and remaps item names FBE doesn't know."""
    from factorio_display.api import server
    from factorio_display.api.server import _make_fbe_compatible

    data = {"blueprint": {"item": "blueprint", "version": 1, "icons": [], "entities": [
        {"entity_number": 1, "name": "decider-combinator", "position": {"x": 0, "y": 0},
         "control_behavior": {"decider_conditions": {"outputs": [
             {"signal": {"type": "item", "name": "iron-chest", "quality": "uncommon"}},
             {"signal": {"type": "item", "name": "turbo-transport-belt", "quality": "rare"}},
             {"signal": {"type": "virtual", "name": "signal-0", "quality": "legendary"}},
         ]}}},
    ]}}
    bp = "0" + base64.b64encode(zlib.compress(json.dumps(data, separators=(",", ":")).encode())).decode()
    out = json.loads(zlib.decompress(base64.b64decode(_make_fbe_compatible(bp)[1:])).decode())
    outs = out["blueprint"]["entities"][0]["control_behavior"]["decider_conditions"]["outputs"]
    sig_known, sig_unknown, sig_virtual = (o["signal"] for o in outs)
    assert "quality" not in sig_known
    assert "quality" not in sig_unknown
    assert "quality" not in sig_virtual
    assert sig_known["name"] == "iron-chest"
    assert sig_unknown["name"] != "turbo-transport-belt"  # remapped to a known item
    assert sig_unknown["name"] in server._FBE_FALLBACK_ITEMS
    assert sig_virtual["name"] == "signal-0"  # virtual signals keep their name


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


def _seed_job(store: Store, principal: str, job_id: str, status: str, created_at: float) -> None:
    store.save_job(principal, job_id, {
        "job_id": job_id,
        "owner": principal,
        "type": "encode",
        "name": "x",
        "status": status,
        "progress": {"phase": status},
        "created_at": created_at,
        "started_at": created_at,
        "finished_at": created_at,
        "error": None,
        "result": None,
        "inputs": [],
        "callback_url": None,
        "config": {},
    })


def test_anonymous_rate_limit_processing_and_queued(tmp_path) -> None:
    """The anonymous bucket allows at most 1 processing and 5 queued jobs."""
    settings = Settings(
        data_dir=tmp_path / "data",
        max_workers=1,
        max_jobs_per_user=100,          # non-anonymous callers would be fine
        anonymous_max_processing=1,
        anonymous_max_queued=2,         # 1 running + 2 queued → next is rejected
        anonymous_max_per_hour=100,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        now = time.time()
        for i, status in enumerate(["running", "queued", "queued"]):
            _seed_job(app.state.store, "anonymous", f"j_{i}", status, now)
        r = c.post("/api/v1/jobs", json={"type": "encode", "inputs": [], "options": {}})
        assert r.status_code == 429
        assert r.json()["detail"]["error"]["code"] == "rate_limited"


def test_anonymous_rate_limit_per_hour(tmp_path) -> None:
    """The anonymous bucket also caps submissions at 20 (here: 3) per hour."""
    settings = Settings(
        data_dir=tmp_path / "data",
        max_workers=1,
        max_jobs_per_user=100,
        anonymous_max_processing=5,     # active-count limits won't trigger
        anonymous_max_queued=50,
        anonymous_max_per_hour=3,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        now = time.time()
        for i in range(3):  # 3 recent jobs (succeeded) → hourly cap reached
            _seed_job(app.state.store, "anonymous", f"j_{i}", "succeeded", now - i)
        r = c.post("/api/v1/jobs", json={"type": "encode", "inputs": [], "options": {}})
        assert r.status_code == 429

        # Old jobs (outside the 1h window) don't count against the hourly cap.
        for i in range(3, 6):
            _seed_job(app.state.store, "anonymous", f"j_old_{i}", "succeeded", now - 7200)
        # But we still need a free active slot to reach the hourly check.
        app.state.store.delete_job("anonymous", "j_0")
        app.state.store.delete_job("anonymous", "j_1")
        app.state.store.delete_job("anonymous", "j_2")
        r = c.post("/api/v1/jobs", json={"type": "encode", "inputs": [], "options": {}})
        assert r.status_code == 202


def test_anonymous_limits_do_not_apply_to_token_users(tmp_path) -> None:
    """Rate limits for the anonymous bucket don't leak to signed-in users."""
    settings = Settings(
        data_dir=tmp_path / "data",
        max_workers=1,
        max_jobs_per_user=1,            # signed-in users capped at 1 active job
        token_key="tok",
        anonymous_max_processing=1,
        anonymous_max_queued=1,
        anonymous_max_per_hour=1,
    )
    app = create_app(settings)
    from factorio_display.api.tokens import sign
    with TestClient(app) as c:
        token = sign("tok", "alice")
        now = time.time()
        # Fill the anonymous bucket to its limits.
        for i, status in enumerate(["running", "queued"]):
            _seed_job(app.state.store, "anonymous", f"j_{i}", status, now)
        _seed_job(app.state.store, "anonymous", "j_hour", "succeeded", now)
        # A signed-in user with a free slot can still submit.
        r = c.post(
            "/api/v1/jobs",
            json={"type": "encode", "inputs": [], "options": {}},
            headers={"X-API-Token": token},
        )
        assert r.status_code == 202
