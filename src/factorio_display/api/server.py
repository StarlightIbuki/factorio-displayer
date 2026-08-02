"""FastAPI application for factorio-display.

Route layout (all under ``/api/v1``):

- ``GET  /health``, ``GET /capabilities``            — public metadata
- ``POST /uploads``, ``GET/DELETE /uploads/{id}``    — media uploads (auth-gated)
- ``POST /jobs``, ``GET /jobs``                      — async encode jobs
- ``GET /jobs/{id}``, ``POST /jobs/{id}/cancel``,
  ``DELETE /jobs/{id}``
- ``GET /jobs/{id}/result``                          — result (blueprint/toml/yaml/json)
- ``POST /jobs/{id}/share``, ``GET /share/{token}``  — temporary public share link
- ``GET /jobs/{id}/artifacts[/{name}]``              — intermediate artifacts
- ``POST /blueprints/display|audio-decoder|logical|decode`` — fast sync builders

Every upload/job/artifact is scoped to the caller's principal.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
import zlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__
from .. import cli as _cli
from .compression import CompressionMiddleware
from .github_auth import (
    GitHubAuthError,
    build_authorize_url,
    exchange_code,
    fetch_user,
    is_configured,
    user_login,
)
from .jobs import JobRunner
from .principal import get_principal
from .schemas import (
    AudioDecoderRequest,
    BuildOut,
    CapabilitiesOut,
    DecodeRequest,
    DisplayRequest,
    HealthOut,
    JobCreate,
    JobListOut,
    JobOut,
    LogicalRequest,
    UploadOut,
)
from .settings import Settings
from .store import Store
from .tokens import sign

_POWER_TYPES = ["small", "medium", "substation", "none"]
_RAIL_MODES = ["piano", "all", "auto[:threshold]", "comma-separated"]
_RESULT_FORMATS = ["blueprint", "toml", "yaml", "json"]
_INSTRUMENTS = ["piano", "bass", "celesta", "plucked", "drum"]

_RESULT_ARTIFACT = {
    "blueprint": "result.txt",
    "json": "result.json",
    "yaml": "result.yaml",
    "toml": "result.toml",
}
_CONTENT_TYPE = {
    "blueprint": "text/plain; charset=utf-8",
    "toml": "text/plain; charset=utf-8",
    "yaml": "application/yaml; charset=utf-8",
    "json": "application/json; charset=utf-8",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application (factory — no import-time side effects)."""
    settings = (settings or Settings()).ensure()
    store = Store(settings)
    runner = JobRunner(settings, store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        runner.shutdown(wait=False)

    app = FastAPI(
        title="factorio-display API",
        version=__version__,
        description="Encode media and build Factorio display/audio blueprints.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.runner = runner
    app.state.started_at = time.time()
    app.state.share_tokens: dict[str, dict] = {}  # token -> {job_id, principal, expires_at}
    app.add_middleware(CompressionMiddleware, minimum_size=settings.compress_min_size)

    # CORS — allow the GitHub Pages frontend (and localhost dev origins) to
    # call this API cross-origin.  Both the explicit list and the regex are
    # configurable via Settings / --cors-origins / --cors-origin-regex.
    if settings.cors_allow_origins or settings.cors_allow_origin_regex:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allow_origins),
            allow_origin_regex=settings.cors_allow_origin_regex or None,
            allow_methods=["*"],
            allow_headers=["*"],
            # Auth is via an X-API-Token header (not cookies), so credentials
            # stay off — this keeps explicit-origin matching simple and safe.
            allow_credentials=False,
        )

    # ── public metadata ──────────────────────────────────────────────
    @app.get("/api/v1/health", response_model=HealthOut, tags=["meta"])
    def health() -> HealthOut:
        busy, queued = _job_counts(settings, store)
        return HealthOut(
            version=__version__,
            workers={"busy": busy, "queued": queued, "max": settings.max_workers},
            uptime_seconds=time.time() - app.state.started_at,
        )

    @app.get("/api/v1/capabilities", response_model=CapabilitiesOut, tags=["meta"])
    def capabilities() -> CapabilitiesOut:
        return CapabilitiesOut(
            version=__version__,
            display={"default_width": 28, "default_height": 26},
            input_extensions={
                "video": sorted(_cli._VIDEO_EXTENSIONS),
                "audio": sorted(_cli._AUDIO_EXTENSIONS),
                "midi": [".mid", ".midi"],
                "image": sorted(_cli._IMAGE_EXTENSIONS),
            },
            instruments=_INSTRUMENTS,
            rail_modes=_RAIL_MODES,
            result_formats=_RESULT_FORMATS,
            power_types=_POWER_TYPES,
            auth={"github": _github_capabilities(settings)},
        )

    # ── GitHub OAuth (login with GitHub) ──────────────────────────────
    @app.get("/auth/github/login", tags=["auth"])
    def auth_github_login(state: str = Query("")) -> RedirectResponse:
        if not is_configured(settings):
            raise HTTPException(
                status_code=503,
                detail=_err("oauth_not_configured", "GitHub OAuth is not configured on this server."),
            )
        return RedirectResponse(build_authorize_url(settings, state))

    @app.get("/auth/github/callback", tags=["auth"])
    async def auth_github_callback(
        code: str | None = Query(None),
        state: str = Query(""),
        error: str | None = Query(None),
    ) -> RedirectResponse:
        if not is_configured(settings):
            raise HTTPException(
                status_code=503,
                detail=_err("oauth_not_configured", "GitHub OAuth is not configured on this server."),
            )
        # User denied / GitHub reported an error → send them back with it.
        if error or not code:
            return _oauth_redirect(settings, {"error": error or "access_denied", "state": state})
        try:
            access_token = await exchange_code(settings, code)
            user = await fetch_user(access_token)
        except GitHubAuthError as exc:
            return _oauth_redirect(settings, {"error": f"oauth_failed: {exc}", "state": state})
        login = user_login(user)
        our_token = sign(
            settings.token_key,
            sub=f"github:{login}",
            ttl_seconds=7 * 24 * 3600,
            scope="*",
        )
        return _oauth_redirect(settings, {"fd_token": our_token, "state": state})

    # ── uploads ──────────────────────────────────────────────────────
    @app.post("/api/v1/uploads", response_model=list[UploadOut], status_code=201, tags=["uploads"])
    async def upload_files(
        files: list[UploadFile] = File(...),
        principal: str = Depends(get_principal),
    ) -> list[UploadOut]:
        out: list[UploadOut] = []
        for file in files:
            data = await file.read()
            if len(data) > settings.max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=_err(
                        "too_large",
                        f"upload too large: {len(data)} bytes "
                        f"(max {settings.max_upload_bytes} bytes)",
                    ),
                )
            rec = store.save_upload(principal, file.filename or "upload.bin", data)
            out.append(UploadOut(**rec.to_dict()))
        return out

    @app.get("/api/v1/uploads/{upload_id}", response_model=UploadOut, tags=["uploads"])
    def get_upload(upload_id: str, principal: str = Depends(get_principal)) -> UploadOut:
        rec = store.load_upload(principal, upload_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=_err("not_found", "upload not found"))
        return UploadOut(**rec.to_dict())

    @app.delete("/api/v1/uploads/{upload_id}", status_code=204, tags=["uploads"])
    def delete_upload(upload_id: str, principal: str = Depends(get_principal)) -> Response:
        if not store.delete_upload(principal, upload_id):
            raise HTTPException(status_code=404, detail=_err("not_found", "upload not found"))
        return Response(status_code=204)

    # ── jobs ─────────────────────────────────────────────────────────
    @app.post("/api/v1/jobs", status_code=202, tags=["jobs"])
    def create_job(
        body: JobCreate,
        principal: str = Depends(get_principal),
    ) -> JSONResponse:
        spec = {
            "type": body.type,
            "inputs": body.inputs,
            "name": body.options.name,
            "callback_url": body.callback_url,
            "config": body.options.to_config_dict(),
        }
        try:
            job_id = runner.submit(principal, spec)
        except RuntimeError as exc:
            raise HTTPException(status_code=429, detail=_err("rate_limited", str(exc))) from exc
        return JSONResponse(
            status_code=202,
            content={
                "job_id": job_id,
                "status": "queued",
                "type": body.type,
                "created_at": runner.get(principal, job_id)["created_at"],
                "result_url": f"/api/v1/jobs/{job_id}/result",
            },
        )

    @app.get("/api/v1/jobs", response_model=JobListOut, tags=["jobs"])
    def list_jobs(
        status: str | None = Query(None),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        principal: str = Depends(get_principal),
    ) -> JobListOut:
        jobs = runner.list(principal, status=status, limit=limit, offset=offset)
        return JobListOut(
            jobs=[_job_out(j, principal) for j in jobs],
            total=len(runner.list(principal, status=status)),
            limit=limit,
            offset=offset,
        )

    @app.get("/api/v1/jobs/{job_id}", response_model=JobOut, tags=["jobs"])
    def get_job(job_id: str, principal: str = Depends(get_principal)) -> JobOut:
        rec = runner.get(principal, job_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=_err("not_found", "job not found"))
        return _job_out(rec, principal)

    @app.post("/api/v1/jobs/{job_id}/cancel", tags=["jobs"])
    def cancel_job(job_id: str, principal: str = Depends(get_principal)) -> dict:
        status = runner.cancel(principal, job_id)
        if status is None:
            raise HTTPException(status_code=404, detail=_err("not_found", "job not found"))
        return {"job_id": job_id, "status": status}

    @app.delete("/api/v1/jobs/{job_id}", status_code=204, tags=["jobs"])
    def delete_job(job_id: str, principal: str = Depends(get_principal)) -> Response:
        if not runner.delete(principal, job_id):
            raise HTTPException(status_code=404, detail=_err("not_found", "job not found"))
        return Response(status_code=204)

    @app.get("/api/v1/jobs/{job_id}/result", tags=["jobs"])
    def job_result(
        job_id: str,
        format: Literal["blueprint", "toml", "yaml", "json"] = "blueprint",
        principal: str = Depends(get_principal),
    ) -> Response:
        rec = runner.get(principal, job_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=_err("not_found", "job not found"))
        status = rec.get("status")
        if status in ("queued", "running"):
            raise HTTPException(
                status_code=409,
                detail=_err("job_running", f"job is {status}; poll GET /jobs/{job_id}"),
            )
        if status == "failed":
            raise HTTPException(status_code=422, detail=_err("job_failed", rec.get("error") or "job failed"))
        if status == "cancelled":
            raise HTTPException(status_code=422, detail=_err("job_failed", "job was cancelled"))

        text = _materialize_result(store, runner, principal, job_id, rec, format)
        return Response(content=text, media_type=_CONTENT_TYPE[format])

    @app.get("/api/v1/jobs/{job_id}/artifacts", tags=["jobs"])
    def list_artifacts(job_id: str, principal: str = Depends(get_principal)) -> dict:
        if runner.get(principal, job_id) is None:
            raise HTTPException(status_code=404, detail=_err("not_found", "job not found"))
        paths = store.artifact_paths(principal, job_id)
        return {
            "artifacts": [
                {
                    "name": p.name,
                    "size_bytes": p.stat().st_size,
                    "url": f"/api/v1/jobs/{job_id}/artifacts/{p.name}",
                }
                for p in paths
            ]
        }

    @app.get("/api/v1/jobs/{job_id}/artifacts/{artifact_name}", tags=["jobs"])
    def download_artifact(
        job_id: str, artifact_name: str, principal: str = Depends(get_principal)
    ) -> FileResponse:
        if runner.get(principal, job_id) is None:
            raise HTTPException(status_code=404, detail=_err("not_found", "job not found"))
        if "/" in artifact_name or "\\" in artifact_name or artifact_name in ("", ".", ".."):
            raise HTTPException(status_code=400, detail=_err("validation_error", "bad artifact name"))
        path = store.artifact_dir(principal, job_id) / artifact_name
        if not path.exists():
            raise HTTPException(status_code=404, detail=_err("not_found", "artifact not found"))
        return FileResponse(path)

    # ── temporary public share links ────────────────────────────────
    @app.post("/api/v1/jobs/{job_id}/share", tags=["jobs"])
    def create_share(job_id: str, principal: str = Depends(get_principal)) -> dict:
        """Issue a short-lived, public URL that serves this job's blueprint with
        permissive CORS — used by "Copy link" and as the FBE source.  No third
        party paste service or server-side key is needed."""
        rec = runner.get(principal, job_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=_err("not_found", "job not found"))
        if rec.get("status") != "succeeded":
            raise HTTPException(
                status_code=409,
                detail=_err("job_not_ready", "only finished jobs can be shared"),
            )
        token = secrets.token_urlsafe(18)
        expires_at = time.time() + settings.share_ttl_hours * 3600
        # Opportunistically drop expired links so the table stays small.
        now = time.time()
        for stale in [k for k, v in app.state.share_tokens.items() if v["expires_at"] < now]:
            app.state.share_tokens.pop(stale, None)
        app.state.share_tokens[token] = {
            "job_id": job_id,
            "principal": principal,
            "expires_at": expires_at,
        }
        return {
            "url": f"/api/v1/share/{token}",
            "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
        }

    @app.get("/api/v1/share/{token}", tags=["meta"])
    def get_share(token: str) -> Response:
        """Publicly serve a shared blueprint (raw text) with ``Access-Control-
        Allow-Origin: *`` so any origin — including FBE's CORS proxy — can fetch it."""
        rec = app.state.share_tokens.get(token)
        now = time.time()
        if rec is None or rec["expires_at"] < now:
            raise HTTPException(
                status_code=410,
                detail=_err("share_expired", "share link is invalid or expired"),
            )
        job_rec = runner.get(rec["principal"], rec["job_id"])
        if job_rec is None or job_rec.get("status") != "succeeded":
            raise HTTPException(
                status_code=410,
                detail=_err("share_expired", "share link is invalid or expired"),
            )
        text = _materialize_result(store, runner, rec["principal"], rec["job_id"], job_rec, "blueprint")
        # Adapt the blueprint for FBE: drop the Space Age `quality`/`id` fields
        # and remap signal names FBE's data doesn't know, so FBE can load it.  The
        # real blueprint (with quality and original signals) is still served by
        # the result endpoint.
        text = _make_fbe_compatible(text)
        return Response(
            content=text,
            media_type=_CONTENT_TYPE["blueprint"],
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=3600",
            },
        )

    # ── fast sync builders ───────────────────────────────────────────
    @app.post("/api/v1/blueprints/display", response_model=BuildOut, tags=["builders"])
    def build_display(
        body: DisplayRequest,
        principal: str = Depends(get_principal),  # pylint: disable=unused-argument
    ) -> BuildOut:
        from .. import service  # pylint: disable=import-outside-toplevel

        res = service.export_display(service.DisplayConfig(**body.model_dump()))
        return _build_out(res)

    @app.post("/api/v1/blueprints/audio-decoder", response_model=BuildOut, tags=["builders"])
    def build_audio_decoder(
        body: AudioDecoderRequest,
        principal: str = Depends(get_principal),  # pylint: disable=unused-argument
    ) -> BuildOut:
        from .. import service  # pylint: disable=import-outside-toplevel

        res = service.export_audio_decoder(service.AudioDecoderConfig(**body.model_dump()))
        return _build_out(res)

    @app.post("/api/v1/blueprints/logical", response_model=BuildOut, tags=["builders"])
    def build_logical(
        body: LogicalRequest,
        principal: str = Depends(get_principal),  # pylint: disable=unused-argument
    ) -> BuildOut:
        from .. import service  # pylint: disable=import-outside-toplevel

        res = service.export_logical(service.LogicalConfig(**body.model_dump()))
        return _build_out(res)

    @app.post("/api/v1/blueprints/decode", response_model=BuildOut, tags=["builders"])
    def decode(
        body: DecodeRequest,
        principal: str = Depends(get_principal),  # pylint: disable=unused-argument
    ) -> BuildOut:
        from .. import service  # pylint: disable=import-outside-toplevel

        res = service.decode_blueprint(body.blueprint)
        return _build_out(res)

    # ── static web app (mounted last so /api/v1/* routes win) ────────
    static_dir = settings.static_dir or (Path(__file__).resolve().parent / "static")
    if static_dir.is_dir():
        app.mount(
            "/", StaticFiles(directory=str(static_dir), html=True), name="static"
        )

    return app


