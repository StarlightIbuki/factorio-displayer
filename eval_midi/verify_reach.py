"""Reach-aware verification for factorio-display blueprint strings.

Wires are valid iff the Euclidean distance between the two entities'
serialized positions is <= min(circuit_wire_max_distance) of the two
entities.  For a legendary small electric pole that reach is 17.5 tiles;
for a normal combinator/speaker it is 9 tiles.  This matches draftsman's
own ConnectionDistanceWarning logic (it compares against
min(entity1.circuit_wire_max_distance, entity2.circuit_wire_max_distance)).
"""
import base64
import json
import math
import sys
import zlib

POLE_REACH = 17.5
COMBINATOR_REACH = 9.0


def load_bp(path):
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read().strip()
    body = text[1:]  # strip version byte
    data = json.loads(zlib.decompress(base64.b64decode(body)))
    bp = data.get("blueprint", data)
    return bp


def reach_of(entity):
    q = entity.get("quality", "normal")
    if entity["name"] == "small-electric-pole" and q == "legendary":
        return POLE_REACH
    return COMBINATOR_REACH


def main(path):
    bp = load_bp(path)
    entities = bp["entities"]
    by_num = {e["entity_number"]: e for e in entities}
    pos = {e["entity_number"]: (e["position"]["x"], e["position"]["y"])
           for e in entities}
    reach = {e["entity_number"]: reach_of(e) for e in entities}

    poles = [e for e in entities if e["name"] == "small-electric-pole"]
    pole_quals = {}
    for p in poles:
        pole_quals[p.get("quality", "normal")] = \
            pole_quals.get(p.get("quality", "normal"), 0) + 1

    bad = []
    max_ratio = 0.0
    wires = bp.get("wires", [])
    for w in wires:
        n1, c1, n2, c2 = w
        e1, e2 = by_num.get(n1), by_num.get(n2)
        if e1 is None or e2 is None:
            continue
        d = math.dist(pos[n1], pos[n2])
        lim = min(reach[n1], reach[n2])
        ratio = d / lim
        max_ratio = max(max_ratio, ratio)
        if d > lim + 1e-9:
            bad.append((e1["name"], n1, pos[n1], e2["name"], n2, pos[n2],
                        round(d, 3), round(lim, 3)))

    # overlap check (poles vs combinators and pole vs pole)
    overlaps = []
    occ = {}
    for e in entities:
        name = e["name"]
        x, y = e["position"]["x"], e["position"]["y"]
        if name in ("arithmetic-combinator", "decider-combinator"):
            direction = e.get("direction", 0)
            if direction in (2, 6):  # east/west -> 2 wide
                tiles = [(x, y), (x + 1, y)]
            else:
                tiles = [(x, y), (x, y + 1)]
        else:
            tiles = [(x, y)]
        for t in tiles:
            key = (int(round(t[0] - 0.5)), int(round(t[1] - 0.5)))
            # raw positions are center; map back to tile
            occ.setdefault((int(t[0] - 0.5), int(t[1] - 0.5)), []).append(
                (name, n := e.get("entity_number")))

    # simpler: use raw tile positions from position-0.5
    occ2 = {}
    for e in entities:
        name = e["name"]
        x, y = e["position"]["x"], e["position"]["y"]
        tx, ty = int(round(x - 0.5)), int(round(y - 0.5))
        if name in ("arithmetic-combinator", "decider-combinator"):
            direction = e.get("direction", 0)
            if direction in (2, 6):
                cells = [(tx, ty), (tx + 1, ty)]
            else:
                cells = [(tx, ty), (tx, ty + 1)]
        else:
            cells = [(tx, ty)]
        for c in cells:
            occ2.setdefault(c, []).append(name)
    overlaps = {c: v for c, v in occ2.items() if len(v) > 1}

    print(f"total entities: {len(entities)}")
    print(f"poles: {sum(pole_quals.values())}  by quality: {pole_quals}")
    print(f"total wires: {len(wires)}")
    print(f"max wire/limit ratio: {max_ratio:.4f} "
          f"({'OK' if not bad else 'EXCEEDS'})")
    print(f"wires exceeding min reach: {len(bad)}")
    for b in bad:
        print("  BAD:", b)
    print(f"overlapping tiles: {len(overlaps)}")
    for c, v in overlaps.items():
        print("  OVERLAP:", c, v)
    return 1 if (bad or overlaps) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
