"""Minimal reproduction: does the project's encoder emit wires at all?"""

from __future__ import annotations

import base64
import json
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from draftsman.blueprintable import Blueprint  # noqa: E402

from factorio_display import QUALITIES, SIGNAL_POOL  # noqa: E402
from factorio_display.audio.encoder import encode_audio_memory  # noqa: E402
from factorio_display.audio.player_blueprint import build_multi_rail_decoder  # noqa: E402


def raw_connections(bs: str) -> int:
    body = bs[1:] if bs[:1].isdigit() else bs
    data = json.loads(zlib.decompress(base64.b64decode(body)))
    ents = data.get("blueprint", data).get("entities", [])
    n = 0
    for ent in ents:
        conns = ent.get("connections")
        if not conns:
            continue
        for _pt, nets in conns.items():
            for c in ("red", "green"):
                n += len(nets.get(c, []))
    return n


def main() -> None:
    # tiny 2-tick, 48-channel audio memory, 2 pages
    tick_data = [[1] * 48, [2] * 48, [3] * 48, [4] * 48]
    mem = encode_audio_memory(
        tick_data, "tiny", list(SIGNAL_POOL), list(QUALITIES),
        clock_signal="signal-clock", id_prefix="ap",
    )
    print("mem blueprint len:", len(mem))
    print("mem raw wire entries:", raw_connections(mem))
    mbp = Blueprint.from_string(mem)
    print("mem draftsman entities:", len(mbp.entities))
    red = green = 0
    for e in mbp.entities:
        c = getattr(e, "connections", None)
        red += len(getattr(c, "red", None) or [])
        green += len(getattr(c, "green", None) or [])
    print(f"mem draftsman red={red} green={green}")

    # decoder
    dec = build_multi_rail_decoder(name="dec", instruments=["piano"], clock_signal="signal-clock")
    print("\ndecoder blueprint len:", len(dec))
    print("decoder raw wire entries:", raw_connections(dec))
    dbp = Blueprint.from_string(dec)
    red = green = 0
    for e in dbp.entities:
        c = getattr(e, "connections", None)
        red += len(getattr(c, "red", None) or [])
        green += len(getattr(c, "green", None) or [])
    print(f"decoder draftsman red={red} green={green}")


if __name__ == "__main__":
    main()
