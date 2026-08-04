"""Media encoder converts video, GIF, PNG series, and still images into
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
    QUALITIES,
    SIGNAL_POOL,
)

from ..logical_blueprint import assert_wire_topology
from ..cache_paths import cache_dir, cache_file as make_cache_file

try:
    from tqdm import tqdm
except ImportError:
    class tqdm:  # pylint: disable=invalid-name
        """Graceful fallback if tqdm is not installed."""
        def __init__(self, iterable=None, *_args, **_kwargs):  # pylint: disable=keyword-arg-before-vararg
            self.iterable = iterable or []
        def __iter__(self):
            yield from self.iterable
        def update(self, n=1):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass


# ── UTF-8 path helpers for OpenCV on Windows ─────────────────────────────
# cv2.imread() and cv2.VideoCapture() can fail with non-ASCII paths on
# Windows.  Use bytes-based APIs instead.

def _imread_utf8(path: str | os.PathLike) -> np.ndarray | None:
    """Read an image file with cv2, supporting non-ASCII paths on Windows."""
    try:
        with open(path, "rb") as f:
            data = np.frombuffer(f.read(), dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return None


def _videocap_utf8(path: str | os.PathLike):
    """Open a video with cv2.VideoCapture, supporting non-ASCII paths on Windows."""
    return cv2.VideoCapture(os.fsencode(os.fspath(path)))


# ═══════════════════════════════════════════════════════════════════════
# Dimension resolution — auto-calculate omitted dimension from aspect ratio
# ═══════════════════════════════════════════════════════════════════════

def resolve_dimensions(
    source_w: int,
    source_h: int,
    width: int | None = None,
    height: int | None = None,
) -> tuple[int, int]:
    """Compute the final ``(total_w, total_h)`` for frame resizing.

    Always preserves the source aspect ratio unless both *width* and
    *height* are explicitly given (which overrides the ratio).  When
    neither is given, the result is the largest size that fits within
    ``DISPLAY_WIDTH × DISPLAY_HEIGHT`` while keeping the source ratio.
    When only one is given, the other is computed from the ratio.

    Parameters
    ----------
    source_w, source_h : int
        Original media dimensions (pixels).
    width, height : int or None
        User-specified overrides from ``--width`` / ``--height``.

    Returns
    -------
    (total_w, total_h) : tuple[int, int]
    """
    # Both given — trust the user (may change aspect ratio)
    if width is not None and height is not None:
        return width, height

    # No valid source geometry (e.g. audio-only stream) — fall back to defaults.
    if source_w is None or source_h is None or source_w <= 0 or source_h <= 0:
        return width or DISPLAY_WIDTH, height or DISPLAY_HEIGHT

    # Neither given — fit within display bounds, preserving source ratio
    if width is None and height is None:
        w = DISPLAY_WIDTH
        h = max(1, round(w * source_h / source_w))
        if h > DISPLAY_HEIGHT:
            h = DISPLAY_HEIGHT
            w = max(1, round(h * source_w / source_h))
        return w, h

    # One given — compute the other from source aspect ratio
    if width is not None:
        height = max(1, round(width * source_h / source_w))
    elif height is not None:
        width = max(1, round(height * source_w / source_h))

    return width, height


def _frame_diff(a: np.ndarray, b: np.ndarray) -> float:
    """Return 0.0–1.0 normalised mean absolute difference between two RGB frames."""
    return float(np.mean(np.abs(a.astype(np.float32) - b.astype(np.float32))) / 255.0)


def _fix_conditions_in_dict(d: dict) -> None:
    """Fix ``compare_type`` fields in a blueprint dict *in place*.

    Draftsman's ``Condition.compare_type`` defaults to ``"or"``, and
    ``Blueprint.to_dict()`` omits default values.  Factorio may interpret
    a missing ``compare_type`` differently, causing AND/OR confusion.

    This walks every decider combinator in the blueprint dict and ensures
    that within-range pairs carry ``"and"`` while inter-range boundaries
    carry ``"or"``.  Conditions that already carry an explicit
    ``compare_type`` are left untouched — so a builder can express a
    deliberate AND/OR combination and it is not rewritten by this pass.
    """
    for entity in d.get("blueprint", {}).get("entities", []):
        cb = entity.get("control_behavior", {})
        decider = cb.get("decider_conditions", {})
        conds: list[dict] = decider.get("conditions", [])
        if not conds:
            continue

        i = 0
        range_idx = 0
        while i < len(conds):
            cond_a = conds[i]
            comp = cond_a.get("comparator", "")

            if comp == "=":
                # Default: an "= x" condition starts a new range (AND for the
                # first, OR thereafter).  But if the builder explicitly set a
                # compare_type (e.g. the audio player's match0, which needs
                # "signal-M == 0 AND each == 60"), honour it — overriding it
                # to "or" makes match0 fire every tick (the t=0 beep).
                if "compare_type" not in cond_a:
                    cond_a["compare_type"] = "and"
                    if range_idx > 0:
                        cond_a["compare_type"] = "or"
                i += 1
                range_idx += 1
                continue

            # comp is "≥" — first of a pair  [>= start, <= end]
            cond_b = conds[i + 1] if i + 1 < len(conds) else None
            is_pair = (
                cond_b is not None
                and cond_b.get("comparator", "") == "\u2264"
            )
            if is_pair:
                cond_a["compare_type"] = "or" if range_idx > 0 else "and"
                cond_b.setdefault("compare_type", "and")
                i += 2
                range_idx += 1
            else:
                if "compare_type" not in cond_a:
                    cond_a["compare_type"] = "and"
                i += 1


def _to_fixed_string(bp: Blueprint) -> str:
    """Like ``bp.to_string()``, but with corrected ``compare_type`` fields."""
    from draftsman.utils import JSON_to_string
    d = bp.to_dict()
    _fix_conditions_in_dict(d)
    return JSON_to_string(d)


def _fix_blueprint_conditions(bp: Blueprint) -> Blueprint:
    """Convenience wrapper that returns a *new* Blueprint with fixed conditions.
    Prefer :func:`_to_fixed_string` for serialization to avoid re-serializing
    from objects (which would lose the fix).
    """
    d = bp.to_dict()
    _fix_conditions_in_dict(d)
    return Blueprint.from_dict(d)


# ══════════════════════════════════════════════════════════════════════
# Core blueprint builder (extracted for reuse by chunked encoder)
# ══════════════════════════════════════════════════════════════════════


def _layout_and_prewire_memory_bank(
    lb: "LogicalBlueprint",
    dc_ids: list[str],
    *,
    connectors: bool = False,
    connector_label: str | None = None,
    fragment_index: int | None = None,
) -> None:
    """Assign compact square positions and deterministic internal prewiring.

    Memory banks are regular: all DC inputs share one red network and all DC
    outputs share another. We assign a square-ish layout and attach prewired
    snake connections on both buses so the bank can be treated as one compact
    block during composition.

    When *connectors* is True (split-output mode), the bank also gets:
      * constant-combinator connectors on the LEFT and RIGHT, wired into the
        red *output* (data) bus and carrying *connector_label* at value 0 —
        a visual hint that adds nothing to the bus, used for in-game wiring
        to the matching display chunk;
      * a non-wired series-label CC carrying *fragment_index* (which time
        fragment this piece is).
    """
    from ..logical_blueprint import Endpoint, LogicalEntity

    n = len(dc_ids)
    if n == 0:
        return

    cols = max(1, math.ceil(math.sqrt(n)))

    # Decider combinators are 1x2 (north-facing) in this project.
    for idx, dc_id in enumerate(dc_ids):
        row = idx // cols
        col = idx % cols
        ent = lb.entities.get(dc_id)
        if ent is None:
            continue
        ent.position = (col, row * 2)

    if n <= 1:
        return

    rows = math.ceil(n / cols)
    snake_ids: list[str] = []
    for row in range(rows):
        row_start = row * cols
        row_end = min(row_start + cols, n)
        row_ids = dc_ids[row_start:row_end]
        if row % 2 == 1:
            row_ids = list(reversed(row_ids))
        snake_ids.extend(row_ids)

    def _pairs_for_port(port: str):
        return [
            (Endpoint(snake_ids[i], port), Endpoint(snake_ids[i + 1], port))
            for i in range(len(snake_ids) - 1)
        ]

    input_anchor = Endpoint(dc_ids[0], "input")
    output_anchor = Endpoint(dc_ids[0], "output")
    for net in lb.networks:
        if net.color != "red":
            continue
        if input_anchor in net.endpoints:
            net.prewired_pairs = _pairs_for_port("input")
        elif output_anchor in net.endpoints:
            output_pairs = _pairs_for_port("output")
            if connectors and connector_label:
                left_cc = f"{dc_ids[0]}_ccL"
                right_cc = f"{dc_ids[0]}_ccR"
                lb.add_entity(LogicalEntity(
                    left_cc, "constant-combinator",
                    properties={"signals": [{"name": connector_label, "value": 0}]},
                    position=(-1, 0),
                ))
                lb.add_entity(LogicalEntity(
                    right_cc, "constant-combinator",
                    properties={"signals": [{"name": connector_label, "value": 0}]},
                    position=(cols, 0),
                ))
                if fragment_index is not None:
                    lb.add_entity(LogicalEntity(
                        f"{dc_ids[0]}_label", "constant-combinator",
                        properties={"signals": [{"name": "signal-info", "value": fragment_index}]},
                        position=(cols, 1),
                    ))
                # Join the data bus (network membership) + include in prewired.
                lb.connect("red", Endpoint(left_cc, "input"), output_anchor)
                lb.connect("red", Endpoint(right_cc, "input"), output_anchor)
                output_pairs.append((Endpoint(left_cc, "input"), output_anchor))
                output_pairs.append((Endpoint(right_cc, "input"), output_anchor))
            net.prewired_pairs = output_pairs

def _encode_frames_core(
    kept_frames: list[np.ndarray],
    tick_ranges: list[tuple[int, int]],
    output_name: str,
    deduplicate: bool,
    mapping_params: dict,
    clock: str,
    current_tick: int,
    label_suffix: str = "",
    *,
    connectors: bool = False,
    fragment_index: int | None = None,
) -> "LogicalBlueprint":
    """Build a LogicalBlueprint from pre-processed frame data.

    Takes already-resized and adaptive-dropped frames plus tick ranges,
    and produces a LogicalBlueprint with DC entities and star-wired
    red networks (inputs joined, outputs joined).  No spatial layout —
    that is the composer's job.

    *mapping_params* is a dict with keys ``width``, ``height``, ``qualities``,
    ``signal_pool`` — reconstructable as ``SignalMapping(**mapping_params)``.

    Returns a :class:`~factorio_display.logical_blueprint.LogicalBlueprint`
    with ``input_ports={"clock": ...}`` and ``output_ports={"data": ...}``.
    """
    from ..logical_blueprint import Endpoint, LogicalBlueprint, LogicalEntity

    mapping = SignalMapping(**mapping_params)

    total_input = len(kept_frames)
    if total_input == 0:
        sys.stderr.write("No frames to encode.\n")
        return LogicalBlueprint(label=f"Video Memory: {output_name}{label_suffix}")

    # Normalise tick_ranges: accept either flat list[tuple] or
    # list[list[tuple]] (from cross-chunk dedup where one frame
    # may have multiple merged tick ranges).
    ranges_per_frame: list[list[tuple[int, int]]]
    if tick_ranges and isinstance(tick_ranges[0], list):
        ranges_per_frame = tick_ranges  # type: ignore[assignment]
    else:
        ranges_per_frame = [[r] for r in tick_ranges]  # type: ignore[arg-type]

    frame_entries = [(f, ranges) for f, ranges in zip(kept_frames, ranges_per_frame)]

    if deduplicate:
        seen: dict[int, tuple[np.ndarray, list[tuple[int, int]]]] = {}
        order: list[int] = []
        for resized, ranges in frame_entries:
            h = hash(resized.tobytes())
            if h not in seen:
                seen[h] = (resized, [])
                order.append(h)
            seen[h][1].extend(ranges)
        unique_frames = [seen[h] for h in order]
    else:
        unique_frames = [(resized, list(ranges)) for resized, ranges in frame_entries]

    lb = LogicalBlueprint(label=f"Video Memory: {output_name}{label_suffix}")

    # ── Build DC entities — no spatial layout (that's the composer's job).
    # All DCs join a shared red network: inputs star-joined, outputs star-joined.
    dc_ids: list[str] = []
    for gate_num, (resized, ranges) in enumerate(unique_frames, start=1):
        dc_id = f"gate_{gate_num}"

        conditions: list[dict] = []
        for start, end in ranges:
            if start == end:
                conditions.append({
                    "first": clock,
                    "op": "=",
                    "constant": start,
                    "compare_type": "and",
                })
            else:
                conditions.append({
                    "first": clock,
                    "op": ">=",
                    "constant": start,
                    "compare_type": "and",
                })
                conditions.append({
                    "first": clock,
                    "op": "<=",
                    "constant": end,
                    "compare_type": "and",
                })

        # Non-zero pixel fast path: only iterate pixels that have colour.
        outputs: list[dict] = []
        nonzero_mask = np.any(resized != 0, axis=2)
        ys, xs = np.nonzero(nonzero_mask)
        for y, x in zip(ys, xs):
            sig = mapping.get_signal(int(x), int(y))
            if sig is None:
                continue
            r, g, b = resized[y, x]
            color_int = (int(r) << 16) | (int(g) << 8) | int(b)
            sig_str = sig["name"]
            if sig.get("quality") and sig["quality"] != "normal":
                sig_str = f"{sig['name']}@{sig['quality']}"
            outputs.append({
                "signal": sig_str,
                "copy_count": False,
                "constant": color_int,
            })

        dc = LogicalEntity(
            dc_id,
            "decider-combinator",
            properties={
                "conditions": conditions,
                "outputs": outputs,
            },
        )
        lb.add_entity(dc)
        dc_ids.append(dc_id)

    # ── Join all inputs together (red), all outputs together (red) ──
    # This expresses logical network membership — no spatial knowledge.
    if len(dc_ids) >= 2:
        first_id = dc_ids[0]
        for dc_id in dc_ids[1:]:
            lb.connect("red", Endpoint(first_id, "input"), Endpoint(dc_id, "input"))
            lb.connect("red", Endpoint(first_id, "output"), Endpoint(dc_id, "output"))

    # ── Declare ports ───────────────────────────────────────────────
    connector_label = None
    if connectors:
        _sig0 = mapping.get_signal(0, 0)
        if _sig0:
            connector_label = _sig0["name"]
            if _sig0.get("quality") and _sig0["quality"] != "normal":
                connector_label = f"{_sig0['name']}@{_sig0['quality']}"
    _layout_and_prewire_memory_bank(
        lb, dc_ids,
        connectors=connectors,
        connector_label=connector_label,
        fragment_index=fragment_index,
    )

    if dc_ids:
        first_input_ep = Endpoint(dc_ids[0], "input")
        for net in lb.networks:
            if net.color == "red" and first_input_ep in net.endpoints:
                lb.set_input_port("clock", net.network_id)
                break
        last_output_ep = Endpoint(dc_ids[-1], "output")
        for net in lb.networks:
            if net.color == "red" and last_output_ep in net.endpoints:
                lb.set_output_port("data", net.network_id)
                break

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
    return lb


def _encode_frames_logical(
    kept_frames: list[np.ndarray],
    tick_ranges: list[tuple[int, int]],
    output_name: str,
    deduplicate: bool,
    mapping_params: dict,
    clock: str,
    current_tick: int,
    label_suffix: str = "",
) -> "LogicalBlueprint":
    """Build a LogicalBlueprint from pre-processed frame data.

    Same as :func:`_encode_frames_core` but returns a
    :class:`~factorio_display.logical_blueprint.LogicalBlueprint`
    with ``input_ports={"clock": ...}`` and ``output_ports={"data": ...}``.
    """
    from ..logical_blueprint import Endpoint, LogicalBlueprint, LogicalEntity

    mapping = SignalMapping(**mapping_params)

    total_input = len(kept_frames)
    if total_input == 0:
        sys.stderr.write("No frames to encode.\n")
        return LogicalBlueprint(label=f"Video Memory: {output_name}{label_suffix}")

    # Normalise tick_ranges (same as _encode_frames_core)
    ranges_per_frame: list[list[tuple[int, int]]]
    if tick_ranges and isinstance(tick_ranges[0], list):
        ranges_per_frame = tick_ranges  # type: ignore[assignment]
    else:
        ranges_per_frame = [[r] for r in tick_ranges]  # type: ignore[arg-type]

    frame_entries = [(f, ranges) for f, ranges in zip(kept_frames, ranges_per_frame)]

    if deduplicate:
        seen: dict[int, tuple[np.ndarray, list[tuple[int, int]]]] = {}
        order: list[int] = []
        for resized, ranges in frame_entries:
            h = hash(resized.tobytes())
            if h not in seen:
                seen[h] = (resized, [])
                order.append(h)
            seen[h][1].extend(ranges)
        unique_frames = [seen[h] for h in order]
    else:
        unique_frames = [(resized, list(ranges)) for resized, ranges in frame_entries]

    lb = LogicalBlueprint(label=f"Video Memory: {output_name}{label_suffix}")

    dc_ids: list[str] = []
    for gate_num, (resized, ranges) in enumerate(unique_frames, start=1):
        dc_id = f"gate_{gate_num}"

        conditions: list[dict] = []
        for start, end in ranges:
            if start == end:
                conditions.append({
                    "first": clock,
                    "op": "=",
                    "constant": start,
                    "compare_type": "and",
                })
            else:
                conditions.append({
                    "first": clock,
                    "op": ">=",
                    "constant": start,
                    "compare_type": "and",
                })
                conditions.append({
                    "first": clock,
                    "op": "<=",
                    "constant": end,
                    "compare_type": "and",
                })

        # Non-zero pixel fast path: only iterate pixels that have colour.
        outputs: list[dict] = []
        nonzero_mask = np.any(resized != 0, axis=2)
        ys, xs = np.nonzero(nonzero_mask)
        for y, x in zip(ys, xs):
            sig = mapping.get_signal(int(x), int(y))
            if sig is None:
                continue
            r, g, b = resized[y, x]
            color_int = (int(r) << 16) | (int(g) << 8) | int(b)
            sig_str = sig["name"]
            if sig.get("quality") and sig["quality"] != "normal":
                sig_str = f"{sig['name']}@{sig['quality']}"
            outputs.append({
                "signal": sig_str,
                "copy_count": False,
                "constant": color_int,
            })

        dc = LogicalEntity(
            dc_id,
            "decider-combinator",
            properties={
                "conditions": conditions,
                "outputs": outputs,
            },
        )
        lb.add_entity(dc)
        dc_ids.append(dc_id)

    # Wire all inputs together (red) and all outputs together (red)
    if len(dc_ids) >= 2:
        first_id = dc_ids[0]
        for dc_id in dc_ids[1:]:
            lb.connect("red", Endpoint(first_id, "input"), Endpoint(dc_id, "input"))
            lb.connect("red", Endpoint(first_id, "output"), Endpoint(dc_id, "output"))

    # Declare ports
    _layout_and_prewire_memory_bank(lb, dc_ids)

    if dc_ids:
        first_input_ep = Endpoint(dc_ids[0], "input")
        for net in lb.networks:
            if net.color == "red" and first_input_ep in net.endpoints:
                lb.set_input_port("clock", net.network_id)
                break
        last_output_ep = Endpoint(dc_ids[-1], "output")
        for net in lb.networks:
            if net.color == "red" and last_output_ep in net.endpoints:
                lb.set_output_port("data", net.network_id)
                break

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
    return lb


# ══════════════════════════════════════════════════════════════════════�?
# Chunk-cache helpers
# ══════════════════════════════════════════════════════════════════════�?

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
    return cache_dir("video_time_chunks", f"encode_chunks_{safe}")


def _chunk_meta_path(cache_dir: Path) -> Path:
    return cache_dir / "meta.json"


def _write_toml_cache_async(lb: "LogicalBlueprint", cache_path: Path) -> None:
    """Write a LogicalBlueprint as TOML to *cache_path* in a
    background thread (non-blocking).  Errors are silently ignored."""
    import threading
    from ..logical_blueprint import to_toml

    def _write() -> None:
        try:
            cache_path.write_text(to_toml(lb), encoding="utf-8")
        except Exception:
            pass  # cache write failures are non-fatal

    t = threading.Thread(target=_write, daemon=True)
    t.start()


# ══════════════════════════════════════════════════════════════════════�?
# ProcessPoolExecutor worker (top-level so it can be pickled)
# ══════════════════════════════════════════════════════════════════════�?

def _build_chunk_worker(payload: bytes) -> tuple[int, str]:
    """Build a single time-chunk LogicalBlueprint in a worker process.

    Returns ``(chunk_idx, toml_string)``.  LogicalBlueprint is serialized
    as TOML because it cannot be pickled across process boundaries.
    """
    from ..logical_blueprint import to_toml

    data = pickle.loads(payload)
    chunk_idx = data["chunk_idx"]
    lb = _encode_frames_core(
        kept_frames=data["kept_frames"],
        tick_ranges=data["tick_ranges"],
        output_name=data["output_name"],
        deduplicate=data["deduplicate"],
        mapping_params=data["mapping_params"],
        clock=data["clock"],
        current_tick=data["current_tick"],
        label_suffix=data.get("label_suffix", ""),
    )
    return chunk_idx, to_toml(lb)


def _build_vertical_chunk_worker(payload: bytes) -> tuple[int, bytes]:
    """Build a single vertical-chunk LogicalBlueprint in a worker process.

    Returns ``(chunk_idx, pickled LogicalBlueprint)``.  LogicalBlueprint
    pickles cleanly (verified) and is faster to transfer than TOML for the
    large per-frame DC banks these chunks produce.
    """
    data = pickle.loads(payload)
    lb = _encode_frames_core(
        kept_frames=data["chunk_frames"],
        tick_ranges=data["tick_ranges"],
        output_name=data["output_name"],
        deduplicate=data["deduplicate"],
        mapping_params=data["mapping_params"],
        clock=data["clock"],
        current_tick=data["current_tick"],
        label_suffix=data.get("label_suffix", ""),
    )
    return data["chunk_idx"], pickle.dumps(lb)


# ══════════════════════════════════════════════════════════════════════�?
# Merge helpers
# ══════════════════════════════════════════════════════════════════════�?

def _merge_chunk_blueprints(
    chunk_lbs: list["LogicalBlueprint"],
    output_name: str,
) -> "LogicalBlueprint":
    """Merge multiple time-chunk LogicalBlueprints into one.

    Each chunk's entities and networks are prefixed to avoid collisions,
    then their ``data`` ports are connected in time order.
    """
    from ..logical_blueprint import LogicalBlueprint

    if not chunk_lbs:
        return LogicalBlueprint(label=f"Video Memory: {output_name}")

    if len(chunk_lbs) == 1:
        return chunk_lbs[0]

    merged = LogicalBlueprint(label=f"Video Memory: {output_name}")

    x_cursor = 0
    clock_net_ids: list[str] = []

    # Merge chunks with prefixes. Chunk clocks and chunk data outputs are
    # both unified onto shared buses so composition only needs one logical
    # bridge per bus.
    data_net_ids: list[str] = []
    for ci, chunk_lb in enumerate(chunk_lbs):
        prefix = f"tc{ci}_"
        if not chunk_lb.entities:
            continue

        xs = [e.position[0] for e in chunk_lb.entities.values() if e.position is not None]
        chunk_min_x = min(xs) if xs else 0
        chunk_max_x = max(xs) if xs else 0
        # 2-tile spacing between time-chunk blocks keeps them visually separate.
        offset_x = x_cursor - chunk_min_x

        merged.merge(
            chunk_lb,
            entity_prefix=prefix,
            network_prefix=prefix,
            position_offset=(offset_x, 0),
        )

        x_cursor += (chunk_max_x - chunk_min_x + 1) + 2
        # Track each chunk's prefixed data network id; they are unified
        # into one shared data bus below.
        old_data = chunk_lb.output_ports.get("data")
        if old_data is not None:
            data_net_ids.append(prefix + old_data)

        old_clock = chunk_lb.input_ports.get("clock")
        if old_clock is not None:
            clock_net_ids.append(prefix + old_clock)

    if len(clock_net_ids) >= 2:
        shared_clock_net_id = clock_net_ids[0]
        shared_clock_net = next(
            (n for n in merged.networks if n.network_id == shared_clock_net_id),
            None,
        )
        if shared_clock_net is not None and shared_clock_net.endpoints:
            shared_clock_ep = sorted(
                shared_clock_net.endpoints,
                key=lambda ep: (ep.entity_id, ep.port),
            )[0]
            for net_id in clock_net_ids[1:]:
                other_net = next((n for n in merged.networks if n.network_id == net_id), None)
                if other_net is None or not other_net.endpoints:
                    continue
                other_ep = sorted(
                    other_net.endpoints,
                    key=lambda ep: (ep.entity_id, ep.port),
                )[0]
                merged.connect(shared_clock_net.color, shared_clock_ep, other_ep)
            merged.input_ports["clock"] = shared_clock_net_id
    elif clock_net_ids:
        merged.input_ports["clock"] = clock_net_ids[0]

    if len(data_net_ids) >= 2:
        shared_data_net_id = data_net_ids[0]
        shared_data_net = next(
            (n for n in merged.networks if n.network_id == shared_data_net_id),
            None,
        )
        if shared_data_net is not None and shared_data_net.endpoints:
            shared_data_ep = sorted(
                shared_data_net.endpoints,
                key=lambda ep: (ep.entity_id, ep.port),
            )[0]
            for net_id in data_net_ids[1:]:
                other_net = next((n for n in merged.networks if n.network_id == net_id), None)
                if other_net is None or not other_net.endpoints:
                    continue
                other_ep = sorted(
                    other_net.endpoints,
                    key=lambda ep: (ep.entity_id, ep.port),
                )[0]
                merged.connect(shared_data_net.color, shared_data_ep, other_ep)
            merged.output_ports["data"] = shared_data_net_id
    elif data_net_ids:
        merged.output_ports["data"] = data_net_ids[0]

    sys.stderr.write(
        f"Merged {len(chunk_lbs)} time chunks "
        f"-> {len(merged.entities)} entities.\n"
    )
    return merged


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
    *,
    use_cache: bool = False,
) -> Blueprint:
    """Encode an iterable of RGB ``(H, W, 3)`` uint8 frames into a Blueprint.

    If the display has more pixels than the signal pool can address, the
    display is split into disconnected vertical chunks, each with its own
    signal mapping and combinator bank.

    Parameters
    ----------
    use_cache : bool
        If True, cache the core output as LogicalBlueprint TOML on disk
        (async, non-blocking).  Default is False.
    """
    if fps <= 0:
        fps = 60.0
    fps = max(1.0, min(fps, 60.0))
    ticks_float = 60.0 / fps

    # ── Determine display size ────────────────────────────────────────
    total_w = total_width if total_width is not None else DISPLAY_WIDTH
    total_h = total_height if total_height is not None else DISPLAY_HEIGHT

    # ── Power supply warning for large displays ───────────────────────
    if total_w > 28 and total_h > 28:
        sys.stderr.write(
            f"Warning: Display is {total_w}×{total_h} — large lamp grids "
            f"may need multiple substations. Plan your power layout in-game.\n"
        )

    # ── Determine available unique signals ────────────────────────────
    qualities = QUALITIES
    if mapping is not None:
        signal_pool = mapping.base_signals
        qualities = mapping.qualities
    else:
        signal_pool = SIGNAL_POOL

    available = len(signal_pool) * len(qualities)
    total_pixels = total_w * total_h

    # ── Vertical chunk splitting (when pool can't cover all pixels) ───
    from ..integer2signal.mapping import compute_chunking
    chunk_height, num_chunks = compute_chunking(total_w, total_h, signal_pool, qualities)

    clock = CLOCK_SIGNAL

    # ==================================================================
    # Phase 0 & 1: Parallel Resizing, Adaptive Dropping, and Caching
    # ==================================================================

    hash_str = f"{source_id}_{output_name}_{total_w}_{total_h}_{fps}_{adaptive}_{threshold}"
    safe_name = hashlib.md5(hash_str.encode('utf-8')).hexdigest()[:8]
    frame_cache_file = make_cache_file("video_frames", f"encode_cache_{safe_name}", ".pkl")
    loaded_from_cache = False

    kept_frames: list[np.ndarray] = []
    tick_ranges: list[tuple[int, int]] = []
    current_tick = 0

    if use_cache and frame_cache_file.exists():
        sys.stderr.write(f"Found cache {frame_cache_file}, loading intermediate results...\n")
        try:
            with open(frame_cache_file, "rb") as f:
                cache_data = pickle.load(f)
                kept_frames = cache_data["frames"]
                tick_ranges = cache_data["ticks"]
                current_tick = cache_data.get("current_tick", 0)
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

        if use_cache:
            try:
                with open(frame_cache_file, "wb") as f:
                    pickle.dump({
                        "frames": kept_frames,
                        "ticks": tick_ranges,
                        "current_tick": current_tick,
                    }, f)
            except Exception as e:
                sys.stderr.write(f"Failed to write cache: {e}\n")

    if not kept_frames:
        sys.stderr.write("No frames to encode.\n")
        bp = Blueprint()
        bp.label = f"Video Memory: {output_name}"
        bp.icons = ["signal-0"]
        return bp

    # ==================================================================
    # Phase 2: Build blueprint — one chunk per vertical slice
    # ==================================================================

    if num_chunks == 1:
        if mapping is None:
            mapping = SignalMapping(total_w, total_h, qualities, signal_pool)
        mapping_params = {
            "width": mapping.width,
            "height": mapping.height,
            "qualities": mapping.qualities,
            "signal_pool": mapping.base_signals,
        }

        # ── Optional TOML-format cache ────────────────────────────
        from ..logical_blueprint import from_toml, to_draftsman

        if use_cache:
            _frame_hashes = hashlib.sha256(
                b"".join(f.tobytes() for f in kept_frames)
            ).hexdigest()
            _core_cache_key = (
                f"{source_id}_{_frame_hashes[:12]}_"
                f"{json.dumps(mapping_params, sort_keys=True)}_"
                f"dedup{deduplicate}_clk{clock}_tick{current_tick}"
            )
            _core_cache_hash = hashlib.md5(_core_cache_key.encode()).hexdigest()[:12]
            _toml_cache_file = make_cache_file("video_core", f"encode_core_{_core_cache_hash}", ".toml")

            if _toml_cache_file.exists():
                sys.stderr.write(
                    f"Found TOML cache {_toml_cache_file}, "
                    f"skipping combinator build.\n"
                )
                lb = from_toml(_toml_cache_file.read_text(encoding="utf-8"))
                return to_draftsman(lb)

        lb = _encode_frames_core(
            kept_frames=kept_frames,
            tick_ranges=tick_ranges,
            output_name=output_name,
            deduplicate=deduplicate,
            mapping_params=mapping_params,
            clock=clock,
            current_tick=current_tick,
        )

        if use_cache:
            _write_toml_cache_async(lb, _toml_cache_file)

        result = to_draftsman(lb)
        return result

    # ── Multi-chunk path ──────────────────────────────────────────────
    sys.stderr.write(
        f"Display {total_w}×{total_h} ({total_pixels} px) exceeds pool "
        f"({available} signals). Splitting into {num_chunks} vertical "
        f"chunks of {total_w}×{chunk_height}.\n"
    )

    # ── Cache directory for vertical chunk blueprints ─────────────────
    vchunk_hash = hashlib.md5(
        f"{source_id}_{total_w}x{total_h}_{chunk_height}_{num_chunks}_{fps}".encode()
    ).hexdigest()[:12]
    vcache_dir = cache_dir("video_vertical_chunks", f"encode_vchunks_{vchunk_hash}")
    vcache_dir.mkdir(parents=True, exist_ok=True)

    # ── Pre-slice frames + build mapping params for each chunk ────────
    chunk_meta: list[dict] = []
    for ci in range(num_chunks):
        y0 = ci * chunk_height
        y1 = min(y0 + chunk_height, total_h)
        ch_h = y1 - y0
        cpath = vcache_dir / f"vchunk_{ci:04d}.toml" if use_cache else None

        ch_mapping = SignalMapping(total_w, ch_h, qualities, signal_pool)
        mp = {
            "width": ch_mapping.width,
            "height": ch_mapping.height,
            "qualities": ch_mapping.qualities,
            "signal_pool": ch_mapping.base_signals,
        }
        chunk_meta.append({
            "ci": ci, "y0": y0, "y1": y1, "ch_h": ch_h,
            "cpath": cpath, "mapping_params": mp,
        })

    # ── Load cached / build uncached chunks in parallel ───────────────
    from ..logical_blueprint import LogicalBlueprint, from_toml, to_draftsman

    chunk_results: dict[int, LogicalBlueprint] = {}
    pending: list[dict] = []

    for cm in chunk_meta:
        if use_cache and cm["cpath"] is not None and cm["cpath"].exists():
            chunk_results[cm["ci"]] = from_toml(cm["cpath"].read_text(encoding="utf-8"))
        else:
            pending.append(cm)

    if pending:
        workers = min(len(pending), (os.cpu_count() or 4))
        sys.stderr.write(
            f"Building {len(pending)}/{num_chunks} vertical chunk(s) "
            f"with {workers} worker(s)…\n"
        )

        # Slice frames per chunk up front and ship each worker its own
        # pickled payload.  _encode_frames_core is CPU-bound Python, so
        # processes (not threads) give real parallelism on multi-core.
        payloads: list[bytes] = []
        for cm in pending:
            y0, y1 = cm["y0"], cm["y1"]
            chunk_frames = [f[y0:y1, :, :] for f in kept_frames]
            payloads.append(pickle.dumps({
                "chunk_idx": cm["ci"],
                "chunk_frames": chunk_frames,
                "tick_ranges": tick_ranges,
                "output_name": f"{output_name} vc{cm['ci']}",
                "deduplicate": deduplicate,
                "mapping_params": cm["mapping_params"],
                "clock": clock,
                "current_tick": current_tick,
                "label_suffix": f" [vchunk {cm['ci'] + 1}/{num_chunks}]",
            }))

        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_build_vertical_chunk_worker, p) for p in payloads]
            for future in tqdm(
                concurrent.futures.as_completed(futures),
                total=len(futures), desc="Building vertical chunks", unit="chunk",
            ):
                ci, lb_bytes = future.result()
                lb = pickle.loads(lb_bytes)
                chunk_results[ci] = lb
                # Async TOML cache
                if use_cache:
                    cm = next(c for c in chunk_meta if c["ci"] == ci)
                    if cm["cpath"] is not None:
                        _write_toml_cache_async(lb, cm["cpath"])
    else:
        sys.stderr.write(f"All {num_chunks} vertical chunk(s) cached, skipping build.\n")

    # ── Merge chunk LogicalBlueprints (no spatial knowledge) ──────────
    merged = LogicalBlueprint(
        label=f"Video Memory: {output_name} ({total_w}×{total_h}, {num_chunks} chunks)"
    )

    y_cursor = 0

    for cm in chunk_meta:
        ci = cm["ci"]
        chunk_lb = chunk_results.get(ci)
        if chunk_lb is None or not chunk_lb.entities:
            continue
        prefix = f"vc{ci}_"

        ys = [e.position[1] for e in chunk_lb.entities.values() if e.position is not None]
        chunk_min_y = min(ys) if ys else 0
        chunk_max_y = max(ys) if ys else 0
        offset_y = y_cursor - chunk_min_y

        merged.merge(
            chunk_lb,
            entity_prefix=prefix,
            network_prefix=prefix,
            position_offset=(0, offset_y),
        )

        y_cursor += (chunk_max_y - chunk_min_y + 1) + 2
        # Re-declare data port with prefixed name
        old_data = chunk_lb.output_ports.get("data")
        if old_data is not None:
            merged.output_ports[f"{prefix}data"] = prefix + old_data

    total_ticks = current_tick - 1
    sys.stderr.write(
        f"\nEncoded {len(kept_frames)} frames over {total_ticks} ticks "
        f"across {num_chunks} vertical chunk(s).\n"
    )
    result = to_draftsman(merged)
    return result


# ══════════════════════════════════════════════════════════════════════
# Chunked time-dimension encoder
# ══════════════════════════════════════════════════════════════════════

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
    use_cache: bool = False,
) -> dict:
    """Encode frames with time-dimension chunking for parallel generation.

    Splits the video into *time_chunks* time slices, builds each slice's
    combinator grid in parallel (via :class:`~concurrent.futures.ProcessPoolExecutor`),
    and merges them into one blueprint.

    Parameters
    ----------
    time_chunks : int
        Number of time slices (default 1 = no chunking, behaves like
        :func:`encode_frames`).
    chunk_workers : int or None
        Max parallel worker processes.  ``None`` uses ``os.cpu_count()``.
    output_chunks_dir : str or None
        If set, write each chunk's individual blueprint to this directory
        (for inspection).  Writes as blueprint strings.
    deduplicate_cross : bool
        After building all chunks, run a cross-chunk deduplication pass
        during merge.
    use_cache : bool
        If True, cache individual chunk results as LogicalBlueprint TOML
        on disk (async).  Default is False.

    Returns
    -------
    dict
        ``{"full": Blueprint, "chunks": list[Blueprint]}``
    """
    # ── Phase 0 & 1 (same as encode_frames) ───────────────────────────
    if fps <= 0:
        fps = 60.0
    fps = max(1.0, min(fps, 60.0))
    ticks_float = 60.0 / fps

    total_w = total_width if total_width is not None else DISPLAY_WIDTH
    total_h = total_height if total_height is not None else DISPLAY_HEIGHT

    if total_w > 28 and total_h > 28:
        sys.stderr.write(
            f"Warning: Display is {total_w}×{total_h} — large lamp grids "
            f"may need multiple substations. Plan your power layout in-game.\n"
        )

    clock = CLOCK_SIGNAL

    hash_str = f"{source_id}_{output_name}_{total_w}_{total_h}_{fps}_{adaptive}_{threshold}"
    safe_name = hashlib.md5(hash_str.encode('utf-8')).hexdigest()[:8]
    frame_cache_file = make_cache_file("video_frames", f"encode_cache_{safe_name}", ".pkl")

    kept_frames: list[np.ndarray] = []
    tick_ranges: list[tuple[int, int]] = []
    current_tick = 0

    if use_cache and frame_cache_file.exists():
        sys.stderr.write(f"Found cache {frame_cache_file}, loading intermediate results...\n")
        try:
            with open(frame_cache_file, "rb") as f:
                cache_data = pickle.load(f)
                kept_frames = cache_data["frames"]
                tick_ranges = cache_data["ticks"]
                current_tick = cache_data.get("current_tick", 0)
        except Exception as e:
            sys.stderr.write(f"Failed to load cache: {e}\n")
            kept_frames, tick_ranges, current_tick = [], [], 0

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

        if use_cache:
            try:
                with open(frame_cache_file, "wb") as f:
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
        bp = Blueprint()
        bp.label = f"Video Memory: {output_name}"
        bp.icons = ["signal-0"]
        return {"full": bp, "chunks": []}

    # ── Determine signal pool for mapping ─────────────────────────────
    if mapping is None:
        mapping = SignalMapping(total_w, total_h, QUALITIES, SIGNAL_POOL)
    mapping_params = {
        "width": mapping.width,
        "height": mapping.height,
        "qualities": mapping.qualities,
        "signal_pool": mapping.base_signals,
    }

    # ── Fast path: single time chunk (no parallelism) ─────────────────
    if time_chunks <= 1:
        from ..logical_blueprint import to_draftsman
        lb = _encode_frames_core(
            kept_frames=kept_frames, tick_ranges=tick_ranges,
            output_name=output_name, deduplicate=deduplicate,
            mapping_params=mapping_params,
            clock=clock, current_tick=current_tick,
        )
        result = to_draftsman(lb)
        return {"full": result, "chunks": [result]}

    # ── Cross-chunk dedup at frame level (before splitting) ───────────
    # Deduplicate identical frames across the entire video, merging their
    # tick ranges.  This happens at the raw frame level — no DCs involved —
    # so spatial layout is left entirely to the composer.
    #
    # After dedup, each unique frame stays paired with all its merged
    # tick ranges (list[list[tuple[int,int]]]) so that when split into
    # chunks, identical frames never straddle chunk boundaries.
    if deduplicate_cross:
        seen: dict[int, tuple[np.ndarray, list[tuple[int, int]]]] = {}
        order: list[int] = []
        for frame, (start, end) in zip(kept_frames, tick_ranges):
            h = hash(frame.tobytes())
            if h not in seen:
                seen[h] = (frame, [])
                order.append(h)
            seen[h][1].append((start, end))
        deduped = [seen[h] for h in order]
        kept_frames = [f for f, _ in deduped]
        tick_ranges = [ranges for _, ranges in deduped]
        total_input = len(kept_frames)
        sys.stderr.write(
            f"Cross-chunk dedup: {total_input} unique frames "
            f"({sum(len(r) for r in tick_ranges) - total_input} duplicates removed).\n"
        )

    # ── Split frames into time chunks ─────────────────────────────────
    total_ticks = current_tick - 1
    chunk_size = math.ceil(total_input / time_chunks)
    chunk_frames: list[list[np.ndarray]] = []
    chunk_tick_ranges: list[list] = []  # list[list] — may be multi-range per frame
    for ci in range(time_chunks):
        start = ci * chunk_size
        end = min(start + chunk_size, total_input)
        chunk_frames.append(kept_frames[start:end])
        chunk_tick_ranges.append(tick_ranges[start:end])

    def _rebase_chunk_tick_ranges(ranges: list) -> list:
        if not ranges:
            return ranges
        if isinstance(ranges[0], list):
            first_range = ranges[0][0] if ranges[0] else None
            if first_range is None:
                return ranges
            base_tick = first_range[0]
            return [
                [(start - base_tick, end - base_tick) for start, end in frame_ranges]
                for frame_ranges in ranges
            ]
        base_tick = ranges[0][0]
        return [(start - base_tick, end - base_tick) for start, end in ranges]

    sys.stderr.write(
        f"Splitting {total_input} frames over {total_ticks} ticks "
        f"→ {time_chunks} time chunk(s) (~{chunk_size} frames each).\n"
    )

    # ── Chunk cache setup ─────────────────────────────────────────────
    if use_cache:
        cache_dir = _chunk_cache_dir(
            source_id, time_chunks, total_w, total_h,
            fps, adaptive, threshold, deduplicate,
        )
        cache_dir.mkdir(parents=True, exist_ok=True)

        meta = {"time_chunks": time_chunks, "total_ticks": total_ticks}
        with open(_chunk_meta_path(cache_dir), "w") as f:
            json.dump(meta, f)
    else:
        cache_dir = None

    # ── Build chunks (parallel, with optional caching) ────────────────
    from ..logical_blueprint import LogicalBlueprint, from_toml, to_draftsman

    chunk_results: dict[int, LogicalBlueprint] = {}
    pending_indices: list[int] = []

    for ci in range(time_chunks):
        if use_cache and cache_dir is not None:
            cpath = cache_dir / f"chunk_{ci:04d}.toml"
            if cpath.exists():
                sys.stderr.write(f"Chunk {ci + 1}/{time_chunks}: cached, skipping.\n")
                chunk_results[ci] = from_toml(cpath.read_text(encoding="utf-8"))
                continue
        pending_indices.append(ci)

    if pending_indices:
        workers = chunk_workers or os.cpu_count() or 1
        sys.stderr.write(
            f"Building {len(pending_indices)} chunk(s) "
            f"with {workers} worker(s)…\n"
        )

        payloads: list[bytes] = []
        for ci in pending_indices:
            local_tick_ranges = _rebase_chunk_tick_ranges(chunk_tick_ranges[ci])
            if local_tick_ranges:
                # local_tick_ranges[ci] may be list[tuple] or list[list[tuple]]
                # after cross-dedup.  Access the last range's end tick safely.
                last_item = local_tick_ranges[-1]
                if isinstance(last_item, list):
                    chunk_cur_tick = last_item[-1][1] + 1
                else:
                    chunk_cur_tick = last_item[1] + 1
            else:
                chunk_cur_tick = 1
            data = {
                "chunk_idx": ci,
                "kept_frames": chunk_frames[ci],
                "tick_ranges": local_tick_ranges,
                "output_name": output_name,
                "deduplicate": deduplicate,
                "mapping_params": mapping_params,
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
                ci, lb_toml = future.result()
                lb = from_toml(lb_toml)
                chunk_results[ci] = lb
                if use_cache and cache_dir is not None:
                    cpath = cache_dir / f"chunk_{ci:04d}.toml"
                    _write_toml_cache_async(lb, cpath)
                    sys.stderr.write(f"Chunk {ci + 1}/{time_chunks}: cached.\n")

    # ── Assemble ordered chunk list ───────────────────────────────────
    chunk_lbs = [chunk_results[ci] for ci in range(time_chunks)]

    # ── Write individual chunk files if requested ─────────────────────
    if output_chunks_dir:
        from ..logical_blueprint import to_toml as _to_toml
        out_dir = Path(output_chunks_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for ci, lb in enumerate(chunk_lbs):
            cpath = out_dir / f"chunk_{ci:04d}.toml"
            cpath.write_text(_to_toml(lb), encoding="utf-8")
        sys.stderr.write(
            f"Wrote {len(chunk_lbs)} individual chunk TOML(s) "
            f"to {out_dir}/\n"
        )

    # ── Merge chunks ──────────────────────────────────────────────────
    full_lb = _merge_chunk_blueprints(chunk_lbs, output_name)
    full_bp = to_draftsman(full_lb)

    return {"full": full_bp, "chunks": [to_draftsman(lb) for lb in chunk_lbs]}


def _build_split_piece_worker(payload: bytes) -> tuple[str, str]:
    """Build one (vertical chunk × time fragment) memory piece in a worker.

    Returns ``(piece_label, blueprint_string)``.  ``to_draftsman`` + string
    serialisation run inside the worker so the heavy materialisation is
    parallel across pieces (the point of the split-output design).
    """
    from ..logical_blueprint import to_draftsman

    data = pickle.loads(payload)
    lb = _encode_frames_core(
        kept_frames=data["kept_frames"],
        tick_ranges=data["tick_ranges"],
        output_name=data["output_name"],
        deduplicate=data["deduplicate"],
        mapping_params=data["mapping_params"],
        clock=data["clock"],
        current_tick=data["current_tick"],
        label_suffix=data.get("label_suffix", ""),
        connectors=True,
        fragment_index=data.get("fragment_index"),
    )
    return data["label"], to_draftsman(lb).to_string()


def encode_frames_split(
    rgb_frames: Iterator[np.ndarray],
    output_name: str,
    fps: float = 0.0,
    adaptive: bool = False,
    threshold: float = 0.03,
    deduplicate: bool = False,
    total_width: int | None = None,
    total_height: int | None = None,
    expected_frames: int | None = None,
    source_id: str = "",
    *,
    time_chunks: int = 2,
    chunk_workers: int | None = None,
) -> dict:
    """Encode a video into independently-wireable pieces (split output).

    Instead of one giant merged blueprint, the video memory is emitted as a
    grid of pieces — one per (vertical display chunk × time fragment) — and
    the display as a single blueprint with per-chunk connector CCs.

    Each memory piece carries constant-combinator connectors on the LEFT and
    RIGHT of its data bus (carrying the chunk's identifying signal at value 0,
    so they add nothing to the bus) plus a non-wired fragment-series label CC.
    The display chunk connectors carry the same identifying signal.  The user
    places the display and each piece, then wires matching connectors in game.

    Each piece is built and materialised (``to_draftsman`` + ``to_string``)
    in a worker process, so the dominant cost parallelises across pieces.

    Returns ``{"display": str, "pieces": [(label, str), ...],
               "num_chunks": int, "time_chunks": int}``.
    """
    from ..logical_blueprint import to_draftsman
    from .player_blueprint import build_display_logical

    if fps <= 0:
        fps = 60.0
    fps = max(1.0, min(fps, 60.0))
    ticks_float = 60.0 / fps

    total_w = total_width if total_width is not None else DISPLAY_WIDTH
    total_h = total_height if total_height is not None else DISPLAY_HEIGHT

    # ── Phase 1: decode + resize + adaptive drop (same as encode_frames) ──
    kept_frames: list[np.ndarray] = []
    tick_ranges: list[tuple[int, int]] = []
    current_tick = 0
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
        for resized in tqdm(
            futures, total=expected_frames, desc="Resizing & Dropping", unit="frame",
        ):
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

    if not kept_frames:
        raise ValueError("No frames to encode.")

    total_input = len(kept_frames)
    qualities = QUALITIES
    signal_pool = SIGNAL_POOL

    # ── Display blueprint (one per display, with per-chunk connectors) ──
    display_lb = build_display_logical(
        f"{output_name} Display", total_w, total_h, connectors=True,
    )
    display_str = to_draftsman(display_lb).to_string()

    # ── Vertical chunks × time fragments ────────────────────────────
    from ..integer2signal.mapping import compute_chunking
    chunk_height, num_chunks = compute_chunking(total_w, total_h, signal_pool, qualities)

    time_chunks = max(1, time_chunks)
    fragment_size = math.ceil(total_input / time_chunks)

    payloads: list[bytes] = []
    for ci in range(num_chunks):
        y0 = ci * chunk_height
        y1 = min(y0 + chunk_height, total_h)
        ch_h = y1 - y0
        ch_mapping = SignalMapping(total_w, ch_h, qualities, signal_pool)
        mp = {
            "width": ch_mapping.width, "height": ch_mapping.height,
            "qualities": ch_mapping.qualities,
            "signal_pool": ch_mapping.base_signals,
        }
        for f in range(time_chunks):
            start = f * fragment_size
            end = min(start + fragment_size, total_input)
            if start >= end:
                continue
            chunk_frames = [fr[y0:y1, :, :] for fr in kept_frames[start:end]]
            frag_ticks = tick_ranges[start:end]
            last_tick = frag_ticks[-1][1] if frag_ticks else 0
            payloads.append(pickle.dumps({
                "label": f"memory_c{ci}_f{f}",
                "kept_frames": chunk_frames,
                "tick_ranges": frag_ticks,
                "output_name": f"{output_name} c{ci} f{f}",
                "deduplicate": deduplicate,
                "mapping_params": mp,
                "clock": CLOCK_SIGNAL,
                "current_tick": last_tick + 1,
                "label_suffix": f" [chunk {ci + 1}/{num_chunks}, frag {f + 1}/{time_chunks}]",
                "fragment_index": f,
            }))

    workers = chunk_workers or os.cpu_count() or 1
    workers = min(workers, len(payloads)) if payloads else 1
    sys.stderr.write(
        f"Splitting into {num_chunks} vertical chunk(s) × {time_chunks} time "
        f"fragment(s) = {len(payloads)} memory piece(s), built with "
        f"{workers} worker(s)...\n"
    )

    pieces: list[tuple[str, str]] = []
    if payloads:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_build_split_piece_worker, p) for p in payloads]
            for future in tqdm(
                concurrent.futures.as_completed(futures),
                total=len(futures), desc="Building memory pieces", unit="piece",
            ):
                pieces.append(future.result())

    # Keep deterministic order (label-sorted).
    pieces.sort(key=lambda kv: kv[0])

    return {
        "display": display_str,
        "pieces": pieces,
        "num_chunks": num_chunks,
        "time_chunks": time_chunks,
    }


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
    time_chunks: int = 1,
    chunk_workers: int | None = None,
    output_chunks_dir: str | None = None,
    deduplicate_cross: bool = False,
    use_cache: bool = False,
    split: bool = False,
) -> Blueprint:
    """Encode a video file (``.mp4``, ``.avi``, ``.mov``, etc.).

    Returns a :class:`~draftsman.blueprintable.Blueprint`, or (when
    *split* is True) a ``dict`` from :func:`encode_frames_split`.
    """
    cap = _videocap_utf8(str(video_path))
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
        width=total_width, height=total_height,
    )
    sys.stderr.write(
        f"Source: {source_w}×{source_h} -> output: {resolved_w}×{resolved_h}"
    )
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
        if split:
            return encode_frames_split(
                _iter(), output_name, effective_fps, adaptive, threshold, deduplicate,
                total_width=resolved_w, total_height=resolved_h,
                expected_frames=expected_frames, source_id=source_id,
                time_chunks=time_chunks, chunk_workers=chunk_workers,
            )
        if time_chunks > 1 or deduplicate_cross:
            result = encode_frames_chunked(
                _iter(), output_name, effective_fps, adaptive, threshold, deduplicate,
                total_width=resolved_w, total_height=resolved_h,
                expected_frames=expected_frames, source_id=source_id,
                time_chunks=time_chunks, chunk_workers=chunk_workers,
                output_chunks_dir=output_chunks_dir,
                deduplicate_cross=deduplicate_cross,
                use_cache=use_cache,
            )
            return result["full"]
        return encode_frames(_iter(), output_name, effective_fps, adaptive, threshold, deduplicate,
                              total_width=resolved_w, total_height=resolved_h,
                              expected_frames=expected_frames, source_id=source_id,
                              use_cache=use_cache)
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
    time_chunks: int = 1,
    chunk_workers: int | None = None,
    output_chunks_dir: str | None = None,
    deduplicate_cross: bool = False,
    use_cache: bool = False,
) -> Blueprint:
    """Encode an animated GIF.

    Returns a :class:`~draftsman.blueprintable.Blueprint`.
    """
    from PIL import Image

    gif = Image.open(str(gif_path))

    # Resolve output dimensions from source GIF size
    source_w, source_h = gif.size
    resolved_w, resolved_h = resolve_dimensions(
        source_w, source_h,
        width=total_width, height=total_height,
    )
    sys.stderr.write(
        f"Source GIF: {source_w}×{source_h} -> output: {resolved_w}×{resolved_h}"
    )
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
            use_cache=use_cache,
        )
        return result["full"]
    return encode_frames(_iter(), output_name, effective_fps, adaptive, threshold, deduplicate,
                          total_width=resolved_w, total_height=resolved_h,
                          expected_frames=expected_frames, source_id=source_id,
                          use_cache=use_cache)


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
    time_chunks: int = 1,
    chunk_workers: int | None = None,
    output_chunks_dir: str | None = None,
    deduplicate_cross: bool = False,
    use_cache: bool = False,
) -> Blueprint:
    """Encode a sequence of image files (PNG, JPEG, etc.).

    Returns a :class:`~draftsman.blueprintable.Blueprint`.
    """
    if fps <= 0:
        fps = 60.0

    # Read the first image to resolve output dimensions from source aspect ratio
    first_img = _imread_utf8(str(paths[0]))
    if first_img is None:
        raise FileNotFoundError(f"Cannot read image: {paths[0]}")
    source_h, source_w = first_img.shape[:2]
    resolved_w, resolved_h = resolve_dimensions(
        source_w, source_h,
        width=total_width, height=total_height,
    )
    sys.stderr.write(
        f"Source image: {source_w}×{source_h} -> output: {resolved_w}×{resolved_h}"
    )
    sys.stderr.write("\n")

    expected_frames = math.ceil(len(paths) / fps_skip)
    effective_fps = fps / float(fps_skip) if fps_skip > 0 else fps

    def _iter() -> Iterator[np.ndarray]:
        for i, p in enumerate(paths):
            if i % fps_skip != 0:
                continue
            img = _imread_utf8(str(p))
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
            use_cache=use_cache,
        )
        return result["full"]
    return encode_frames(_iter(), output_name, effective_fps, adaptive, threshold, deduplicate,
                          total_width=resolved_w, total_height=resolved_h,
                          expected_frames=expected_frames, source_id=source_id,
                          use_cache=use_cache)


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
    time_chunks: int = 1,
    chunk_workers: int | None = None,
    output_chunks_dir: str | None = None,
    deduplicate_cross: bool = False,
    use_cache: bool = False,
) -> Blueprint:
    """Encode a single still image as a one-frame blueprint.

    Returns a :class:`~draftsman.blueprintable.Blueprint`.
    """
    if fps <= 0:
        fps = 60.0
    return encode_png_series(
        [image_path], output_name, fps_skip=1, fps=fps,
        adaptive=adaptive, threshold=threshold, deduplicate=deduplicate,
        total_width=total_width, total_height=total_height,
        time_chunks=time_chunks, chunk_workers=chunk_workers,
        output_chunks_dir=output_chunks_dir,
        deduplicate_cross=deduplicate_cross,
        use_cache=use_cache,
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
    time_chunks: int = 1,
    chunk_workers: int | None = None,
    output_chunks_dir: str | None = None,
    deduplicate_cross: bool = False,
    use_cache: bool = False,
    split: bool = False,
) -> Blueprint:
    """Auto-detect input type and call the appropriate encoder.

    Returns a :class:`~draftsman.blueprintable.Blueprint`, or (when *split*
    is True and the input is a video) the ``dict`` from
    :func:`encode_frames_split`.
    """
    path = Path(input_path)
    video_exts = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
    image_exts = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif"}

    chunk_kwargs = {
        "time_chunks": time_chunks, "chunk_workers": chunk_workers,
        "output_chunks_dir": output_chunks_dir,
        "deduplicate_cross": deduplicate_cross,
        "use_cache": use_cache,
    }

    if path.is_dir():
        pngs = sorted(path.glob("*.png"))
        if not pngs:
            raise FileNotFoundError(f"No .png files found in directory: {input_path}")
        sys.stderr.write(f"Found {len(pngs)} PNG(s) in {input_path}\n")
        return encode_png_series(pngs, output_name, fps_skip, fps, adaptive, threshold, deduplicate,
                                  total_width=total_width, total_height=total_height,
                                  **chunk_kwargs)

    if "*" in input_path or "?" in input_path:
        matches = sorted(Path().glob(input_path))
        if not matches:
            raise FileNotFoundError(f"No files match pattern: {input_path}")
        sys.stderr.write(f"Matched {len(matches)} file(s) for pattern: {input_path}\n")
        return encode_png_series(matches, output_name, fps_skip, fps, adaptive, threshold, deduplicate,
                                  total_width=total_width, total_height=total_height,
                                  **chunk_kwargs)

    ext = path.suffix.lower()

    if ext in video_exts:
        return encode_video(input_path, output_name, fps_skip, fps, adaptive, threshold, deduplicate,
                             total_width=total_width, total_height=total_height,
                             split=split,
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
                                 **chunk_kwargs)
        return encode_gif(input_path, output_name, fps_skip, fps, adaptive, threshold, deduplicate,
                           total_width=total_width, total_height=total_height,
                           **chunk_kwargs)

    if ext in image_exts:
        return encode_frame(input_path, output_name, fps, adaptive, threshold, deduplicate,
                             total_width=total_width, total_height=total_height,
                             **chunk_kwargs)

    raise ValueError(
        f"Cannot determine input type for: {input_path}. "
        f"Use an explicit subcommand or a recognised file extension."
    )

