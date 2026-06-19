"""Command Line Interface for factorio-display.

Provides subcommands to encode media, export the physical display grid,
export the audio decoder circuitry, and encode MIDI audio files.
"""

import argparse
import sys
import contextlib
import os

from .audio.player_blueprint import build_audio_decoder
from .video.encoder import encode_auto
# Assuming you have a builder.py containing the logic to build the physical screen
from .video.player_blueprint import build_display


def _is_midi_file(path: str) -> bool:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return ext in ("mid", "midi")


def _add_audio_midi_options(parser: argparse.ArgumentParser) -> None:
    """Add MIDI→audio translation options to a subparser."""
    g = parser.add_argument_group("MIDI translation")
    g.add_argument("--ticks-per-beat", type=int, default=30,
                   help="Game ticks per quarter note (default: 30)")
    g.add_argument("--boost-melody", type=float, default=1.0,
                   help="Melody velocity multiplier (default: 1.0 = off, 1.5 = 50%% boost)")
    g.add_argument("--velocity-scale", type=float, default=1.0,
                   help="Global velocity multiplier (default: 1.0)")
    g2 = parser.add_argument_group("ADSR envelope")
    g2.add_argument("--attack-ticks", type=int, default=0,
                    help="ADSR attack duration in game ticks (default: 0 = off)")
    g2.add_argument("--decay-ticks", type=int, default=0,
                    help="ADSR decay duration in game ticks (default: 0 = off)")
    g2.add_argument("--sustain-level", type=float, default=1.0,
                    help="ADSR sustain level 0.0–1.0 (default: 1.0)")
    g2.add_argument("--release-ticks", type=int, default=0,
                    help="ADSR release duration in game ticks (default: 0 = off)")
    g3 = parser.add_argument_group("Debug / intermediate files")
    g3.add_argument("--debug-json", type=str, default=None,
                    help="Dump tick_data as JSON to PATH (development only)")
    g3.add_argument("--processed-midi", type=str, default=None,
                    help="Save octave-folded MIDI to PATH for preview")
    g4 = parser.add_argument_group("Output")
    g4.add_argument("-o", "--output", type=str, default=None,
                    help="Write blueprint to file instead of stdout") 

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
    encode_parser.add_argument("--no-round-units", action="store_true",
                               help="Disable auto-rounding dimensions to unit boundaries (28×28). "
                                    "By default, dimensions are rounded up to fill display units.")

    # Time-chunking options
    chunk_g = encode_parser.add_argument_group("Time-chunked generation")
    chunk_g.add_argument("--time-chunks", type=int, default=1,
                         help="Split video into N time slices for parallel encoding (default: 1 = off).")
    chunk_g.add_argument("--chunk-workers", type=int, default=None,
                         help="Max parallel worker processes (default: CPU count).")
    chunk_g.add_argument("--output-chunks", type=str, default=None,
                         help="Write individual chunk blueprints to DIR for inspection.")
    chunk_g.add_argument("--deduplicate-cross", action="store_true",
                         help="Deduplicate identical frames across time chunks during merge (slower).")

    # Audio-specific
    encode_parser.add_argument("--no-audio", action="store_true", help="Disable automatic audio track encoding.")

    display_parser = subparsers.add_parser(
        "export-display", 
        help="Generate the physical video display grid blueprint."
    )
    display_parser.add_argument("--name", default="Video Display", help="Blueprint name")
    display_parser.add_argument("--width", type=int, default=None, help="Display width in tiles.")
    display_parser.add_argument("--height", type=int, default=None, help="Display height in tiles.")

    audio_parser = subparsers.add_parser(
        "export-audio", 
        help="Generate the audio decoder blueprint."
    )
    audio_parser.add_argument("--name", default="Audio Decoder", help="Blueprint name")
    audio_parser.add_argument(
        "--instrument", 
        default="piano", 
        help="Factorio instrument name (piano, bass, celesta, plucked, drum)"
    )

    # ==================================================================
    # Subcommand: encode-audio (MIDI → audio memory blueprint)
    # ==================================================================
    encode_audio_parser = subparsers.add_parser(
        "encode-audio",
        help="Encode a .mid file into a Factorio audio memory blueprint."
    )
    encode_audio_parser.add_argument("input_path", help="Path to .mid file")
    _add_audio_midi_options(encode_audio_parser)

    args = parser.parse_args()

    from . import CLOCK_SIGNAL
    from .integer2signal.pool import get_filtered_pool

    if args.command == "encode":
        # If input is a .mid file, route to audio pipeline
        if _is_midi_file(args.input_path):
            sys.stderr.write(f"Detected MIDI file, routing to audio encoder: {args.input_path}\n")
            from .audio.encoder import encode_audio_auto

            midi_kwargs: dict[str, object] = {}
            # Only forward args that are set (not defaults we can't detect from 'encode' parser)
            # For 'encode' command with .mid, we don't have audio options — use defaults
            audio_bp = encode_audio_auto(args.input_path, **midi_kwargs)
            if audio_bp:
                sys.stdout.write(audio_bp + "\n")
            return

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
            round_units=not args.no_round_units,
            time_chunks=args.time_chunks,
            chunk_workers=args.chunk_workers,
            output_chunks_dir=args.output_chunks,
            deduplicate_cross=args.deduplicate_cross,
        )
        
        # Output ONLY the blueprint string
        sys.stdout.write(video_bp + "\n")

        # Hooked Audio Pipeline
        if not args.no_audio:
            sys.stderr.write("\n")
            from .audio.encoder import encode_audio_auto
            audio_bp = encode_audio_auto(args.input_path)
            if audio_bp:
                sys.stdout.write(audio_bp + "\n")

    elif args.command == "encode-audio":
        from .audio.encoder import encode_audio_auto

        midi_kwargs: dict[str, object] = {
            "ticks_per_beat": args.ticks_per_beat,
            "boost_melody": args.boost_melody,
            "velocity_scale": args.velocity_scale,
            "attack_ticks": args.attack_ticks,
            "decay_ticks": args.decay_ticks,
            "sustain_level": args.sustain_level,
            "release_ticks": args.release_ticks,
            "processed_midi_path": args.processed_midi,
            "debug_json_path": args.debug_json,
        }
        audio_bp = encode_audio_auto(args.input_path, **midi_kwargs)
        if audio_bp:
            if args.output:
                with open(args.output, "w") as f:
                    f.write(audio_bp + "\n")
                sys.stderr.write(f"Audio blueprint written to: {args.output}\n")
            else:
                sys.stdout.write(audio_bp + "\n")
            
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
            name=args.name,
            instrument=args.instrument,
            clock_signal=CLOCK_SIGNAL,
        )
        sys.stdout.write(audio_bp + "\n")

if __name__ == "__main__":
    main()
