// factorio-display web app — main application logic (vanilla ES module).
// Consumes the /api/v1 API served by the same FastAPI process.

/* eslint-env browser */

import { compressVideo } from "./compress.js";
import { attachCropBox, exportEditedImage, exportConcatenated, makePreview, FRAME, snapFrame } from "./editor.js";
import { saveMedia, getMedia, deleteMedia } from "./mediacache.js";
import { currentLocale, setLocale, t, applyStaticI18n } from "./i18n.js";
import { DEFAULT_REMOTE_BASE, apiUrl, configuredApiBase, currentApiBase, resolveApiBase, setConfiguredApiBase } from "./api-config.js";
import { openBlueprintInspector } from "./inspect.js";

// ── tiny helpers ───────────────────────────────────────────────────────
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    node.append(c.nodeType ? c : document.createTextNode(c));
  }
  return node;
}

function fmtBytes(n) {
  if (n == null) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
}

function fmtRel(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.floor(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function shortName(n) { return n && n.length > 22 ? n.slice(0, 20) + "…" : n; }

function toast(msg, kind = "info", dur = 6000) {
  const box = $("#toasts");
  const cls = kind === "error" ? " error" : kind === "warning" ? " warning" : "";
  const t = el("div", { class: `toast${cls}`, text: msg });
  box.append(t);
  setTimeout(() => t.remove(), dur);
}

function isVideoFile(name) { return /\.(mp4|avi|mov|mkv|webm)$/i.test(name); }
function isVisualFile(name) { return isVideoFile(name) || /\.(png|jpg|jpeg|bmp|tiff|tif|gif)$/i.test(name); }
function mediaKind(name) {
  if (isVideoFile(name)) return "video";
  if (/\.(wav|flac|ogg|mp3|m4a|aac|wma|aiff|aif|au|caf)$/i.test(name)) return "audio";
  if (/\.(mid|midi)$/i.test(name)) return "midi";
  if (isVisualFile(name)) return "image";
  return "other";
}

// Audio-ish clips (audio files + MIDI) ride the audio rail and are uploaded
// as separate inputs so they can be updated independently for a task.
function isSoundKind(k) { return k === "audio" || k === "midi"; }

// ── API client ─────────────────────────────────────────────────────────
const state = {
  token: localStorage.getItem("fd_token") || "",
  anonToken: "",            // server-signed default token for anonymous users (not persisted)
  anonSupported: false,     // backend advertises anonymous use (auth.anonymous_limits/token)
  clips: [],        // [{ id, file, name, size, kind, edit, cacheStatus }]
  editorClipId: null,
  running: new Set(),
  expanded: new Set(),
  pollTimer: null,
  jobsFilter: "",          // active status tab ("" = All)
  jobCache: new Map(),     // job_id -> signature of the last rendered record
  jobEls: new Map(),       // job_id -> current card element (in-place updates)
  previewUrl: null,
  previewRendered: false,
};

// Cached 1s low-res preview node per job (job_id -> { node }).
const jobPreviewCache = new Map();
// Cached result texts (key `${job_id}:${format}`) so re-rendering the job list
// (e.g. every 2s poll while other jobs run) doesn't re-fetch the large
// blueprint from the backend on each refresh.
const resultTextCache = new Map();

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  // A signed-in user's token wins; otherwise fall back to the server's default
  // anonymous token (if the backend provides one) so anonymous jobs are
  // attributed to the shared "anonymous" bucket.
  const token = state.token || state.anonToken;
  if (token) headers["X-API-Token"] = token;
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  let base = await resolveApiBase();
  // "Default to local, fall back to remote": when the effective base is the
  // same origin, retry once against the remote backend if the request fails
  // at the network level.  HTTP error responses (4xx/5xx) are NOT retried.
  const attempts = base === "" ? [base, DEFAULT_REMOTE_BASE] : [base];
  for (let i = 0; i < attempts.length; i++) {
    let res;
    try {
      res = await fetch(attempts[i] + path, { ...options, headers });
    } catch (_e) {
      if (i < attempts.length - 1) continue; // network failure → try next base
      throw new Error("Cannot reach the server");
    }
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try {
        const d = await res.json();
        msg = (d && d.detail && d.detail.error && d.detail.error.message)
          || (d && d.detail && d.detail.message)
          || (d && d.error && d.error.message)
          || msg;
      } catch (_) { /* ignore */ }
      const err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    if (res.status === 204) return null;
    return res;
  }
  throw new Error("Cannot reach the server");
}

// ── views ──────────────────────────────────────────────────────────────
// Start a fresh blueprint (clears the in-progress wizard state).
function resetCreate() {
  state.clips = [];
  state.previewUrl = null;
  state.previewRendered = false;
  selectedClipId = null;
  clipInfo.clear();
  const insp = $("#clip-inspector");
  if (insp) insp.classList.add("hidden");
  renderPlaylist();
  renderInspector();
  $("#btn-next-media").disabled = true;
  $("#btn-generate").disabled = true;
}

function showView(name) {
  $("#view-home").classList.toggle("hidden", name !== "home");
  $("#view-create").classList.toggle("hidden", name !== "create");
  if (name === "home") { renderHome(); if (state.token) startPolling(); }
  else { showStep(1); stopPolling(); }
}

$("#btn-first").addEventListener("click", () => { resetCreate(); showView("create"); });
$("#btn-new").addEventListener("click", () => { resetCreate(); showView("create"); });
$("#btn-back").addEventListener("click", () => { resetCreate(); showView("home"); });
// Blueprint viewer utility → the unified inspector modal (paste mode).
$("#btn-tools").addEventListener("click", () =>
  openBlueprintInspector({}));

// token
const tokenInput = $("#token-input");
tokenInput.value = state.token;
tokenInput.addEventListener("change", () => {
  state.token = tokenInput.value.trim();
  if (state.token) localStorage.setItem("fd_token", state.token);
  else localStorage.removeItem("fd_token");
  renderAuth();
});

// ── GitHub login ──────────────────────────────────────────────────────
let githubAuth = null; // { client_id, redirect_uri, frontend_url } | null
let oauthPending = false; // true while an OAuth popup is up
const $login = $("#btn-login");
const $authUser = $("#auth-user");

// Persist an access token (used by the API client + the token input).
function setToken(t) {
  state.token = t;
  if (t) localStorage.setItem("fd_token", t);
  else localStorage.removeItem("fd_token");
  if (tokenInput) tokenInput.value = t;
}

// Decode the current token's subject claim (e.g. "github:octocat" or "anonymous").
function tokenSubject() {
  if (!state.token) return null;
  try {
    const b64 = state.token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const payload = JSON.parse(atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4)));
    return String(payload.sub || "");
  } catch (_) { return null; }
}

// Decode the current token's subject (e.g. "github:octocat" → "octocat").
function currentUser() {
  const sub = tokenSubject();
  return sub && sub.startsWith("github:") ? sub.slice(7) : null;
}

// Is the caller effectively anonymous (the shared public bucket)?
function isAnonymous() {
  const sub = tokenSubject();
  // No token → server treats the caller as anonymous.  A token with an empty
  // or "anonymous" subject is the default anonymous token.
  return !sub || sub === "" || sub === "anonymous";
}

// Show/hide the "not signed in → data is public" warning.
function renderAnonWarning() {
  const el = $("#anon-warning");
  if (el) el.classList.toggle("hidden", !isAnonymous());
}

function renderAuth() {
  const user = currentUser();
  if (user) {
    if ($authUser) { $authUser.textContent = "👤 " + user; $authUser.classList.remove("hidden"); }
    if ($login) $login.classList.add("hidden");
  } else if (githubAuth) {
    if ($authUser) $authUser.classList.add("hidden");
    if ($login) $login.classList.remove("hidden");
  } else {
    if ($authUser) $authUser.classList.add("hidden");
    if ($login) $login.classList.add("hidden");
  }
  renderAnonWarning();
}

async function initAuth() {
  // OAuth return — the backend redirected here with ?fd_token=…&state=….
  // When login ran in a popup this code runs in the popup (window.opener set);
  // after a full-page redirect it runs in this window.
  const params = new URLSearchParams(location.search);
  const tok = params.get("fd_token");
  const st = params.get("state");
  if (tok) {
    const expected = localStorage.getItem("fd_oauth_state");
    if (st && expected && st === expected) {
      setToken(tok);
      if (!window.opener) toast(t("auth.signedIn", { user: currentUser() || "" }));
    } else if (params.get("error")) {
      toast(t("auth.oauthError", { msg: params.get("error") }), "error");
    } else {
      toast(t("auth.stateMismatch"), "error");
    }
    localStorage.removeItem("fd_oauth_state");
    history.replaceState(null, "", location.pathname + location.hash);
    if (window.opener) {
      // We're the OAuth popup: the opener already got the token via the
      // storage event. Close quietly.
      setTimeout(() => window.close(), 400);
      return;
    }
  }
  // Does this backend support GitHub login?  (public info from /capabilities)
  try {
    const res = await api("/api/v1/capabilities");
    const authInfo = (await res.json()).auth || {};
    githubAuth = authInfo.github || null;
    // Default anonymous token (if the backend is token-gated): the client
    // sends this when the user isn't signed in so anonymous jobs land in the
    // shared "anonymous" bucket.  Never persisted (it can expire server-side).
    state.anonToken = authInfo.anonymous_token || "";
    // Only offer "continue as guest" when the backend actually supports
    // anonymous use — the new backend always advertises anonymous_limits
    // (and anonymous_token when token-gated).  Old backends that require a
    // token don't, so we must not present a guest path that would 401.
    state.anonSupported = !!(authInfo.anonymous_token || authInfo.anonymous_limits);
  } catch (_) { githubAuth = null; }
  renderAuth();
}

// Start GitHub OAuth in a POPUP so the in-progress wizard (clips, edits)
// survives the round-trip — you can log in mid-flow without losing work.
// On success the popup stores the token in localStorage (same origin) and
// closes; this window picks it up via the `storage` event below.
function startOAuth() {
  if (!githubAuth) return;
  const state = (crypto.randomUUID && crypto.randomUUID()) || Math.random().toString(36).slice(2);
  // localStorage (not sessionStorage): the popup is a separate window with its
  // own sessionStorage, but shares localStorage with this origin.
  localStorage.setItem("fd_oauth_state", state);
  const url = new URL("https://github.com/login/oauth/authorize");
  url.searchParams.set("client_id", githubAuth.client_id);
  url.searchParams.set("redirect_uri", githubAuth.redirect_uri);
  url.searchParams.set("state", state);
  url.searchParams.set("scope", "read:user");
  oauthPending = true;
  const w = window.open(url.toString(), "github-oauth", "width=560,height=720,popup=yes");
  if (!w) {
    // Popup blocked → full-page redirect (wizard state would be lost, but
    // login still works).
    location.href = url.toString();
  }
}

