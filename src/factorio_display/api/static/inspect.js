// inspect.js — unified Blueprint Inspector modal.
//
// A single modal is used both for the job's blueprint inspection and the
// topbar "Blueprint viewer" utility.  It shows four tabs:
//   * Blueprint — the raw string (+ copy).
//   * Inspect   — the YAML tree (backend decode).
//   * ASCII     — the ASCII render split into pages (Entities / RED / GREEN…).
//   * Preview   — an interactive SVG: combinator outlines, red/green wire
//                 overlays, network-coloured ports, hover-to-highlight.
//
// The structured model + ASCII text come from POST /api/v1/blueprints/render,
// so the preview is always consistent with the ASCII maps.

/* eslint-env browser */
import { t } from "./i18n.js";

// ── tiny DOM helpers (kept local; the app's helpers are not exported) ──
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

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

// ── colour palette for networks (ports are coloured by network) ────────
const NET_COLORS = [
  "#e5484d", "#46a758", "#f5a623", "#5b8def", "#e93d82", "#8e4ec6",
  "#00a2ae", "#ff7d45", "#b6e34d", "#9d9d9d", "#f76b15", "#00d4ff",
  "#ffd166", "#e066ff", "#2b6cb0", "#7ddc8f", "#d4a373", "#00b5ad",
];
function netColor(idx) {
  return NET_COLORS[idx % NET_COLORS.length];
}

// ── ASCII → pages ─────────────────────────────────────────────────────
// Group the ASCII text into sections by their "=== Title ===" headers so
// each map (entities / RED / GREEN…) becomes its own page.
function splitAsciiPages(text) {
  const lines = String(text || "").split("\n");
  const pages = [];
  let cur = null;
  for (const line of lines) {
    const m = line.trim().match(/^=== (.+) ===$/);
    if (m) {
      if (cur) pages.push(cur);
      cur = { title: m[1].trim(), text: line };
    } else if (cur) {
      cur.text += "\n" + line;
    }
  }
  if (cur) pages.push(cur);
  return pages;
}

// ── SVG preview ───────────────────────────────────────────────────────
const TILE = 26;
const PAD = 22;

// Port anchor in tile coordinates for an entity, per side.
function portAnchor(ent, side) {
  const x = ent.x, y = ent.y;
  if (ent.kind === "combinator") {
    if (side === "output") {
      if (ent.dir === 4) return [x + 1, y + 1];      // east → output right
      if (ent.dir === 12) return [x, y + 1];         // west → output left
      if (ent.dir === 0) return [x + 0.5, y];        // north → output top
      return [x + 0.5, y + 2];                       // south → output bottom
    }
    if (ent.dir === 4) return [x, y + 1];            // input left
    if (ent.dir === 12) return [x + 1, y + 1];       // input right
    if (ent.dir === 0) return [x + 0.5, y + 2];      // input bottom
    return [x + 0.5, y];                             // input top
  }
  if (ent.kind === "one") return side === "input" ? [x + 0.5, y + 1] : null; // lamp/speaker
  if (ent.kind === "cc") return side === "output" ? [x + 0.5, y + 1] : null; // constant
  return null;
}

