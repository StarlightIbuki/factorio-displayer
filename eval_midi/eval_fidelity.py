"""End-to-end fidelity evaluation of the MIDI → Factorio translator.

For each reference MIDI:
  1. translate via ``midi_to_multi_rail_tick_data`` (the real code path)
  2. reconstruct notes from the tick_data (invert the encoding)
  3. note-level metrics (recall / pitch / timing / dynamics)
  4. render both as audio and compare with the project's own analyzer
     (chroma / onset envelope / spectral cosine / DTW)

Usage::
    python eval_midi/eval_fidelity.py [--out DIR] [--adsr]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import mido

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factorio_display.audio.midi_translator import midi_to_multi_rail_tick_data  # noqa: E402
from factorio_display.audio.pitch_mapping import INSTRUMENT_MIDI_BASES, MIDI_BASE  # noqa: E402
from factorio_display._unicode_io import mido_open  # noqa: E402

from eval_midi import metrics, synth  # noqa: E402
from eval_midi.make_reference_midis import (  # noqa: E402
    melody, wide_range, polyphonic, multitrack, drums_only,
)


def run_scenario(
    name: str,
    mid: mido.MidiFile,
    out_dir: Path,
    rail_mode: str,
    adsr: bool,
    map_drums: bool,
    reartic: int = 0,
) -> dict:
    print(f"\n{'=' * 72}\nSCENARIO: {name}  (rail_mode={rail_mode}, adsr={adsr}, map_drums={map_drums}, reartic={reartic})\n{'=' * 72}")
    ref_notes = metrics.extract_notes(mid)

    kwargs: dict = {"ticks_per_beat": 30, "map_drums": map_drums, "use_global_shift": True}
    if rail_mode != "auto":  # "all" -> keep everything
        kwargs["rail_mode"] = rail_mode
    if adsr:
        kwargs.update(attack_ticks=10, decay_ticks=10, sustain_level=1.0, release_ticks=10)
    if reartic:
        kwargs["rearticulation_ticks"] = reartic

    instruments, rail_data = midi_to_multi_rail_tick_data(mid, **kwargs)
    print(f"  rails: {instruments}")
    for ri, inst in enumerate(instruments):
        active = sum(1 for t in rail_data[ri] if any(v > 0 for v in t))
        print(f"    r{ri} [{inst}] {len(rail_data[ri])} ticks, {active} active ticks")

    # ── note-level reconstruction per rail ─────────────────────────
    rec_notes: list[dict] = []
    for ri, inst in enumerate(instruments):
        base = INSTRUMENT_MIDI_BASES.get(inst, MIDI_BASE)
        rec_notes.extend(metrics.tickdata_to_notes(rail_data[ri], base))
    note_metrics = metrics.note_level_metrics(ref_notes, rec_notes)
    print("  note-level:", json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                                       for k, v in note_metrics.items()}, indent=2))

    # ── audio-domain comparison ────────────────────────────────────
    ref_wav = synth.midi_to_wav(mid)
    ref_sine = synth.midi_to_wav(mid, tone="sine")  # timbre-fair baseline
    factorio_wav = synth.tickdata_to_wav(rail_data, instruments)
    content_wav = synth.notes_to_wav(rec_notes)

    target = max(len(ref_wav), len(factorio_wav), len(content_wav))
    pad = int(0.2 * 44100)
    ref_wav = synth.pad_audio(ref_wav, target + pad)
    ref_sine = synth.pad_audio(ref_sine, target + pad)
    factorio_wav = synth.pad_audio(factorio_wav, target + pad)[: len(ref_wav)]
    content_wav = synth.pad_audio(content_wav, target + pad)[: len(ref_wav)]

    # "content": same piano timbre both sides → isolates translator loss
    content_audio = metrics.audio_metrics(ref_wav, content_wav)
    # "factorio": how it actually sounds in-game vs the original
    factorio_audio = metrics.audio_metrics(ref_wav, factorio_wav)
    # "sine" reference vs factorio: both pure tones → timbre-free content loss
    sine_factorio_audio = metrics.audio_metrics(ref_sine, factorio_wav)
    print("  audio(content, piano-vs-piano):",
          json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                      for k, v in content_audio.items()}, indent=2))
    print("  audio(factorio emulation vs piano):",
          json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                      for k, v in factorio_audio.items()}, indent=2))
    print("  audio(sine-ref vs factorio, timbre-free):",
          json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                      for k, v in sine_factorio_audio.items()}, indent=2))

    synth.write_wav(ref_wav, out_dir / f"{name}_ref.wav")
    synth.write_wav(factorio_wav, out_dir / f"{name}_factorio.wav")
    synth.write_wav(content_wav, out_dir / f"{name}_content.wav")

    return {
        "name": name, "rail_mode": rail_mode, "adsr": adsr, "map_drums": map_drums,
        "instruments": instruments,
        "note": note_metrics,
        "audio_content": content_audio,
        "audio_factorio": factorio_audio,
        "audio_sine_factorio": sine_factorio_audio,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="eval_midi/out")
    ap.add_argument("--adsr", action="store_true", help="Use CLI-default ADSR (10/10/1.0/10)")
    ap.add_argument("--rail-mode", default="auto", choices=["auto", "all", "piano"])
    ap.add_argument("--map-drums", action="store_true", default=False,
                    help="Route below-range melodic notes to the kick drum (default off)")
    ap.add_argument("--rearticulation-ticks", type=int, default=0,
                    help="Re-articulation gap in game ticks (0=off)")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    scenarios = [
        ("melody", melody()),
        ("wide_range", wide_range()),
        ("polyphonic", polyphonic()),
        ("multitrack", multitrack()),
        ("drums_only", drums_only()),
    ]

    results = []
    for name, mid in scenarios:
        r = run_scenario(name, mid, out_dir, args.rail_mode, args.adsr,
                         args.map_drums, args.rearticulation_ticks)
        results.append(r)

    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nReport written to {report_path}")


if __name__ == "__main__":
    main()