// The OAuth popup writes fd_token to localStorage → storage event here.
window.addEventListener("storage", (e) => {
  if (e.key === "fd_token") {
    state.token = localStorage.getItem("fd_token") || "";
    renderAuth();
    if (oauthPending && state.token) {
      oauthPending = false;
      closeLoginModal();
      toast(t("auth.signedIn", { user: currentUser() || "" }));
      renderHome(); // the signed-in user's job list may differ
      startPolling();
    }
  }
});

if ($login) $login.addEventListener("click", startOAuth);
if ($authUser) {
  $authUser.addEventListener("click", () => {
    if (confirm(t("auth.signOutConfirm"))) {
      setToken("");
      stopPolling();
      renderAuth();
      renderHome();
    }
  });
}

// ── login prompt (shown when creating a job without being signed in) ──
// When generate() opens the modal, dismissing it ("continue as guest") runs
// this callback so the wizard proceeds as an anonymous user.
let loginProceed = null;
function openLoginModal() {
  const modal = $("#login-modal");
  if (!modal) return;
  modal.classList.remove("hidden");
  const btn = $("#login-modal-btn");
  const hint = $("#login-modal-dev");
  if (btn) btn.classList.toggle("hidden", !githubAuth);
  if (hint) {
    hint.classList.toggle("hidden", !!githubAuth);
    hint.textContent = t("auth.noGithub");
  }
  // "Continue as guest" when we were interrupted mid-generate, else "Cancel".
  const dismiss = $("#login-modal-dismiss");
  if (dismiss) dismiss.textContent = loginProceed ? t("auth.continueGuest") : t("auth.cancel");
}
function closeLoginModal() {
  const modal = $("#login-modal");
  if (modal) modal.classList.add("hidden");
}
const $loginModal = $("#login-modal");
if ($loginModal) {
  $loginModal.addEventListener("click", (e) => {
    if (e.target === e.currentTarget) closeLoginModal();
  });
}
const $loginModalBtn = $("#login-modal-btn");
if ($loginModalBtn) $loginModalBtn.addEventListener("click", () => {
  loginProceed = null;
  closeLoginModal();
  startOAuth();
});
const $loginModalClose = $("#login-modal-close");
if ($loginModalClose) $loginModalClose.addEventListener("click", closeLoginModal);
const $loginModalDismiss = $("#login-modal-dismiss");
if ($loginModalDismiss) $loginModalDismiss.addEventListener("click", () => {
  closeLoginModal();
  const proceed = loginProceed;
  loginProceed = null;
  if (proceed) proceed();
});

// ── developer mode ────────────────────────────────────────────────────
// Hides raw JSON (results/artifacts) from non-developers; removes TOML.
function isDev() { return localStorage.getItem("fd_dev") === "1"; }
function setDev(on) { if (on) localStorage.setItem("fd_dev", "1"); else localStorage.removeItem("fd_dev"); }

function renderDevUI() {
  // Manual token entry is a developer affordance — hide it for normal users.
  if (tokenInput) tokenInput.classList.toggle("hidden", !isDev());
}

const devToggle = $("#dev-toggle");
if (devToggle) {
  devToggle.checked = isDev();
  devToggle.addEventListener("change", () => {
    setDev(devToggle.checked);
    renderFormatSelectOptions();
    renderHome();
    renderDevUI();
  });
}
renderDevUI();

function renderFormatSelectOptions() {
  const sel = $("#opt-result-format");
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = "";
  sel.append(el("option", { value: "blueprint", text: t("s3.formatBlueprint") }));
  sel.append(el("option", { value: "yaml", text: t("s3.formatInspect") }));
  if (isDev()) sel.append(el("option", { value: "json", text: t("s3.formatJson") }));
  sel.value = cur === "yaml" ? "yaml" : (cur === "json" && isDev() ? "json" : "blueprint");
}

// ── wizard ──────────────────────────────────────────────────────────────
const STEP_PANES = { 1: "#wstep-media", 2: "#wstep-timeline", 3: "#wstep-generate" };
function showStep(n) {
  $$(".wstep").forEach((s) => s.classList.toggle("active", Number(s.dataset.step) === n));
  for (const [k, sel] of Object.entries(STEP_PANES)) $(sel).classList.toggle("hidden", Number(k) !== n);
  if (n === 2) {
    renderTimeline();
    if (!state.previewRendered && state.clips.length) refreshPreview();
  }
  if (n === 3) refreshRecommendation();
}
$("#btn-next-media").addEventListener("click", () => {
  if (state.clips.length) showStep(2);
  else toast(t("t.addClipFirst"), "error");
});
$("#btn-prev-timeline").addEventListener("click", () => showStep(1));
$("#btn-next-timeline").addEventListener("click", () => showStep(3));
$("#btn-prev-generate").addEventListener("click", () => showStep(2));

// ── timeline (step 2) ──────────────────────────────────────────────────
const clipInfo = new Map();   // clipId -> { dur, w, h } (probed)
let selectedClipId = null;
// Cached timeline pixel scale.  Re-renders that only change clip positions
// (dragging) reuse it, so a dragged block rests exactly under the mouse
// instead of the whole timeline auto-rescaling and jumping on release.  The
// scale is recomputed whenever the clip set or their durations change
// (add/remove/trim/probe).
let tlScaleSig = "";
let tlScalePx = 0;

function probeClip(c) {
  return new Promise((resolve) => {
    // MIDI can't be decoded by <audio>; give it a nominal duration — the
    // real length is determined server-side when the raw .mid is encoded.
    if (c.kind === "midi") { resolve({ dur: 30, w: 0, h: 0 }); return; }
    if (c.kind === "image") {
      const img = new Image();
      const url = URL.createObjectURL(c.file);
      img.onload = () => { URL.revokeObjectURL(url); resolve({ dur: 0, w: img.naturalWidth, h: img.naturalHeight }); };
      img.onerror = () => { URL.revokeObjectURL(url); resolve({ dur: 0 }); };
      img.src = url;
      return;
    }
    const el = document.createElement(isSoundKind(c.kind) ? "audio" : "video");
    el.preload = "metadata";
    const url = URL.createObjectURL(c.file);
    el.onloadedmetadata = () => { URL.revokeObjectURL(url); resolve({ dur: el.duration || 0, w: el.videoWidth || 0, h: el.videoHeight || 0 }); };
    el.onerror = () => { URL.revokeObjectURL(url); resolve({ dur: 0 }); };
    el.src = url;
  });
}

function editedDurOf(c) {
  const info = clipInfo.get(c.id);
  if (c.kind === "image") return snapFrame(Math.max(FRAME, c.edit.duration || 1));
  if (!info || !info.dur) return FRAME;
  const start = Math.max(0, c.edit.trimStart || 0);
  const end = c.edit.trimEnd > 0 && c.edit.trimEnd <= info.dur ? c.edit.trimEnd : info.dur;
  return snapFrame(Math.max(FRAME, end - start));
}

// Displayed on-rail position for a clip (ripple for video, absolute for audio).
function currentPos(c) {
  const d = editedDurOf(c);
  if (isSoundKind(c.kind)) return { start: snapFrame(Math.max(0, c.edit.start || 0)), dur: d };
  let cursor = 0;
  for (const x of state.clips) {
    if (isSoundKind(x.kind)) continue;
    const es = x.edit.start != null && x.edit.start >= 0 ? x.edit.start : null;
    const s = es != null ? snapFrame(Math.max(es, cursor)) : cursor;
    if (x.id === c.id) return { start: s, dur: d };
    cursor = s + editedDurOf(x);
  }
  return { start: 0, dur: d };
}

function prevVisualEnd(id) {
  let end = 0;
  for (const c of state.clips) {
    if (isSoundKind(c.kind)) continue;
    if (c.id === id) return end;
    end += editedDurOf(c);
  }
  return end;
}

function currentTotalDur() {
  let end = 0;
  let audioEnd = 0;
  for (const c of state.clips) {
    const p = currentPos(c);
    if (isSoundKind(c.kind)) audioEnd = Math.max(audioEnd, p.start + p.dur);
    else end = Math.max(end, p.start + p.dur);
  }
  return Math.max(end, audioEnd, 1);
}

async function renderTimeline() {
  const railV = $("#rail-video");
  const railA = $("#rail-audio");
  railV.innerHTML = "";
  railA.innerHTML = "";
  $("#timeline-scale").innerHTML = "";
  if (!state.clips.length) { renderInspector(); return; }

  const toProbe = state.clips.filter((c) => !clipInfo.has(c.id));
  const infos = await Promise.all(toProbe.map((c) => probeClip(c)));
  infos.forEach((info, i) => clipInfo.set(toProbe[i].id, info));

  const positions = {};
  let cursor = 0;
  for (const c of state.clips) {
    if (isSoundKind(c.kind)) continue;
    const d = editedDurOf(c);
    const es = c.edit.start != null && c.edit.start >= 0 ? c.edit.start : null;
    const s = es != null ? snapFrame(Math.max(es, cursor)) : cursor;
    positions[c.id] = { start: s, dur: d };
    cursor = s + d;
  }
  let audioEnd = 0;
  for (const c of state.clips) {
    if (!isSoundKind(c.kind)) continue;
    const d = editedDurOf(c);
    const s = snapFrame(Math.max(0, c.edit.start || 0));
    positions[c.id] = { start: s, dur: d };
    audioEnd = Math.max(audioEnd, s + d);
  }
  const tlDur = Math.max(cursor, audioEnd, 1);
  const sig = state.clips.map((c) => `${c.id}:${c.kind}:${editedDurOf(c)}`).join("|");
  let pxPerSec;
  if (tlScaleSig === sig && tlScalePx > 0) {
    pxPerSec = tlScalePx; // only positions changed → keep the scale
  } else {
    pxPerSec = Math.max(20, 900 / tlDur);
    tlScaleSig = sig;
    tlScalePx = pxPerSec;
  }

  for (const c of state.clips) {
    const p = positions[c.id];
    if (!p) continue;
    const rail = isSoundKind(c.kind) ? railA : railV;
    const block = el("div", {
      class: "tl-block " + (isSoundKind(c.kind) ? "audio" : "video") + (selectedClipId === c.id ? " selected" : ""),
      dataset: { id: c.id },
      style: `left:${p.start * pxPerSec}px;width:${Math.max(26, p.dur * pxPerSec)}px`,
      title: `${c.name}\nPos ${p.start.toFixed(1)}s · ${p.dur.toFixed(1)}s`,
    });
    block.append(el("span", { class: "tl-name", text: `${shortName(c.name)} · ${p.dur.toFixed(1)}s` }));
    block.append(el("div", { class: "tl-handle left" }));
    block.append(el("div", { class: "tl-handle right" }));
    rail.append(block);
    wireBlock(block, c, pxPerSec);
  }
  for (let t = 0; t <= Math.ceil(tlDur); t += 5) {
    $("#timeline-scale").append(el("span", { class: "tl-tick", style: `left:${t * pxPerSec}px`, text: `${t}s` }));
  }
  renderInspector();
}

