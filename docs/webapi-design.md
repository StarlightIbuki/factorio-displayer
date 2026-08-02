# factorio-display Web API — Design Proposal

**Status:** DRAFT — awaiting approval before implementation
**Scope:** (1) CLI improvements that make a solid web API possible, (2) the REST API surface, (3) roadmap for a web frontend.

> **Implementation status (2026-08-02):** The initial API is **implemented and tested**.
> - `src/factorio_display/service.py` — typed configs, results, in-process builders, `MediaConfig.to_argv()`.
> - `src/factorio_display/cli.py` — `--json` envelope on every subcommand; dispatch extracted to `_dispatch()`; fixed a latent `export-display` bug (`Blueprint + str`).
> - `src/factorio_display/__main__.py` — `python -m factorio_display` (used by the job runner).
> - `src/factorio_display/api/` — `settings`, `principal` (per-caller isolation + `--api-token` gate), `store` (per-principal dirs, gzip artifacts), `jobs` (async runner, encode runs as an isolated subprocess, persisted `job.json`, webhooks), `schemas`, `compression` (gzip + brotli), `server` (FastAPI factory, lifespan shutdown).
> - CLI `factorio-display server` subcommand; `pyproject.toml` `web` extra.
> - Tests: `tests/test_service.py`, `tests/test_api.py` (23 tests incl. end-to-end encode job, principal isolation, compression); full suite 433 passed / 2 skipped.
> - Verified: server boots via `factorio-display server`, `/health` + `/capabilities` respond.
> - **Not yet done:** webpage (step 4) and OIDC auth (step 5, decoupled & last).

---

## 0. Executive summary

The current CLI is a **single-process, stdout-oriented** tool. To build a solid web API we need three architectural shifts:

1. **Decouple the encode pipeline from `argparse`** so the exact same logic is callable from CLI and HTTP with a typed configuration object (one source of truth).
2. **Return structured results** (blueprint + metadata) instead of printing to stdout, so the API can respond with JSON.
3. **Make long-running work asynchronous** — encode jobs run in a worker pool with persisted state, and clients poll or stream progress.

Everything below is designed so the CLI stays fully functional (it becomes a thin wrapper over a new service layer), and the API is a second, thin wrapper over the same layer.

---

## Part A — CLI improvements (refactor)

### A.1. Introduce a typed service layer (`service.py`)

**Problem:** `_handle_encode`, `_handle_audio_only`, `_handle_audio_encode`, and `_encode_audio_for_composition` all read from an `argparse.Namespace` via `getattr(args, ...)` with inline defaults. A web API cannot pass a Namespace; it needs a typed schema, and the CLI defaults are duplicated in three places.

**Change:** Add `src/factorio_display/service.py` exposing **pure, typed entry points** that return result objects:

```python
@dataclass
class MediaConfig:            # == encode subcommand options
    inputs: list[str]          # file paths (uploaded files for the API)
    name: str = "Media Data"
    # video
    skip: int = 1
    fps: float = 0.0
    adaptive: bool = False
    threshold: float = 0.01
    deduplicate: bool = False
    width: int | None = None
    height: int | None = None
    time_chunks: int = 1
    chunk_workers: int | None = None
    output_chunks_dir: str | None = None
    deduplicate_cross: bool = False
    # audio / midi
    ticks_per_beat: int = 30
    boost_melody: float = 1.0
    velocity_scale: float = 1.0
    attack_ticks: int = 10
    decay_ticks: int = 10
    sustain_level: float = 1.0
    release_ticks: int = 10
    attack_curve: float = 1.0
    decay_curve: float = 1.0
    release_curve: float = 1.0
    rail_mode: str = "auto:0.05"
    map_drums: bool = True
    drum_gain: float = 0.25
    use_global_shift: bool = True
    # audio file (non-MIDI)
    use_basic_pitch: bool = True
    activation_threshold: float = 0.0
    midi_threshold: float = 0.05
    condense_midi: bool = True
    max_polyphony: int = 0
    # composition / output
    audio_only: bool = False
    no_audio: bool = False
    attach_player: bool = True
    power: str | None = "substation"    # small|medium|substation|none
    progress_bar: bool = False
    use_cache: bool = True
    debug_toml_dir: str | None = None
    output_midi_path: str | None = None
    processed_midi_path: str | None = None
    debug_json_path: str | None = None

@dataclass
class MediaResult:
    blueprint: str            # the draftsman string(s)
    name: str
    kind: str                 # "video" | "audio" | "midi" | "image" | "combined"
    dimensions: tuple[int, int] | None
    total_ticks: int
    entity_count: int | None
    instruments: list[str]
    warnings: list[str]
    artifacts: list[str]      # paths to intermediate outputs (chunks, toml, midi…)
```