function buildPreview(model) {
  const minX = model.min_x, minY = model.min_y;
  const w = (model.max_x - model.min_x + 2) * TILE + PAD * 2;
  const h = (model.max_y - model.min_y + 3) * TILE + PAD * 2;
  const toX = (tx) => (tx - minX) * TILE + PAD;
  const toY = (ty) => (ty - minY) * TILE + PAD;

  const svg = svgEl("svg", { class: "preview-svg", viewBox: `0 0 ${w} ${h}` });

  // Wires behind entities.
  for (const [pa, pb] of model.wires || []) {
    const ea = model.entities[pa.entity], eb = model.entities[pb.entity];
    if (!ea || !eb) continue;
    const a = portAnchor(ea, pa.side), b = portAnchor(eb, pb.side);
    if (!a || !b) continue;
    const netIdx = model.ports[pa.entity][pa.color][pa.side];
    svg.append(svgEl("line", {
      class: "wire", "data-net": netIdx,
      x1: toX(a[0]), y1: toY(a[1]), x2: toX(b[0]), y2: toY(b[1]),
      stroke: pa.color === "red" ? "#ff5c5c" : "#46d160",
      "stroke-width": 2, "stroke-opacity": 0.65,
    }));
  }

  model.entities.forEach((ent, i) => {
    const nets = [];
    const p = model.ports[i];
    for (const c of ["red", "green"]) for (const s of ["input", "output"]) {
      if (p[c][s] >= 0) nets.push(p[c][s]);
    }
    const boxW = 1;
    const boxH = ent.kind === "combinator" ? 2 : 1;
    const rx = toX(ent.x), ry = toY(ent.y);

    const g = svgEl("g", { class: "ent", "data-nets": nets.join(",") });
    g.append(svgEl("rect", {
      class: "ent-box",
      x: rx + 1, y: ry + 1, width: boxW * TILE - 2, height: boxH * TILE - 2,
      rx: 4, fill: "rgba(255,255,255,0.05)", stroke: "rgba(255,255,255,0.45)",
    }));
    const letter = svgEl("text", {
      class: "ent-letter",
      x: rx + (boxW * TILE) / 2, y: ry + (boxH * TILE) / 2 + 4,
      "text-anchor": "middle",
    });
    letter.textContent = ent.letter;
    g.append(letter);

    for (const c of ["red", "green"]) for (const s of ["input", "output"]) {
      const n = p[c][s];
      if (n < 0) continue;
      const anchor = portAnchor(ent, s);
      if (!anchor) continue;
      g.append(svgEl("circle", {
        class: "port", "data-net": n, cx: toX(anchor[0]), cy: toY(anchor[1]), r: 4,
        fill: netColor(n), stroke: "#0b0e11", "stroke-width": 1,
      }));
    }
    svg.append(g);
  });

  // Hover → highlight all ports / wires / entities in the same network(s).
  svg.addEventListener("mouseover", (e) => {
    const target = e.target.closest(".port, .ent");
    if (!target) { clearHighlight(svg); return; }
    const nets = target.classList.contains("port")
      ? [target.dataset.net]
      : (target.dataset.nets || "").split(",").filter(Boolean);
    highlight(svg, nets);
  });
  svg.addEventListener("mouseleave", () => clearHighlight(svg));
  return svg;
}

function highlight(svg, nets) {
  const set = new Set(nets);
  svg.querySelectorAll(".port, .wire, .ent").forEach((node) => {
    let mine;
    if (node.classList.contains("port") || node.classList.contains("wire")) {
      mine = set.has(node.dataset.net);
    } else {
      mine = (node.dataset.nets || "").split(",").some((n) => set.has(n));
    }
    node.classList.toggle("hl", mine);
    node.classList.toggle("dim", !mine);
  });
}
function clearHighlight(svg) {
  svg.querySelectorAll(".port, .wire, .ent").forEach((node) => node.classList.remove("hl", "dim"));
}

function previewView(model) {
  const wrap = el("div");
  const toolbar = el("div", { class: "preview-toolbar" });
  const showWires = el("label", { class: "row", style: "gap:6px;align-items:center" }, [
    el("input", { type: "checkbox", checked: "", id: "preview-wires" }),
    el("span", { text: t("preview.showWires") }),
  ]);
  toolbar.append(showWires);
  wrap.append(toolbar);

  const body = el("div");
  const svg = buildPreview(model);
  body.append(svg);

  // legend
  const legend = el("div", { class: "preview-legend" });
  (model.networks || []).forEach((net, idx) => {
    legend.append(el("span", { class: "preview-legend-item" }, [
      el("span", { class: "swatch", style: `background:${netColor(idx)}` }),
      el("span", { text: `${net.char} ${net.color} · ${net.endpoints.length} pt` }),
    ]));
  });
  wrap.append(body, legend);

  $("#preview-wires", wrap).addEventListener("change", (e) => {
    body.querySelectorAll(".wire").forEach((w) => w.classList.toggle("hidden", !e.target.checked));
  });
  return wrap;
}

// ── ASCII pages view ──────────────────────────────────────────────────
function asciiView(pages) {
  const wrap = el("div");
  const tabs = el("div", { class: "result-tabs ascii-pages" });
  const host = el("div");
  const show = (i) => {
    $$("[data-page]", tabs).forEach((b) => b.classList.toggle("active", +b.dataset.page === i));
    host.innerHTML = "";
    const pre = document.createElement("pre");
    pre.className = "mono ascii";
    pre.textContent = pages[i].text;
    host.append(pre);
  };
  pages.forEach((p, i) => tabs.append(el("button", { "data-page": i, text: p.title, class: i === 0 ? "active" : "" })));
  tabs.addEventListener("click", (e) => {
    const b = e.target.closest("[data-page]");
    if (b) show(+b.dataset.page);
  });
  wrap.append(tabs, host);
  show(0);
  return wrap;
}

// ── modal state + tab rendering ───────────────────────────────────────
const _state = { api: null, renderYamlTree: null, bpString: "", asciiPages: [], model: null, yaml: null };

