# factorio-display Web App — Design Proposal

**Status:** IMPLEMENTED (v3) — step wizard (Media → Edit → Generate); per-clip downsampled working caches; two-rail drag-edit timeline; live final-result preview; 1s job thumbnails; dev-mode result panel with structured YAML + Pastebin/FBE sharing. See the design sections below.
**Depends on:** the API (implemented) — `src/factorio_display/api/server.py`, endpoints under `/api/v1`, static files mounted at `/`.
**Auth:** not needed for v1. The app works anonymously (or with an optional API-token field when the server runs with `--api-token`). OIDC UI lands with the final auth phase.

---

## 1. Stack (no build step)

- Single-page app served by FastAPI `StaticFiles` at `/` (one small change in `server.py`).
- **Vanilla JS (ES modules) + one hand-written CSS file** — no bundler, no framework, no CDN dependency. (Recommended — see rationale below.)

```
src/factorio_display/api/static/
  index.html
  app.js          # app shell, views, polling, API client, wizard + timeline
  style.css
  compress.js     # lossy video re-encode (canvas + MediaRecorder, main thread)
  editor.js       # per-clip trim/crop/audio offset + concatenation to one file
  mediacache.js   # IndexedDB cache of the media uploaded per job (job preview)
```

**Why vanilla fits this page best:**
- The one heavy lift — client-side video compression/editing — uses DOM APIs (`<video>`, canvas, MediaRecorder, AudioContext) that are main-thread anyway, so a framework adds nothing there.
- The rest is forms, list rendering and polling — plain DOM work.
- No build step / CDN keeps it dependency-free and reviewable, and avoids fragile external assets.
- If you later want React/Vite, the worker + API client stay reusable as-is.

> **Decisions (from feedback):** dark Factorio theme with custom CSS; 3-step wizard; each clip is downsampled into a working cache on add (reused for editing, preview and the final render); two-rail timeline (video/audio) where you drag a block to align, drag its edges to cut, and type precise start/in/out times in an inline inspector; preview of the final result sits above the rails and auto-refreshes on small edits; folded options; empty-state CTA → job list with a large "Generate a new one"; queued jobs surfaced in the list; blueprint viewer is a small helper (modal).

---

## 2. Wizard (3 steps)

1. **Media** — drop/add any number of video/audio/image clips. Each clip is immediately downsampled to a ~320px working cache (`compress.js` for video, PNG re-encode for images; audio kept as-is) and the playlist shows the cache status. The cached blob replaces `clip.file`, so editing, preview and the final render all reuse it.
2. **Edit** — preview (final result) above a two-rail timeline (Video / Audio).
   - All durations, trims and gap positions are snapped to a **1/60 s frame grain**; a picture lasts **at least 1/60 s**.
   - Gaps between clips are allowed (drag a block or type its Pos) and are rendered as **blank (black) frames**.
   - Drag a block body → align (video: ripple; audio: absolute start).
   - Drag the left/right edge handles → cut (trimStart/trimEnd; images adjust duration).
   - Click a block → inline inspector with precise **Pos / In / Out** (or **Dur** for images) inputs.
   - Double-click (or "Edit…") → the full editor modal for crop / offset / mute / resolution preview.
   - "Refresh preview" button, clicking the preview, and (for timelines ≤ 20s) any edit auto-re-render the final result.
3. **Generate** — display size + folded options, then generate → compress → upload → submit → home.

Jobs may be queued when workers are busy; the job list shows `queued` (with a "will start when a worker is free" note) and a submission that hits the per-user cap (429) is reported as "Server busy" and routed to the job list.

## 2b. Jobs list, previews & result sharing (v3)

- **Job thumbnails** — when a job is submitted, a tiny **1-second low-res preview** is generated client-side (`editor.js makePreview` → `app.js makeJobPreview`) and cached in IndexedDB under `preview:<job_id>`:
  - video jobs → a small **muted looping video**;
  - image jobs → a small **thumbnail image**;
  - **sound-only jobs omit the preview** and show a "♪ Sound only — no video preview" note instead.
  The thumbnail **takes the place of the job title**: `.job-head` is a row with the preview on the left and a vertically-centered meta column on the right (status badge · age · job id, then the action buttons), so the card height matches the preview and clicking anywhere (incl. the preview) unfolds the job.