function wireBlock(block, c, pxPerSec) {
  block.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".tl-handle")) return;
    e.preventDefault();
    selectClip(c.id);
    startAlignDrag(e, block, c, pxPerSec);
  });
  block.querySelector(".tl-handle.left").addEventListener("pointerdown", (e) => {
    e.stopPropagation();
    e.preventDefault();
    selectClip(c.id);
    startTrimDrag(e, block, c, "left", pxPerSec);
  });
  block.querySelector(".tl-handle.right").addEventListener("pointerdown", (e) => {
    e.stopPropagation();
    e.preventDefault();
    selectClip(c.id);
    startTrimDrag(e, block, c, "right", pxPerSec);
  });
  block.addEventListener("dblclick", () => openEditor(c.id));
}

function startAlignDrag(e, block, c, pxPerSec) {
  if (e.button !== 0) return;
  const startX = e.clientX;
  const startPx = block.offsetLeft; // the block's actual on-screen left edge
  try { block.setPointerCapture(e.pointerId); } catch (_) { /* noop */ }
  const onMove = (ev) => {
    // Follow the mouse 1:1 in pixels so the block stays exactly under the
    // cursor while dragging (the scale is cached across renders).
    const leftPx = Math.max(0, startPx + (ev.clientX - startX));
    const newStart = snapFrame(leftPx / pxPerSec);
    if (!isSoundKind(c.kind) && newStart < prevVisualEnd(c.id)) return; // no overlap on the video rail
    c.edit.start = newStart;
    block.style.left = `${leftPx}px`;
  };
  const onUp = () => {
    block.removeEventListener("pointermove", onMove);
    block.removeEventListener("pointerup", onUp);
    try { block.releasePointerCapture(e.pointerId); } catch (_) { /* noop */ }
    renderTimeline();
    schedulePreviewRefresh();
  };
  block.addEventListener("pointermove", onMove);
  block.addEventListener("pointerup", onUp);
}

function startTrimDrag(e, block, c, side, pxPerSec) {
  if (e.button !== 0) return;
  const startX = e.clientX;
  const srcDur = (clipInfo.get(c.id) || {}).dur || 0;
  try { block.setPointerCapture(e.pointerId); } catch (_) { /* noop */ }
  const onMove = (ev) => {
    const dsec = (ev.clientX - startX) / pxPerSec;
    if (c.kind === "image") {
      if (side === "right") c.edit.duration = snapFrame(Math.max(FRAME, Math.min(60, (c.edit.duration || 1) + dsec)));
    } else if (srcDur > 0) {
      if (side === "left") {
        c.edit.trimStart = snapFrame(Math.max(0, Math.min(srcDur - FRAME, (c.edit.trimStart || 0) + dsec)));
      } else {
        const cur = c.edit.trimEnd > 0 && c.edit.trimEnd <= srcDur ? c.edit.trimEnd : srcDur;
        c.edit.trimEnd = snapFrame(Math.max((c.edit.trimStart || 0) + FRAME, Math.min(srcDur, cur + dsec)));
      }
    }
    block.style.width = `${Math.max(26, editedDurOf(c) * pxPerSec)}px`;
  };
  const onUp = () => {
    block.removeEventListener("pointermove", onMove);
    block.removeEventListener("pointerup", onUp);
    try { block.releasePointerCapture(e.pointerId); } catch (_) { /* noop */ }
    renderTimeline();
    schedulePreviewRefresh();
  };
  block.addEventListener("pointermove", onMove);
  block.addEventListener("pointerup", onUp);
}

function selectClip(id) {
  selectedClipId = id;
  $$(".tl-block").forEach((b) => b.classList.toggle("selected", b.dataset.id === id));
  renderInspector();
}

function renderInspector() {
  const insp = $("#clip-inspector");
  if (!selectedClipId) { insp.classList.add("hidden"); return; }
  const c = state.clips.find((x) => x.id === selectedClipId);
  if (!c) { insp.classList.add("hidden"); return; }
  insp.classList.remove("hidden");
  $("#clip-inspector-title").textContent = `${shortName(c.name)} (${c.kind})`;
  const p = currentPos(c);
  const info = clipInfo.get(c.id);
  const isImg = c.kind === "image";
  $("#insp-in-wrap").classList.toggle("hidden", isImg);
  $("#insp-out-wrap").classList.toggle("hidden", isImg);
  $("#insp-dur-wrap").classList.toggle("hidden", !isImg);
  $("#insp-pos").value = p.start.toFixed(2);
  if (isImg) {
    $("#insp-dur").value = (c.edit.duration || 1).toFixed(2);
  } else {
    $("#insp-in").value = (c.edit.trimStart || 0).toFixed(2);
    $("#insp-out").value = (c.edit.trimEnd > 0 ? c.edit.trimEnd : (info ? info.dur : 0)).toFixed(2);
  }
}

const getSelectedClip = () => (selectedClipId ? state.clips.find((x) => x.id === selectedClipId) : null);

$("#insp-pos").addEventListener("change", () => {
  const c = getSelectedClip();
  if (!c) return;
  c.edit.start = snapFrame(Math.max(0, parseFloat($("#insp-pos").value) || 0));
  renderTimeline();
  schedulePreviewRefresh();
});
$("#insp-in").addEventListener("change", () => {
  const c = getSelectedClip();
  if (!c || c.kind === "image") return;
  const srcDur = (clipInfo.get(c.id) || {}).dur || 0;
  c.edit.trimStart = snapFrame(Math.max(0, Math.min(srcDur - FRAME, parseFloat($("#insp-in").value) || 0)));
  renderTimeline();
  schedulePreviewRefresh();
});
$("#insp-out").addEventListener("change", () => {
  const c = getSelectedClip();
  if (!c || c.kind === "image") return;
  const srcDur = (clipInfo.get(c.id) || {}).dur || 0;
  c.edit.trimEnd = snapFrame(Math.max((c.edit.trimStart || 0) + FRAME, Math.min(srcDur, parseFloat($("#insp-out").value) || srcDur)));
  renderTimeline();
  schedulePreviewRefresh();
});
$("#insp-dur").addEventListener("change", () => {
  const c = getSelectedClip();
  if (!c || c.kind !== "image") return;
  c.edit.duration = snapFrame(Math.max(FRAME, parseFloat($("#insp-dur").value) || 1));
  renderTimeline();
  schedulePreviewRefresh();
});
$("#insp-edit").addEventListener("click", () => { if (selectedClipId) openEditor(selectedClipId); });

// ── final preview (step 2) ─────────────────────────────────────────────
const PREVIEW_AUTO_MAX = 20; // seconds — auto re-render after edits under this
let previewTimer = null;
let previewBusy = false;

function schedulePreviewRefresh() {
  if (currentTotalDur() <= PREVIEW_AUTO_MAX) {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(() => refreshPreview(), 500);
  }
}

async function refreshPreview() {
  if (!state.clips.length || previewBusy) return;
  // MIDI is uploaded raw to the backend and can't be decoded in-browser, so it
  // is never part of the on-screen final preview.
  const previewable = state.clips.filter((c) => c.kind !== "midi");
  if (!previewable.length) return;
  previewBusy = true;
  const status = $("#preview-status");
  const player = $("#preview-player");
  status.textContent = t("t.rendering");
  try {
    const specs = previewable.map((c) => ({ id: c.id, file: c.file, name: c.name, kind: c.kind, edit: c.edit }));
    const out = await exportConcatenated(specs, { mode: $("#opt-output-mode").value, maxDim: parseInt($("#compress-dim").value, 10) || 256 },
      (pct) => { status.textContent = t("t.renderingPct", { pct: Math.round(pct) }); });
    if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
    state.previewUrl = URL.createObjectURL(out.blob);
    player.src = state.previewUrl;
    player.classList.remove("hidden");
    player.play().catch(() => {});
    state.previewRendered = true;
    status.textContent = t("t.rendered", { sec: out.duration.toFixed(1) });
  } catch (e) {
    status.textContent = "";
    toast(t("t.previewFailed", { msg: e.message }), "error");
  } finally {
    previewBusy = false;
  }
}
$("#btn-preview").addEventListener("click", refreshPreview);
$("#preview-player").addEventListener("click", () => {
  if (state.clips.length) refreshPreview();
});


$("#btn-preview-size").addEventListener("click", () => {
  const player = $("#preview-player");
  const canvas = $("#final-preview");
  const ctx = canvas.getContext("2d");
  let w = (state.recommended && state.recommended[0]) || 28;
  let h = (state.recommended && state.recommended[1]) || 26;
  if (!$("#opt-auto-size").checked) {
    w = parseInt($("#opt-width").value, 10) || w;
    h = parseInt($("#opt-height").value, 10) || h;
  }
  canvas.width = Math.max(2, w * 4);
  canvas.height = Math.max(2, h * 4);
  ctx.imageSmoothingEnabled = false;
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  if (player.videoWidth) ctx.drawImage(player, 0, 0, canvas.width, canvas.height);
  else toast(t("t.noPreviewFirst"), "error");
});

// ═══════════════════════════════════════════════════════════════════════
// Create — media clip playlist (append, reorder, edit each)
// ═══════════════════════════════════════════════════════════════════════
const dropzone = $("#dropzone");
const fileInput = $("#file-input");
const playlistEl = $("#playlist");
const generateBtn = $("#btn-generate");
const generateStatus = $("#generate-status");

function defaultEdit() {
  return { trimStart: 0, trimEnd: 0, crop: { x: 0, y: 0, w: 1, h: 1 }, offset: 0, mute: false, start: null };
}
function isEdited(e) {
  return e.trimStart !== 0 || e.trimEnd !== 0 || e.crop.x !== 0 || e.crop.y !== 0
    || e.crop.w !== 1 || e.crop.h !== 1 || e.offset !== 0 || e.mute || e.start != null;
}

// Working-cache max dimension — each clip is downsampled to this on add, and
// the cache is reused for editing, previewing and the final render.
const PROXY_MAX = 512;

// Reject files above this size up-front (matches the backend upload limit).
const MAX_UPLOAD_BYTES = 256 * 1024 * 1024; // 256 MiB

