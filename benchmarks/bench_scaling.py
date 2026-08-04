"""Measure end-to-end blueprint-generation scaling with display size.

Production mode (validations OFF): measures display build + compose +
to_draftsman.  If generation is now ~linear, time-per-lamp should be flat
instead of climbing superlinearly like the old O(n²) reachability pass.

Run with FACTORIO_DISPLAY_DEBUG_VALIDATE=1 to compare the validated path.
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np

import factorio_display as fd
from factorio_display.video.player_blueprint import build_display_logical
from factorio_display.timer import build_raw_timer, build_mod_timer
from factorio_display.video.encoder import _encode_frames_core
from factorio_display.cli import _declare_memory_ports, _connect_data_ports
from factorio_display.composer import compose, PortConnection
from factorio_display.logical_blueprint import to_draftsman
from factorio_display import CLOCK_SIGNAL, SIGNAL_POOL, QUALITIES

N_FRAMES = 10


def measure(w, h):
    rng = np.random.default_rng(7)
    t0 = time.perf_counter()
    disp = build_display_logical(name="Display", width=w, height=h)
    t_disp = time.perf_counter() - t0

    frames = [rng.integers(0, 256, (h, w, 3), dtype=np.uint8) for _ in range(N_FRAMES)]
    tick = [(i * 60, i * 60) for i in range(N_FRAMES)]
    mp = {"width": w, "height": h, "qualities": QUALITIES,
          "signal_pool": SIGNAL_POOL}
    vid = _encode_frames_core(kept_frames=frames, tick_ranges=tick,
                              output_name="VM", deduplicate=False,
                              mapping_params=mp, clock=CLOCK_SIGNAL,
                              current_tick=N_FRAMES * 60)
    _declare_memory_ports(vid)
    t = build_raw_timer("Timer", with_kick=False)
    t.output_ports["raw"] = t.output_ports.pop("out")
    m = build_mod_timer(N_FRAMES * 60 + 1, name="SubTick")
    t.merge(m, entity_prefix="mod_", network_prefix="mod_")
    t.output_ports["clock_red"] = t.output_ports.pop("out")
    disp.label = "Display"
    conns = [PortConnection("Timer", "clock_red", vid.label, "clock")]
    _connect_data_ports(conns, vid, disp)

    t0 = time.perf_counter()
    merged = compose(components=[t, vid, disp], connections=conns,
                     output_name="Bench", use_cache=False)
    t_compose = time.perf_counter() - t0

    t0 = time.perf_counter()
    to_draftsman(merged)
    t_bp = time.perf_counter() - t0

    n_ent = len(merged.entities)
    total = t_disp + t_compose + t_bp
    return n_ent, t_disp, t_compose, t_bp, total


def main():
    print(f"mode: {'VALIDATED' if os.environ.get('FACTORIO_DISPLAY_DEBUG_VALIDATE') in ('1','true','yes','on') else 'FAST (validations off)'}")
    print(f"{'display':>10} {'ents':>6} {'display':>8} {'compose':>8} {'to_draftsman':>11} {'total':>8} {'ms/ent':>7}")
    for (w, h) in [(14, 13), (22, 20), (28, 26), (30, 30)]:
        n, td, tc, tb, tot = measure(w, h)
        print(f"{str(w)+'x'+str(h):>10} {n:>6} {td:8.3f} {tc:8.3f} {tb:11.3f} {tot:8.3f} {tot/n*1000:7.2f}")


if __name__ == "__main__":
    main()
