"""Media encoder — converts video, GIF, PNG series, and still images into
Factorio animation-memory blueprint strings.

All encoders share a common :func:`encode_frames` pipeline that builds the
decider-combinator chains (with embedded frame data) from an iterable of RGB frames.
"""

from __future__ import annotations

import argparse
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
    accum = 0.0                   # fractional remainder accumulator

    if config is None:
        config = load_config()
    if mapping is None:
        mapping = SignalMapping.from_manifest(config)

    w, h = config["display"]["width"], config["display"]["height"]
    clock = config["reserved"]["clock_signal"]

    # ==================================================================
    # Phase 1 — collect frame entries (resized image, tick range)
    # ==================================================================
    current_tick = 1
    prev_resized: np.ndarray | None = None
    carry_ticks = 0
    frame_entries: list[tuple[np.ndarray, int, int]] = []  # (resized, start, end_inclusive)

    for rgb in rgb_frames:
        resized = cv2.resize(rgb, (w, h), interpolation=cv2.INTER_AREA)
        if resized.dtype != np.uint8:
            resized = resized.astype(np.uint8)

        # ---- per-frame tick budget (error-accumulation, no static rounding) --
        accum += ticks_float
        # int() truncates; a tiny epsilon avoids 2.999… → 2 from float drift
        needed = max(1, int(accum + 1e-9))
        accum -= needed

        # ---- adaptive consecutive-frame dropping ----------------------------
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

    # ==================================================================
    # Phase 2 — deduplicate (optional)
    # ==================================================================
    # Each unique frame gets one combinator whose conditions cover
    # all the tick ranges where that frame appeared.
    if deduplicate:
        seen: dict[str, tuple[np.ndarray, list[tuple[int, int]]]] = {}
        order: list[str] = []  # hashes in first-occurrence order
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

    # ==================================================================
    # Phase 3 — build blueprint
    # ==================================================================
    blueprint = Blueprint()
    blueprint.label = f"Video Memory: {output_name}"

    prev_dc_id: str | None = None
    x_offset = 0

    for gate_num, (resized, ranges) in enumerate(unique_frames, start=1):
        dc_id = f"gate_{gate_num}"

        # ---- conditions (one or more tick ranges, OR-ed) -------------------
        # Factorio semantics: compare_type on condition N specifies how N
        # relates to the *preceding* accumulated result.  Draftsman's
        # default is "or" (which Factorio also defaults to when omitted),
        # so only "and" connections must be set explicitly.
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

        # ---- outputs (pixel colour signals) --------------------------------
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
            tile_position=(x_offset, 1),
            direction=Direction.SOUTH,
        )
        dc.conditions = conditions
        dc.outputs = outputs
        blueprint.entities.append(dc)

        # ---- chain wiring --------------------------------------------------
        if prev_dc_id is not None:
            # Ticks in green, frame data in red
            blueprint.add_circuit_connection(
                "green", prev_dc_id, dc_id, side_1="input", side_2="input"
            )
            blueprint.add_circuit_connection(
                "red", prev_dc_id, dc_id, side_1="output", side_2="output"
            )

        prev_dc_id = dc_id
        x_offset += 1

    # ==================================================================
    # Summary
    # ==================================================================
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
        return encode_frames(_iter(), output_name, fps, adaptive, threshold, deduplicate)
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

    return encode_frames(_iter(), output_name, fps, adaptive, threshold, deduplicate)


def encode_png_series(
    paths: list[str | Path],
    output_name: str = "Animation Data",
    fps_skip: int = 1,
    fps: int = 0,
    adaptive: bool = False,
    threshold: float = 0.03,
    deduplicate: bool = False,
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

    return encode_frames(_iter(), output_name, fps, adaptive, threshold, deduplicate)


def encode_frame(
    image_path: str | Path,
    output_name: str = "Frame Data",
    fps: int = 0,
    adaptive: bool = False,
    threshold: float = 0.03,
    deduplicate: bool = False,
) -> str:
    """Encode a single still image as a one-frame blueprint."""
    if fps == 0:
        fps = 60
    return encode_png_series(
        [image_path], output_name, fps_skip=1, fps=fps,
        adaptive=adaptive, threshold=threshold, deduplicate=deduplicate,
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
        return encode_png_series(pngs, output_name, fps_skip, fps, adaptive, threshold, deduplicate)

    # Glob patterns must be checked *before* extension matching
    if "*" in input_path or "?" in input_path:
        matches = sorted(Path().glob(input_path))
        if not matches:
            raise FileNotFoundError(f"No files match pattern: {input_path}")
        sys.stderr.write(f"Matched {len(matches)} file(s) for pattern: {input_path}\n")
        return encode_png_series(matches, output_name, fps_skip, fps, adaptive, threshold, deduplicate)

    ext = path.suffix.lower()

    if ext in video_exts:
        return encode_video(input_path, output_name, fps_skip, fps, adaptive, threshold, deduplicate)

    if ext == ".gif":
        # Check if animated or static
        from PIL import Image
        try:
            gif = Image.open(str(path))
            gif.seek(1)
        except EOFError:
            # Static GIF → treat as single frame
            return encode_frame(input_path, output_name, fps, adaptive, threshold, deduplicate)
        return encode_gif(input_path, output_name, fps_skip, fps, adaptive, threshold, deduplicate)

    if ext in image_exts:
        return encode_frame(input_path, output_name, fps, adaptive, threshold, deduplicate)

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

    args = parser.parse_args()
    bp_string = encode_auto(
        args.input_path, args.name, args.skip, args.fps,
        args.adaptive, args.threshold, args.deduplicate,
    )
    print(bp_string)


if __name__ == "__main__":
    main()