// Cache builds are serialized (one at a time): running several heavy
// main-thread encoders at once would freeze the UI and block everything else.
// Chosen files land in the list immediately with a "processing…" state and
// their chips stay interactive while the queue works through them.
let cacheQueue = [];
let cacheBusy = false;
const chipEls = new Map(); // clip id -> chip element, for live progress updates

function queueCacheBuild(clip) {
  cacheQueue.push(clip);
  if (!cacheBusy) void drainCacheQueue();
}

async function drainCacheQueue() {
  cacheBusy = true;
  try {
    while (cacheQueue.length) {
      const c = cacheQueue.shift();
      // The user may have removed this clip while it waited — skip it.
      if (!state.clips.includes(c)) continue;
      if (c.cacheStatus === "pending") await buildCache(c);
    }
  } finally {
    cacheBusy = false;
  }
}

function paintClipProgress(c) {
  const chip = chipEls.get(c.id);
  if (!chip) return;
  const fill = chip.querySelector(".progressbar > span");
  const pct = chip.querySelector(".clip-progress .pct");
  if (fill) fill.style.width = `${Math.round((c.progress || 0) * 100)}%`;
  if (pct) pct.textContent = `${Math.round((c.progress || 0) * 100)}%`;
}

async function buildCache(c) {
  if (c.cacheStatus === "caching" || c.cacheStatus === "cached") return;
  c.cacheStatus = "caching";
  c.progress = 0;
  renderPlaylist();
  try {
    if (c.kind === "video") {
      const blob = await compressVideo(c.file, {
        quality: "high",
        maxDim: PROXY_MAX,
        onProgress: (pct) => { c.progress = pct / 100; paintClipProgress(c); },
      });
      blob.name = c.name.replace(/\.[^.]+$/i, "") + "-proxy.webm";
      c.file = blob;
      c.size = blob.size;
    } else if (c.kind === "image") {
      const out = await exportEditedImage(c.file, { x: 0, y: 0, w: 1, h: 1 }, PROXY_MAX);
      c.file = out.blob;
      c.size = out.blob.size;
    }
    // audio & MIDI: kept as-is (MIDI can't be decoded in-browser; the raw
    // file is uploaded and the backend handles it) — marked cached for the
    // pipeline either way.
    c.cacheStatus = "cached";
  } catch (e) {
    c.cacheStatus = "failed";
    console.warn("cache build failed:", c.name, e);
  }
  c.progress = 1;
  clipInfo.delete(c.id);
  renderPlaylist();
  if (!$("#wstep-timeline").classList.contains("hidden")) {
    await renderTimeline();
    schedulePreviewRefresh();
  }
  refreshRecommendation();
}

