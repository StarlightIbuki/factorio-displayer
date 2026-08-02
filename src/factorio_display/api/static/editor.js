// editor.js — in-browser media editor: trim, crop, audio offset, and single-file export.
//
// Exports:
//   attachCropBox(stage, onchange)  — draggable/resizable crop rectangle controller.
//   exportEdited(file, opts, onProgress) — render trimmed/cropped/offset media to ONE file.
//   exportEditedImage(file, opts)    — crop + downscale an image to ONE file.
//   isEditorSupported()

/* eslint-env browser */

export function isEditorSupported() {
  return typeof MediaRecorder !== "undefined" || typeof HTMLCanvasElement !== "undefined";
}

// Frame grain — every edited duration, gap and trim is snapped to this (1/60 s)
// so the timeline and the rendered output stay in sync.
export const FRAME = 1 / 60;
export function snapFrame(sec) {
  if (sec == null || !(sec > 0)) return FRAME;
  return Math.max(FRAME, Math.round(sec / FRAME) * FRAME);
}

function pickVideoMime() {
  const cands = [
    "video/mp4;codecs=avc1.42E01E,mp4a.40.2",
    "video/mp4",
    "video/webm;codecs=vp9",
    "video/webm;codecs=vp8",
    "video/webm",
  ];
  for (const c of cands) if (MediaRecorder.isTypeSupported(c)) return c;
  return "video/webm";
}
function pickAudioMime() {
  const cands = ["audio/mp4", "audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
  for (const c of cands) if (MediaRecorder.isTypeSupported(c)) return c;
  return "audio/webm";
}

// ────────────────────────────────────────────────────────────────────────
// Crop box: normalized {x, y, w, h} (0..1) over a stage that matches the
// media's displayed aspect ratio.
// ────────────────────────────────────────────────────────────────────────
export function attachCropBox(stage, onchange) {
  const box = document.createElement("div");
  box.className = "crop-box hidden";
  stage.appendChild(box);

  let sx = 0, sy = 0, sw = 1, sh = 1;
  let dragging = null;

  const get = () => ({ x: sx, y: sy, w: sw, h: sh });
  const set = (n) => { sx = n.x; sy = n.y; sw = Math.min(1, Math.max(0.05, n.w)); sh = Math.min(1, Math.max(0.05, n.h)); paint(); };
  const reset = () => { sx = sy = 0; sw = sh = 1; paint(); };
  const setVisible = (v) => box.classList.toggle("hidden", !v);

  function paint() {
    box.style.left = `${sx * 100}%`;
    box.style.top = `${sy * 100}%`;
    box.style.width = `${sw * 100}%`;
    box.style.height = `${sh * 100}%`;
  }
  function emit() { if (onchange) onchange(get()); }

  box.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    dragging = { kind: "move", px: e.clientX, py: e.clientY, sx, sy, sw, sh };
    box.setPointerCapture(e.pointerId);
  });
  for (const [dx, dy, cx, cy] of [[0, 0, 0, 0], [1, 0, -1, 0], [0, 1, 0, -1], [1, 1, -1, -1]]) {
    const h = document.createElement("div");
    h.className = "crop-handle";
    h.style.left = `${dx * 100}%`;
    h.style.top = `${dy * 100}%`;
    h.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
      e.preventDefault();
      dragging = { kind: "resize", px: e.clientX, py: e.clientY, sx, sy, sw, sh, cx, cy };
      h.setPointerCapture(e.pointerId);
    });
    box.appendChild(h);
  }

  function move(e) {
    if (!dragging) return;
    const rect = stage.getBoundingClientRect();
    const dx = (e.clientX - dragging.px) / (rect.width || 1);
    const dy = (e.clientY - dragging.py) / (rect.height || 1);
    if (dragging.kind === "move") {
      sx = Math.max(0, Math.min(1 - sw, dragging.sx + dx));
      sy = Math.max(0, Math.min(1 - sh, dragging.sy + dy));
    } else {
      const { cx, cy } = dragging;
      let nsx = dragging.sx, nsy = dragging.sy, nsw = dragging.sw, nsh = dragging.sh;
      if (cx === 0) { nsx = Math.max(0, Math.min(dragging.sx + dragging.sw - 0.05, dragging.sx + dx)); nsw = dragging.sx + dragging.sw - nsx; }
      else { nsw = Math.max(0.05, Math.min(1 - dragging.sx, dragging.sw + dx)); }
      if (cy === 0) { nsy = Math.max(0, Math.min(dragging.sy + dragging.sh - 0.05, dragging.sy + dy)); nsh = dragging.sy + dragging.sh - nsy; }
      else { nsh = Math.max(0.05, Math.min(1 - dragging.sy, dragging.sh + dy)); }
      sx = nsx; sy = nsy; sw = nsw; sh = nsh;
    }
    paint();
    emit();
  }
  function up() { dragging = null; }

  stage.addEventListener("pointermove", move);
  stage.addEventListener("pointerup", up);
  stage.addEventListener("pointercancel", up);
  paint();
  return { get, set, reset, setVisible };
}

