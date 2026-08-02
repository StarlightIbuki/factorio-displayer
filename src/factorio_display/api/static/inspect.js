// inspect.js — unified Blueprint Inspector modal.
//
// A single modal is used both for the job's blueprint inspection and the
// topbar "Blueprint viewer" utility.  It shows three tabs:
//   * Blueprint — the raw string (+ copy).
//   * Inspect   — the parts list: one card per entity with property
//                 key-value rows (position, facing, condition).
//   * Preview   — an interactive SVG (combinator outlines with facing,
//                 red/green wire overlays, network-coloured ports, hover-
//                 to-highlight) with ASCII as an alternative view mode.
//
// Everything is rendered CLIENT-SIDE by ascii.js (a port of the backend
// ascii_render.py); no backend round-trip is needed at all.

/* eslint-env browser */
import { t } from "./i18n.js";
import { decodeBlueprintString, renderBlueprintAscii, renderBlueprintModel } from "./ascii.js";

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

function buildPreview(model, onSelect) {
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
    // Footprint-aware box: east/west combinators are 2x1, north/south are 1x2.
    let boxW = 1, boxH = 1;
    if (ent.kind === "combinator") {
      if (ent.dir === 4 || ent.dir === 12) { boxW = 2; boxH = 1; }
      else { boxW = 1; boxH = 2; }
    }
    const rx = toX(ent.x), ry = toY(ent.y);
    const cx = rx + (boxW * TILE) / 2;
    const cy = ry + (boxH * TILE) / 2;

    const g = svgEl("g", { class: "ent", "data-nets": nets.join(",") });
    g.dataset.idx = i;
    g.append(svgEl("rect", {
      class: "ent-box",
      x: rx + 1, y: ry + 1, width: boxW * TILE - 2, height: boxH * TILE - 2,
      rx: 4, fill: "rgba(255,255,255,0.05)", stroke: "rgba(255,255,255,0.45)",
    }));
    const letter = svgEl("text", {
      class: "ent-letter",
      x: cx, y: cy + 4, "text-anchor": "middle",
    });
    letter.textContent = ent.letter;
    g.append(letter);

    // Combinator facing: a small triangle ("angle in the shape") pointing the
    // way the combinator faces — inside the box, near the facing edge.
    if (ent.kind === "combinator") {
      let pts;
      if (ent.dir === 4) pts = `${rx + 2 * TILE - 3},${cy} ${rx + 2 * TILE - 11},${cy - 6} ${rx + 2 * TILE - 11},${cy + 6}`;
      else if (ent.dir === 12) pts = `${rx + 3},${cy} ${rx + 11},${cy - 6} ${rx + 11},${cy + 6}`;
      else if (ent.dir === 0) pts = `${cx},${ry + 3} ${cx - 6},${ry + 11} ${cx + 6},${ry + 11}`;
      else pts = `${cx},${ry + 2 * TILE - 3} ${cx - 6},${ry + 2 * TILE - 11} ${cx + 6},${ry + 2 * TILE - 11}`;
      g.append(svgEl("polygon", { class: "ent-facing", points: pts }));
    }

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
  // Click an entity → select it and report its index for the config panel.
  svg.addEventListener("click", (e) => {
    const g = e.target.closest(".ent");
    svg.querySelectorAll(".ent.selected").forEach((n) => n.classList.remove("selected"));
    if (g) g.classList.add("selected");
    if (onSelect) onSelect(g ? Number(g.dataset.idx) : null);
  });
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

function svgView(model, doc) {
  const wrap = el("div");
  const toolbar = el("div", { class: "preview-toolbar" });
  const showWires = el("label", { class: "row", style: "gap:6px;align-items:center" }, [
    el("input", { type: "checkbox", checked: "", id: "preview-wires" }),
    el("span", { text: t("preview.showWires") }),
  ]);
  const zoomOut = el("button", { class: "pv-zoom", text: "−", title: t("preview.zoomOut") });
  const zoomVal = el("span", { class: "pv-zoom-val", text: "100%" });
  const zoomIn = el("button", { class: "pv-zoom", text: "+", title: t("preview.zoomIn") });
  const zoomReset = el("button", { class: "pv-zoom", text: "1:1", title: t("preview.zoomReset") });
  toolbar.append(showWires, el("span", { class: "spacer" }), zoomOut, zoomVal, zoomIn, zoomReset);
  wrap.append(toolbar);

  // Split layout: scrollable canvas on the left, properties on the right.
  const layout = el("div", { class: "preview-layout" });
  const canvas = el("div", { class: "preview-canvas" });
  const side = el("div", { class: "preview-side" });

  // Raw blueprint entities (with position/direction/control_behavior) — the
  // render model entities are only {x,y,name,dir,letter,kind}.
  const entities = _entities(doc);
  const showDetail = (idx) => {
    side.innerHTML = "";
    if (idx == null) side.append(el("p", { class: "hint", text: t("preview.clickHint") }));
    else side.append(entityDetail(entities[idx], idx));
  };
  showDetail(null);

  const svg = buildPreview(model, showDetail);
  const holder = el("div", { class: "preview-holder" });
  holder.append(svg);
  canvas.append(holder);
  layout.append(canvas, side);
  wrap.append(layout);

  // Zoom — scrollbars live in the canvas, so a scaled-up holder tracks them.
  const W = (model.max_x - model.min_x + 2) * TILE + PAD * 2;
  const H = (model.max_y - model.min_y + 3) * TILE + PAD * 2;
  let zoom = 0.8; // 80% default — fits more of the blueprint in view
  const applyZoom = () => {
    holder.style.width = (W * zoom) + "px";
    holder.style.height = (H * zoom) + "px";
    svg.style.width = (W * zoom) + "px";
    svg.style.height = (H * zoom) + "px";
    zoomVal.textContent = Math.round(zoom * 100) + "%";
    zoomIn.disabled = zoom >= 4;
    zoomOut.disabled = zoom <= 0.25;
  };
  zoomIn.addEventListener("click", () => { zoom = Math.min(4, zoom * 1.25); applyZoom(); });
  zoomOut.addEventListener("click", () => { zoom = Math.max(0.25, zoom / 1.25); applyZoom(); });
  zoomReset.addEventListener("click", () => { zoom = 1; applyZoom(); });
  // Ctrl/⌘ + mouse wheel zooms (plain wheel still pans/scrolls the canvas).
  canvas.addEventListener("wheel", (e) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    zoom = Math.min(4, Math.max(0.25, zoom * (e.deltaY < 0 ? 1.1 : 1 / 1.1)));
    applyZoom();
  }, { passive: false });
  applyZoom();

  // legend
  const legend = el("div", { class: "preview-legend" });
  (model.networks || []).forEach((net, idx) => {
    legend.append(el("span", { class: "preview-legend-item" }, [
      el("span", { class: "swatch", style: `background:${netColor(idx)}` }),
      el("span", { text: `${net.char} ${net.color} · ${net.endpoints.length} pt` }),
    ]));
  });
  wrap.append(legend);

  $("#preview-wires", wrap).addEventListener("change", (e) => {
    canvas.querySelectorAll(".wire").forEach((w) => w.classList.toggle("hidden", !e.target.checked));
  });
  return wrap;
}