function addFiles(files) {
  for (const file of files) {
    if (file.size > MAX_UPLOAD_BYTES) {
      toast(t("t.tooLarge", { size: fmtBytes(file.size), max: fmtBytes(MAX_UPLOAD_BYTES) }), "error");
      continue;
    }
    const kind = mediaKind(file.name);
    const clip = {
      id: `f${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
      file, name: file.name, size: file.size, kind,
      edit: defaultEdit(), cacheStatus: "pending",
    };
    if (isSoundKind(kind)) clip.edit.start = 0;
    state.clips.push(clip);
    queueCacheBuild(clip);
  }
  renderPlaylist();
  refreshRecommendation();
  generateBtn.disabled = state.clips.length === 0;
  $("#btn-next-media").disabled = state.clips.length === 0;
}

dropzone.addEventListener("click", (e) => {
  // Interacting with a listed clip (edit/move/remove) must not reopen the picker.
  if (e.target.closest("button, a, .upload-chip")) return;
  fileInput.click();
});
dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("drag"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("drag"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("drag");
  addFiles([...e.dataTransfer.files]);
});
fileInput.addEventListener("change", () => {
  // Always clear the input (even if adding throws) so re-selecting the same
  // file, or adding more files, always fires `change` again.
  try { addFiles([...fileInput.files]); }
  finally { fileInput.value = ""; }
});
// Allow adding more media while editing (timeline step) — buildCache re-renders
// the timeline because the step is visible.
const btnAddMedia = $("#btn-add-media");
if (btnAddMedia) btnAddMedia.addEventListener("click", () => fileInput.click());

function clipStatus(c) {
  if (c.cacheStatus === "pending" || c.cacheStatus === "caching") return t("t.clipsProcessing");
  if (c.cacheStatus === "failed") return t("t.clipsFailed");
  return t("t.clipsCached");
}

function renderPlaylist() {
  // Chosen files are displayed inside the dotted box — hide the drop hint
  // once something is listed.
  const hint = $("#dropzone-hint");
  if (hint) hint.classList.toggle("hidden", state.clips.length > 0);
  playlistEl.innerHTML = "";
  chipEls.clear();
  if (!state.clips.length) return;
  for (const c of state.clips) {
    const processing = c.cacheStatus === "pending" || c.cacheStatus === "caching";
    const chip = el("div", { class: "upload-chip" + (processing ? " processing" : ""), dataset: { id: c.id } }, [
      el("span", { class: "badge " + c.kind, text: c.kind }),
      el("span", { class: "name", text: c.name }),
      el("span", { class: "meta" }, [
        `${fmtBytes(c.size)} · `,
        el("span", { class: processing ? "processing" : "", text: clipStatus(c) }),
        isEdited(c.edit) ? ` · ${t("t.edited")}` : "",
      ]),
      el("button", { text: t("t.clipsEdit"), onclick: () => openEditor(c.id) }),
      el("button", { text: "↑", title: "Move up", onclick: () => moveClip(c.id, -1) }),
      el("button", { text: "↓", title: "Move down", onclick: () => moveClip(c.id, 1) }),
      el("button", { class: "danger", text: "✕", title: "Remove", onclick: () => removeClip(c.id) }),
    ]);
    if (processing) {
      chip.append(el("div", { class: "clip-progress" }, [
        el("div", { class: "progressbar" }, [el("span", { style: `width:${Math.round((c.progress || 0) * 100)}%` })]),
        el("span", { class: "pct", text: `${Math.round((c.progress || 0) * 100)}%` }),
      ]));
    }
    chipEls.set(c.id, chip);
    playlistEl.append(chip);
  }
}

function moveClip(id, dir) {
  const i = state.clips.findIndex((c) => c.id === id);
  const j = i + dir;
  if (i < 0 || j < 0 || j >= state.clips.length) return;
  const [item] = state.clips.splice(i, 1);
  state.clips.splice(j, 0, item);
  renderPlaylist();
  if (!$("#wstep-timeline").classList.contains("hidden")) { renderTimeline(); schedulePreviewRefresh(); }
}

function removeClip(id) {
  state.clips = state.clips.filter((c) => c.id !== id);
  clipInfo.delete(id);
  chipEls.delete(id);
  cacheQueue = cacheQueue.filter((c) => c.id !== id);
  if (selectedClipId === id) selectedClipId = null;
  renderPlaylist();
  renderInspector();
  refreshRecommendation();
  generateBtn.disabled = state.clips.length === 0;
  $("#btn-next-media").disabled = state.clips.length === 0;
  if (!$("#wstep-timeline").classList.contains("hidden")) { renderTimeline(); schedulePreviewRefresh(); }
}

// ═══════════════════════════════════════════════════════════════════════
// Editor (modal) — edits ONE clip
// ═══════════════════════════════════════════════════════════════════════
let editorVideo = null;   // <video> or <img> inside the stage
let editorCrop = null;    // crop controller
let editorKind = "video"; // video | audio | image
let editorDuration = 0;
let editorUrl = null;

function openEditor(clipId) {
  const c = state.clips.find((x) => x.id === clipId);
  if (!c) return;
  state.editorClipId = clipId;
  editorKind = c.kind === "image" ? "image" : (isSoundKind(c.kind) ? "audio" : "video");
  const stage = $("#editor-stage");
  stage.innerHTML = "";
  editorUrl = URL.createObjectURL(c.file);

  const edit = c.edit;
  $("#editor-start").value = edit.trimStart;
  $("#editor-end").value = edit.trimEnd;
  $("#editor-crop-enable").checked = edit.crop.w < 1 || edit.crop.h < 1 || edit.crop.x > 0 || edit.crop.y > 0;
  $("#editor-offset").value = edit.offset;
  $("#editor-offset-val").textContent = `${edit.offset.toFixed(1)}s`;
  $("#editor-mute").checked = edit.mute;
  $("#editor-start-at").value = edit.start || 0;
  $("#editor-start-at").disabled = editorKind !== "audio";
  $("#editor-status").textContent = "";
  $("#editor-audio-panel").classList.remove("hidden");
  $("#editor-start").disabled = false;
  $("#editor-end").disabled = false;
  $("#editor-play").disabled = false;

  const sizeStage = (w, h) => {
    const maxW = 560, maxH = 340;
    const s = Math.min(1, maxW / w, maxH / h);
    stage.style.width = `${Math.round(w * s)}px`;
    stage.style.height = `${Math.round(h * s)}px`;
  };

  if (editorKind === "image") {
    const img = document.createElement("img");
    img.src = editorUrl;
    img.alt = "preview";
    img.onload = () => sizeStage(img.naturalWidth, img.naturalHeight);
    stage.appendChild(img);
    editorVideo = img;
    $("#editor-audio-panel").classList.add("hidden");
    $("#editor-start").disabled = true;
    $("#editor-end").disabled = true;
    $("#editor-play").disabled = true;
  } else {
    const vid = document.createElement("video");
    vid.muted = true;
    vid.playsInline = true;
    vid.controls = true;
    vid.src = editorUrl;
    vid.onloadedmetadata = () => {
      editorDuration = vid.duration || 0;
      sizeStage(vid.videoWidth || 640, vid.videoHeight || 360);
      const d = editorDuration.toFixed(1);
      if (!edit.trimEnd || edit.trimEnd > editorDuration) $("#editor-end").value = d;
      $("#editor-start").max = d;
      $("#editor-end").max = d;
    };
    stage.appendChild(vid);
    editorVideo = vid;
  }

  editorCrop = attachCropBox(stage, () => {});
  $("#editor-crop-enable").checked = edit.crop.w < 1 || edit.crop.h < 1;
  editorCrop.setVisible($("#editor-crop-enable").checked);
  if ($("#editor-crop-enable").checked) editorCrop.set(edit.crop);
  $("#editor-modal").classList.remove("hidden");
}

function cleanupEditor() {
  if (editorUrl) URL.revokeObjectURL(editorUrl);
  editorUrl = null;
  editorVideo = null;
  editorCrop = null;
  editorDuration = 0;
  $("#editor-stage").innerHTML = "";
  $("#editor-modal").classList.add("hidden");
}
$("#editor-close").addEventListener("click", cleanupEditor);
$("#editor-modal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) cleanupEditor();
});

$("#editor-crop-enable").addEventListener("change", () => {
  if (!editorCrop) return;
  editorCrop.setVisible($("#editor-crop-enable").checked);
  if ($("#editor-crop-enable").checked) editorCrop.reset();
});
$("#editor-crop-reset").addEventListener("click", () => editorCrop && editorCrop.reset());
$("#editor-offset").addEventListener("input", () => {
  $("#editor-offset-val").textContent = `${parseFloat($("#editor-offset").value).toFixed(1)}s`;
});

$("#editor-play").addEventListener("click", () => {
  const v = editorVideo;
  if (!v || v.tagName !== "VIDEO") return;
  const start = parseFloat($("#editor-start").value) || 0;
  const end = parseFloat($("#editor-end").value) || editorDuration;
  v.currentTime = Math.min(start, Math.max(0, end - 0.1));
  v.play();
  const stopAt = () => { if (v.currentTime >= end) v.pause(); };
  v.addEventListener("timeupdate", stopAt);
});

function drawResolutionPreview() {
  const canvas = $("#editor-preview");
  const ctx = canvas.getContext("2d");
  const src = editorVideo;
  if (!src) return;
  const crop = editorCrop ? editorCrop.get() : { x: 0, y: 0, w: 1, h: 1 };
  let w = (state.recommended && state.recommended[0]) || 28;
  let h = (state.recommended && state.recommended[1]) || 26;
  if (!$("#opt-auto-size").checked) {
    w = parseInt($("#opt-width").value, 10) || w;
    h = parseInt($("#opt-height").value, 10) || h;
  }
  canvas.width = Math.max(2, w * 4);
  canvas.height = Math.max(2, h * 4);
  ctx.imageSmoothingEnabled = false;
  const sw = src.videoWidth || src.naturalWidth || 1;
  const sh = src.videoHeight || src.naturalHeight || 1;
  ctx.drawImage(src, crop.x * sw, crop.y * sh, crop.w * sw, crop.h * sh, 0, 0, canvas.width, canvas.height);
}
$("#editor-preview-size").addEventListener("click", () => {
  const v = editorVideo;
  if (v && v.tagName === "VIDEO") { v.currentTime = parseFloat($("#editor-start").value) || 0; v.pause(); }
  drawResolutionPreview();
});

$("#editor-export").addEventListener("click", () => {
  const c = state.clips.find((x) => x.id === state.editorClipId);
  if (!c) return;
  c.edit = {
    trimStart: snapFrame(Math.max(0, parseFloat($("#editor-start").value) || 0)),
    trimEnd: snapFrame(parseFloat($("#editor-end").value) || 0),
    crop: editorCrop ? editorCrop.get() : { x: 0, y: 0, w: 1, h: 1 },
    offset: parseFloat($("#editor-offset").value) || 0,
    mute: $("#editor-mute").checked,
    // start-on-rail is editable only for audio here; video keeps its timeline position
    start: editorKind === "audio" ? snapFrame(Math.max(0, parseFloat($("#editor-start-at").value) || 0)) : c.edit.start,
  };
  cleanupEditor();
  renderPlaylist();
  renderTimeline();
  refreshRecommendation();
  schedulePreviewRefresh();
  toast(t("t.clipUpdated"));
});

// ═══════════════════════════════════════════════════════════════════════
// Display size recommendation
// ═══════════════════════════════════════════════════════════════════════
const DISPLAY_MAX_W = 54;
const DISPLAY_MAX_H = 30;

async function probeDimensions(media) {
  if (!media || !isVisualFile(media.name)) return null;
  if (isVideoFile(media.name)) {
    const url = URL.createObjectURL(media.file);
    try {
      const v = document.createElement("video");
      v.muted = true;
      v.preload = "metadata";
      v.src = url;
      await new Promise((res, rej) => { v.onloadedmetadata = res; v.onerror = () => rej(new Error()); });
      const dims = [v.videoWidth, v.videoHeight];
      URL.revokeObjectURL(url);
      return dims;
    } catch (_) {
      URL.revokeObjectURL(url);
      return null;
    }
  }
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve([img.naturalWidth, img.naturalHeight]);
    img.onerror = () => resolve(null);
    img.src = URL.createObjectURL(media.file);
  });
}

// Replicates the backend's resolve_dimensions: fit into 28×26 keeping ratio.
function fitDisplay(sw, sh) {
  let w = DISPLAY_MAX_W;
  let h = Math.max(1, Math.round((w * sh) / sw));
  if (h > DISPLAY_MAX_H) {
    h = DISPLAY_MAX_H;
    w = Math.max(1, Math.round((h * sw) / sh));
  }
  return [w, h];
}

async function refreshRecommendation() {
  const recommend = $("#size-recommend");
  const auto = $("#opt-auto-size").checked;
  $("#opt-width").disabled = auto;
  $("#opt-height").disabled = auto;
  const clip = state.clips.find((c) => isVisualFile(c.name));
  if (!clip) {
    recommend.textContent = "Upload a video or image to see the recommended size.";
    state.recommended = null;
    if (auto) { $("#opt-width").value = ""; $("#opt-height").value = ""; }
    return;
  }
  const dims = await probeDimensions(clip);
  if (!dims || !dims[0] || !dims[1]) {
    recommend.textContent = `Could not probe "${clip.name}" — size will be auto-detected server-side.`;
    state.recommended = null;
    return;
  }
  const [w, h] = fitDisplay(dims[0], dims[1]);
  state.recommended = [w, h];
  recommend.textContent = `Recommended: ${w} × ${h} tiles (from ${clip.name}, ${dims[0]}×${dims[1]})`;
  if (auto) { $("#opt-width").value = w; $("#opt-height").value = h; }
}

$("#opt-auto-size").addEventListener("change", () => {
  const auto = $("#opt-auto-size").checked;
  $("#opt-width").disabled = auto;
  $("#opt-height").disabled = auto;
  if (auto && state.recommended) {
    $("#opt-width").value = state.recommended[0];
    $("#opt-height").value = state.recommended[1];
  }
  refreshRecommendation();
});

// Manual width/height: keep the other side matching the source aspect ratio.
async function keepAspectFrom(which) {
  const clip = state.clips.find((c) => isVisualFile(c.name));
  const dims = clip ? await probeDimensions(clip) : null;
  if (!dims || !dims[0] || !dims[1]) return;
  const ratio = dims[0] / dims[1];
  if (which === "width") {
    const w = parseInt($("#opt-width").value, 10);
    if (w) $("#opt-height").value = Math.max(1, Math.round(w / ratio));
  } else {
    const h = parseInt($("#opt-height").value, 10);
    if (h) $("#opt-width").value = Math.max(1, Math.round(h * ratio));
  }
}
$("#opt-width").addEventListener("input", () => {
  if (!$("#opt-auto-size").checked && $("#opt-keep-aspect").checked) keepAspectFrom("width");
});
$("#opt-height").addEventListener("input", () => {
  if (!$("#opt-auto-size").checked && $("#opt-keep-aspect").checked) keepAspectFrom("height");
});

// ═══════════════════════════════════════════════════════════════════════
// Options
// ═══════════════════════════════════════════════════════════════════════
function readOptions() {
  const val = (sel, def) => { const n = $(sel); return n && n.value !== "" ? n.value : def; };
  const bool = (sel) => !!$(sel) && $(sel).checked;
  const num = (sel, def) => { const v = parseFloat(val(sel, "")); return Number.isFinite(v) ? v : def; };
  const int = (sel, def) => { const v = parseInt(val(sel, ""), 10); return Number.isFinite(v) ? v : def; };
  const opts = {
    name: val("#opt-name", "Media Data") || "Media Data",
    result_format: val("#opt-result-format", "blueprint"),
    power: val("#opt-power", "substation"),
    attach_player: bool("#opt-attach-player"),
    progress_bar: bool("#opt-progress-bar"),
    fps: num("#opt-fps", 0),
    skip: int("#opt-skip", 1),
    adaptive: val("#opt-adaptive", "false") === "true",
    threshold: num("#opt-threshold", 0.01),
    deduplicate: val("#opt-deduplicate", "false") === "true",
    time_chunks: int("#opt-time-chunks", 1),
    chunk_workers: $("#opt-chunk-workers").value ? int("#opt-chunk-workers") : null,
    deduplicate_cross: bool("#opt-dedup-cross"),
    ticks_per_beat: int("#opt-ticks-per-beat", 30),
    boost_melody: num("#opt-boost-melody", 1.0),
    velocity_scale: num("#opt-velocity-scale", 1.0),
    rail_mode: val("#opt-rail-mode", "auto:0.05"),
    map_drums: bool("#opt-map-drums"),
    drum_gain: num("#opt-drum-gain", 0.25),
    use_global_shift: bool("#opt-global-shift"),
    attack_ticks: int("#opt-attack-ticks", 10),
    decay_ticks: int("#opt-decay-ticks", 10),
    sustain_level: num("#opt-sustain-level", 1.0),
    release_ticks: int("#opt-release-ticks", 10),
    attack_curve: num("#opt-attack-curve", 1.0),
    decay_curve: num("#opt-decay-curve", 1.0),
    release_curve: num("#opt-release-curve", 1.0),
    use_basic_pitch: bool("#opt-basic-pitch"),
    activation_threshold: num("#opt-activation-threshold", 0.0),
    midi_threshold: num("#opt-midi-threshold", 0.05),
    condense_midi: bool("#opt-condense"),
    max_polyphony: int("#opt-max-polyphony", 0),
    use_cache: bool("#opt-use-cache"),
  };
  if ($("#opt-auto-size").checked) {
    if (state.recommended) { opts.width = state.recommended[0]; opts.height = state.recommended[1]; }
    else { opts.width = null; opts.height = null; }
  } else {
    opts.width = $("#opt-width").value ? int("#opt-width") : null;
    opts.height = $("#opt-height").value ? int("#opt-height") : null;
  }
  return opts;
}

// ═══════════════════════════════════════════════════════════════════════
// Generate — concatenate/export clips → compress → upload → cache → submit
// ═══════════════════════════════════════════════════════════════════════
generateBtn.addEventListener("click", generate);

async function generate() {
  if (!state.clips.length) return;
  // Not signed in → warn that uploads/blueprints are public.  Offer "continue
  // as guest" only when the backend supports anonymous use; otherwise the
  // server requires sign-in, so the only way forward is to log in.
  if (isAnonymous() && githubAuth) {
    if (state.anonSupported) loginProceed = () => { runGenerate(); };
    openLoginModal();
    return;
  }
  await runGenerate();
}

async function runGenerate() {
  const quality = $("#compress-quality").value;
  const outputMode = $("#opt-output-mode").value;
  // Videos are always compressed to the display resolution (non-optional).
  const dispOpts = readOptions();
  const maxDim = Math.max(dispOpts.width || DISPLAY_MAX_W, dispOpts.height || DISPLAY_MAX_H);

  generateBtn.disabled = true;
  try {
    generateStatus.textContent = t("t.renderingEdited");
    const visuals = state.clips.filter((c) => c.kind === "video" || c.kind === "image");
    const sounds = state.clips.filter((c) => isSoundKind(c.kind));
    const hasMidi = sounds.some((s) => s.kind === "midi");
    // Separate-input mode when MIDI is involved (the browser can't decode MIDI,
    // so the raw file must go to the backend) or when a task mixes visuals with
    // a separate audio track — that keeps the audio an independent, updatable
    // input for the job.
    const useSeparate = hasMidi || (visuals.length > 0 && sounds.length > 0);

    const uploadIds = [];
    const previews = []; // {blob, name, kind} — first one drives the job preview
    const uploadOne = async (blob, fname) => {
      generateStatus.textContent = t("t.uploading");
      const fd = new FormData();
      fd.append("files", blob, fname);
      const res = await api("/api/v1/uploads", { method: "POST", body: fd });
      const ups = await res.json();
      return ups[0].upload_id;
    };

    if (useSeparate) {
      // ── visual track → one exported file ─────────────────────────
      if (visuals.length) {
        let out;
        if (visuals.length === 1 && visuals[0].kind === "image") {
          const c = visuals[0];
          out = await exportEditedImage(c.file, c.edit.crop, maxDim);
        } else {
          const specs = visuals.map((c) => ({ id: c.id, file: c.file, name: c.name, kind: c.kind, edit: c.edit }));
          // The audio track is provided separately → mute the video's own audio.
          const mode = sounds.length ? "video-muted" : outputMode;
          out = await exportConcatenated(specs, { mode, maxDim },
            (pct) => { generateStatus.textContent = t("t.renderingPct", { pct: Math.round(pct) }); });
        }
        let blob = out.blob;
        if (out.kind === "video" && blob.size > 128 * 1024) {
          generateStatus.textContent = t("t.compressing");
          blob = await compressVideo(blob, { quality, maxDim });
        }
        uploadIds.push(await uploadOne(blob, out.name));
        previews.push({ blob, name: out.name, kind: out.kind });
      }
      // ── sound track (audio + MIDI) → raw uploads, independent inputs ──
      for (const s of sounds) {
        uploadIds.push(await uploadOne(s.file, s.name));
        previews.push({ blob: s.file, name: s.name, kind: s.kind });
      }
      if (!uploadIds.length) throw new Error("no media to generate");
    } else {
      // ── existing single-file path (pure video / pure audio / single image) ──
      let toUpload;
      let kind;
      let name;
      if (state.clips.length === 1 && state.clips[0].kind === "image") {
        const c = state.clips[0];
        const out = await exportEditedImage(c.file, c.edit.crop, maxDim);
        toUpload = out.blob; kind = out.kind; name = out.name;
      } else {
        const specs = state.clips.map((c) => ({ id: c.id, file: c.file, name: c.name, kind: c.kind, edit: c.edit }));
        const out = await exportConcatenated(specs, { mode: outputMode, maxDim },
          (pct) => { generateStatus.textContent = t("t.renderingPct", { pct: Math.round(pct) }); });
        toUpload = out.blob; kind = out.kind; name = out.name;
      }
      if (kind === "video" && toUpload.size > 128 * 1024) {
        generateStatus.textContent = t("t.compressing");
        toUpload = await compressVideo(toUpload, { quality, maxDim });
      }
      uploadIds.push(await uploadOne(toUpload, toUpload.name || name));
      previews.push({ blob: toUpload, name: toUpload.name || name, kind });
    }

    generateStatus.textContent = t("t.submitting");
    const body = { type: "encode", inputs: uploadIds, options: readOptions() };
    const cb = $("#opt-callback-url").value.trim();
    if (cb) body.callback_url = cb;
    const jres = await api("/api/v1/jobs", { method: "POST", body });
    const data = await jres.json();

    // Keep the (compressed) media locally so the job list can preview it.
    const primary = previews[0];
    try { await saveMedia(data.job_id, primary.blob, primary.name, primary.kind); } catch (_) { /* non-fatal */ }

    // Build a 1s low-res preview clip so the job list shows what this job is.
    // (makeJobPreview → makePreview; sound-only jobs render a note, no video.)
    makeJobPreview(data.job_id, primary.blob, primary.kind);

    generateStatus.textContent = "";
    toast(t("t.jobQueued", { id: data.job_id }));
    state.running.add(data.job_id);
    state.expanded.add(data.job_id);
    showView("home");
    startPolling();
  } catch (e) {
    generateStatus.textContent = "";
    if (e.status === 401) {
      // The server rejected us (it requires sign-in, or our anonymous token
      // was rejected/expired).  Say why instead of silently flashing the
      // modal, then offer login as the way forward.
      toast(t("auth.loginRequired"), "error");
      openLoginModal();
    } else if (e.status === 429 || e.status === 409) {
      toast(t("t.serverBusy", { msg: e.message }));
      showView("home");
    } else {
      toast(t("t.failed", { msg: e.message }), "error");
    }
  } finally {
    generateBtn.disabled = false;
  }
}

// ═══════════════════════════════════════════════════════════════════════
// Result helpers
// ═══════════════════════════════════════════════════════════════════════
const FORMAT_EXT = { blueprint: "txt", toml: "toml", yaml: "yaml", json: "json" };
const FORMAT_MIME = {
  blueprint: "text/plain", toml: "text/plain", yaml: "application/yaml", json: "application/json",
};

function metaItem(label, value) {
  if (value == null || value === "") return null;
  return el("span", {}, [el("b", { text: `${label}: ` }), String(value)]);
}

function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text).then(() => toast(t("t.copied")));
  }
  const ta = el("textarea", { class: "mono", style: "position:fixed;left:-9999px", text });
  document.body.append(ta);
  ta.select();
  try { document.execCommand("copy"); toast(t("t.copied")); } catch (_) { toast(t("t.copyFailed"), "error"); }
  ta.remove();
  return Promise.resolve();
}

function downloadText(text, name) {
  const blob = new Blob([text], { type: FORMAT_MIME[name.split(".").pop()] || "text/plain" });
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

// Render a result for a completed job: blueprint / "Inspect item" (structured
// YAML) tabs, JSON only in dev mode, plus copy/download/Pastebin/FBE actions.
async function renderJobResult(host, job) {
  const id = job.job_id;
  host.innerHTML = "";
  const meta = job.result || {};
  const metaStrip = el("div", { class: "meta-strip" }, [
    metaItem(t("result.entityCount"), meta.entity_count != null ? Number(meta.entity_count).toLocaleString() : null),
    metaItem(t("result.totalTicks"), meta.total_ticks),
    metaItem(t("result.dimensions"), meta.dimensions ? meta.dimensions.join(" × ") : null),
    metaItem(t("result.instruments"), meta.instruments ? meta.instruments.join(", ") : null),
    metaItem(t("result.kind"), meta.kind),
  ]);
  // Quick actions — the single "Inspect" button opens the shared inspector
  // modal (string / parts list / interactive preview).  Opening it fetches
  // the blueprint text when it isn't cached yet (getResultText), then the
  // modal decodes and renders everything client-side.
  host.append(metaStrip,
    el("div", { class: "row", style: "margin-top:8px;gap:8px;flex-wrap:wrap" }, [
      el("button", { class: "primary", text: t("result.inspect"), title: t("result.inspectTitle"), onclick: () => openBlueprintInspector({ jobId: id, getResultText, title: job.name || t("viewer.title") }) }),
      el("button", { text: t("result.copy"), onclick: () => copyJobResult(id) }),
      el("button", { text: t("result.download"), onclick: () => downloadJobResult(id, job.name) }),
      el("button", { text: t("result.pastebin"), title: "Create a temporary public link for this blueprint", onclick: () => shareJob(id) }),
      el("button", { text: t("result.fbe"), title: "Render it in the Factorio Blueprint Editor (via the share link)", onclick: () => openFBE(id) }),
    ]));

  try {
    const res = await api(`/api/v1/jobs/${id}/artifacts`);
    const data = await res.json();
    if (data.artifacts && data.artifacts.length) {
      const list = el("div", { class: "artifacts" }, [el("b", { text: t("jobs.artifacts") })]);
      for (const a of data.artifacts) {
        const low = a.name.toLowerCase();
        if (low.endsWith(".toml")) continue;              // toml removed
        if (low.endsWith(".json") && !isDev()) continue;  // json dev-only
        list.append(
          el("a", { href: await apiUrl(`/api/v1/jobs/${id}/artifacts/${encodeURIComponent(a.name)}`), target: "_blank", rel: "noopener", text: `${a.name} (${fmtBytes(a.size_bytes)})` }),
          " "
        );
      }
      host.append(list);
    }
  } catch (_) { /* non-fatal */ }
}

// ── temporary share link (Pastebin / FBE source) ──────────────────────
// The backend issues a short-lived public URL that serves the blueprint raw
// text with permissive CORS — no third-party paste service or server key.
async function createShareUrl(jobId) {
  const res = await api(`/api/v1/jobs/${jobId}/share`, { method: "POST" });
  const data = await res.json();
  const base = await currentApiBase();
  return (base || location.origin) + data.url;
}

// Shared result helpers — the (large) blueprint text is only fetched when the
// user actually views / copies / downloads it, never when the job list renders.
async function getResultText(jobId, format = "blueprint") {
  const cacheKey = `${jobId}:${format}`;
  let text = resultTextCache.get(cacheKey);
  if (text == null) {
    const res = await api(`/api/v1/jobs/${jobId}/result?format=${format}`);
    text = await res.text();
    resultTextCache.set(cacheKey, text);
  }
  return text;
}

async function copyJobResult(jobId) {
  try { await copyText(await getResultText(jobId, "blueprint")); }
  catch (e) { toast(e.message, "error"); }
}

async function downloadJobResult(jobId, name) {
  try {
    const text = await getResultText(jobId, "blueprint");
    downloadText(text, `${name || "result"}.txt`);
  } catch (e) { toast(e.message, "error"); }
}

async function shareJob(jobId) {
  try {
    toast(t("t.pastebinUploading"));
    const url = await createShareUrl(jobId);
    await copyText(url);
    toast(`${t("result.pastebin")} ${url}`);
  } catch (e) { toast(t("t.pastebinFail", { msg: e.message }), "error"); }
}

async function openFBE(jobId) {
  try {
    toast(t("t.pastebinUploading"));
    const url = await createShareUrl(jobId);
    // FBE's naive `?source=` parser splits on '=' and never URL-decodes, so
    // the share URL must be passed RAW (it only contains URL-safe chars).
    window.open(`https://fbe.teoxoy.com/?source=${url}`, "_blank", "noopener");
    // FBE's data is base-game 2.0.68, so the share path remaps signal names it
    // doesn't know — what FBE shows is an adapted preview, not the real bp.
    toast(t("result.fbeWarning"), "warning", 12000);
  } catch (e) { toast(t("t.fbeFail", { msg: e.message }), "error"); }
}

// ── structured viewers ────────────────────────────────────────────────
// Tiny indentation-based YAML parser → { type: 'map'|'list'|'scalar', ... }.
function parseYaml(text) {
  const rows = [];
  for (const raw of text.split("\n")) {
    const t = raw.trim();
    if (!t || t.startsWith("#") || t === "---") continue;
    rows.push({ indent: raw.match(/^\s*/)[0].length, text: t });
  }
  rows.push({ indent: -1, text: "" });
  let i = 0;
  const scalar = (t) => {
    t = t.trim();
    if ((t.startsWith('"') && t.endsWith('"')) || (t.startsWith("'") && t.endsWith("'"))) return { type: "scalar", value: t.slice(1, -1) };
    if (t === "null" || t === "~") return { type: "scalar", value: null };
    if (t === "true" || t === "false") return { type: "scalar", value: t === "true" };
    if (/^-?\d+(\.\d+)?([eE][+-]?\d+)?$/.test(t)) return { type: "scalar", value: Number(t) };
    const h = t.indexOf(" #");
    return { type: "scalar", value: (h >= 0 ? t.slice(0, h) : t).trim() };
  };
  function block(indent) {
    const isList = rows[i].indent === indent && (/^-\s/.test(rows[i].text) || rows[i].text === "-");
    if (isList) {
      const children = [];
      while (rows[i].indent === indent && (/^-\s/.test(rows[i].text) || rows[i].text === "-")) {
        const item = rows[i].text === "-" ? "" : rows[i].text.replace(/^-\s*/, "");
        i++;
        if (item === "") {
          children.push(block(rows[i].indent));
        } else {
          const m = item.match(/^([^:]+):\s*(.*)$/);
          if (m) {
            const key = m[1].trim();
            const rest = m[2].trim();
            if (rest === "") children.push({ type: "map", entries: [{ key, value: block(rows[i].indent) }] });
            else children.push({ type: "map", entries: [{ key, value: scalar(rest) }] });
          } else {
            children.push(scalar(item));
          }
        }
      }
      return { type: "list", children };
    }
    const entries = [];
    while (rows[i].indent === indent && rows[i].text && !/^-\s/.test(rows[i].text)) {
      const m = rows[i].text.match(/^([^:]+):\s*(.*)$/);
      if (!m) { i++; continue; }
      const key = m[1].trim();
      const rest = m[2].trim();
      i++;
      if (rest === "" || /^[|>]/.test(rest)) {
        entries.push({ key, value: block(rows[i].indent) });
      } else {
        entries.push({ key, value: scalar(rest) });
      }
    }
    return { type: "map", entries };
  }
  return block(rows[0].indent);
}

function renderNode(node, label) {
  if (node.type === "scalar") {
    return el("span", { class: "tree-scalar", text: node.value == null ? "null" : String(node.value) });
  }
  const n = node.type === "map" ? (node.entries || []).length : (node.children || []).length;
  const details = el("details", { class: "tree", open: "" });
  details.append(el("summary", { text: label != null ? String(label) : (node.type === "map" ? `{ ${n} }` : `[ ${n} ]`) }));
  const body = el("div", { class: "tree-body" });
  if (node.type === "map") {
    for (const e of node.entries) {
      if (e.value.type === "scalar") {
        const row = el("div", { class: "tree-row" });
        row.append(el("span", { class: "tree-key", text: e.key + ":" }), renderNode(e.value));
        body.append(row);
      } else {
        body.append(renderNode(e.value, e.key));
      }
    }
  } else {
    for (const c of node.children) {
      const row = el("div", { class: "tree-row" });
      row.append(el("span", { class: "tree-idx", text: "-" }));
      if (c.type === "scalar") { row.append(renderNode(c)); body.append(row); }
      else body.append(renderNode(c));
    }
  }
  details.append(body);
  return details;
}

function renderYamlTree(text) {
  try {
    const root = parseYaml(text);
    return el("div", { class: "tree-view" }, [renderNode(root)]);
  } catch (_) {
    return el("pre", { class: "logbox", text });
  }
}

function renderTreeView(obj) {
  const convert = (o, label) => {
    if (o === null || typeof o !== "object") {
      return el("span", { class: "tree-scalar", text: String(o) });
    }
    const isArr = Array.isArray(o);
    const keys = Object.keys(o);
    const details = el("details", { class: "tree", open: "" });
    details.append(el("summary", { text: label != null ? String(label) : (isArr ? `[ ${keys.length} ]` : `{ ${keys.length} }`) }));
    const body = el("div", { class: "tree-body" });
    for (const k of keys) {
      const child = o[k];
      if (child !== null && typeof child === "object") {
        body.append(convert(child, k));
      } else {
        const row = el("div", { class: "tree-row" });
        if (!isArr) row.append(el("span", { class: "tree-key", text: k + ":" }));
        row.append(convert(child, k));
        body.append(row);
      }
    }
    details.append(body);
    return details;
  };
  return el("div", { class: "tree-view" }, [convert(obj)]);
}

// ═══════════════════════════════════════════════════════════════════════
// Jobs (home list)
// ═══════════════════════════════════════════════════════════════════════
async function makeJobPreview(jobId, blob, kind) {
  try {
    const p = await makePreview(blob, kind, 96);
    await saveMedia("preview:" + jobId, p.blob, p.name, p.kind);
    jobPreviewCache.delete(jobId);
    if (!$("#view-home").classList.contains("hidden")) renderHome();
  } catch (_) { /* non-fatal */ }
}

const jobsList = $("#jobs-list");

// Signature of everything the card renders: the job record PLUS UI state that
// changes how the card is drawn.  Polling re-renders a card ONLY when this
// signature changes — so an expansion toggle (or a running job's progress)
// triggers a re-render, while an unchanged job is left completely untouched.
function jobSig(job) {
  const p = job.progress || {};
  return JSON.stringify([
    job.status, job.name, job.created_at, job.started_at, job.finished_at, job.error,
    p.phase, p.log_tail ? p.log_tail.join("\n") : "",
    job.result ? job.result.entity_count : null,
    state.expanded.has(job.job_id) ? 1 : 0,
  ]);
}

async function renderHome() {
  const filter = state.jobsFilter;
  const qs = filter ? `?status=${encodeURIComponent(filter)}` : "";
  let jobs = [];
  try {
    const res = await api(`/api/v1/jobs${qs}`);
    jobs = (await res.json()).jobs || [];
  } catch (e) {
    jobs = [];
  }
  const emptyState = $("#empty-state");
  const listState = $("#list-state");
  const emptyHint = $("#jobs-empty-hint");
  if (jobs.length === 0) {
    // Drop any cards left over from a previous filter.
    jobsList.innerHTML = "";
    state.jobCache.clear();
    state.jobEls.clear();
    if (filter) {
      // A status tab is active: keep the list (and its tabs) visible and
      // show a "no jobs in this status" hint — not the first-run empty state.
      emptyState.classList.add("hidden");
      listState.classList.remove("hidden");
      if (emptyHint) emptyHint.classList.remove("hidden");
    } else {
      // No jobs at all → the first-run "Generate your first blueprint" state.
      emptyState.classList.remove("hidden");
      listState.classList.add("hidden");
    }
    return;
  }
  emptyState.classList.add("hidden");
  listState.classList.remove("hidden");
  if (emptyHint) emptyHint.classList.add("hidden");

  // Diff against what's already on screen: only re-render cards whose record
  // actually changed (e.g. a running job's status/progress), instead of wiping
  // the whole list on every poll.
  //
  // The API returns jobs newest-first (sorted by created_at desc), so the DOM
  // must stay in that order.  A card that already exists is replaced in place;
  // a brand-new card is inserted *before* the first already-rendered card that
  // follows it in the list — a plain append would drop the newest job to the
  // bottom.
  const seen = new Set();
  for (let idx = 0; idx < jobs.length; idx++) {
    const job = jobs[idx];
    seen.add(job.job_id);
    const sig = jobSig(job);
    if (state.jobCache.get(job.job_id) === sig) continue; // unchanged
    state.jobCache.set(job.job_id, sig);
    if (["queued", "running"].includes(job.status)) state.running.add(job.job_id);
    else state.running.delete(job.job_id);
    const card = buildJobCard(job);
    const old = state.jobEls.get(job.job_id);
    state.jobEls.set(job.job_id, card);
    if (old && old.parentNode) {
      old.replaceWith(card);
    } else {
      // New card: insert at the correct newest-first position.
      let ref = null;
      for (let k = idx + 1; k < jobs.length; k++) {
        const laterEl = state.jobEls.get(jobs[k].job_id);
        if (laterEl && laterEl.parentNode) { ref = laterEl; break; }
      }
      if (ref) jobsList.insertBefore(card, ref);
      else jobsList.append(card);
    }
  }
  // Remove cards for jobs that vanished from the (filtered) list.
  for (const [id, el] of state.jobEls) {
    if (!seen.has(id)) {
      if (el.parentNode) el.remove();
      state.jobEls.delete(id);
      state.jobCache.delete(id);
      state.running.delete(id);
    }
  }
}

async function copyJobBlueprint(id) {
  try { await copyText(await getResultText(id, "blueprint")); }
  catch (e) { toast(e.message, "error"); }
}

// Preview the locally cached (compressed) media used for this job.
async function toggleMediaPreview(jobId) {
  const card = $(`[data-job="${jobId}"]`);
  if (!card) return;
  const existing = card.querySelector(".media-preview");
  if (existing) { existing.remove(); return; }
  const box = el("div", { class: "job-detail media-preview" });
  box.append(el("p", { class: "hint", text: t("jobs.loadingMedia") }));
  card.append(box);
  try {
    const cached = await getMedia(jobId);
    if (!cached) {
      box.innerHTML = "";
      box.append(el("p", { class: "hint", text: t("jobs.noCachedMedia") }));
      return;
    }
    const url = URL.createObjectURL(cached.blob);
    let player;
    if (isSoundKind(cached.kind)) player = el("audio", { controls: "", src: url, style: "width:100%" });
    else if (cached.kind === "image") player = el("img", { src: url, style: "max-width:100%" });
    else player = el("video", { controls: "", src: url, style: "max-width:100%" });
    box.innerHTML = "";
    box.append(player);
  } catch (e) {
    box.innerHTML = "";
    box.append(el("p", { class: "hint", text: t("t.previewFailed", { msg: e.message }) }));
  }
}

async function loadJobPreview(card, host, jobId) {
  if (jobPreviewCache.has(jobId)) {
    const entry = jobPreviewCache.get(jobId);
    if (entry && entry.node) { host.classList.remove("hidden"); host.append(entry.node.cloneNode(true)); }
    return;
  }
  try {
    const p = await getMedia("preview:" + jobId);
    if (!p || !p.blob || !p.blob.size) return;
    let node;
    if (p.kind === "video") {
      const url = URL.createObjectURL(p.blob);
      node = el("video", { class: "job-preview-media", muted: "", loop: "", autoplay: "", playsinline: "", preload: "auto", src: url, title: p.name || "" });
    } else if (p.kind === "image") {
      const url = URL.createObjectURL(p.blob);
      node = el("img", { class: "job-preview-media", src: url, title: p.name || "" });
    } else {
      // sound-only job — omit the preview, just note it
      node = el("span", { class: "job-preview-audio", text: t("jobs.soundOnly") });
    }
    jobPreviewCache.set(jobId, { node });
    host.classList.remove("hidden");
    host.append(node.cloneNode(true));
  } catch (_) { /* ignore */ }
}

function buildJobCard(job) {
  const card = el("div", { class: "job-card", dataset: { job: job.job_id } });
  // The preview thumbnail takes the place of the job title. Everything lives
  // inside the clickable .job-head so the whole card toggles expansion.
  const head = el("div", { class: "job-head" });
  const previewHost = el("div", { class: "job-preview hidden" });
  head.append(previewHost);
  const actions = el("span", { class: "row" });
  actions.append(el("button", { text: t("jobs.previewMedia"), onclick: (ev) => { ev.stopPropagation(); toggleMediaPreview(job.job_id); } }));
  if (job.status === "succeeded") {
    actions.append(el("button", { text: t("jobs.copyBlueprint"), onclick: (ev) => { ev.stopPropagation(); copyJobBlueprint(job.job_id); } }));
    actions.append(el("button", { text: t("result.pastebin"), title: "Create a temporary public link", onclick: (ev) => { ev.stopPropagation(); shareJob(job.job_id); } }));
    actions.append(el("button", { text: t("result.fbe"), title: "Open in the Factorio Blueprint Editor", onclick: (ev) => { ev.stopPropagation(); openFBE(job.job_id); } }));
  }
  if (["queued", "running"].includes(job.status)) {
    actions.append(el("button", { class: "danger", text: t("jobs.cancel"), onclick: (ev) => { ev.stopPropagation(); cancelJob(job.job_id); } }));
  }
  if (["succeeded", "failed", "cancelled"].includes(job.status)) {
    actions.append(el("button", { text: t("jobs.reportBug"), title: t("jobs.reportBugTitle"), onclick: (ev) => { ev.stopPropagation(); openReportModal(job.job_id); } }));
  }
  actions.append(el("button", { class: "danger", text: t("jobs.delete"), onclick: (ev) => { ev.stopPropagation(); deleteJob(job.job_id); } }));
  const meta = el("div", { class: "job-meta" }, [
    el("div", { class: "job-meta-row" }, [
      el("span", { class: `badge ${job.status}`, text: job.status }),
      el("span", { class: "job-sub", text: `${fmtRel(job.created_at)} ago` }),
      el("span", { class: "job-sub", text: job.job_id }),
    ]),
    el("div", { class: "job-meta-row job-meta-actions" }, [actions]),
  ]);
  head.append(meta);
  card.append(head);
  loadJobPreview(card, previewHost, job.job_id);

  const expanded = state.expanded.has(job.job_id);
  if (expanded) {
    const detail = el("div", { class: "job-detail" });
    if (job.status === "succeeded") {
      // Always render fresh: the cached clone lost its event handlers and was
      // never re-attached to the card, which made a folded job silently refuse
      // to unfold (until a refresh cleared the cache).
      card.append(detail);
      renderJobResult(detail, job);
    } else if (job.status === "running") {
      const log = job.progress && job.progress.log_tail ? job.progress.log_tail.join("\n") : t("t.working");
      detail.append(el("pre", { class: "logbox", text: log }));
      card.append(detail);
    } else if (job.status === "queued") {
      detail.append(el("p", { class: "hint", text: t("jobs.inQueue") }));
      card.append(detail);
    } else {
      detail.append(el("p", { class: "hint", text: job.error || `status: ${job.status}` }));
      card.append(detail);
    }
  }
  head.addEventListener("click", () => {
    if (state.expanded.has(job.job_id)) state.expanded.delete(job.job_id);
    else state.expanded.add(job.job_id);
    renderHome();
  });
  return card;
}

async function cancelJob(id) {
  try { await api(`/api/v1/jobs/${id}/cancel`, { method: "POST" }); toast(t("jobs.cancelled")); renderHome(); }
  catch (e) { toast(e.message, "error"); }
}

// ── bug report modal (comment / contact) ────────────────────────────
let reportJobId = null;
function openReportModal(jobId) {
  reportJobId = jobId;
  $("#report-comment").value = "";
  $("#report-contact").value = "";
  $("#report-modal").classList.remove("hidden");
  $("#report-comment").focus();
}
function closeReportModal() {
  $("#report-modal").classList.add("hidden");
  reportJobId = null;
}
$("#report-close").addEventListener("click", closeReportModal);
$("#report-cancel").addEventListener("click", closeReportModal);
$("#report-modal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) closeReportModal();
});
$("#report-submit").addEventListener("click", async () => {
  const jobId = reportJobId;
  if (!jobId) return;
  const btn = $("#report-submit");
  btn.disabled = true;
  try {
    const comment = $("#report-comment").value.trim();
    const contact = $("#report-contact").value.trim();
    await api(`/api/v1/jobs/${jobId}/bug-report`, { method: "POST", body: { comment, contact } });
    toast(t("t.bugReported"));
    closeReportModal();
  } catch (e) {
    toast(t("t.bugReportFail", { msg: e.message }), "error");
  } finally {
    btn.disabled = false;
  }
});

