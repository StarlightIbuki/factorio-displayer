"""Identify entities involved in wires longer than 9 tiles."""

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

info = {}
for i, e in enumerate(ents):
    p = e.get("position", {})
    info[i + 1] = (e.get("name"), p.get("x", 0), p.get("y", 0))

def color(cid):
    return {1: "red-in", 2: "green-in", 3: "red-out", 4: "green-out"}.get(cid, cid)

too_long = []
for w in wires:
    a, ca, b, cb = w
    pa, pb = info.get(a), info.get(b)
    if pa is None or pb is None:
        continue
    d = max(abs(pa[1] - pb[1]), abs(pa[2] - pb[2]))
    if d > 9.0:
        too_long.append((a, b, round(d, 1)))

print(f"wires > 9 tiles: {len(too_long)}")
for a, b, d in too_long:
    na = f"{info[a][0]}@{info[a][1]},{info[a][2]}"
    nb = f"{info[b][0]}@{info[b][1]},{info[b][2]}"
    print(f"  #{a}({na}) <-> #{b}({nb}) : {d}")