```python
# service.py entry points
def encode_media(cfg: MediaConfig, *, progress: ProgressReporter | None = None) -> MediaResult
def export_display(width: int | None = None, height: int | None = None,
                   name: str = "Video Display", power: str | None = "substation") -> BuildResult
def export_audio_decoder(instruments: list[str], name: str = "Audio Decoder",
                         power: str | None = "substation", format: str = "blueprint") -> BuildResult
def export_logical(instrument: str, name: str = "Audio Decoder") -> BuildResult   # TOML text
def decode_blueprint(bp_string: str) -> BuildResult                                # YAML text
```

**CLI becomes a thin wrapper:** `cli.main()` parses args → builds `MediaConfig`/etc → calls `service.*` → prints `result.blueprint` (and warnings to stderr). No behavior change.

**Why this is the foundation of the web API:** FastAPI routes call exactly these functions. Pydantic request models mirror the dataclasses (or we switch the dataclasses to Pydantic models and share them — see A.3).

### A.2. `--json` machine-readable output

Add a `--json` flag to the CLI that prints a JSON envelope instead of a raw blueprint:

```jsonc
{
  "version": "0.1.0",
  "result": {
    "blueprint": "0eNqj...",
    "name": "Bad Apple Frame Data",
    "kind": "video",
    "dimensions": [28, 26],
    "total_ticks": 14560,
    "entity_count": 1820,
    "instruments": [],
    "warnings": ["Display is 28x26..."],
    "artifacts": ["chunks/chunk_0000.toml"]
  }
}
```

This makes the CLI scriptable and gives us a golden contract to test the service layer against (the API returns the same shape).

### A.3. Options schema shared between CLI and API

Use **Pydantic models** as the single definition of every option group. The CLI builds them from argparse (or we generate argparse from the model), the API accepts them directly as request bodies. No more `getattr(args, ...)` with scattered defaults.

### A.4. Progress reporting abstraction

**Problem:** tqdm writes to stderr; there is no programmatic way to consume progress, and stderr is not structured.

**Change:** a `ProgressReporter` protocol:

```python
class ProgressReporter(Protocol):
    def phase(self, name: str, total: int | None = None) -> None: ...
    def update(self, n: int = 1) -> None: ...
    def message(self, text: str) -> None: ...
```

- CLI: default reporter wraps tqdm (unchanged UX).
- API: reporter appends events to the job record; the latest progress snapshot is what `GET /jobs/{id}` returns when polled.
- The encoder/composer already emit progress via `tqdm`; we thread the reporter through `service.*` (progress callbacks passed into `encode_auto`, chunk builders, etc.). Where code calls `sys.stderr.write` directly, route through the reporter where feasible; otherwise capture stderr per-job.

### A.5. Isolation and naming for concurrent jobs

- **Cache isolation:** cache keys currently derive from input path + options and live in shared `.factorio_display_cache/`. Concurrent API jobs would collide. Introduce a `job_id` namespace so each job's frame/core/chunk caches live under `.factorio_display_cache/jobs/{job_id}/...`.
- **Temp files:** audio extraction currently uses `tempfile.mkdtemp` and cleans up. In the API, uploaded media lives in a per-job workspace dir (`server_data/jobs/{job_id}/input/...`, `.../output/...`).
- **Deterministic output names:** the API generates a unique job id and uses it in artifact filenames; no global mutable state.

### A.6. Small CLI cleanups that help the API