- **Result panel** (expand a succeeded job): tabs **blueprint** / **Inspect item** (structured, collapsible YAML tree viewer). **TOML is removed**, and **JSON (results + `.json` artifacts) is hidden unless Developer mode** is on (a `dev` toggle in the top bar, persisted in `localStorage`).
- **1-click share**: **Share link ↗** calls `POST /api/v1/jobs/{id}/share`; the backend mints a short-lived public token (default 24 h, `share_ttl_hours`) and returns a link. The public `GET /api/v1/share/{token}` serves the raw blueprint with `Access-Control-Allow-Origin: *`, so any origin can fetch it — no third-party paste service or server key. **Open in FBE ↗** opens `https://fbe.teoxoy.com/?source=<share link>` (creating the link first if needed); FBE loads it through its own CORS proxy and renders it in the Factorio Blueprint Editor.

## 2. App shell

Top nav bar with 4 tabs; one screen visible at a time. State is in-memory JS; jobs/upload lists are fetched from the API.

```
┌──────────────────────────────────────────────────────────────┐
│  factorio-display          [Encode] [Builders] [Jobs] [Viewer]│
├──────────────────────────────────────────────────────────────┤
│  (active screen content)                                      │
└──────────────────────────────────────────────────────────────┘
```

- **Encode** — the main workflow (upload → configure → run → result).
- **Builders** — quick sync builders (display / audio-decoder / logical / decode).
- **Jobs** — history + live status (polling) + results.
- **Viewer** — paste a blueprint, decode to YAML, view stats.

## 3. Screen: Encode

### 3.1 Upload box
Drag & drop (or file picker), **multiple** files. On drop → `POST /api/v1/uploads` (multipart) → show uploaded chips (name, size, detected type) with delete buttons (`DELETE /uploads/{id}`). Uploaded ids are captured for the job.

**Video compression toggle** (new): before uploading a video file, optionally re-encode it lossily in the browser to shrink the upload. See §7 below.

### 3.2 Options form
Mirrors the CLI option groups as **collapsible `<details>` sections**. Only "Name" and "Result format" are open by default; everything else collapsed with sensible defaults.

| Section | Fields |
|---|---|
| **Basic** | Blueprint name, Result format (`blueprint`/`toml`/`yaml`/`json`), Power (`substation`/`small`/`medium`/`none`), Attach player, Progress bar |
| **Video** | FPS, frame skip, adaptive, threshold, deduplicate, width, height, time-chunks, chunk-workers, deduplicate-cross |
| **MIDI / ADSR** | ticks-per-beat, boost-melody, velocity-scale, attack/decay/sustain/release ticks + curves, rail-mode, map-drums, drum-gain, global-shift |
| **Audio file** | use-basic-pitch, activation-threshold, midi-threshold, condense, max-polyphony |
| **Advanced** | callback-url, use-cache |

Each field is a labeled control; the form model maps 1:1 to `EncodeOptions` (the API request body) — no CLI-arg translation on the client.

### 3.3 Submit → job flow
1. `POST /api/v1/jobs` `{type:"encode", inputs:[uploadIds], options:{...}}` → `202 {job_id, result_url}`.
2. Navigate to the **Jobs** tab / show an inline job card.
3. Poll `GET /jobs/{id}` every **2 s**; render status badge + phase + last log lines (`progress.log_tail`).
4. On **succeeded** → enable result actions; on **failed** → show `error` + log tail; on **cancelled** → show cancelled.

## 4. Screen: Jobs

- Table/cards: name, type, status badge (queued/running/succeeded/failed/cancelled), created time, error.
- Filter by status; refresh on tab open + while any row is running.
- Row actions: **Cancel** (`POST /jobs/{id}/cancel`) when queued/running; **Delete** (`DELETE /jobs/{id}`); **Open result**.
- Clicking a job expands it: progress phase, log tail (monospace, scrollable), **result tabs** + **artifacts list**.

### Result panel (reused in Encode too)
Tabs: `blueprint | toml | yaml | json` → `GET /jobs/{id}/result?format=...`.
- Blueprint: show full string in a `<textarea readonly>`, **Copy to clipboard** + **Download .txt**.
- TOML/YAML/JSON: show in a read-only pre/code block, Copy + Download.
- Metadata strip: entity count, total ticks, dimensions, instruments (from `result`).
- Artifacts: `GET /jobs/{id}/artifacts` → each artifact row with **Download** (`GET /jobs/{id}/artifacts/{name}`).

