"""Evaluate the translator + re-articulation on a REAL music track.

Pipeline: extract a segment of the track → Basic Pitch (AI) transcription →
``midi_to_multi_rail_tick_data`` (with and without re-articulation) →
Factorio-speaker render, scored against the original segment with the
project's own analyzer.

Usage::
    $env:FACTORIO_BASIC_PITCH_PYTHON = "...\\.venv-bp\\Scripts\\python.exe"
    python eval_midi/eval_real_track.py --input "<mp3>" [--start 0 --dur 60]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import soundfile as sf

from factorio_display.audio.basic_pitch_transcriber import transcribe_audio
from factorio_display.audio.midi_translator import midi_to_multi_rail_tick_data
from factorio_display.audio.pitch_mapping import INSTRUMENT_MIDI_BASES, MIDI_BASE
from factorio_display._unicode_io import mido_open

from eval_midi import metrics, synth

OUT = Path("eval_midi/out/real")


def count_attacks(td: list[list[float]], threshold: float = 1.0) -> int:
    """Count distinct note attacks in a per-rail tick→loudness grid."""
    if not td:
        return 0
    active = [False] * len(td[0])
    count = 0
    for tick in td:
        for p, v in enumerate(tick):
            is_on = v > threshold
            if is_on and not active[p]:
                count += 1
            active[p] = is_on
    return count


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="Path to the audio file")
    ap.add_argument("--start", type=float, default=0.0, help="Segment start (s)")
    ap.add_argument("--dur", type=float, default=60.0, help="Segment duration (s)")
    ap.add_argument("--reartic", nargs="+", type=int, default=[0, 2],
                    help="Re-articulation tick values to compare (default: 0 2)")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    src = Path(args.input)
    stem = src.stem.replace(" ", "_").replace("=", "")

    # ── 1. extract segment ────────────────────────────────────────
    data, sr = sf.read(str(src), dtype="float64", always_2d=True)
    mono = data.mean(axis=1)
    i0 = int(args.start * sr)
    i1 = min(len(mono), int((args.start + args.dur) * sr))
    seg = mono[i0:i1]
    seg_wav = OUT / f"{stem}_seg_{int(args.start)}-{int(args.start + args.dur)}.wav"
    sf.write(str(seg_wav), seg, sr)
    print(f"segment: {args.start}-{args.start + args.dur}s ({len(seg) / sr:.1f}s), "
          f"sr={sr} -> {seg_wav}")

    # ── 2. AI transcription ───────────────────────────────────────
    ai_path = transcribe_audio(str(seg_wav), cache=False)
    if ai_path is None:
        sys.exit("Basic Pitch transcription failed — cannot continue.")
    ai_mid = mido_open(ai_path)
    print(f"AI MIDI: {ai_path}")
    print(f"  tracks={len(ai_mid.tracks)} length={ai_mid.length:.2f}s")
    ai_notes = metrics.extract_notes(ai_mid)
    print(f"  AI note events: {len(ai_notes)}")

    # ── 3. translate with each re-articulation setting ────────────
    results: list[dict] = []
    ref_wav = synth.midi_to_wav(ai_mid, tone="sine")  # timbre-free AI reference
    for reartic in args.reartic:
        instruments, rail_data = midi_to_multi_rail_tick_data(
            ai_mid, ticks_per_beat=30, map_drums=False, use_global_shift=True,
            rearticulation_ticks=reartic,
        )
        print(f"\n--- rearticulation_ticks={reartic} ---")
        print(f"  rails: {instruments}")
        total_attacks = 0
        for ri, inst in enumerate(instruments):
            n = count_attacks(rail_data[ri])
            total_attacks += n
            active = sum(1 for t in rail_data[ri] if any(v > 0 for v in t))
            print(f"    r{ri} [{inst}] attacks={n} active_ticks={active}/{len(rail_data[ri])}")
        print(f"  total attacks: {total_attacks}")

        factorio_wav = synth.tickdata_to_wav(rail_data, instruments)
        target = max(len(ref_wav), len(factorio_wav))
        pad = int(0.2 * sr)
        ref_p = synth.pad_audio(ref_wav, target + pad)
        fac_p = synth.pad_audio(factorio_wav, target + pad)[: len(ref_p)]
        audio = metrics.audio_metrics(ref_p, fac_p)
        print("  audio (AI-MIDI vs factorio):",
              json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                          for k, v in audio.items()}))

        wav_name = f"{stem}_reartic{reartic}.wav"
        synth.write_wav(fac_p, OUT / wav_name)
        results.append({
            "rearticulation_ticks": reartic,
            "instruments": instruments,
            "attacks_per_rail": [count_attacks(rd) for rd in rail_data],
            "total_attacks": total_attacks,
            "audio_vs_ai_midi": audio,
            "factorio_wav": str(OUT / wav_name),
        })

    # ── also compare the factorio renders against the ORIGINAL audio ──
    for r in results:
        fac = np.asarray(sf.read(r["factorio_wav"], dtype="float64", always_2d=True)[0].mean(axis=1))
        target = max(len(seg), len(fac))
        pad = int(0.2 * sr)
        seg_p = synth.pad_audio(seg, target + pad)
        fac_p = synth.pad_audio(fac, target + pad)[: len(seg_p)]
        audio_orig = metrics.audio_metrics(seg_p, fac_p)
        r["audio_vs_original"] = audio_orig
        print(f"\naudio vs ORIGINAL (reartic={r['rearticulation_ticks']}):",
              json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                          for k, v in audio_orig.items()}))

    report = {
        "input": str(src), "segment": [args.start, args.start + args.dur],
        "ai_midi": str(ai_path), "ai_note_events": len(ai_notes),
        "configs": results,
    }
    rep = OUT / f"{stem}_report.json"
    rep.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nreport -> {rep}")


if __name__ == "__main__":
    main()