- Add `factorio-display server` subcommand to launch the web service (`--host`, `--port`, `--data-dir`, `--max-workers`, `--max-jobs-per-user`, `--compress-artifacts`).
- Add `--no-color`/plain progress (progress bars are noisy in server logs).
- Add a `capabilities` JSON dump (`factorio-display capabilities`) listing supported instruments, rail modes, input extensions, formats — served by `GET /api/v1/capabilities`.
- Optional `--api-token` (shared secret) middleware: a trivial interim gate so the core phase is not wide open. It carries **no identity** — the real auth layer (OIDC, final phase) replaces it.

---

## Part B — REST API design

### B.0. Guiding principles

- **Versioned** (`/api/v1`), JSON in/out, OpenAPI docs served at `/docs` (FastAPI default).
- **Async job model** for anything that can take >~2s (encode). Fast builders (`export-display`, `export-audio`, decode) are synchronous.
- **All media is uploaded first** → a job references upload ids or paths. Jobs are self-contained and replayable.
- **Everything is downloadable**: blueprint string, logical TOML, YAML, and raw draftsman JSON.
- **Progress via polling** — clients poll `GET /jobs/{id}`; no SSE/WebSocket.
- **Concurrency is configurable** via CLI/server settings (global + per-user caps).
- **Compression everywhere** — large text payloads (blueprint strings, TOML, YAML, draftsman JSON) are compressed at the HTTP layer and on disk; see B.7.
- **Auth is decoupled and LAST** — the core API is built and works without it. The final phase (B.8) adds an **OAuth 2.0 / OpenID Connect (Google)** identity layer whose **sole goal is DoS protection** (rate limiting + identity gate). Per-user artifact permission is a byproduct, not a design driver.

### B.1. Core resources

```
Upload   — a media file stored by the server (video/audio/midi/image).
Job      — an asynchronous unit of work (encode, build display, …).
Artifact — an output file attached to a job (blueprint text, toml, yaml, chunks…).
```

### B.2. Endpoint map

| Method | Path | Purpose | Async? |
|---|---|---|---|
| `GET` | `/api/v1/health` | Service health, version, uptime, worker status | — |
| `GET` | `/api/v1/capabilities` | Supported instruments, rail modes, formats, extensions, limits | — |
| `POST` | `/api/v1/uploads` | Upload media file(s) (multipart) → `Upload[]` | — |
| `GET` | `/api/v1/uploads/{upload_id}` | Upload metadata (name, size, detected type, probe info) | — |
| `DELETE` | `/api/v1/uploads/{upload_id}` | Delete an upload | — |
| `POST` | `/api/v1/jobs` | Create an encode/build job | — |
| `GET` | `/api/v1/jobs` | List jobs (`?status=`, `?limit=`, `?offset=`) | — |
| `GET` | `/api/v1/jobs/{job_id}` | Job status + metadata + progress (poll here) | — |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | Cancel queued/running job | — |
| `DELETE` | `/api/v1/jobs/{job_id}` | Delete job + its artifacts | — |
| `GET` | `/api/v1/jobs/{job_id}/result` | Final result (blueprint) `?format=blueprint\|toml\|yaml\|json` | waits until done |
| `GET` | `/api/v1/jobs/{job_id}/artifacts` | List intermediate artifacts | — |
| `GET` | `/api/v1/artifacts/{artifact_id}` | Download an artifact file | — |
| `POST` | `/api/v1/blueprints/display` | Sync `export-display` | sync |
| `POST` | `/api/v1/blueprints/audio-decoder` | Sync `export-audio` | sync |
| `POST` | `/api/v1/blueprints/logical` | Sync `export-logical` (TOML) | sync |
| `POST` | `/api/v1/blueprints/decode` | Blueprint string → YAML | sync |
| `GET` | `/api/v1/auth/login` | Redirect to Google OIDC authorize endpoint | — |
| `GET` | `/api/v1/auth/callback` | OIDC callback → exchanges code → sets session | — |
| `POST` | `/api/v1/auth/logout` | Destroy session | — |
| `GET` | `/api/v1/auth/me` | Current authenticated user (for the web UI) | — |

### B.3. Detailed endpoint specs

#### `GET /api/v1/health`
```jsonc
{
  "status": "ok",
  "version": "0.1.0",
  "workers": { "busy": 1, "max": 4, "queued": 2 },
  "uptime_seconds": 12345
}
```

