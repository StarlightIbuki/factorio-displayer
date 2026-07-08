"""Command Line Interface for factorio-display.

Provides subcommands to encode media, export the physical display grid,
export the audio decoder circuitry, and encode MIDI audio files.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from .audio.player_blueprint import build_multi_rail_decoder
from .video.encoder import encode_auto, _to_fixed_string
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


def _read_text_auto(path: Path) -> str:
    """Read text using common encodings produced by shells/editors."""
    data = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    # Last resort for mixed legacy files.
    return data.decode("latin-1")


def _is_midi_file(path: str) -> bool:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return ext in ("mid", "midi")


_AUDIO_EXTENSIONS = {".wav", ".flac", ".ogg", ".aiff", ".aif", ".au", ".caf", ".mp3", ".mp4", ".m4a", ".aac", ".wma"}

_VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif"}


def _is_video_file(path: str) -> bool:
    ext = Path(path).suffix.lower()
    return ext in _VIDEO_EXTENSIONS


def _is_audio_file(path: str) -> bool:
    ext = Path(path).suffix.lower()
    return ext in _AUDIO_EXTENSIONS


def _classify_input(path: str) -> str:
    """Return 'video', 'audio', 'midi', 'image', or 'unknown'."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in _VIDEO_EXTENSIONS:
        return "video"
    if ext in _AUDIO_EXTENSIONS:
        return "audio"
    if ext in (".mid", ".midi"):
        return "midi"
    if ext in _IMAGE_EXTENSIONS or ext == ".gif":
        return "image"
    if p.is_dir():
        return "image"
    if "*" in path or "?" in path:
        return "image"
    return "unknown"


def _extract_audio_from_video(video_path: str, output_wav: str) -> bool:
    """Extract audio track from video using ffmpeg.

    Returns True on success, False if ffmpeg is unavailable or the
    video has no audio track.
    """
    import subprocess
    import shutil

    if shutil.which("ffmpeg") is None:
        sys.stderr.write(f"  [audio] ffmpeg not found — skipping audio extraction from {Path(video_path).name}\n")
        return False

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", video_path,
                "-vn", "-acodec", "pcm_s16le",
                "-ar", "44100", "-ac", "1",
                output_wav,
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            sys.stderr.write(f"  [audio] ffmpeg failed for {Path(video_path).name} (maybe no audio track)\n")
            return False
        # Check if the output file actually has audio content
        if Path(output_wav).stat().st_size < 100:
            Path(output_wav).unlink(missing_ok=True)
            return False
        return True
    except FileNotFoundError:
        sys.stderr.write(f"  [audio] ffmpeg not found — skipping audio extraction\n")
        return False
    except Exception as e:
        sys.stderr.write(f"  [audio] ffmpeg error: {e}\n")
        return False


def _get_video_tick_count(video_path: str, fps: float, skip: int) -> int:
    """Get total tick count for a video at the given fps and frame skip."""
    import cv2
    from .video.encoder import _videocap_utf8

    cap = _videocap_utf8(video_path)
    if not cap.isOpened():
        cap.release()
        return 0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    if total_frames <= 0:
        return 0
    effective_fps = max(1.0, min(fps if fps > 0 else 60.0, 60.0))
    ticks_per_frame = 60.0 / effective_fps
    effective_frames = (total_frames + skip - 1) // skip
    return max(1, int(effective_frames * ticks_per_frame))


