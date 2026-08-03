"""Emit the processed (octave-folded) MIDI and validate the blueprint files.

The folded MIDI shows what the translator actually places on the speaker grid
(all notes folded into a single playable range), so it can be compared against
the raw AI transcription in a MIDI player.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factorio_display._unicode_io import mido_open
from factorio_display.audio.midi_translator import midi_to_tick_data

RAW = Path("eval_midi/out/real/iron_soul_ai_0-60.mid")
FOLDED = Path("eval_midi/out/real/iron_soul_processed_0-60.mid")
BLUEPRINTS = [
    "eval_midi/out/real/iron_soul_r0.txt",          # plain, re-artic off
    "eval_midi/out/real/iron_soul_r2.txt",          # plain, re-artic on
    "eval_midi/out/real/iron_soul_r2_allinone.txt",  # composed w/ timer
]


def main() -> None:
    # ── 1. emit folded MIDI ───────────────────────────────────────
    mid = mido_open(str(RAW))
    print(f"raw AI MIDI: {len(mid.tracks)} track(s), length={mid.length:.2f}s")
    midi_to_tick_data(
        mid, ticks_per_beat=30, rearticulation_ticks=2,
        processed_midi_path=str(FOLDED),
    )
    print(f"folded MIDI written -> {FOLDED} ({FOLDED.stat().st_size} bytes)")

    # ── 2. validate blueprints ────────────────────────────────────
    from draftsman.blueprintable import Blueprint

    for bp_path in BLUEPRINTS:
        p = Path(bp_path)
        text = p.read_text(encoding="utf-8").strip()
        try:
            bp = Blueprint.from_string(text)
            ents = len(bp.entities)
            print(f"[OK] {p.name}: {len(text)} chars, {ents} entities, "
                  f"label={bp.label!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {p.name}: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