#### `GET /api/v1/capabilities`
```jsonc
{
  "version": "0.1.0",
  "display": { "default_width": 28, "default_height": 26, "max_pixels": 1000 },
  "input_extensions": { "video": [".mp4",".avi",".mov",".mkv",".webm"],
                        "audio": [".wav",".flac",".ogg",".mp3","..."],
                        "midi": [".mid",".midi"],
                        "image": [".png",".jpg",".gif","..."] },
  "instruments": ["piano","bass","celesta","plucked","drum"],
  "rail_modes": ["piano","all","auto[:threshold]","comma,separated"],
  "result_formats": ["blueprint","toml","yaml","json"],
  "power_types": ["small","medium","substation","none"]
}
```

#### `POST /api/v1/uploads` (multipart)
Request: `files: File[]` (+ optional `name` override). Response `201`:
```jsonc
{
  "uploads": [
    {
      "upload_id": "u_8f3a...",
      "name": "bad_apple.mp4",
      "size_bytes": 48210934,
      "media_type": "video",        // video|audio|midi|image|unknown
      "probe": { "width": 640, "height": 480, "fps": 30, "frames": 6572,
                 "duration_seconds": 219 },
      "path": "/server_data/uploads/u_8f3a.../bad_apple.mp4"
    }
  ]
}
```
Errors: unsupported type → `422` with detected type.

#### `POST /api/v1/jobs`
```jsonc
// Body (all fields optional except the operation inputs)
{
  "type": "encode",                // encode | display | audio-decoder | logical | decode
  "name": "Bad Apple",
  "inputs": ["u_8f3a..."],         // upload ids (for encode)
  // video options
  "skip": 1, "fps": 30, "adaptive": true, "threshold": 0.01,
  "deduplicate": false, "width": null, "height": null,
  "time_chunks": 1, "chunk_workers": null,
  // audio / midi options
  "ticks_per_beat": 30, "boost_melody": 1.5, "velocity_scale": 1.0,
  "attack_ticks": 10, "decay_ticks": 10, "sustain_level": 0.8, "release_ticks": 10,
  "rail_mode": "auto:0.05", "map_drums": true, "drum_gain": 0.25,
  "use_basic_pitch": true,
  // composition / output
  "power": "substation", "progress_bar": false,
  "attach_player": true, "audio_only": false, "no_audio": false,
  "result_format": "blueprint",    // blueprint | toml | yaml | json
  "callback_url": null             // optional webhook on completion
}
```
Response `202 Accepted`:
```jsonc
{
  "job_id": "j_1f9c...",
  "status": "queued",
  "type": "encode",
  "created_at": "2026-08-02T10:00:00Z",
  "result_url": "/api/v1/jobs/j_1f9c.../result?format=blueprint"
}
```

#### `GET /api/v1/jobs/{job_id}`
```jsonc
{
  "job_id": "j_1f9c...",
  "type": "encode",
  "name": "Bad Apple",
  "status": "running",             // queued|running|succeeded|failed|cancelled
  "progress": { "phase": "Building chunks", "done": 3, "total": 4, "percent": 75.0 },
  "created_at": "...", "started_at": "...", "finished_at": null,
  "error": null,                   // message when failed
  "result_url": "...",
  "artifacts_url": "..."
}
```

#### `GET /api/v1/jobs/{job_id}/result?format=blueprint`
- `format=blueprint` (default): `text/plain`, the blueprint string.
- `format=toml` / `yaml` / `json`: `text/plain` / `application/json` with the converted output.
- Response is compressed (gzip/br) per B.7.
- While running: `409` + current status. On failure: `422` + error message.

#### `POST /api/v1/blueprints/display` (sync)
```jsonc
{ "name": "Video Display", "width": 28, "height": 28, "power": "substation" }
// 200 → { "blueprint": "0eNqj...", "name": "Video Display", "entity_count": 784 }
```

#### `POST /api/v1/blueprints/audio-decoder` (sync)
```jsonc
{ "name": "Audio Decoder", "instruments": ["piano","bass"], "power": "substation" }
// 200 → { "blueprint": "0eNqj...", "name": "Audio Decoder", "instruments": ["piano","bass"] }
```

### B.4. Error model