# ── helpers ────────────────────────────────────────────────────────────


def _err(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _github_capabilities(settings: Settings) -> dict | None:
    """Public GitHub OAuth info for the frontend (never the client secret)."""
    if not is_configured(settings):
        return None
    return {
        "client_id": settings.github_oauth_client_id,
        "redirect_uri": settings.github_oauth_redirect_uri,
        "frontend_url": settings.frontend_url,
    }


def _oauth_redirect(settings: Settings, params: dict) -> RedirectResponse:
    """Redirect the browser back to the SPA with OAuth params appended."""
    from urllib.parse import urlencode  # pylint: disable=import-outside-toplevel

    base = settings.frontend_url or "http://127.0.0.1:8000/"
    sep = "&" if "?" in base else "?"
    return RedirectResponse(base + sep + urlencode(params))


def _job_out(rec: dict, principal: str) -> JobOut:
    job_id = rec.get("job_id", "")
    return JobOut(
        job_id=job_id,
        type=rec.get("type", ""),
        name=rec.get("name", ""),
        status=rec.get("status", "unknown"),
        progress=rec.get("progress", {}),
        created_at=rec.get("created_at", 0.0),
        started_at=rec.get("started_at"),
        finished_at=rec.get("finished_at"),
        error=rec.get("error"),
        result=rec.get("result"),
        result_url=f"/api/v1/jobs/{job_id}/result",
    )


def _build_out(res) -> BuildOut:
    return BuildOut(
        blueprint=res.blueprint,
        text=res.text,
        format=res.format,
        name=res.name,
        entity_count=res.entity_count,
        instruments=res.instruments,
    )


def _job_counts(settings: Settings, store: Store) -> tuple[int, int]:
    busy = 0
    queued = 0
    data_dir = settings.data_dir
    if data_dir.exists():
        for owner_dir in data_dir.iterdir():
            jobs_dir = owner_dir / "jobs"
            if not jobs_dir.is_dir():
                continue
            for rec in store.list_jobs(owner_dir.name):
                status = rec.get("status")
                if status == "running":
                    busy += 1
                elif status == "queued":
                    queued += 1
    return busy, queued


def _ensure_blueprint_icons(text: str) -> str:
    """Inject a minimal ``icons`` array into a blueprint string that lacks one.

    Factorio always writes icons and FBE's schema requires the field — without
    it FBE rejects the blueprint.  Applied at serve time so blueprints produced
    before the generator emitted icons (old jobs) are still FBE-compatible.
    """
    if not text or not text.startswith("0"):
        return text
    try:
        raw = base64.b64decode(text[1:])
        data = json.loads(zlib.decompress(raw).decode("utf-8"))
    except Exception:  # pylint: disable=broad-exception-caught
        return text
    bp = data.get("blueprint")
    if not isinstance(bp, dict) or bp.get("icons"):
        return text
    bp["icons"] = [{"index": 1, "signal": {"type": "virtual", "name": "signal-0"}}]
    try:
        raw = zlib.compress(json.dumps(data, separators=(",", ":")).encode("utf-8"))
        return "0" + base64.b64encode(raw).decode("ascii")
    except Exception:  # pylint: disable=broad-exception-caught
        return text


# Item names used by this project's signal pool (see integer2signal/mapping.py)
# that FBE's bundled Factorio data (2.0.68, base game) does not include.  FBE's
# ajv keywords (itemName/itemFluidSignalRecipeEntityName) check these against its
# data and reject the whole blueprint as "modded" if any are present, so on the
# share path we remap them to known items.
_FBE_UNKNOWN_ITEMS = {
    "agricultural-tower",
    "artificial-jellynut-soil",
    "artificial-yumako-soil",
    "big-mining-drill",
    "biochamber",
    "biolab",
    "captive-biter-spawner",
    "cryogenic-plant",
    "electromagnetic-plant",
    "foundation",
    "foundry",
    "fusion-generator",
    "fusion-reactor",
    "heating-tower",
    "ice-platform",
    "lightning-collector",
    "lightning-rod",
    "overgrowth-jellynut-soil",
    "overgrowth-yumako-soil",
    "quality-module",
    "quality-module-2",
    "quality-module-3",
    "rail-ramp",
    "rail-support",
    "recycler",
    "stack-inserter",
    "turbo-loader",
    "turbo-splitter",
    "turbo-transport-belt",
    "turbo-underground-belt",
}

# Known-safe fallback items (all present in FBE's 2.0.68 data) substituted for
# the unknown ones.  Each unknown name maps to a distinct fallback so the
# channels still look different in FBE.
_FBE_FALLBACK_ITEMS = [
    "wooden-chest",
    "iron-chest",
    "steel-chest",
    "storage-tank",
    "transport-belt",
    "fast-transport-belt",
    "express-transport-belt",
    "underground-belt",
    "fast-underground-belt",
    "express-underground-belt",
    "splitter",
    "fast-splitter",
    "express-splitter",
    "burner-inserter",
    "inserter",
    "long-handed-inserter",
    "fast-inserter",
    "bulk-inserter",
    "small-electric-pole",
    "medium-electric-pole",
    "big-electric-pole",
    "substation",
    "pipe",
    "pipe-to-ground",
    "pump",
    "boiler",
    "steam-engine",
    "small-lamp",
    "constant-combinator",
    "arithmetic-combinator",
]

_FBE_ITEM_REMAP = {
    name: _FBE_FALLBACK_ITEMS[i % len(_FBE_FALLBACK_ITEMS)]
    for i, name in enumerate(sorted(_FBE_UNKNOWN_ITEMS))
}


def _make_fbe_compatible(text: str) -> str:
    """Adapt a blueprint for FBE (Factorio 2.0.68, base game) so it can load it.

    FBE's schema only allows ``{name, type}`` on signals (``additionalProperties:
    false``), so we drop the Space Age ``quality``/``id`` fields; and any signal
    name FBE's bundled data doesn't know is remapped to a known item, otherwise
    FBE rejects the blueprint as "modded".  FBE is a viewer — the authoritative
    blueprint (with quality and original signals) is still served by the result
    endpoint.  Applied only to the public share link.
    """
    if not text or not text.startswith("0"):
        return text
    try:
        raw = base64.b64decode(text[1:])
        data = json.loads(zlib.decompress(raw).decode("utf-8"))
    except Exception:  # pylint: disable=broad-exception-caught
        return text

    def _clean(node):
        if isinstance(node, dict):
            if "name" in node and "type" in node:
                node.pop("quality", None)
                node.pop("id", None)
                if node.get("type") == "item" and node.get("name") in _FBE_ITEM_REMAP:
                    node["name"] = _FBE_ITEM_REMAP[node["name"]]
            for v in node.values():
                _clean(v)
        elif isinstance(node, list):
            for v in node:
                _clean(v)

    _clean(data)
    try:
        raw = zlib.compress(json.dumps(data, separators=(",", ":")).encode("utf-8"))
        return "0" + base64.b64encode(raw).decode("ascii")
    except Exception:  # pylint: disable=broad-exception-caught
        return text


def _materialize_result(
    store: Store, runner: JobRunner, principal: str, job_id: str, rec: dict, fmt: str
) -> str:
    """Return the result text for *fmt*, using the stored artifact or synthesising it."""
    name = _RESULT_ARTIFACT[fmt]
    text = store.read_artifact_text(principal, job_id, name)
    if text is not None:
        if fmt == "blueprint":
            text = _ensure_blueprint_icons(text)
        return text

    # Not stored — synthesise from the blueprint (the common case for encode).
    result = rec.get("result") or {}
    blueprint = str(result.get("blueprint") or "")
    if not blueprint:
        raise HTTPException(
            status_code=415,
            detail=_err("unsupported_format", f"result format '{fmt}' is not available for this job"),
        )
    try:
        if fmt == "json":
            return json.dumps(result, ensure_ascii=False, indent=2)
        if fmt == "yaml":
            from ..logical_blueprint import blueprint_string_to_yaml  # pylint: disable=import-outside-toplevel

            return blueprint_string_to_yaml(blueprint)
        if fmt == "toml":
            from draftsman.blueprintable import Blueprint  # pylint: disable=import-outside-toplevel
            from ..logical_blueprint import from_draftsman, to_toml  # pylint: disable=import-outside-toplevel

            return to_toml(from_draftsman(Blueprint.from_string(blueprint)))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise HTTPException(
            status_code=415,
            detail=_err("unsupported_format", f"could not produce '{fmt}': {exc}"),
        ) from exc
    raise HTTPException(status_code=415, detail=_err("unsupported_format", f"unknown format '{fmt}'"))


def serve(settings: Settings) -> None:
    """Run the uvicorn server (blocking)."""
    import uvicorn  # pylint: disable=import-outside-toplevel

    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port)