// ────────────────────────────────────────────────────────────────────────
// Export edited video/audio to a single file.
// opts: { trimStart, trimEnd, crop:{x,y,w,h}, offset, mute, mode, maxDim }
//   mode: "video-sound" | "video-muted" | "audio"
// Returns { blob, kind, name, width, height, duration }.
// ────────────────────────────────────────────────────────────────────────
export async function exportEdited(file, opts = {}, onProgress) {
  const {
    trimStart = 0,
    trimEnd = 0,
    crop = { x: 0, y: 0, w: 1, h: 1 },
    offset = 0,
    mute = false,
    mode = "video-sound",
    maxDim = 256,
  } = opts;

  const url = URL.createObjectURL(file);
  const video = document.createElement("video");
  video.muted = true;
  video.playsInline = true;
  video.preload = "auto";
  video.src = url;
  await new Promise((res, rej) => {
    video.onloadedmetadata = res;
    video.onerror = () => rej(new Error("Cannot read the media file"));
  });

  const dur = video.duration || 0;
  const end = trimEnd > 0 && trimEnd <= dur ? trimEnd : dur;
  const start = Math.min(Math.max(0, trimStart), Math.max(0, end - 0.1));

  const isAudio = mode === "audio";
  const wantSound = mode === "video-sound" && !mute;

  // output dimensions from the crop region, capped at maxDim
  const srcW = video.videoWidth || 640;
  const srcH = video.videoHeight || 360;
  const cropW = Math.max(1, crop.w * srcW);
  const cropH = Math.max(1, crop.h * srcH);
  const scale = Math.min(1, maxDim / Math.max(cropW, cropH));
  let outW = Math.max(2, Math.round(cropW * scale)); outW += outW % 2;
  let outH = Math.max(2, Math.round(cropH * scale)); outH += outH % 2;

  // ── audio pipeline (AudioContext) ───────────────────────────────
  let audioCtx = null, dest = null, source = null;
  function ensureAudio(mutedOnly) {
    if (!audioCtx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) throw new Error("AudioContext not supported");
      audioCtx = new AC();
    }
    dest = dest || audioCtx.createMediaStreamDestination();
    source = source || audioCtx.createMediaElementSource(video);
    const gain = audioCtx.createGain();
    if (mutedOnly) {
      gain.gain.value = 0;
      source.connect(gain); gain.connect(dest);
      return;
    }
    const delay = audioCtx.createDelay(6);
    delay.delayTime.value = Math.max(0, offset);
    gain.gain.setValueAtTime(0, audioCtx.currentTime);
    if (offset < 0) gain.gain.setValueAtTime(1, audioCtx.currentTime + Math.abs(offset));
    else gain.gain.value = 1;
    source.connect(delay); delay.connect(gain); gain.connect(dest);
  }

  let canvas = null, ctx = null, stream;
  if (isAudio) {
    ensureAudio(mute);
    stream = dest.stream;
  } else {
    canvas = document.createElement("canvas");
    canvas.width = outW;
    canvas.height = outH;
    ctx = canvas.getContext("2d");
    stream = canvas.captureStream(30);
    if (wantSound) {
      ensureAudio(false);
      for (const t of dest.stream.getAudioTracks()) stream.addTrack(t);
    }
  }

  const mime = isAudio ? pickAudioMime() : pickVideoMime();
  const recorder = new MediaRecorder(stream, {
    mimeType: mime,
    videoBitsPerSecond: 1_200_000,
    audioBitsPerSecond: 96_000,
  });
  const chunks = [];
  recorder.ondataavailable = (ev) => { if (ev.data && ev.data.size) chunks.push(ev.data); };
  const stopped = new Promise((res) => { recorder.onstop = res; });
  recorder.start(500);

  function drawFrame() {
    if (!ctx) return;
    const sx = crop.x * srcW;
    const sy = crop.y * srcH;
    const sw = crop.w * srcW;
    const sh = crop.h * srcH;
    ctx.drawImage(video, sx, sy, sw, sh, 0, 0, outW, outH);
  }
  if (ctx && typeof video.requestVideoFrameCallback === "function") {
    const tick = () => {
      drawFrame();
      if (video.currentTime < end && !video.ended) video.requestVideoFrameCallback(tick);
    };
    video.requestVideoFrameCallback(tick);
  }

  video.addEventListener("timeupdate", () => {
    if (video.currentTime >= end) {
      video.pause();
      setTimeout(() => { try { recorder.stop(); } catch (_) { /* already stopped */ } }, 250);
    }
    if (onProgress && end > start) {
      onProgress(Math.min(100, ((video.currentTime - start) / (end - start)) * 100));
    }
  });
  video.addEventListener("ended", () => setTimeout(() => { try { recorder.stop(); } catch (_) { /* noop */ } }, 250));

  video.muted = isAudio ? mute : !wantSound;
  video.currentTime = start;
  await video.play().catch(() => {});
  await stopped;

  URL.revokeObjectURL(url);
  const type = (mime.split(";")[0]) || (isAudio ? "audio/webm" : "video/webm");
  const blob = new Blob(chunks, { type });
  const base = (file.name.replace(/\.[^.]+$/i, "") || "media") + "-edit";
  blob.name = `${base}.webm`;
  return {
    blob,
    kind: isAudio ? "audio" : "video",
    name: blob.name,
    width: isAudio ? null : outW,
    height: isAudio ? null : outH,
    duration: Math.max(0, end - start),
  };
}

