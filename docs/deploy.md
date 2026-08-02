# Deployment Guide

How to run the factorio-display web app — locally and on the public server.

## Architecture

```
┌──────────────────────────────┐        ┌─────────────────────────────────────┐
│  Frontend (static, no build) │        │  Backend (FastAPI + uvicorn)        │
│                              │  HTTPS │                                     │
│  • GitHub Pages:             │ ─────► │  • 101.35.244.49:60012 (public,     │
│    https://StarlightIbuki.   │   CORS │    TLS by Caddy, cert from acme.sh)  │
│    github.io/factorio-       │        │  • 127.0.0.1:8000 (uvicorn, local)  │
│    displayer/                │        │  • systemd: factorio-display        │
│  • or served by FastAPI at / │        └─────────────────────────────────────┘
└──────────────────────────────┘
```

- The frontend is plain static files (vanilla JS ES modules). Hosting it on
  GitHub Pages keeps the server CPU/RAM free for encoding jobs.
- The backend is HTTPS on a **high port (60012)** — port 80/443 stay closed,
  which avoids exposing a "website" (and the associated registration
  requirements). The TLS certificate is obtained via the **DNS-01** challenge
  (domain DNS on Aliyun/alidns) so no inbound HTTP validation is needed.
- The browser only allows an HTTPS page to call an HTTPS API (mixed-content
  rule), so the remote backend **must** be HTTPS for the GitHub Pages frontend.

## Frontend backend resolution

`src/factorio_display/api/static/api-config.js` decides which backend to call:

1. **Explicit override** — `localStorage.fd_api_base` (set in the UI: About ▸
   Backend API). Wins over everything.
2. **Local backend** — if the page is served by the FastAPI process, requests
   go to the same origin (relative `/api/v1/...`).
3. **Remote fallback** — `DEFAULT_REMOTE_BASE` (`https://factorio.qvq.moe:60012`).
   Used when there is no local backend (e.g. on GitHub Pages). The API client
   also retries the remote once if a same-origin request fails at the network
   level.

To point at a different backend at runtime, open **About ▸ Backend API**, enter
a URL, and Save (cleared with "Use auto").

## Local deployment

Requirements: Python ≥ 3.11, a venv.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e '.[web,audio]'     # dev install; audio extra enables MIDI/WAV
```

### Run the backend (serves the frontend too)

```powershell
.\.venv\Scripts\python.exe -m factorio_display server --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/> — the frontend auto-detects the same-origin
backend (no config needed). Add auth if you like:

```powershell
# 1) generate a token for a user
$tok = .\.venv\Scripts\python.exe -m factorio_display token issue --key my-secret --user alice
# 2) run the server with the same key
.\.venv\Scripts\python.exe -m factorio_display server --host 127.0.0.1 --port 8000 --token-key my-secret
# 3) paste $tok into the "API token" box in the UI (or send X-API-Token / Bearer)
```

### Run the frontend standalone (against any backend)

Serve `src/factorio_display/api/static/` with any static server, then set the
backend in **About ▸ Backend API** (or rely on the remote fallback).

```powershell
cd src\factorio_display\api\static
python -m http.server 5173
# open http://127.0.0.1:5173/ → it will use the remote backend by default
```

## Remote deployment (this project)

### 1. Build & ship the backend

```powershell
python -m pip wheel . -w dist --no-deps
scp dist\factorio_display-0.1.0-py3-none-any.whl tc:/opt/factorio-display/
scp deploy\setup_server.sh deploy\issue_cert.sh tc:/tmp/
ssh tc "sudo bash /tmp/setup_server.sh"
```

`setup_server.sh`:
- creates `/opt/factorio-display` + venv, installs the wheel (`[web,audio]`),
- generates & stores the **token key** at `/etc/factorio-display/env`,
- writes `/etc/caddy/Caddyfile` (port 60012, manual TLS cert),
- installs/enables the `factorio-display` systemd unit.

> On a mainland-China host, if PyPI is slow, add a mirror, e.g.
> `--index-url https://mirrors.cloud.tencent.com/pypi/simple`.