def _videos_have_audio(video_paths: list[str]) -> bool:
    """Quick check: does any video have an audio stream?"""
    import subprocess
    import shutil

    if shutil.which("ffprobe") is None:
        # Fall back to ffmpeg
        if shutil.which("ffmpeg") is None:
            return False
        probe_cmd = ["ffmpeg"]
    else:
        probe_cmd = ["ffprobe"]

    for vp in video_paths:
        try:
            result = subprocess.run(
                [*probe_cmd, "-i", vp, "-show_streams", "-select_streams", "a",
                 "-loglevel", "error"],
                capture_output=True, text=True,
            )
            if "codec_type=audio" in result.stdout or "Audio:" in result.stderr:
                return True
        except Exception:
            pass
    return False


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
            from ._unicode_io import image_open  # pylint: disable=import-outside-toplevel
            gif = image_open(str(path))
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
    _END_OPS = frozenset({"<=", "\u2264", "="})
    max_end = 0
    for ent in lb.entities.values():
        if ent.type != "decider-combinator":
            continue
        for cond in ent.properties.get("conditions", []):
            if cond.get("op") in _END_OPS and cond.get("first", "").startswith("signal-clock"):
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
    from .composer import _connect_nets_by_color

    # _extract_total_ticks returns the largest tick index referenced by
    # memory conditions. A single-frame image typically uses only tick 0,
    # which must produce modulo interval 1 (always-on), not 2.
    max_tick_index = _extract_total_ticks(memory_lb)
    if max_tick_index < 0:
        max_tick_index = 0

    # Determine the clock port colour from the memory blueprint.
    clock_net_id = memory_lb.input_ports.get("clock")
    clock_color: str = "red"  # default (video memory)
    if clock_net_id is not None:
        for net in memory_lb.networks:
            if net.network_id == clock_net_id:
                clock_color = net.color
                break

    timer = build_raw_timer("Timer", with_kick=False)
    # Raw timer outputs on RED.  Rename "out" → "raw" to avoid collision
    # with mod timer's "out" during the merge below.
    timer.output_ports["raw"] = timer.output_ports.pop("out")

    # Mod timer: reads RED clock, outputs sub_tick on RED.
    mod = build_mod_timer(max_tick_index + 1, name="SubTick")
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