async function loadBlueprint(bpString) {
  _state.bpString = bpString;
  _state.yaml = null;
  const content = $("#inspect-content");
  content.innerHTML = "";
  content.append(el("p", { class: "hint", text: t("t.decoding") }));
  try {
    const res = await _state.api("/api/v1/blueprints/render", { method: "POST", body: { blueprint: bpString } });
    const data = await res.json();
    _state.asciiPages = splitAsciiPages(data.ascii || "");
    _state.model = data.model || null;
  } catch (e) {
    content.innerHTML = "";
    content.append(el("p", { class: "hint", text: t("t.asciiFail", { msg: e.message }) }));
    return;
  }
  renderTab("string");
}

async function renderTab(key) {
  const content = $("#inspect-content");
  $$("#inspect-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === key));

  if (key === "string") {
    content.innerHTML = "";
    const ta = el("textarea", { class: "mono bpview", readonly: "", text: _state.bpString });
    content.append(ta, el("div", { class: "row", style: "margin-top:8px" }, [
      el("button", { class: "primary", text: t("result.copy"), onclick: () => { navigator.clipboard.writeText(_state.bpString); } }),
    ]));
    return;
  }

  if (key === "ascii") {
    content.innerHTML = "";
    content.append(asciiView(_state.asciiPages));
    return;
  }

  if (key === "preview") {
    content.innerHTML = "";
    if (_state.model) content.append(previewView(_state.model));
    else content.append(el("p", { class: "hint", text: t("result.viewHint") }));
    return;
  }

  if (key === "yaml") {
    content.innerHTML = "";
    if (!_state.yaml) {
      content.append(el("p", { class: "hint", text: t("t.decoding") }));
      try {
        const res = await _state.api("/api/v1/blueprints/decode", { method: "POST", body: { blueprint: _state.bpString } });
        const obj = await res.json();
        _state.yaml = obj.text || "";
      } catch (e) {
        content.innerHTML = "";
        content.append(el("p", { class: "hint", text: t("t.decodeError", { msg: e.message }) }));
        return;
      }
    }
    content.innerHTML = "";
    content.append(_state.renderYamlTree ? _state.renderYamlTree(_state.yaml) : el("pre", { class: "mono", text: _state.yaml }));
  }
}

// ── public entry ──────────────────────────────────────────────────────
export function openBlueprintInspector(opts) {
  const { api, renderYamlTree, jobId, bpString, title, getResultText } = opts;
  _state.api = api;
  _state.renderYamlTree = renderYamlTree;
  _state.bpString = "";
  _state.asciiPages = [];
  _state.model = null;
  _state.yaml = null;

  $("#inspect-title").textContent = title || t("viewer.title");
  const paste = $("#inspect-paste");
  const body = $("#inspect-body");

  const showJob = async (id) => {
    paste.classList.add("hidden");
    body.classList.remove("hidden");
    $("#inspect-content").innerHTML = "";
    $("#inspect-content").append(el("p", { class: "hint", text: t("t.decoding") }));
    try {
      const text = await getResultText(id, "blueprint");
      loadBlueprint(text);
    } catch (e) {
      $("#inspect-content").innerHTML = "";
      $("#inspect-content").append(el("p", { class: "hint", text: t("result.couldNotLoad", { fmt: "blueprint", msg: e.message }) }));
    }
  };

  if (jobId) { showJob(jobId); }
  else if (bpString) {
    paste.classList.add("hidden");
    body.classList.remove("hidden");
    loadBlueprint(bpString);
  } else {
    body.classList.add("hidden");
    paste.classList.remove("hidden");
    $("#inspect-input").value = "";
    $("#inspect-input").focus();
  }
  $("#inspect-modal").classList.remove("hidden");
}

// ── modal wiring (idempotent) ─────────────────────────────────────────
let _wired = false;
function wireModal() {
  if (_wired) return;
  _wired = true;
  $("#inspect-close").addEventListener("click", () => $("#inspect-modal").classList.add("hidden"));
  $("#inspect-modal").addEventListener("click", (e) => {
    if (e.target === e.currentTarget) $("#inspect-modal").classList.add("hidden");
  });
  $("#inspect-load").addEventListener("click", () => {
    const v = $("#inspect-input").value.trim();
    if (!v) return;
    $("#inspect-body").classList.remove("hidden");
    loadBlueprint(v);
  });
  $("#inspect-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      const v = e.target.value.trim();
      if (v) { $("#inspect-body").classList.remove("hidden"); loadBlueprint(v); }
    }
  });
  $("#inspect-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-tab]");
    if (b) renderTab(b.dataset.tab);
  });
}
if (typeof document !== "undefined" && document.getElementById("inspect-modal")) wireModal();
