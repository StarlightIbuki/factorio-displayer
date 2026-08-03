"""Render piano+bass+detected-drums playback and measure percussion recovery.

Uses the same pipeline as the real-track eval (AI MIDI → multi-rail translate
with re-articulation) but additionally recovers kick/snare/hat from the raw
waveform (Basic Pitch misses unpitched percussion) and appends a drum rail.

Writes:
    eval_midi/out/real/11_IRON_SOUL_Karaoke_reartic2_drums.wav  (full)
    eval_midi/out/real/11_IRON_SOUL_Karaoke_drums_only.wav      (drums only)
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factorio_display._unicode_io import mido_open
from factorio_display.audio.audio_analyzer import read_audio_file
from factorio_display.audio.drum_detector import (
    detect_drum_events,
    events_to_drum_rail,
)
from factorio_display.audio.midi_translator import midi_to_multi_rail_tick_data

from eval_midi import metrics, synth

MIDI = "eval_midi/out/real/iron_soul_ai_0-60.mid"
WAV = "eval_midi/out/real/11_IRON_SOUL_Karaoke_seg_0-60.wav"
OUT = Path("eval_midi/out/real")


def pad_to(a, n):
    return synth.pad_audio(a, n)


def main() -> None:
    seg, sr = read_audio_file(WAV)
    mid = mido_open(MIDI)
    instruments, rail_data = midi_to_multi_rail_tick_data(
        mid, ticks_per_beat=30, map_drums=False, use_global_shift=True,
        rearticulation_ticks=2,
    )
    print("melodic rails:", instruments)
    n_ticks = max((len(td) for td in rail_data), default=0)

    events = detect_drum_events(seg, sr)
    print("drum events:", len(events),
          dict(Counter(w for _, w, _ in events)))
    drum_rail = events_to_drum_rail(events, n_ticks)

    # Full 3-rail render (align lengths).
    instruments2 = instruments + ["drum"]
    rail2 = rail_data + [drum_rail]
    max_t = max(len(td) for td in rail2)
    rail2 = [td + [[0.0] * 48 for _ in range(max_t - len(td))]
             for td in rail2]
    fac = synth.tickdata_to_wav(rail2, instruments2)
    synth.write_wav(fac, OUT / "11_IRON_SOUL_Karaoke_reartic2_drums.wav")

    drums_only = synth.tickdata_to_wav([rail2[-1]], ["drum"])
    synth.write_wav(drums_only, OUT / "11_IRON_SOUL_Karaoke_drums_only.wav")

    target = max(len(seg), len(fac))
    padn = int(0.2 * sr)
    seg_p = pad_to(seg, target + padn)
    fac_p = pad_to(fac, target + padn)[: len(seg_p)]
    drums_p = pad_to(drums_only, target + padn)[: len(seg_p)]

    def fmt(d):
        return {k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in d.items()}

    a_full = metrics.audio_metrics(seg_p, fac_p)
    a_drums = metrics.audio_metrics(seg_p, drums_p)
    print("full 3-rail render vs ORIGINAL :", fmt(a_full))
    print("drums-only vs ORIGINAL          :", fmt(a_drums))
    print("wrote:", OUT / "11_IRON_SOUL_Karaoke_reartic2_drums.wav")


if __name__ == "__main__":
    main()