def _connect_data_ports(
    connections: list[PortConnection],
    video_lb: LogicalBlueprint,
    display_lb: LogicalBlueprint,
) -> None:
    """Add PortConnection(s) from video memory data port(s) to display data port(s).

    For single-chunk displays both sides have a single ``"data"`` port.
    For chunked displays the ports are named ``"data_0"``, ``"data_1"``, …
    and are matched in sorted order.
    """
    video_ports = sorted(
        [p for p in video_lb.output_ports if p == "data" or p.startswith("data_")],
    )
    display_ports = sorted(
        [p for p in display_lb.input_ports if p == "data" or p.startswith("data_")],
    )
    for vp, dp in zip(video_ports, display_ports):
        connections.append(PortConnection(video_lb.label, vp, display_lb.label, dp))


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
        # No network at all — create a clock network containing
        # ALL DC inputs (not just the first one).
        clock_net = Network(
            network_id=f"{clock_color}_clock",
            color=clock_color,
            endpoints={Endpoint(dc_id, "input") for dc_id, _ in dcs},
        )
        lb.add_network(clock_net)
        clock_net_id = clock_net.network_id

    lb.set_input_port("clock", clock_net_id)

    # ── Data output port(s) (red) — one per isolated red network ──
    # Chunked memory has multiple disconnected red networks, each
    # carrying one chunk's pixel data.  Collect all red networks
    # that have DC output endpoints and sort by entity Y-position
    # so chunk indices match the display's port naming.
    red_data_nets: list[tuple[str, float]] = []  # (net_id, min_y)
    for net in lb.networks:
        if net.color != "red":
            continue
        min_y = float("inf")
        for ep in net.endpoints:
            if ep.port != "output":
                continue
            ent = lb.entities.get(ep.entity_id)
            if ent is not None and ent.type == "decider-combinator":
                pos = ent.position
                if pos is not None:
                    min_y = min(min_y, pos[1])
        if min_y != float("inf"):
            red_data_nets.append((net.network_id, min_y))

    if not red_data_nets:
        # No red network at all — create one isolated network per
        # chunk's DC output, grouped by Y-position so chunk indices
        # match the display's port naming.
        # Collect DCs with their output endpoints and Y-positions.
        dc_outputs: list[tuple[str, float]] = []
        for dc_id, ent in dcs:
            pos = ent.position
            if pos is not None:
                dc_outputs.append((dc_id, float(pos[1])))
        dc_outputs.sort(key=lambda x: x[1])
        # Assign each DC to its own isolated red output network
        for i, (dc_id, _) in enumerate(dc_outputs):
            net_id = f"red_data_{i}"
            lb.add_network(Network(
                network_id=net_id,
                color="red",
                endpoints={Endpoint(dc_id, "output")},
            ))
            red_data_nets.append((net_id, float(i)))

    # Sort by Y-position so chunk 0 = topmost
    red_data_nets.sort(key=lambda item: item[1])

    if len(red_data_nets) == 1:
        lb.set_output_port("data", red_data_nets[0][0])
    else:
        for ci, (net_id, _min_y) in enumerate(red_data_nets):
            lb.set_output_port(f"data_{ci}", net_id)


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
        help="Encode media (video/gif/images/audio) into Factorio blueprints."
    )
    encode_parser.add_argument(
        "input_paths", nargs="+",
        help="Path(s) to input media file(s). Multiple inputs are concatenated sequentially.",
    )
    encode_parser.add_argument("--name", default="Media Data", help="Base name of the blueprint")

    # ── Video options ────────────────────────────────────────────
    vid_g = encode_parser.add_argument_group("Video encoding")
    vid_g.add_argument("--skip", type=int, default=1, help="Read every Nth frame")
    vid_g.add_argument("--fps", type=float, default=0.0,
                       help="Source frame rate (1-60). 0 = auto-detect.")
    vid_g.add_argument("--adaptive", action="store_true",
                       help="Drop near-duplicate frames.")
    vid_g.add_argument("--threshold", type=float, default=0.01,
                       help="Similarity cutoff for adaptive mode (default: 0.01).")
    vid_g.add_argument("--deduplicate", action="store_true",
                       help="Share one combinator across identical frames.")
    vid_g.add_argument("--width", type=int, default=None,
                       help="Override display width (tiles).")
    vid_g.add_argument("--height", type=int, default=None,
                       help="Override display height (tiles).")

    chunk_g = encode_parser.add_argument_group("Time-chunked generation (video)")
    chunk_g.add_argument("--time-chunks", type=int, default=1,
                         help="Split video into N time slices for parallel encoding (default: 1 = off).")
    chunk_g.add_argument("--chunk-workers", type=int, default=None,
                         help="Max parallel worker processes (default: CPU count).")
    chunk_g.add_argument("--output-chunks", type=str, default=None,
                         help="Write individual chunk blueprints to DIR for inspection.")
    chunk_g.add_argument("--deduplicate-cross", action="store_true",
                         help="Deduplicate identical frames across time chunks (slower).")

    # ── Audio / MIDI options ─────────────────────────────────────
    _add_audio_midi_options(encode_parser)

    audio_g = encode_parser.add_argument_group("Audio encoding")
    audio_g.add_argument("--audio-only", action="store_true",
                         help="Extract and encode audio only from video input(s). Errors if no audio track found.")
    audio_g.add_argument("--no-audio", action="store_true",
                         help="Skip audio encoding entirely (video-only output).")
    audio_g.add_argument("--no-attach-player", action="store_true",
                         help="Output audio memory pages only, without the player decoder attached.")
    audio_g.add_argument("--rail-mode", type=str, default="auto:0.05",
                         help="Multi-rail mode: 'piano', 'all', 'auto[:threshold]' (default), or comma-separated instruments.")
    audio_g.add_argument("--instruments", type=str, default=None,
                         help="Deprecated alias for --rail-mode.")
    audio_g.add_argument("--map-drums", action="store_true", default=True,
                         help="Map GM drum notes (24-81) to Factorio drum-kit sounds (default: on).")
    audio_g.add_argument("--no-global-shift", action="store_true", default=False,
                         help="Disable optimal global octave shift.")

    g5 = encode_parser.add_argument_group("Audio file encoding (non-MIDI)")
    g5.add_argument("--output-midi", type=str, default=None,
                    help="Export extracted audio as MIDI to PATH before encoding.")
    g5.add_argument("--activation-threshold", type=float, default=0.0,
                    help="STFT activation threshold for audio analysis.")
    g5.add_argument("--midi-threshold", type=float, default=0.05,
                    help="MIDI extraction activation threshold.")
    g5.add_argument("--no-condense", action="store_true",
                    help="Disable MIDI note condensation.")
    g5.add_argument("--max-polyphony", type=int, default=0,
                    help="Cap simultaneous notes (0 = unlimited).")

    # ── Shared options ───────────────────────────────────────────
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

    # ==================================================================
    # Subcommand: blueprint-to-yaml
    # ==================================================================
    b2y_parser = subparsers.add_parser(
        "blueprint-to-yaml",
        help="Convert a blueprint string text file into logical YAML.",
    )
    b2y_parser.add_argument(
        "input",
        help="Path to a text file containing one blueprint string, or '-' for stdin.",
    )
    b2y_parser.add_argument(
        "-o", "--output", type=str, default=None,
        help="Write YAML to file instead of stdout.",
    )

    args = parser.parse_args()

    from . import CLOCK_SIGNAL  # pylint: disable=import-outside-toplevel

    if args.command == "encode":
        _handle_encode(args)

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
                map_drums=getattr(args, "map_drums", True),
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
            map_drums=getattr(args, "map_drums", True),
        )
        toml_str = to_toml(lb)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(toml_str)
            sys.stderr.write(f"Logical blueprint written to: {args.output}\n")
        else:
            sys.stdout.write(toml_str)

    elif args.command == "blueprint-to-yaml":
        from .logical_blueprint import blueprint_string_to_yaml

        if args.input == "-":
            bp_str = sys.stdin.read().strip()
        else:
            bp_str = _read_text_auto(Path(args.input)).strip()

        yaml_text = blueprint_string_to_yaml(bp_str)
        if args.output:
            Path(args.output).write_text(yaml_text, encoding="utf-8")
            sys.stderr.write(f"YAML written to: {args.output}\n")
        else:
            sys.stdout.write(yaml_text)