All errors are JSON:
```jsonc
{
  "error": {
    "code": "validation_error",     // validation_error|not_found|unsupported_media|
                                    // job_running|job_failed|internal_error
    "message": "Human readable message",
    "details": { ... }              // optional (e.g. pydantic errors, probe info)
  }
}
```
HTTP mapping: `400` validation, `404` not found, `409` job still running, `422` job failed / unsupported media, `500` internal.

### B.5. Job lifecycle & concurrency

```
queued → running → succeeded
           │  │
           │  └──→ failed
           └─────→ cancelled
```

- A bounded **worker pool**; the cap is **configurable** via server settings:
  - `--max-workers` — global concurrency (default = CPU count),
  - `--max-jobs-per-user` — per-user cap (default e.g. 2) to prevent one account from flooding the queue. In the unauthenticated core phase the "user" is the anonymous bucket; per-user caps become meaningful once auth lands in B.8.
- Encode already uses `ProcessPoolExecutor` internally for chunked work; the job runner stays a single OS thread/process per job and lets the encoder manage its own parallelism. Because two encode jobs can both spawn process pools, the global cap should stay conservative on encode-heavy loads — but it is fully tunable.
- State is persisted to `server_data/jobs/{job_id}/job.json` so the server can restart mid-flight and jobs are visible via `GET /jobs`.

### B.6. Storage layout

```
server_data/
  uploads/
    u_8f3a.../bad_apple.mp4
  jobs/
    j_1f9c.../
      job.json                # status, config, progress
      input/                  # copied media (or symlinks to uploads)
      output/
        result.txt            # final blueprint string (or result.toml/.yaml)
        chunk_0000.toml
        processed.mid
      cache/                  # this job's .factorio_display_cache namespace
```

### B.7. Compression

Blueprint strings are already zlib-compressed inside Factorio's string format, but the **JSON envelopes, logical TOML, YAML, and draftsman JSON can be megabytes**. Compression is handled in two layers.

**1) HTTP transport (always on).**
- Response-compression middleware (gzip, and brotli when `brotli` is installed) for `text/*`, `application/json`, and other text payloads.
- Respects `Accept-Encoding` (gzip/br/identity) and sets `Vary: Accept-Encoding`; skips bodies under ~1 KiB and already-compressed media types.
- Browsers/curl send `Accept-Encoding` automatically — no client work needed.
- Large artifact downloads use **pre-compressed files** (see below) served with `Content-Encoding: gzip` + `Content-Length`, avoiding streaming-compression overhead.

**2) Storage (on disk).**
- Blueprint string (`result.txt`): stored as-is (already compressed internally).
- Logical TOML / YAML / draftsman JSON: gzip-compressed on disk when above a threshold (e.g. > 256 KiB) as `result.toml.gz` etc. Decompressed on download; served with correct `Content-Encoding`.
- Uploaded media is never re-compressed (mp4/mp3/png/midi are already compressed formats).
- Toggle: `--compress-artifacts` (default on above threshold).

**Effect on the API contract:** none — compression is transparent. `Content-Encoding` and `Vary: Accept-Encoding` headers are set; clients that don't advertise gzip get plain bodies.

### B.8. Authentication — final phase, decoupled (OAuth 2.0 / OpenID Connect — Google)

**Status: LAST step, fully decoupled.** The core API (uploads, jobs, sync builders, compression) is built and functional without this section. This phase is added at the end and only touches the middleware/dependency layer — no changes to the service layer.

**Goal: prevent DoS.** Require an authenticated identity for uploads and job creation, using a standards-based IdP, plus rate limiting and per-user concurrency caps. **Per-user artifact permission is a byproduct** of having an identity — jobs/uploads are scoped to the session that created them — but it is not the design driver.

