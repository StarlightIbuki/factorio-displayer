"""End-to-end evaluation of the AI-driven transcription path.

Original audio (synthesized from a reference MIDI) → Basic Pitch (AI) →
MIDI → Factorio translator → tick_data → Factorio-speaker audio.

We measure:
  1. Transcription accuracy: AI MIDI notes vs the original MIDI notes.
  2. End-to-end playback fidelity: Factorio render of the AI-transcribed
     MIDI vs the original audio (via the project's own analyzer).
  3. A head-to-head against the STFT fallback path.

Usage::
    set FACTORIO_BASIC_PITCH_PYTHON=...\\venv-bp\\Scripts\\python.exe
    python eval_midi/eval_basic_pitch.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from factorio_display.audio.basic_pitch_transcriber import (
    basic_pitch_available,
    find_basic_pitch_python,
    transcribe_audio,
)
from factorio_display.audio.midi_translator import midi_to_multi_rail_tick_data
from factorio_display.audio.pitch_mapping import INSTRUMENT_MIDI_BASES, MIDI_BASE
from factorio_display._unicode_io import mido_open

from eval_midi import metrics, synth
from eval_midi.make_reference_midis import melody, polyphonic

OUT = Path("eval_midi/out/bp")


def transcribe(path: str):
    """Transcribe via Basic Pitch (cache disabled)."""
    sys.stderr.write(f"\n>> Basic Pitch transcribing {Path(path).name} ...\n")
    return transcribe_audio(path, cache=False)


def stft_fallback_midi(path: str):
    """Replicate the encoder's STFT fallback path → MIDI."""
    from factorio_display.audio.audio_analyzer import audio_file_to_loudness
    from factorio_display.audio.loudness_to_midi import loudness_to_midi

    full = audio_file_to_loudness(path, activation_threshold=0.0)
    mid = loudness_to_midi(full, activation_threshold=0.05, condense=True)
    return mid


def evaluate_chain(name: str, ref_mid, input_wav: Path, out_dir: Path) -> dict:
    """Transcribe input_wav → translate → compare against ref_mid audio."""
    print(f"\n{'=' * 72}\nAI CHAIN: {name}\n{'=' * 72}")
    ref_wav = synth.midi_to_wav(ref_mid)

    # ── 1. AI transcription ───────────────────────────────────────
    ai_path = transcribe(str(input_wav))
    if ai_path is None:
        print("  Basic Pitch unavailable or failed — skipping AI chain.")
        return {}
    ai_mid = mido_open(ai_path)
    print(f"  AI MIDI: {ai_path}")
    print(f"    tracks={len(ai_mid.tracks)} length={ai_mid.length:.2f}s")

    ref_notes = metrics.extract_notes(ref_mid)
    ai_notes = metrics.extract_notes(ai_mid)
    trans_metrics = metrics.note_level_metrics(ref_notes, ai_notes)
    print("  transcription (ref MIDI vs AI MIDI):",
          json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                      for k, v in trans_metrics.items()}, indent=2))

    # ── 2. Translate AI MIDI → Factorio ───────────────────────────
    instruments, rail_data = midi_to_multi_rail_tick_data(
        ai_mid, ticks_per_beat=30, map_drums=False, use_global_shift=True,
    )
    print(f"  rails: {instruments}")
    rec_notes: list[dict] = []
    for ri, inst in enumerate(instruments):
        base = INSTRUMENT_MIDI_BASES.get(inst, MIDI_BASE)
        rec_notes.extend(metrics.tickdata_to_notes(rail_data[ri], base))
    final_metrics = metrics.note_level_metrics(ref_notes, rec_notes)
    print("  final (ref MIDI vs Factorio-from-AI):",
          json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                      for k, v in final_metrics.items()}, indent=2))

    # ── 3. Audio: original vs Factorio render of AI transcription ─
    factorio_wav = synth.tickdata_to_wav(rail_data, instruments)
    content_wav = synth.notes_to_wav(rec_notes)
    target = max(len(ref_wav), len(factorio_wav))
    pad = int(0.2 * 44100)
    ref_wav_p = synth.pad_audio(ref_wav, target + pad)
    factorio_wav_p = synth.pad_audio(factorio_wav, target + pad)[: len(ref_wav_p)]
    content_wav_p = synth.pad_audio(content_wav, target + pad)[: len(ref_wav_p)]
    audio_factorio = metrics.audio_metrics(ref_wav_p, factorio_wav_p)
    audio_content = metrics.audio_metrics(ref_wav_p, content_wav_p)
    print("  audio (original vs factorio-from-AI):",
          json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                      for k, v in audio_factorio.items()}, indent=2))
    print("  audio (original vs content-from-AI, timbre-free):",
          json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                      for k, v in audio_content.items()}, indent=2))

    synth.write_wav(ref_wav_p, out_dir / f"{name}_orig.wav")
    synth.write_wav(factorio_wav_p, out_dir / f"{name}_factorio.wav")
    synth.write_wav(content_wav_p, out_dir / f"{name}_content.wav")

    return {
        "name": name,
        "transcription": trans_metrics,
        "final": final_metrics,
        "audio_factorio": audio_factorio,
        "audio_content": audio_content,
        "instruments": instruments,
        "ai_midi": str(ai_path),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    print("Basic Pitch available:", basic_pitch_available(),
          "python:", find_basic_pitch_python())

    results = []
    for name, ref_mid in [("melody", melody()), ("polyphonic", polyphonic())]:
        wav = OUT / f"{name}_input.wav"
        synth.write_wav(synth.midi_to_wav(ref_mid), str(wav))
        r = evaluate_chain(name, ref_mid, wav, OUT)
        if r:
            results.append(r)

    # ── STFT fallback head-to-head ─────────────────────────────────
    print(f"\n{'=' * 72}\nSTFT FALLBACK CHAIN (no AI)\n{'=' * 72}")
    for name, ref_mid in [("melody", melody()), ("polyphonic", polyphonic())]:
        wav = OUT / f"{name}_input.wav"
        stft_mid = stft_fallback_midi(str(wav))
        ref_notes = metrics.extract_notes(ref_mid)
        stft_notes = metrics.extract_notes(stft_mid)
        tm = metrics.note_level_metrics(ref_notes, stft_notes)
        print(f"  STFT {name}: transcription",
              json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                          for k, v in tm.items()}))
        # translate + audio
        instruments, rail_data = midi_to_multi_rail_tick_data(
            stft_mid, ticks_per_beat=30, map_drums=False, use_global_shift=True,
        )
        rec_notes = []
        for ri, inst in enumerate(instruments):
            base = INSTRUMENT_MIDI_BASES.get(inst, MIDI_BASE)
            rec_notes.extend(metrics.tickdata_to_notes(rail_data[ri], base))
        fm = metrics.note_level_metrics(ref_notes, rec_notes)
        factorio_wav = synth.tickdata_to_wav(rail_data, instruments)
        ref_wav = synth.midi_to_wav(ref_mid)
        target = max(len(ref_wav), len(factorio_wav))
        pad = int(0.2 * 44100)
        af = metrics.audio_metrics(
            synth.pad_audio(ref_wav, target + pad),
            synth.pad_audio(factorio_wav, target + pad)[: target + pad],
        )
        print(f"  STFT {name}: final", json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                                                   for k, v in fm.items()}))
        print(f"  STFT {name}: audio", json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                                                   for k, v in af.items()}))
        results.append({"name": f"stft_{name}", "transcription": tm, "final": fm,
                        "audio_factorio": af})

    (OUT / "report.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nReport written to {OUT / 'report.json'}")


if __name__ == "__main__":
    main()