# ═══════════════════════════════════════════════════════════════════════
# Unified encode handler
# ═══════════════════════════════════════════════════════════════════════


def _build_combined_timer(total_ticks: int) -> LogicalBlueprint:
    """Build a timer for combined video+audio, exposing:
    - ``"clock_red"`` — modded (wrapping) clock on RED (for video memory)
    - ``"clock_green"`` — raw clock on GREEN (for audio memory + sub-tick)
    - ``"sub_tick"`` — sub-tick on RED (for progress bar, from raw clock)
    """
    from .timer import build_raw_timer, build_mod_timer, build_clock_bridge
    from .composer import _connect_nets_by_color

    timer = build_raw_timer("Timer", with_kick=False)
    timer.output_ports["raw"] = timer.output_ports.pop("out")

    # Mod timer: reads RED clock, wraps at total_ticks+1 → RED
    mod = build_mod_timer(total_ticks + 1, name="SubTick")
    timer.merge(mod, entity_prefix="mod_", network_prefix="mod_")
    timer.output_ports["clock_red"] = timer.output_ports.pop("out")

    # Sub-tick: raw clock % 60 → RED (for progress bar)
    sub = build_mod_timer(60, name="Mod60")
    timer.merge(sub, entity_prefix="sub60_", network_prefix="sub60_")
    timer.output_ports["sub_tick"] = timer.output_ports.pop("out")

    # Bridge: raw clock RED → GREEN (for audio)
    bridge = build_clock_bridge("Clock Bridge")
    timer.merge(bridge, entity_prefix="bridge_", network_prefix="bridge_")
    timer.output_ports["clock_green"] = timer.output_ports.pop("out")

    # Wire raw timer (RED) → mod timer (RED input)
    _connect_nets_by_color(
        timer, "red",
        entity_contains="_inc", port="output",
        other_entity_contains="mod_sub", other_port="input",
    )
    # Wire raw timer (RED) → sub60 timer (RED input)
    _connect_nets_by_color(
        timer, "red",
        entity_contains="_inc", port="output",
        other_entity_contains="sub60_sub", other_port="input",
    )
    # Wire raw timer (RED) → bridge (RED input)
    _connect_nets_by_color(
        timer, "red",
        entity_contains="_inc", port="output",
        other_entity_contains="bridge_clock", other_port="input",
    )

    timer.label = "Timer"
    return timer


def _should_process_audio(
    videos: list[str],
    standalone_audios: list[str],
    no_audio: bool,
) -> bool:
    """Return True when the encode pipeline should build audio components.

    Image-only inputs must not enter the audio composition path unless the
    user supplied standalone audio files.
    """
    if no_audio:
        return False
    if standalone_audios:
        return True
    return bool(videos)


