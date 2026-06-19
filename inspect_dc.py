"""Quick inspection of audio memory DC outputs."""
import sys
sys.path.insert(0, "src")

from draftsman.blueprintable import Blueprint
from factorio_display.audio.encoder import unpack_four

text = open("twinkle.txt", encoding="utf-8").read()
lines = text.split("\n")
bp_lines = [l for l in lines if l.startswith("0e")]

# BP1 = audio memory
bp = Blueprint.from_string(bp_lines[1])
dcs = [e for e in bp.entities if "decider-combinator" in e.name]

dc0 = dcs[0]
print(f"DC[0] conditions: {[(c.comparator, c.constant) for c in dc0.conditions]}")
print(f"DC[0] outputs: {len(dc0.outputs)}")
print()

# Group outputs by tick (cell_offset // 12 = tick, cell_offset % 12 = channel)
# We need to map signal back to cell_offset.
# The signal ordering: cell_offset 0 -> signal_pool[0], quality[0]
#                       cell_offset 1 -> signal_pool[0], quality[1]
#                       ...
#                       cell_offset 5 -> signal_pool[1], quality[0]
# cell_offset = signal_idx * 5 + quality_idx

from factorio_display import SIGNAL_POOL, QUALITIES

QUAL_MAP = {q: i for i, q in enumerate(QUALITIES)}
SIG_MAP = {s: i for i, s in enumerate(SIGNAL_POOL)}

# Sort outputs by cell_offset
def cell_offset_of(output):
    sig_name = output.signal.name
    sig_qual = output.signal.quality
    sig_idx = SIG_MAP.get(sig_name, 99999)
    qual_idx = QUAL_MAP.get(sig_qual, 0)
    return sig_idx * len(QUALITIES) + qual_idx

sorted_outs = sorted(dc0.outputs, key=cell_offset_of)

print("First 15 outputs (by cell_offset):")
for o in sorted_outs[:15]:
    co = cell_offset_of(o)
    tick = co // 12
    ch = co % 12
    u = unpack_four(o.constant)
    print(f"  cell={co:4d}  tick={tick:2d}  ch={ch:2d}  val={o.constant:10d}  unpack={u}")

print()
print("Checking sustained F4 (ch0):")
f4_outs = [o for o in sorted_outs if cell_offset_of(o) % 12 == 0 and unpack_four(o.constant)[1] > 0]
print(f"  Found {len(f4_outs)} occurrences of F4 in ch0")
for o in f4_outs[:5]:
    co = cell_offset_of(o)
    tick = co // 12
    print(f"    tick={tick} val={o.constant} unpack_l2={unpack_four(o.constant)[1]}")
