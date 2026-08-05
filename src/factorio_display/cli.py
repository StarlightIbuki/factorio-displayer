"""Command Line Interface for factorio-display.

Provides subcommands to encode media, export the physical display grid,
export the audio decoder circuitry, and encode MIDI audio files.
"""

from __future__ import annotations

import argparse
import hashlib
import math
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
    g.add_argument("--rearticulation-ticks", type=int, default=2,
                   help="Game ticks of silence inserted before a same-pitch note "
                        "that re-triggers within that window of the previous note's "
                        "end, so repeated notes re-attack instead of merging into one "
                        "sustained tone (default: 2; 0 = off)")
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


def _build_timer_for_memory(
    memory_lb: LogicalBlueprint,
    total_ticks: int | None = None,
) -> LogicalBlueprint:
    """Build a combined raw+mod timer suitable for a memory blueprint.

    The clock output colour is chosen to match the *memory_lb* clock input
    port colour (detected by :func:`_declare_memory_ports`).  With the unified
    bus schema both video and audio memory use the **GREEN** time bus:

    - **GREEN** (video + audio memory): the mod timer outputs the wrapping
      clock directly on the GREEN time bus (no red→green relay AC).  A second
      mod timer keeps ``"sub_tick"`` on RED for the progress bar.
    - **RED** (legacy / backward compatibility): the mod timer output
      (wrapping ``clock % N``) is exposed as both ``"clock"`` and
      ``"sub_tick"`` on RED; the raw clock stays internal.

    Exposes two output ports:
    - ``"clock"`` — clock signal for memory DC gating
    - ``"sub_tick"`` (red) — sub-tick for progress bar
    """
    from .timer import build_raw_timer, build_mod_timer
    from .composer import _connect_nets_by_color

    # _extract_total_ticks returns the largest tick index referenced by
    # memory conditions. A single-frame image typically uses only tick 0,
    # which must produce modulo interval 1 (always-on), not 2.
    if total_ticks is None:
        max_tick_index = _extract_total_ticks(memory_lb)
    else:
        max_tick_index = total_ticks
    if max_tick_index < 0:
        max_tick_index = 0

    # Determine the clock port colour from the memory blueprint.  With the
    # unified bus schema both video and audio memory use the GREEN time bus;
    # RED is kept only as a legacy fallback (e.g. pre-existing red-clock
    # blueprints or the synthetic test helper).
    clock_net_id = memory_lb.input_ports.get("clock")
    clock_color: str = "red"  # legacy fallback
    if clock_net_id is not None:
        for net in memory_lb.networks:
            if net.network_id == clock_net_id:
                clock_color = net.color
                break

    timer = build_raw_timer("Timer", with_kick=False)
    # Raw timer outputs on RED.  Rename "out" → "raw" to avoid collision
    # with mod timer's "out" during the merge below.
    timer.output_ports["raw"] = timer.output_ports.pop("out")

    # Mod timer (RED): reads RED clock, outputs the looping clock on RED
    # as "sub_tick" (used by the progress bar).
    mod = build_mod_timer(max_tick_index + 1, name="SubTick")
    timer.merge(mod, entity_prefix="mod_", network_prefix="mod_")
    timer.output_ports["sub_tick"] = timer.output_ports.pop("out")

    if clock_color == "red":
        # Legacy red clock bus (backward compatibility) — the modded
        # (wrapping) clock drives everything on RED.
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
        # Unified GREEN time bus (video + audio memory) — the *modded*
        # (looping) clock is output directly on GREEN from the mod AC — no
        # red→green relay combinator is needed anymore.
        mod_green = build_mod_timer(
            max_tick_index + 1, name="SubTickGreen", output_color="green",
        )
        timer.merge(mod_green, entity_prefix="modg_", network_prefix="modg_")

        # Wire raw timer (RED) → mod_green (RED input): the mod AC wraps the
        # raw clock at the *song length* (max_tick_index + 1) so the audio
        # loops back to the start when it reaches the end.
        _connect_nets_by_color(
            timer, "red",
            entity_contains="_inc", port="output",
            other_entity_contains="modg_sub", other_port="input",
        )
        # Wire raw timer (RED) → mod (RED input) for the red "sub_tick".
        _connect_nets_by_color(
            timer, "red",
            entity_contains="_inc", port="output",
            other_entity_contains="mod_sub", other_port="input",
        )
        # The mod_green "out" port is on GREEN — rename to "clock".
        timer.output_ports["clock"] = timer.output_ports.pop("out")
        # Drop the now-unused "raw" port
        del timer.output_ports["raw"]

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
    def _port_sort_key(port_name: str) -> tuple[int, int | str]:
        if port_name == "data":
            return (0, 0)
        if port_name.startswith("data_"):
            suffix = port_name[5:]
            if suffix.isdigit():
                return (1, int(suffix))
        return (2, port_name)

    video_ports = sorted(
        [p for p in video_lb.output_ports if p == "data" or p.startswith("data_")],
        key=_port_sort_key,
    )
    display_ports = sorted(
        [p for p in display_lb.input_ports if p == "data" or p.startswith("data_")],
        key=_port_sort_key,
    )
    for vp, dp in zip(video_ports, display_ports):
        connections.append(PortConnection(video_lb.label, vp, display_lb.label, dp))


