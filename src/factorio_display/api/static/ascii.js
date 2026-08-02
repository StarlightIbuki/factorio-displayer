// ascii.js — client-side ASCII blueprint rendering (port of ascii_render.py).
//
// Everything needed to turn a raw Factorio blueprint string into ASCII art
// (entity map + per-colour wiring maps) or a structured preview model runs
// in the browser — no backend round-trip required.
//
// Blueprint string format handled here:
//   "0" <version byte> + base64( zlib( JSON ) )
// The JSON uses *binary-compass* directions (0=N, 4=E, 8=S, 12=W — same as
// draftsman) and *centre-of-footprint* floating point positions; the anchor
// (top-left tile) is recovered from the entity's footprint and direction.

/* eslint-env browser */

const _CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
const _MAP_SIZE = _CHARS.length; // 62

// entity name -> [letter, kind] ; kind in {"combinator", "cc", "one"}
const _ENTITY_LETTERS = {
  "decider-combinator": ["D", "combinator"],
  "arithmetic-combinator": ["A", "combinator"],
  "selector-combinator": ["S", "combinator"],
  "constant-combinator": ["C", "cc"],
  "programmable-speaker": ["S", "one"],
  "small-lamp": ["L", "one"],
};

// ── blueprint string → JSON ──────────────────────────────────────────

/**
 * Decode a Factorio blueprint string into its JSON document.
 * Handles both "blueprint" and "blueprint_book" documents.
 * @param {string} str raw blueprint string (e.g. "0eN...")
 * @returns {Promise<object>} the decoded JSON
 */
export async function decodeBlueprintString(str) {
  let s = String(str || "").trim();
  if (!s) throw new Error("empty blueprint string");
  // strip the leading version byte ("0")
  if (/^[0-9]/.test(s)) s = s.slice(1);
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(s)) throw new Error("invalid blueprint string encoding");
  const bin = atob(s);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const ds = new DecompressionStream("deflate");
  const stream = new Blob([bytes]).stream().pipeThrough(ds);
  const out = new Uint8Array(await new Response(stream).arrayBuffer());
  return JSON.parse(new TextDecoder().decode(out));
}

// ── shared helpers ───────────────────────────────────────────────────

/** Return the (x, y) top-left tile anchor of an entity. */
function _anchor(ent) {
  const pos = ent.position;
  if (!pos || pos.x == null || pos.y == null) return null;
  const x = pos.x, y = pos.y;
  const dir = ent.direction ?? 0;
  const name = ent.name;
  if (_ENTITY_LETTERS[name] && _ENTITY_LETTERS[name][1] === "combinator") {
    // 2x1 footprint for east/west (4/12), 1x2 otherwise
    if (dir === 4 || dir === 12) return [Math.round(x - 1.0), Math.round(y - 0.5)];
    return [Math.round(x - 0.5), Math.round(y - 1.0)];
  }
  return [Math.round(x - 0.5), Math.round(y - 0.5)];
}

/** Draftsman binary-compass direction int (0/4/8/12). */
function _direction(ent) {
  return ent.direction ?? 0;
}

/** Glyph cells [[dx, dy, char], ...] relative to the entity's anchor tile. */
function _glyph(name, dir) {
  const info = _ENTITY_LETTERS[name];
  if (!info) return [[0, 0, "."]];
  const [letter, kind] = info;
  if (kind === "cc") return [[0, 0, "C"]];
  if (kind === "one") return [[0, 0, letter]];
  if (dir === 4) return [[0, 0, letter], [1, 0, ">"]];
  if (dir === 12) return [[-1, 0, "<"], [0, 0, letter]];
  if (dir === 0) return [[0, -1, "^"], [0, 0, letter]];
  return [[0, 0, letter], [0, 1, "V"]];
}

/**
 * Unwrap a decoded JSON document into the blueprint object (following
 * blueprint-book nesting), plus the entity list and entity_number→index map.
 */
function _unwrap(doc) {
  let bp;
  if (doc.blueprint) bp = doc.blueprint;
  else if (doc.blueprint_book && doc.blueprint_book.blueprints && doc.blueprint_book.blueprints.length) {
    bp = doc.blueprint_book.blueprints[0].blueprint || {};
  } else {
    bp = {};
  }
  const entities = bp.entities || [];
  const idxByNumber = new Map();
  entities.forEach((e, i) => { if (e.entity_number != null) idxByNumber.set(e.entity_number, i); });
  return { bp, entities, idxByNumber };
}

