"""Media encoder — converts video, GIF, PNG series, and still images into
Factorio animation-memory blueprint strings.

All encoders share a common :func:`encode_frames` pipeline that builds the
decider-combinator chains (with embedded frame data) from an iterable of RGB frames.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys
from typing import Iterator

import cv2
import numpy as np
from draftsman.blueprintable import Blueprint
from draftsman.constants import Direction
from draftsman.entity import DeciderCombinator, new_entity

from .config_loader import load_config
from .signal_mapping import SignalMapping


# ---------------------------------------------------------------------------
# Common pipeline
# ---------------------------------------------------------------------------

def _frame_diff(a: np.ndarray, b: np.ndarray) -> float:
    """Return 0.0–1.0 normalised mean absolute difference between two RGB frames."""
    return float(np.mean(np.abs(a.astype(np.float32) - b.astype(np.float32))) / 255.0)


def encode_frames(
    rgb_frames: Iterator[np.ndarray],
    output_name: str,
    fps: int = 0,
    adaptive: bool = False,
    threshold: float = 0.03,
    deduplicate: bool = False,
    config: dict | None = None,
    mapping: SignalMapping | None = None,
    total_width: int | None = None,
    total_height: int | None = None,
) -> str:
    """Encode an iterable of RGB ``(H, W, 3)`` uint8 frames into a blueprint.

    Each frame becomes one decider-combinator whose outputs hold the pixel
    colour data directly.  Combinators are chained for sequential playback
    via a clock signal.

    Parameters
    ----------
    rgb_frames : Iterator[np.ndarray]
        Iterator yielding RGB images as ``(H, W, 3)`` uint8 numpy arrays.
    output_name : str
        Label for the generated blueprint.
    fps : int
        **Source** frame rate (1–60).  1 s = 60 Factorio ticks, so a 30 fps
        source gets ``round(60/30) = 2`` ticks per frame.  When ``0``
        (default) it falls back to 60 — the outer encoder functions
        auto-detect the real value from media metadata.
    adaptive : bool
        Drop near-duplicate *consecutive* frames whose pixel difference falls
        below *threshold*; their tick budget merges into the preceding frame.
    threshold : float
        Normalised mean-absolute-difference cutoff (0.0–1.0) for adaptive
        dropping.  Only used when *adaptive* is *True*.
    deduplicate : bool
        When *True*, frames with identical content (even non-adjacent) share
        a single combinator whose conditions cover all their tick ranges via
        OR logic.  Frame identity is determined by a SHA-256 hash of the
        resized pixel buffer.
    config : dict | None
        Parsed TOML config.  Loaded from ``config.toml`` when *None*.
    mapping : SignalMapping | None
        Pre-built signal mapping.  Reconstructed from the manifest when *None*.
    """
    import hashlib

    if fps == 0:
        fps = 60
    fps = max(1, min(fps, 60))
    ticks_float = 60.0 / fps      # non-integer ticks-per-frame target

    if config is None:
        config = load_config()
    if mapping is None:
        mapping = SignalMapping.from_manifest(config)

    unit_w = config["display"]["width"]
    unit_h = config["display"]["height"]
    total_w = total_width if total_width is not None else unit_w
    total_h = total_height if total_height is not None else unit_h
    unit_cols = math.ceil(total_w / unit_w)
    unit_rows = math.ceil(total_h / unit_h)
    num_units = unit_cols * unit_rows

    clock = config["reserved"]["clock_signal"]

    # ==================================================================
    # Single-unit path (backward-compatible fast path)
    # ==================================================================
    if num_units == 1:
        accum = 0.0
        w, h = unit_w, unit_h

        current_tick = 1
        prev_resized: np.ndarray | None = None
        carry_ticks = 0
        frame_entries: list[tuple[np.ndarray, int, int]] = []

        for rgb in rgb_frames:
            resized = cv2.resize(rgb, (w, h), interpolation=cv2.INTER_AREA)
            if resized.dtype != np.uint8:
                resized = resized.astype(np.uint8)

            accum += ticks_float
            needed = max(1, int(accum + 1e-9))
            accum -= needed

            if adaptive and prev_resized is not None:
                if _frame_diff(prev_resized, resized) < threshold:
                    carry_ticks += needed
                    prev_resized = resized
                    continue

            prev_resized = resized.copy()
            frame_ticks = needed + carry_ticks
            carry_ticks = 0

            tick_end = current_tick + frame_ticks - 1
            frame_entries.append((resized, current_tick, tick_end))
            current_tick += frame_ticks

        total_input = len(frame_entries)
        if total_input == 0:
            sys.stderr.write("No frames to encode.\n")
            return ""

        if deduplicate:
            seen: dict[str, tuple[np.ndarray, list[tuple[int, int]]]] = {}
            order: list[str] = []
            for resized, start, end in frame_entries:
                h = hashlib.sha256(resized.tobytes()).hexdigest()
                if h not in seen:
                    seen[h] = (resized, [])
                    order.append(h)
                seen[h][1].append((start, end))
            unique_frames: list[tuple[np.ndarray, list[tuple[int, int]]]] = [
                seen[h] for h in order
            ]
        else:
            unique_frames = [(resized, [(start, end)]) for resized, start, end in frame_entries]

        blueprint = Blueprint()
        blueprint.label = f"Video Memory: {output_name}"

        total = len(unique_frames)
        cols = max(1, math.isqrt(max(0, 2 * total - 1)) + 1) if total > 0 else 1
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
                f"Encoded {total_combinators} combinators for {total_input} frames "
                f"({total_input - total_combinators} deduplicated) "
                f"over {total_ticks} ticks.\n"
            )
        elif adaptive and total_input > 0:
            sys.stderr.write(
                f"Encoded {total_input} frames over {total_ticks} ticks "
                f"(adaptive, threshold={threshold:.3f}).\n"
            )
        else:
            sys.stderr.write(
                f"Encoded {total_input} frames over {total_ticks} ticks "
                f"(~{total_ticks / max(1, total_input):.1f} tick(s)/frame, source {fps} fps).\n"
            )
        return blueprint.to_string()

    # ==================================================================
    # Multi-unit path
    # ==================================================================
    # Phase 0 — buffer all input frames at total resolution
    all_frames: list[np.ndarray] = []
    for rgb in rgb_frames:
        resized = cv2.resize(rgb, (total_w, total_h), interpolation=cv2.INTER_AREA)
        if resized.dtype != np.uint8:
            resized = resized.astype(np.uint8)
        all_frames.append(resized)

    if not all_frames:
        sys.stderr.write("No frames to encode.\n")
        return ""

    # Phase 1 — compute tick ranges via full-frame adaptive dropping
    # (tick ranges must be consistent across all units)
    accum = 0.0
    carry_ticks = 0
    prev_resized: np.ndarray | None = None
    frame_ticks_list: list[int] = []
    kept_frames: list[np.ndarray] = []

    for resized in all_frames:
        accum += ticks_float
        needed = max(1, int(accum + 1e-9))
        accum -= needed

        if adaptive and prev_resized is not None:
            if _frame_diff(prev_resized, resized) < threshold:
                carry_ticks += needed
                prev_resized = resized
                continue

        if adaptive:
            prev_resized = resized.copy()
        frame_ticks = needed + carry_ticks
        carry_ticks = 0
        frame_ticks_list.append(frame_ticks)
        kept_frames.append(resized)

    current_tick = 1
    tick_ranges: list[tuple[int, int]] = []
    for ft in frame_ticks_list:
        tick_ranges.append((current_tick, current_tick + ft - 1))
        current_tick += ft

    total_input = len(kept_frames)

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
            unit_unique.append(
                [(resized, [(start, end)]) for resized, start, end in entries]
            )

    # Phase 4 — build blueprint: per-unit grids, 2-tile margins, relay poles
    blueprint = Blueprint()
    blueprint.label = (
        f"Video Memory: {output_name} "
        f"({total_w}×{total_h}, {unit_cols}×{unit_rows} units)"
    )

    # Compute per-unit grid dimensions, pad to fill the rectangle completely
    # so the snake wiring never encounters gaps > 1 tile.
    unit_grids: list[tuple[int, int, int]] = []  # (padded_total, cols, rows)
    for ui, unique_frames in enumerate(unit_unique):
        t = len(unique_frames)
        c = max(1, math.isqrt(max(0, 2 * t - 1)) + 1) if t > 0 else 1
        if c > 26:
            raise ValueError(
                f"Unit {ui} combinator grid is {c} columns wide (max 26). "
                f"Too many frames ({t}) — reduce frame count, increase --skip, "
                f"or use --adaptive/--deduplicate."
            )
        r = (t + c - 1) // c
        # Pad with dummy frames so every grid cell is occupied
        missing = c * r - t
        if missing > 0:
            dummy = np.zeros((unit_h, unit_w, 3), dtype=np.uint8)
            dummy_ranges = [(999999, 999999)]  # never-active condition
            for _ in range(missing):
                unique_frames.append((dummy, dummy_ranges))
        unit_grids.append((c * r, c, r))

    MARGIN = 2  # tiles between unit grids

    # Compute row-wise max heights and cumulative column offsets
    row_max_rows: list[int] = []
    for ur in range(unit_rows):
        mr = 0
        for uc in range(unit_cols):
            _, _, gr = unit_grids[ur * unit_cols + uc]
            mr = max(mr, gr)
        row_max_rows.append(mr)

    # For each unit, compute its absolute origin (top-left tile)
    unit_origins: list[tuple[int, int]] = []  # (col, row) — row in TILES (not ×2)
    cum_row = 0
    for ur in range(unit_rows):
        cum_col = 0
        for uc in range(unit_cols):
            unit_origins.append((cum_col, cum_row))
            _, c, _ = unit_grids[ur * unit_cols + uc]
            cum_col += c + MARGIN
        cum_row += row_max_rows[ur] * 2 + MARGIN

    # Track each unit's boundary combinators for direct green-wire hops.
    unit_top_left: dict[int, str] = {}
    unit_top_right: dict[int, str] = {}
    unit_bottom_right: dict[int, str] = {}

    for ui, (unique_frames, (total, cols, rows)) in enumerate(
        zip(unit_unique, unit_grids)
    ):
        ox, oy = unit_origins[ui]  # tile origin
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

        # Store boundary combinators for direct inter-unit green wiring.
        # top-left: entry point. top-right: first row last col. bottom-right: last row last col.
        unit_top_left[ui] = dc_grid[(0, 0)]
        unit_top_right[ui] = dc_grid[(0, cols - 1)]
        unit_bottom_right[ui] = dc_grid[(rows - 1, cols - 1)]

    # ---- direct inter-unit green wiring (no poles) ------------------------
    # Horizontal: connect left unit's top-right → right unit's top-left.
    # Distance = MARGIN + 1 ≤ 3 tiles.
    for ur in range(unit_rows):
        for uc in range(unit_cols - 1):
            ui_left = ur * unit_cols + uc
            ui_right = ur * unit_cols + uc + 1
            blueprint.add_circuit_connection(
                "green", unit_top_right[ui_left], unit_top_left[ui_right],
                side_1="input", side_2="input",
            )

    # Vertical: connect upper unit's bottom-right → lower unit's top-right.
    # Both at the same column within their respective grids → dx = 0.
    # dy = margin_gap + 2*(max_rows - rows_upper + 0) … worst-case dy ≤ MARGIN*2 + 2 ≤ 6.
    for ur in range(unit_rows - 1):
        for uc in range(unit_cols):
            ui_top = ur * unit_cols + uc
            ui_bot = (ur + 1) * unit_cols + uc
            blueprint.add_circuit_connection(
                "green", unit_bottom_right[ui_top],
                unit_top_right[ui_bot],
                side_1="input", side_2="input",
            )

    # ==================================================================
    # Summary
    # ==================================================================
    total_ticks = current_tick - 1
    total_combinators = sum(len(uf) for uf in unit_unique)
    sys.stderr.write(
        f"Encoded {total_input} frames over {total_ticks} ticks "
        f"→ {total_combinators} combinators across {num_units} display units "
        f"({unit_cols}×{unit_rows} grid, unit size {unit_w}×{unit_h}).\n"
    )
    return blueprint.to_string()


# ---------------------------------------------------------------------------
# Input-specific encoders
# ---------------------------------------------------------------------------

def encode_video(
    video_path: str | Path,
    output_name: str = "Animation Data",
    fps_skip: int = 1,
    fps: int = 0,
    adaptive: bool = False,
    threshold: float = 0.03,
    deduplicate: bool = False,
    total_width: int | None = None,
    total_height: int | None = None,
) -> str:
    """Encode a video file (``.mp4``, ``.avi``, ``.mov``, …).

    *fps* is the source frame rate.  When ``0`` (default) it is
    auto-detected from the video metadata.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {video_path}")

    # ---- auto-detect source FPS -------------------------------------------
    if fps == 0:
        detected = cap.get(cv2.CAP_PROP_FPS)
        fps = int(round(detected)) if detected and detected > 0 else 30
        fps = max(1, min(fps, 60))
        sys.stderr.write(f"Detected source FPS: {fps}\n")

    def _iter() -> Iterator[np.ndarray]:
        while True:
            for _ in range(fps_skip):
                ret, frame = cap.read()
            if not ret:
                return
            yield cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    try:
        return encode_frames(_iter(), output_name, fps, adaptive, threshold, deduplicate,
                              total_width=total_width, total_height=total_height)
    finally:
        cap.release()


