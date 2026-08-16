// api-config.js — configurable backend endpoint for the web app.
//
// Resolution order (first match wins):
//   1. localStorage "fd_api_base"   — explicit user config (set in About ▸ Backend)
//   2. same-origin                  — page served by the FastAPI process (local backend)
//   3. DEFAULT_REMOTE_BASE          — the public backend (see below)
//
// "Default to local, fall back to remote": when the page is served by
// FastAPI we use the local backend (relative URLs).  When it is served from
// GitHub Pages (no /api/v1 on the same origin) we automatically fall back to
// DEFAULT_REMOTE_BASE.  The runtime API client additionally retries once with
// the remote base if a same-origin request fails at the network level.
//
// DEFAULT_REMOTE_BASE: the public backend.  HTTPS on a high port (no public
// 80/443) so the GitHub Pages frontend can call it without mixed-content
// blocks.  Change this when the server moves.
export const DEFAULT_REMOTE_BASE = "https://factorio.qvq.moe:60012";

const KEY = "fd_api_base";
let resolved = null; // "" (same-origin) or an absolute base URL, cached

/** Explicitly configured base URL ("" when unset). */
export function configuredApiBase() {
  return (localStorage.getItem(KEY) || "").trim().replace(/\/+$/, "");
}

/** Set (or clear, with "") the explicit backend base URL. */
export function setConfiguredApiBase(value) {
  const v = (value || "").trim().replace(/\/+$/, "");
  if (v) localStorage.setItem(KEY, v);
  else localStorage.removeItem(KEY);
  resolved = null; // force re-resolution on next call
}

/** The fallback backend base URL (used when no local backend is reachable). */
export function remoteFallbackBase() {
  return DEFAULT_REMOTE_BASE;
}

/** Resolve the effective backend base ("" = same-origin), cached after first call. */
export async function resolveApiBase() {
  if (resolved !== null) return resolved;
  const cfg = configuredApiBase();
  if (cfg) {
    resolved = cfg;
    return resolved;
  }
  // Probe the same origin: only the FastAPI backend answers /api/v1/health.
  try {
    const r = await fetch("/api/v1/health", { method: "GET", cache: "no-store" });
    if (r.ok) {
      resolved = "";
      return resolved;
    }
  } catch (_) { /* not reachable → fall through to remote */ }
  resolved = DEFAULT_REMOTE_BASE;
  return resolved;
}

/** Full URL for a backend path (e.g. for <a href> download links). */
export async function apiUrl(path) {
  const base = await resolveApiBase();
  return base + path;
}

/** The base currently in use after resolution ("" = same-origin/local). */
export async function currentApiBase() {
  return resolveApiBase();
}
