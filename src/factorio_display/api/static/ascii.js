// ascii.js — ASCII-art blueprint debugging view.
//
// The ASCII rendering (entity map + per-colour wiring maps) is computed by
// the backend (ascii_render.py) and exposed at /api/v1/blueprints/ascii, so
// this module only handles the request and building the <pre> element.

/* eslint-env browser */

/**
 * Request the ASCII-art rendering of a blueprint string from the backend.
 *
 * @param {Function} apiFn the app's `api(path, options)` helper.
 * @param {string} bpString raw blueprint string.
 * @returns {Promise<string>} the ASCII art text.
 */
export async function blueprintAscii(apiFn, bpString) {
  const res = await apiFn("/api/v1/blueprints/ascii", {
    method: "POST",
    body: { blueprint: bpString },
  });
  const obj = await res.json().catch(() => ({}));
  return obj.text || "";
}

/**
 * Build a monospace <pre> containing ASCII art (scrollable, no wrap).
 * @param {string} text
 * @returns {HTMLPreElement}
 */
export function asciiPre(text) {
  const pre = document.createElement("pre");
  pre.className = "mono ascii";
  pre.textContent = text;
  return pre;
}
