"""Media encoder — converts video, GIF, PNG series, and still images into
Factorio animation-memory blueprint strings.

All encoders share a common :func:`encode_frames` pipeline that builds the
decider-combinator chains (with embedded frame data) from an iterable of RGB frames.
"""

from __future__ import annotations

import json
import math
import os
import sys
import pickle
import hashlib
import concurrent.futures
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np
from draftsman.blueprintable import Blueprint
from draftsman.constants import Direction
from draftsman.entity import DeciderCombinator, new_entity

from ..integer2signal.mapping import SignalMapping

from .. import (
    CLOCK_SIGNAL,
    DISPLAY_HEIGHT,
    DISPLAY_WIDTH,
    HOLE_BOTTOM_RIGHT,
    HOLE_TOP_LEFT,
    QUALITIES,
    SIGNAL_POOL,
)

try:
    from tqdm import tqdm
except ImportError:
    class tqdm:
        """Graceful fallback if tqdm is not installed."""
        def __init__(self, iterable=None, *args, **kwargs):
            self.iterable = iterable or []
        def __iter__(self):
            yield from self.iterable
        def update(self, n=1):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass


# ═══════════════════════════════════════════════════════════════════════
# Dimension resolution — auto-calculate omitted dimension + unit rounding
# ═══════════════════════════════════════════════════════════════════════

