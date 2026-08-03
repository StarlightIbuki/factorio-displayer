"""Standalone Basic Pitch transcription worker.

Runs under a Python interpreter that has **basic-pitch** + **tensorflow**
installed (e.g. the project's ``.venv-bp`` with Python 3.11/3.13).  The main
application invokes this module as a **subprocess** so that basic-pitch stays
an *optional* dependency — it is only needed when encoding non-MIDI audio
(MP3/WAV/…) and is never imported by the main package.

This module deliberately imports **only** the standard library and
``basic_pitch`` — it must not import anything from ``factorio_display``.

Usage
-----
``python basic_pitch_worker.py <input_audio> <output_dir>``

Writes ``<output_dir>/<stem>_basic_pitch.mid`` (plus a small ``.json`` of
model outputs unless disabled).  Prints the MIDI path on success.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _resolve_midi_path(output_dir: str, input_audio: str) -> Path:
    """Basic Pitch names its output ``<stem>_basic_pitch.mid``."""
    stem = Path(input_audio).stem
    return Path(output_dir) / f"{stem}_basic_pitch.mid"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python basic_pitch_worker.py <input_audio> <output_dir>",
              file=sys.stderr)
        return 2

    input_audio, output_dir = argv[0], argv[1]

    # Imported lazily/here so this module can be *syntax*-checked by the main
    # interpreter without tensorflow being importable.
    try:
        from basic_pitch import ICASSP_2022_MODEL_PATH  # type: ignore[import-not-found]
        from basic_pitch.inference import predict_and_save  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - env error
        print(f"basic_pitch is not importable in this interpreter: {exc}",
              file=sys.stderr)
        return 3

    try:
        predict_and_save(
            [input_audio],
            output_dir,
            save_midi=True,
            sonify_midi=False,
            save_model_outputs=False,
            save_notes=False,
            model_or_model_path=ICASSP_2022_MODEL_PATH,
            onset_threshold=0.5,
            frame_threshold=0.3,
            minimum_note_length=58,     # ms — drop ultra-short glitches
            minimum_frequency=32.70,    # C1
            maximum_frequency=2093.00,  # C7
            melodia_trick=True,
            midi_tempo=120,
        )
    except Exception as exc:  # pragma: no cover - runtime error
        print(f"basic_pitch transcription failed: {exc}", file=sys.stderr)
        return 4

    midi_path = _resolve_midi_path(output_dir, input_audio)
    if not midi_path.exists():
        print(f"basic_pitch produced no MIDI at {midi_path}", file=sys.stderr)
        return 5

    print(midi_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
