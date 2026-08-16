// compress.js — in-browser lossy video compression.
//
// Re-encodes a video lossily and downscales it to a small max dimension
// (the backend display is tiny, so extra source resolution is discarded
// server-side anyway). Uses <video> → <canvas> → captureStream →
// MediaRecorder (VP9/VP8/WebM), so it needs no demuxer and works in any
// browser with MediaRecorder support.
//
// Runs on the main thread (canvas/MediaRecorder are DOM APIs); playback
// and recording are async so the UI stays responsive via onProgress.

/* eslint-env browser */

const QUALITY = {
  high: { bitrate: 1_200_000 },   // 256px-ish, ~1.2 Mbps
  medium: { bitrate: 700_000 },   // ~700 kbps
  low: { bitrate: 400_000 },      // ~400 kbps
};

export function isCompressionSupported() {
  return (
    typeof HTMLVideoElement !== "undefined" &&
    typeof HTMLVideoElement.prototype.captureStream === "function" &&
    typeof MediaRecorder !== "undefined"
  );
}

function pickMime() {
  const candidates = [
    "video/mp4;codecs=avc1.42E01E,mp4a.40.2",
    "video/mp4",
    "video/webm;codecs=vp9",
    "video/webm;codecs=vp8",
    "video/webm",
  ];
  for (const c of candidates) {
    if (MediaRecorder.isTypeSupported(c)) return c;
  }
  return "video/webm";
}

/**
 * Compress *file* to a lossy WebM blob, downscaled to at most *maxDim* px.
 * @param {File} file
 * @param {{quality?: string, maxDim?: number, onProgress?: (pct:number)=>void}} options
 * @returns {Promise<Blob>}
 */
export async function compressVideo(file, { quality = "medium", maxDim = 256, onProgress } = {}) {
  if (!isCompressionSupported()) {
    throw new Error("Video compression is not supported in this browser");
  }
  const q = QUALITY[quality] || QUALITY.medium;
  const url = URL.createObjectURL(file);

  const video = document.createElement("video");
  video.muted = true;
  video.playsInline = true;
  video.preload = "auto";
  video.src = url;

  await new Promise((resolve, reject) => {
    video.onloadedmetadata = () => resolve();
    video.onerror = () => reject(new Error("Could not read the video file"));
  });

  const sw = video.videoWidth || 640;
  const sh = video.videoHeight || 360;
  const scale = Math.min(1, (maxDim || 256) / Math.max(sw, sh));
  let w = Math.max(2, Math.round(sw * scale));
  let h = Math.max(2, Math.round(sh * scale));
  w += w % 2; // even dimensions for codecs
  h += h % 2;

  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, w, h); // prime the canvas before captureStream

  const stream = canvas.captureStream(30);
  // Canvas capture is video-only: carry the SOURCE's audio tracks into the
  // recorder's stream so a "video with sound" upload keeps its soundtrack
  // (previously the compressed upload was always silent).
  if (typeof video.captureStream === "function") {
    const srcStream = video.captureStream();
    for (const t of srcStream.getAudioTracks()) stream.addTrack(t);
  }
  const mimeType = pickMime();
  const recorder = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: q.bitrate });
  const chunks = [];
  recorder.ondataavailable = (ev) => { if (ev.data && ev.data.size) chunks.push(ev.data); };
  const stopped = new Promise((resolve, reject) => {
    recorder.onstop = resolve;
    recorder.onerror = () => reject(new Error("MediaRecorder failed while compressing the video"));
  });
  // Hang guard: if the video never ends (autoplay blocked, codec issue,
  // seek error), stop the recorder after duration + 15 s so the caller's
  // await settles instead of blocking the whole cache queue forever.
  const hangTimer = setTimeout(() => {
    if (recorder.state !== "inactive") {
      try { recorder.stop(); } catch (_e) { /* already stopped */ }
    }
  }, ((video.duration || 60) * 1000) + 15000);

  recorder.start(500);

  const draw = () => ctx.drawImage(video, 0, 0, w, h);
  if (typeof video.requestVideoFrameCallback === "function") {
    const tick = () => {
      draw();
      if (!video.ended) video.requestVideoFrameCallback(tick);
    };
    video.requestVideoFrameCallback(tick);
  } else {
    const iv = setInterval(() => { draw(); if (video.ended) clearInterval(iv); }, 50);
  }

  if (onProgress) {
    video.addEventListener("timeupdate", () => {
      if (video.duration) onProgress(Math.min(100, (video.currentTime / video.duration) * 100));
    });
  }

  video.addEventListener("ended", () => setTimeout(() => recorder.stop(), 300));

  await video.play().catch(() => { /* muted autoplay is generally allowed */ });
  await stopped;
  clearTimeout(hangTimer);
  URL.revokeObjectURL(url);

  const blob = new Blob(chunks, { type: "video/webm" });
  if (!blob.size) {
    // Nothing was recorded (e.g. the video never played and the hang guard
    // stopped an empty recording) — let the caller keep the original file.
    throw new Error("Video compression produced no data");
  }
  blob.name = file.name.replace(/\.[^.]+$/i, "") + ".webm";
  return blob;
}