def encode_gif(
    gif_path: str | Path,
    output_name: str = "Animation Data",
    fps_skip: int = 1,
    fps: int = 0,
    adaptive: bool = False,
    threshold: float = 0.03,
    deduplicate: bool = False,
    total_width: int | None = None,
    total_height: int | None = None,
) -> str:
    """Encode an animated GIF.

    *fps* is the source frame rate.  When ``0`` (default) it is derived
    from the GIF's per-frame duration metadata.
    """
    from PIL import Image

    gif = Image.open(str(gif_path))

    # ---- auto-detect source FPS from GIF frame duration --------------------
    if fps == 0:
        duration = gif.info.get("duration", 0)  # ms per frame (global default)
        if not duration:
            # Try per-frame duration
            try:
                gif.seek(0)
                duration = gif.info.get("duration", 100)
            except Exception:
                duration = 100
        fps = max(1, min(60, round(1000 / duration))) if duration else 10
        sys.stderr.write(f"Detected source FPS: {fps} (from GIF)\n")

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

    return encode_frames(_iter(), output_name, fps, adaptive, threshold, deduplicate,
                          total_width=total_width, total_height=total_height)


def encode_png_series(
    paths: list[str | Path],
    output_name: str = "Animation Data",
    fps_skip: int = 1,
    fps: int = 0,
    adaptive: bool = False,
    threshold: float = 0.03,
    deduplicate: bool = False,
    total_width: int | None = None,
    total_height: int | None = None,
) -> str:
    """Encode a sequence of image files (PNG, JPEG, …).

    *fps* is the source frame rate.  When ``0`` (default) it falls back
    to 60 (PNG series carry no inherent timing metadata).
    """
    if fps == 0:
        fps = 60

    def _iter() -> Iterator[np.ndarray]:
        for i, p in enumerate(paths):
            if i % fps_skip != 0:
                continue
            img = cv2.imread(str(p))
            if img is None:
                raise FileNotFoundError(f"Cannot read image: {p}")
            yield cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    return encode_frames(_iter(), output_name, fps, adaptive, threshold, deduplicate,
                          total_width=total_width, total_height=total_height)