async function deleteJob(id) {
  try {
    await api(`/api/v1/jobs/${id}`, { method: "DELETE" });
    state.expanded.delete(id);
    jobPreviewCache.delete(id);
    for (const k of [...resultTextCache.keys()]) if (k.startsWith(id + ":")) resultTextCache.delete(k);
    try { await deleteMedia(id); } catch (_) { /* non-fatal */ }
    try { await deleteMedia("preview:" + id); } catch (_) { /* non-fatal */ }
    renderHome();
  }
  catch (e) { toast(e.message, "error"); }
}

// Status tabs replace the old dropdown + "Refresh" button.  Clicking a tab
// re-filters the list (and re-renders only what changed).
const jobsTabs = $("#jobs-tabs");
jobsTabs.addEventListener("click", (e) => {
  const btn = e.target.closest(".jobs-tab");
  if (!btn) return;
  state.jobsFilter = btn.dataset.status || "";
  $$(".jobs-tab", jobsTabs).forEach((b) => b.classList.toggle("active", b === btn));
  renderHome();
});

// ═══════════════════════════════════════════════════════════════════════
// Polling — while the home view is visible, keep the list in sync without a
// "Refresh" button.  Each tick re-fetches the (cheap) job list and the diff
// renderer in renderHome() updates only cards whose record actually changed.
// Poll faster while jobs are running, slower when idle.
// ═══════════════════════════════════════════════════════════════════════
function startPolling() {
  if (state.pollTimer) return;
  const tick = () => {
    state.pollTimer = setTimeout(async () => {
      state.pollTimer = null;
      if ($("#view-home").classList.contains("hidden")) return;
      await renderHome();
      startPolling(); // reschedule: 2s while running, 4s when idle
    }, state.running.size ? 2000 : 4000);
  };
  tick();
}
function stopPolling() {
  if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; }
}

