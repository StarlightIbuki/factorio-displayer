"""Render a blueprint as ASCII art for debugging (entities + circuit wiring).

The **entity map** uses single/multi-character glyphs:

- Combinators carry a direction marker: ``D>`` / ``<D`` for east/west facing
  deciders, ``A>`` / ``<A`` for arithmetic, ``S>`` / ``<S`` for selectors;
  north/south facing draw ``^`` above / ``V`` below the letter::

        ^          D
        D          V

- ``C`` = constant combinator, ``S`` = programmable speaker (and selector),
  ``L`` = small lamp, ``.`` = any other entity.

The **wiring map** is drawn as a separate grid per connection map, and red
and green networks live on their **own** connection maps (a port can carry
both a red and a green network at once, so the two colours must never share
a character pool).  Within one colour every circuit network is assigned a
character from ``0-9 A-Z a-z`` (62 per map); when a blueprint has more than
62 networks of that colour the same characters are reused on that colour's
next connection map.  Each entity shows the character(s) of the network(s)
it is wired into on that colour — e.g. a combinator whose input and output
ride two separate red networks shows both chars (``01``).  ``.`` means the
entity is not on any network of that colour.  Input vs output side is not
shown: a circuit connection doesn't care which connector it lands on, and
the combinator's facing in the entity map already tells you which side is
the input vs the output.  This makes it easy to eyeball e.g. whether a
memory bank's data bus actually reaches the display lamps, and which
networks are still disconnected.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterator

_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_MAP_SIZE = len(_CHARS)  # 62

# entity name -> (letter, kind) ; kind in {"combinator", "cc", "one"}
_ENTITY_LETTERS = {
    "decider-combinator": ("D", "combinator"),
    "arithmetic-combinator": ("A", "combinator"),
    "selector-combinator": ("S", "combinator"),
    "constant-combinator": ("C", "cc"),
    "programmable-speaker": ("S", "one"),
    "small-lamp": ("L", "one"),
}


def _entity_anchor(e: Any) -> tuple[int, int] | None:
    """Return the (x, y) tile anchor of *e*, or None if unknown."""
    pos = getattr(e, "tile_position", None)
    if pos is None:
        pos = getattr(e, "position", None)
    if pos is None:
        return None
    try:
        return int(pos[0]), int(pos[1])
    except (TypeError, ValueError):
        return None


def _entity_direction(e: Any) -> int:
    """Return the draftsman Direction int (0/4/8/12 = N/E/S/W, binary compass)."""
    d = getattr(e, "direction", None)
    if d is None:
        return 0
    try:
        return int(d)
    except (TypeError, ValueError):
        return 0


def _entity_glyph(name: str, direction: int) -> list[tuple[tuple[int, int], str]]:
    """Return ``[(offset, char), ...]`` glyph cells for an entity.

    Offsets are relative to the entity's anchor tile; combinators are drawn
    with their letter at the anchor and a direction marker to the side.
    Draftsman/Factorio Direction uses a binary compass (0=N, 4=E, 8=S, 12=W);
    diagonals (unused by combinators) fall back to the south glyph.
    """
    info = _ENTITY_LETTERS.get(name)
    if info is None:
        return [((0, 0), ".")]
    letter, kind = info
    if kind == "cc":
        return [((0, 0), "C")]
    if kind == "one":
        # Speaker / lamp — no facing marker
        return [((0, 0), letter)]
    # Facing combinator
    if direction == 4:      # east
        return [((0, 0), letter), ((1, 0), ">")]
    if direction == 12:     # west
        return [((-1, 0), "<"), ((0, 0), letter)]
    if direction == 0:      # north
        return [((0, -1), "^"), ((0, 0), letter)]
    return [((0, 0), letter), ((0, 1), "V")]  # south (8) or diagonal fallback


# ── Circuit network extraction ────────────────────────────────────────

def _iter_wire_ports(
    bp: Any,
) -> Iterator[tuple[tuple[int, str, str], tuple[int, str, str]]]:
    """Yield ``(port_a, port_b)`` for every circuit wire.

    A port is ``(entity_object_id, side, color)`` where *side* is
    ``"input"`` or ``"output"``.  Uses Python object identity as the entity
    key because parsed draftsman blueprints don't guarantee ``.id``.
    """
    wires = getattr(bp, "wires", None) or []
    for w in wires:
        a, cn1, b, cn2 = w
        ea, eb = a(), b()
        t1 = cn1.value if hasattr(cn1, "value") else int(cn1)
        t2 = cn2.value if hasattr(cn2, "value") else int(cn2)
        color = "red" if t1 % 2 == 1 else "green"
        side_a = "input" if t1 <= 2 else "output"
        side_b = "input" if t2 <= 2 else "output"
        # A constant combinator's single connector is its output, but Draftsman
        # encodes it on the "input" side (the entity is not dual-circuit).
        if getattr(ea, "name", None) == "constant-combinator" and side_a == "input":
            side_a = "output"
        if getattr(eb, "name", None) == "constant-combinator" and side_b == "input":
            side_b = "output"
        yield (id(ea), side_a, color), (id(eb), side_b, color)


def _networks(bp: Any) -> list[list[tuple[int, str, str]]]:
    """Return circuit networks as lists of ports (union-find over wires).

    Red and green networks are kept separate (a wire is one colour), and a
    combinator's input/output sides are distinct ports, so e.g. a decider
    whose input and output ride different buses yields two networks.
    """
    parent: dict[tuple[int, str, str], tuple[int, str, str]] = {}

    def _find(k):
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def _union(a, b):
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[rb] = ra

    for pa, pb in _iter_wire_ports(bp):
        if pa[2] != pb[2]:
            continue
        _union(pa, pb)

    comps: dict[tuple, list[tuple[int, str, str]]] = defaultdict(list)
    for key in parent:
        comps[_find(key)].append(key)
    return list(comps.values())


# ── Grid rendering ────────────────────────────────────────────────────

def _render_grid(cells: dict[tuple[int, int], str], coords: bool = True) -> list[str]:
    """Render a sparse ``{(x, y): char}`` dict as a rectangular ASCII grid."""
    if not cells:
        return ["(empty)"]
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    width = max_x - min_x + 1
    height = max_y - min_y + 1
    rows = [[" "] * width for _ in range(height)]
    for (x, y), ch in cells.items():
        rows[y - min_y][x - min_x] = ch
    lines: list[str] = []
    if coords:
        lines.append("     " + "".join(str((i + min_x) % 10) for i in range(width)))
    for i, row in enumerate(rows):
        prefix = f"{i + min_y:4d} " if coords else ""
        lines.append(prefix + "".join(row))
    return lines


def _describe_net(
    keys: list[tuple[int, str, str]], ent_by_objid: dict[int, Any]
) -> str:
    """Compact description of a network, e.g. ``"decider, lamp x3"``."""
    cnt: Counter[str] = Counter()
    for (oid, _side, _color) in keys:
        e = ent_by_objid.get(oid)
        cnt[e.name if e is not None else "?"] += 1
    return ", ".join(
        f"{n} x{c}" if c > 1 else n for n, c in sorted(cnt.items())
    )


# ── Public API ────────────────────────────────────────────────────────

def render_blueprint(bp: Any, *, coords: bool = True) -> str:
    """Render *bp* (a blueprint string or draftsman Blueprint) as ASCII art.

    Returns a multi-line string containing the entity map followed by one
    wiring map per connection-map page (62 networks per page).
    """
    from draftsman.blueprintable import Blueprint

    if isinstance(bp, str):
        bp = Blueprint.from_string(bp)

    # Blueprint books render each contained blueprint separately.
    books = getattr(bp, "blueprints", None)
    if books is not None and len(books) > 0:
        parts: list[str] = []
        for idx, inner in enumerate(books):
            inner_bp = inner.blueprint if hasattr(inner, "blueprint") else inner
            parts.append(f"===== Blueprint book item {idx} =====")
            parts.append(render_blueprint(inner_bp, coords=coords))
        return "\n\n".join(parts)

    entities = list(bp.entities)
    ent_by_objid: dict[int, Any] = {id(e): e for e in entities}

    # ── Entity map ──────────────────────────────────────────────
    ent_cells: dict[tuple[int, int], str] = {}
    for e in entities:
        anchor = _entity_anchor(e)
        if anchor is None:
            continue
        x, y = anchor
        for (dx, dy), ch in _entity_glyph(e.name, _entity_direction(e)):
            ent_cells[(x + dx, y + dy)] = ch

    # ── Wiring maps — red and green on their own connection maps ──
    # A port may carry a red AND a green network simultaneously, so the two
    # colours are never mixed in one character pool.  Each colour has its own
    # set of 0-9 A-Z a-z maps (62 networks per map).
    networks = _networks(bp)

    def _net_pos(keys: list[tuple[int, str, str]]) -> tuple[int, int]:
        xs: list[int] = []
        ys: list[int] = []
        for (oid, _side, _color) in keys:
            e = ent_by_objid.get(oid)
            if e is None:
                continue
            anchor = _entity_anchor(e)
            if anchor is None:
                continue
            xs.append(anchor[0])
            ys.append(anchor[1])
        return (min(ys) if ys else 0, min(xs) if xs else 0)

    net_of_key: dict[tuple[int, str, str], tuple[int, str]] = {}
    # net_legend[(color, mapnum, char)] -> (endpoint_count, keys)
    net_legend: dict[tuple[str, int, str], tuple[int, list]] = {}
    for color in ("red", "green"):
        color_nets = [n for n in networks if n and n[0][2] == color]
        for i, keys in enumerate(sorted(color_nets, key=_net_pos)):
            char = _CHARS[i % _MAP_SIZE]
            mapnum = i // _MAP_SIZE + 1
            for key in keys:
                net_of_key[key] = (mapnum, char)
            net_legend[(color, mapnum, char)] = (len(keys), list(keys))

    def _map_count(color: str) -> int:
        return max(
            (m for (c, m, _ch) in net_legend if c == color),
            default=0,
        )

    red_maps = _map_count("red")
    green_maps = _map_count("green")

    # wire_cells[(color, mapnum, x, y)] -> single char.  Each entity shows the
    # character(s) of the network(s) it connects to on that colour — a
    # combinator wired into two separate networks shows both (e.g. "01").
    # Input/output is NOT shown: the entity map's direction glyph already
    # conveys which connector is the input vs the output side.
    wire_cells: dict[tuple[str, int, int, int], str] = {}
    for e in entities:
        anchor = _entity_anchor(e)
        if anchor is None:
            continue
        x, y = anchor
        oid = id(e)
        for color, n_maps in (("red", red_maps), ("green", green_maps)):
            nets = sorted({
                net_of_key[(oid, side, color)]
                for side in ("input", "output")
                if (oid, side, color) in net_of_key
            })
            if not nets:
                for mapnum in range(1, n_maps + 1):
                    wire_cells[(color, mapnum, x, y)] = "."
                continue
            for mapnum in range(1, n_maps + 1):
                chars_this_map = [c for (m, c) in nets if m == mapnum]
                if not chars_this_map:
                    wire_cells[(color, mapnum, x, y)] = " "
                else:
                    for i, c in enumerate(sorted(chars_this_map)):
                        wire_cells[(color, mapnum, x + i, y)] = c

    # ── Assemble output ─────────────────────────────────────────
    lines: list[str] = []
    lines.append("=== Blueprint entities ===")
    lines.append(
        "Legend: D=decider  A=arithmetic  S=selector/speaker  "
        "C=constant  L=lamp  .=other"
    )
    lines.append("        > east  < west  ^ north  V south  (combinator facing)")
    lines.extend(_render_grid(ent_cells, coords=coords))
    lines.append("")

    if not net_legend:
        lines.append("=== Wiring ===  (no circuit networks found)")
        return "\n".join(lines)

    for color, n_maps in (("red", red_maps), ("green", green_maps)):
        if n_maps == 0:
            continue
        label = "RED" if color == "red" else "GREEN"
        for mapnum in range(1, n_maps + 1):
            lines.append(f"=== Wiring - {label} connection map {mapnum} ===")
            entries = [
                (char, info)
                for (c2, m, char), info in sorted(net_legend.items())
                if c2 == color and m == mapnum
            ]
            for char, (n, keys) in entries:
                names = _describe_net(keys, ent_by_objid)
                lines.append(
                    f"  '{char}' = {color} network, {n} endpoint(s) [{names}]"
                )
            cells = {
                k[2:]: v
                for k, v in wire_cells.items()
                if k[0] == color and k[1] == mapnum
            }
            lines.extend(_render_grid(cells, coords=coords))
            lines.append("")

    return "\n".join(lines)


def blueprint_string_to_ascii(bp_string: str, *, coords: bool = True) -> str:
    """Render a blueprint string as ASCII art (entity + wiring maps).

    Thin convenience wrapper around :func:`render_blueprint` so callers can
    pass a raw blueprint string (mirrors :func:`blueprint_string_to_yaml`).
    """
    return render_blueprint((bp_string or "").strip(), coords=coords)
