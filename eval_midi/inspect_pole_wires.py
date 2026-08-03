"""Examine wires touching poles in a blueprint (top-level 'wires' format).

Factorio 2.0 wire entry: [entity_number_1, connector_1, entity_number_2, connector_2]
connector ids: 1=red, 2=green, 3=power(copper); +2 for output side on combinators
(combinators: 1=red in, 2=green in, 3=red out, 4=green out).
"""

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

num_to_name = {i + 1: e.get("name") for i, e in enumerate(ents)}
num_to_pos = {i + 1: tuple(e.get("position", {}).values()) for i, e in enumerate(ents)}
pole_nums = {i + 1 for i, e in enumerate(ents) if "pole" in e.get("name", "") or "substation" in e.get("name", "")}

def color(cid: int) -> str:
    return {1: "red", 2: "green", 3: "copper", 4: "green-out"}.get(cid, str(cid))

print(f"poles: {len(pole_nums)}")
for pn in sorted(pole_nums):
    # wires touching this pole
    mine = [w for w in wires if w[0] == pn or w[2] == pn]
    print(f"\npole #{pn} {num_to_name[pn]} @ {num_to_pos[pn]}  ({len(mine)} wires)")
    for w in mine:
        a, ca, b, cb = w
        other = b if a == pn else a
        ocon = cb if a == pn else ca
        # distance
        pa, pb = num_to_pos[a], num_to_pos[b]
        d = max(abs(pa[0] - pb[0]), abs(pa[1] - pb[1]))
        print(f"   <-> #{other} {num_to_name[other]} @ {num_to_pos[other]} "
              f"conn={color(ocon)} chebyshev={d:.1f}")