def _handle_encode(args) -> None:  # pylint: disable=too-many-locals,too-many-statements
    """Unified encode: classify inputs and route accordingly."""
    power_type: str | None = getattr(args, "power", None)
    if power_type == "none":
        power_type = None
    use_progress: bool = getattr(args, "progress_bar", False)
    use_cache: bool = getattr(args, "cache", True)
    audio_only: bool = getattr(args, "audio_only", False)
    no_audio: bool = getattr(args, "no_audio", False)
    debug_dir: str | None = getattr(args, "debug_toml", None)

    input_paths: list[str] = list(args.input_paths)

    # ── Classify all inputs ───────────────────────────────────────
    videos: list[str] = []
    audios: list[str] = []
    images: list[str] = []
    for p in input_paths:
        kind = _classify_input(p)
        if kind == "video":
            videos.append(p)
        elif kind in ("audio", "midi"):
            audios.append(p)
        elif kind == "image":
            images.append(p)
        else:
            sys.exit(f"Error: cannot determine input type for: {p}")

    # ── Sanity checks ────────────────────────────────────────────
    if audio_only and not videos:
        sys.exit("Error: --audio-only requires at least one video input.")
    if audio_only and no_audio:
        sys.exit("Error: --audio-only and --no-audio are mutually exclusive.")

    if audio_only:
        # Extract audio from videos only
        _handle_audio_only(videos, args)
        return

    has_video = bool(videos) or bool(images)
    has_standalone_audio = bool(audios)
    has_any_audio = _should_process_audio(videos, audios, no_audio)

    # ── Pure audio: no video/images ──────────────────────────────
    if not has_video:
        _handle_audio_encode(audios, args)
        return

    # ── Video (possibly with audio) ──────────────────────────────
    sys.stderr.write(f"Encoding {len(videos)} video(s), {len(images)} image(s)...\n")

    # Resolve display dimensions from the first video/image
    first_visual = videos[0] if videos else images[0]
    resolved_w, resolved_h = _resolve_display_dims(
        first_visual, width=args.width, height=args.height,
    )

    # ── Encode video frames ──────────────────────────────────────
    from .logical_blueprint import from_draftsman

    video_bp = encode_auto(
        first_visual if len(input_paths) == 1 and not audios else input_paths[0],
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
        use_cache=use_cache,
    )

    if power_type is None:
        # Plain output: just write the blueprint string(s)
        sys.stdout.write(_to_fixed_string(video_bp) + "\n")
        if has_standalone_audio and not no_audio:
            sys.stderr.write("\n")
            _handle_audio_encode(audios, args)
        return

    # ── All-in-one composition ───────────────────────────────────
    video_lb = from_draftsman(video_bp)
    video_lb.label = f"Video Memory: {args.name}"
    _declare_memory_ports(video_lb)
    if debug_dir:
        _debug_dump_toml(video_lb, "01_video_memory", debug_dir)

    display_lb = build_display_logical(
        name="Display", width=resolved_w, height=resolved_h,
    )
    if debug_dir:
        _debug_dump_toml(display_lb, "02_display", debug_dir)

    total_video_ticks = _extract_total_ticks(video_lb)
    if total_video_ticks < 0:
        total_video_ticks = 0

    # ── Process audio ────────────────────────────────────────────
    audio_mem_lb: LogicalBlueprint | None = None
    player_lb: LogicalBlueprint | None = None
    total_audio_ticks = 0

    if has_any_audio:
        audio_mem_lb, player_lb, total_audio_ticks = _encode_audio_for_composition(
            videos, audios, args, total_video_ticks,
        )

    total_ticks = max(total_video_ticks, total_audio_ticks)

    # ── Build components and connections ─────────────────────────
    components: list[LogicalBlueprint] = []
    connections: list[PortConnection] = []

    if audio_mem_lb is not None and player_lb is not None:
        # Combined timer with RED (video) and GREEN (audio) outputs
        timer = _build_combined_timer(total_ticks)
        components.append(timer)
        if debug_dir:
            _debug_dump_toml(timer, "03_timer", debug_dir)

        components.append(video_lb)
        components.append(display_lb)
        components.append(audio_mem_lb)
        components.append(player_lb)

        connections.append(PortConnection("Timer", "clock_red", video_lb.label, "clock"))
        _connect_data_ports(connections, video_lb, display_lb)
        connections.append(PortConnection("Timer", "clock_green", audio_mem_lb.label, "clock"))
        connections.append(PortConnection(audio_mem_lb.label, "data", player_lb.label, "data"))
    else:
        # Video-only timer
        timer = _build_timer_for_memory(video_lb)
        components.append(timer)
        if debug_dir:
            _debug_dump_toml(timer, "03_timer", debug_dir)

        components.append(video_lb)
        components.append(display_lb)

        connections.append(PortConnection("Timer", "clock", video_lb.label, "clock"))
        _connect_data_ports(connections, video_lb, display_lb)

    # Progress bar
    if use_progress:
        from .progress_bar import build_progress_bar
        pb_max = max(1, total_ticks)
        pb = build_progress_bar("Progress", length=10,
                    signal_name="signal-clock", max_value=pb_max)
        components.append(pb)
        if debug_dir:
            _debug_dump_toml(pb, "04_progress", debug_dir)
        connections.append(PortConnection("Timer", "sub_tick", "Progress", "in"))

    result = compose(
        components=components,
        connections=connections,
        output_name=args.name,
        pole_type=power_type,
        use_cache=use_cache,
        cache_key_parts=("allinone", args.name, f"{resolved_w}x{resolved_h}",
                         hashlib.sha256(_to_fixed_string(video_bp).encode("utf-8")).hexdigest()[:16] if video_bp else ""),
    )
    if debug_dir:
        _debug_dump_toml(result, "05_merged", debug_dir)
    final_bp = to_draftsman(result)
    from .logical_blueprint import assert_wire_topology
    assert_wire_topology(final_bp, label=args.name)
    sys.stdout.write(_to_fixed_string(final_bp) + "\n")