def _declare_memory_ports(lb: LogicalBlueprint, clock_color: str | None = None) -> None:
    """Declare ``clock`` and ``data`` ports on a memory LogicalBlueprint
    parsed from a draftsman string.

    The clock port colour is determined by inspecting which network the
    DCs' input side already belongs to (GREEN for both video and audio
    memory — unified time bus).  The data port is always RED (DC outputs
    carry colour data on the unified signal bus).

    When there are no networks (single-frame memory with one DC and no
    wires), networks are created from the DC's endpoints directly.  In that
    case *clock_color* lets the caller force the expected colour (defaults
    to ``"green"`` — the unified time bus).
    """
    from .logical_blueprint import Endpoint, Network

    dcs = [(eid, ent) for eid, ent in lb.entities.items()
           if ent.type == "decider-combinator"]
    if not dcs:
        return

    # ── Clock input port — detect actual colour from DC inputs ────
    clock_net_id: str | None = None
    detected_color: str | None = None
    for net in lb.networks:
        for ep in net.endpoints:
            if ep.port == "input":
                ent = lb.entities.get(ep.entity_id)
                if ent is not None and ent.type == "decider-combinator":
                    clock_net_id = net.network_id
                    detected_color = net.color
                    break
        if clock_net_id is not None:
            break

    if clock_net_id is None:
        # No network at all — create a clock network containing
        # ALL DC inputs (not just the first one).  Use the caller-supplied
        # colour when available; default to the unified green time bus.
        color = clock_color or "green"
        clock_net = Network(
            network_id=f"{color}_clock",
            color=color,
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


def _restore_memory_prewiring(lb: LogicalBlueprint) -> None:
    """Rebuild deterministic prewiring metadata on memory buses.

    Memory blueprints reconstructed from draftsman lose ``prewired_pairs``.
    Re-attaching deterministic chain wiring lets network merges preserve
    internal buses and add only one compose bridge to downstream components.
    """
    from .logical_blueprint import Endpoint

    def _pos(ep: Endpoint) -> tuple[int, int]:
        ent = lb.entities.get(ep.entity_id)
        if ent is None or ent.position is None:
            return (0, 0)
        return ent.position

    def _snake_pairs(eps: list[Endpoint]) -> list[tuple[Endpoint, Endpoint]]:
        """Chain endpoints as a row-snake: each row left→right, alternating
        direction per row.  This mirrors the draftsman audio-memory grid and
        keeps every wire short (a naive (y, x) sort would wrap the rightmost
        column back to the leftmost column of the next row with an 11-tile
        jump for a 12-wide bank)."""
        by_row: dict[int, list[Endpoint]] = {}
        for ep in eps:
            by_row.setdefault(_pos(ep)[1], []).append(ep)
        ordered: list[Endpoint] = []
        for i, y in enumerate(sorted(by_row)):
            row = sorted(by_row[y], key=lambda e: _pos(e)[0])
            if i % 2 == 1:
                row = list(reversed(row))
            ordered.extend(row)
        return [(ordered[i], ordered[i + 1]) for i in range(len(ordered) - 1)]

    for net in lb.networks:
        if net.color != "red":
            continue
        in_eps = [
            ep for ep in net.endpoints
            if ep.port == "input"
            and (ent := lb.entities.get(ep.entity_id)) is not None
            and ent.type == "decider-combinator"
        ]
        out_eps = [
            ep for ep in net.endpoints
            if ep.port == "output"
            and (ent := lb.entities.get(ep.entity_id)) is not None
            and ent.type == "decider-combinator"
        ]

        # Prefer output-bus prewiring when present (memory→display bridge path).
        if len(out_eps) >= 2:
            net.prewired_pairs = _snake_pairs(out_eps)
        elif len(in_eps) >= 2:
            net.prewired_pairs = _snake_pairs(in_eps)


def _finalize_audio_composition(lb: LogicalBlueprint) -> None:
    """Post-compose fix-up for the composed audio blueprint (multi-rail).

    The generic composer lays the large audio-memory banks out to the right of
    the players and bridges them to the decoders using whichever endpoints
    happen to be spare in each network's pre-wired chain.  For a tall bank
    (hundreds of pages) those spare endpoints sit at the bank's extremes, far
    (> 9 tiles) from the player's page-data / clock inputs — Factorio silently
    drops such wires, leaving the memory and timer unconnected to the decoder.

    This pass makes the geometry and wiring deterministic, for **one or more
    rails** (each rail = one instrument, with its own memory bank and player):

    1. Shift each rail's memory bank so its *top* row aligns with that rail's
       page-port row — its row-snake then starts 1 tile from the page port.
    2. Shift the timer so its clock output sits just above the banks.
    3. Rebuild the merged clock (green) bus as a row-snake over *all* memory
       endpoints, splicing the timer output and every player's mod AC + page
       port into the *nearest* snake edge (keeps wires ≤ 9 tiles).
    4. Rebuild each rail's data (red) bus as a row-snake over that rail's
       memory outputs, splicing in its page port and selector chain.
    """
    from .logical_blueprint import (  # pylint: disable=import-outside-toplevel,relative-beyond-top-level
        Endpoint, _chebyshev, _endpoint_position, _entity_wire_point,
    )

    def _is_memory_page(eid: str) -> bool:
        ent = lb.entities.get(eid)
        if ent is None or ent.type != "decider-combinator":
            return False
        return any(
            c.get("first", "").startswith("signal-clock")
            for c in ent.properties.get("conditions", [])
        )

    mem_ids = [eid for eid in lb.entities if _is_memory_page(eid)]
    timer_ids = [eid for eid in lb.entities if eid.startswith("timer_")]
    player_ids = [eid for eid in lb.entities if eid.startswith("audio_player_")]
    if not mem_ids or not timer_ids or not player_ids:
        return

    # Distinguish a single-rail player (audio_player_…_mod / _page_port) from a
    # multi-rail player (audio_player_…_r0_…, audio_player_…_r1_…).  The label
    # prefix sits between "audio_player_" and the rail id, so search for the
    # "_r<digit>_" rail marker anywhere in the id.
    import re as _re
    # The entity id is ``audio_player_<label>_r<ri>_<role>``.  The label (from
    # the user's ``--name``) may itself contain "_r<digit>_" (e.g. "song r2"),
    # so match the LAST "_r<digit>_" — that is the real rail marker in the
    # suffix, not a label artefact.
    _RAIL_RE = _re.compile(r".*_r(\d+)_")
    multi = any(_RAIL_RE.search(eid) for eid in player_ids)
    if multi:
        rail_indices = sorted({
            int(m.group(1))
            for eid in player_ids
            for m in [_RAIL_RE.search(eid)] if m
        })
    else:
        rail_indices = [0]

    def _bbox(ids: list[str]) -> tuple[int, int, int, int] | None:
        xs = [lb.entities[e].position[0] for e in ids if lb.entities[e].position]
        ys = [lb.entities[e].position[1] for e in ids if lb.entities[e].position]
        if not xs:
            return None
        return min(xs), min(ys), max(xs), max(ys)

    def _page_port(ri: int) -> str | None:
        if multi:
            # Label prefix varies (e.g. "audio_player_media_data_"), find by suffix.
            return next(
                (e for e in player_ids if e.endswith(f"r{ri}_page_port")), None
            )
        # Single-rail player: label prefix varies (e.g. "large_"), find by suffix.
        return next((e for e in player_ids if e.endswith("page_port")), None)

    def _rail_mem(ri: int) -> list[str]:
        if multi:
            return [e for e in mem_ids if f"audio_memory_{ri}_" in e]
        return list(mem_ids)

    # ── 1. Deterministic multi-rail layout ──────────────────────────
    # Stack the rails vertically at x = [0, 12] (page ports at x = 12) and
    # place every rail's memory bank in the shared column x = [13, 24] below
    # its rail's page-port row.  The banks are stacked one under the other
    # (4-tile gap) so the shared green clock bus can be a single continuous
    # row-snake over ALL banks — no long cross-bank bridge wires, and the
    # timer at the top reaches the snake start within a couple of tiles.
    if multi:
        # Normalise the whole player so rail 0's page port is at x = 12.
        pp0_id = _page_port(0)
        pp0_ent = lb.entities.get(pp0_id) if pp0_id else None
        if pp0_ent is not None and pp0_ent.position is not None:
            dx0 = 12 - pp0_ent.position[0]  # type: ignore[index]
            if dx0:
                for eid in player_ids:
                    ent = lb.entities[eid]
                    if ent.position:
                        ent.position = (ent.position[0] + dx0, ent.position[1])
        # Stack: each later rail is moved into rail 0's x column (x = [0,12])
        # and dropped below the previous rail by the LARGER of the decoder
        # height (~24) or that rail's memory height — so a rail with a short
        # memory never lets the next decoder overlap the previous one.  For
        # long songs the memory height dominates (unchanged from before).
        prev_rows: int = 12
        prev_port_y: int | None = None
        for ri in rail_indices:
            pp_id = _page_port(ri)
            pp_ent = lb.entities.get(pp_id)
            if pp_ent is None or pp_ent.position is None:
                continue
            if ri > 0 and prev_port_y is not None:
                target_y = prev_port_y + max(24, 2 * (prev_rows - 1) + 4)
                dy = target_y - pp_ent.position[1]  # type: ignore[index]
                dxr = -(ri * 13)  # built at x = ri*13 → back to x = [0,12]
                for eid in player_ids:
                    m = _RAIL_RE.search(eid)
                    if m is not None and int(m.group(1)) == ri:
                        ent = lb.entities[eid]
                        if ent.position:
                            x, y = ent.position
                            ent.position = (x + dxr, y + dy)
            pp_x, pp_y = pp_ent.position  # type: ignore[misc]
            mem_ri = _rail_mem(ri)
            prev_rows = (len(mem_ri) + 11) // 12 if mem_ri else 1
            prev_port_y = pp_y

    # Place each rail's memory bank in a 12-column grid just right of its
    # page port, top-aligned with the page-port row.  (For multi-rail the
    # banks share the column x = [13,24] and stack vertically under the
    # rails, so the clock bus is one continuous snake.)
    first_pp: tuple[int, int] | None = None
    for ri in rail_indices:
        pp_id = _page_port(ri)
        pp_ent = lb.entities.get(pp_id)
        if pp_ent is None or pp_ent.position is None:
            continue
        pp_x, pp_y = pp_ent.position  # type: ignore[misc]
        if first_pp is None:
            first_pp = (pp_x, pp_y)
        mem_ri = _rail_mem(ri)
        for idx, eid in enumerate(mem_ri):
            m_ent = lb.entities.get(eid)
            if m_ent is None:
                continue
            col = idx % 12
            row = idx // 12
            m_ent.position = (pp_x + 1 + col, pp_y + row * 2)

    # ── 2. Timer placement ─────────────────────────────────────────
    # The timer sits just above bank 0's top-left, a tile or two from the
    # clock snake's start — a single short wire carries the clock into the
    # whole (vertically stacked) bank chain.
    timer_box = _bbox(timer_ids)
    if timer_box is not None and first_pp is not None:
        t_dx = (first_pp[0] + 1) - timer_box[0]
        t_dy = (first_pp[1] - 2) - timer_box[1]
        if t_dx or t_dy:
            for e in timer_ids:
                ent = lb.entities[e]
                if ent.position:
                    x, y = ent.position
                    ent.position = (x + t_dx, y + t_dy)

    lb._network_bounds_cache.clear()  # pylint: disable=protected-access

    def _pos(ep: Endpoint) -> tuple[int, int]:
        return _endpoint_position(ep, lb)

    def _row_snake(eps: list[Endpoint]) -> list[tuple[Endpoint, Endpoint]]:
        by_row: dict[int, list[Endpoint]] = {}
        for ep in eps:
            by_row.setdefault(_pos(ep)[1], []).append(ep)
        ordered: list[Endpoint] = []
        for i, y in enumerate(sorted(by_row)):
            row = sorted(by_row[y], key=lambda e: _pos(e)[0])
            if i % 2 == 1:
                row = list(reversed(row))
            ordered.extend(row)
        return [
            (ordered[i], ordered[i + 1]) for i in range(len(ordered) - 1)
        ], ordered

    def _splice(attach: Endpoint, pairs: list[tuple[Endpoint, Endpoint]]) -> None:
        """Splice *attach* into the pair of consecutive nodes nearest to it."""
        pa = _pos(attach)
        best_i = -1
        best_d = 1 << 30
        for i, (a, b) in enumerate(pairs):
            d = max(_chebyshev(_pos(a), pa), _chebyshev(_pos(b), pa))
            if d < best_d:
                best_d = d
                best_i = i
        if best_i < 0:
            return
        a, b = pairs[best_i]
        pairs[best_i] = (a, attach)
        pairs.insert(best_i + 1, (attach, b))

    def _snake_endpoints(pairs: list[tuple[Endpoint, Endpoint]]) -> list[Endpoint]:
        deg: dict[Endpoint, int] = {}
        for a, b in pairs:
            deg[a] = deg.get(a, 0) + 1
            deg[b] = deg.get(b, 0) + 1
        return [ep for ep, d in deg.items() if d < 2]

    def _wire_point(ep: Endpoint) -> tuple[float, float]:
        """Physical connection point of *ep* (direction-aware), matching what
        Factorio uses to measure circuit-wire reach."""
        return _entity_wire_point(ep, lb)

    def _wdist_points(pa: tuple[float, float], pb: tuple[float, float]) -> float:
        """Euclidean distance between two connection points."""
        return math.dist(pa, pb)

    def _wdist(ep_a: Endpoint, ep_b: Endpoint) -> float:
        return _wdist_points(_wire_point(ep_a), _wire_point(ep_b))

    def _find_relay_tile(
        tx: int, ty: int, occ: set[tuple[int, int]],
        prev_w: tuple[float, float], target_w: tuple[float, float],
        reach: float,
    ) -> tuple[int, int] | None:
        """Find a free tile near (tx, ty) whose connection point is within
        *reach* tiles (Euclidean) of *prev_w* and strictly closer to
        *target_w* (so bridging provably terminates).  Farthest reachable tiles
        win, so the fewest relays are needed.  Returns None when no such tile
        exists near the ideal step (the caller then retries with a shorter
        step)."""
        best: tuple[tuple[float, float], tuple[int, int]] | None = None
        for r in range(0, 6):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if abs(dx) != r and abs(dy) != r:
                        continue
                    cand = (tx + dx, ty + dy)
                    if cand in occ:
                        continue
                    cand_w = (cand[0] + 0.5, cand[1] + 0.5)
                    d_prev = math.dist(prev_w, cand_w)
                    if d_prev > reach:
                        continue
                    if math.dist(cand_w, target_w) >= math.dist(prev_w, target_w):
                        continue  # must make progress toward the far end
                    # Farthest reachable tile wins (fewest relays); ties broken
                    # by proximity to the ideal step (tidier layout).
                    key = (-d_prev, abs(dx) + abs(dy))
                    if best is None or key < best[0]:
                        best = (key, cand)
        if best is not None:
            return best[1]
        # Dense neighbourhood: relax the progress requirement, keep the reach
        # bound and never return an occupied tile.
        for r in range(1, 7):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if abs(dx) != r and abs(dy) != r:
                        continue
                    cand = (tx + dx, ty + dy)
                    cand_w = (cand[0] + 0.5, cand[1] + 0.5)
                    if cand not in occ and math.dist(prev_w, cand_w) <= reach:
                        return cand
        return None

    # Relay pole ids must be unique across EVERY network bridged below (the
    # green clock bus plus each rail's red data bus), so the counter is shared
    # at this scope instead of being reset per call.
    _shared_pole_counter = [0]

    # A legendary small electric pole reaches 17.5 tiles (base 7.5 wire reach
    # + 2×quality-level), so pole↔pole hops can be much longer than with
    # normal poles.  Hops that touch a normal-quality combinator are still
    # capped at 9 tiles (the combinator's own circuit_wire_max_distance).
    _POLE_REACH = 17.5
    _COMBINATOR_REACH = 9.0

    def _bridge_with_poles(
        pairs: list[tuple[Endpoint, Endpoint]], net,
    ) -> list[tuple[Endpoint, Endpoint]]:
        """Split long pairs with legendary ``small-electric-pole`` relays.

        Factorio silently drops circuit wires longer than the effective
        connection reach (measured Euclidean between connection points): 9
        tiles for normal combinators, 17.5 tiles for a legendary small
        electric pole.  A long clock/data run is relayed through intermediate
        legendary poles — the first hop (combinator → pole) and the final hop
        (pole → combinator) stay within 9 tiles, while pole↔pole hops may use
        the full 17.5-tile reach.  That longer reach means far fewer relays
        than the previous normal-quality poles.
        """
        from .logical_blueprint import LogicalEntity  # pylint: disable=import-outside-toplevel

        prefix = "green_bus_pole_" if net.color == "green" else "red_bus_pole_"

        # Occupancy in tile space, honouring each entity's footprint and
        # direction (combinators are 1×2 north/south or 2×1 east/west).
        occ = set()
        for e in lb.entities.values():
            if e.position is None:
                continue
            x, y = e.position
            if e.type in ("arithmetic-combinator", "decider-combinator"):
                if e.direction in (2, 6):  # east/west → 2 wide × 1 tall
                    occ.add((x, y))
                    occ.add((x + 1, y))
                else:  # north/south → 1 wide × 2 tall
                    occ.add((x, y))
                    occ.add((x, y + 1))
            else:
                occ.add((x, y))

        out: list[tuple[Endpoint, Endpoint]] = []
        for a, b in pairs:
            if _wdist(a, b) <= _COMBINATOR_REACH:
                out.append((a, b))
                continue
            prev = a
            prev_is_pole = False
            for _guard in range(512):  # safety cap; normally only a few relays
                prev_w = _wire_point(prev)
                b_w = _wire_point(b)
                if _wdist_points(prev_w, b_w) <= _COMBINATOR_REACH:
                    out.append((prev, b))
                    break
                tile_p = _pos(prev)
                dx = b_w[0] - prev_w[0]
                dy = b_w[1] - prev_w[1]
                reach = _POLE_REACH if prev_is_pole else _COMBINATOR_REACH
                steps = (
                    (17, 15, 13, 11, 9, 7, 5, 3, 1) if prev_is_pole
                    else (8, 6, 4, 3, 2, 1)
                )
                placed: tuple[int, int] | None = None
                # Try progressively shorter axis-aligned steps; the largest
                # step that yields a valid free relay tile wins.
                for step in steps:
                    tx, ty = tile_p
                    if abs(dx) >= abs(dy):
                        tx += (1 if dx > 0 else -1) * min(step, int(abs(dx)))
                    else:
                        ty += (1 if dy > 0 else -1) * min(step, int(abs(dy)))
                    placed = _find_relay_tile(tx, ty, occ, prev_w, b_w, reach)
                    if placed is not None:
                        break
                if placed is None:
                    # Genuinely stuck (fully packed within reach): keep the
                    # direct wire rather than overlap or loop forever.
                    out.append((prev, b))
                    break
                pid = f"{prefix}{_shared_pole_counter[0]}"
                _shared_pole_counter[0] += 1
                lb.add_entity(LogicalEntity(
                    pid, "small-electric-pole", {"quality": "legendary"}, placed,
                ))
                occ.add(placed)
                p_ep = Endpoint(pid, "input")
                net.endpoints.add(p_ep)
                out.append((prev, p_ep))
                prev = p_ep
                prev_is_pole = True
            else:
                # Safety: never leave the pair unconnected.
                out.append((prev, b))
        return out

    # ── 3a. Rebuild the shared green clock bus ─────────────────────────
    # The banks are stacked in one column, so ONE row-snake over every memory
    # page keeps the whole clock bus a single connected component.  Each
    # rail's mod + page port splices into the snake at its own bank; the
    # timer output bridges to the nearest degree-1 endpoint (the snake start,
    # a couple of tiles below the timer).
    for net in lb.networks:
        if net.color != "green":
            continue
        mem_eps = [
            ep for ep in net.endpoints
            if ep.entity_id in mem_ids and ep.port == "input"
        ]
        if len(mem_eps) < 2:
            continue
        timer_out = next(
            (ep for ep in net.endpoints
             if ep.entity_id in timer_ids and ep.port == "output"), None
        )
        attach_eps = [
            ep for ep in net.endpoints
            if ep.entity_id in player_ids and ep.port == "input"
            and (ep.entity_id.endswith("mod") or ep.entity_id.endswith("page_port"))
        ]

        pairs, ordered = _row_snake(mem_eps)
        for at in attach_eps:
            _splice(at, pairs)
        if timer_out is not None and ordered:
            ends = _snake_endpoints(pairs)
            nearest = min(
                ends, key=lambda ep: _chebyshev(_pos(ep), _pos(timer_out))
            )
            pairs.append((timer_out, nearest))
        pairs = _bridge_with_poles(pairs, net)
        net.prewired_pairs = pairs
        lb._network_bounds_cache.clear()  # pylint: disable=protected-access

    # ── 3b. Rebuild each rail's red data bus ───────────────────────────
    for net in lb.networks:
        if net.color != "red":
            continue
        mem_eps = [
            ep for ep in net.endpoints
            if ep.entity_id in mem_ids and ep.port == "output"
        ]
        if len(mem_eps) < 2:
            continue
        # Only this rail's player selectors/page port sit on this red network
        # (each rail's data bus is independent), so the filter below naturally
        # picks the matching rail.
        sel_eps = [
            ep for ep in net.endpoints
            if ep.entity_id in player_ids and (
                ep.entity_id.endswith("_sel") or ep.entity_id.endswith("page_port")
            )
        ]
        if not sel_eps:
            continue
        pairs, ordered = _row_snake(mem_eps)
        page_port_out = next(
            (ep for ep in sel_eps if ep.entity_id.endswith("page_port")), None
        )
        sels = sorted(
            (ep for ep in sel_eps if ep.entity_id.endswith("_sel")),
            key=lambda e: int(e.entity_id.rsplit("ch", 1)[1].rsplit("_", 1)[0]),
            reverse=True,  # ch11 → ch0
        )
        if page_port_out is not None:
            chain: list[Endpoint] = [page_port_out] + sels
            for i in range(len(chain) - 1):
                pairs.append((chain[i], chain[i + 1]))
            start = ordered[0]
            pairs.append((start, page_port_out))
        # Long red data runs (e.g. a distant selector page port) must also be
        # relayed so every wire respects Factorio's 9-tile limit.
        pairs = _bridge_with_poles(pairs, net)
        net.prewired_pairs = pairs


# ── Main CLI ───────────────────────────────────────────────────────────

def _add_json_option(parser: argparse.ArgumentParser) -> None:
    """Add the machine-readable ``--json`` output flag to *parser*."""
    parser.add_argument(
        "--json", action="store_true", default=False,
        help="Emit a JSON envelope {version, result} instead of a raw blueprint/text.",
    )


def _json_envelope(args, out_text: str, err_text: str, exit_code: int) -> dict:
    """Build the ``--json`` output envelope for a completed command.

    The shape is the golden contract shared with the web API: the ``encode``
    command emits ``result.blueprint`` plus metadata; builder commands emit
    ``result.blueprint`` (display/audio) or ``result.text`` (logical/yaml).
    """
    import json  # pylint: disable=import-outside-toplevel

    from . import __version__  # pylint: disable=import-outside-toplevel
    from .service import (  # pylint: disable=import-outside-toplevel
        _extract_ticks_from_logs,
        _extract_warnings,
        count_entities,
    )

    cmd = args.command
    primary = out_text.strip()
    result: dict = {
        "command": cmd,
        "blueprint": primary,
        "logs": err_text,
    }

    # Piecewise (split) output emits a JSON envelope on stdout — merge its
    # pieces/book into the result while still filling the shared metadata.
    split_envelope: dict | None = None
    if cmd == "encode" and primary.startswith("{"):
        try:
            _parsed = json.loads(primary)
            if isinstance(_parsed, dict) and "split_envelope" in _parsed:
                split_envelope = _parsed["split_envelope"]
        except Exception:  # pylint: disable=broad-except
            split_envelope = None

    if cmd == "encode":
        inputs = list(getattr(args, "input_paths", []) or [])
        known = [k for k in (_classify_input(p) for p in inputs) if k != "unknown"]
        result["name"] = getattr(args, "name", "Media Data")
        result["kind"] = known[0] if known else "unknown"
        w = getattr(args, "width", None)
        h = getattr(args, "height", None)
        if w is not None or h is not None:
            result["dimensions"] = [w, h]
        rail = getattr(args, "rail_mode", None) or getattr(args, "instruments", None)
        if rail and str(rail) not in ("auto:0.05", "auto", "all"):
            result["instruments"] = [s.strip() for s in str(rail).split(",") if s.strip()]
        result["total_ticks"] = _extract_ticks_from_logs(err_text)
        result["warnings"] = _extract_warnings(err_text)
        result["artifacts"] = []
        if split_envelope is not None:
            result["blueprint"] = split_envelope.get("blueprint", "")
            result["pieces"] = split_envelope.get("pieces", [])
            result["book"] = split_envelope.get("book", "")
            result["split"] = True
            # A book string can't be counted by count_entities (it expects a
            # single blueprint doc); report the per-piece entity counts.
            ec = []
            for piece in result["pieces"]:
                n = count_entities(piece.get("blueprint", ""))
                if n is not None:
                    ec.append(n)
            result["entity_count"] = sum(ec) if ec else None
            result["piece_count"] = len(result["pieces"])
        else:
            result["entity_count"] = count_entities(primary)
    elif cmd in ("export-display", "export-audio"):
        result["name"] = getattr(args, "name", None)
        result["format"] = "blueprint"
        result["entity_count"] = count_entities(primary)
    elif cmd == "export-logical":
        result["name"] = getattr(args, "name", None)
        result["blueprint"] = ""
        result["text"] = primary
        result["format"] = "toml"
    elif cmd == "blueprint-to-yaml":
        result["blueprint"] = ""
        result["text"] = primary
        result["format"] = "yaml"
    elif cmd == "blueprint-ascii":
        result["blueprint"] = ""
        result["text"] = primary
        result["format"] = "ascii"

    if exit_code:
        last = [ln for ln in err_text.strip().splitlines() if ln.strip()]
        result["error"] = {
            "code": "cli_error",
            "message": last[-1] if last else f"command exited with code {exit_code}",
        }
    return {"version": __version__, "result": result}


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
    vid_g.add_argument("--adaptive", action=argparse.BooleanOptionalAction, default=True,
                       help="Drop near-duplicate frames (default: on). Use --no-adaptive to keep every frame.")
    vid_g.add_argument("--threshold", type=float, default=0.005,
                       help="Similarity cutoff for adaptive mode, 0-1 (default: 0.005 = conservative, "
                            "barely-noticeable frame merging).")
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

    split_g = encode_parser.add_argument_group("Piecewise (chunked) output — DEFAULT")
    split_g.add_argument("--all-in-one", action="store_true",
                         help="Legacy single merged blueprint (timer + power + everything in one "
                              "string). NOT recommended: for large videos this produces an "
                              "enormous blueprint that is slow to generate. The default is "
                              "piecewise chunked output (independent pieces, wired in game).")
    split_g.add_argument("--output-dir", type=str, default="split_output",
                         help="Directory to write piecewise blueprint files to (default: split_output).")
    split_g.add_argument("--max-piece-mb", type=float, default=2.0,
                         help="Target maximum serialised size of each memory piece in MB "
                              "(default: 2.0). Videos whose memory would exceed this are "
                              "auto-split into more time fragments so every piece stays "
                              "near this size.")
    split_g.add_argument("--book", action="store_true",
                         help="Always emit a single blueprint book containing all pieces "
                              "(default: only when the total output is small, roughly <1 MB).")
    split_g.add_argument("--no-book", action="store_true",
                         help="Never emit a blueprint book (write individual piece files only).")

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
    audio_g.add_argument("--map-drums", action="store_true", default=False,
                         help="Route below-range low notes to a kick drum instead of covering them "
                              "with the low-pitch bass instrument (default: off — accurate instrument "
                              "mapping is preferred).")
    audio_g.add_argument("--drum-gain", type=float, default=0.25,
                         help="Volume scale for the drum rail (0-1; default 0.25 — drums sit low "
                         "in the mix and sound more dominating than pitched notes).")
    audio_g.add_argument("--no-global-shift", action="store_true", default=False,
                         help="Disable optimal global octave shift.")

    g5 = encode_parser.add_argument_group("Audio file encoding (non-MIDI)")
    g5.add_argument("--no-ai-transcribe", action="store_true",
                    help="Disable the optional AI transcription (Basic Pitch) for non-MIDI "
                         "audio; always use the built-in STFT analysis instead.")
    g5.add_argument("--drums", nargs="?", const="auto", default=None,
                    choices=["auto", "off"],
                    help="Detect kick/snare/hat from the audio waveform and add a drum rail. "
                         "Basic Pitch (and the STFT fallback) only transcribe pitched notes, "
                         "so real drums are otherwise missing. 'auto' (default when the flag "
                         "is given) adds a drum rail whenever enough hits are detected.")
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
    _add_json_option(encode_parser)

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
    _add_json_option(display_parser)

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
    _add_json_option(audio_parser)

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
    _add_json_option(export_logical_parser)

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
    _add_json_option(b2y_parser)

    # ==================================================================
    # Subcommand: blueprint-ascii
    # ==================================================================
    ascii_parser = subparsers.add_parser(
        "blueprint-ascii",
        help="Render a blueprint string as ASCII art (entities + wiring) for debugging.",
    )
    ascii_parser.add_argument(
        "input",
        help="Path to a text file containing one blueprint string, or '-' for stdin.",
    )
    ascii_parser.add_argument(
        "-o", "--output", type=str, default=None,
        help="Write the ASCII art to file instead of stdout.",
    )
    ascii_parser.add_argument(
        "--no-coords", action="store_true",
        help="Omit the row/column coordinate headers from the grids.",
    )
    _add_json_option(ascii_parser)

    # ==================================================================
    # Subcommand: token  (sign & verify access tokens)
    # ==================================================================
    token_parser = subparsers.add_parser(
        "token",
        help="Sign and verify HMAC-signed access tokens.",
    )
    token_sub = token_parser.add_subparsers(dest="token_cmd", required=True, title="token commands")
    token_issue = token_sub.add_parser("issue", help="Sign a new access token for a user.")
    token_issue.add_argument("--key", required=True,
                             help="Shared HMAC key (must match the server's --token-key).")
    token_issue.add_argument("--user", required=True,
                             help="Owner/subject of the token (any string, e.g. a username).")
    token_issue.add_argument("--ttl-hours", type=int, default=24 * 7,
                             help="Token lifetime in hours (default: 168 = 7 days).")
    token_issue.add_argument("--scope", default="*",
                             help="Scope claim (default: '*').")
    token_verify = token_sub.add_parser("verify", help="Verify a token and print its claims.")
    token_verify.add_argument("--key", required=True,
                              help="Shared HMAC key (must match the server's --token-key).")
    token_verify.add_argument("token", help="The token string to verify.")
    _add_json_option(token_parser)

    # ==================================================================
    # Subcommand: server
    # ==================================================================
    server_parser = subparsers.add_parser(
        "server",
        help="Run the FastAPI web server (requires the optional 'web' extra).",
    )
    server_parser.add_argument("--host", type=str, default="127.0.0.1",
                               help="Bind host (default: 127.0.0.1).")
    server_parser.add_argument("--port", type=int, default=8000,
                               help="Bind port (default: 8000).")
    server_parser.add_argument("--data-dir", type=str, default=None,
                               help="Server data directory (default: server_data/).")
    server_parser.add_argument("--max-workers", type=int, default=2,
                               help="Max concurrent encode jobs (default: 2).")
    server_parser.add_argument("--max-jobs-per-user", type=int, default=2,
                               help="Max active jobs per caller (default: 2).")
    server_parser.add_argument("--anonymous-max-processing", type=int, default=1,
                               help="Max concurrently-processing jobs for the shared anonymous "
                                    "bucket (default: 1).")
    server_parser.add_argument("--anonymous-max-queued", type=int, default=5,
                               help="Max queued jobs for the shared anonymous bucket (default: 5).")
    server_parser.add_argument("--anonymous-max-per-hour", type=int, default=20,
                               help="Max jobs per rolling hour for the shared anonymous bucket (default: 20).")
    server_parser.add_argument("--max-upload-mb", type=int, default=None,
                               help="Reject uploads larger than this many MiB (default: 256).")
    server_parser.add_argument("--api-token", type=str, default=None,
                               help="Optional shared-secret gate (Authorization: Bearer or X-API-Token).")
    server_parser.add_argument("--token-key", type=str, default=None,
                               help="HMAC key used to sign/verify access tokens. Issue tokens "
                                    "with 'factorio-display token issue --key <same key>'.")
    server_parser.add_argument("--base-url", type=str, default=None,
                               help="Public base URL (default: http://<host>:<port>).")
    server_parser.add_argument(
        "--compress-artifacts", dest="compress_artifacts",
        action=argparse.BooleanOptionalAction, default=True,
        help="Gzip large text artifacts on disk (default: on).",
    )
    server_parser.add_argument(
        "--cors-origins", type=str, default=None, metavar="LIST",
        help="Comma-separated allowed CORS origins (replaces the defaults, "
             "which include https://StarlightIbuki.github.io).",
    )
    server_parser.add_argument(
        "--cors-origin-regex", type=str, default=None, metavar="REGEX",
        help="Regex of allowed CORS origins (replaces the default "
             "https://[a-z0-9-]+\\.github\\.io).",
    )
    server_parser.add_argument(
        "--github-client-id", type=str, default=None,
        help="GitHub OAuth App client id (env GITHUB_OAUTH_CLIENT_ID).",
    )
    server_parser.add_argument(
        "--github-client-secret", type=str, default=None,
        help="GitHub OAuth App client secret (env GITHUB_OAUTH_CLIENT_SECRET). "
             "Stored server-side only.",
    )
    server_parser.add_argument(
        "--github-redirect-uri", type=str, default=None,
        help="OAuth callback URL, e.g. https://factorio.qvq.moe:60012/auth/github/callback "
             "(env GITHUB_OAUTH_REDIRECT_URI).",
    )
    server_parser.add_argument(
        "--frontend-url", type=str, default=None,
        help="Where to redirect back after OAuth — the SPA origin, e.g. "
             "https://StarlightIbuki.github.io/factorio-displayer/ (env FRONTEND_URL).",
    )

    args = parser.parse_args()

    if getattr(args, "json", False):
        import contextlib  # pylint: disable=import-outside-toplevel
        import io  # pylint: disable=import-outside-toplevel
        import json as _json  # pylint: disable=import-outside-toplevel

        out_buf = io.StringIO()
        err_buf = io.StringIO()
        exit_code = 0
        try:
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                _dispatch(args)
        except SystemExit as exc:
            code = exc.code
            exit_code = int(code) if isinstance(code, int) else (1 if code else 0)
        envelope = _json_envelope(args, out_buf.getvalue(), err_buf.getvalue(), exit_code)
        sys.stdout.write(_json.dumps(envelope, ensure_ascii=False))
        if exit_code:
            sys.exit(exit_code)
        return

    _dispatch(args)


def _dispatch(args) -> None:
    """Dispatch parsed args to the appropriate encoder/builder."""
    from . import CLOCK_SIGNAL  # pylint: disable=import-outside-toplevel

    if args.command == "encode":
        _handle_encode(args)

    elif args.command == "export-display":
        from .video.encoder import _to_fixed_string  # pylint: disable=import-outside-toplevel
        sys.stderr.write(f"Building display blueprint: {args.name}...\n")
        display_bp = build_display(
            name=args.name,
            width=args.width,
            height=args.height
        )
        sys.stdout.write(_to_fixed_string(display_bp) + "\n")

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

    elif args.command == "blueprint-ascii":
        from .ascii_render import render_blueprint

        if args.input == "-":
            bp_str = sys.stdin.read().strip()
        else:
            bp_str = _read_text_auto(Path(args.input)).strip()

        ascii_text = render_blueprint(bp_str, coords=not getattr(args, "no_coords", False))
        if args.output:
            Path(args.output).write_text(ascii_text, encoding="utf-8")
            sys.stderr.write(f"ASCII art written to: {args.output}\n")
        else:
            sys.stdout.write(ascii_text)

    elif args.command == "token":
        from .api.tokens import sign, verify, TokenError  # pylint: disable=import-outside-toplevel

        if args.token_cmd == "issue":
            token = sign(args.key, args.user, ttl_seconds=args.ttl_hours * 3600, scope=args.scope)
            sys.stdout.write(token + "\n")
        elif args.token_cmd == "verify":
            try:
                claims = verify(args.key, args.token)
            except TokenError as exc:
                sys.exit(f"INVALID: {exc}")
            for k, v in claims.items():
                sys.stdout.write(f"{k}: {v}\n")

    elif args.command == "server":
        _handle_server(args)


def _handle_server(args) -> None:
    """Launch the FastAPI web server (requires the optional 'web' extra)."""
    try:
        from .api.server import serve  # pylint: disable=import-outside-toplevel
        from .api.settings import Settings  # pylint: disable=import-outside-toplevel
    except ImportError as exc:
        sys.exit(
            "The web server requires the optional 'web' extra.\n"
            "Install it with:  pip install -e '.[web]'"
        )

    base_url = args.base_url or f"http://{args.host}:{args.port}"
    import os  # pylint: disable=import-outside-toplevel

    # CORS: CLI flags win, then env vars, then the built-in defaults.
    cors_env = os.environ.get("CORS_ALLOW_ORIGINS", "")
    cors_regex_env = os.environ.get("CORS_ALLOW_ORIGIN_REGEX", "")
    cors_origins = tuple(
        o.strip() for o in (args.cors_origins if args.cors_origins is not None else cors_env).split(",")
        if o.strip()
    )
    cors_regex = args.cors_origin_regex or cors_regex_env or None
    settings = Settings(
        data_dir=Path(args.data_dir) if args.data_dir else Path("server_data"),
        max_workers=args.max_workers,
        max_jobs_per_user=args.max_jobs_per_user,
        anonymous_max_processing=args.anonymous_max_processing,
        anonymous_max_queued=args.anonymous_max_queued,
        anonymous_max_per_hour=args.anonymous_max_per_hour,
        max_upload_bytes=(args.max_upload_mb or 256) * 1024 * 1024,
        api_token=args.api_token,
        token_key=args.token_key,
        compress_artifacts=args.compress_artifacts,
        host=args.host,
        port=args.port,
        base_url=base_url,
        cors_allow_origins=cors_origins or Settings.cors_allow_origins,
        cors_allow_origin_regex=cors_regex or Settings.cors_allow_origin_regex,
        # GitHub OAuth — CLI wins, then env vars.
        github_oauth_client_id=args.github_client_id
        or os.environ.get("GITHUB_OAUTH_CLIENT_ID", "")
        or Settings.github_oauth_client_id,
        github_oauth_client_secret=args.github_client_secret
        or os.environ.get("GITHUB_OAUTH_CLIENT_SECRET", "")
        or Settings.github_oauth_client_secret,
        github_oauth_redirect_uri=args.github_redirect_uri
        or os.environ.get("GITHUB_OAUTH_REDIRECT_URI", "")
        or Settings.github_oauth_redirect_uri,
        frontend_url=args.frontend_url
        or os.environ.get("FRONTEND_URL", "")
        or Settings.frontend_url,
    )
    serve(settings)


# ═══════════════════════════════════════════════════════════════════════
# Unified encode handler
# ═══════════════════════════════════════════════════════════════════════


def _build_combined_timer(total_ticks: int) -> LogicalBlueprint:
    """Build a timer for combined video+audio, exposing:
    - ``"clock"`` — modded (wrapping) clock on GREEN (unified time bus: video
      and audio memory share the same looping clock at *total_ticks*)
    - ``"sub_tick"`` — sub-tick on RED (for progress bar, from raw clock)
    """
    from .timer import build_raw_timer, build_mod_timer
    from .composer import _connect_nets_by_color

    timer = build_raw_timer("Timer", with_kick=False)
    timer.output_ports["raw"] = timer.output_ports.pop("out")

    # Unified time bus: the modded (looping) clock is output directly on GREEN
    # from a mod AC — no red→green relay combinator needed.  Video and audio
    # memory share this clock (both wrap at total_ticks + 1).
    mod_green = build_mod_timer(total_ticks + 1, name="SubTickGreen", output_color="green")
    timer.merge(mod_green, entity_prefix="modg_", network_prefix="modg_")
    timer.output_ports["clock"] = timer.output_ports.pop("out")

    # Sub-tick: raw clock % 60 → RED (for progress bar)
    sub = build_mod_timer(60, name="Mod60")
    timer.merge(sub, entity_prefix="sub60_", network_prefix="sub60_")
    timer.output_ports["sub_tick"] = timer.output_ports.pop("out")

    # Wire raw timer (RED) → the green mod timer (RED input)
    _connect_nets_by_color(
        timer, "red",
        entity_contains="_inc", port="output",
        other_entity_contains="modg_sub", other_port="input",
    )
    # Wire raw timer (RED) → sub60 timer (RED input)
    _connect_nets_by_color(
        timer, "red",
        entity_contains="_inc", port="output",
        other_entity_contains="sub60_sub", other_port="input",
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


# Rough threshold for auto-book: if the total piecewise output is below this
# many characters (~1 MB), a single blueprint book is emitted by default
# (small enough to paste/copy at once).  Precision is intentionally loose.
_BOOK_CHAR_THRESHOLD = 1_000_000


def _assemble_book(pieces: list[tuple[str, str]], book_label: str) -> str | None:
    """Build a :class:`BlueprintBook` string from *pieces*, or ``None``."""
    from draftsman.blueprintable import Blueprint, BlueprintBook

    book = BlueprintBook()
    book.label = book_label
    for label, s in pieces:
        try:
            bp = Blueprint.from_string(s)
        except Exception as exc:  # pylint: disable=broad-except
            sys.stderr.write(f"  Skipping {label} in book: {exc}\n")
            continue
        bp.label = label
        book.blueprints.append(bp)
    if not book.blueprints:
        return None
    return book.to_string()


def _write_split_output(pieces: list[tuple[str, str]], args, *, book_label: str) -> dict:
    """Emit piecewise output; returns a dict for the ``--json`` envelope.

    *pieces* is a list of ``(label, blueprint_string)``.

    Output strategy (default = piecewise):
      * If the total output is small (or ``--book``), a single blueprint
        book is written to ``book.txt`` and returned as the primary string.
      * Otherwise individual piece files are written to ``args.output_dir``
        and the first piece is the primary string (nothing else is usable as
        one paste).

    The returned dict has the keys the JSON envelope needs:
    ``{"blueprint", "pieces", "book"}``.
    """
    out_dir = Path(getattr(args, "output_dir", "split_output"))
    out_dir.mkdir(parents=True, exist_ok=True)

    total_chars = sum(len(s) for _, s in pieces)
    force_book = bool(getattr(args, "book", False))
    no_book = bool(getattr(args, "no_book", False))
    make_book = force_book or (not no_book and total_chars <= _BOOK_CHAR_THRESHOLD)

    book_str: str | None = None
    if make_book:
        book_str = _assemble_book(pieces, book_label)

    primary = book_str if book_str is not None else (pieces[0][1] if pieces else "")

    # Always write individual piece files (book.txt additionally when made).
    for label, s in pieces:
        (out_dir / f"{label}.txt").write_text(s + "\n", encoding="utf-8")
    if book_str is not None:
        (out_dir / "book.txt").write_text(book_str + "\n", encoding="utf-8")

    sys.stderr.write(
        f"Piecewise output written to {out_dir}/ — {len(pieces)} piece(s), "
        f"{total_chars:,} chars total\n"
    )
    for label, s in sorted(pieces):
        sys.stderr.write(f"    {label}.txt ({len(s):,} chars)\n")
    if book_str is not None:
        sys.stderr.write(f"  blueprint book: book.txt ({len(book_str):,} chars)\n")
    else:
        sys.stderr.write(
            "  (output too large for a single book; individual pieces only)\n"
        )
    sys.stderr.write(
        "In-game wiring: place each piece next to its neighbours and wire the "
        "matching connector CCs (the ones carrying the same signal) — red wire "
        "joins the data bus, green wire joins the time/clock bus. Also join "
        "every piece's clock input to the shared clock.\n"
    )

    return {
        "blueprint": primary,
        "pieces": [{"label": label, "blueprint": s} for label, s in pieces],
        "book": book_str or "",
    }


def _encode_audio_split_pieces(args) -> list[tuple[str, str]]:
    """Encode standalone audio into piecewise (player + per-rail memory) pieces."""
    from .audio.encoder import (  # pylint: disable=import-outside-toplevel
        DRUM_TICKS_PER_PAGE, TICKS_PER_PAGE,
        _audio_rails, encode_audio_split,
    )
    from .audio.pitch_mapping import drum_grouping  # pylint: disable=import-outside-toplevel
    from . import SIGNAL_POOL, QUALITIES  # pylint: disable=import-outside-toplevel

    audio_paths = list(getattr(args, "input_paths", []))
    audio_paths = [p for p in audio_paths if _classify_input(p) in ("audio", "midi")]
    if not audio_paths:
        return []

    rail_mode: str = getattr(args, "rail_mode", "auto:0.05")
    if getattr(args, "instruments", None):
        rail_mode = args.instruments

    midi_kwargs: dict[str, object] = {
        "attach_player": True,
        "map_drums": getattr(args, "map_drums", True),
        "drum_gain": getattr(args, "drum_gain", 0.25),
        "rail_mode": rail_mode,
        "use_global_shift": not getattr(args, "no_global_shift", False),
        "ticks_per_beat": getattr(args, "ticks_per_beat", 30),
        "boost_melody": getattr(args, "boost_melody", 1.0),
        "velocity_scale": getattr(args, "velocity_scale", 1.0),
        "rearticulation_ticks": getattr(args, "rearticulation_ticks", 2),
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
        "use_basic_pitch": not getattr(args, "no_ai_transcribe", False),
        "drums": getattr(args, "drums", None) or False,
    }

    instruments, int_data_list = _audio_rails(audio_paths[0], midi_kwargs)
    if not instruments or not int_data_list:
        return []

    active_drum_pitches: list[set[int] | None] = []
    for ri, inst in enumerate(instruments):
        if "drum" in inst.lower():
            active_drum_pitches.append({
                p for p in range(48)
                if any(td[p] > 0 for td in int_data_list[ri])
            })
        else:
            active_drum_pitches.append(None)

    rail_ticks_per_page: list[int] = []
    rail_groupings: list[object | None] = []
    for ri, inst in enumerate(instruments):
        if "drum" in inst.lower():
            grp = drum_grouping(active_drum_pitches[ri]) if active_drum_pitches[ri] else None
            rail_groupings.append(grp)
            cpt = len(grp) if grp else 1
            max_page = (len(SIGNAL_POOL) * len(QUALITIES)) // max(1, cpt)
            rail_ticks_per_page.append(
                max(TICKS_PER_PAGE, min(DRUM_TICKS_PER_PAGE, max_page))
            )
        else:
            rail_groupings.append(None)
            rail_ticks_per_page.append(TICKS_PER_PAGE)

    result = encode_audio_split(
        int_data_list,
        instruments,
        output_name=args.name,
        signal_pool=list(SIGNAL_POOL),
        qualities=list(QUALITIES),
        clock_signal="signal-clock",
        map_drums=getattr(args, "map_drums", True),
        active_drum_pitches=active_drum_pitches,
        rail_ticks_per_page=rail_ticks_per_page,
        rail_groupings=rail_groupings,
    )

    pieces: list[tuple[str, str]] = [("player", result["player"])]
    pieces.extend(result["pieces"])
    return pieces


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

    all_in_one = bool(getattr(args, "all_in_one", False))
    if not all_in_one and _classify_input(first_visual) == "video":
        # ── Piecewise (chunked) output — DEFAULT ────────────────
        if power_type is not None and power_type != "none":
            sys.stderr.write(
                "Note: --power is ignored in piecewise mode (pieces are wired in game).\n"
            )
        sys.stderr.write("Piecewise mode: emitting display + memory pieces...\n")
        split_result = encode_auto(
            first_visual,
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
            split=True,
            max_piece_mb=getattr(args, "max_piece_mb", 2.0),
        )
        pieces: list[tuple[str, str]] = [("display", split_result["display"])]
        pieces.extend(split_result["pieces"])
        # Video + sound: extract embedded/standalone audio → player + memory
        # pieces, so video-with-audio works without the all-in-one composer.
        if has_any_audio and not no_audio:
            audio_td = _extract_combined_audio_tick_data(videos, audios, args)
            if audio_td:
                rail_mode = getattr(args, "rail_mode", "auto:0.05")
                if getattr(args, "instruments", None):
                    rail_mode = args.instruments
                pieces.extend(_build_audio_pieces_from_tick_data(audio_td, args, rail_mode))

        envelope = _write_split_output(pieces, args, book_label=args.name)
        if getattr(args, "json", False):
            import json as _json  # pylint: disable=import-outside-toplevel
            sys.stdout.write(_json.dumps({"split_envelope": envelope}, ensure_ascii=False))
            return
        if envelope["book"]:
            sys.stdout.write(envelope["book"] + "\n")
        elif envelope["blueprint"]:
            sys.stdout.write(envelope["blueprint"] + "\n")
        return

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

        connections.append(PortConnection("Timer", "clock", video_lb.label, "clock"))
        _connect_data_ports(connections, video_lb, display_lb)
        connections.append(PortConnection("Timer", "clock", audio_mem_lb.label, "clock"))
        connections.append(PortConnection("Timer", "clock", player_lb.label, "clock"))
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
    assert_wire_topology(final_bp, label=args.name, lb=result)
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
        "drum_gain": getattr(args, "drum_gain", 0.25),
        "rail_mode": rail_mode,
        "use_global_shift": not getattr(args, "no_global_shift", False),
        "ticks_per_beat": getattr(args, "ticks_per_beat", 30),
        "boost_melody": getattr(args, "boost_melody", 1.0),
        "velocity_scale": getattr(args, "velocity_scale", 1.0),
        "rearticulation_ticks": getattr(args, "rearticulation_ticks", 2),
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
        "use_basic_pitch": not getattr(args, "no_ai_transcribe", False),
        "drums": getattr(args, "drums", None) or False,
    }

    # For now, encode the first audio file; multi-audio concatenation
    # can be added later at the tick_data level.
    if len(audio_paths) > 1:
        sys.stderr.write(
            f"Note: {len(audio_paths)} audio inputs provided; "
            f"encoding first only. Multi-audio concatenation coming soon.\n"
        )

    all_in_one = bool(getattr(args, "all_in_one", False))
    attach_player = not getattr(args, "no_attach_player", False)

    if not all_in_one and attach_player:
        # ── Piecewise (chunked) output — DEFAULT ────────────────
        if power_type is not None and power_type != "none":
            sys.stderr.write(
                "Note: --power is ignored in piecewise mode (pieces are wired in game).\n"
            )
        pieces = _encode_audio_split_pieces(args)
        if not pieces:
            return
        envelope = _write_split_output(pieces, args, book_label=args.name)
        if getattr(args, "json", False):
            import json as _json  # pylint: disable=import-outside-toplevel
            sys.stdout.write(_json.dumps({"split_envelope": envelope}, ensure_ascii=False))
            return
        if envelope["book"]:
            sys.stdout.write(envelope["book"] + "\n")
        elif envelope["blueprint"]:
            sys.stdout.write(envelope["blueprint"] + "\n")
        if output_path:
            sys.stderr.write(
                f"Note: -o is ignored in piecewise mode; pieces are written to "
                f"{getattr(args, 'output_dir', 'split_output')}/.\n"
            )
        return

    if power_type is not None:
        midi_kwargs["attach_player"] = False

        # Determine the rails (instruments + per-rail tick data) so the
        # composed blueprint can carry ALL of them — one memory bank and one
        # player rail per instrument (multi-rail by default).  MIDI and
        # Basic-Pitch transcriptions may yield several rails (e.g. piano +
        # drum); plain audio falls back to a single piano rail.
        from .audio.encoder import (  # pylint: disable=import-outside-toplevel
            DRUM_TICKS_PER_PAGE, TICKS_PER_PAGE,
            _audio_rails, encode_audio_to_logical,
        )
        from .audio.pitch_mapping import drum_grouping
        from .audio.player_blueprint import build_multi_rail_decoder_logical
        from . import SIGNAL_POOL, QUALITIES  # pylint: disable=import-outside-toplevel

        instruments, int_data_list = _audio_rails(audio_paths[0], midi_kwargs)
        if not instruments or not int_data_list:
            return

        # For drum rails, only store/place the drum TYPES the song actually
        # uses (at most the 17 Factorio drum-kit sounds — not 48 placeholders).
        # The memory chunk packs just those loudnesses 4-per-cell and the
        # decoder builds only the channels/speakers that are used.
        active_drum_pitches: list[set[int] | None] = []
        for ri, inst in enumerate(instruments):
            if "drum" in inst.lower():
                active_drum_pitches.append({
                    p for p in range(48)
                    if any(td[p] > 0 for td in int_data_list[ri])
                })
            else:
                active_drum_pitches.append(None)

        # Drum rails only record each used drum's loudness (1 cell/tick), so
        # their pages can span many more ticks per DC than the 60-tick melodic
        # pages — far fewer DCs for the same song.  Clamp to the signal pool.
        rail_ticks_per_page: list[int] = []
        for ri, inst in enumerate(instruments):
            if "drum" in inst.lower():
                cpt = len(drum_grouping(active_drum_pitches[ri])) if active_drum_pitches[ri] else 1
                max_page = (len(SIGNAL_POOL) * len(QUALITIES)) // max(1, cpt)
                rail_ticks_per_page.append(
                    max(TICKS_PER_PAGE, min(DRUM_TICKS_PER_PAGE, max_page))
                )
            else:
                rail_ticks_per_page.append(TICKS_PER_PAGE)

        mem_lbs: list[LogicalBlueprint] = []
        for ri, int_data in enumerate(int_data_list):
            grouping = (
                drum_grouping(active_drum_pitches[ri])
                if active_drum_pitches[ri] is not None else None
            )
            mem = encode_audio_to_logical(
                int_data, f"{args.name} r{ri}",
                signal_pool=list(SIGNAL_POOL), qualities=list(QUALITIES),
                clock_signal="signal-clock", id_prefix=f"r{ri}_",
                grouping=grouping,
                ticks_per_page=rail_ticks_per_page[ri],
            )
            mem.label = f"Audio Memory {ri}"
            mem_lbs.append(mem)
            if debug_dir:
                _debug_dump_toml(mem, f"01_memory_r{ri}", debug_dir)

        components: list[LogicalBlueprint] = []
        connections: list[PortConnection] = []

        total_ticks = max(
            (_extract_total_ticks(m) for m in mem_lbs), default=0,
        )
        timer = _build_timer_for_memory(mem_lbs[0], total_ticks=total_ticks)
        components.append(timer)
        if debug_dir:
            _debug_dump_toml(timer, "02_timer", debug_dir)

        if use_progress:
            if total_ticks < 1:
                total_ticks = 60
            from .progress_bar import build_progress_bar
            pb = build_progress_bar("Progress", length=10,
                                    signal_name="signal-clock", max_value=total_ticks)
            components.append(pb)
            if debug_dir:
                _debug_dump_toml(pb, "03_progress", debug_dir)
            connections.append(PortConnection("Timer", "sub_tick", "Progress", "in"))

        for mem in mem_lbs:
            components.append(mem)
            connections.append(PortConnection("Timer", "clock", mem.label, "clock"))

        player_lb = build_multi_rail_decoder_logical(
            name=f"Audio Player: {args.name}",
            instruments=instruments,
            clock_signal="signal-clock",
            map_drums=getattr(args, "map_drums", True),
            active_drum_pitches=active_drum_pitches,
            ticks_per_page=rail_ticks_per_page,
        )
        components.append(player_lb)
        connections.append(PortConnection("Timer", "clock", player_lb.label, "clock"))
        for ri in range(len(mem_lbs)):
            connections.append(
                PortConnection(mem_lbs[ri].label, "data", player_lb.label, f"data_{ri}")
            )

        cache_key_parts = (
            "audio", args.name, ",".join(instruments),
            str(sum(len(td) for td in int_data_list)),
        )
        result = compose(
            components=components,
            connections=connections,
            output_name=args.name,
            pole_type=power_type,
            use_cache=use_cache,
            cache_key_parts=cache_key_parts,
        )
        # Deterministically re-align the memory banks / timer and re-wire the
        # clock/data buses so every wire stays within Factorio's 9-tile limit.
        _finalize_audio_composition(result)
        if debug_dir:
            _debug_dump_toml(result, "04_merged", debug_dir)
        final_bp2 = to_draftsman(result)
        from .logical_blueprint import assert_wire_topology
        assert_wire_topology(final_bp2, label=args.name, lb=result)
        output = _to_fixed_string(final_bp2) + "\n"
        from .logical_blueprint import assert_wire_topology
        assert_wire_topology(final_bp2, label=args.name, lb=result)
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


def _extract_combined_audio_tick_data(
    videos: list[str],
    standalone_audios: list[str],
    args,
) -> list[list[int]] | None:
    """Extract audio from *videos* (timeline-aligned) + *standalone_audios*.

    Returns a single combined 48-channel tick-data list, or ``None`` when
    there is no audible audio to encode.  Shared by the all-in-one composer
    and the piecewise (default) path so video+sound is supported either way.
    """
    import tempfile
    import shutil

    all_tick_data: list[list[int]] = []
    cumulative_tick = 0

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
        return None
    return all_tick_data


def _build_audio_pieces_from_tick_data(
    all_tick_data: list[list[int]],
    args,
    rail_mode: str,
) -> list[tuple[str, str]]:
    """Build piecewise audio pieces (player + memory) from combined tick data.

    Mirrors the all-in-one audio composition: extracted audio is treated as
    a single rail (default ``piano``).
    """
    from . import SIGNAL_POOL, QUALITIES  # pylint: disable=import-outside-toplevel
    from .audio.encoder import encode_audio_split  # pylint: disable=import-outside-toplevel

    instrument = rail_mode.split(",")[0].strip() if "," in rail_mode else rail_mode
    if ":" in instrument:
        instrument = instrument.split(":")[0]
    if instrument in ("auto", "all"):
        instrument = "piano"

    result = encode_audio_split(
        [all_tick_data],
        [instrument],
        output_name=args.name,
        signal_pool=list(SIGNAL_POOL),
        qualities=list(QUALITIES),
        clock_signal="signal-clock",
        map_drums=getattr(args, "map_drums", True),
    )
    pieces: list[tuple[str, str]] = [("player", result["player"])]
    pieces.extend(result["pieces"])
    return pieces


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
    from . import SIGNAL_POOL, QUALITIES
    from .audio.encoder import encode_audio_to_logical
    from .audio.player_blueprint import build_audio_decoder_logical

    rail_mode: str = getattr(args, "rail_mode", "auto:0.05")
    if getattr(args, "instruments", None):
        rail_mode = args.instruments

    all_tick_data = _extract_combined_audio_tick_data(videos, standalone_audios, args)
    if all_tick_data is None:
        return None, None, 0

    total_audio_ticks = max(len(all_tick_data), video_total_ticks)

    # Pad to match video length if needed
    while len(all_tick_data) < total_audio_ticks:
        all_tick_data.append([0] * 48)

    # Encode audio memory
    # Use single-rail piano for extracted audio by default
    instrument = rail_mode.split(",")[0].strip() if "," in rail_mode else rail_mode
    if ":" in instrument:
        instrument = instrument.split(":")[0]
    if instrument in ("auto", "all"):
        instrument = "piano"

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
    _declare_memory_ports(audio_mem_lb, clock_color="green")
    _restore_memory_prewiring(audio_mem_lb)

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
