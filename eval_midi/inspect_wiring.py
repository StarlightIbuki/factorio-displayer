"""Check wiring in a blueprint: draftsman API vs raw JSON."""

from __future__ import annotations

import base64
import json
import sys
import zlib
from pathlib import Path

from draftsman.blueprintable import Blueprint

path = Path(sys.argv[1]) if len(sys.argv) > 1 else "eval_midi/out/real/iron_soul_r2_allinone.txt"
text = Path(path).read_text(encoding="utf-8").strip()

bp = Blueprint.from_string(text)
print(f"blueprint: {path.name}")
print(f"draftsman entity count: {len(bp.entities)}")

# raw JSON decode (Factorio: "<version byte><base64 of zlib(json)>")
try:
    body = text[1:] if text[:1].isdigit() else text
    data = json.loads(zlib.decompress(base64.b64decode(body)))
except Exception as exc:  # noqa: BLE001
    print("raw decode failed:", exc)
    raise SystemExit
bp_json = data.get("blueprint", data)
ents = bp_json.get("entities", [])
wires = bp_json.get("wires", [])
print(f"raw entities: {len(ents)}, top-level wires: {len(wires)}")

# wire entries reference entity_number (1-based). Map to entity names.
num_to_name = {i + 1: e.get("name") for i, e in enumerate(ents)}
num_to_pole = {
    i + 1: e.get("position") for i, e in enumerate(ents)
    if "pole" in e.get("name", "") or "substation" in e.get("name", "")
}
pole_wires = 0
sample = 0
for w in wires:
    if w[0] in num_to_pole or w[2] in num_to_pole:
        pole_wires += 1
    if sample < 10:
        print(f"  wire {w} -> {num_to_name.get(w[0])} <-> {num_to_name.get(w[2])}")
        sample += 1
print(f"wires touching poles: {pole_wires} / {len(wires)}")

# raw per-entity connections (legacy format) — usually absent in 1.1/2.0
wired = sum(1 for ent in ents if ent.get("connections"))
print(f"entities with legacy 'connections' field: {wired}")