### 2. Open the port

In the Tencent Cloud console → security group → add inbound **TCP 60012**
(source `0.0.0.0/0`, or your user ranges). Leave 80/443 closed.

### 3. Issue the TLS certificate (DNS-01 via alidns)

The domain DNS stays on Aliyun; we only use its API to create the
`_acme-challenge` TXT record (verification only — the cert is from
Let's Encrypt). On the server, type your own keys:

```bash
export Ali_Key='<Aliyun AccessKey ID>'
export Ali_Secret='<Aliyun AccessKey Secret>'
sudo bash /tmp/issue_cert.sh
```

The script runs acme.sh (auto-renewing via cron) and installs the cert to
`/etc/factorio-display/ssl/`, then reloads Caddy.

```bash
sudo systemctl enable --now caddy     # start Caddy now that a cert exists
curl -s https://factorio.qvq.moe:60012/api/v1/health
```

### 4. Deploy the frontend to GitHub Pages

```bash
bash deploy/deploy_ghpages.sh
```

Publishes `src/factorio_display/api/static/` to the `gh-pages` branch and
enables Pages (URL: `https://StarlightIbuki.github.io/factorio-displayer/`).

### 5. Issue tokens for users

The server runs with `--token-key <key>` (stored at `/etc/factorio-display/env`).
On any machine with the package installed:

```powershell
.\.venv\Scripts\python.exe -m factorio_display token issue --key <token-key> --user alice --ttl-hours 720
```

Give the token to the user; they paste it into the "API token" box (or send
`Authorization: Bearer <token>` / `X-API-Token: <token>`). Each `--user` maps
to an isolated principal, so users cannot see each other's jobs/uploads.

## CORS

The backend allows these origins by default (`Settings.cors_allow_origins` /
`cors_allow_origin_regex`):

- `https://StarlightIbuki.github.io` and any `https://*.github.io`
- localhost dev origins (`http://localhost:5173`, `:8000`, `127.0.0.1:…`)

Override with `--cors-origins a,b --cors-origin-regex <re>` or the env vars
`CORS_ALLOW_ORIGINS` / `CORS_ALLOW_ORIGIN_REGEX`.

## GitHub login (OAuth)

The backend runs the OAuth flow (client secret stays server-side), issues one
of its own signed tokens with `sub = "github:<login>"`, and redirects the
browser back to the SPA, which stores the token and shows the signed-in user.

### 1. Create a GitHub OAuth App

At <https://github.com/settings/developers> → **New OAuth App**:

- **Application name**: `factorio-display`
- **Homepage URL**: `https://StarlightIbuki.github.io/factorio-displayer/`
- **Authorization callback URL**: `https://factorio.qvq.moe:60012/auth/github/callback`

Save the **Client ID** and **Client Secret**.

### 2. Configure the server

Add to `/etc/factorio-display/env` (the systemd unit loads this file):

```bash
GITHUB_OAUTH_CLIENT_ID=<client id>
GITHUB_OAUTH_CLIENT_SECRET=<client secret>
GITHUB_OAUTH_REDIRECT_URI=https://factorio.qvq.moe:60012/auth/github/callback
FRONTEND_URL=https://StarlightIbuki.github.io/factorio-displayer/
```

```bash
sudo systemctl restart factorio-display
```

The frontend fetches `/api/v1/capabilities` and shows the **Login with GitHub**
button only when the backend advertises `auth.github`. The Client Secret never
appears in capabilities or in the browser.

> The same flags work as CLI options: `--github-client-id`,
> `--github-client-secret`, `--github-redirect-uri`, `--frontend-url`.

## Updating the backend

```powershell
python -m pip wheel . -w dist --no-deps
scp dist\factorio_display-0.1.0-py3-none-any.whl tc:/opt/factorio-display/
ssh tc "cd /opt/factorio-display && ./venv/bin/pip install --force-reinstall --no-deps ./factorio_display-0.1.0-py3-none-any.whl && sudo systemctl restart factorio-display"
```
