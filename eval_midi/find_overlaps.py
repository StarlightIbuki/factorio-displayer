"""Find overlapping pole/combinator entities in a blueprint (raw JSON)."""

from __future__ import annotations

import base64
import json
import sys
import zlib
from pathlib import Path

path = Path(sys.argv[1]) if len(sys.argv) > 1 else "eval_midi/out/real/iron_soul_r2_allinone_fixed.txt"
text = Path(path).read_text(encoding="utf-8").strip()
body = text[1:] if text[:1].isdigit() else text
data = json.loads(zlib.decompress(base64.b64decode(body)))
ents = data.get("blueprint", data).get("entities", [])

# For overlap in tile space, convert each entity's JSON position to its
# occupied tiles. 1x2 combinators (decider/arithmetic) have position = center
# (x+0.5, y+1.0) -> tiles (round(x-0.5), round(y-1.0)) and (round(x-0.5), round(y-0.0)).
# 1x1 entities have position (x+0.5, y+0.5) -> tile (round(x-0.5), round(y-0.5)).
def tiles(ent):
    p = ent.get("position", {})
    x, y = p.get("x", 0), p.get("y", 0)
    name = ent.get("name", "")
    if name in ("decider-combinator", "arithmetic-combinator"):
        tx = round(x - 0.5)
        ty = round(y - 1.0)
        return [(tx, ty), (tx, ty + 1)]
    return [(round(x - 0.5), round(y - 0.5))]

occupied = {}
for i, ent in enumerate(ents):
    for t in tiles(ent):
        occupied.setdefault(t, []).append((i + 1, ent.get("name")))

print("overlaps (tile -> entities):")
found = 0
for t, lst in occupied.items():
    if len(lst) > 1:
        found += 1
        print(f"  tile {t}: {lst}")
print("total overlapping tiles:", found)