// ────────────────────────────────────────────────────────────────────────
// Crop + downscale an image to a single file.
// ────────────────────────────────────────────────────────────────────────
export function exportEditedImage(file, crop = { x: 0, y: 0, w: 1, h: 1 }, maxDim = 256) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const cw = Math.max(1, crop.w * img.naturalWidth);
      const ch = Math.max(1, crop.h * img.naturalHeight);
      const scale = Math.min(1, maxDim / Math.max(cw, ch));
      const w = Math.max(1, Math.round(cw * scale));
      const h = Math.max(1, Math.round(ch * scale));
      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext("2d");
      ctx.drawImage(img, crop.x * img.naturalWidth, crop.y * img.naturalHeight, cw, ch, 0, 0, w, h);
      URL.revokeObjectURL(url);
      canvas.toBlob((blob) => {
        if (!blob) return reject(new Error("Image export failed"));
        blob.name = (file.name.replace(/\.[^.]+$/i, "") || "image") + "-edit.png";
        resolve({ blob, kind: "image", name: blob.name, width: w, height: h, duration: 0 });
      }, "image/png");
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("Cannot read the image")); };
    img.src = url;
  });
}

// ────────────────────────────────────────────────────────────────────────
// Concatenate multiple edited clips into ONE file.
// clips: [{ file, name, kind, edit: { trimStart, trimEnd, crop, offset, mute, duration? } }]
//   * trim/crop/offset/mute are applied per clip;
//   * image clips become a static frame of `edit.duration` seconds (default 1);
//   * audio-only clips play audio with a black frame in video modes.
// opts: { mode: "video-sound" | "video-muted" | "audio", maxDim }
// Returns { blob, kind, name, width, height, duration }.
// ────────────────────────────────────────────────────────────────────────
function loadMeta(file) {
  return new Promise((resolve, reject) => {
    const isImg = /\.(png|jpg|jpeg|bmp|tiff|tif|gif)$/i.test(file.name);
    if (isImg) {
      const img = new Image();
      const url = URL.createObjectURL(file);
      img.onload = () => { URL.revokeObjectURL(url); resolve({ w: img.naturalWidth, h: img.naturalHeight, dur: 0, image: img }); };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("Cannot read image")); };
      img.src = url;
      return;
    }
    // MIDI can't be decoded by <video>/<audio>; give it a nominal duration
    // (matching app.js probeClip) — the real length is determined server-side.
    if (/\.(mid|midi)$/i.test(file.name)) { resolve({ w: 0, h: 0, dur: 30 }); return; }
    const v = document.createElement("video");
    v.muted = true;
    v.preload = "metadata";
    const url = URL.createObjectURL(file);
    v.onloadedmetadata = () => { URL.revokeObjectURL(url); resolve({ w: v.videoWidth, h: v.videoHeight, dur: v.duration || 0 }); };
    v.onerror = () => { URL.revokeObjectURL(url); reject(new Error("Cannot read media")); };
    v.src = url;
  });
}