def _handle_audio_only(videos: list[str], args) -> None:
    """Extract audio from video files, encode as audio blueprint."""
    import tempfile
    import os as _os

    sys.stderr.write(f"Extracting audio from {len(videos)} video(s)...\n")
    temp_dir = Path(tempfile.mkdtemp(prefix="fd_audio_"))
    wav_paths: list[str] = []
    try:
        for vp in videos:
            wav_path = str(temp_dir / f"{Path(vp).stem}_audio.wav")
            ok = _extract_audio_from_video(vp, wav_path)
            if ok:
                wav_paths.append(wav_path)
                sys.stderr.write(f"  Extracted: {Path(vp).name}\n")
            else:
                sys.stderr.write(f"  No audio in: {Path(vp).name}\n")

        if not wav_paths:
            sys.exit("Error: no audio tracks found in any of the input videos.")

        _handle_audio_encode(wav_paths, args)
    finally:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)


def _handle_audio_encode(audio_paths: list[str], args) -> None:
    """Encode standalone audio files into a blueprint."""
    from .audio.encoder import encode_audio_auto

    rail_mode: str = getattr(args, "rail_mode", "auto:0.05")
    if getattr(args, "instruments", None):
        rail_mode = args.instruments

    power_type: str | None = getattr(args, "power", None)
    if power_type == "none":
        power_type = None
    use_progress: bool = getattr(args, "progress_bar", False)
    use_cache: bool = getattr(args, "cache", True)
    debug_dir: str | None = getattr(args, "debug_toml", None)
    output_path: str | None = getattr(args, "output", None)

    # Build kwargs for encode_audio_auto
    midi_kwargs: dict[str, object] = {
        "attach_player": not getattr(args, "no_attach_player", False),
        "map_drums": getattr(args, "map_drums", True),
        "rail_mode": rail_mode,
        "use_global_shift": not getattr(args, "no_global_shift", False),
        "ticks_per_beat": getattr(args, "ticks_per_beat", 30),
        "boost_melody": getattr(args, "boost_melody", 1.0),
        "velocity_scale": getattr(args, "velocity_scale", 1.0),
        "attack_ticks": getattr(args, "attack_ticks", 10),
        "decay_ticks": getattr(args, "decay_ticks", 10),
        "sustain_level": getattr(args, "sustain_level", 1.0),
        "release_ticks": getattr(args, "release_ticks", 10),
        "attack_curve": getattr(args, "attack_curve", 1.0),
        "decay_curve": getattr(args, "decay_curve", 1.0),
        "release_curve": getattr(args, "release_curve", 1.0),
        "processed_midi_path": getattr(args, "processed_midi", None),
        "debug_json_path": getattr(args, "debug_json", None),
        "output_midi": getattr(args, "output_midi", None),
        "activation_threshold": getattr(args, "activation_threshold", 0.0),
        "midi_activation_threshold": getattr(args, "midi_threshold", 0.05),
        "condense_midi": not getattr(args, "no_condense", False),
        "max_polyphony": getattr(args, "max_polyphony", 0),
    }

    # For now, encode the first audio file; multi-audio concatenation
    # can be added later at the tick_data level.
    if len(audio_paths) > 1:
        sys.stderr.write(
            f"Note: {len(audio_paths)} audio inputs provided; "
            f"encoding first only. Multi-audio concatenation coming soon.\n"
        )

    if power_type is not None:
        midi_kwargs["attach_player"] = False
        audio_bp_str = encode_audio_auto(audio_paths[0], **midi_kwargs)
        if not audio_bp_str:
            return

        from draftsman.blueprintable import Blueprint
        from .logical_blueprint import from_draftsman

        audio_lb = from_draftsman(Blueprint.from_string(audio_bp_str))
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
            cache_key_parts=("audio", args.name,
                             hashlib.sha256(audio_bp_str.encode("utf-8")).hexdigest()[:16] if audio_bp_str else ""),
        )
        if debug_dir:
            _debug_dump_toml(result, "04_merged", debug_dir)
        final_bp2 = to_draftsman(result)
        from .logical_blueprint import assert_wire_topology
        assert_wire_topology(final_bp2, label=args.name)
        output = _to_fixed_string(final_bp2) + "\n"
    else:
        output = encode_audio_auto(audio_paths[0], **midi_kwargs)
        if output:
            output += "\n"

    if output:
        if output_path:
            Path(output_path).write_text(output, encoding="utf-8")
            sys.stderr.write(f"Blueprint written to: {output_path}\n")
        else:
            sys.stdout.write(output)