def encode_frame(
    image_path: str | Path,
    output_name: str = "Frame Data",
    fps: int = 0,
    adaptive: bool = False,
    threshold: float = 0.03,
    deduplicate: bool = False,
    total_width: int | None = None,
    total_height: int | None = None,
) -> str:
    """Encode a single still image as a one-frame blueprint."""
    if fps == 0:
        fps = 60
    return encode_png_series(
        [image_path], output_name, fps_skip=1, fps=fps,
        adaptive=adaptive, threshold=threshold, deduplicate=deduplicate,
        total_width=total_width, total_height=total_height,
    )


# ---------------------------------------------------------------------------
# Convenience — auto-detect input type and dispatch
# ---------------------------------------------------------------------------

def encode_auto(
    input_path: str,
    output_name: str = "Animation Data",
    fps_skip: int = 1,
    fps: int = 0,
    adaptive: bool = False,
    threshold: float = 0.03,
    deduplicate: bool = False,
    total_width: int | None = None,
    total_height: int | None = None,
) -> str:
    """Auto-detect input type and call the appropriate encoder.

    Detection rules
    ---------------
    * ``.mp4`` / ``.avi`` / ``.mov`` / ``.mkv`` / ``.webm`` → :func:`encode_video`
    * ``.gif`` → :func:`encode_gif`
    * Single image (``.png``, ``.jpg``, …) → :func:`encode_frame`
    * Directory → glob ``*.png`` inside, call :func:`encode_png_series`
    * Glob pattern (contains ``*`` or ``?``) → expand, call :func:`encode_png_series`
    """
    path = Path(input_path)
    video_exts = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
    image_exts = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif"}

    if path.is_dir():
        pngs = sorted(path.glob("*.png"))
        if not pngs:
            raise FileNotFoundError(f"No .png files found in directory: {input_path}")
        sys.stderr.write(f"Found {len(pngs)} PNG(s) in {input_path}\n")
        return encode_png_series(pngs, output_name, fps_skip, fps, adaptive, threshold, deduplicate,
                                  total_width=total_width, total_height=total_height)

    # Glob patterns must be checked *before* extension matching
    if "*" in input_path or "?" in input_path:
        matches = sorted(Path().glob(input_path))
        if not matches:
            raise FileNotFoundError(f"No files match pattern: {input_path}")
        sys.stderr.write(f"Matched {len(matches)} file(s) for pattern: {input_path}\n")
        return encode_png_series(matches, output_name, fps_skip, fps, adaptive, threshold, deduplicate,
                                  total_width=total_width, total_height=total_height)

    ext = path.suffix.lower()

    if ext in video_exts:
        return encode_video(input_path, output_name, fps_skip, fps, adaptive, threshold, deduplicate,
                             total_width=total_width, total_height=total_height)

    if ext == ".gif":
        # Check if animated or static
        from PIL import Image
        try:
            gif = Image.open(str(path))
            gif.seek(1)
        except EOFError:
            # Static GIF → treat as single frame
            return encode_frame(input_path, output_name, fps, adaptive, threshold, deduplicate,
                                 total_width=total_width, total_height=total_height)
        return encode_gif(input_path, output_name, fps_skip, fps, adaptive, threshold, deduplicate,
                           total_width=total_width, total_height=total_height)

    if ext in image_exts:
        return encode_frame(input_path, output_name, fps, adaptive, threshold, deduplicate,
                             total_width=total_width, total_height=total_height)

    raise ValueError(
        f"Cannot determine input type for: {input_path}. "
        f"Use an explicit subcommand or a recognised file extension."
    )


