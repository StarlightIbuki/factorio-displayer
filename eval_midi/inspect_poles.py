"""Inspect pole/substation entities in a blueprint and their wiring."""

from __future__ import annotations

import sys
from pathlib import Path

from draftsman.blueprintable import Blueprint

path = Path(sys.argv[1]) if len(sys.argv) > 1 else "eval_midi/out/real/iron_soul_r2_allinone.txt"
bp = Blueprint.from_string(Path(path).read_text(encoding="utf-8").strip())

poles = [e for e in bp.entities if "pole" in e.name or "substation" in e.name]
print(f"total entities: {len(bp.entities)}")
print(f"pole/substation entities: {len(poles)}")

print("\n--- per-pole connection counts (circuit wires) ---")
total_pole_circuit = 0
total_pole_power = 0
for e in poles:
    conns = getattr(e, "connections", None)
    red = getattr(conns, "red", None)
    green = getattr(conns, "green", None)
    copper = getattr(conns, "copper", None)
    n_red = len(red) if red else 0
    n_green = len(green) if green else 0
    n_copper = len(copper) if copper else 0
    total_pole_circuit += n_red + n_green
    total_pole_power += n_copper
    print(f"  pole@{e.tile_position} red={n_red} green={n_green} copper={n_copper}")

print(f"\nTOTAL pole circuit wires (red+green): {total_pole_circuit}")
print(f"TOTAL pole copper (power) wires: {total_pole_power}")