def resolve_dimensions(
    source_w: int,
    source_h: int,
    user_w: int | None = None,
    user_h: int | None = None,
    *,
    round_units: bool = True,
    unit_w: int = DISPLAY_WIDTH,
    unit_h: int = DISPLAY_HEIGHT,
) -> tuple[int, int]:
    """Compute the final ``(total_w, total_h)`` for frame resizing.

    Parameters
    ----------
    source_w, source_h : int
        Original media dimensions (pixels).
    user_w, user_h : int or None
        User-specified overrides from ``--width`` / ``--height``.
    round_units : bool
        If True (default), round each dimension **up** to the nearest
        multiple of *unit_w* / *unit_h* so no display units are partially
        filled.
    unit_w, unit_h : int
        Tile dimensions of a single display unit (default 28×28).

    Returns
    -------
    (total_w, total_h) : tuple[int, int]
    """
    if user_w is not None and user_h is not None:
        w, h = user_w, user_h
    elif user_w is not None:
        h = max(1, round(user_w * source_h / source_w))
        w = user_w
    elif user_h is not None:
        w = max(1, round(user_h * source_w / source_h))
        h = user_h
    else:
        # Neither specified — use the single-unit default (current behaviour)
        w, h = unit_w, unit_h

    if round_units:
        w = ((w + unit_w - 1) // unit_w) * unit_w
        h = ((h + unit_h - 1) // unit_h) * unit_h

    return w, h


def _frame_diff(a: np.ndarray, b: np.ndarray) -> float:
    """Return 0.0–1.0 normalised mean absolute difference between two RGB frames."""
    return float(np.mean(np.abs(a.astype(np.float32) - b.astype(np.float32))) / 255.0)


# ═══════════════════════════════════════════════════════════════════════
# Core blueprint builder (extracted for reuse by chunked encoder)
# ═══════════════════════════════════════════════════════════════════════

def _encode_frames_core(
    kept_frames: list[np.ndarray],
    tick_ranges: list[tuple[int, int]],
    output_name: str,
    deduplicate: bool,
    mapping_params: dict,
    total_w: int,
    total_h: int,
    unit_w: int,
    unit_h: int,
    unit_cols: int,
    unit_rows: int,
    clock: str,
    current_tick: int,
    label_suffix: str = "",
) -> str:
    """Build a blueprint string from pre-processed frame data.

    This is the second half of :func:`encode_frames` — it takes already-resized
    and adaptive-dropped frames plus tick ranges, and produces the combinator
    blueprint.  It is a top-level function so :class:`~concurrent.futures.ProcessPoolExecutor`
    can serialise it.
    """
    # Reconstruct the SignalMapping from serialisable params inside the worker.
    mapping = SignalMapping(**mapping_params)

    num_units = unit_cols * unit_rows
    total_input = len(kept_frames)
    if total_input == 0:
        sys.stderr.write("No frames to encode.\n")
        return ""

    # ==================================================================
    # Single-unit path
    # ==================================================================
    if num_units == 1:
        frame_entries = [(f, s, e) for f, (s, e) in zip(kept_frames, tick_ranges)]

        if deduplicate:
            seen: dict[str, tuple[np.ndarray, list[tuple[int, int]]]] = {}
            order: list[str] = []
            for resized, start, end in frame_entries:
                h = hashlib.sha256(resized.tobytes()).hexdigest()
                if h not in seen:
                    seen[h] = (resized, [])
                    order.append(h)
                seen[h][1].append((start, end))
            unique_frames = [seen[h] for h in order]
        else:
            unique_frames = [(resized, [(start, end)]) for resized, start, end in frame_entries]

        blueprint = Blueprint()
        blueprint.label = f"Video Memory: {output_name}{label_suffix}"
        blueprint.icons = ["parameter-0"]

        total = len(unique_frames)
        cols = max(1, math.isqrt(max(0, 2 * total - 1)) + 1) if total > 0 else 1
        if cols > 26:
            cols = 26
        rows = (total + cols - 1) // cols

        dc_grid: dict[tuple[int, int], str] = {}
        for gate_num, (resized, ranges) in enumerate(unique_frames, start=1):
            idx = gate_num - 1
            col = idx % cols
            row = idx // cols
            dc_id = f"gate_{gate_num}"

            conditions: list = []
            for start, end in ranges:
                if start == end:
                    conditions.append(
                        DeciderCombinator.Condition(
                            first_signal={"name": clock},
                            comparator="=",
                            constant=start,
                        )
                    )
                else:
                    conditions.append(
                        DeciderCombinator.Condition(
                            first_signal={"name": clock},
                            comparator=">=",
                            constant=start,
                        )
                    )
                    conditions.append(
                        DeciderCombinator.Condition(
                            first_signal={"name": clock},
                            comparator="<=",
                            constant=end,
                            compare_type="and",
                        )
                    )

            outputs: list = []
            for (x, y), sig in mapping.iter_pixels():
                r, g, b = resized[y, x]
                color_int = (int(r) << 16) | (int(g) << 8) | int(b)
                if color_int > 0:
                    outputs.append(
                        DeciderCombinator.Output(
                            signal={"name": sig["name"], "quality": sig["quality"]},
                            copy_count_from_input=False,
                            constant=color_int,
                        )
                    )

            dc = new_entity(
                "decider-combinator",
                id=dc_id,
                tile_position=(col, row * 2),
                direction=Direction.SOUTH,
            )
            dc.conditions = conditions
            dc.outputs = outputs
            blueprint.entities.append(dc)
            dc_grid[(row, col)] = dc_id

        prev_id: str | None = None
        for r in range(rows):
            col_iter = range(cols) if r % 2 == 0 else range(cols - 1, -1, -1)
            for c in col_iter:
                dc_id = dc_grid.get((r, c))
                if dc_id is None:
                    continue
                if prev_id is not None:
                    blueprint.add_circuit_connection(
                        "green", prev_id, dc_id, side_1="input", side_2="input"
                    )
                    blueprint.add_circuit_connection(
                        "red", prev_id, dc_id, side_1="output", side_2="output"
                    )
                prev_id = dc_id

        total_ticks = current_tick - 1
        total_combinators = len(unique_frames)
        if deduplicate and total_combinators < total_input:
            sys.stderr.write(
                f"\nEncoded {total_combinators} combinators for {total_input} frames "
                f"({total_input - total_combinators} deduplicated) "
                f"over {total_ticks} ticks.\n"
            )
        else:
            sys.stderr.write(
                f"\nEncoded {total_input} frames over {total_ticks} ticks "
                f"(~{total_ticks / max(1, total_input):.1f} tick(s)/frame).\n"
            )
        return blueprint.to_string()

    # ==================================================================
    # Multi-unit path
    # ==================================================================

    # Phase 2 — split into per-unit regions
    unit_entries: list[list[tuple[np.ndarray, int, int]]] = [
        [] for _ in range(num_units)
    ]
    for frame, (start, end) in zip(kept_frames, tick_ranges):
        for ur in range(unit_rows):
            for uc in range(unit_cols):
                ui = ur * unit_cols + uc
                y0 = ur * unit_h
                y1 = min((ur + 1) * unit_h, total_h)
                x0 = uc * unit_w
                x1 = min((uc + 1) * unit_w, total_w)
                region = frame[y0:y1, x0:x1]
                if region.shape[0] < unit_h or region.shape[1] < unit_w:
                    padded = np.zeros((unit_h, unit_w, 3), dtype=np.uint8)
                    padded[: region.shape[0], : region.shape[1]] = region
                    region = padded
                unit_entries[ui].append((region, start, end))

    # Phase 3 — deduplicate per unit
    unit_unique: list[list[tuple[np.ndarray, list[tuple[int, int]]]]] = []
    for entries in unit_entries:
        if deduplicate:
            seen: dict[str, tuple[np.ndarray, list[tuple[int, int]]]] = {}
            order: list[str] = []
            for resized, start, end in entries:
                h = hashlib.sha256(resized.tobytes()).hexdigest()
                if h not in seen:
                    seen[h] = (resized, [])
                    order.append(h)
                seen[h][1].append((start, end))
            unit_unique.append([seen[h] for h in order])
        else:
            current_unique = []
            for resized, start, end in entries:
                current_unique.append((resized, [(start, end)]))
            unit_unique.append(current_unique)

    # Phase 4 — build blueprint: per-unit grids, padding, direct wiring
    blueprint = Blueprint()
    blueprint.label = (
        f"Video Memory: {output_name}{label_suffix} "
        f"({total_w}×{total_h}, {unit_cols}×{unit_rows} units)"
    )

    unit_grids: list[tuple[int, int, int]] = []
    for ui, unique_frames in enumerate(unit_unique):
        t = len(unique_frames)
        c = max(1, math.isqrt(max(0, 2 * t - 1)) + 1) if t > 0 else 1
        if c > 26:
            c = 26
        r = (t + c - 1) // c
        missing = c * r - t
        if missing > 0:
            dummy = np.zeros((unit_h, unit_w, 3), dtype=np.uint8)
            dummy_ranges = [(999999, 999999)]
            for _ in range(missing):
                unique_frames.append((dummy, dummy_ranges))
        unit_grids.append((c * r, c, r))

    MARGIN = 2

    row_max_rows: list[int] = []
    for ur in range(unit_rows):
        mr = 0
        for uc in range(unit_cols):
            _, _, gr = unit_grids[ur * unit_cols + uc]
            mr = max(mr, gr)
        row_max_rows.append(mr)

    unit_origins: list[tuple[int, int]] = []
    cum_row = 0
    for ur in range(unit_rows):
        cum_col = 0
        for uc in range(unit_cols):
            unit_origins.append((cum_col, cum_row))
            _, c, _ = unit_grids[ur * unit_cols + uc]
            cum_col += c + MARGIN
        cum_row += row_max_rows[ur] * 2 + MARGIN

    unit_top_left: dict[int, str] = {}
    unit_top_right: dict[int, str] = {}
    unit_bottom_right: dict[int, str] = {}

    for ui, (unique_frames, (total, cols, rows)) in enumerate(zip(unit_unique, unit_grids)):
        ox, oy = unit_origins[ui]
        dc_grid: dict[tuple[int, int], str] = {}

        for gate_num, (resized, ranges) in enumerate(unique_frames, start=1):
            idx = gate_num - 1
            col = ox + (idx % cols)
            row = oy + (idx // cols) * 2
            dc_id = f"unit{ui}_gate_{gate_num}"

            conditions: list = []
            for start, end in ranges:
                if start == end:
                    conditions.append(
                        DeciderCombinator.Condition(
                            first_signal={"name": clock},
                            comparator="=",
                            constant=start,
                        )
                    )
                else:
                    conditions.append(
                        DeciderCombinator.Condition(
                            first_signal={"name": clock},
                            comparator=">=",
                            constant=start,
                        )
                    )
                    conditions.append(
                        DeciderCombinator.Condition(
                            first_signal={"name": clock},
                            comparator="<=",
                            constant=end,
                            compare_type="and",
                        )
                    )

            outputs: list = []
            if ranges != [(999999, 999999)]:
                for (x, y), sig in mapping.iter_pixels():
                    r, g, b = resized[y, x]
                    color_int = (int(r) << 16) | (int(g) << 8) | int(b)
                    if color_int > 0:
                        outputs.append(
                            DeciderCombinator.Output(
                                signal={"name": sig["name"], "quality": sig["quality"]},
                                copy_count_from_input=False,
                                constant=color_int,
                            )
                        )

            dc = new_entity(
                "decider-combinator",
                id=dc_id,
                tile_position=(col, row),
                direction=Direction.SOUTH,
            )
            dc.conditions = conditions
            dc.outputs = outputs
            blueprint.entities.append(dc)
            dc_grid[(idx // cols, idx % cols)] = dc_id

        # ---- wire within this unit (green + red, same snake) ----------
        prev_id: str | None = None
        for r in range(rows):
            col_iter = range(cols) if r % 2 == 0 else range(cols - 1, -1, -1)
            for c in col_iter:
                dc_id = dc_grid.get((r, c))
                if dc_id is None:
                    continue
                if prev_id is not None:
                    blueprint.add_circuit_connection(
                        "green", prev_id, dc_id, side_1="input", side_2="input"
                    )
                    blueprint.add_circuit_connection(
                        "red", prev_id, dc_id, side_1="output", side_2="output"
                    )
                prev_id = dc_id

        unit_top_left[ui] = dc_grid[(0, 0)]
        unit_top_right[ui] = dc_grid[(0, cols - 1)]
        unit_bottom_right[ui] = dc_grid[(rows - 1, cols - 1)]

    # ---- direct inter-unit green wiring (no poles) ------------------------
    for ur in range(unit_rows):
        for uc in range(unit_cols - 1):
            ui_left = ur * unit_cols + uc
            ui_right = ur * unit_cols + uc + 1
            blueprint.add_circuit_connection(
                "green", unit_top_right[ui_left], unit_top_left[ui_right],
                side_1="input", side_2="input",
            )

    for ur in range(unit_rows - 1):
        for uc in range(unit_cols):
            ui_top = ur * unit_cols + uc
            ui_bot = (ur + 1) * unit_cols + uc
            blueprint.add_circuit_connection(
                "green", unit_bottom_right[ui_top],
                unit_top_right[ui_bot],
                side_1="input", side_2="input",
            )

    total_combinators = sum(len(uf) for uf in unit_unique)
    sys.stderr.write(
        f"\nEncoded {total_input} frames "
        f"→ {total_combinators} combinators across {num_units} display units "
        f"({unit_cols}×{unit_rows} grid, unit size {unit_w}×{unit_h}).\n"
    )
    return blueprint.to_string()


# ═══════════════════════════════════════════════════════════════════════
# Chunk-cache helpers
# ═══════════════════════════════════════════════════════════════════════

def _chunk_cache_dir(source_id: str, time_chunks: int, total_w: int, total_h: int,
                     fps: float, adaptive: bool, threshold: float, deduplicate: bool) -> Path:
    """Return a unique cache directory for a chunked encode run."""
    key = f"{source_id}_{total_w}x{total_h}_fps{fps}"
    if adaptive:
        key += f"_adp{threshold:.3f}"
    if deduplicate:
        key += "_dedup"
    key += f"_tc{time_chunks}"
    safe = hashlib.md5(key.encode()).hexdigest()[:12]
    return Path(f".encode_chunks_{safe}")


def _chunk_meta_path(cache_dir: Path) -> Path:
    return cache_dir / "meta.json"


# ═══════════════════════════════════════════════════════════════════════
# ProcessPoolExecutor worker (top-level so it can be pickled)
# ═══════════════════════════════════════════════════════════════════════

def _build_chunk_worker(payload: bytes) -> tuple[int, str]:
    """Build a single time-chunk blueprint in a worker process.

    Returns ``(chunk_idx, blueprint_string)``.
    """
    data = pickle.loads(payload)
    chunk_idx = data["chunk_idx"]
    bp_str = _encode_frames_core(
        kept_frames=data["kept_frames"],
        tick_ranges=data["tick_ranges"],
        output_name=data["output_name"],
        deduplicate=data["deduplicate"],
        mapping_params=data["mapping_params"],
        total_w=data["total_w"],
        total_h=data["total_h"],
        unit_w=data["unit_w"],
        unit_h=data["unit_h"],
        unit_cols=data["unit_cols"],
        unit_rows=data["unit_rows"],
        clock=data["clock"],
        current_tick=data["current_tick"],
        label_suffix=data.get("label_suffix", ""),
    )
    return chunk_idx, bp_str


# ═══════════════════════════════════════════════════════════════════════
# Merge helpers
# ═══════════════════════════════════════════════════════════════════════

def _max_col_in_blueprint(bp: Blueprint) -> int:
    """Return the maximum tile X coordinate of any entity in the blueprint."""
    max_c = 0
    for e in bp.entities:
        tp = getattr(e, "tile_position", None)
        if tp is not None:
            max_c = max(max_c, tp[0])
    return max_c


def _merge_chunk_blueprints(
    chunk_bps: list[str],
    output_name: str,
    deduplicate_cross: bool = False,
    total_ticks: int = 0,
) -> str:
    """Merge multiple time-chunk blueprint strings into one.

    Each chunk's entities are placed at increasing X offsets.  Green (input)
    and red (output) wires connect the last DC of chunk *i* to the first DC
    of chunk *i+1*.
    """
    if not chunk_bps:
        return ""

    non_empty = [bp for bp in chunk_bps if bp and bp.strip()]
    if not non_empty:
        return ""

    if len(non_empty) == 1:
        return non_empty[0]

    parsed: list[Blueprint] = []
    for bp_str in non_empty:
        parsed.append(Blueprint.from_string(bp_str))

    if deduplicate_cross:
        sys.stderr.write("Cross-chunk deduplication started (this may be slow)…\n")
        return _merge_with_cross_dedup(parsed, output_name, total_ticks)

    # ── Build merged blueprint with fresh entities ────────────────────
    merged = Blueprint()
    merged.label = f"Video Memory: {output_name}"

    # Per-chunk: first and last DC entity IDs in merged (for inter-chunk wiring)
    chunk_first_id: list[str] = []
    chunk_last_id: list[str] = []

    cum_x_offset = 0.0
    MARGIN = 2

    for ci, bp in enumerate(parsed):
        entity_dicts = bp.to_dict().get("blueprint", {}).get("entities", [])

        if not entity_dicts:
            chunk_first_id.append("")
            chunk_last_id.append("")
            continue

        # Map: id(old_entity_object) → new entity ID string
        old_obj_to_new_id: dict[int, str] = {}

        first_dc_id = ""
        last_dc_id = ""
        first_dc_pos = (1e9, 1e9)
        last_dc_pos = (-1, -1)

        for old_e in bp.entities:
            name = getattr(old_e, "name", "")
            tp = getattr(old_e, "tile_position", None)
            if tp is None:
                continue
            x = tp[0] + cum_x_offset
            y = tp[1]
            eid = f"c{ci}_e{len(merged.entities)}"

            # Create fresh entity with same properties
            e = new_entity(name, id=str(eid), tile_position=(int(x), int(y)))

            # Copy combinator-specific properties from old entity
            # Decider combinators
            if "decider-combinator" in name:
                old_conds = getattr(old_e, "conditions", None)
                old_outs = getattr(old_e, "outputs", None)
                old_dir = getattr(old_e, "direction", None)
                if old_conds is not None:
                    e.conditions = list(old_conds)
                if old_outs is not None:
                    e.outputs = list(old_outs)
                if old_dir is not None:
                    e.direction = old_dir
                # Track first/last DC
                if y < first_dc_pos[1] or (y == first_dc_pos[1] and x < first_dc_pos[0]):
                    first_dc_pos = (x, y)
                    first_dc_id = str(eid)
                if y > last_dc_pos[1] or (y == last_dc_pos[1] and x > last_dc_pos[0]):
                    last_dc_pos = (x, y)
                    last_dc_id = str(eid)

            # Arithmetic combinators
            elif "arithmetic-combinator" in name:
                old_cond = getattr(old_e, "arithmetic_condition", None)
                old_dir = getattr(old_e, "direction", None)
                if old_cond is not None:
                    e.set_arithmetic_condition(
                        first_operand=getattr(old_cond, "first_operand", None),
                        operation=getattr(old_cond, "operation", None),
                        second_operand=getattr(old_cond, "second_operand", None),
                        output_signal=getattr(old_cond, "output_signal", None),
                    )
                if old_dir is not None:
                    e.direction = old_dir

            # Constant combinators
            elif "constant-combinator" in name:
                old_signals = getattr(old_e, "signals", None)
                if old_signals is not None:
                    for slot, sig in enumerate(old_signals):
                        if sig is not None:
                            e.set_signal(slot, sig)

            # Speakers
            elif "programmable-speaker" in name:
                for attr in ("instrument_name", "note_name", "volume_signal",
                             "volume_controlled_by_signal", "allow_polyphony",
                             "circuit_enabled"):
                    val = getattr(old_e, attr, None)
                    if val is not None:
                        setattr(e, attr, val)

            merged.entities.append(e)
            old_obj_to_new_id[id(old_e)] = str(eid)

        # ── Copy intra-chunk wires ────────────────────────────────────
        for wire in getattr(bp, "wires", []):
            if len(wire) < 4:
                continue
            old_e1, wire_type_1, old_e2, wire_type_2 = wire[0], wire[1], wire[2], wire[3]
            new_id1 = old_obj_to_new_id.get(id(old_e1))
            new_id2 = old_obj_to_new_id.get(id(old_e2))
            if not new_id1 or not new_id2:
                continue

            # wire_type: 2 = green, 3 = red (from draftsman's internal encoding)
            # Actual Factorio: color 1=red, 2=green; circuit_id 1=input, 2=output
            # draftsman encodes: wire type = color + (side * 2?) 
            # Observed: green wire with input→input = 2, red wire with output→output = 3
            # Let's just add both green+red for robustness
            if wire_type_1 in (2,) and wire_type_2 in (2,):
                try:
                    merged.add_circuit_connection(
                        "green", new_id1, new_id2,
                        side_1="input", side_2="input",
                    )
                except Exception:
                    pass
            if wire_type_1 in (3,) and wire_type_2 in (3,):
                try:
                    merged.add_circuit_connection(
                        "red", new_id1, new_id2,
                        side_1="output", side_2="output",
                    )
                except Exception:
                    pass

        chunk_first_id.append(first_dc_id)
        chunk_last_id.append(last_dc_id)

        # Compute next X offset
        cum_x_offset += _max_col_in_blueprint(bp) + 1 + MARGIN

    # ── inter-chunk wiring ────────────────────────────────────────────
    for ci in range(len(non_empty) - 1):
        prev_last = chunk_last_id[ci]
        next_first = chunk_first_id[ci + 1]
        if prev_last and next_first:
            merged.add_circuit_connection(
                "green", prev_last, next_first,
                side_1="input", side_2="input",
            )
            merged.add_circuit_connection(
                "red", prev_last, next_first,
                side_1="output", side_2="output",
            )

    sys.stderr.write(
        f"Merged {len(non_empty)} time chunks "
        f"→ {len(merged.entities)} entities.\n"
    )
    return merged.to_string()


def _merge_with_cross_dedup(
    parsed: list[Blueprint],
    output_name: str,
    total_ticks: int,
) -> str:
    """Merge chunks with cross-chunk deduplication.

    Identical DC outputs across chunks are merged: their tick-range conditions
    are combined into a single DC, reducing the total combinator count.
    """
    # Collect all DCs from all chunks
    # Key: hash of (output_signals, output_values) → merged conditions
    from collections import OrderedDict

    # dc_signature → (conditions_list, first_entity_for_reference)
    merged_dcs: dict[str, dict] = OrderedDict()

    for bp in parsed:
        for e in bp.entities:
            if "decider-combinator" not in e.name:
                continue
            # Build a signature from the outputs
            outputs = getattr(e, "outputs", []) or []
            sig_parts: list[str] = []
            for o in outputs:
                sig = getattr(o, "signal", None)
                const = getattr(o, "constant", None)
                if sig is not None:
                    # SignalID from draftsman has .name and .quality attributes
                    sig_name = getattr(sig, "name", None) or str(sig)
                    sig_qual = getattr(sig, "quality", "normal") or "normal"
                    sig_parts.append(f"{sig_name}|{sig_qual}={const}")
            sig_key = "||".join(sorted(sig_parts))

            conditions = getattr(e, "conditions", []) or []
            cond_parts: list[str] = []
            for c in conditions:
                fs = getattr(c, "first_signal", None)
                comp = getattr(c, "comparator", "")
                const = getattr(c, "constant", 0)
                if fs is not None:
                    fs_name = getattr(fs, "name", None) or str(fs)
                    cond_parts.append(f"{fs_name}{comp}{const}")

            if sig_key not in merged_dcs:
                merged_dcs[sig_key] = {
                    "conditions": [],  # list of (start_tick, end_tick)
                    "outputs": outputs,
                    "clock_signal": "",
                }

            # Parse conditions into tick ranges
            tick_ranges_for_dc: list[tuple[int, int]] = []
            i = 0
            cond_list = conditions
            while i < len(cond_list):
                c = cond_list[i]
                fs = getattr(c, "first_signal", None)
                comp = getattr(c, "comparator", "")
                const = getattr(c, "constant", 0)
                clock_name = ""
                if fs is not None:
                    clock_name = getattr(fs, "name", None) or str(fs)
                    merged_dcs[sig_key]["clock_signal"] = clock_name

                if comp == "=":
                    tick_ranges_for_dc.append((const, const))
                    i += 1
                elif comp == ">=":
                    start = const
                    if i + 1 < len(cond_list):
                        c2 = cond_list[i + 1]
                        if getattr(c2, "comparator", "") == "<=":
                            end = getattr(c2, "constant", start)
                            tick_ranges_for_dc.append((start, end))
                            i += 2
                        else:
                            i += 1
                    else:
                        i += 1
                else:
                    i += 1

            merged_dcs[sig_key]["conditions"].extend(tick_ranges_for_dc)

    # Build the merged blueprint
    merged = Blueprint()
    merged.label = f"Video Memory: {output_name} (cross-dedup)"

    total = len(merged_dcs)
    cols = max(1, math.isqrt(max(0, 2 * total - 1)) + 1) if total > 0 else 1
    if cols > 26:
        cols = 26
    rows = (total + cols - 1) // cols

    dc_grid: dict[tuple[int, int], str] = {}

    for gate_num, (sig_key, info) in enumerate(merged_dcs.items(), start=1):
        idx = gate_num - 1
        col = idx % cols
        row = idx // cols
        dc_id = f"gate_{gate_num}"

        # Build conditions from merged tick ranges
        clock = info["clock_signal"]
        conditions: list = []
        for start, end in info["conditions"]:
            if start == end:
                conditions.append(
                    DeciderCombinator.Condition(
                        first_signal={"name": clock},
                        comparator="=",
                        constant=start,
                    )
                )
            else:
                conditions.append(
                    DeciderCombinator.Condition(
                        first_signal={"name": clock},
                        comparator=">=",
                        constant=start,
                    )
                )
                conditions.append(
                    DeciderCombinator.Condition(
                        first_signal={"name": clock},
                        comparator="<=",
                        constant=end,
                        compare_type="and",
                    )
                )

        dc = new_entity(
            "decider-combinator",
            id=dc_id,
            tile_position=(col, row * 2),
            direction=Direction.SOUTH,
        )
        dc.conditions = conditions
        dc.outputs = info["outputs"]
        merged.entities.append(dc)
        dc_grid[(row, col)] = dc_id

    # Snake wiring
    prev_id: str | None = None
    for r in range(rows):
        col_iter = range(cols) if r % 2 == 0 else range(cols - 1, -1, -1)
        for c in col_iter:
            dc_id = dc_grid.get((r, c))
            if dc_id is None:
                continue
            if prev_id is not None:
                merged.add_circuit_connection(
                    "green", prev_id, dc_id, side_1="input", side_2="input"
                )
                merged.add_circuit_connection(
                    "red", prev_id, dc_id, side_1="output", side_2="output"
                )
            prev_id = dc_id

    original_total = sum(len([e for e in bp.entities if "decider-combinator" in e.name])
                         for bp in parsed)
    sys.stderr.write(
        f"Cross-chunk dedup: {original_total} → {total} combinators "
        f"({original_total - total} removed) over {total_ticks} ticks.\n"
    )
    return merged.to_string()


def encode_frames(
    rgb_frames: Iterator[np.ndarray],
    output_name: str,
    fps: float = 0.0,
    adaptive: bool = False,
    threshold: float = 0.03,
    deduplicate: bool = False,
    mapping: SignalMapping | None = None,
    total_width: int | None = None,
    total_height: int | None = None,
    expected_frames: int | None = None,
    source_id: str = "",
) -> str:
    """Encode an iterable of RGB ``(H, W, 3)`` uint8 frames into a blueprint."""
    if fps <= 0:
        fps = 60.0
    fps = max(1.0, min(fps, 60.0))
    ticks_float = 60.0 / fps

    if mapping is None:
        mapping = SignalMapping(
            DISPLAY_WIDTH,
            DISPLAY_HEIGHT,
            HOLE_TOP_LEFT,
            HOLE_BOTTOM_RIGHT,
            QUALITIES,
            SIGNAL_POOL,
        )

    unit_w = DISPLAY_WIDTH
    unit_h = DISPLAY_HEIGHT
    total_w = total_width if total_width is not None else unit_w
    total_h = total_height if total_height is not None else unit_h
    unit_cols = math.ceil(total_w / unit_w)
    unit_rows = math.ceil(total_h / unit_h)
    num_units = unit_cols * unit_rows

    clock = CLOCK_SIGNAL

    # ==================================================================
    # Phase 0 & 1: Parallel Resizing, Adaptive Dropping, and Caching
    # ==================================================================
    
    # Generate a unique cache name based on inputs to prevent cross-run collisions
    hash_str = f"{source_id}_{output_name}_{total_w}_{total_h}_{fps}_{adaptive}_{threshold}"
    safe_name = hashlib.md5(hash_str.encode('utf-8')).hexdigest()[:8]
    cache_file = Path(f".encode_cache_{safe_name}.pkl")
    loaded_from_cache = False

    kept_frames: list[np.ndarray] = []
    tick_ranges: list[tuple[int, int]] = []
    current_tick = 1

    if cache_file.exists():
        sys.stderr.write(f"Found cache {cache_file}, loading intermediate results...\n")
        try:
            with open(cache_file, "rb") as f:
                cache_data = pickle.load(f)
                kept_frames = cache_data["frames"]
                tick_ranges = cache_data["ticks"]
                current_tick = cache_data.get("current_tick", 1)
            loaded_from_cache = True
        except Exception as e:
            sys.stderr.write(f"Failed to load cache: {e}\n")

    if not loaded_from_cache:
        sys.stderr.write("Decoding, resizing, and processing frames...\n")
        
        def _resize_task(rgb):
            resized = cv2.resize(rgb, (total_w, total_h), interpolation=cv2.INTER_AREA)
            if resized.dtype != np.uint8:
                resized = resized.astype(np.uint8)
            return resized

        accum = 0.0
        carry_ticks = 0
        prev_resized: np.ndarray | None = None

        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = executor.map(_resize_task, rgb_frames)
            
            for resized in tqdm(futures, total=expected_frames, desc="Resizing & Dropping", unit="frame"):
                accum += ticks_float
                needed = max(1, int(accum + 1e-9))
                accum -= needed

                if adaptive and prev_resized is not None:
                    # Compare against the LAST KEPT frame, not the last dropped frame!
                    if _frame_diff(prev_resized, resized) < threshold:
                        carry_ticks += needed
                        continue

                # We are keeping this frame. Update the anchor.
                if adaptive:
                    prev_resized = resized.copy()
                
                frame_ticks = needed + carry_ticks
                carry_ticks = 0

                tick_ranges.append((current_tick, current_tick + frame_ticks - 1))
                kept_frames.append(resized)
                current_tick += frame_ticks
                
        # Flush any remaining ticks if the video ends on a dropped/static frame
        if carry_ticks > 0 and tick_ranges:
            start, end = tick_ranges[-1]
            tick_ranges[-1] = (start, end + carry_ticks)
            current_tick += carry_ticks

        try:
            with open(cache_file, "wb") as f:
                pickle.dump({
                    "frames": kept_frames,
                    "ticks": tick_ranges,
                    "current_tick": current_tick
                }, f)
        except Exception as e:
            sys.stderr.write(f"Failed to write cache: {e}\n")

    # Build the SignalMapping params dict for serialisation
    mapping_params = {
        "width": mapping.width,
        "height": mapping.height,
        "hole_tl": mapping.hole_tl,
        "hole_br": mapping.hole_br,
        "qualities": mapping.qualities,
        "signal_pool": mapping.base_signals,
    }

    return _encode_frames_core(
        kept_frames=kept_frames,
        tick_ranges=tick_ranges,
        output_name=output_name,
        deduplicate=deduplicate,
        mapping_params=mapping_params,
        total_w=total_w,
        total_h=total_h,
        unit_w=unit_w,
        unit_h=unit_h,
        unit_cols=unit_cols,
        unit_rows=unit_rows,
        clock=clock,
        current_tick=current_tick,
    )


# ═══════════════════════════════════════════════════════════════════════
# Chunked time-dimension encoder
# ═══════════════════════════════════════════════════════════════════════

def encode_frames_chunked(
    rgb_frames: Iterator[np.ndarray],
    output_name: str,
    fps: float = 0.0,
    adaptive: bool = False,
    threshold: float = 0.03,
    deduplicate: bool = False,
    mapping: SignalMapping | None = None,
    total_width: int | None = None,
    total_height: int | None = None,
    expected_frames: int | None = None,
    source_id: str = "",
    *,
    time_chunks: int = 1,
    chunk_workers: int | None = None,
    output_chunks_dir: str | None = None,
    deduplicate_cross: bool = False,
) -> dict:
    """Encode frames with time-dimension chunking for parallel generation.

    Splits the video into *time_chunks* time slices, builds each slice's
    combinator grid in parallel (via :class:`~concurrent.futures.ProcessPoolExecutor`),
    caches finished chunks to disk, and merges them into one blueprint.

    Parameters
    ----------
    time_chunks : int
        Number of time slices (default 1 = no chunking, behaves like
        :func:`encode_frames`).
    chunk_workers : int or None
        Max parallel worker processes.  ``None`` uses ``os.cpu_count()``.
    output_chunks_dir : str or None
        If set, write each chunk's individual blueprint to this directory
        (for inspection).
    deduplicate_cross : bool
        After building all chunks, run a cross-chunk deduplication pass
        during merge.  Identical DC outputs from different time chunks are
        merged into a single combinator.  This runs single-threaded and
        **may be slow** for large videos.

    Returns
    -------
    dict
        ``{"full": str, "chunks": list[str]}`` — the merged blueprint
        string and a list of per-chunk blueprint strings.
    """
    # ── Phase 0 & 1 (same as encode_frames) ───────────────────────────
    if fps <= 0:
        fps = 60.0
    fps = max(1.0, min(fps, 60.0))
    ticks_float = 60.0 / fps

    if mapping is None:
        mapping = SignalMapping(
            DISPLAY_WIDTH,
            DISPLAY_HEIGHT,
            HOLE_TOP_LEFT,
            HOLE_BOTTOM_RIGHT,
            QUALITIES,
            SIGNAL_POOL,
        )

    unit_w = DISPLAY_WIDTH
    unit_h = DISPLAY_HEIGHT
    total_w = total_width if total_width is not None else unit_w
    total_h = total_height if total_height is not None else unit_h
    unit_cols = math.ceil(total_w / unit_w)
    unit_rows = math.ceil(total_h / unit_h)
    clock = CLOCK_SIGNAL

    hash_str = f"{source_id}_{output_name}_{total_w}_{total_h}_{fps}_{adaptive}_{threshold}"
    safe_name = hashlib.md5(hash_str.encode('utf-8')).hexdigest()[:8]
    cache_file = Path(f".encode_cache_{safe_name}.pkl")

    kept_frames: list[np.ndarray] = []
    tick_ranges: list[tuple[int, int]] = []
    current_tick = 1

    if cache_file.exists():
        sys.stderr.write(f"Found cache {cache_file}, loading intermediate results...\n")
        try:
            with open(cache_file, "rb") as f:
                cache_data = pickle.load(f)
                kept_frames = cache_data["frames"]
                tick_ranges = cache_data["ticks"]
                current_tick = cache_data.get("current_tick", 1)
        except Exception as e:
            sys.stderr.write(f"Failed to load cache: {e}\n")
            kept_frames, tick_ranges, current_tick = [], [], 1

    if not kept_frames:
        sys.stderr.write("Decoding, resizing, and processing frames...\n")

        def _resize_task(rgb):
            resized = cv2.resize(rgb, (total_w, total_h), interpolation=cv2.INTER_AREA)
            if resized.dtype != np.uint8:
                resized = resized.astype(np.uint8)
            return resized

        accum = 0.0
        carry_ticks = 0
        prev_resized: np.ndarray | None = None

        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = executor.map(_resize_task, rgb_frames)
            for resized in tqdm(futures, total=expected_frames, desc="Resizing & Dropping", unit="frame"):
                accum += ticks_float
                needed = max(1, int(accum + 1e-9))
                accum -= needed

                if adaptive and prev_resized is not None:
                    if _frame_diff(prev_resized, resized) < threshold:
                        carry_ticks += needed
                        continue

                if adaptive:
                    prev_resized = resized.copy()

                frame_ticks = needed + carry_ticks
                carry_ticks = 0
                tick_ranges.append((current_tick, current_tick + frame_ticks - 1))
                kept_frames.append(resized)
                current_tick += frame_ticks

        if carry_ticks > 0 and tick_ranges:
            start, end = tick_ranges[-1]
            tick_ranges[-1] = (start, end + carry_ticks)
            current_tick += carry_ticks

        try:
            with open(cache_file, "wb") as f:
                pickle.dump({
                    "frames": kept_frames,
                    "ticks": tick_ranges,
                    "current_tick": current_tick,
                }, f)
        except Exception as e:
            sys.stderr.write(f"Failed to write cache: {e}\n")

    total_input = len(kept_frames)
    if total_input == 0:
        sys.stderr.write("No frames to encode.\n")
        return {"full": "", "chunks": []}

    # ── Fast path: single chunk (no parallelism) ──────────────────────
    if time_chunks <= 1:
        mapping_params = {
            "width": mapping.width, "height": mapping.height,
            "hole_tl": mapping.hole_tl, "hole_br": mapping.hole_br,
            "qualities": mapping.qualities, "signal_pool": mapping.base_signals,
        }
        bp_str = _encode_frames_core(
            kept_frames=kept_frames, tick_ranges=tick_ranges,
            output_name=output_name, deduplicate=deduplicate,
            mapping_params=mapping_params,
            total_w=total_w, total_h=total_h,
            unit_w=unit_w, unit_h=unit_h,
            unit_cols=unit_cols, unit_rows=unit_rows,
            clock=clock, current_tick=current_tick,
        )
        return {"full": bp_str, "chunks": [bp_str]}

    # ── Split frames into time chunks ─────────────────────────────────
    total_ticks = current_tick - 1
    chunk_size = math.ceil(total_input / time_chunks)
    chunk_frames: list[list[np.ndarray]] = []
    chunk_tick_ranges: list[list[tuple[int, int]]] = []
    for ci in range(time_chunks):
        start = ci * chunk_size
        end = min(start + chunk_size, total_input)
        chunk_frames.append(kept_frames[start:end])
        chunk_tick_ranges.append(tick_ranges[start:end])

    sys.stderr.write(
        f"Splitting {total_input} frames over {total_ticks} ticks "
        f"→ {time_chunks} time chunk(s) (~{chunk_size} frames each).\n"
    )

    # ── Chunk cache setup ─────────────────────────────────────────────
    mapping_params = {
        "width": mapping.width, "height": mapping.height,
        "hole_tl": mapping.hole_tl, "hole_br": mapping.hole_br,
        "qualities": mapping.qualities, "signal_pool": mapping.base_signals,
    }

    cache_dir = _chunk_cache_dir(
        source_id, time_chunks, total_w, total_h,
        fps, adaptive, threshold, deduplicate,
    )
    cache_dir.mkdir(parents=True, exist_ok=True)

    meta = {"time_chunks": time_chunks, "total_ticks": total_ticks}
    with open(_chunk_meta_path(cache_dir), "w") as f:
        json.dump(meta, f)

    # ── Build chunks (parallel, with caching) ─────────────────────────
    chunk_results: dict[int, str] = {}
    pending_indices: list[int] = []

    for ci in range(time_chunks):
        cpath = cache_dir / f"chunk_{ci:04d}.bp.txt"
        if cpath.exists():
            sys.stderr.write(f"Chunk {ci + 1}/{time_chunks}: cached, skipping.\n")
            chunk_results[ci] = cpath.read_text(encoding="utf-8")
        else:
            pending_indices.append(ci)

    if pending_indices:
        workers = chunk_workers or os.cpu_count() or 1
        sys.stderr.write(
            f"Building {len(pending_indices)} chunk(s) "
            f"with {workers} worker(s)…\n"
        )

        # Build payloads — picklable data for each worker
        payloads: list[bytes] = []
        for ci in pending_indices:
            # Determine current_tick for this chunk (for the summary line)
            if chunk_tick_ranges[ci]:
                chunk_cur_tick = chunk_tick_ranges[ci][-1][1] + 1
            else:
                chunk_cur_tick = 1
            data = {
                "chunk_idx": ci,
                "kept_frames": chunk_frames[ci],
                "tick_ranges": chunk_tick_ranges[ci],
                "output_name": output_name,
                "deduplicate": deduplicate,
                "mapping_params": mapping_params,
                "total_w": total_w, "total_h": total_h,
                "unit_w": unit_w, "unit_h": unit_h,
                "unit_cols": unit_cols, "unit_rows": unit_rows,
                "clock": clock,
                "current_tick": chunk_cur_tick,
                "label_suffix": f" [chunk {ci + 1}/{time_chunks}]",
            }
            payloads.append(pickle.dumps(data))

        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_build_chunk_worker, p) for p in payloads]
            for future in tqdm(
                concurrent.futures.as_completed(futures),
                total=len(futures), desc="Building chunks", unit="chunk",
            ):
                ci, bp_str = future.result()
                chunk_results[ci] = bp_str
                # Cache immediately
                cpath = cache_dir / f"chunk_{ci:04d}.bp.txt"
                cpath.write_text(bp_str, encoding="utf-8")
                sys.stderr.write(f"Chunk {ci + 1}/{time_chunks}: cached.\n")

    # ── Assemble ordered chunk list ───────────────────────────────────
    chunk_bps = [chunk_results[ci] for ci in range(time_chunks)]

    # ── Write individual chunk files if requested ─────────────────────
    if output_chunks_dir:
        out_dir = Path(output_chunks_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for ci, bp_str in enumerate(chunk_bps):
            cpath = out_dir / f"chunk_{ci:04d}.bp.txt"
            cpath.write_text(bp_str, encoding="utf-8")
        sys.stderr.write(
            f"Wrote {len(chunk_bps)} individual chunk blueprint(s) "
            f"to {out_dir}/\n"
        )

    # ── Merge chunks ──────────────────────────────────────────────────
    full_bp = _merge_chunk_blueprints(
        chunk_bps, output_name,
        deduplicate_cross=deduplicate_cross,
        total_ticks=total_ticks,
    )

    return {"full": full_bp, "chunks": chunk_bps}


# ---------------------------------------------------------------------------
# Input-specific encoders
# ---------------------------------------------------------------------------

def encode_video(
    video_path: str | Path,
    output_name: str = "Animation Data",
    fps_skip: int = 1,
    fps: float = 0.0,
    adaptive: bool = False,
    threshold: float = 0.03,
    deduplicate: bool = False,
    total_width: int | None = None,
    total_height: int | None = None,
    *,
    round_units: bool = True,
    time_chunks: int = 1,
    chunk_workers: int | None = None,
    output_chunks_dir: str | None = None,
    deduplicate_cross: bool = False,
) -> str:
    """Encode a video file (``.mp4``, ``.avi``, ``.mov``, …)."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {video_path}")

    if fps <= 0:
        detected = cap.get(cv2.CAP_PROP_FPS)
        fps = float(detected) if detected and detected > 0 else 30.0
        fps = max(1.0, min(fps, 60.0))
        sys.stderr.write(f"Detected source FPS: {fps}\n")

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    expected_frames = max(1, total_frames // fps_skip) if total_frames > 0 else None

    # Resolve output dimensions from source aspect ratio + user overrides
    source_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    source_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    resolved_w, resolved_h = resolve_dimensions(
        source_w, source_h,
        user_w=total_width, user_h=total_height,
        round_units=round_units,
    )
    sys.stderr.write(
        f"Source: {source_w}×{source_h} → output: {resolved_w}×{resolved_h}"
    )
    if round_units:
        sys.stderr.write(f"  (rounded to units, {resolved_w // DISPLAY_WIDTH}×{resolved_h // DISPLAY_HEIGHT} units)")
    sys.stderr.write("\n")
    
    # Scale the FPS so skipped frames still preserve identical playback duration 
    effective_fps = fps / float(fps_skip) if fps_skip > 0 else fps

    def _iter() -> Iterator[np.ndarray]:
        while True:
            ret, frame = False, None
            for _ in range(fps_skip):
                ret, f = cap.read()
                if not ret:
                    break
                frame = f
            if frame is not None:
                yield cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            if not ret:
                break

    try:
        source_id = f"{video_path}_{fps_skip}"
        if time_chunks > 1 or deduplicate_cross:
            result = encode_frames_chunked(
                _iter(), output_name, effective_fps, adaptive, threshold, deduplicate,
                total_width=resolved_w, total_height=resolved_h,
                expected_frames=expected_frames, source_id=source_id,
                time_chunks=time_chunks, chunk_workers=chunk_workers,
                output_chunks_dir=output_chunks_dir,
                deduplicate_cross=deduplicate_cross,
            )
            return result["full"]
        return encode_frames(_iter(), output_name, effective_fps, adaptive, threshold, deduplicate,
                              total_width=resolved_w, total_height=resolved_h,
                              expected_frames=expected_frames, source_id=source_id)
    finally:
        cap.release()


def encode_gif(
    gif_path: str | Path,
    output_name: str = "Animation Data",
    fps_skip: int = 1,
    fps: float = 0.0,
    adaptive: bool = False,
    threshold: float = 0.03,
    deduplicate: bool = False,
    total_width: int | None = None,
    total_height: int | None = None,
    *,
    round_units: bool = True,
    time_chunks: int = 1,
    chunk_workers: int | None = None,
    output_chunks_dir: str | None = None,
    deduplicate_cross: bool = False,
) -> str:
    """Encode an animated GIF."""
    from PIL import Image

    gif = Image.open(str(gif_path))

    # Resolve output dimensions from source GIF size
    source_w, source_h = gif.size
    resolved_w, resolved_h = resolve_dimensions(
        source_w, source_h,
        user_w=total_width, user_h=total_height,
        round_units=round_units,
    )
    sys.stderr.write(
        f"Source GIF: {source_w}×{source_h} → output: {resolved_w}×{resolved_h}"
    )
    if round_units:
        sys.stderr.write(f"  (rounded to units, {resolved_w // DISPLAY_WIDTH}×{resolved_h // DISPLAY_HEIGHT} units)")
    sys.stderr.write("\n")

    if fps <= 0:
        duration = gif.info.get("duration", 0)
        if not duration:
            try:
                gif.seek(0)
                duration = gif.info.get("duration", 100)
            except Exception:
                duration = 100
        fps = max(1.0, min(60.0, 1000.0 / duration)) if duration else 10.0
        sys.stderr.write(f"Detected source FPS: {fps:.1f} (from GIF)\n")

    try:
        expected_frames = max(1, getattr(gif, "n_frames", 1) // fps_skip)
    except Exception:
        expected_frames = None

    effective_fps = fps / float(fps_skip) if fps_skip > 0 else fps

    def _iter() -> Iterator[np.ndarray]:
        idx = 0
        while True:
            try:
                gif.seek(idx)
                if idx % fps_skip == 0:
                    frame = gif.convert("RGB")
                    yield np.array(frame)
                idx += 1
            except EOFError:
                return

    source_id = f"{gif_path}_{fps_skip}"
    if time_chunks > 1 or deduplicate_cross:
        result = encode_frames_chunked(
            _iter(), output_name, effective_fps, adaptive, threshold, deduplicate,
            total_width=resolved_w, total_height=resolved_h,
            expected_frames=expected_frames, source_id=source_id,
            time_chunks=time_chunks, chunk_workers=chunk_workers,
            output_chunks_dir=output_chunks_dir,
            deduplicate_cross=deduplicate_cross,
        )
        return result["full"]
    return encode_frames(_iter(), output_name, effective_fps, adaptive, threshold, deduplicate,
                          total_width=resolved_w, total_height=resolved_h,
                          expected_frames=expected_frames, source_id=source_id)


def encode_png_series(
    paths: list[str | Path],
    output_name: str = "Animation Data",
    fps_skip: int = 1,
    fps: float = 0.0,
    adaptive: bool = False,
    threshold: float = 0.03,
    deduplicate: bool = False,
    total_width: int | None = None,
    total_height: int | None = None,
    *,
    round_units: bool = True,
    time_chunks: int = 1,
    chunk_workers: int | None = None,
    output_chunks_dir: str | None = None,
    deduplicate_cross: bool = False,
) -> str:
    """Encode a sequence of image files (PNG, JPEG, …)."""
    if fps <= 0:
        fps = 60.0

    # Read the first image to resolve output dimensions from source aspect ratio
    first_img = cv2.imread(str(paths[0]))
    if first_img is None:
        raise FileNotFoundError(f"Cannot read image: {paths[0]}")
    source_h, source_w = first_img.shape[:2]
    resolved_w, resolved_h = resolve_dimensions(
        source_w, source_h,
        user_w=total_width, user_h=total_height,
        round_units=round_units,
    )
    sys.stderr.write(
        f"Source image: {source_w}×{source_h} → output: {resolved_w}×{resolved_h}"
    )
    if round_units:
        sys.stderr.write(f"  (rounded to units, {resolved_w // DISPLAY_WIDTH}×{resolved_h // DISPLAY_HEIGHT} units)")
    sys.stderr.write("\n")

    expected_frames = math.ceil(len(paths) / fps_skip)
    effective_fps = fps / float(fps_skip) if fps_skip > 0 else fps

    def _iter() -> Iterator[np.ndarray]:
        for i, p in enumerate(paths):
            if i % fps_skip != 0:
                continue
            img = cv2.imread(str(p))
            if img is None:
                raise FileNotFoundError(f"Cannot read image: {p}")
            yield cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    source_id = f"pngs_{len(paths)}_{fps_skip}_{hashlib.md5(str(paths[0]).encode()).hexdigest()[:8]}"
    if time_chunks > 1 or deduplicate_cross:
        result = encode_frames_chunked(
            _iter(), output_name, effective_fps, adaptive, threshold, deduplicate,
            total_width=resolved_w, total_height=resolved_h,
            expected_frames=expected_frames, source_id=source_id,
            time_chunks=time_chunks, chunk_workers=chunk_workers,
            output_chunks_dir=output_chunks_dir,
            deduplicate_cross=deduplicate_cross,
        )
        return result["full"]
    return encode_frames(_iter(), output_name, effective_fps, adaptive, threshold, deduplicate,
                          total_width=resolved_w, total_height=resolved_h,
                          expected_frames=expected_frames, source_id=source_id)


def encode_frame(
    image_path: str | Path,
    output_name: str = "Frame Data",
    fps: float = 0.0,
    adaptive: bool = False,
    threshold: float = 0.03,
    deduplicate: bool = False,
    total_width: int | None = None,
    total_height: int | None = None,
    *,
    round_units: bool = True,
    time_chunks: int = 1,
    chunk_workers: int | None = None,
    output_chunks_dir: str | None = None,
    deduplicate_cross: bool = False,
) -> str:
    """Encode a single still image as a one-frame blueprint."""
    if fps <= 0:
        fps = 60.0
    return encode_png_series(
        [image_path], output_name, fps_skip=1, fps=fps,
        adaptive=adaptive, threshold=threshold, deduplicate=deduplicate,
        total_width=total_width, total_height=total_height,
        round_units=round_units,
        time_chunks=time_chunks, chunk_workers=chunk_workers,
        output_chunks_dir=output_chunks_dir,
        deduplicate_cross=deduplicate_cross,
    )


# ---------------------------------------------------------------------------
# Convenience — auto-detect input type and dispatch
# ---------------------------------------------------------------------------

def encode_auto(
    input_path: str,
    output_name: str = "Animation Data",
    fps_skip: int = 1,
    fps: float = 0.0,
    adaptive: bool = False,
    threshold: float = 0.03,
    deduplicate: bool = False,
    total_width: int | None = None,
    total_height: int | None = None,
    *,
    round_units: bool = True,
    time_chunks: int = 1,
    chunk_workers: int | None = None,
    output_chunks_dir: str | None = None,
    deduplicate_cross: bool = False,
) -> str:
    """Auto-detect input type and call the appropriate encoder."""
    path = Path(input_path)
    video_exts = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
    image_exts = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif"}

    chunk_kwargs = {
        "time_chunks": time_chunks, "chunk_workers": chunk_workers,
        "output_chunks_dir": output_chunks_dir,
        "deduplicate_cross": deduplicate_cross,
    }

    if path.is_dir():
        pngs = sorted(path.glob("*.png"))
        if not pngs:
            raise FileNotFoundError(f"No .png files found in directory: {input_path}")
        sys.stderr.write(f"Found {len(pngs)} PNG(s) in {input_path}\n")
        return encode_png_series(pngs, output_name, fps_skip, fps, adaptive, threshold, deduplicate,
                                  total_width=total_width, total_height=total_height,
                                  round_units=round_units,
                                  **chunk_kwargs)

    if "*" in input_path or "?" in input_path:
        matches = sorted(Path().glob(input_path))
        if not matches:
            raise FileNotFoundError(f"No files match pattern: {input_path}")
        sys.stderr.write(f"Matched {len(matches)} file(s) for pattern: {input_path}\n")
        return encode_png_series(matches, output_name, fps_skip, fps, adaptive, threshold, deduplicate,
                                  total_width=total_width, total_height=total_height,
                                  round_units=round_units,
                                  **chunk_kwargs)

    ext = path.suffix.lower()

    if ext in video_exts:
        return encode_video(input_path, output_name, fps_skip, fps, adaptive, threshold, deduplicate,
                             total_width=total_width, total_height=total_height,
                             round_units=round_units,
                             **chunk_kwargs)

    if ext == ".gif":
        try:
            from PIL import Image
        except ImportError:
            raise ImportError(
                "Pillow is required for GIF encoding. "
                "Install it with: pip install Pillow"
            )
        try:
            gif = Image.open(str(path))
            gif.seek(1)
        except EOFError:
            return encode_frame(input_path, output_name, fps, adaptive, threshold, deduplicate,
                                 total_width=total_width, total_height=total_height,
                                 round_units=round_units,
                                 **chunk_kwargs)
        return encode_gif(input_path, output_name, fps_skip, fps, adaptive, threshold, deduplicate,
                           total_width=total_width, total_height=total_height,
                           round_units=round_units,
                           **chunk_kwargs)

    if ext in image_exts:
        return encode_frame(input_path, output_name, fps, adaptive, threshold, deduplicate,
                             total_width=total_width, total_height=total_height,
                             round_units=round_units,
                             **chunk_kwargs)

    raise ValueError(
        f"Cannot determine input type for: {input_path}. "
        f"Use an explicit subcommand or a recognised file extension."
    )