/**
 * Yield [port_a, port_b] for every circuit wire. A port is
 * [entity_number, side, color] where side is "input"|"output".
 */
function _iterWirePorts(unwrapped) {
  const { bp, entities, idxByNumber } = unwrapped;
  const out = [];
  const nameOf = (num) => {
    const i = idxByNumber.get(num);
    return i == null ? null : entities[i].name;
  };
  for (const w of bp.wires || []) {
    const [ea, t1, eb, t2] = w;
    if (idxByNumber.get(ea) == null || idxByNumber.get(eb) == null) continue;
    const color = (t1 % 2 === 1) ? "red" : "green";
    let sideA = t1 <= 2 ? "input" : "output";
    let sideB = t2 <= 2 ? "input" : "output";
    // A constant combinator's single connector is its output.
    if (nameOf(ea) === "constant-combinator" && sideA === "input") sideA = "output";
    if (nameOf(eb) === "constant-combinator" && sideB === "input") sideB = "output";
    out.push([[ea, sideA, color], [eb, sideB, color]]);
  }
  return out;
}

const _key = (num, side, color) => `${num}|${side}|${color}`;

/** Circuit networks as lists of ports (union-find over wires). */
function _networks(unwrapped) {
  const parent = new Map();
  const find = (k) => {
    if (!parent.has(k)) parent.set(k, k);
    let root = k;
    while (parent.get(root) !== root) root = parent.get(root);
    // path halving
    let n = k;
    while (parent.get(n) !== root) {
      const p = parent.get(n);
      parent.set(n, root);
      n = p;
    }
    return root;
  };
  const union = (a, b) => {
    const ra = find(a), rb = find(b);
    if (ra !== rb) parent.set(rb, ra);
  };
  for (const [pa, pb] of _iterWirePorts(unwrapped)) {
    if (pa[2] !== pb[2]) continue;
    union(_key(...pa), _key(...pb));
  }
  const comps = new Map();
  for (const k of parent.keys()) {
    const root = find(k);
    if (!comps.has(root)) comps.set(root, []);
    comps.get(root).push(k.split("|"));
  }
  return [...comps.values()];
}