# ---------------------------------------------------------------------------
# Legacy CLI (kept for ``python -m factorio_display.encoder``)
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Convert media to Factorio Animation Memory")
    parser.add_argument("input_path", help="Path to input file or directory")
    parser.add_argument("--name", default="Animation Data", help="Name of the blueprint")
    parser.add_argument("--skip", type=int, default=2, help="Read every Nth frame")
    parser.add_argument(
        "--fps", type=int, default=0,
        help="Source frame rate (1–60).  0 = auto-detect from media metadata.",
    )
    parser.add_argument(
        "--adaptive", action="store_true",
        help="Drop near-duplicate frames to compress static sections",
    )
    parser.add_argument(
        "--threshold", type=float, default=0.03,
        help="Similarity cutoff for adaptive mode (0.0–1.0, default: 0.03)",
    )
    parser.add_argument(
        "--deduplicate", action="store_true",
        help="Share one combinator across non-adjacent identical frames",
    )
    parser.add_argument(
        "--width", type=int, default=None,
        help="Total display width in tiles (overrides config).",
    )
    parser.add_argument(
        "--height", type=int, default=None,
        help="Total display height in tiles (overrides config).",
    )

    args = parser.parse_args()
    bp_string = encode_auto(
        args.input_path, args.name, args.skip, args.fps,
        args.adaptive, args.threshold, args.deduplicate,
        total_width=args.width, total_height=args.height,
    )
    print(bp_string)


if __name__ == "__main__":
    main()
