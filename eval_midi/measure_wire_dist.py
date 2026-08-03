"""Measure all wire distances in a blueprint; report segments > 9 tiles."""

from __future__ import annotations

import base64
import json
import sys
import zlib
from pathlib import Path

path = Path(sys.argv[1]) if len(sys.argv) > 1 else "eval_midi/out/real/iron_soul_r2_allinone.txt"
text = Path(path).read_text(encoding="utf-8").strip()
body = text[1:] if text[:1].isdigit() else text
data = json.loads(zlib.decompress(base64.b64decode(body)))
bp_json = data.get("blueprint", data)
ents = bp_json.get("entities", [])
wires = bp_json.get("wires", [])

pos = {}
for i, e in enumerate(ents):
    p = e.get("position", {})
    pos[i + 1] = (p.get("x", 0), p.get("y", 0))

too_long = []
maxd = 0.0
for w in wires:
    a, _ca, b, _cb = w
    pa, pb = pos.get(a), pos.get(b)
    if pa is None or pb is None:
        continue
    d = max(abs(pa[0] - pb[0]), abs(pa[1] - pb[1]))
    maxd = max(maxd, d)
    if d > 9.0:
        too_long.append((a, b, round(d, 1)))

print(f"total wires: {len(wires)}")
print(f"max chebyshev distance: {maxd:.1f}")
print(f"wires > 9 tiles: {len(too_long)}")
for a, b, d in too_long[:40]:
    print(f"  wire #{a} <-> #{b} : {d}")