function fitDraw(ctx, src, sx, sy, sw, sh, w, h) {
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, w, h);
  const s = Math.min(w / sw, h / sh);
  const dw = sw * s;
  const dh = sh * s;
  ctx.drawImage(src, sx, sy, sw, sh, (w - dw) / 2, (h - dh) / 2, dw, dh);
}

export async function exportConcatenated(clips, opts = {}, onProgress) {
  const { mode = "video-sound", maxDim = 256 } = opts;
  const isAudio = mode === "audio";
  const wantSound = mode === "video-sound";
  if (!clips.length) throw new Error("No clips to export");
  // Ensure every clip has a unique id so probe metadata doesn't collide.
  for (let i = 0; i < clips.length; i++) if (clips[i].id == null) clips[i].id = `auto${i}`;

  // video/image clips play on the video timeline; audio-only clips (and MIDI,
  // which can't be decoded in-browser and is uploaded raw to the backend) are
  // placed on the audio rail at an absolute start time (edit.start).
  const visuals = [];
  const overlays = [];
  for (const c of clips) ((c.kind === "audio" || c.kind === "midi") ? overlays : visuals).push(c);

  const prep = new Map();
  async function probe(c) {
    if (prep.has(c.id)) return prep.get(c.id);
    const meta = await loadMeta(c.file);
    const edit = c.edit || {};
    const crop = edit.crop || { x: 0, y: 0, w: 1, h: 1 };
    let dur;
    if (meta.dur === 0) dur = snapFrame(Math.max(FRAME, edit.duration || 1));
    else {
      const start = Math.max(0, edit.trimStart || 0);
      const end = edit.trimEnd > 0 && edit.trimEnd <= meta.dur ? edit.trimEnd : meta.dur;
      dur = snapFrame(Math.max(FRAME, end - start));
    }
    const p = { c, meta, crop, dur, id: c.id };
    prep.set(c.id, p);
    return p;
  }
  for (const c of clips) await probe(c);

  const visualList = visuals.map((c) => prep.get(c.id));
  const overlayList = overlays.map((c) => prep.get(c.id));

  // Video clips play in order; an explicit edit.start positions a clip at an
  // absolute time (ripple: later clips follow). Gaps are inserted as needed.
  const visualStarts = [];
  let cursor = 0;
  for (const p of visualList) {
    const es = p.c.edit && p.c.edit.start != null && p.c.edit.start >= 0 ? p.c.edit.start : null;
    const s = es != null ? Math.max(es, cursor) : cursor;
    visualStarts.push(s);
    cursor = s + p.dur;
  }
  const videoEnd = visualList.length ? visualStarts[visualStarts.length - 1] + visualList[visualList.length - 1].dur : 0;
  const overlayEnd = overlayList.reduce((s, p) => Math.max(s, (p.c.edit.start || 0) + p.dur), 0);
  const totalDur = Math.max(videoEnd, overlayEnd);

  // output canvas = largest visual crop, capped at maxDim
  let outW = 2;
  let outH = 2;
  for (const p of visualList) {
    outW = Math.max(outW, Math.round(p.crop.w * (p.meta.w || 1)));
    outH = Math.max(outH, Math.round(p.crop.h * (p.meta.h || 1)));
  }
  const scale = Math.min(1, maxDim / Math.max(outW, outH));
  outW = Math.max(2, Math.round(outW * scale)); outW += outW % 2;
  outH = Math.max(2, Math.round(outH * scale)); outH += outH % 2;

  // shared <video> element for the visual timeline + persistent audio graph
  const video = document.createElement("video");
  video.muted = mode === "video-muted";
  video.playsInline = true;
  video.preload = "auto";

  let audioCtx = null;
  let dest = null;
  let source = null;
  let delay = null;
  let gain = null;
  function setupAudio() {
    if (audioCtx) return;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) throw new Error("AudioContext not supported");
    audioCtx = new AC();
    dest = audioCtx.createMediaStreamDestination();
    source = audioCtx.createMediaElementSource(video);
    delay = audioCtx.createDelay(6);
    gain = audioCtx.createGain();
    source.connect(delay);
    delay.connect(gain);
    gain.connect(dest);
  }

  // audio-only overlays get their own <audio> element routed to the dest
  const overlayNodes = [];
  function setupOverlay(p) {
    const el = document.createElement("audio");
    el.preload = "auto";
    const url = URL.createObjectURL(p.c.file);
    el.src = url;
    const n = audioCtx.createMediaElementSource(el);
    const g = audioCtx.createGain();
    n.connect(g);
    g.connect(dest);
    overlayNodes.push({ el, url, gain: g, p });
  }

  let canvas = null;
  let ctx = null;
  let stream;
  if (isAudio) {
    setupAudio();
    stream = dest.stream;
  } else {
    canvas = document.createElement("canvas");
    canvas.width = outW;
    canvas.height = outH;
    ctx = canvas.getContext("2d");
    // Prime the canvas (must be painted before captureStream) so frames flow.
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, outW, outH);
    stream = canvas.captureStream(30);
    if (wantSound) {
      setupAudio();
      for (const t of dest.stream.getAudioTracks()) stream.addTrack(t);
    }
  }

  const mime = isAudio ? pickAudioMime() : pickVideoMime();
  const recorder = new MediaRecorder(stream, {
    mimeType: mime, videoBitsPerSecond: 1_200_000, audioBitsPerSecond: 96_000,
  });
  const chunks = [];
  recorder.ondataavailable = (ev) => { if (ev.data && ev.data.size) chunks.push(ev.data); };
  const stopped = new Promise((res) => { recorder.onstop = res; });
  recorder.start(500);

  let elapsed = 0;
  let currentUrl = null;

  function report() {
    if (onProgress && totalDur > 0) onProgress(Math.min(100, (elapsed / totalDur) * 100));
  }

  // start an audio overlay (trimmed to its range) now
  function startOverlay(node, trimStart) {
    if (node.started) return;
    node.started = true;
    const e = node.p.c.edit || {};
    node.gain.gain.value = e.mute ? 0 : 1;
    node.el.currentTime = trimStart;
    node.el.play().catch(() => {});
  }

  async function playVisual(p) {
    const edit = p.c.edit || {};
    if (currentUrl) URL.revokeObjectURL(currentUrl);
    currentUrl = URL.createObjectURL(p.c.file);
    video.src = currentUrl;
    await new Promise((res, rej) => { video.onloadedmetadata = res; video.onerror = () => rej(new Error("Cannot load " + p.c.name)); });

    // per-clip audio settings on the shared graph
    if (audioCtx) {
      const off = Math.max(0, edit.offset || 0);
      delay.delayTime.value = off;
      gain.gain.cancelScheduledValues(audioCtx.currentTime);
      gain.gain.setValueAtTime(0, audioCtx.currentTime);
      if (edit.mute) gain.gain.value = 0;
      else if ((edit.offset || 0) < 0) gain.gain.setValueAtTime(1, audioCtx.currentTime + Math.abs(edit.offset));
      else gain.gain.value = 1;
    }

    const start = Math.min(edit.trimStart || 0, Math.max(0, p.dur - 0.1));
    const end = p.dur;
    const hasVideo = (video.videoWidth || 0) > 0 && ctx;
    video.currentTime = start;

    await new Promise((resolve) => {
      const onTime = () => {
        if (video.currentTime >= end) {
          video.removeEventListener("timeupdate", onTime);
          video.pause();
          elapsed += p.dur;
          report();
          resolve();
        }
      };
      if (hasVideo && typeof video.requestVideoFrameCallback === "function") {
        const tick = () => {
          if (ctx) fitDraw(ctx, video, edit.crop.x * video.videoWidth, edit.crop.y * video.videoHeight, edit.crop.w * video.videoWidth, edit.crop.h * video.videoHeight, outW, outH);
          if (video.currentTime < end && !video.ended) video.requestVideoFrameCallback(tick);
        };
        video.requestVideoFrameCallback(tick);
      } else if (ctx) {
        ctx.fillStyle = "#000";
        ctx.fillRect(0, 0, outW, outH);
      }
      video.addEventListener("timeupdate", onTime);
      video.play().catch(() => { onTime(); });
    });
  }

  async function playImage(p) {
    if (ctx) fitDraw(ctx, p.meta.image, p.crop.x * p.meta.w, p.crop.y * p.meta.h, p.crop.w * p.meta.w, p.crop.h * p.meta.h, outW, outH);
    await new Promise((resolve) => {
      const t0 = performance.now();
      const iv = setInterval(() => {
        if (ctx) fitDraw(ctx, p.meta.image, p.crop.x * p.meta.w, p.crop.y * p.meta.h, p.crop.w * p.meta.w, p.crop.h * p.meta.h, outW, outH);
        if (performance.now() - t0 >= p.dur * 1000) { clearInterval(iv); resolve(); }
      }, 40);
    });
    elapsed += p.dur;
    report();
  }

  // schedule overlays whose start falls within [elapsed, elapsed+dur)
  function scheduleOverlays(dur) {
    if (!audioCtx) return;
    const segStart = elapsed;
    for (const node of overlayNodes) {
      if (node.started) continue;
      const s = node.p.c.edit.start || 0;
      if (s <= segStart) startOverlay(node, node.p.c.edit.trimStart || 0);
      else if (s < segStart + dur) {
        setTimeout(() => startOverlay(node, node.p.c.edit.trimStart || 0), (s - segStart) * 1000);
      }
    }
  }

  // advance elapsed to target, starting any overlays that begin in the gap and
  // painting blank (black) frames so gaps between clips render as black.
  async function waitGap(target) {
    if (target <= elapsed) return;
    for (const node of overlayNodes) {
      if (node.started) continue;
      const os = node.p.c.edit.start || 0;
      if (os >= elapsed && os < target) {
        setTimeout(() => startOverlay(node, node.p.c.edit.trimStart || 0), (os - elapsed) * 1000);
      }
    }
    const endT = performance.now() + Math.max(0, target - elapsed) * 1000;
    await new Promise((resolve) => {
      const draw = () => {
        if (ctx) {
          ctx.fillStyle = "#000";
          ctx.fillRect(0, 0, outW, outH);
        }
        if (performance.now() < endT) setTimeout(draw, 33);
        else resolve();
      };
      draw();
    });
    elapsed = target;
  }

  try {
    if (audioCtx) {
      for (const p of overlayList) setupOverlay(p);
      // start overlays that begin at 0
      for (const node of overlayNodes) if ((node.p.c.edit.start || 0) === 0) startOverlay(node, node.p.c.edit.trimStart || 0);
    }
    for (let i = 0; i < visualList.length; i++) {
      const p = visualList[i];
      await waitGap(visualStarts[i]);
      scheduleOverlays(p.dur);
      if (p.c.kind === "image") await playImage(p);
      else await playVisual(p);
    }
    // wait for the audio tail (overlays extending beyond the video)
    if (totalDur > elapsed) {
      for (const node of overlayNodes) {
        if (!node.started) {
          const s = node.p.c.edit.start || 0;
          if (s >= elapsed) {
            setTimeout(() => startOverlay(node, node.p.c.edit.trimStart || 0), Math.max(0, s - elapsed) * 1000);
          } else {
            startOverlay(node, node.p.c.edit.trimStart || 0);
          }
        }
      }
      await new Promise((res) => setTimeout(res, Math.max(0, totalDur - elapsed) * 1000));
    }
  } finally {
    if (currentUrl) URL.revokeObjectURL(currentUrl);
    for (const n of overlayNodes) URL.revokeObjectURL(n.url);
    setTimeout(() => { try { recorder.stop(); } catch (_) { /* already stopped */ } }, 250);
  }
  await stopped;

  const type = (mime.split(";")[0]) || (isAudio ? "audio/webm" : "video/webm");
  const blob = new Blob(chunks, { type });
  blob.name = `concat${isAudio ? "-audio" : ""}.${type.includes("mp4") ? "mp4" : "webm"}`;
  return {
    blob, kind: isAudio ? "audio" : "video",
    name: blob.name, width: isAudio ? null : outW, height: isAudio ? null : outH,
    duration: totalDur,
  };
}