## 5. Screen: Builders

Four compact synchronous forms (`POST /blueprints/*`):

| Builder | Fields |
|---|---|
| Display | name, width, height, power |
| Audio decoder | name, instruments (multi-select chips), power, format (blueprint/logical) |
| Logical | name, instrument |
| Decode | paste blueprint string |

Each returns immediately (`BuildOut`); render in the same Result panel (copy/download/stats).

## 6. Screen: Viewer

- Paste a blueprint string → `POST /blueprints/decode` → show YAML + entity count.
- Also lets you paste a raw result and get its stats without running anything.

## 7. Client-side video compression (lossy, in-browser)

**Goal:** shrink large video uploads by re-encoding in the browser before `POST /uploads`.

**Key insight (from feedback):** the backend display is small — inputs are effectively capped at ~255×255 and the encoder downscales the source to fit the display anyway. So the browser should **downscale aggressively** (default cap ≈ 256px longest side); extra source resolution is discarded server-side regardless, so downscaling here is free and yields a huge size cut.

**Implementation — canvas + MediaRecorder (main thread, no demuxer):**
```
video file
  → <video> (object URL)
  → draw each frame to <canvas> at the capped small resolution
  → canvas.captureStream() → MediaRecorder (lossy)
  → WebM blob → POST /uploads
```
- **Codec:** VP9 → VP8 → any `video/webm` (feature-detected). H.264/MP4 is a possible future option (needs a vendored `mp4-muxer`).
- **Controls:** per-video "Compress before upload"; quality presets map to **(max dimension, bitrate)** tuned for small output, e.g. High `256px / ~1.2 Mbps`, Medium `192px / ~700 kbps`, Low `128px / ~400 kbps`; optional explicit max dimension / max FPS.
- **Progress:** compression runs ~realtime on the main thread via async frame callbacks; shows original vs. compressed size and a **progress bar + Cancel**. (WebCodecs-in-a-Worker is a later optimization; canvas+MediaRecorder needs no demuxer and works everywhere.)
- **Fallback:** if `captureStream`/`MediaRecorder` is unavailable or encoding fails, upload the original and show a notice. Compression is **off by default**, auto-suggested for large videos (>~50 MB).
- **Server impact:** none — the server receives the compressed file exactly like any other upload.

## 8. API wire-up (single table)

| Screen | Endpoint(s) |
|---|---|
| Encode upload | `POST /uploads`, `DELETE /uploads/{id}` |
| Encode submit | `POST /jobs` |
| Jobs list/poll | `GET /jobs`, `GET /jobs/{id}` |
| Job control | `POST /jobs/{id}/cancel`, `DELETE /jobs/{id}` |
| Result | `GET /jobs/{id}/result?format=` |
| Artifacts | `GET /jobs/{id}/artifacts`, `GET /jobs/{id}/artifacts/{name}` |
| Builders / viewer | `POST /blueprints/display|audio-decoder|logical|decode` |
| Boot metadata | `GET /health`, `GET /capabilities` (populate dropdowns: instruments, formats, power) |

## 9. Behavior details

- **Polling:** a single `setInterval` while any job is running; stops when none are. On visibility change (`document.visibilitychange`) resume to stay fresh.
- **Errors:** map HTTP status → friendly message from the `{error:{code,message}}` envelope; toast notifications.
- **Token (optional):** a small "API token" field in the nav (persisted to `localStorage`, sent as `X-API-Token`). Only shown/needed when the server is started with `--api-token`. Default off → anonymous.
- **Formatting:** timestamps as relative ("2m ago"), entity counts with thousands separators.
- **Theme:** dark, Factorio-adjacent palette (near-black bg, amber/green accents), system font stack + monospace for logs/blueprints.

## 10. Resolved decisions & remaining question

**Resolved (from feedback):** full options form in the first cut · dark Factorio theme with custom CSS · Jobs tab + inline job card after submit · vanilla JS + Web Worker stack (recommendation above).

**Remaining decision:** video compression defaults — **VP9 → WebM** as the default codec (recommended, no muxer dependency) vs **H.264 → MP4** (smaller files, needs a vendored `mp4-muxer`). Everything else is settled and I can start implementing on your go-ahead.