// Preview tab: SVG and ASCII are alternative views of the same blueprint.
function previewView(model, asciiPages, doc) {
  const wrap = el("div");
  const toggle = el("div", { class: "preview-mode" }, [
    el("button", { "data-mode": "svg", class: "active", text: t("preview.modePreview") }),
    el("button", { "data-mode": "ascii", text: t("preview.modeAscii") }),
  ]);
  const host = el("div");
  const show = (mode) => {
    $$("[data-mode]", toggle).forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
    host.innerHTML = "";
    if (mode === "ascii") host.append(asciiView(asciiPages));
    else host.append(svgView(model, doc));
  };
  toggle.addEventListener("click", (e) => {
    const b = e.target.closest("[data-mode]");
    if (b) show(b.dataset.mode);
  });
  wrap.append(toggle, host);
  show("svg");
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

// ── parts list view (entity cards with property key-value rows) ───────
const _ENTITY_LETTER = {
  "decider-combinator": "D", "arithmetic-combinator": "A", "selector-combinator": "S",
  "constant-combinator": "C", "programmable-speaker": "S", "small-lamp": "L",
};
const _FACING_NAMES = { 0: "north", 2: "northeast", 4: "east", 6: "southeast", 8: "south", 10: "southwest", 12: "west", 14: "northwest" };
const _FACING_ARROW = { 0: "↑", 4: "→", 8: "↓", 12: "←" };

function _entities(doc) {
  if (!doc) return [];
  if (doc.blueprint) return doc.blueprint.entities || [];
  if (doc.blueprint_book && doc.blueprint_book.blueprints && doc.blueprint_book.blueprints.length) {
    const b = doc.blueprint_book.blueprints[0].blueprint;
    return (b && b.entities) || [];
  }
  return [];
}

function fmtPos(pos) {
  if (!pos || pos.x == null || pos.y == null) return "—";
  const f = (n) => (Number.isInteger(n) ? String(n) : String(Math.round(n * 10) / 10));
  return `(${f(pos.x)}, ${f(pos.y)})`;
}

function facingOf(ent) {
  const d = ent.direction ?? 0;
  const name = _FACING_NAMES[d] || "north";
  return _FACING_ARROW[d] ? `${name} ${_FACING_ARROW[d]}` : name;
}

// Circuit signal object → short name (each/anything/everything specials).
const _SIG_SPECIAL = { "signal-each": "each", "signal-anything": "anything", "signal-everything": "everything" };
function _sig(s) {
  if (!s) return "";
  return _SIG_SPECIAL[s.name] || s.name;
}

// ── condition / output list views (read-only "controllers") ────────────
function ctlInput(value) {
  return el("input", { type: "text", class: "pv-ctl", readonly: "", value: value == null ? "" : String(value) });
}
function ctlSelect(value, options) {
  const sel = el("select", { class: "pv-ctl", disabled: "" });
  const v = value == null ? "" : String(value);
  let found = false;
  for (const o of options) {
    const opt = el("option", { text: o });
    if (String(o) === v) { opt.selected = true; found = true; }
    sel.append(opt);
  }
  if (!found && v !== "") sel.append(el("option", { text: v, selected: "" }));
  return sel;
}
function sigCtl(sig) { return ctlInput(sig ? _sig(sig) : ""); }
function condRow(label, ...ctls) {
  return el("div", { class: "pv-row" }, [el("span", { class: "pv-row-label", text: label }), ...ctls]);
}
function boolCtl(label, value) {
  const box = el("input", { type: "checkbox", disabled: "" });
  if (value) box.checked = true;
  return el("label", { class: "pv-row" }, [
    el("span", { class: "pv-row-label", text: label }),
    box,
  ]);
}

// Build the two numbered lists (conditions / outputs) from an entity's REAL
// serialized control_behavior.  In Factorio 2.0 / draftsman:
//   * decider:      control_behavior.decider_conditions = { conditions: [...], outputs: [...] }
//   * arithmetic:   control_behavior.arithmetic_conditions = { first_signal, operation, second_signal, output_signal }
//   * constant:     control_behavior.filters = [{ signal, count }]
//   * speaker:      control_behavior.circuit_condition (enable) + circuit_parameters,
//                   volume signal at top-level entity.parameters.volume_signal_id
//   * lamp:         control_behavior.circuit_condition
function conditionSections(ent) {
  const conditions = [];
  const outputs = [];
  const cb = ent && ent.control_behavior;
  if (!cb) return { conditions, outputs };

  if (cb.decider_conditions) {
    const dc = cb.decider_conditions;
    (Array.isArray(dc.conditions) ? dc.conditions : []).forEach((c) => {
      conditions.push({ body: () => [
        condRow(t("inspect.first"), c.first_signal ? sigCtl(c.first_signal) : ctlInput(c.first_constant != null ? c.first_constant : "")),
        condRow(t("inspect.comparator"), ctlSelect(c.comparator || "=", [">", "<", "=", "≥", "≤", "≠"])),
        condRow(t("inspect.second"), c.second_signal ? sigCtl(c.second_signal) : ctlInput(c.constant != null ? c.constant : "")),
        ...(c.compare_type ? [condRow(t("inspect.combine"), ctlSelect(c.compare_type, ["and", "or"]))] : []),
      ] });
    });
    (Array.isArray(dc.outputs) ? dc.outputs : []).forEach((o) => {
      outputs.push({ body: () => [
        condRow(t("inspect.signal"), o.signal ? sigCtl(o.signal) : ctlInput(o.constant != null ? o.constant : "")),
        boolCtl(t("inspect.copyCount"), !!o.copy_count_from_input),
      ] });
    });
  }

  if (cb.arithmetic_conditions) {
    const ac = cb.arithmetic_conditions;
    conditions.push({ body: () => [
      condRow(t("inspect.first"), ac.first_signal ? sigCtl(ac.first_signal) : ctlInput(ac.first_constant != null ? ac.first_constant : "0")),
      condRow(t("inspect.operation"), ctlSelect(ac.operation || "+", ["+", "-", "*", "/", "%", "^", "<<", ">>", "AND", "OR", "XOR"])),
      condRow(t("inspect.second"), ac.second_signal ? sigCtl(ac.second_signal) : ctlInput(ac.second_constant != null ? ac.second_constant : "0")),
    ] });
    outputs.push({ body: () => [
      condRow(t("inspect.outputSignal"), sigCtl(ac.output_signal)),
    ] });
  }

  if (Array.isArray(cb.filters)) {
    cb.filters.filter((f) => f && f.signal).forEach((f) => {
      outputs.push({ body: () => [
        condRow(t("inspect.signal"), sigCtl(f.signal)),
        condRow(t("inspect.count"), ctlInput(f.count != null ? f.count : 1)),
      ] });
    });
  }

  if (cb.circuit_condition) {
    const c = cb.circuit_condition;
    conditions.push({ body: () => [
      condRow(t("inspect.first"), c.first_signal ? sigCtl(c.first_signal) : ctlInput(c.constant != null ? c.constant : "?")),
      condRow(t("inspect.comparator"), ctlSelect(c.comparator || "=", [">", "<", "=", "≥", "≤", "≠"])),
      condRow(t("inspect.second"), c.second_signal ? sigCtl(c.second_signal) : ctlInput(c.constant != null ? c.constant : "?")),
    ] });
  }

  // Speaker output — the volume signal lives at top-level entity.parameters.
  const params = ent.parameters || {};
  if (cb.circuit_parameters || params.volume_signal_id || params.volume_controlled_by_signal) {
    const cp = cb.circuit_parameters || {};
    outputs.push({ body: () => [
      ...(params.volume_signal_id ? [condRow(t("inspect.volumeSignal"), sigCtl(params.volume_signal_id))] : []),
      ...(params.volume_controlled_by_signal != null ? [boolCtl(t("inspect.volumeControlled"), !!params.volume_controlled_by_signal)] : []),
      ...(cp.instrument_id != null ? [condRow(t("inspect.instrument"), ctlInput(cp.instrument_id))] : []),
    ] });
  }

  return { conditions, outputs };
}

// One numbered, browsable accordion item (open only if it's the first).
function listItem(i, entry) {
  const item = el("div", { class: "pv-item" });
  const body = el("div", { class: "pv-item-body" });
  body.append(...entry.body());
  const caret = el("span", { class: "pv-item-caret", text: i === 0 ? "▾" : "▸" });
  const head = el("button", { class: "pv-item-head", type: "button" }, [
    el("span", { class: "pv-item-num", text: `#${i + 1}` }),
    caret,
  ]);
  head.addEventListener("click", () => {
    caret.textContent = body.classList.toggle("open") ? "▾" : "▸";
  });
  if (i === 0) body.classList.add("open");
  item.append(head, body);
  return item;
}

// Two numbered list sections: "Condition" (#1, #2, …) and "Output" (#1, #2, …).
function conditionView(ent) {
  const wrap = el("div", { class: "pv-list" });
  const { conditions, outputs } = conditionSections(ent);
  if (!conditions.length && !outputs.length) {
    wrap.append(el("p", { class: "hint", text: "—" }));
    return wrap;
  }
  if (conditions.length) {
    wrap.append(el("div", { class: "pv-section", text: t("inspect.condition") }));
    conditions.forEach((entry, i) => wrap.append(listItem(i, entry)));
  }
  if (outputs.length) {
    wrap.append(el("div", { class: "pv-section", text: t("inspect.output") }));
    outputs.forEach((entry, i) => wrap.append(listItem(i, entry)));
  }
  return wrap;
}

// One entity card, shared by the parts list and the preview side panel.
// Position/facing as key-value rows; condition/output as a structured list
// of read-only controllers (conditionView).
function entityDetail(ent, idx) {
  const card = el("div", { class: "inspect-item" });
  const head = el("div", { class: "inspect-item-head" }, [
    el("span", { class: "inspect-item-badge", text: _ENTITY_LETTER[ent.name] || "." }),
    el("span", { class: "inspect-item-name", text: `#${ent.entity_number ?? idx + 1} ${ent.name}` }),
  ]);
  const rows = el("div", { class: "inspect-item-rows" });
  const kv = (label, value) => el("div", { class: "inspect-item-row" }, [
    el("span", { class: "inspect-item-key", text: label }),
    el("span", { class: "inspect-item-val", text: value }),
  ]);
  rows.append(
    kv(t("inspect.position"), fmtPos(ent.position)),
    kv(t("inspect.facing"), facingOf(ent)),
  );
  card.append(head, rows);
  card.append(conditionView(ent));
  return card;
}

function itemsView(doc) {
  const ents = _entities(doc);
  const wrap = el("div", { class: "inspect-items" });
  if (!ents.length) {
    wrap.append(el("p", { class: "hint", text: t("result.viewHint") }));
    return wrap;
  }
  ents.forEach((ent, i) => wrap.append(entityDetail(ent, i)));
  return wrap;
}

// ── modal state + tab rendering ───────────────────────────────────────
const _state = { bpString: "", doc: null, asciiPages: [], model: null };

async function loadBlueprint(bpString) {
  _state.bpString = bpString;
  const content = $("#inspect-content");
  content.innerHTML = "";
  content.append(el("p", { class: "hint", text: t("t.decoding") }));
  try {
    // Everything is computed in-browser (ascii.js): the parts list, the
    // ASCII pages, and the preview model — no backend round-trip at all.
    const doc = await decodeBlueprintString(bpString);
    _state.doc = doc;
    _state.asciiPages = splitAsciiPages(renderBlueprintAscii(doc));
    _state.model = renderBlueprintModel(doc);
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

  if (key === "items") {
    content.innerHTML = "";
    if (_state.doc) content.append(itemsView(_state.doc));
    else content.append(el("p", { class: "hint", text: t("result.viewHint") }));
    return;
  }

  if (key === "preview") {
    content.innerHTML = "";
    if (_state.model) content.append(previewView(_state.model, _state.asciiPages, _state.doc));
    else content.append(el("p", { class: "hint", text: t("result.viewHint") }));
    return;
  }
}

// ── public entry ──────────────────────────────────────────────────────
export function openBlueprintInspector(opts) {
  const { jobId, bpString, title, getResultText } = opts;
  _state.bpString = "";
  _state.doc = null;
  _state.asciiPages = [];
  _state.model = null;

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