/** Render a sparse {(x, y): char} map as a rectangular ASCII grid. */
function _renderGrid(cells, coords = true) {
  const keys = Object.keys(cells).map((k) => k.split(",").map(Number));
  if (!keys.length) return ["(empty)"];
  const xs = keys.map((c) => c[0]);
  const ys = keys.map((c) => c[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const width = maxX - minX + 1;
  const height = maxY - minY + 1;
  const rows = Array.from({ length: height }, () => Array(width).fill(" "));
  for (const [x, y] of keys) rows[y - minY][x - minX] = cells[`${x},${y}`];
  const lines = [];
  if (coords) {
    lines.push("     " + Array.from({ length: width }, (_, i) => String((i + minX) % 10)).join(""));
  }
  rows.forEach((row, i) => {
    const prefix = coords ? `${String(i + minY).padStart(4, " ")} ` : "";
    lines.push(prefix + row.join(""));
  });
  return lines;
}

/** Compact description of a network, e.g. "decider, lamp x3". */
function _describeNet(keys, nameByNum) {
  const cnt = new Map();
  for (const [num] of keys) {
    const n = nameByNum.get(Number(num)) || "?";
    cnt.set(n, (cnt.get(n) || 0) + 1);
  }
  return [...cnt.entries()]
    .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
    .map(([n, c]) => (c > 1 ? `${n} x${c}` : n))
    .join(", ");
}

// ── ASCII renderer ───────────────────────────────────────────────────

/**
 * Render a decoded blueprint JSON document as ASCII art (entity + wiring
 * maps). Mirrors ascii_render.py `render_blueprint`.
 * @param {object} doc decoded blueprint JSON (or a book)
 * @param {{coords?: boolean}} [opts]
 * @returns {string}
 */
export function renderBlueprintAscii(doc, opts = {}) {
  const coords = opts.coords !== false;
  // Blueprint books render each contained blueprint separately (like the
  // Python render_blueprint).
  if (doc && doc.blueprint_book && doc.blueprint_book.blueprints && doc.blueprint_book.blueprints.length) {
    const parts = [];
    doc.blueprint_book.blueprints.forEach((inner, idx) => {
      parts.push(`===== Blueprint book item ${idx} =====`);
      parts.push(renderBlueprintAscii(inner, opts));
    });
    return parts.join("\n\n");
  }
  const unwrapped = _unwrap(doc);
  const { bp, entities, idxByNumber } = unwrapped;
  const nameByNum = new Map();
  idxByNumber.forEach((i, num) => nameByNum.set(num, entities[i].name));

  // ── Entity map ──────────────────────────────────────────────
  const entCells = {};
  for (const e of entities) {
    const a = _anchor(e);
    if (!a) continue;
    for (const [dx, dy, ch] of _glyph(e.name, _direction(e))) {
      entCells[`${a[0] + dx},${a[1] + dy}`] = ch;
    }
  }

  // ── Wiring maps — red and green on their own connection maps ──
  const networks = _networks(unwrapped);

  const netPos = (keys) => {
    let minY = Infinity, minX = Infinity;
    for (const [num] of keys) {
      const i = idxByNumber.get(Number(num));
      if (i == null) continue;
      const a = _anchor(entities[i]);
      if (!a) continue;
      if (a[1] < minY) minY = a[1];
      if (a[0] < minX) minX = a[0];
    }
    return [minY === Infinity ? 0 : minY, minX === Infinity ? 0 : minX];
  };

  const netOfKey = new Map(); // key -> [mapnum, char]
  const netLegend = new Map(); // "color|mapnum|char" -> [count, keys]
  for (const color of ["red", "green"]) {
    const colorNets = networks.filter((n) => n.length && n[0][2] === color);
    colorNets.sort((a, b) => {
      const pa = netPos(a), pb = netPos(b);
      return pa[0] - pb[0] || pa[1] - pb[1];
    });
    colorNets.forEach((keys, i) => {
      const char = _CHARS[i % _MAP_SIZE];
      const mapnum = Math.floor(i / _MAP_SIZE) + 1;
      for (const k of keys) netOfKey.set(_key(...k), [mapnum, char]);
      netLegend.set(`${color}|${mapnum}|${char}`, [keys.length, keys]);
    });
  }

  const mapCount = (color) => {
    let m = 0;
    for (const k of netLegend.keys()) {
      if (k.startsWith(color + "|")) {
        const mm = Number(k.split("|")[1]);
        if (mm > m) m = mm;
      }
    }
    return m;
  };
  const redMaps = mapCount("red");
  const greenMaps = mapCount("green");

  // wire_cells["color|mapnum|x,y"] -> char
  const wireCells = {};
  for (const e of entities) {
    const a = _anchor(e);
    if (!a) continue;
    const [x, y] = a;
    const num = e.entity_number;
    for (const [color, nMaps] of [["red", redMaps], ["green", greenMaps]]) {
      const nets = new Set();
      for (const side of ["input", "output"]) {
        const rec = netOfKey.get(_key(num, side, color));
        if (rec) nets.add(`${rec[0]}|${rec[1]}`);
      }
      const sortedNets = [...nets].sort();
      if (!sortedNets.length) {
        for (let m = 1; m <= nMaps; m++) wireCells[`${color}|${m}|${x},${y}`] = ".";
        continue;
      }
      for (let m = 1; m <= nMaps; m++) {
        const charsThisMap = sortedNets
          .filter((s) => s.startsWith(m + "|"))
          .map((s) => s.split("|")[1])
          .sort();
        if (!charsThisMap.length) wireCells[`${color}|${m}|${x},${y}`] = " ";
        else charsThisMap.forEach((c, i) => { wireCells[`${color}|${m}|${x + i},${y}`] = c; });
      }
    }
  }

  // ── Assemble output ─────────────────────────────────────────
  const lines = [];
  lines.push("=== Blueprint entities ===");
  lines.push("Legend: D=decider  A=arithmetic  S=selector/speaker  C=constant  L=lamp  .=other");
  lines.push("        > east  < west  ^ north  V south  (combinator facing)");
  lines.push(..._renderGrid(entCells, coords));
  lines.push("");

  if (!netLegend.size) {
    lines.push("=== Wiring ===  (no circuit networks found)");
    return lines.join("\n");
  }

  for (const [color, nMaps] of [["red", redMaps], ["green", greenMaps]]) {
    if (nMaps === 0) continue;
    const label = color === "red" ? "RED" : "GREEN";
    for (let mapnum = 1; mapnum <= nMaps; mapnum++) {
      lines.push(`=== Wiring - ${label} connection map ${mapnum} ===`);
      const entries = [];
      for (const [k, info] of [...netLegend.entries()].sort()) {
        const [c, m, char] = k.split("|");
        if (c === color && Number(m) === mapnum) entries.push([char, info]);
      }
      for (const [char, [n, keys]] of entries) {
        lines.push(`  '${char}' = ${color} network, ${n} endpoint(s) [${_describeNet(keys, nameByNum)}]`);
      }
      const cells = {};
      for (const [k, v] of Object.entries(wireCells)) {
        const [c, m, xy] = k.split("|");
        if (c === color && Number(m) === mapnum) cells[xy] = v;
      }
      lines.push(..._renderGrid(cells, coords));
      lines.push("");
    }
  }

  return lines.join("\n");
}

// ── structured render model (for the SVG preview) ────────────────────

/**
 * Build the structured preview model from a decoded blueprint JSON,
 * mirroring ascii_render.py `blueprint_render_model`.
 * @param {object} doc decoded blueprint JSON
 * @returns {object} {entities, ports, networks, wires, min_x, min_y, max_x, max_y}
 */
export function renderBlueprintModel(doc) {
  const unwrapped = _unwrap(doc);
  const { bp, entities, idxByNumber } = unwrapped;

  const entRecords = entities.map((e) => {
    const a = _anchor(e);
    const info = _ENTITY_LETTERS[e.name];
    return {
      x: a ? a[0] : 0,
      y: a ? a[1] : 0,
      name: e.name,
      dir: _direction(e),
      letter: info ? info[0] : ".",
      kind: info ? info[1] : "other",
    };
  });

  const networks = _networks(unwrapped);

  const netPos = (keys) => {
    let minY = Infinity, minX = Infinity;
    for (const [num] of keys) {
      const i = idxByNumber.get(Number(num));
      if (i == null) continue;
      const a = _anchor(entities[i]);
      if (!a) continue;
      if (a[1] < minY) minY = a[1];
      if (a[0] < minX) minX = a[0];
    }
    return [minY === Infinity ? 0 : minY, minX === Infinity ? 0 : minX];
  };

  const networkRecords = [];
  const netOfKey = new Map(); // key -> network index
  for (const color of ["red", "green"]) {
    const colorNets = networks.filter((n) => n.length && n[0][2] === color);
    colorNets.sort((a, b) => {
      const pa = netPos(a), pb = netPos(b);
      return pa[0] - pb[0] || pa[1] - pb[1];
    });
    colorNets.forEach((keys, i) => {
      const idx = networkRecords.length;
      networkRecords.push({
        color,
        char: _CHARS[i % _MAP_SIZE],
        map: Math.floor(i / _MAP_SIZE) + 1,
        endpoints: keys.map(([num, side]) => ({ entity: idxByNumber.get(Number(num)), side })),
      });
      for (const k of keys) netOfKey.set(_key(...k), idx);
    });
  }

  const ports = entities.map((e, i) => {
    const entry = { entity: i, red: { input: -1, output: -1 }, green: { input: -1, output: -1 } };
    for (const color of ["red", "green"]) {
      for (const side of ["input", "output"]) {
        const rec = netOfKey.get(_key(e.entity_number, side, color));
        if (rec != null) entry[color][side] = rec;
      }
    }
    return entry;
  });

  const wires = [];
  for (const [pa, pb] of _iterWirePorts(unwrapped)) {
    const ia = idxByNumber.get(Number(pa[0])), ib = idxByNumber.get(Number(pb[0]));
    if (ia == null || ib == null) continue;
    wires.push([
      { entity: ia, side: pa[1], color: pa[2] },
      { entity: ib, side: pb[1], color: pb[2] },
    ]);
  }

  const xs = entRecords.map((r) => r.x);
  const ys = entRecords.map((r) => r.y);
  return {
    entities: entRecords,
    ports,
    networks: networkRecords,
    wires,
    min_x: xs.length ? Math.min(...xs) : 0,
    min_y: ys.length ? Math.min(...ys) : 0,
    max_x: xs.length ? Math.max(...xs) : 0,
    max_y: ys.length ? Math.max(...ys) : 0,
  };
}

// ── string conveniences ──────────────────────────────────────────────

/** Render a raw blueprint string as ASCII art. */
export async function blueprintStringToAscii(str, opts = {}) {
  const doc = await decodeBlueprintString(str);
  return renderBlueprintAscii(doc, opts);
}

/** Build the structured preview model from a raw blueprint string. */
export async function blueprintStringRenderModel(str) {
  const doc = await decodeBlueprintString(str);
  return renderBlueprintModel(doc);
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