// ────────────────────────────────────────────────────────────────────────
// Build a compact ~1-second, low-res preview clip used to identify a job
// in the job list. Returns { blob, kind: "video" | "audio" | "image" }.
// ────────────────────────────────────────────────────────────────────────
export async function makePreview(blob, kind, maxDim = 96) {
  const url = URL.createObjectURL(blob);
  try {
    if (kind === "image") {
      return await new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => {
          const s = Math.min(1, maxDim / Math.max(img.naturalWidth, img.naturalHeight));
          const w = Math.max(1, Math.round(img.naturalWidth * s));
          const h = Math.max(1, Math.round(img.naturalHeight * s));
          const canvas = document.createElement("canvas");
          canvas.width = w;
          canvas.height = h;
          const ctx = canvas.getContext("2d");
          ctx.drawImage(img, 0, 0, w, h);
          canvas.toBlob((b) => {
            if (!b) return reject(new Error("thumbnail failed"));
            b.name = "preview.jpg";
            resolve({ blob: b, kind: "image" });
          }, "image/jpeg", 0.8);
        };
        img.onerror = () => reject(new Error("cannot read image"));
        img.src = url;
      });
    }

    if (kind === "audio" || kind === "midi") {
      // Reliable 1s preview: decode the audio and re-encode its first second
      // as a small WAV (no playback/autoplay needed). Falls back to the
      // original file if decoding isn't possible (e.g. MIDI).
      try {
        const ab = await blob.arrayBuffer();
        const AC = window.AudioContext || window.webkitAudioContext;
        const ac = new AC();
        const buf = await ac.decodeAudioData(ab);
        const sampleRate = buf.sampleRate || 44100;
        const channels = buf.numberOfChannels || 1;
        const n = Math.min(buf.length, sampleRate); // 1 second
        const bytesPerSample = 2;
        const dataSize = n * channels * bytesPerSample;
        const raw = new ArrayBuffer(44 + dataSize);
        const view = new DataView(raw);
        const wstr = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
        wstr(0, "RIFF"); view.setUint32(4, 36 + dataSize, true); wstr(8, "WAVE");
        wstr(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
        view.setUint16(22, channels, true);
        view.setUint32(24, sampleRate, true);
        view.setUint32(28, sampleRate * channels * bytesPerSample, true);
        view.setUint16(32, channels * bytesPerSample, true);
        view.setUint16(34, 16, true);
        wstr(36, "data"); view.setUint32(40, dataSize, true);
        const ch = [];
        for (let c = 0; c < channels; c++) ch.push(buf.getChannelData(c));
        let off = 44;
        for (let i = 0; i < n; i++) {
          for (let c = 0; c < channels; c++) {
            const s = Math.max(-1, Math.min(1, ch[c][i]));
            view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
            off += 2;
          }
        }
        const wav = new Blob([raw], { type: "audio/wav" });
        wav.name = "preview.wav";
        return { blob: wav, kind: "audio" };
      } catch (e) {
        const b = blob;
        b.name = b.name || "preview";
        return { blob: b, kind: "audio" };
      }
    }

    // video preview: first ~1 second, downscaled, muted
    const el = document.createElement("video");
    el.playsInline = true;
    el.muted = true;
    el.preload = "auto";
    el.src = url;
    await new Promise((res, rej) => { el.onloadedmetadata = res; el.onerror = () => rej(new Error("cannot read media")); });

    let stream;
    let drawTick = null;
    {
      const sw = el.videoWidth || 160;
      const sh = el.videoHeight || 90;
      const s = Math.min(1, maxDim / Math.max(sw, sh));
      let w = Math.max(2, Math.round(sw * s)); w += w % 2;
      let h = Math.max(2, Math.round(sh * s)); h += h % 2;
      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#000";
      ctx.fillRect(0, 0, w, h); // prime the canvas before captureStream
      stream = canvas.captureStream(15);
      const draw = () => { if (ctx && el.videoWidth) { try { ctx.drawImage(el, 0, 0, w, h); } catch (_) { /* noop */ } } };
      draw();
      drawTick = setInterval(draw, 66);
    }

    const mime = pickVideoMime();
    const recorder = new MediaRecorder(stream, {
      mimeType: mime,
      videoBitsPerSecond: 320_000,
      audioBitsPerSecond: 48_000,
    });
    const chunks = [];
    recorder.ondataavailable = (ev) => { if (ev.data && ev.data.size) chunks.push(ev.data); };
    const stopped = new Promise((res) => { recorder.onstop = res; });
    recorder.start(250);

    el.currentTime = 0;
    const t0 = performance.now();
    await el.play().catch(() => {});
    await new Promise((resolve) => {
      const check = () => {
        if (el.ended || performance.now() - t0 >= 1000) resolve();
        else setTimeout(check, 80);
      };
      check();
    });
    if (drawTick) clearInterval(drawTick);
    el.pause();
    setTimeout(() => { try { recorder.stop(); } catch (_) { /* already stopped */ } }, 100);
    await stopped;

    const type = mime.split(";")[0] || "video/webm";
    const out = new Blob(chunks, { type });
    out.name = `preview.${type.includes("mp4") ? "mp4" : "webm"}`;
    if (!out.size) throw new Error("empty preview");
    return { blob: out, kind: "video" };
  } finally {
    URL.revokeObjectURL(url);
  }
}
