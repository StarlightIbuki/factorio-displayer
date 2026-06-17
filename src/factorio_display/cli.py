"""Unified CLI for factorio-display.

``display``  — output the pre-computed display-unit blueprint
``encode``   — auto-detect input (video / GIF / PNG series / image) → blueprint
``frame``    — encode a single still image as a one-frame blueprint
"""

from __future__ import annotations

import argparse
import sys

from ._generated import DISPLAY_BLUEPRINT, POOL_HASH, VERSION
from .encoder import encode_auto, encode_frame


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="factorio-display",
        description="Factorio display blueprint builder and video encoder",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"factorio-display {VERSION}",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # ---- display -----------------------------------------------------------
    disp = subparsers.add_parser(
        "display",
        help="Output the pre-computed display-unit blueprint",
    )
    disp.add_argument(
        "--hash",
        action="store_true",
        help="Print only the signal-pool hash (useful for version checks)",
    )

    # ---- encode (auto-detect) ----------------------------------------------
    enc = subparsers.add_parser(
        "encode",
        help="Auto-detect input type (video, GIF, PNG series, image) and encode",
    )
    enc.add_argument("input_path", help="Path to input file, directory, or glob pattern")
    enc.add_argument(
        "--name",
        default="Animation Data",
        help="Label for the generated blueprint",
    )
    enc.add_argument(
        "--skip",
        type=int,
        default=2,
        help="Sample every Nth frame to reduce combinator count (default: 2)",
    )
    enc.add_argument(
        "--fps",
        type=int,
        default=0,
        help="Source frame rate (1–60). 0 = auto-detect from video/GIF metadata. 1s = 60 Factorio ticks.",
    )
    enc.add_argument(
        "--adaptive",
        action="store_true",
        help="Drop near-duplicate frames (based on pixel difference) to compress static sections",
    )
    enc.add_argument(
        "--threshold",
        type=float,
        default=0.03,
        help="Similarity cutoff for --adaptive (0.0–1.0, lower = stricter, default: 0.03)",
    )
    enc.add_argument(
        "--deduplicate",
        action="store_true",
        help="Share one combinator across non-adjacent identical frames (SHA-256 hash)",
    )
    enc.add_argument(
        "--width",
        type=int,
        default=None,
        help="Total display width in tiles (overrides config). When larger than the unit width, "
        "the display is split into multiple display units each with their own parallel memory.",
    )
    enc.add_argument(
        "--height",
        type=int,
        default=None,
        help="Total display height in tiles (overrides config). When larger than the unit height, "
        "the display is split into multiple display units each with their own parallel memory.",
    )

    # ---- frame (single image) ----------------------------------------------
    frm = subparsers.add_parser(
        "frame",
        help="Encode a single image as a one-frame blueprint",
    )
    frm.add_argument("image", help="Path to the image file (.png, .jpg, .gif, …)")
    frm.add_argument(
        "--name",
        default="Frame Data",
        help="Label for the generated blueprint",
    )
    frm.add_argument(
        "--fps",
        type=int,
        default=0,
        help="Source frame rate for the single frame (1–60, 0 = default 60)",
    )
    frm.add_argument(
        "--width",
        type=int,
        default=None,
        help="Total display width in tiles (overrides config).",
    )
    frm.add_argument(
        "--height",
        type=int,
        default=None,
        help="Total display height in tiles (overrides config).",
    )

    args = parser.parse_args()

    if args.command == "display":
        if args.hash:
            print(POOL_HASH)
        else:
            sys.stdout.write(DISPLAY_BLUEPRINT)

    elif args.command == "encode":
        bp = encode_auto(
            args.input_path, args.name, args.skip, args.fps,
            args.adaptive, args.threshold, args.deduplicate,
            args.width, args.height,
        )
        sys.stdout.write(bp)

    elif args.command == "frame":
        bp = encode_frame(args.image, args.name, args.fps,
                          total_width=args.width, total_height=args.height)
        sys.stdout.write(bp)


if __name__ == "__main__":
    main()
