"""Command Line Interface for factorio-display.

Provides subcommands to encode media, export the physical display grid,
export the audio decoder circuitry, and encode MIDI audio files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .audio.player_blueprint import build_audio_decoder, build_multi_rail_decoder
from .video.encoder import encode_auto
from .video.player_blueprint import build_display, build_display_logical
from .logical_blueprint import LogicalBlueprint, to_draftsman
from .composer import compose, PortConnection


# ── UTF-8 path support on Windows ────────────────────────────────────────

def _fix_argv_encoding() -> None:
    """On Windows, replace sys.argv with the true Unicode command line."""
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

            script = sys.argv[0]
            offset = 0
            script_base = _os.path.basename(script)
            for i, arg in enumerate(api_argv):
                if arg == script or _os.path.basename(arg) == script_base:
                    offset = i
                    break
            else:
                offset = len(api_argv) - len(sys.argv)

            if offset >= 0:
                sys.argv = api_argv[offset:]
    except Exception:
        pass


def _is_midi_file(path: str) -> bool:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return ext in ("mid", "midi")


_AUDIO_EXTENSIONS = {"wav", "flac", "ogg", "aiff", "aif", "au", "caf", "mp3", "mp4", "m4a", "aac", "wma"}


def _is_audio_file(path: str) -> bool:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return ext in _AUDIO_EXTENSIONS


def _resolve_display_dims(
    input_path: str,
    width: int | None = None,
    height: int | None = None,
) -> tuple[int, int]:
    """Probe source dimensions and call resolve_dimensions once."""
    from pathlib import Path

    from . import DISPLAY_HEIGHT, DISPLAY_WIDTH
    from .video.encoder import resolve_dimensions

    if width is not None and height is not None:
        return width, height

    path = Path(input_path)
    ext = path.suffix.lower()

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
    elif ext == ".gif":
        try:
            from PIL import Image
            gif = Image.open(str(path))
            source_w, source_h = gif.size
            return resolve_dimensions(source_w, source_h, width=width, height=height)
        except Exception:
            pass
    elif ext in {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif"}:
        import cv2
        from .video.encoder import _imread_utf8
        img = _imread_utf8(str(path))
        if img is not None:
            source_h, source_w = img.shape[:2]
            return resolve_dimensions(source_w, source_h, width=width, height=height)
    elif path.is_dir():
        pngs = sorted(path.glob("*.png"))
        if pngs:
            return _resolve_display_dims(str(pngs[0]), width=width, height=height)
    elif "*" in input_path or "?" in input_path:
        matches = sorted(Path().glob(input_path))
        if matches:
            return _resolve_display_dims(str(matches[0]), width=width, height=height)

    return DISPLAY_WIDTH, DISPLAY_HEIGHT


def _add_power_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--power", type=str, default="substation",
        choices=["small", "medium", "substation", "none"],
        help="Power supply type for all-in-one blueprint (legendary quality).",
    )


def _add_progress_bar_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--progress-bar", action="store_true", default=False,
        help="Attach a progress bar to the all-in-one blueprint.",
    )


def _add_cache_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cache", action=argparse.BooleanOptionalAction, default=True,
        help="Cache intermediate logical blueprints for resume support (default: on).",
    )


def _add_audio_midi_options(parser: argparse.ArgumentParser) -> None:
    g = parser.add_argument_group("MIDI translation")
    g.add_argument("--ticks-per-beat", type=int, default=30,
                   help="Game ticks per quarter note (default: 30)")
    g.add_argument("--boost-melody", type=float, default=1.0,
                   help="Melody velocity multiplier (default: 1.0 = off, 1.5 = 50%% boost)")
    g.add_argument("--velocity-scale", type=float, default=1.0,
                   help="Global velocity multiplier (default: 1.0)")
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


# ── Timer assembly helper ──────────────────────────────────────────────


def _debug_dump_toml(lb: LogicalBlueprint, step: str, debug_dir: str) -> None:
    """Save *lb* as a TOML file under *debug_dir* / ``{step}.toml``."""
    from pathlib import Path
    from .logical_blueprint import to_toml

    out_dir = Path(debug_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{step}.toml"
    path.write_text(to_toml(lb), encoding="utf-8")
    sys.stderr.write(f"  [debug] wrote {step}.toml\n")


def _extract_total_ticks(lb: LogicalBlueprint) -> int:
    """Find the max tick end from DC conditions in a logical blueprint."""
    _LE_OPS = frozenset({"<=", "\u2264"})
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


def _build_timer_for_memory(memory_lb: LogicalBlueprint) -> LogicalBlueprint:
    """Build a combined raw+mod timer suitable for a memory blueprint.

    The clock output colour is chosen to match the *memory_lb* clock
    input port colour (detected by :func:`_declare_memory_ports`):

    - **RED** (video memory): no bridge needed — the mod timer output
      (wrapping ``clock % N``) is exposed as both ``"clock"`` and
      ``"sub_tick"`` on RED.  The raw clock stays internal.
    - **GREEN** (audio memory): a clock bridge AC
      (``signal-clock + 0 → signal-clock``, RED→GREEN) copies the
      raw clock to GREEN for the memory DCs.  The mod timer output
      (sub_tick) stays on RED for the progress bar.

    Exposes two output ports:
    - ``"clock"`` — clock signal for memory DC gating
    - ``"sub_tick"`` (red) — sub-tick for progress bar
    """
    from .timer import build_raw_timer, build_mod_timer, build_clock_bridge
    from .composer import _assign_tile_positions, _connect_nets_by_color

    total_ticks = _extract_total_ticks(memory_lb)
    if total_ticks < 1:
        total_ticks = 60

    # Determine the clock port colour from the memory blueprint.
    clock_net_id = memory_lb.input_ports.get("clock")
    clock_color: str = "red"  # default (video memory)
    if clock_net_id is not None:
        for net in memory_lb.networks:
            if net.network_id == clock_net_id:
                clock_color = net.color
                break

    timer = build_raw_timer("Timer")
    # Raw timer outputs on RED.  Rename "out" → "raw" to avoid collision
    # with mod timer's "out" during the merge below.
    timer.output_ports["raw"] = timer.output_ports.pop("out")

    # Mod timer: reads RED clock, outputs sub_tick on RED.
    mod = build_mod_timer(total_ticks + 1, name="SubTick")
    _assign_tile_positions(mod, start_x=0, start_y=4)
    timer.merge(mod, entity_prefix="mod_", network_prefix="mod_")
    timer.output_ports["sub_tick"] = timer.output_ports.pop("out")

    if clock_color == "red":
        # Video memory — the modded (wrapping) clock drives everything on RED.
        # Wire raw timer (RED) → mod timer (RED input)
        _connect_nets_by_color(
            timer, "red",
            entity_contains="_inc", port="output",
            other_entity_contains="mod_sub", other_port="input",
        )
        # Both "clock" and "sub_tick" carry the modded (wrapping) clock.
        # The raw clock stays internal — only the raw AC and kick CC use it.
        timer.output_ports["clock"] = timer.output_ports["sub_tick"]
        # Drop the now-unused "raw" port
        del timer.output_ports["raw"]
    else:
        # Audio memory — need RED→GREEN clock bridge.
        bridge = build_clock_bridge("Clock Bridge")
        _assign_tile_positions(bridge, start_x=0, start_y=6)
        timer.merge(bridge, entity_prefix="bridge_", network_prefix="bridge_")

        # Wire raw timer (RED) → bridge (RED input)
        _connect_nets_by_color(
            timer, "red",
            entity_contains="_inc", port="output",
            other_entity_contains="bridge_clock", other_port="input",
        )
        # Wire raw timer (RED) → mod timer (RED input)
        _connect_nets_by_color(
            timer, "red",
            entity_contains="_inc", port="output",
            other_entity_contains="mod_sub", other_port="input",
        )
        # Bridge's "out" port is on GREEN — rename to "clock"
        timer.output_ports["clock"] = timer.output_ports.pop("out")

    timer.label = "Timer"
    return timer


def _declare_memory_ports(lb: LogicalBlueprint) -> None:
    """Declare ``clock`` and ``data`` ports on a memory LogicalBlueprint
    parsed from a draftsman string.

    The clock port colour is determined by inspecting which network the
    DCs' input side already belongs to (RED for video memory, GREEN for
    audio memory).  The data port is always RED (DC outputs carry colour
    data on the unified signal bus).

    When there are no networks (single-frame video with one DC and no
    wires), networks are created from the DC's endpoints directly.
    """
    from .logical_blueprint import Endpoint, Network

    dcs = [(eid, ent) for eid, ent in lb.entities.items()
           if ent.type == "decider-combinator"]
    if not dcs:
        return

    # ── Clock input port — detect actual colour from DC inputs ────
    clock_net_id: str | None = None
    clock_color: str = "red"  # default for video memory (all-red bus)
    for net in lb.networks:
        for ep in net.endpoints:
            if ep.port == "input":
                ent = lb.entities.get(ep.entity_id)
                if ent is not None and ent.type == "decider-combinator":
                    clock_net_id = net.network_id
                    clock_color = net.color
                    break
        if clock_net_id is not None:
            break

    if clock_net_id is None:
        # No network at all — create a clock network on the default colour
        dc_id = dcs[0][0]
        clock_net = Network(
            network_id=f"{clock_color}_clock",
            color=clock_color,
            endpoints=[Endpoint(dc_id, "input")],
        )
        lb.add_network(clock_net)
        clock_net_id = clock_net.network_id

    lb.set_input_port("clock", clock_net_id)

    # ── Data output port (red) ────────────────────────────────────
    data_net_id: str | None = None
    for net in lb.networks:
        if net.color != "red":
            continue
        for ep in net.endpoints:
            if ep.port == "output":
                ent = lb.entities.get(ep.entity_id)
                if ent is not None and ent.type == "decider-combinator":
                    data_net_id = net.network_id
                    break
        if data_net_id is not None:
            break

    if data_net_id is None:
        # No red network — create one from the first DC's output side
        dc_id = dcs[0][0]
        data_net = Network(
            network_id="red_data",
            color="red",
            endpoints=[Endpoint(dc_id, "output")],
        )
        lb.add_network(data_net)
        data_net_id = data_net.network_id

    lb.set_output_port("data", data_net_id)


# ── Main CLI ───────────────────────────────────────────────────────────

def main():  # pylint: disable=too-many-locals,too-many-statements
    """Parse CLI arguments and dispatch to the appropriate encoder/builder."""
    _fix_argv_encoding()
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
    encode_parser.add_argument("--fps", type=float, default=0.0,
                               help="Source frame rate (1-60). 0 = auto-detect.")
    encode_parser.add_argument("--adaptive", action="store_true",
                               help="Drop near-duplicate frames.")
    encode_parser.add_argument("--threshold", type=float, default=0.01,
                               help="Similarity cutoff for adaptive mode.")
    encode_parser.add_argument("--deduplicate", action="store_true",
                               help="Share one combinator across identical frames.")
    encode_parser.add_argument("--width", type=int, default=None,
                               help="Override display width (tiles).")
    encode_parser.add_argument("--height", type=int, default=None,
                               help="Override display height (tiles).")
    chunk_g = encode_parser.add_argument_group("Time-chunked generation")
    chunk_g.add_argument("--time-chunks", type=int, default=1,
                         help="Split video into N time slices for parallel encoding (default: 1 = off).")
    chunk_g.add_argument("--chunk-workers", type=int, default=None,
                         help="Max parallel worker processes (default: CPU count).")
    chunk_g.add_argument("--output-chunks", type=str, default=None,
                         help="Write individual chunk blueprints to DIR for inspection.")
    chunk_g.add_argument("--deduplicate-cross", action="store_true",
                         help="Deduplicate identical frames across time chunks during merge (slower).")

    encode_parser.add_argument("--no-audio", action="store_true",
                               help="Disable automatic audio track encoding.")
    encode_parser.add_argument("--no-attach-player", action="store_true",
                               help="Output audio memory pages only, without the player decoder attached.")
    encode_parser.add_argument("--rail-mode", type=str, default="piano",
                               help="Multi-rail mode: 'piano', 'all', 'auto[:threshold]', or comma-separated instruments.")
    encode_parser.add_argument("--instruments", type=str, default=None,
                               help="Deprecated alias for --rail-mode.")
    encode_parser.add_argument("--map-drums", action="store_true",
                               help="Map GM drum notes (24-81) to Factorio drum-kit sounds.")
    encode_parser.add_argument("--no-global-shift", action="store_true", default=False,
                               help="Disable optimal global octave shift.")
    _add_power_option(encode_parser)
    _add_progress_bar_option(encode_parser)
    _add_cache_option(encode_parser)
    encode_parser.add_argument(
        "--debug-toml", type=str, default=None, metavar="DIR",
        help="Dump each intermediate LogicalBlueprint as TOML to DIR for debugging.",
    )

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
    _add_power_option(display_parser)

    # ==================================================================
    # Subcommand: export-audio
    # ==================================================================
    audio_parser = subparsers.add_parser(
        "export-audio",
        help="Generate the audio decoder blueprint."
    )
    audio_parser.add_argument("--name", default="Audio Decoder", help="Blueprint name")
    audio_parser.add_argument("--instrument", default="piano",
                              help="Factorio instrument name (piano, bass, celesta, plucked, drum)")
    audio_parser.add_argument("--instruments", type=str, default=None,
                              help="Comma-separated instrument names for multi-rail (e.g. 'piano,bass').")
    audio_parser.add_argument("--format", type=str, choices=["blueprint", "logical"], default="blueprint",
                              help="Output format: 'blueprint' (draftsman string) or 'logical' (LLM-friendly TOML).")
    _add_power_option(audio_parser)

    # ==================================================================
    # Subcommand: encode-audio
    # ==================================================================
    encode_audio_parser = subparsers.add_parser(
        "encode-audio",
        help="Encode audio (.mid/.wav/.flac/.ogg/.mp3) into a Factorio audio memory blueprint."
    )
    encode_audio_parser.add_argument("input_path", help="Path to audio file")
    _add_audio_midi_options(encode_audio_parser)
    encode_audio_parser.add_argument("--no-attach-player", action="store_true",
                                     help="Output audio memory pages only, without the player decoder.")
    encode_audio_parser.add_argument("--rail-mode", type=str, default="piano",
                                     help="Multi-rail mode: 'piano', 'all', 'auto[:threshold]', or comma-separated instruments.")
    encode_audio_parser.add_argument("--instruments", type=str, default=None,
                                     help="Deprecated alias for --rail-mode.")
    encode_audio_parser.add_argument("--map-drums", action="store_true", default=True,
                                     help="Map GM drum notes (24-81) to Factorio drum-kit sounds.")
    encode_audio_parser.add_argument("--no-global-shift", action="store_true", default=False,
                                     help="Disable optimal global octave shift.")
    encode_audio_parser.add_argument("--format", type=str, choices=["blueprint", "logical"], default="blueprint",
                                     help="Output format: 'blueprint' (draftsman string) or 'logical' (LLM-friendly TOML).")
    _add_power_option(encode_audio_parser)
    _add_progress_bar_option(encode_audio_parser)
    _add_cache_option(encode_audio_parser)
    encode_audio_parser.add_argument(
        "--debug-toml", type=str, default=None, metavar="DIR",
        help="Dump each intermediate LogicalBlueprint as TOML to DIR for debugging.",
    )

    g5 = encode_audio_parser.add_argument_group("Audio file encoding (non-MIDI)")
    g5.add_argument("--output-midi", type=str, default=None)
    g5.add_argument("--activation-threshold", type=float, default=0.0)
    g5.add_argument("--midi-threshold", type=float, default=0.05)
    g5.add_argument("--no-condense", action="store_true")
    g5.add_argument("--max-polyphony", type=int, default=0)

    # ==================================================================
    # Subcommand: export-logical
    # ==================================================================
    export_logical_parser = subparsers.add_parser(
        "export-logical",
        help="Export the audio decoder as a logical blueprint (LLM-friendly TOML).",
    )
    export_logical_parser.add_argument("--name", default="Audio Decoder", help="Blueprint name")
    export_logical_parser.add_argument("--instrument", default="piano",
                                       help="Factorio instrument name")
    export_logical_parser.add_argument("-o", "--output", type=str, default=None,
                                       help="Write logical blueprint TOML to file instead of stdout.")

    args = parser.parse_args()

    from . import CLOCK_SIGNAL  # pylint: disable=import-outside-toplevel

    if args.command == "encode":
        power_type = getattr(args, "power", None)
        if power_type == "none":
            power_type = None
        use_progress = getattr(args, "progress_bar", False)
        use_cache = getattr(args, "cache", False)

        # ── Audio file routing ──────────────────────────────────────
        if _is_midi_file(args.input_path) or _is_audio_file(args.input_path):
            sys.stderr.write(f"Detected audio file, routing to audio encoder: {args.input_path}\n")
            from .audio.encoder import encode_audio_auto

            rail_mode = args.rail_mode
            if args.instruments:
                rail_mode = args.instruments

            midi_kwargs: dict[str, object] = {
                "attach_player": not args.no_attach_player,
                "map_drums": args.map_drums,
                "rail_mode": rail_mode,
                "use_global_shift": not args.no_global_shift,
            }

            if power_type is not None:
                midi_kwargs["attach_player"] = False
                audio_bp_str = encode_audio_auto(args.input_path, **midi_kwargs)
                if not audio_bp_str:
                    return
                # Convert to LogicalBlueprint for composition
                from draftsman.blueprintable import Blueprint
                from .logical_blueprint import from_draftsman

                debug_dir = getattr(args, "debug_toml", None)

                audio_lb = from_draftsman(Blueprint.from_string(audio_bp_str))
                audio_lb.label = f"Audio Memory: {args.name}"
                _declare_memory_ports(audio_lb)
                if debug_dir:
                    _debug_dump_toml(audio_lb, "01_audio_memory", debug_dir)

                components: list[LogicalBlueprint] = []
                connections: list[PortConnection] = []

                # Timer
                timer = _build_timer_for_memory(audio_lb)
                components.append(timer)
                if debug_dir:
                    _debug_dump_toml(timer, "02_timer", debug_dir)
                connections.append(PortConnection("Timer", "clock", audio_lb.label, "clock"))

                # Progress bar
                if use_progress:
                    total_ticks = _extract_total_ticks(audio_lb)
                    if total_ticks < 1:
                        total_ticks = 60
                    from .progress_bar import build_progress_bar
                    pb = build_progress_bar("Progress", length=10,
                                            signal_name="signal-clock", max_value=total_ticks)
                    components.append(pb)
                    if debug_dir:
                        _debug_dump_toml(pb, "03_progress", debug_dir)
                    connections.append(PortConnection("Timer", "sub_tick", "Progress", "in"))

                # Audio memory
                components.append(audio_lb)
                # Audio memory needs clock input, provides data output
                connections.append(PortConnection("Timer", "clock", audio_lb.label, "clock"))

                result = compose(
                    components=components,
                    connections=connections,
                    output_name=args.name,
                    pole_type=power_type,
                    use_cache=use_cache,
                    cache_key_parts=("audio", args.name),
                )
                if debug_dir:
                    _debug_dump_toml(result, "04_merged", debug_dir)
                sys.stdout.write(to_draftsman(result).to_string() + "\n")
            else:
                audio_bp = encode_audio_auto(args.input_path, **midi_kwargs)
                if audio_bp:
                    sys.stdout.write(audio_bp + "\n")
            return

        # ── Video routing ───────────────────────────────────────────
        sys.stderr.write(f"Encoding video data from {args.input_path}...\n")

        resolved_w, resolved_h = _resolve_display_dims(
            args.input_path, width=args.width, height=args.height,
        )

        video_bp_str = encode_auto(
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
            # ── All-in-one video composition ────────────────────
            from draftsman.blueprintable import Blueprint
            from .logical_blueprint import from_draftsman

            debug_dir = getattr(args, "debug_toml", None)

            video_lb = from_draftsman(Blueprint.from_string(video_bp_str))
            video_lb.label = f"Video Memory: {args.name}"
            _declare_memory_ports(video_lb)
            if debug_dir:
                _debug_dump_toml(video_lb, "01_video_memory", debug_dir)

            display_lb = build_display_logical(
                name="Display", width=resolved_w, height=resolved_h,
            )
            if debug_dir:
                _debug_dump_toml(display_lb, "02_display", debug_dir)

            components: list[LogicalBlueprint] = []
            connections: list[PortConnection] = []

            # Timer
            timer = _build_timer_for_memory(video_lb)
            components.append(timer)
            if debug_dir:
                _debug_dump_toml(timer, "03_timer", debug_dir)

            # Video memory
            components.append(video_lb)

            # Display
            components.append(display_lb)

            # Progress bar
            if use_progress:
                total_ticks = _extract_total_ticks(video_lb)
                if total_ticks < 1:
                    total_ticks = 60
                from .progress_bar import build_progress_bar
                pb = build_progress_bar("Progress", length=10,
                                        signal_name="signal-clock", max_value=total_ticks)
                components.append(pb)
                if debug_dir:
                    _debug_dump_toml(pb, "04_progress", debug_dir)

            # Connections
            connections.append(PortConnection("Timer", "clock", video_lb.label, "clock"))
            connections.append(PortConnection(video_lb.label, "data", "Display", "data"))
            if use_progress:
                connections.append(PortConnection("Timer", "sub_tick", "Progress", "in"))

            result = compose(
                components=components,
                connections=connections,
                output_name=args.name,
                pole_type=power_type,
                use_cache=use_cache,
                cache_key_parts=("video", args.name, f"{resolved_w}x{resolved_h}"),
            )
            if debug_dir:
                _debug_dump_toml(result, "05_merged", debug_dir)
            sys.stdout.write(to_draftsman(result).to_string() + "\n")
        else:
            sys.stdout.write(video_bp_str + "\n")

        # Hooked Audio Pipeline
        if not args.no_audio and _is_midi_file(args.input_path):
            sys.stderr.write("\n")
            from .audio.encoder import encode_audio_auto
            audio_bp = encode_audio_auto(args.input_path)
            if audio_bp:
                sys.stdout.write(audio_bp + "\n")

    elif args.command == "encode-audio":
        power_type = getattr(args, "power", None)
        if power_type == "none":
            power_type = None
        use_progress = getattr(args, "progress_bar", False)
        use_cache = getattr(args, "cache", False)

        from .audio.encoder import encode_audio_auto

        rail_mode = args.rail_mode
        if args.instruments:
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
            "output_midi": getattr(args, "output_midi", None),
            "activation_threshold": getattr(args, "activation_threshold", 0.0),
            "midi_activation_threshold": getattr(args, "midi_threshold", 0.05),
            "condense_midi": not getattr(args, "no_condense", False),
            "max_polyphony": getattr(args, "max_polyphony", 0),
        }

        if getattr(args, "format", "blueprint") == "logical" and _is_midi_file(args.input_path):
            from . import SIGNAL_POOL, QUALITIES
            from .logical_blueprint import to_toml
            from .audio.midi_translator import midi_to_tick_data
            from .audio.encoder import encode_audio_to_logical
            from .audio.player_blueprint import build_audio_decoder_logical
            import mido

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
                from draftsman.blueprintable import Blueprint
                from .logical_blueprint import from_draftsman

                debug_dir = getattr(args, "debug_toml", None)

                audio_lb = from_draftsman(Blueprint.from_string(audio_bp))
                audio_lb.label = f"Audio Memory: {args.name}"
                _declare_memory_ports(audio_lb)
                if debug_dir:
                    _debug_dump_toml(audio_lb, "01_audio_memory", debug_dir)

                components: list[LogicalBlueprint] = []
                connections: list[PortConnection] = []

                timer = _build_timer_for_memory(audio_lb)
                components.append(timer)
                if debug_dir:
                    _debug_dump_toml(timer, "02_timer", debug_dir)
                connections.append(PortConnection("Timer", "clock", audio_lb.label, "clock"))

                if use_progress:
                    total_ticks = _extract_total_ticks(audio_lb)
                    if total_ticks < 1:
                        total_ticks = 60
                    from .progress_bar import build_progress_bar
                    pb = build_progress_bar("Progress", length=10,
                                            signal_name="signal-clock", max_value=total_ticks)
                    components.append(pb)
                    if debug_dir:
                        _debug_dump_toml(pb, "03_progress", debug_dir)
                    connections.append(PortConnection("Timer", "sub_tick", "Progress", "in"))

                components.append(audio_lb)
                connections.append(PortConnection("Timer", "clock", audio_lb.label, "clock"))

                result = compose(
                    components=components,
                    connections=connections,
                    output_name=args.name,
                    pole_type=power_type,
                    use_cache=use_cache,
                    cache_key_parts=("audio", args.name),
                )
                if debug_dir:
                    _debug_dump_toml(result, "04_merged", debug_dir)
                output = to_draftsman(result).to_string() + "\n"

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
            from .audio.player_blueprint import build_audio_decoder_logical
            from .logical_blueprint import to_toml
            sys.stderr.write(
                f"Building logical audio decoder (Instrument: {instruments[0]})...\n"
            )
            lb = build_audio_decoder_logical(
                name=args.name,
                instrument=instruments[0],
                clock_signal=CLOCK_SIGNAL,
            )
            sys.stdout.write(to_toml(lb))
        else:
            sys.stderr.write(
                f"Building audio decoder blueprint (Instruments: {', '.join(instruments)})...\n"
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
            f"Building logical audio decoder (Instrument: {args.instrument})...\n"
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


if __name__ == "__main__":
    main()
