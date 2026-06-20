"""Command Line Interface for factorio-display.

Provides subcommands to encode media, export the physical display grid,
export the audio decoder circuitry, and encode MIDI audio files.
"""

import argparse
import sys

from .audio.player_blueprint import build_audio_decoder, build_multi_rail_decoder
from .video.encoder import encode_auto
# Assuming you have a builder.py containing the logic to build the physical screen
from .video.player_blueprint import build_display


# ── UTF-8 path support on Windows ────────────────────────────────────────
# Python on Windows decodes sys.argv from the ANSI codepage, which
# corrupts characters outside the system locale (CJK, emoji, etc.).
# We recover the original UTF-16 command line via the Windows API.

def _fix_argv_encoding() -> None:
    """On Windows, replace sys.argv with the true Unicode command line.

    Python on Windows decodes ``sys.argv`` from the ANSI codepage, which
    corrupts characters that fall outside the system locale (CJK, emoji,
    etc.).  We use the Windows API to get the original UTF-16 arguments.

    For pip-installed wrapper scripts the Windows command line includes
    the Python interpreter and the wrapper exe *before* the user args
    (e.g. ``python.exe script.exe encode path``), while ``sys.argv``
    starts at the script.  We detect the offset by matching
    ``sys.argv[0]`` in the API result.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import os as _os
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)

        GetCommandLineW = kernel32.GetCommandLineW
        GetCommandLineW.restype = ctypes.c_wchar_p
        GetCommandLineW.argtypes = []

        CommandLineToArgvW = shell32.CommandLineToArgvW
        CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
        CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]

        cmd = GetCommandLineW()
        argc = ctypes.c_int()
        argv_ptr = CommandLineToArgvW(cmd, ctypes.byref(argc))

        if argv_ptr and argc.value > 0:
            api_argv = [argv_ptr[i] for i in range(argc.value)]
            kernel32.LocalFree(argv_ptr)

            # ── Align with sys.argv ──────────────────────────────────
            # sys.argv[0] is the script path; find it in the API result.
            script = sys.argv[0]
            offset = 0
            script_base = _os.path.basename(script)
            for i, arg in enumerate(api_argv):
                if arg == script or _os.path.basename(arg) == script_base:
                    offset = i
                    break
            else:
                # Fallback: assume leading elements are interpreter + wrapper.
                offset = len(api_argv) - len(sys.argv)

            if offset >= 0:
                sys.argv = api_argv[offset:]
    except Exception:
        pass  # Fall back to potentially corrupted sys.argv


def _is_midi_file(path: str) -> bool:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return ext in ("mid", "midi")


_AUDIO_EXTENSIONS = {"wav", "flac", "ogg", "aiff", "aif", "au", "caf", "mp3", "mp4", "m4a", "aac", "wma"}


def _is_audio_file(path: str) -> bool:
    """Check if a path has a non-MIDI audio file extension."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return ext in _AUDIO_EXTENSIONS


