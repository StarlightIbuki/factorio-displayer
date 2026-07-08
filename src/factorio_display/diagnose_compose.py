"""Diagnostic: dump intermediate LogicalBlueprints using synthetic data.

Usage:
    .venv\Scripts\python.exe -m factorio_display.diagnose_compose
"""

import sys
from pathlib import Path

import numpy as np

from factorio_display.logical_blueprint import (
    from_draftsman, to_toml, to_draftsman,
)
from factorio_display.composer import _entity_bounding_box as _bb
from factorio_display.video.player_blueprint import build_display
from factorio_display.video.encoder import encode_frames, _to_fixed_string
from factorio_display.timer import build_raw_timer, build_mod_timer
from factorio_display.progress_bar import build_progress_bar
from factorio_display.composer import Composer, _connect_nets_by_color

from draftsman.blueprintable import Blueprint

# Small display for quick diagnostics
W, H = 8, 6  # 48 lamps


def main():
    out_dir = Path("_diagnose")
    out_dir.mkdir(exist_ok=True)

    print("=== Step 1: Building sub-blueprints ===")

    # ── Display ───────────────────────────────────────────────────
    display_bp = build_display(name="Display", width=W, height=H)
    display_lb = from_draftsman(display_bp)

    red_nets = [n for n in display_lb.networks if n.color == "red" and n.endpoints]
    print(f"  Display: {len(display_lb.entities)} entities, {len(red_nets)} red networks")
    for net in red_nets:
        lamp_eps = [ep for ep in net.endpoints if "lamp" in ep.entity_id]
        print(f"    {net.network_id}: {len(net.endpoints)} total eps, "
              f"{len(lamp_eps)} lamp eps")
        for ep in net.endpoints[:5]:
            ent = display_lb.entities.get(ep.entity_id)
            print(f"      {ep.to_string()} pos={ent.position if ent else '?'}")
    _dump(display_lb, out_dir / "01_display.toml")

    bb = _bb(display_lb)
    print(f"  Display bounds: x=[{bb[0]},{bb[2]}], y=[{bb[1]},{bb[3]}]")

    # ── Synthetic video memory (3 frames) ─────────────────────────
    frames = []
    for i in range(3):
        frame = np.zeros((H, W, 3), dtype=np.uint8)
        frame[0, i, :] = [255, 0, 0]
        frame[1, i, :] = [0, 255, 0]
        frames.append(frame)

    print(f"\n  Encoding {len(frames)} synthetic frames ({W}x{H})...")
    video_bp = encode_frames(
        frames, output_name="Synthetic", fps=3.0,
        total_width=W, total_height=H, deduplicate=False,
    )
    if not video_bp:
        print("  ERROR: encode_frames returned empty Blueprint!")
        return
    video_lb = from_draftsman(video_bp)

    red_nets_v = [n for n in video_lb.networks if n.color == "red" and n.endpoints]
    green_nets_v = [n for n in video_lb.networks if n.color == "green" and n.endpoints]
    print(f"  Video memory: {len(video_lb.entities)} entities, "
          f"{len(red_nets_v)} red, {len(green_nets_v)} green networks")
    for net in red_nets_v:
        print(f"    red {net.network_id}: {len(net.endpoints)} eps")
        for ep in net.endpoints[:5]:
            ent = video_lb.entities.get(ep.entity_id)
            print(f"      {ep.to_string()} pos={ent.position if ent else '?'}")
    _dump(video_lb, out_dir / "02_video_memory.toml")

    # ── Timer: raw self-loop → mod ───────────────────────────────
    timer_lb = build_raw_timer("Clock")
    mod_lb = build_mod_timer(60, name="SubTick")  # diagnostic: fixed interval
    timer_lb.merge(mod_lb, entity_prefix="mod_", network_prefix="mod_")
    _connect_nets_by_color(
        timer_lb, "red",
        entity_contains="clock_", port="output",
        other_entity_contains="mod_sub", other_port="input",
    )
    print(f"\n  Timer: {len(timer_lb.entities)} entities, {len(timer_lb.networks)} networks")
    for net in timer_lb.networks:
        print(f"    {net.network_id}({net.color}): {len(net.endpoints)} eps: "
              f"{[e.to_string() for e in net.endpoints]}")
    _dump(timer_lb, out_dir / "03_timer.toml")

    # ── Progress bar ──────────────────────────────────────────────
    progress_lb = build_progress_bar("Progress", length=8, signal_name="signal-clock", max_value=59)
    print(f"\n  Progress bar: {len(progress_lb.entities)} entities")
    _dump(progress_lb, out_dir / "04_progress.toml")

    # ═══════════════════════════════════════════════════════════════
    print("\n=== Step 2: Composing ===")
    composer = Composer(output_name="Diagnostic")
    composer.set_display(display_lb)
    composer.set_video_memory(video_lb)
    composer.set_timer(timer_lb)
    composer.set_progress_bar(progress_lb)

    result = composer.compose()
    _dump(result, out_dir / "05_merged.toml")

    red_nets_m = [n for n in result.networks if n.color == "red" and n.endpoints]
    green_nets_m = [n for n in result.networks if n.color == "green" and n.endpoints]
    print(f"  Merged: {len(result.entities)} entities, "
          f"{len(red_nets_m)} red nets, {len(green_nets_m)} green nets")

    positioned = sum(1 for e in result.entities.values() if e.position is not None)
    unpositioned = sum(1 for e in result.entities.values() if e.position is None)
    print(f"  Positioned: {positioned}, Unpositioned: {unpositioned}")

    bb_m = _bb(result)
    print(f"  Bounding box: x=[{bb_m[0]},{bb_m[2]}], y=[{bb_m[1]},{bb_m[3]}]")

    # ── Show WHERE things are ─────────────────────────────────────
    print("\n  Entity positions by prefix:")
    prefixes = {}
    for eid, ent in result.entities.items():
        if "_" in eid:
            pf = eid.split("_")[0] + "_"
        else:
            pf = eid
        prefixes.setdefault(pf, []).append(ent.position)
    for pf, positions in sorted(prefixes.items()):
        pos_set = set(positions)
        xs = [p[0] for p in pos_set if p is not None]
        ys = [p[1] for p in pos_set if p is not None]
        if xs:
            print(f"    {pf}: {len(positions)} entities, "
                  f"x=[{min(xs)},{max(xs)}], y=[{min(ys)},{max(ys)}]")

    # ═══════════════════════════════════════════════════════════════
    print("\n=== Step 3: Lamp connectivity in merged ===")
    lamp_ents = {eid: ent for eid, ent in result.entities.items()
                 if ent.type == "small-lamp"}
    print(f"  Total lamps: {len(lamp_ents)}")

    lamp_networks = []
    for net in result.networks:
        if net.color != "red":
            continue
        lamp_eps = [ep for ep in net.endpoints if ep.entity_id in lamp_ents]
        if lamp_eps:
            lamp_networks.append((net, lamp_eps))

    print(f"  Networks containing lamps: {len(lamp_networks)}")
    for net, eps in lamp_networks:
        print(f"    {net.network_id}: {len(eps)} lamps, "
              f"{len(net.endpoints)} total endpoints")
        for ep in eps[:5]:
            ent = result.entities.get(ep.entity_id)
            print(f"      {ep.to_string()} pos={ent.position}")

    # ═══════════════════════════════════════════════════════════════
    print("\n=== Step 4: Materialize + round-trip ===")
    bp_out = to_draftsman(result)
    bp_str = _to_fixed_string(bp_out)
    out_dir.joinpath("06_final_bp.txt").write_text(bp_str, encoding="utf-8")

    bp_check = Blueprint.from_string(bp_str)
    lb_check = from_draftsman(bp_check)
    _dump(lb_check, out_dir / "07_roundtrip.toml")

    red_nets_c = [n for n in lb_check.networks if n.color == "red" and n.endpoints]
    print(f"  Round-trip: {len(lb_check.entities)} entities, {len(red_nets_c)} red nets")

    lamp_ents_c = {eid: ent for eid, ent in lb_check.entities.items()
                   if ent.type == "small-lamp"}
    lamp_nets_c = []
    for net in red_nets_c:
        lamp_eps = [ep for ep in net.endpoints if ep.entity_id in lamp_ents_c]
        if lamp_eps:
            lamp_nets_c.append((net, lamp_eps))
    print(f"  Lamp networks after round-trip: {len(lamp_nets_c)}")
    for net, eps in lamp_nets_c[:5]:
        print(f"    {net.network_id}: {len(eps)} lamps")

    if len(lamp_nets_c) >= 1:
        print(f"  ✓ Lamps on {len(lamp_nets_c)} separate networks (display data + progress sub-tick)")
    else:
        print(f"  ✗ Lamps split across {len(lamp_nets_c)} networks!")

    print("\n  Wire connections (from round-trip):")
    for net in red_nets_c:
        eps = net.endpoints
        lamp_count = sum(1 for ep in eps if ep.entity_id in lamp_ents_c)
        other_count = len(eps) - lamp_count
        print(f"    {net.network_id}: {len(eps)} total ({lamp_count} lamps, "
              f"{other_count} other)")

    print(f"\nDone. Diagnostic files in: {out_dir}/")


def _dump(lb, path: Path):
    toml_str = to_toml(lb)
    if len(toml_str) > 200_000:
        lines = toml_str.split("\n")
        toml_str = "\n".join(lines[:200]) + "\n\n... (truncated) ...\n\n" + "\n".join(lines[-100:])
    path.write_text(toml_str, encoding="utf-8")


if __name__ == "__main__":
    main()