// boot
document.documentElement.lang = currentLocale();
const localeSelect = $("#locale-select");
if (localeSelect) {
  localeSelect.value = currentLocale();
  localeSelect.addEventListener("change", () => {
    setLocale(localeSelect.value);
    document.documentElement.lang = localeSelect.value;
    applyStaticI18n();
    renderFormatSelectOptions();
    renderHome();
    renderInspector();
  });
}

// About / Acknowledgements — the first-visit page shows only the terms plus
// an OK button; the Backend API panel is a developer-only affordance.
$("#btn-about").addEventListener("click", openAbout);
$("#about-ok").addEventListener("click", () => $("#about-modal").classList.add("hidden"));
$("#about-modal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.classList.add("hidden");
});
async function openAbout() {
  const modal = $("#about-modal");
  const body = $("#about-body");
  modal.classList.remove("hidden");
  body.textContent = "…";
  try {
    // Relative path so it also works when the page is hosted under a
    // GitHub Pages sub-path (e.g. /factorio-displayer/).
    const res = await fetch(`./acknowledgement.${currentLocale()}.txt`);
    if (!res.ok) throw new Error(String(res.status));
    body.textContent = await res.text();
  } catch (_) {
    body.textContent = t("about.fetchFail");
  }
  // Backend config panel — developer-only (hidden for normal users).
  const backend = $("#about-backend");
  if (backend) backend.classList.toggle("hidden", !isDev());
  // Backend config panel — show what's currently in use.
  const input = $("#backend-input");
  const status = $("#backend-status");
  if (input) {
    input.value = configuredApiBase();
    try {
      const base = await currentApiBase();
      status.textContent = base === "" ? t("about.backendLocal") : `${t("about.backendRemote")} ${base}`;
    } catch (_) {
      status.textContent = "";
    }
  }
}
const $backendSave = $("#backend-save");
if ($backendSave) {
  $backendSave.addEventListener("click", async () => {
    setConfiguredApiBase($("#backend-input").value);
    toast(t("about.backendSaved"));
    openAbout();
    renderHome();
  });
}
// Reset the backend override back to auto (local → remote fallback).
const $backendReset = $("#backend-reset");
if ($backendReset) {
  $backendReset.addEventListener("click", async () => {
    setConfiguredApiBase("");
    toast(t("about.backendReset"));
    openAbout();
    renderHome();
  });
}

applyStaticI18n();
renderFormatSelectOptions();
showView("home");
initAuth();

// Show the terms-of-use / acknowledgement automatically on first visit.
// (Skip in the OAuth popup — only the main window should show it.)
if (!window.opener && !localStorage.getItem("fd_ack_seen")) {
  localStorage.setItem("fd_ack_seen", "1");
  setTimeout(openAbout, 400);
}