def _encode_audio_for_composition(
    videos: list[str],
    standalone_audios: list[str],
    args,
    video_total_ticks: int,
) -> tuple[LogicalBlueprint | None, LogicalBlueprint | None, int]:
    """Process audio for the all-in-one composition.

    Extracts audio from videos (timeline-aligned), combines with
    standalone audio files, encodes to LogicalBlueprints.
    Returns (audio_memory_lb, player_lb, total_audio_ticks).
    """
    import tempfile
    import os as _os
    import shutil

    from . import SIGNAL_POOL, QUALITIES
    from .audio.encoder import encode_audio_to_logical
    from .audio.player_blueprint import build_audio_decoder_logical

    rail_mode: str = getattr(args, "rail_mode", "auto:0.05")
    if getattr(args, "instruments", None):
        rail_mode = args.instruments

    # Collect all audio tick_data, aligned to the video timeline
    all_tick_data: list[list[int]] = []
    cumulative_tick = 0

    # Extract audio from each video
    temp_dir = Path(tempfile.mkdtemp(prefix="fd_audio_"))
    try:
        for vp in videos:
            video_ticks = _get_video_tick_count(vp, args.fps, args.skip)
            if video_ticks <= 0:
                continue

            wav_path = str(temp_dir / f"{Path(vp).stem}_audio.wav")
            ok = _extract_audio_from_video(vp, wav_path)
            if ok:
                # Convert extracted audio to tick_data
                audio_td = _audio_wav_to_tick_data(wav_path)
                # Pad with silence before (align to cumulative_tick)
                silence_before = cumulative_tick - len(all_tick_data)
                if silence_before > 0:
                    for _ in range(silence_before):
                        all_tick_data.append([0] * 48)
                # Add the audio data
                all_tick_data.extend(audio_td)
                sys.stderr.write(
                    f"  Audio from {Path(vp).name}: {len(audio_td)} ticks "
                    f"at offset {cumulative_tick}\n"
                )
            else:
                # Pad with silence for this video's duration
                silence_needed = cumulative_tick + video_ticks - len(all_tick_data)
                if silence_needed > 0:
                    for _ in range(silence_needed):
                        all_tick_data.append([0] * 48)

            cumulative_tick += video_ticks

        # Process standalone audio files
        for ap in standalone_audios:
            audio_td = _audio_file_to_tick_data(ap, args)
            if audio_td:
                all_tick_data.extend(audio_td)
                sys.stderr.write(f"  Audio from {Path(ap).name}: {len(audio_td)} ticks\n")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    if not all_tick_data or not any(any(v for v in tick) for tick in all_tick_data):
        sys.stderr.write("  No audio data to encode.\n")
        return None, None, 0

    total_audio_ticks = max(len(all_tick_data), video_total_ticks)

    # Pad to match video length if needed
    while len(all_tick_data) < total_audio_ticks:
        all_tick_data.append([0] * 48)

    # Encode audio memory
    # Use single-rail piano for extracted audio by default
    instrument = rail_mode.split(",")[0].strip() if "," in rail_mode else rail_mode
    if instrument in ("auto", "all"):
        instrument = "piano"
    if ":" in instrument:
        instrument = instrument.split(":")[0]

    signal_pool = list(SIGNAL_POOL)
    qualities = list(QUALITIES)

    from . import CLOCK_SIGNAL as _CS
    audio_mem_lb = encode_audio_to_logical(
        all_tick_data,
        output_name=f"Audio: {args.name}",
        signal_pool=signal_pool,
        qualities=qualities,
        clock_signal=_CS,
    )
    if not audio_mem_lb.entities:
        return None, None, 0

    audio_mem_lb.label = f"Audio Memory: {args.name}"
    _declare_memory_ports(audio_mem_lb)

    # Build player
    player_lb = build_audio_decoder_logical(
        name=f"Audio Player: {args.name}",
        instrument=instrument,
        clock_signal=_CS,
        map_drums=getattr(args, "map_drums", True),
    )

    audio_total = _extract_total_ticks(audio_mem_lb)
    return audio_mem_lb, player_lb, max(audio_total, total_audio_ticks)