- **Flow:** OIDC **Authorization Code + PKCE**, Google as the IdP.
  - `GET /api/v1/auth/login` → builds Google authorize URL (`response_type=code`, `scope=openid email profile`, `code_challenge`) → 302.
  - Google redirects to `GET /api/v1/auth/callback?code=...` → server exchanges code for tokens (verifies PKCE verifier + `id_token` signature against Google's JWKS) → creates a server session → sets `HttpOnly; Secure; SameSite=Lax` session cookie.
- **Session:** opaque random session id stored server-side (`server_data/sessions/{sid}.json`, with expiry). No JWT-to-the-browser needed; the cookie is HttpOnly so the JS frontend never touches tokens. Frontend reads identity via `GET /api/v1/auth/me`.
- **Enforcement:**
  - `POST /uploads`, `POST /jobs`, `POST /jobs/{id}/cancel`, `DELETE /jobs/{id}`, `GET /jobs/{id}/result`, `GET /artifacts/{id}`, `POST /blueprints/*` → **require** a valid session.
  - `GET /health`, `GET /capabilities`, `GET /jobs` (own jobs only), `GET /auth/*` → anonymous OK (limited).
- **Per-user DoS controls:** `--max-jobs-per-user`, plus optional `--rate-limit` (requests/min per session) using `slowapi`/`limits`.
- **Configuration (secrets via env, never committed):**
  - `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
  - `FACTORIO_DISPLAY_BASE_URL` (e.g. `http://localhost:8000`) to build the callback URL,
  - `SESSION_SECRET` (for signing/encrypting stored sessions if needed).
- **Local development fallback:** `--dev-no-auth` starts the server in unauthenticated mode for local testing (warns loudly). Production runs require OIDC.

---

## Part C — Web frontend (roadmap, detailed design after API approval)

Single-page app (vanilla JS or a light framework) served by FastAPI static files, **not** a separate build step. Screens:

1. **Dashboard** — list jobs, live status via polling, cancel/delete, link to results.
2. **Encoder** — upload media, configure options (grouped form mirroring the CLI: video / MIDI+ADSR / audio / output), submit job, watch progress, copy blueprint / download file.
3. **Builder** — quick forms for `export-display` / `export-audio` / `export-logical` / decode.
4. **Blueprint viewer** — paste or load a result; preview decoded stats (entity count, ticks, dims); copy to clipboard.

> The full page design (layout, component tree, interactions, wire-up points) will be drafted as a separate document and reviewed with you **before any code is written**.

---

## Suggested implementation order (after approval)

1. Add `service.py` + Pydantic configs; refactor `cli.py` to use them; add `--json`; add tests. (No behavior change.)
2. Add `api/` package: schemas, jobs runner (worker pool + persisted state), FastAPI app, uploads, sync builders, **compression**, optional `--api-token` interim gate, polling progress.
3. Add `factorio-display server` subcommand + `pyproject.toml` optional `web` extra (`fastapi`, `uvicorn`, `python-multipart`, `brotli`, `httpx`).
4. Design webpage → review → implement.
5. **LAST — auth (decoupled):** OIDC (Google) + rate limiting + per-user caps + artifact ownership. Add `authlib`/`httpx` + `PyJWT`/`python-jose`, `slowapi` to the `web` extra. No changes to steps 1–4 code paths.

## Approved decisions (2026-08-02)

| Decision | Choice |
|---|---|
| Async model | Yes — background job + polling (`GET /jobs/{id}`), sync path for fast builders |
| Auth | **Decoupled & LAST** — OAuth2/OIDC via Google added as the final phase; goal is **DoS protection** only (artifact permission is a byproduct). Core API works without it (optional `--api-token` interim gate). |
| Progress | **Polling only** (no SSE/WebSocket) |
| Result formats | blueprint string + logical TOML + YAML + draftsman JSON |
| Concurrency | **Configurable** (`--max-workers` global, `--max-jobs-per-user` per user) |
| Webhooks | Yes — optional `callback_url` notified on completion |
| Compression | **Transport (gzip/br) + storage (gz for large text artifacts)** — transparent to the API contract |

## Open questions for you

1. ~~Async model~~ *(approved)*
2. ~~Auth~~ *(approved — OAuth2/OIDC via Google as the FINAL decoupled phase; DoS protection only, artifact permission is a byproduct; you'll provision `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` + callback URI when we get there)*
3. ~~Progress streaming~~ *(approved — polling)*
4. ~~Result formats~~ *(approved — all four)*
5. ~~Concurrency~~ *(approved — configurable)*
6. ~~Webhook~~ *(approved — included)*
7. **Is there a deployment target for the server** (localhost only, LAN, or a public host with TLS via a reverse proxy)? This affects callback URL setup and whether we need HTTPS for the OAuth cookie.
