"""Command Line Interface for factorio-display.

Provides subcommands to encode media, export the physical display grid,
and export the audio decoder circuitry.
"""

import argparse
import sys

from .build_audio_decoder import build_audio_decoder
from .encoder import encode_auto
# Assuming you have a builder.py containing the logic to build the physical screen
from .build_displayer_blueprint import build_display 

def main():
    parser = argparse.ArgumentParser(
        description="factorio-display: Build video displays and encode media into Factorio blueprints."
    )
    subparsers = parser.add_subparsers(dest="command", required=True, title="commands")

    # ==================================================================
    # Subcommand: encode
    # ==================================================================
    encode_parser = subparsers.add_parser(
        "encode", 
        help="Encode media (video/gif/images/audio) into Factorio memory blueprints."
    )
    encode_parser.add_argument("input_path", help="Path to input media file or directory")
    encode_parser.add_argument("--name", default="Animation Data", help="Base name of the blueprint")
    encode_parser.add_argument("--skip", type=int, default=1, help="Read every Nth frame")
    encode_parser.add_argument("--fps", type=float, default=0.0, help="Source frame rate (1–60). 0 = auto-detect.")
    encode_parser.add_argument("--adaptive", action="store_true", help="Drop near-duplicate frames.")
    encode_parser.add_argument("--threshold", type=float, default=0.03, help="Similarity cutoff for adaptive mode.")
    encode_parser.add_argument("--deduplicate", action="store_true", help="Share one combinator across identical frames.")
    encode_parser.add_argument("--width", type=int, default=None, help="Override display width (tiles).")
    encode_parser.add_argument("--height", type=int, default=None, help="Override display height (tiles).")
    
    # Audio-specific
    encode_parser.add_argument("--no-audio", action="store_true", help="Disable automatic audio track encoding.")

    # ==================================================================
    # Subcommand: export-display
    # ==================================================================
    display_parser = subparsers.add_parser(
        "export-display", 
        help="Generate the physical video display grid blueprint."
    )
    display_parser.add_argument("--name", default="Video Display", help="Blueprint name")
    display_parser.add_argument("--width", type=int, default=None, help="Display width in tiles.")
    display_parser.add_argument("--height", type=int, default=None, help="Display height in tiles.")

    # ==================================================================
    # Subcommand: export-audio
    # ==================================================================
    audio_parser = subparsers.add_parser(
        "export-audio", 
        help="Generate the audio decoder blueprint."
    )
    audio_parser.add_argument("--name", default="Audio Decoder", help="Blueprint name")
    audio_parser.add_argument(
        "--instrument", 
        default="programmable-speaker-instrument-piano", 
        help="Internal Factorio instrument name for the speaker"
    )
    audio_parser.add_argument(
        "--signals", 
        nargs="+", 
        default=["signal-A", "signal-B", "signal-C", "signal-D", "signal-E", "signal-F", "signal-G"], 
        help="List of signals for the audio decoder pool"
    )

    args = parser.parse_args()

    # ==================================================================
    # Command Routing
    # ==================================================================
    if args.command == "encode":
        sys.stderr.write(f"Encoding video data from {args.input_path}...\n")
        video_bp = encode_auto(
            args.input_path, 
            output_name=args.name, 
            fps_skip=args.skip, 
            fps=args.fps,
            adaptive=args.adaptive, 
            threshold=args.threshold, 
            deduplicate=args.deduplicate,
            total_width=args.width, 
            total_height=args.height,
        )
        
        # Output ONLY the blueprint string so `| Set-Clipboard` works perfectly.
        sys.stdout.write(video_bp + "\n")

        if not args.no_audio:
            pass # Hook audio pipeline here in the future
            
    elif args.command == "export-display":
        sys.stderr.write(f"Building display blueprint: {args.name}...\n")
        display_bp = build_display(
            name=args.name, 
            width=args.width, 
            height=args.height
        )
        sys.stdout.write(display_bp + "\n")
        
    elif args.command == "export-audio":
        sys.stderr.write(f"Building audio decoder blueprint (Instrument: {args.instrument})...\n")
        audio_bp = build_audio_decoder(
            signals=args.signals,
            instrument_name=args.instrument
        )
        sys.stdout.write(audio_bp + "\n")

if __name__ == "__main__":
    main()