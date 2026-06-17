"""Video/media encoder — converts video, GIF, PNG series, and still images into
Factorio animation-memory blueprint strings.

All encoders share a common :func:`encode_frames` pipeline that builds the
decider-combinator chains (with embedded frame data) from an iterable of RGB frames.

For audio decoding, see :mod:`factorio_display.build_audio_decoder`.
"""

from __future__ import annotations

import argparse
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

from .config_loader import load_config
from .signal_mapping import SignalMapping

class _DummyTqdm:
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

try:
    from tqdm import tqdm
except ImportError:
    tqdm = _DummyTqdm


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
    expected_frames: int | None = None,
) -> str:
    """Encode an iterable of RGB ``(H, W, 3)`` uint8 frames into a blueprint.

    Each frame becomes one decider-combinator whose outputs hold the pixel
    colour data directly.  Combinators are chained for sequential playback
    via a clock signal.
    """
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
    # Phase 0 & 1: Parallel Resizing, Adaptive Dropping, and Caching
    # ==================================================================
    
    # Generate a unique cache name based on the output intent and parameters
    safe_name = hashlib.md5(f"{output_name}_{total_w}_{total_h}".encode('utf-8')).hexdigest()[:8]
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
                    if _frame_diff(prev_resized, resized) < threshold:
                        carry_ticks += needed
                        prev_resized = resized
                        continue

                if adaptive:
                    prev_resized = resized.copy()
                frame_ticks = needed + carry_ticks
                carry_ticks = 0

                tick_ranges.append((current_tick, current_tick + frame_ticks - 1))
                kept_frames.append(resized)
                current_tick += frame_ticks

        try:
            with open(cache_file, "wb") as f:
                pickle.dump({
                    "frames": kept_frames,
                    "ticks": tick_ranges,
                    "current_tick": current_tick
                }, f)
        except Exception as e:
            sys.stderr.write(f"Failed to write cache: {e}\n")

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
            with tqdm(total=len(frame_entries), desc="Deduplicating", unit="frame") as pbar:
                for resized, start, end in frame_entries:
                    h = hashlib.sha256(resized.tobytes()).hexdigest()
                    if h not in seen:
                        seen[h] = (resized, [])
                        order.append(h)
                    seen[h][1].append((start, end))
                    pbar.update(1)
            unique_frames = [seen[h] for h in order]
        else:
            unique_frames = [(resized, [(start, end)]) for resized, start, end in frame_entries]

        blueprint = Blueprint()
        blueprint.label = f"Video Memory: {output_name}"
        blueprint.icons = ["parameter-0"]

        total = len(unique_frames)
        cols = max(1, math.isqrt(max(0, 2 * total - 1)) + 1) if total > 0 else 1
        rows = (total + cols - 1) // cols

        dc_grid: dict[tuple[int, int], str] = {}
        with tqdm(total=total, desc="Building blueprint", unit="frame") as pbar:
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
                
                # Granular progression
                pbar.update(1)

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

    # Phase 2 — split into per-unit regions
    unit_entries: list[list[tuple[np.ndarray, int, int]]] = [
        [] for _ in range(num_units)
    ]
    for frame, (start, end) in tqdm(zip(kept_frames, tick_ranges), total=len(kept_frames), desc="Splitting regions", unit="frame"):
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
    total_entries = sum(len(e) for e in unit_entries)
    
    with tqdm(total=total_entries, desc="Deduplicating", unit="frame") as pbar:
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
                    pbar.update(1)
                unit_unique.append([seen[h] for h in order])
            else:
                current_unique = []
                for resized, start, end in entries:
                    current_unique.append((resized, [(start, end)]))
                    pbar.update(1)
                unit_unique.append(current_unique)

    # Phase 4 — build blueprint: per-unit grids, 2-tile margins, relay poles
    blueprint = Blueprint()
    blueprint.label = (
        f"Video Memory: {output_name} "
        f"({total_w}×{total_h}, {unit_cols}×{unit_rows} units)"
    )
    blueprint.icons = ["parameter-0"]

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

    unit_top_left: dict[int, str] = {}
    unit_top_right: dict[int, str] = {}
    unit_bottom_right: dict[int, str] = {}

    total_unique_frames = sum(len(uf) for uf in unit_unique)
    with tqdm(total=total_unique_frames, desc="Building blueprints", unit="frame") as pbar:
        for ui, (unique_frames, (total, cols, rows)) in enumerate(zip(unit_unique, unit_grids)):
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
                
                # Granular progression
                pbar.update(1)

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

    total_ticks = current_tick - 1
    total_combinators = sum(len(uf) for uf in unit_unique)
    sys.stderr.write(
        f"\nEncoded {total_input} frames over {total_ticks} ticks "
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
    """Encode a video file (``.mp4``, ``.avi``, ``.mov``, …)."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {video_path}")

    if fps == 0:
        detected = cap.get(cv2.CAP_PROP_FPS)
        fps = int(round(detected)) if detected and detected > 0 else 30
        fps = max(1, min(fps, 60))
        sys.stderr.write(f"Detected source FPS: {fps}\n")

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    expected_frames = max(1, total_frames // fps_skip) if total_frames > 0 else None

    def _iter() -> Iterator[np.ndarray]:
        while True:
            for _ in range(fps_skip):
                ret, frame = cap.read()
            if not ret:
                return
            yield cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    try:
        return encode_frames(_iter(), output_name, fps, adaptive, threshold, deduplicate,
                              total_width=total_width, total_height=total_height,
                              expected_frames=expected_frames)
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
    """Encode an animated GIF."""
    from PIL import Image

    gif = Image.open(str(gif_path))

    if fps == 0:
        duration = gif.info.get("duration", 0)
        if not duration:
            try:
                gif.seek(0)
                duration = gif.info.get("duration", 100)
            except Exception:
                duration = 100
        fps = max(1, min(60, round(1000 / duration))) if duration else 10
        sys.stderr.write(f"Detected source FPS: {fps} (from GIF)\n")

    try:
        expected_frames = max(1, getattr(gif, "n_frames", 1) // fps_skip)
    except Exception:
        expected_frames = None

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
                          total_width=total_width, total_height=total_height,
                          expected_frames=expected_frames)


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
    """Encode a sequence of image files (PNG, JPEG, …)."""
    if fps == 0:
        fps = 60

    expected_frames = math.ceil(len(paths) / fps_skip)

    def _iter() -> Iterator[np.ndarray]:
        for i, p in enumerate(paths):
            if i % fps_skip != 0:
                continue
            img = cv2.imread(str(p))
            if img is None:
                raise FileNotFoundError(f"Cannot read image: {p}")
            yield cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    return encode_frames(_iter(), output_name, fps, adaptive, threshold, deduplicate,
                          total_width=total_width, total_height=total_height,
                          expected_frames=expected_frames)


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
    """Auto-detect input type and call the appropriate encoder."""
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
        from PIL import Image
        try:
            gif = Image.open(str(path))
            gif.seek(1)
        except EOFError:
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