def _resolve_display_dims(
    input_path: str,
    width: int | None = None,
    height: int | None = None,
) -> tuple[int, int]:
    """Probe source dimensions and call :func:`resolve_dimensions` once.

    This is the **single source of truth** for the display size used by
    the memory bank, lamp grid, and all other sub-blueprints.  Call this
    before encoding so that every downstream component gets the same
    ``(resolved_w, resolved_h)``.

    Returns ``(resolved_w, resolved_h)``.
    """
    from pathlib import Path

    from . import DISPLAY_HEIGHT, DISPLAY_WIDTH
    from .video.encoder import resolve_dimensions

    # Both specified — no probing needed; trust the user
    if width is not None and height is not None:
        return width, height

    path = Path(input_path)
    ext = path.suffix.lower()

    # ── Video ──────────────────────────────────────────────────────
    if ext in {".mp4", ".avi", ".mov", ".mkv", ".webm"}:
        import cv2
        from .video.encoder import _videocap_utf8
        cap = _videocap_utf8(str(path))
        if cap.isOpened():
            source_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            source_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            cap.release()
            return resolve_dimensions(source_w, source_h, width=width, height=height)
        cap.release()

    # ── GIF ────────────────────────────────────────────────────────
    elif ext == ".gif":
        try:
            from PIL import Image
            gif = Image.open(str(path))
            source_w, source_h = gif.size
            return resolve_dimensions(source_w, source_h, width=width, height=height)
        except Exception:
            pass

    # ── Still image ────────────────────────────────────────────────
    elif ext in {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif"}:
        import cv2
        from .video.encoder import _imread_utf8
        img = _imread_utf8(str(path))
        if img is not None:
            source_h, source_w = img.shape[:2]
            return resolve_dimensions(source_w, source_h, width=width, height=height)

    # ── Directory / glob — probe the first match ───────────────────
    elif path.is_dir():
        pngs = sorted(path.glob("*.png"))
        if pngs:
            return _resolve_display_dims(str(pngs[0]), width=width, height=height)

    elif "*" in input_path or "?" in input_path:
        matches = sorted(Path().glob(input_path))
        if matches:
            return _resolve_display_dims(str(matches[0]), width=width, height=height)

    # ── Fallback — can't probe, use display defaults ───────────────
    return DISPLAY_WIDTH, DISPLAY_HEIGHT


def _add_power_option(parser: argparse.ArgumentParser) -> None:
    """Add --power option to a subparser."""
    parser.add_argument(
        "--power", type=str, default="substation",
        choices=["small", "medium", "substation", "none"],
        help="Power supply type for all-in-one blueprint (legendary quality).",
    )


def _add_progress_bar_option(parser: argparse.ArgumentParser) -> None:
    """Add --progress-bar option to a subparser."""
    parser.add_argument(
        "--progress-bar", action="store_true", default=False,
        help="Attach a progress bar to the all-in-one blueprint.",
    )


def _add_cache_option(parser: argparse.ArgumentParser) -> None:
    """Add --cache / --no-cache option to a subparser."""
    parser.add_argument(
        "--cache", action=argparse.BooleanOptionalAction, default=True,
        help="Cache intermediate logical blueprints for resume support (default: on).",
    )


def _add_audio_midi_options(parser: argparse.ArgumentParser) -> None:
    """Add MIDI→audio translation options to a subparser."""
    g = parser.add_argument_group("MIDI translation")
    g.add_argument(
        "--ticks-per-beat", type=int, default=30,
        help="Game ticks per quarter note (default: 30)",
    )
    g.add_argument(
        "--boost-melody", type=float, default=1.0,
        help="Melody velocity multiplier (default: 1.0 = off, 1.5 = 50%% boost)",
    )
    g.add_argument(
        "--velocity-scale", type=float, default=1.0,
        help="Global velocity multiplier (default: 1.0)",
    )
    g2 = parser.add_argument_group("ADSR envelope")
    g2.add_argument("--attack-ticks", type=int, default=10,
                    help="ADSR attack duration in game ticks (default: 10, 0 = off)")
    g2.add_argument("--decay-ticks", type=int, default=10,
                    help="ADSR decay duration in game ticks (default: 10, 0 = off)")
    g2.add_argument("--sustain-level", type=float, default=1.0,
                    help="ADSR sustain level 0.0~1.0 (default: 1.0)")
    g2.add_argument("--release-ticks", type=int, default=10,
                    help="ADSR release duration in game ticks (default: 10, 0 = off)")
    g2.add_argument("--attack-curve", type=float, default=1.0,
                    help="ADSR attack power-curve exp (>1=gentle, <1=snappy, default: 1.0=linear)")
    g2.add_argument("--decay-curve", type=float, default=1.0,
                    help="ADSR decay power-curve exp (>1=gentle, <1=snappy, default: 1.0=linear)")
    g2.add_argument("--release-curve", type=float, default=1.0,
                    help="ADSR release power-curve exp (>1=gentle, <1=snappy, default: 1.0=linear)")
    g3 = parser.add_argument_group("Debug / intermediate files")
    g3.add_argument("--debug-json", type=str, default=None,
                    help="Dump tick_data as JSON to PATH (development only)")
    g3.add_argument("--processed-midi", type=str, default=None,
                    help="Save octave-folded MIDI to PATH for preview")
    g4 = parser.add_argument_group("Output")
    g4.add_argument("-o", "--output", type=str, default=None,
                    help="Write blueprint to file instead of stdout")

def main():  # pylint: disable=too-many-locals,too-many-statements
    """Parse CLI arguments and dispatch to the appropriate encoder/builder."""
    _fix_argv_encoding()
    # Reconfigure stdio for UTF-8 so blueprint strings with Unicode print correctly.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

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
    encode_parser.add_argument(
        "--fps", type=float, default=0.0,
        help="Source frame rate (1-60). 0 = auto-detect.",
    )
    encode_parser.add_argument(
        "--adaptive", action="store_true",
        help="Drop near-duplicate frames.",
    )
    encode_parser.add_argument(
        "--threshold", type=float, default=0.01,
        help="Similarity cutoff for adaptive mode.",
    )
    encode_parser.add_argument(
        "--deduplicate", action="store_true",
        help="Share one combinator across identical frames.",
    )
    encode_parser.add_argument(
        "--width", type=int, default=None,
        help="Override display width (tiles).",
    )
    encode_parser.add_argument(
        "--height", type=int, default=None,
        help="Override display height (tiles).",
    )
    chunk_g = encode_parser.add_argument_group("Time-chunked generation")
    chunk_g.add_argument(
        "--time-chunks", type=int, default=1,
        help="Split video into N time slices for parallel encoding (default: 1 = off).",
    )
    chunk_g.add_argument(
        "--chunk-workers", type=int, default=None,
        help="Max parallel worker processes (default: CPU count).",
    )
    chunk_g.add_argument(
        "--output-chunks", type=str, default=None,
        help="Write individual chunk blueprints to DIR for inspection.",
    )
    chunk_g.add_argument(
        "--deduplicate-cross", action="store_true",
        help="Deduplicate identical frames across time chunks during merge (slower).",
    )

    # Audio-specific
    encode_parser.add_argument(
        "--no-audio", action="store_true",
        help="Disable automatic audio track encoding.",
    )
    encode_parser.add_argument(
        "--no-attach-player", action="store_true",
        help="Output audio memory pages only, without the player decoder attached.",
    )
    encode_parser.add_argument(
        "--rail-mode", type=str, default="piano",
        help=(
            "Multi-rail mode: 'piano' (default, single piano rail), "
            "'all' (use all detected instruments), "
            "'auto' or 'auto:0.05' (auto-detect, drop rails below threshold), "
            "or comma-separated instruments like 'piano,bass,drum'."
        ),
    )
    encode_parser.add_argument(
        "--instruments", type=str, default=None,
        help="Deprecated alias for --rail-mode.",
    )
    encode_parser.add_argument(
        "--map-drums", action="store_true",
        help="Map GM drum notes (24-81) to Factorio drum-kit sounds instead of octave folding.",
    )
    encode_parser.add_argument(
        "--no-global-shift", action="store_true", default=False,
        help="Disable optimal global octave shift; use only per-note octave folding.",
    )
    _add_power_option(encode_parser)
    _add_progress_bar_option(encode_parser)
    _add_cache_option(encode_parser)

    display_parser = subparsers.add_parser(
        "export-display",
        help="Generate the physical video display grid blueprint."
    )
    display_parser.add_argument("--name", default="Video Display", help="Blueprint name")
    display_parser.add_argument("--width", type=int, default=None, help="Display width in tiles.")
    display_parser.add_argument("--height", type=int, default=None, help="Display height in tiles.")
    _add_power_option(display_parser)

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
    audio_parser.add_argument(
        "--instruments", type=str, default=None,
        help="Comma-separated instrument names for multi-rail (e.g. 'piano,bass').",
    )
    audio_parser.add_argument(
        "--format", type=str, choices=["blueprint", "logical"], default="blueprint",
        help="Output format: 'blueprint' (draftsman string) or 'logical' (LLM-friendly TOML).",
    )
    _add_power_option(audio_parser)

    # ==================================================================
    # Subcommand: encode-audio (MIDI → audio memory blueprint)
    # ==================================================================
    encode_audio_parser = subparsers.add_parser(
        "encode-audio",
        help="Encode audio (.mid/.wav/.flac/.ogg/.mp3) into a Factorio audio memory blueprint."
    )
    encode_audio_parser.add_argument(
        "input_path",
        help="Path to audio file (.mid, .midi, .wav, .flac, .ogg, .mp3, etc.)",
    )
    _add_audio_midi_options(encode_audio_parser)
    encode_audio_parser.add_argument(
        "--no-attach-player", action="store_true",
        help="Output audio memory pages only, without the player decoder.",
    )
    encode_audio_parser.add_argument(
        "--rail-mode", type=str, default="piano",
        help=(
            "Multi-rail mode: 'piano' (default), 'all', 'auto[:threshold]', "
            "or comma-separated instruments."
        ),
    )
    encode_audio_parser.add_argument(
        "--instruments", type=str, default=None,
        help="Deprecated alias for --rail-mode.",
    )
    encode_audio_parser.add_argument(
        "--map-drums", action="store_true", default=True, # Filter drum notes by default to prevent excessive volume stacking
        help="Map GM drum notes (24-81) to Factorio drum-kit sounds.",
    )
    encode_audio_parser.add_argument(
        "--no-global-shift", action="store_true", default=False,
        help="Disable optimal global octave shift; use only per-note octave folding.",
    )
    encode_audio_parser.add_argument(
        "--format", type=str, choices=["blueprint", "logical"], default="blueprint",
        help="Output format: 'blueprint' (draftsman string) or 'logical' (LLM-friendly TOML).",
    )
    _add_power_option(encode_audio_parser)
    _add_progress_bar_option(encode_audio_parser)
    _add_cache_option(encode_audio_parser)

    # Audio-file-specific options (WAV/FLAC/OGG/MP3)
    g5 = encode_audio_parser.add_argument_group("Audio file encoding (non-MIDI)")
    g5.add_argument(
        "--output-midi", type=str, default=None,
        help="Export a .mid file from the encoded audio (before octave folding).",
    )
    g5.add_argument(
        "--activation-threshold", type=float, default=0.0,
        help="STFT magnitude threshold 0.0–1.0 (default: 0.0 = off).",
    )
    g5.add_argument(
        "--midi-threshold", type=float, default=0.05,
        help="MIDI note activation threshold 0.0–1.0 (default: 0.05).",
    )
    g5.add_argument(
        "--no-condense", action="store_true",
        help="Don't condense contiguous MIDI notes.",
    )
    g5.add_argument(
        "--max-polyphony", type=int, default=0,
        help="Max simultaneous MIDI notes per tick (0 = unlimited).",
    )

    # ==================================================================
    # Subcommand: export-logical
    # ==================================================================
    export_logical_parser = subparsers.add_parser(
        "export-logical",
        help="Export the audio decoder as a logical blueprint (LLM-friendly TOML).",
    )
    export_logical_parser.add_argument("--name", default="Audio Decoder", help="Blueprint name")
    export_logical_parser.add_argument(
        "--instrument", default="piano",
        help="Factorio instrument name (piano, bass, celesta, plucked, drum)",
    )
    export_logical_parser.add_argument(
        "-o", "--output", type=str, default=None,
        help="Write logical blueprint TOML to file instead of stdout.",
    )

    args = parser.parse_args()

    from . import CLOCK_SIGNAL  # pylint: disable=import-outside-toplevel

    if args.command == "encode":
        power_type = getattr(args, "power", None)
        if power_type == "none":
            power_type = None
        use_progress = getattr(args, "progress_bar", False)
        use_cache = getattr(args, "cache", False)

        # If input is an audio file, route to audio pipeline
        if _is_midi_file(args.input_path) or _is_audio_file(args.input_path):
            sys.stderr.write(f"Detected audio file, routing to audio encoder: {args.input_path}\n")
            from .audio.encoder import encode_audio_auto

            rail_mode = args.rail_mode
            if args.instruments:  # deprecated alias
                rail_mode = args.instruments

            midi_kwargs: dict[str, object] = {
                "attach_player": not args.no_attach_player,
                "map_drums": args.map_drums,
                "rail_mode": rail_mode,
                "use_global_shift": not args.no_global_shift,
            }

            if power_type is not None:
                # All-in-one audio mode: encode → compose with timer + progress + power.
                # Don't attach player — composer handles layout separately.
                midi_kwargs["attach_player"] = False
                audio_bp_str = encode_audio_auto(args.input_path, **midi_kwargs)
                if not audio_bp_str:
                    return
                result_bp = _compose_audio_all_in_one(
                    audio_bp_str, args.name, power_type, use_progress, use_cache,
                )
                sys.stdout.write(result_bp + "\n")
            else:
                audio_bp = encode_audio_auto(args.input_path, **midi_kwargs)
                if audio_bp:
                    sys.stdout.write(audio_bp + "\n")
            return

        sys.stderr.write(f"Encoding video data from {args.input_path}...\n")

        # ── Resolve display dimensions ONCE — single source of truth ──
        resolved_w, resolved_h = _resolve_display_dims(
            args.input_path, width=args.width, height=args.height,
        )

        video_bp = encode_auto(
            args.input_path,
            output_name=args.name,
            fps_skip=args.skip,
            fps=args.fps,
            adaptive=args.adaptive,
            threshold=args.threshold,
            deduplicate=args.deduplicate,
            total_width=resolved_w,
            total_height=resolved_h,
            time_chunks=args.time_chunks,
            chunk_workers=args.chunk_workers,
            output_chunks_dir=args.output_chunks,
            deduplicate_cross=args.deduplicate_cross,
        )

        if power_type is not None:
            # All-in-one video mode
            result_bp = _compose_video_all_in_one(
                video_bp, args.name, power_type, use_progress, use_cache,
                width=resolved_w, height=resolved_h,
            )
            sys.stdout.write(result_bp + "\n")
        else:
            # Output ONLY the blueprint string
            sys.stdout.write(video_bp + "\n")

        # Hooked Audio Pipeline
        if not args.no_audio and _is_midi_file(args.input_path):
            sys.stderr.write("\n")
            from .audio.encoder import encode_audio_auto  # pylint: disable=import-outside-toplevel
            audio_bp = encode_audio_auto(args.input_path)
            if audio_bp:
                sys.stdout.write(audio_bp + "\n")

    elif args.command == "encode-audio":
        power_type = getattr(args, "power", None)
        if power_type == "none":
            power_type = None
        use_progress = getattr(args, "progress_bar", False)
        use_cache = getattr(args, "cache", False)

        from .audio.encoder import encode_audio_auto  # pylint: disable=import-outside-toplevel

        rail_mode = args.rail_mode
        if args.instruments:  # deprecated alias
            rail_mode = args.instruments

        midi_kwargs: dict[str, object] = {
            "ticks_per_beat": args.ticks_per_beat,
            "boost_melody": args.boost_melody,
            "velocity_scale": args.velocity_scale,
            "attack_ticks": args.attack_ticks,
            "decay_ticks": args.decay_ticks,
            "sustain_level": args.sustain_level,
            "release_ticks": args.release_ticks,
            "attack_curve": args.attack_curve,
            "decay_curve": args.decay_curve,
            "release_curve": args.release_curve,
            "processed_midi_path": args.processed_midi,
            "debug_json_path": args.debug_json,
            "attach_player": not args.no_attach_player,
            "map_drums": args.map_drums,
            "rail_mode": rail_mode,
            "use_global_shift": not args.no_global_shift,
            # Audio-file encoding kwargs
            "output_midi": getattr(args, "output_midi", None),
            "activation_threshold": getattr(args, "activation_threshold", 0.0),
            "midi_activation_threshold": getattr(args, "midi_threshold", 0.05),
            "condense_midi": not getattr(args, "no_condense", False),
            "max_polyphony": getattr(args, "max_polyphony", 0),
        }

        if getattr(args, "format", "blueprint") == "logical" and _is_midi_file(args.input_path):
            # Encode to logical TOML (MIDI only)
            from . import SIGNAL_POOL, QUALITIES  # pylint: disable=import-outside-toplevel
            from .logical_blueprint import to_toml  # pylint: disable=import-outside-toplevel
            from .audio.midi_translator import midi_to_tick_data  # pylint: disable=import-outside-toplevel
            from .audio.encoder import encode_audio_to_logical  # pylint: disable=import-outside-toplevel
            from .audio.player_blueprint import build_audio_decoder_logical  # pylint: disable=import-outside-toplevel
            import mido  # pylint: disable=import-outside-toplevel

            mid = mido.MidiFile(args.input_path)
            td_kwargs = {k: v for k, v in midi_kwargs.items()
                         if k in ("ticks_per_beat", "boost_melody", "velocity_scale",
                                  "attack_ticks", "decay_ticks", "sustain_level",
                                  "release_ticks", "attack_curve", "decay_curve",
                                  "release_curve")}
            tick_data = midi_to_tick_data(mid, **td_kwargs)  # type: ignore[arg-type]
            lb = encode_audio_to_logical(
                tick_data, args.name, SIGNAL_POOL, QUALITIES, clock_signal=CLOCK_SIGNAL,
            )
            if not args.no_attach_player:
                player_lb = build_audio_decoder_logical(
                    name=f"Player: {args.name}",
                    instrument=rail_mode.split(",")[0].strip(),
                    clock_signal=CLOCK_SIGNAL,
                )
                # Merge: add player entities and networks to the memory LB
                for ent in player_lb.entities.values():
                    if ent.entity_id not in lb.entities:
                        lb.add_entity(ent)
                for net in player_lb.networks:
                    lb.networks.append(net)
            output = to_toml(lb)
        else:
            audio_bp = encode_audio_auto(args.input_path, **midi_kwargs)
            output = audio_bp + "\n"

            if power_type is not None and output.strip():
                output = _compose_audio_all_in_one(
                    audio_bp, args.name, power_type, use_progress, use_cache,
                )

        if output:
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    f.write(output)
                sys.stderr.write(f"Blueprint written to: {args.output}\n")
            else:
                sys.stdout.write(output)

    elif args.command == "export-display":
        sys.stderr.write(f"Building display blueprint: {args.name}...\n")
        display_bp = build_display(
            name=args.name,
            width=args.width,
            height=args.height
        )
        sys.stdout.write(display_bp + "\n")

    elif args.command == "export-audio":
        instruments: list[str]
        if args.instruments:
            instruments = [s.strip() for s in args.instruments.split(",") if s.strip()]
        else:
            instruments = [args.instrument]

        if getattr(args, "format", "blueprint") == "logical":
            # Single-rail logical export
            from .audio.player_blueprint import build_audio_decoder_logical
            from .logical_blueprint import to_toml
            sys.stderr.write(
                f"Building logical audio decoder "
                f"(Instrument: {instruments[0]})...\n"
            )
            lb = build_audio_decoder_logical(
                name=args.name,
                instrument=instruments[0],
                clock_signal=CLOCK_SIGNAL,
            )
            sys.stdout.write(to_toml(lb))
        else:
            sys.stderr.write(
                f"Building audio decoder blueprint "
                f"(Instruments: {', '.join(instruments)})...\n"
            )
            audio_bp = build_multi_rail_decoder(
                name=args.name,
                instruments=instruments,
                clock_signal=CLOCK_SIGNAL,
            )
            sys.stdout.write(audio_bp + "\n")

    elif args.command == "export-logical":
        from .audio.player_blueprint import build_audio_decoder_logical
        from .logical_blueprint import to_toml
        sys.stderr.write(
            f"Building logical audio decoder "
            f"(Instrument: {args.instrument})...\n"
        )
        lb = build_audio_decoder_logical(
            name=args.name,
            instrument=args.instrument,
            clock_signal=CLOCK_SIGNAL,
        )
        toml_str = to_toml(lb)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(toml_str)
            sys.stderr.write(f"Logical blueprint written to: {args.output}\n")
        else:
            sys.stdout.write(toml_str)


# ── All-in-one composition helpers ──────────────────────────────────────


def _extract_total_ticks(lb: "LogicalBlueprint") -> int:
    """Find the max tick end from DC conditions in a logical blueprint.

    Scans all decider combinators for ``clock >= start AND clock <= end``
    conditions and returns the highest *end* value (or 0 if none found).

    Handles both ASCII (``<=``) and Unicode (``≤``) comparators produced
    by different draftsman versions / parse paths.
    """
    _LE_OPS = frozenset({"<=", "\u2264"})  # <= and ≤
    max_end = 0
    for ent in lb.entities.values():
        if ent.type != "decider-combinator":
            continue
        for cond in ent.properties.get("conditions", []):
            if cond.get("op") in _LE_OPS and cond.get("first", "").startswith("signal-clock"):
                val = cond.get("constant", 0)
                if val > max_end:
                    max_end = val
    return max_end


def _compose_audio_all_in_one(
    audio_bp_str: str,
    name: str,
    power_type: str,
    use_progress: bool,
    use_cache: bool,
) -> str:
    """Wrap an audio-only memory blueprint into an all-in-one."""
    from draftsman.blueprintable import Blueprint  # pylint: disable=import-outside-toplevel
    from .logical_blueprint import from_draftsman, to_draftsman  # pylint: disable=import-outside-toplevel
    from .composer import compose_all_in_one, _assign_tile_positions, _connect_nets_by_color  # pylint: disable=import-outside-toplevel
    from .timer import build_raw_timer, build_mod_timer  # pylint: disable=import-outside-toplevel
    from .progress_bar import build_progress_bar  # pylint: disable=import-outside-toplevel

    sys.stderr.write("Composing all-in-one audio blueprint...\n")
    bp = Blueprint.from_string(audio_bp_str)
    audio_lb = from_draftsman(bp)

    # Extract total tick count for mod interval
    total_ticks = _extract_total_ticks(audio_lb)
    if total_ticks < 1:
        total_ticks = 60  # fallback

    # Timer assembly: raw (RED self-loop) → mod (RED→RED signal-clock)
    timer = build_raw_timer("Clock")
    mod = build_mod_timer(total_ticks + 1, name="SubTick")
    _assign_tile_positions(mod, start_x=0, start_y=4)
    timer.merge(mod, entity_prefix="mod_", network_prefix="mod_")
    _connect_nets_by_color(
        timer, "red",
        entity_contains="clock_", port="output",
        other_entity_contains="mod_sub", other_port="input",
    )

    progress = None
    if use_progress:
        progress = build_progress_bar(
            "Progress", length=10, signal_name="signal-clock",
            max_value=total_ticks,
        )

    result = compose_all_in_one(
        audio_memory_lb=audio_lb,
        timer_lb=timer,
        progress_bar_lb=progress,
        pole_type=power_type,
        output_name=name,
        use_cache=use_cache,
        cache_key_parts=("audio", name),
    )
    bp_out = to_draftsman(result)
    return bp_out.to_string()


def _compose_video_all_in_one(
    video_bp_str: str,
    name: str,
    power_type: str,
    use_progress: bool,
    use_cache: bool,
    width: int | None = None,
    height: int | None = None,
) -> str:
    """Wrap a video memory blueprint into an all-in-one."""
    from draftsman.blueprintable import Blueprint  # pylint: disable=import-outside-toplevel
    from . import DISPLAY_WIDTH, DISPLAY_HEIGHT  # pylint: disable=import-outside-toplevel
    from .logical_blueprint import from_draftsman, to_draftsman  # pylint: disable=import-outside-toplevel
    from .composer import compose_all_in_one, _assign_tile_positions, _connect_nets_by_color  # pylint: disable=import-outside-toplevel
    from .timer import build_raw_timer, build_mod_timer  # pylint: disable=import-outside-toplevel
    from .progress_bar import build_progress_bar  # pylint: disable=import-outside-toplevel
    from .video.player_blueprint import build_display  # pylint: disable=import-outside-toplevel

    sys.stderr.write("Composing all-in-one video blueprint...\n")
    w = width if width is not None else DISPLAY_WIDTH
    h = height if height is not None else DISPLAY_HEIGHT

    bp = Blueprint.from_string(video_bp_str)
    video_lb = from_draftsman(bp)

    # Extract total tick count from the encoded DCs to use as mod interval
    total_ticks = _extract_total_ticks(video_lb)
    if total_ticks < 1:
        total_ticks = 60  # fallback

    display_bp_str = build_display(name="Display", width=w, height=h)
    display_bp = Blueprint.from_string(display_bp_str)
    display_lb = from_draftsman(display_bp)

    # Timer assembly: raw (RED self-loop) → mod (RED→RED signal-clock)
    timer = build_raw_timer("Clock")
    mod = build_mod_timer(total_ticks + 1, name="SubTick")
    _assign_tile_positions(mod, start_x=0, start_y=4)
    timer.merge(mod, entity_prefix="mod_", network_prefix="mod_")
    # Wire: raw output (RED) → mod input (RED)
    _connect_nets_by_color(
        timer, "red",
        entity_contains="clock_", port="output",
        other_entity_contains="mod_sub", other_port="input",
    )

    progress = None
    if use_progress:
        progress = build_progress_bar(
            "Progress", length=10, signal_name="signal-clock",
            max_value=total_ticks,
        )

    result = compose_all_in_one(
        display_lb=display_lb,
        video_memory_lb=video_lb,
        timer_lb=timer,
        progress_bar_lb=progress,
        pole_type=power_type,
        output_name=name,
        use_cache=use_cache,
        cache_key_parts=("video", name, f"{w}x{h}"),
    )
    bp_out = to_draftsman(result)
    return bp_out.to_string()


if __name__ == "__main__":
    main()