def _audio_wav_to_tick_data(wav_path: str) -> list[list[int]]:
    """Convert a WAV file to tick_data (48 pitches, 0-100 int)."""
    from .audio.audio_analyzer import audio_file_to_loudness, fold_loudness_array

    full_loudness = audio_file_to_loudness(wav_path, activation_threshold=0.0)
    if not full_loudness:
        return []

    game_loudness = fold_loudness_array(full_loudness)

    # Normalize to 0-100
    global_max = 0.0
    for tick in game_loudness:
        for v in tick:
            if v > global_max:
                global_max = v
    scale = 100.0 / global_max if global_max > 0 else 1.0

    return [
        [max(0, min(100, int(round(v * scale)))) for v in tick]
        for tick in game_loudness
    ]


def _audio_file_to_tick_data(path: str, args) -> list[list[int]]:
    """Convert an audio file (WAV/FLAC/OGG/MP3) or MIDI to tick_data."""
    if _is_midi_file(path):
        return _midi_to_tick_data(path, args)
    return _audio_wav_to_tick_data(path)


def _midi_to_tick_data(midi_path: str, args) -> list[list[int]]:
    """Convert a MIDI file to tick_data (48 pitches, 0-100 int)."""
    import mido
    from .audio.midi_translator import midi_to_tick_data

    from ._unicode_io import mido_open  # pylint: disable=import-outside-toplevel

    mid = mido_open(midi_path)
    midi_kwargs: dict[str, object] = {}
    for key in (
        "ticks_per_beat", "boost_melody", "velocity_scale",
        "attack_ticks", "decay_ticks", "sustain_level", "release_ticks",
        "attack_curve", "decay_curve", "release_curve",
    ):
        val = getattr(args, key, None)
        if val is not None:
            midi_kwargs[key] = val

    float_data = midi_to_tick_data(mid, **midi_kwargs)  # type: ignore[arg-type]
    return [
        [max(0, min(100, int(round(v)))) for v in tick]
        for tick in float_data
    ]


if __name__ == "__main__":
    main()
