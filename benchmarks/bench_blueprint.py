"""Benchmark the blueprint-building pipeline stage by stage + cProfile.

Run:
    .venv\\Scripts\\python.exe bench_blueprint.py            # timed stages
    .venv\\Scripts\\python.exe bench_blueprint.py --profile  # cProfile hotspots
"""
from __future__ import annotations

import sys
import time

import numpy as np


def timeit(label, fn):
    t0 = time.perf_counter()
    result = fn()
    dt = time.perf_counter() - t0
    print(f"{label:<55} {dt:8.3f}s")
    return dt, result


def main() -> None:
    import factorio_display as fd
    from factorio_display.logical_blueprint import (
        LogicalBlueprint, to_draftsman, from_draftsman,
    )
    from factorio_display.video.player_blueprint import build_display_logical
    from factorio_display.timer import build_raw_timer, build_mod_timer
    from factorio_display.composer import (
        compose, PortConnection, _assign_tile_positions, _connect_nets_by_color,
    )
    from factorio_display.video.encoder import _encode_frames_core
    from factorio_display import CLOCK_SIGNAL, SIGNAL_POOL, QUALITIES

    W, H = 28, 26          # default display size
    N_FRAMES = 30          # seconds of 28x26 animation at 1 fps-skip

    rng = np.random.default_rng(7)

    # ── Display ──────────────────────────────────────────────────
    _, display_lb = timeit("build_display_logical(%dx%d)" % (W, H),
                           lambda: build_display_logical(name="Display", width=W, height=H))
    print(f"    display entities: {len(display_lb.entities)}, networks: {len(display_lb.networks)}")

    _, display_bp = timeit("to_draftsman(display_lb)",
                           lambda: to_draftsman(display_lb))

    # ── Video memory ─────────────────────────────────────────────
    frames = [rng.integers(0, 256, (H, W, 3), dtype=np.uint8) for _ in range(N_FRAMES)]
    tick_ranges = [(i * 60, i * 60) for i in range(N_FRAMES)]
    mapping_params = {
        "width": W, "height": H,
        "qualities": QUALITIES,
        "signal_pool": SIGNAL_POOL or [f"sig-{i:04d}" for i in range(300)],
    }
    _, video_lb = timeit(
        "_encode_frames_core(%d frames %dx%d)" % (N_FRAMES, W, H),
        lambda: _encode_frames_core(
            kept_frames=frames, tick_ranges=tick_ranges,
            output_name="VideoMem", deduplicate=False,
            mapping_params=mapping_params,
            clock=CLOCK_SIGNAL, current_tick=N_FRAMES * 60,
        ),
    )
    print(f"    video entities: {len(video_lb.entities)}, networks: {len(video_lb.networks)}")

    # ── Timer ────────────────────────────────────────────────────
    total_ticks = N_FRAMES * 60

    def build_timer():
        timer = build_raw_timer("Timer", with_kick=False)
        timer.output_ports["raw"] = timer.output_ports.pop("out")
        mod = build_mod_timer(total_ticks + 1, name="SubTick")
        timer.merge(mod, entity_prefix="mod_", network_prefix="mod_")
        timer.output_ports["clock_red"] = timer.output_ports.pop("out")
        return timer

    _, timer_lb = timeit("build_raw_timer + build_mod_timer", build_timer)

    # ── Compose ──────────────────────────────────────────────────
    display_lb.label = "Display"
    from factorio_display.cli import _declare_memory_ports, _connect_data_ports
    _, _ = timeit("_declare_memory_ports(video_lb)",
                  lambda: _declare_memory_ports(video_lb))
    components = [timer_lb, video_lb, display_lb]
    connections = [
        PortConnection("Timer", "clock_red", video_lb.label, "clock"),
    ]
    _connect_data_ports(connections, video_lb, display_lb)

    _, merged_lb = timeit(
        "compose(timer + video_mem + display)",
        lambda: compose(components=components, connections=connections,
                        output_name="Bench", use_cache=False),
    )
    print(f"    merged entities: {len(merged_lb.entities)}, networks: {len(merged_lb.networks)}")

    # ── Final serialization + topology assert ────────────────────
    _, final_bp = timeit("to_draftsman(merged_lb)",
                         lambda: to_draftsman(merged_lb))
    from factorio_display.logical_blueprint import assert_wire_topology
    _, _ = timeit("assert_wire_topology(final_bp)",
                  lambda: assert_wire_topology(final_bp, label="Bench", lb=merged_lb))
    bp_str = final_bp.to_string()
    print(f"    final blueprint string length: {len(bp_str)}")
    print(f"    final blueprint entities: {len(final_bp.entities)}, "
          f"wires: {len(final_bp.wires)}")


if __name__ == "__main__":
    if "--profile" in sys.argv:
        import cProfile
        import pstats
        import io
        pr = cProfile.Profile()
        pr.enable()
        main()
        pr.disable()
        s = io.StringIO()
        ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
        ps.print_stats(60)
        print(s.getvalue())
    else:
        main()
