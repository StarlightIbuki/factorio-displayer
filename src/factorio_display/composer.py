"""Generic blueprint composer — merges LogicalBlueprints by port connections.

The composer is **blind** to what it composes.  It receives a list of
components (each exposing named input/output ports) and a list of port
connections, then merges, lays out, wires, and caches the result.

Usage::

    from factorio_display.composer import compose, PortConnection

    result = compose(
        components=[timer_lb, memory_lb, display_lb, progress_lb],
        connections=[
            PortConnection("Timer", "clock", "Video Memory", "clock"),
            PortConnection("Timer", "sub_tick", "Progress", "sub_tick"),
            PortConnection("Video Memory", "data", "Display", "data"),
        ],
        output_name="My Media Player",
    )
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .logical_blueprint import (
    Endpoint,
    LogicalBlueprint,
    LogicalEntity,
    Network,
    _endpoint_position,
    _chebyshev,
    _find_closest_pair,
    to_toml,
    from_toml,
)
from .timer import build_raw_timer, build_mod_timer
from .cache_paths import cache_namespace_dir, version_prefix

try:
    from tqdm import tqdm
except ImportError:
    class tqdm:  # pylint: disable=invalid-name
        """Graceful fallback if tqdm is not installed."""
        def __init__(self, iterable=None, *_args, **_kwargs):
            self.iterable = iterable or []
            self._total = len(self.iterable) if hasattr(self.iterable, '__len__') else None
        def __iter__(self):
            yield from self.iterable
        def update(self, n=1):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def set_description(self, desc):
            pass
        @property
        def total(self):
            return self._total
        @total.setter
        def total(self, v):
            self._total = v


__all__ = [
    "PortConnection",
    "compose",
    # ── legacy API (deprecated, kept for transition) ──────────
    "Composer",
    "compose_all_in_one",
    "cache_put",
    "cache_get",
    "_assign_tile_positions",
    "_connect_nets_by_color",
    "_connect_networks",
    "_entity_bounding_box",
    "_shift_positions",
]


# ═══════════════════════════════════════════════════════════════════════
# Port connection spec
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class PortConnection:
    """Specifies a connection between two named ports across components.

    *from_component* and *to_component* are the ``label`` attributes of
    the source/destination :class:`LogicalBlueprint` components.
    """

    from_component: str
    from_port: str
    to_component: str
    to_port: str


# ═══════════════════════════════════════════════════════════════════════
# Cache
# ═══════════════════════════════════════════════════════════════════════

_CACHE_DIR = cache_namespace_dir("compose")
# Bump whenever composition layout/wiring changes (the cache key does not
# include component versions, only caller hash parts + this revision).
_LAYOUT_CACHE_REV = "layout-v6"


def _cache_key(*parts: str) -> str:
    """Deterministic cache key from string parts."""
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
    return h


def _cache_path(key: str) -> Path:
    return _CACHE_DIR / f"{version_prefix()}_{key}.toml"


def cache_put(lb: LogicalBlueprint, *key_parts: str) -> None:
    """Cache a logical blueprint to disk."""
    _CACHE_DIR.mkdir(exist_ok=True)
    path = _cache_path(_cache_key(*key_parts))
    path.write_text(to_toml(lb), encoding="utf-8")


def cache_get(*key_parts: str) -> LogicalBlueprint | None:
    """Retrieve a cached logical blueprint, or None if not found."""
    path = _cache_path(_cache_key(*key_parts))
    if path.exists():
        return from_toml(path.read_text(encoding="utf-8"))
    return None


# ═══════════════════════════════════════════════════════════════════════
# Layout helpers (public — used by other modules)
# ═══════════════════════════════════════════════════════════════════════


def _entity_bounding_box(lb: LogicalBlueprint) -> tuple[int, int, int, int]:
    """Return (min_x, min_y, max_x, max_y) of all positioned entities in *lb*."""
    xs: list[int] = []
    ys: list[int] = []
    for ent in lb.entities.values():
        if ent.position is not None:
            xs.append(ent.position[0])
            ys.append(ent.position[1])
    if not xs:
        return 0, 0, 0, 0
    return min(xs), min(ys), max(xs), max(ys)


def _shift_positions(lb: LogicalBlueprint, dx: int, dy: int) -> None:
    """Offset all entity positions in *lb* by (dx, dy)."""
    for ent in lb.entities.values():
        if ent.position is not None:
            x, y = ent.position
            ent.position = (x + dx, y + dy)


def _assign_tile_positions(lb: LogicalBlueprint, start_x: int = 0, start_y: int = 0) -> None:
    """Assign tile positions to all entities in *lb*, relocating to (start_x, start_y).

    Entities that already have positions are shifted relative to their
    current bounding-box origin.  Entities without positions are placed
    in a vertical column starting from the shifted origin.
    """
    lb.place_relative(
        origin_x=start_x,
        origin_y=start_y,
        assign_unpositioned=True,
        unpositioned_step_y=2,
    )


# ═══════════════════════════════════════════════════════════════════════
# Generic compose
# ═══════════════════════════════════════════════════════════════════════


def _sanitize_label(label: str) -> str:
    """Derive a short prefix from a label for use in entity/network ids."""
    result = "".join(c if c.isalnum() or c == "_" else "_" for c in label)
    while "__" in result:
        result = result.replace("__", "_")
    result = result.strip("_").lower()
    if not result:
        result = "comp"
    return result + "_"


def _merge_components(
    merged: LogicalBlueprint,
    components: list[LogicalBlueprint],
) -> tuple[dict[str, dict[str, dict[str, str]]], dict[str, str]]:
    """Merge all components into *merged*, tracking port → prefixed net id.

    Returns ``(port_map, prefixes)`` where *port_map* is
    ``{label: {"input_ports": {name: prefixed_net_id},
               "output_ports": {name: prefixed_net_id}}}``
    and *prefixes* is ``{label: entity_id_prefix}``.

    Port design
    -----------
    - **Input ports** are unprefixed — same-name inputs (e.g. ``"clock"``)
      implicitly merge into a shared network.
    - **Output ports** are prefixed — same-name outputs across components
      are kept separate (e.g. different chunks' ``"data"`` buses).
    """
    port_map: dict[str, dict[str, dict[str, str]]] = {}
    prefixes: dict[str, str] = {}

    for comp in tqdm(components, desc="Merging components"):
        label = comp.label or "unnamed"
        prefix = _sanitize_label(label)
        prefixes[label] = prefix

        port_map[label] = {
            "input_ports": {},
            "output_ports": {},
        }

        # Snapshot input ports before merge to detect collisions
        existing_inputs = dict(merged.input_ports)

        # Merge — entities & networks are prefixed; ports are unprefixed
        # so same-name inputs merge naturally.
        merged.merge(comp, entity_prefix=prefix, network_prefix=prefix, port_prefix="")

        # ── Handle input port collisions ─────────────────────────
        # If the component had an input port whose name already existed
        # in *merged*, the dict overwrite orphaned the old network.
        # Connect the new network to the old one so all entities
        # sharing the same clock actually share the same wire.
        for port_name, new_net_id in comp.input_ports.items():
            old_net_id = existing_inputs.get(port_name)
            if old_net_id is not None:
                new_prefixed = prefix + new_net_id
                if old_net_id != new_prefixed:
                    # Only merge same-colour networks.  Same-named input
                    # ports can live on different buses — e.g. the video
                    # memory "clock" on RED vs the audio memory / player
                    # "clock" on GREEN (clock-bus convention).  Connecting
                    # across colours corrupts the topology.
                    old_net = next((n for n in merged.networks if n.network_id == old_net_id), None)
                    new_net = next((n for n in merged.networks if n.network_id == new_prefixed), None)
                    if old_net is not None and new_net is not None and old_net.color == new_net.color:
                        _connect_networks(merged, old_net_id, new_prefixed)

        # ── Namespace output ports ───────────────────────────────
        # Rename output ports to add the component prefix so that
        # different components' data buses are never merged.
        for port_name in list(merged.output_ports.keys()):
            net_id = merged.output_ports[port_name]
            if net_id.startswith(prefix) and not port_name.startswith(prefix):
                merged.output_ports.pop(port_name)
                merged.output_ports[prefix + port_name] = net_id

        # ── Record port mappings ─────────────────────────────────
        for port_name, net_id in merged.input_ports.items():
            port_map[label]["input_ports"][port_name] = net_id

        for port_name, net_id in merged.output_ports.items():
            if net_id.startswith(prefix):
                port_map[label]["output_ports"][port_name] = net_id

    return port_map, prefixes


def _apply_connections(
    merged: LogicalBlueprint,
    connections: list[PortConnection],
    port_map: dict[str, dict[str, dict[str, str]]],
    prefixes: dict[str, str],
) -> int:
    """Connect ports according to *connections*.

    *prefixes* maps component label → port prefix (same as entity prefix).
    Port names in *connections* are unprefixed; they are prefixed before
    lookup in *port_map*.

    Returns the number of connections successfully made.
    """
    connected = 0
    for conn in tqdm(connections, desc="Wiring ports"):
        src_info = port_map.get(conn.from_component, {})
        dst_info = port_map.get(conn.to_component, {})
        src_pfx = prefixes.get(conn.from_component, "")
        dst_pfx = prefixes.get(conn.to_component, "")

        # Output ports are prefixed (namespaced); input ports are not (shared).
        src_net_id = src_info.get("output_ports", {}).get(src_pfx + conn.from_port)
        dst_net_id = dst_info.get("input_ports", {}).get(conn.to_port)

        if src_net_id is None:
            sys.stderr.write(
                f"Compose warning: source port {conn.from_component!r}:{conn.from_port!r} "
                f"not found (available: {list(src_info.get('output_ports', {}).keys())})\n"
            )
            continue
        if dst_net_id is None:
            sys.stderr.write(
                f"Compose warning: dest port {conn.to_component!r}:{conn.to_port!r} "
                f"not found (available: {list(dst_info.get('input_ports', {}).keys())})\n"
            )
            continue

        src_net = next((n for n in merged.networks if n.network_id == src_net_id), None)
        dst_net = next((n for n in merged.networks if n.network_id == dst_net_id), None)

        if src_net is None or dst_net is None:
            continue
        if not src_net.endpoints or not dst_net.endpoints:
            continue
        if src_net.color != dst_net.color:
            sys.stderr.write(
                f"Compose warning: color mismatch for "
                f"{conn.from_component!r}:{conn.from_port!r} "
                f"({src_net.color}) → "
                f"{conn.to_component!r}:{conn.to_port!r} ({dst_net.color})\n"
            )
            continue

        src_ep = next(iter(src_net.endpoints))
        dst_ep = next(iter(dst_net.endpoints))
        pair = _find_closest_pair({src_ep}, dst_net.endpoints, merged)
        if pair is not None:
            src_ep, dst_ep = pair
        merged.connect(src_net.color, src_ep, dst_ep)
        # After merging, one of the two networks was popped.
        # Update port registries so they point to the surviving network.
        _survivor = next(
            (n for n in merged.networks
             if n.color == src_net.color and src_ep in n.endpoints and dst_ep in n.endpoints),
            None,
        )
        if _survivor is not None:
            _survivor_id = _survivor.network_id
            for dead_id in (src_net_id, dst_net_id):
                if dead_id == _survivor_id:
                    continue
                for port_map_entry in (merged.input_ports, merged.output_ports):
                    for pname, pnet in list(port_map_entry.items()):
                        if pnet == dead_id:
                            port_map_entry[pname] = _survivor_id
                # Also update the per-component port_map used for subsequent lookups,
                # so later connections to a merged input port resolve to the surviving network.
                for label_map in port_map.values():
                    for sub in (label_map.get("input_ports", {}), label_map.get("output_ports", {})):
                        for pname, pnet in list(sub.items()):
                            if pnet == dead_id:
                                sub[pname] = _survivor_id
        connected += 1

    return connected


def _layout_components(
    merged: LogicalBlueprint,
    prefixes: dict[str, str] | None = None,
    connections: list[PortConnection] | None = None,
) -> None:
    """Assign positions for merged component groups with adaptive compaction.

    Components may arrive fully positioned, partially positioned, or with no
    positions at all. This pass guarantees every entity is positioned before
    wiring-distance validation and keeps connected groups close together.

    *prefixes* is ``{label: entity_id_prefix}`` from :func:`_merge_components`.
    *connections* informs group ordering — components that share a port
    connection are placed next to each other in the vertical stack.
    """
    def _entity_footprint(ent: LogicalEntity) -> tuple[int, int]:
        """Return a coarse tile footprint used for collision-free placement."""
        if ent.type in ("arithmetic-combinator", "decider-combinator"):
            # East/West combinators occupy 2x1, North/South occupy 1x2.
            if ent.direction in (2, 6):
                return 2, 1
            return 1, 2
        if ent.type == "substation":
            return 2, 2
        return 1, 1

    def _mark_rect(occ: set[tuple[int, int]], x: int, y: int, w: int, h: int) -> None:
        for tx in range(x, x + w):
            for ty in range(y, y + h):
                occ.add((tx, ty))

    def _can_place_rect(occ: set[tuple[int, int]], x: int, y: int, w: int, h: int) -> bool:
        for tx in range(x, x + w):
            for ty in range(y, y + h):
                if (tx, ty) in occ:
                    return False
        return True

    def _cheb_pos(a: tuple[int, int], b: tuple[int, int]) -> int:
        return max(abs(a[0] - b[0]), abs(a[1] - b[1]))

    def _spiral_candidates(cx: int, cy: int, radius: int):
        for r in range(radius + 1):
            if r == 0:
                yield cx, cy
                continue
            x0, x1 = cx - r, cx + r
            y0, y1 = cy - r, cy + r
            for x in range(x0, x1 + 1):
                yield x, y0
                yield x, y1
            for y in range(y0 + 1, y1):
                yield x0, y
                yield x1, y

    def _build_group_adjacency(entity_ids: set[str]) -> dict[str, set[str]]:
        """Build a sparse adjacency map from shared networks within one group."""
        adj: dict[str, set[str]] = {eid: set() for eid in entity_ids}
        for net in merged.networks:
            ids = [ep.entity_id for ep in net.endpoints if ep.entity_id in entity_ids]
            if len(ids) <= 1:
                continue
            # Large dense nets (lamp buses) don't need full O(n^2) adjacency.
            if len(ids) > 64:
                for i in range(len(ids) - 1):
                    a = ids[i]
                    b = ids[i + 1]
                    if a != b:
                        adj[a].add(b)
                        adj[b].add(a)
                continue
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    if a != b:
                        adj[a].add(b)
                        adj[b].add(a)
        return adj

    def _auto_layout_group(items: list[tuple[str, LogicalEntity]]) -> None:
        """Assign compact positions for unpositioned entities in one group."""
        if not items:
            return
        occ: set[tuple[int, int]] = set()
        entity_ids = {eid for eid, _ in items}
        adj = _build_group_adjacency(entity_ids)

        for _eid, ent in items:
            if ent.position is None:
                continue
            w, h = _entity_footprint(ent)
            _mark_rect(occ, ent.position[0], ent.position[1], w, h)

        unplaced = {eid for eid, ent in items if ent.position is None}
        if not unplaced:
            return

        item_map = {eid: ent for eid, ent in items}

        def _pick_next() -> str:
            placed_ids = {eid for eid in entity_ids if item_map[eid].position is not None}
            return max(
                unplaced,
                key=lambda eid: (
                    len(adj.get(eid, set()) & placed_ids),
                    len(adj.get(eid, set())),
                    -len(eid),
                ),
            )

        while unplaced:
            eid = _pick_next()
            ent = item_map[eid]
            w, h = _entity_footprint(ent)

            placed_neighbors: list[tuple[int, int]] = []
            for nid in adj.get(eid, set()):
                nent = item_map.get(nid)
                if nent is not None and nent.position is not None:
                    placed_neighbors.append(nent.position)

            if placed_neighbors:
                anchor_x = round(sum(p[0] for p in placed_neighbors) / len(placed_neighbors))
                anchor_y = round(sum(p[1] for p in placed_neighbors) / len(placed_neighbors))
            else:
                placed = [it.position for _id, it in items if it.position is not None]
                if placed:
                    anchor_x = round(sum(p[0] for p in placed) / len(placed))
                    anchor_y = round(sum(p[1] for p in placed) / len(placed))
                else:
                    anchor_x, anchor_y = 0, 0

            best_pos: tuple[int, int] | None = None
            best_score: float | None = None
            for x, y in _spiral_candidates(anchor_x, anchor_y, radius=40):
                if not _can_place_rect(occ, x, y, w, h):
                    continue
                if placed_neighbors:
                    dsum = sum(_cheb_pos((x, y), p) for p in placed_neighbors)
                else:
                    dsum = abs(x - anchor_x) + abs(y - anchor_y)
                score = float(dsum) + 0.05 * (abs(x) + abs(y))
                if best_score is None or score < best_score:
                    best_score = score
                    best_pos = (x, y)
                    if score <= 0.0:
                        break

            if best_pos is None:
                # Fallback: place below origin with sparse spacing.
                best_pos = (0, len(entity_ids) * 2)

            ent.position = best_pos
            _mark_rect(occ, best_pos[0], best_pos[1], w, h)
            unplaced.remove(eid)

    # Build prefix → group mapping from the actual component prefixes
    prefix_set: list[str] = list(prefixes.values()) if prefixes else []

    # Group by entity id prefix
    groups: dict[str, list[tuple[str, LogicalEntity]]] = {}
    for eid, ent in merged.entities.items():
        matched = "__root__"
        for pfx in prefix_set:
            if eid.startswith(pfx):
                matched = pfx
                break
        groups.setdefault(matched, []).append((eid, ent))

    # Ensure each group has concrete positions before global placement.
    for items in groups.values():
        _auto_layout_group(items)

    if len(groups) <= 1:
        return

    # Build label→prefix and prefix→label maps
    label_to_prefix: dict[str, str] = {}
    prefix_to_label: dict[str, str] = {}
    if prefixes:
        for label, pfx in prefixes.items():
            label_to_prefix[label] = pfx
            prefix_to_label[pfx] = label

    # Build adjacency: which prefixes are connected, and the
    # directed dependency graph (from → to) for topological sort.
    adj: dict[str, set[str]] = {pfx: set() for pfx in groups if pfx != "__root__"}
    # Directed edges: src_pfx → set of dst_pfx (data flows this way)
    deps: dict[str, set[str]] = {pfx: set() for pfx in groups if pfx != "__root__"}
    # In-degree for Kahn's algorithm
    indeg: dict[str, int] = {pfx: 0 for pfx in groups if pfx != "__root__"}
    if connections:
        for conn in connections:
            src_pfx = label_to_prefix.get(conn.from_component)
            dst_pfx = label_to_prefix.get(conn.to_component)
            if src_pfx and dst_pfx and src_pfx != dst_pfx:
                # Undirected adjacency (for tie-breaking)
                if src_pfx in adj:
                    adj[src_pfx].add(dst_pfx)
                if dst_pfx in adj:
                    adj[dst_pfx].add(src_pfx)
                # Directed: source → destination
                if src_pfx in deps and dst_pfx in deps and dst_pfx not in deps.get(src_pfx, set()):
                    deps[src_pfx].add(dst_pfx)
                    indeg[dst_pfx] = indeg.get(dst_pfx, 0) + 1

    # ── Topological sort (Kahn's algorithm) ───────────────────────
    # Prefer sources (out-degree > 0, in-degree = 0) first, then
    # use greedy adjacency for the rest.
    ordered: list[str] = []
    remaining = set(adj.keys())

    if remaining:
        # Collect sources (in-degree 0) and sort by total degree
        # (most connected first) as a tie-break.
        queue = sorted(
            [pfx for pfx in remaining if indeg.get(pfx, 0) == 0],
            key=lambda pfx: len(adj[pfx]),
            reverse=True,
        )
        while queue:
            pfx = queue.pop(0)
            if pfx not in remaining:
                continue
            ordered.append(pfx)
            remaining.discard(pfx)
            # Reduce in-degree of successors
            for dst in deps.get(pfx, set()):
                if dst in indeg:
                    indeg[dst] -= 1
                    if indeg[dst] == 0 and dst in remaining:
                        queue.append(dst)
                        # Keep queue sorted by adjacency for stability
                        queue.sort(key=lambda p: len(adj[p]), reverse=True)

        # Any remaining components (cycles or unconnected) — use
        # greedy adjacency to place them next to already-placed ones.
        while remaining:
            best_pfx = max(
                remaining,
                key=lambda pfx: len(adj[pfx] & set(ordered)),
            )
            ordered.append(best_pfx)
            remaining.discard(best_pfx)

    # Ensure all groups are included (even those with no connections)
    for pfx in groups:
        if pfx != "__root__" and pfx not in ordered:
            ordered.append(pfx)

    # Prefer a display-like sink (lamp-heavy group), then speaker-heavy group.
    def _sink_rank(prefix: str) -> tuple[int, int, int]:
        items = groups.get(prefix, [])
        lamps = sum(1 for _eid, ent in items if ent.type == "small-lamp")
        speakers = sum(1 for _eid, ent in items if ent.type == "programmable-speaker")
        return (1 if lamps > 0 else 0, lamps, speakers)

    if ordered:
        preferred_sink = max(ordered, key=_sink_rank)
        best_rank = _sink_rank(preferred_sink)
        if best_rank[0] > 0 and preferred_sink in ordered:
            ordered = [p for p in ordered if p != preferred_sink] + [preferred_sink]

    # ── Place groups: sink at origin, dependencies to its right ───
    # The last group in topological order is the sink (e.g. the display).
    # Place it at its original position.  Everything else goes to the
    # right of the sink, stacked vertically from the sink's top edge.
    # This keeps the display and its data sources close (same Y band),
    # minimising wire distance for the bridge connection.
    if not ordered:
        return
    if len(ordered) <= 1:
        # Single group — just shift it to the origin
        prefix = ordered[0]
        items = groups.get(prefix, [])
        xs = [ent.position[0] for _, ent in items if ent.position is not None]
        ys = [ent.position[1] for _, ent in items if ent.position is not None]
        if xs:
            dx = -min(xs)
            dy = -min(ys)
            for _, ent in items:
                if ent.position is not None:
                    x, y = ent.position
                    ent.position = (x + dx, y + dy)
        return

    sink_prefix = ordered[-1]
    source_prefixes = ordered[:-1]

    # ── Place sink at origin ─────────────────────────────────────
    sink_items = groups.get(sink_prefix, [])
    sink_xs = [ent.position[0] for _, ent in sink_items if ent.position is not None]
    sink_ys = [ent.position[1] for _, ent in sink_items if ent.position is not None]
    if not sink_xs:
        return

    sink_dx = -min(sink_xs)
    sink_dy = -min(sink_ys)
    sink_max_x = max(sink_xs) + sink_dx
    sink_min_y = min(sink_ys) + sink_dy
    sink_max_y = max(sink_ys) + sink_dy

    for _, ent in sink_items:
        if ent.position is not None:
            x, y = ent.position
            ent.position = (x + sink_dx, y + sink_dy)

    # ── Place sources as one compact side-cluster ────────────────
    # Keep logical modules visually cohesive by choosing one side of
    # the sink (left/right/top/bottom) and packing all sources there.
    sink_min_x = min(sink_xs) + sink_dx
    sink_cx = (sink_min_x + sink_max_x) // 2
    sink_cy = (sink_min_y + sink_max_y) // 2

    source_meta: list[tuple[str, int, int, int, int]] = []
    for prefix in source_prefixes:
        items = groups.get(prefix, [])
        min_x: int | None = None
        min_y: int | None = None
        max_x: int | None = None
        max_y: int | None = None
        for _, ent in items:
            if ent.position is None:
                continue
            x, y = ent.position
            fw, fh = _entity_footprint(ent)
            ex0, ey0 = x, y
            ex1, ey1 = x + fw - 1, y + fh - 1
            if min_x is None:
                min_x, min_y, max_x, max_y = ex0, ey0, ex1, ey1
            else:
                min_x = min(min_x, ex0)
                min_y = min(min_y, ey0)
                max_x = max(max_x, ex1)
                max_y = max(max_y, ey1)

        if min_x is None or min_y is None or max_x is None or max_y is None:
            continue
        w = max_x - min_x + 1
        h = max_y - min_y + 1
        source_meta.append((prefix, min_x, min_y, w, h))

    if not source_meta:
        return

    # Keep modules tightly packed while preserving non-overlap.
    # clearance = minimum tiles between bounding boxes.
    clearance = 1
    stack_gap = 0
    side_layouts: dict[str, dict[str, tuple[int, int]]] = {}
    side_scores: dict[str, tuple[int, int, int]] = {}

    # Left / right: vertical stacking
    for side in ("left", "right"):
        placements: dict[str, tuple[int, int]] = {}
        y_cursor = sink_min_y
        for prefix, _min_x, _min_y, w, h in source_meta:
            if side == "left":
                px = sink_min_x - clearance - w + 1
            else:
                px = sink_max_x + clearance
            py = y_cursor
            placements[prefix] = (px, py)
            y_cursor += h + stack_gap

        centers = [
            (placements[p][0] + w // 2, placements[p][1] + h // 2)
            for p, _mx, _my, w, h in source_meta
        ]
        max_sink_dist = max(_cheb_pos(c, (sink_cx, sink_cy)) for c in centers)
        spread = (y_cursor - sink_min_y)
        side_bias = 0 if side == "right" else 1  # prefer right on ties
        side_layouts[side] = placements
        side_scores[side] = (max_sink_dist, spread, side_bias)

    # Top / bottom: horizontal stacking
    for side in ("top", "bottom"):
        placements = {}
        x_cursor = sink_min_x
        for prefix, _min_x, _min_y, w, h in source_meta:
            px = x_cursor
            if side == "top":
                py = sink_min_y - clearance - h + 1
            else:
                py = sink_max_y + clearance
            placements[prefix] = (px, py)
            x_cursor += w + stack_gap

        centers = [
            (placements[p][0] + w // 2, placements[p][1] + h // 2)
            for p, _mx, _my, w, h in source_meta
        ]
        max_sink_dist = max(_cheb_pos(c, (sink_cx, sink_cy)) for c in centers)
        spread = (x_cursor - sink_min_x)
        side_bias = 2 if side == "top" else 3
        side_layouts[side] = placements
        side_scores[side] = (max_sink_dist, spread, side_bias)

    best_side = min(side_scores.keys(), key=lambda s: side_scores[s])
    best_layout = side_layouts[best_side]

    for prefix, min_x, min_y, _w, _h in source_meta:
        px, py = best_layout[prefix]
        dx = px - min_x
        dy = py - min_y
        for _, ent in groups.get(prefix, []):
            if ent.position is not None:
                x, y = ent.position
                ent.position = (x + dx, y + dy)

    # ── Port-band alignment ──────────────────────────────────────
    # Nudge each source group so the output-port endpoints that connect
    # directly to the sink are aligned (on the stacking axis) with the
    # sink's corresponding input-port endpoints.  This keeps the
    # cross-component bridge wire within Factorio's 9-tile limit even
    # when the sink's input port sits far from its top/left edge (e.g.
    # the audio player's page-data selectors live at y=16, far below
    # the speaker rows where sources would otherwise be stacked).
    #
    # For left/right placement the stacking axis is Y (sources stack
    # vertically); for top/bottom it is X.  The whole group is shifted
    # so its port band center matches the sink's port band center.
    # A shift is only applied when it does not overlap the sink or any
    # other already-aligned source group (fall back to the base layout
    # otherwise).
    if best_side in ("left", "right"):
        _align_axis = 1  # shift y
    else:
        _align_axis = 0  # shift x

    def _group_box(grp_prefix: str) -> tuple[int, int, int, int] | None:
        xs0: list[int] = []
        ys0: list[int] = []
        xs1: list[int] = []
        ys1: list[int] = []
        for _eid, ent in groups.get(grp_prefix, []):
            if ent.position is None:
                continue
            x, y = ent.position
            fw, fh = _entity_footprint(ent)
            xs0.append(x)
            ys0.append(y)
            xs1.append(x + fw - 1)
            ys1.append(y + fh - 1)
        if not xs0:
            return None
        return (min(xs0), min(ys0), max(xs1), max(ys1))

    def _boxes_overlap(b1: tuple[int, int, int, int], b2: tuple[int, int, int, int]) -> bool:
        return not (
            b1[2] < b2[0] or b2[2] < b1[0]
            or b1[3] < b2[1] or b2[3] < b1[1]
        )

    sink_box = _group_box(sink_prefix)
    aligned_boxes: dict[str, tuple[int, int, int, int]] = {
        p: b for p, b in ((p, _group_box(p)) for p, *_ in source_meta) if b is not None
    }

    for conn in connections or []:
        src_p = label_to_prefix.get(conn.from_component)
        if src_p is None or src_p not in aligned_boxes:
            continue
        if label_to_prefix.get(conn.to_component) != sink_prefix:
            continue
        src_out = merged.output_ports.get(src_p + conn.from_port)
        dst_in = merged.input_ports.get(conn.to_port)
        if src_out is None or dst_in is None:
            continue

        def _band(net_id: str, grp_prefix: str, axis: int) -> list[int]:
            coords: list[int] = []
            for net in merged.networks:
                if net.network_id != net_id:
                    continue
                for ep in net.endpoints:
                    ent = merged.entities.get(ep.entity_id)
                    if ent is None or ent.position is None:
                        continue
                    if not ep.entity_id.startswith(grp_prefix):
                        continue
                    coords.append(ent.position[axis])
            return coords

        src_band = _band(src_out, src_p, _align_axis)
        dst_band = _band(dst_in, sink_prefix, _align_axis)
        if not src_band or not dst_band:
            continue
        src_c = sum(src_band) / len(src_band)
        dst_c = sum(dst_band) / len(dst_band)
        shift = int(round(dst_c - src_c))
        if shift == 0:
            continue

        # Reject the shift if it would collide with the sink or another group.
        box = aligned_boxes[src_p]
        shifted_box = (
            box[0] + shift if _align_axis == 0 else box[0],
            box[1] + shift if _align_axis == 1 else box[1],
            box[2] + shift if _align_axis == 0 else box[2],
            box[3] + shift if _align_axis == 1 else box[3],
        )
        collides = (sink_box is not None and _boxes_overlap(shifted_box, sink_box))
        if not collides:
            for other_p, other_box in aligned_boxes.items():
                if other_p == src_p:
                    continue
                if _boxes_overlap(shifted_box, other_box):
                    collides = True
                    break
        if collides:
            continue

        for _eid, ent in groups.get(src_p, []):
            if ent.position is None:
                continue
            x, y = ent.position
            if _align_axis == 1:
                ent.position = (x, y + shift)
            else:
                ent.position = (x + shift, y)
        aligned_boxes[src_p] = shifted_box


def compose(
    components: list[LogicalBlueprint],
    connections: list[PortConnection],
    *,
    output_name: str = "All-in-One",
    pole_type: str | None = None,
    use_cache: bool = True,
    cache_key_parts: Sequence[str] = (),
) -> LogicalBlueprint:
    """Compose multiple LogicalBlueprints into one, connecting named ports.

    The compositor is **blind** to component semantics — it only knows ports
    and connections.  The caller is responsible for building components
    with correct port declarations and specifying what connects to what.

    Parameters
    ----------
    components : list[LogicalBlueprint]
        Components to merge.  Each must have a unique ``label``.
    connections : list[PortConnection]
        Port-to-port connections to wire after merging.
    output_name : str
        Label for the final blueprint.
    pole_type : str | None
        Power pole type: ``"small"``, ``"medium"``, ``"substation"``, or None.
    use_cache : bool
        If True, check cache before composing and cache the result.
    cache_key_parts : Sequence[str]
        Additional key parts for cache identification.

    Returns
    -------
    LogicalBlueprint
        Fully composed blueprint with layout assigned and ports connected.
    """
    # ── Validate unique labels ────────────────────────────────────
    labels = [c.label for c in components]
    if len(labels) != len(set(labels)):
        seen: dict[str, int] = {}
        for i, lbl in enumerate(labels):
            if lbl in seen:
                raise ValueError(
                    f"Duplicate component label {lbl!r} at indices "
                    f"{seen[lbl]} and {i}"
                )
            seen[lbl] = i

    # ── Cache check ───────────────────────────────────────────────
    if use_cache and cache_key_parts:
        cached = cache_get(output_name, *cache_key_parts, _LAYOUT_CACHE_REV)
        if cached is not None:
            return cached

    # ── Merge ─────────────────────────────────────────────────────
    sys.stderr.write(
        f"Composing {len(components)} component(s) "
        f"with {len(connections)} connection(s)...\n"
    )

    merged = LogicalBlueprint(label=output_name)
    port_map, prefixes = _merge_components(merged, components)

    # ── Layout ────────────────────────────────────────────────────
    _layout_components(merged, prefixes, connections)

    # ── Connect ports ─────────────────────────────────────────────
    connected = _apply_connections(merged, connections, port_map, prefixes)
    if connected < len(connections):
        sys.stderr.write(
            f"  Connected {connected}/{len(connections)} port pair(s).\n"
        )
    else:
        sys.stderr.write(f"  Connected {connected} port pair(s).\n")

    # ── Verify every declared port connection was realized ────────
    # Use the per-component port_map because the global input_ports dict is
    # overwritten when components share input port names (e.g. both memory and
    # player have a "clock" input).  After _apply_connections, port_map
    # entries have been updated to the surviving merged network id.
    missing: list[str] = []
    for conn in connections:
        src_pfx = prefixes.get(conn.from_component, "")
        src_info = port_map.get(conn.from_component, {})
        dst_info = port_map.get(conn.to_component, {})
        src_net_id = src_info.get("output_ports", {}).get(src_pfx + conn.from_port)
        dst_net_id = dst_info.get("input_ports", {}).get(conn.to_port)
        if src_net_id is None:
            missing.append(
                f"{conn.from_component!r}:{conn.from_port!r} has no output network"
            )
            continue
        if dst_net_id is None:
            missing.append(
                f"{conn.to_component!r}:{conn.to_port!r} has no input network"
            )
            continue
        # The per-component entries were both updated to the surviving network
        # id when the pair was wired, so they must match for a realized
        # connection.  (A clock bridge legitimately changes colour internally,
        # but the two port_map entries still converge on the same net.)
        if src_net_id != dst_net_id:
            missing.append(
                f"{conn.from_component!r}:{conn.from_port!r} "
                f"({src_net_id!r}) is not wired to "
                f"{conn.to_component!r}:{conn.to_port!r} ({dst_net_id!r})"
            )
    if missing:
        raise ValueError(
            f"Composition failed: {len(missing)} declared connection(s) not realized:\n"
            + "\n".join(f"  - {m}" for m in missing)
        )

    # ── Validate wiring distances ────────────────────────────────
    unreachable = _validate_network_reachability(merged, prefixes)
    if unreachable > 0:
        raise ValueError(
            f"Composition failed: {unreachable} network(s) have endpoints "
            f"that are too far apart (> 64 tiles). "
            f"Components cannot be wired together. "
            f"Consider reducing display size or adding more signals to the pool."
        )

    # ── Sort entities by position ────────────────────────────────
    # Ensures iteration order matches spatial layout so the fast path
    # in _to_draftsman_impl (sorted-position chain) produces correct
    # results for large networks.
    merged.sort_entities_by_position()

    # ── Power ─────────────────────────────────────────────────────
    if pole_type is not None:
        sys.stderr.write(
            "Warning: --power is not yet implemented; "
            "power poles will not be added.\n"
        )

    # ── Verify every declared port connection was realized ────────
    # Use the per-component port_map because the global input_ports dict is
    # overwritten when components share input port names (e.g. both memory and
    # player have a "clock" input).  After _apply_connections, port_map
    # entries have been updated to the surviving merged network id.
    missing: list[str] = []
    for conn in connections:
        src_pfx = prefixes.get(conn.from_component, "")
        src_info = port_map.get(conn.from_component, {})
        dst_info = port_map.get(conn.to_component, {})
        src_net_id = src_info.get("output_ports", {}).get(src_pfx + conn.from_port)
        dst_net_id = dst_info.get("input_ports", {}).get(conn.to_port)
        if src_net_id is None:
            missing.append(
                f"{conn.from_component!r}:{conn.from_port!r} has no output network"
            )
            continue
        if dst_net_id is None:
            missing.append(
                f"{conn.to_component!r}:{conn.to_port!r} has no input network"
            )
            continue
        # The per-component entries were both updated to the surviving network
        # id when the pair was wired, so they must match for a realized
        # connection.  (A clock bridge legitimately changes colour internally,
        # but the two port_map entries still converge on the same net.)
        if src_net_id != dst_net_id:
            missing.append(
                f"{conn.from_component!r}:{conn.from_port!r} "
                f"({src_net_id!r}) is not wired to "
                f"{conn.to_component!r}:{conn.to_port!r} ({dst_net_id!r})"
            )
    if missing:
        raise ValueError(
            f"Composition failed: {len(missing)} declared connection(s) not realized:\n"
            + "\n".join(f"  - {m}" for m in missing)
        )

    # ── Cache ─────────────────────────────────────────────────────
    if use_cache and cache_key_parts:
        cache_put(merged, output_name, *cache_key_parts, _LAYOUT_CACHE_REV)

    return merged


# ═══════════════════════════════════════════════════════════════════════
# Wiring helpers (public — used by timer assembly and legacy code)
# ═══════════════════════════════════════════════════════════════════════


def _connect_nets_by_color(
    lb: LogicalBlueprint,
    color: str,
    entity_contains: str,
    port: str,
    other_entity_contains: str,
    other_port: str,
) -> bool:
    """Connect two single-endpoint networks of the same *color* by
    finding endpoints whose entity ids contain the given substrings.

    Returns True if a connection was made.
    """
    net_a: Network | None = None
    ep_a: Endpoint | None = None
    for net in lb.networks:
        if net.color != color:
            continue
        for ep in net.endpoints:
            if entity_contains in ep.entity_id and ep.port == port:
                net_a = net
                ep_a = ep
                break
        if net_a is not None:
            break

    net_b: Network | None = None
    ep_b: Endpoint | None = None
    for net in lb.networks:
        if net.color != color:
            continue
        for ep in net.endpoints:
            if other_entity_contains in ep.entity_id and ep.port == other_port:
                net_b = net
                ep_b = ep
                break
        if net_b is not None:
            break

    if ep_a is not None and ep_b is not None:
        lb.connect(color, ep_a, ep_b)
        return True
    return False


def _connect_networks(
    lb: LogicalBlueprint,
    net_id_a: str,
    net_id_b: str,
) -> bool:
    """Connect two networks in *lb* by their network ids.
    The closest pair of endpoints is used as the bridge.
    Returns True if a connection was made."""
    net_a = next((n for n in lb.networks if n.network_id == net_id_a), None)
    net_b = next((n for n in lb.networks if n.network_id == net_id_b), None)
    if not net_a or not net_b or not net_a.endpoints or not net_b.endpoints:
        return False
    pair = _find_closest_pair(net_a.endpoints, net_b.endpoints, lb)
    if pair is None:
        return False
    lb.connect(net_a.color, pair[0], pair[1])
    return True


def _would_create_self_loop(
    lb: LogicalBlueprint,
    ep_a: Endpoint,
    ep_b: Endpoint,
    color: str,
) -> bool:
    """Return True if merging the networks containing *ep_a* and *ep_b*
    on *color* would create a NEW self-loop."""
    idx_a = lb._find_network(color, ep_a)
    idx_b = lb._find_network(color, ep_b)
    if idx_a is None or idx_b is None or idx_a == idx_b:
        return False

    net_a = lb.networks[idx_a]
    net_b = lb.networks[idx_b]

    def _entity_ports(net: Network) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for ep in net.endpoints:
            result.setdefault(ep.entity_id, set()).add(ep.port)
        return result

    ports_a = _entity_ports(net_a)
    ports_b = _entity_ports(net_b)

    for eid, ports in ports_b.items():
        if eid not in ports_a:
            continue
        combined = ports_a[eid] | ports
        if "input" in combined and "output" in combined:
            if "input" not in ports_a[eid] or "output" not in ports_a[eid]:
                return True

    return False


def _validate_network_reachability(
    merged: LogicalBlueprint,
    prefixes: dict[str, str] | None = None,
    max_distance: int = 64,
) -> int:
    """Warn about networks whose endpoints cannot all be wired within
    *max_distance* Chebyshev tiles.

    Uses the same union-find logic as :func:`_wire_horizontal_first` to
    determine whether all endpoints in each network can be chained
    together without exceeding *max_distance*.

    Returns the number of networks with unreachable endpoints.
    """
    from .logical_blueprint import _endpoint_position, _chebyshev

    label_to_prefix: dict[str, str] = {}
    prefix_to_label: dict[str, str] = {}
    if prefixes:
        for label, pfx in prefixes.items():
            label_to_prefix[label] = pfx
            prefix_to_label[pfx] = label

    unreachable_count = 0

    for net in merged.networks:
        if net.color == "copper":
            continue
        eps = list(net.endpoints)
        if len(eps) <= 1:
            continue

        # ── Position lookup ──────────────────────────────────
        pos: dict[int, tuple[int, int]] = {}
        for ep in eps:
            pos[id(ep)] = _endpoint_position(ep, merged)

        sorted_eps = sorted(eps, key=lambda ep: (pos[id(ep)][0], pos[id(ep)][1]))
        n = len(sorted_eps)

        # ── Union-find ───────────────────────────────────────
        parent: list[int] = list(range(n))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj

        # Step 1: horizontal neighbours (same y, Δx=1)
        by_y: dict[int, list[int]] = {}
        for i, ep in enumerate(sorted_eps):
            y = pos[id(ep)][1]
            by_y.setdefault(y, []).append(i)

        for y, indices in by_y.items():
            indices.sort(key=lambda i: pos[id(sorted_eps[i])][0])
            for k in range(len(indices) - 1):
                a_idx = indices[k]
                b_idx = indices[k + 1]
                ax = pos[id(sorted_eps[a_idx])][0]
                bx = pos[id(sorted_eps[b_idx])][0]
                if abs(ax - bx) == 1:
                    union(a_idx, b_idx)

        # Step 2: bridge merging within max_distance
        while True:
            roots = {find(i) for i in range(n)}
            if len(roots) <= 1:
                break

            best_i: int = -1
            best_j: int = -1
            best_dist = max_distance + 1
            best_x: int = -(1 << 30)
            best_y: int = -1

            for i in range(n):
                ri = find(i)
                pi = pos[id(sorted_eps[i])]
                for j in range(i + 1, n):
                    rj = find(j)
                    if ri == rj:
                        continue
                    pj = pos[id(sorted_eps[j])]
                    d = _chebyshev(pi, pj)
                    if d > max_distance:
                        continue
                    mx = min(pi[0], pj[0])
                    my = max(pi[1], pj[1])
                    better = False
                    if mx > best_x:
                        better = True
                    elif mx == best_x:
                        if d < best_dist:
                            better = True
                        elif d == best_dist and my > best_y:
                            better = True
                    if better:
                        best_dist = d
                        best_x = mx
                        best_y = my
                        best_i = i
                        best_j = j

            if best_i < 0:
                break
            union(best_i, best_j)

        roots = {find(i) for i in range(n)}
        if len(roots) > 1:
            unreachable_count += 1
            # Build component→endpoints map
            comps: dict[int, list[str]] = {}
            for i in range(n):
                comps.setdefault(find(i), []).append(sorted_eps[i].entity_id)

            comp_labels: list[str] = []
            for comp_ids in comps.values():
                labels_set: set[str] = set()
                for eid in comp_ids:
                    matched = "?"
                    if prefixes:
                        for pfx, label in prefix_to_label.items():
                            if eid.startswith(pfx):
                                matched = label
                                break
                    labels_set.add(matched)
                comp_labels.append(" + ".join(sorted(labels_set)))

            sys.stderr.write(
                f"  Warning: network {net.network_id!r} ({net.color}) has "
                f"{len(roots)} disconnected component(s) "
                f"({', '.join(comp_labels)}) — "
                f"entities are too far apart (> {max_distance} tiles). "
                f"Consider rearranging the layout.\n"
            )

    return unreachable_count


# ═══════════════════════════════════════════════════════════════════════
# Legacy API (deprecated — kept for backward compat during migration)
# ═══════════════════════════════════════════════════════════════════════


class Composer:
    """Legacy composer class — use :func:`compose` instead."""

    def __init__(self, output_name: str = "All-in-One Media Player"):
        self.output_name = output_name
        self._display: LogicalBlueprint | None = None
        self._audio_player: LogicalBlueprint | None = None
        self._video_memory: LogicalBlueprint | None = None
        self._audio_memory: LogicalBlueprint | None = None
        self._timer: LogicalBlueprint | None = None
        self._progress_bar: LogicalBlueprint | None = None
        self._pole_type: str | None = None

    def set_display(self, lb: LogicalBlueprint) -> None:
        self._display = lb

    def set_audio_player(self, lb: LogicalBlueprint) -> None:
        self._audio_player = lb

    def set_video_memory(self, lb: LogicalBlueprint) -> None:
        self._video_memory = lb

    def set_audio_memory(self, lb: LogicalBlueprint) -> None:
        self._audio_memory = lb

    def set_timer(self, lb: LogicalBlueprint) -> None:
        self._timer = lb

    def set_progress_bar(self, lb: LogicalBlueprint) -> None:
        self._progress_bar = lb

    def set_power(self, pole_type: str) -> None:
        self._pole_type = pole_type

    def compose(self) -> LogicalBlueprint:
        """Legacy compose — use :func:`compose` instead."""
        from .progress_bar import build_progress_bar

        merged = LogicalBlueprint(label=self.output_name)
        display_bounds = (0, 0, 0, 0)

        if self._display is not None:
            merged.merge(self._display)
            display_bounds = _entity_bounding_box(merged)

        timer_lb = self._timer
        progress_lb = self._progress_bar

        if timer_lb is None:
            timer_lb = LogicalBlueprint(label="Timer")
            raw = build_raw_timer("Raw Clock")
            mod = build_mod_timer(60, name="Sub Tick")
            _assign_tile_positions(mod, start_x=0, start_y=4)
            timer_lb.merge(raw)
            timer_lb.merge(mod, entity_prefix="mod_", network_prefix="mod_")
            _connect_nets_by_color(
                timer_lb, "red",
                entity_contains="raw_clock", port="output",
                other_entity_contains="mod_sub", other_port="input",
            )

        if progress_lb is None and self._display is not None:
            progress_lb = build_progress_bar(
                "Progress", length=10, signal_name="signal-clock", max_value=59,
            )

        min_x, min_y, max_x, max_y = display_bounds

        components: list[tuple[str, LogicalBlueprint | None, str, str]] = [
            ("timer", timer_lb, "tm_", "tm_"),
            ("progress", progress_lb, "pb_", "pb_"),
            ("video_memory", self._video_memory, "vm_", "vm_"),
            ("audio_memory", self._audio_memory, "am_", "am_"),
            ("audio_player", self._audio_player, "ap_", "ap_"),
        ]

        col_x = max_x + 2
        next_y = min_y

        for _name, comp_lb, ent_prefix, net_prefix in components:
            if comp_lb is None:
                continue
            _assign_tile_positions(comp_lb, start_x=col_x, start_y=next_y)
            comp_bounds = _entity_bounding_box(comp_lb)
            merged.merge(comp_lb, entity_prefix=ent_prefix, network_prefix=net_prefix)
            next_y = comp_bounds[3] + 2

        # ── Port connections (legacy: guess by entity id patterns) ──
        subtick_net_id: str | None = None
        dc_input_net_id: str | None = None
        dc_output_net_id: str | None = None
        lamp_net_id: str | None = None
        pb_net_id: str | None = None

        for net in merged.networks:
            if net.color != "red" or not net.endpoints:
                continue
            for ep in net.endpoints:
                eid = ep.entity_id
                if "sub" in eid and "mod" in eid and ep.port == "output":
                    subtick_net_id = net.network_id
                if eid.startswith("vm_") and ep.port == "input":
                    dc_input_net_id = net.network_id
                if eid.startswith("vm_") and ep.port == "output":
                    dc_output_net_id = net.network_id
                ent = merged.entities.get(eid)
                if ent and ent.type == "small-lamp" and not eid.startswith("pb_"):
                    lamp_net_id = net.network_id
                if eid.startswith("pb_") and ep.port == "input":
                    pb_net_id = net.network_id

        if subtick_net_id and dc_input_net_id:
            _connect_networks(merged, subtick_net_id, dc_input_net_id)
        if dc_output_net_id and lamp_net_id:
            _connect_networks(merged, dc_output_net_id, lamp_net_id)
        if subtick_net_id and pb_net_id:
            _connect_networks(merged, subtick_net_id, pb_net_id)

        if self._pole_type is not None:
            sys.stderr.write(
                "Warning: --power is not yet implemented; "
                "power poles will not be added.\n"
            )

        unreachable = _validate_network_reachability(merged)
        if unreachable > 0:
            raise ValueError(
                f"Legacy composition failed: {unreachable} network(s) have "
                f"endpoints that are too far apart (> 64 tiles)."
            )

        return merged


def compose_all_in_one(
    display_lb: LogicalBlueprint | None = None,
    audio_player_lb: LogicalBlueprint | None = None,
    video_memory_lb: LogicalBlueprint | None = None,
    audio_memory_lb: LogicalBlueprint | None = None,
    timer_lb: LogicalBlueprint | None = None,
    progress_bar_lb: LogicalBlueprint | None = None,
    pole_type: str | None = None,
    output_name: str = "All-in-One Media Player",
    use_cache: bool = True,
    cache_key_parts: Sequence[str] = (),
) -> LogicalBlueprint:
    """Legacy all-in-one composer — use :func:`compose` instead."""
    if use_cache and cache_key_parts:
        cached = cache_get(output_name, *cache_key_parts)
        if cached is not None:
            return cached

    c = Composer(output_name=output_name)
    if display_lb is not None:
        c.set_display(display_lb)
    if audio_player_lb is not None:
        c.set_audio_player(audio_player_lb)
    if video_memory_lb is not None:
        c.set_video_memory(video_memory_lb)
    if audio_memory_lb is not None:
        c.set_audio_memory(audio_memory_lb)
    if timer_lb is not None:
        c.set_timer(timer_lb)
    if progress_bar_lb is not None:
        c.set_progress_bar(progress_bar_lb)
    if pole_type is not None:
        c.set_power(pole_type)

    result = c.compose()

    if use_cache and cache_key_parts:
        cache_put(result, output_name, *cache_key_parts)

    return result
