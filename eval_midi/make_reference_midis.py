"""Generate a battery of reference MIDI files for evaluating the translator.

Scenarios cover the fidelity dimensions that matter for "does it play like
the original": in-range melody, wide-range octave folding, polyphony/chords,
multi-instrument routing, and drums.
"""

from __future__ import annotations

from pathlib import Path

import mido

TPB = 480  # ticks per beat
TEMPO = mido.bpm2tempo(120)  # 120 BPM
BEAT = TPB


def _save(mid: mido.MidiFile, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mid.save(path)
    print(f"wrote {path} ({mid.length:.2f}s)")


def _track(events: list[tuple[int, str, mido.Message]], channel: int = 0) -> mido.MidiTrack:
    """Build a track from (abs_tick, on/off, msg) events, delta-encoded."""
    track = mido.MidiTrack()
    track.append(mido.MetaMessage("set_tempo", tempo=TEMPO, time=0))
    track.append(mido.MetaMessage("track_name", name=f"track {channel}", time=0))
    track.append(mido.Message("program_change", program=0, channel=channel, time=0))
    evs = sorted(events, key=lambda e: (e[0], 0 if e[1] == "off" else 1))
    prev = 0
    for abs_tick, _, msg in evs:
        msg.time = abs_tick - prev
        track.append(msg)
        prev = abs_tick
    return track


def _note_events(
    notes: list[tuple[int, int, int, int]],
    channel: int = 0,
) -> list[tuple[int, str, mido.Message]]:
    """notes: (midi_note, start_beat, dur_beats, velocity) → events."""
    evs: list[tuple[int, str, mido.Message]] = []
    for note, start_b, dur_b, vel in notes:
        s = int(start_b * BEAT)
        d = int(dur_b * BEAT)
        evs.append((s, "on", mido.Message("note_on", note=note, velocity=vel, channel=channel, time=0)))
        evs.append((s + d, "off", mido.Message("note_off", note=note, velocity=0, channel=channel, time=0)))
    return evs


# ── scenario builders ──────────────────────────────────────────────────

def melody() -> mido.MidiFile:
    """Ode to Joy theme, monophonic, well inside piano range F3–E7."""
    # (note, start_beat, dur_beats, vel)
    theme = [
        (76, 0, 1, 90), (76, 1, 1, 90), (79, 2, 1, 90), (81, 3, 1, 90),
        (81, 4, 1, 90), (79, 5, 1, 90), (76, 6, 1, 90), (74, 7, 1, 90),
        (72, 8, 1, 90), (72, 9, 1, 90), (74, 10, 1, 90), (76, 11, 1, 90),
        (76, 12, 1.5, 90), (74, 13.5, 0.5, 90), (74, 14, 2, 80),
    ]
    mid = mido.MidiFile(ticks_per_beat=TPB)
    mid.tracks.append(_track(_note_events(theme), channel=0))
    return mid


def wide_range() -> mido.MidiFile:
    """A melody that swings far outside piano's F3–E7 to stress octave folding."""
    # spans MIDI 24 (C1) → 108 (C8)
    seq = [
        (24, 0, 1, 90), (36, 1, 1, 85), (48, 2, 1, 85), (60, 3, 1, 90),
        (72, 4, 1, 90), (84, 5, 1, 90), (96, 6, 1, 90), (108, 7, 1, 95),
        (96, 8, 1, 90), (84, 9, 1, 90), (72, 10, 1, 90), (60, 11, 1, 90),
        (48, 12, 1, 85), (36, 13, 1, 85), (24, 14, 2, 90),
    ]
    mid = mido.MidiFile(ticks_per_beat=TPB)
    mid.tracks.append(_track(_note_events(seq), channel=0))
    return mid


def polyphonic() -> mido.MidiFile:
    """Ode to Joy theme + chordal accompaniment (4 voices), in range."""
    melody_notes = [
        (76, 0, 1, 90), (76, 1, 1, 90), (79, 2, 1, 90), (81, 3, 1, 90),
        (81, 4, 1, 90), (79, 5, 1, 90), (76, 6, 1, 90), (74, 7, 1, 90),
        (72, 8, 1, 90), (72, 9, 1, 90), (74, 10, 1, 90), (76, 11, 1, 90),
        (76, 12, 1.5, 90), (74, 13.5, 0.5, 90), (74, 14, 2, 80),
    ]
    chords = []
    prog = [(60, 64, 67), (57, 60, 64), (55, 59, 62), (60, 64, 67), (57, 60, 64), (59, 62, 67), (55, 59, 62)]
    for i in range(0, 15, 2):
        ch = prog[(i // 2) % len(prog)]
        for n in ch:
            chords.append((n, i, 2, 55))
    bass = [(36, i * 2, 2, 70) for i in range(8)]
    all_notes = melody_notes + chords + bass
    mid = mido.MidiFile(ticks_per_beat=TPB)
    mid.tracks.append(_track(_note_events(all_notes, channel=0), channel=0))
    return mid


def multitrack() -> mido.MidiFile:
    """Three tracks with distinct GM programs (piano + bass + lead) plus drums."""
    melody_notes = [
        (76, 0, 1, 90), (76, 1, 1, 90), (79, 2, 1, 90), (81, 3, 1, 90),
        (81, 4, 1, 90), (79, 5, 1, 90), (76, 6, 1, 90), (74, 7, 1, 90),
        (72, 8, 1, 90), (72, 9, 1, 90), (74, 10, 1, 90), (76, 11, 1, 90),
        (76, 12, 1.5, 90), (74, 13.5, 0.5, 90), (74, 14, 2, 80),
    ]
    bass_notes = [(36, i * 2, 2, 72) for i in range(8)]
    lead_notes = [(88, i * 2, 1, 60) for i in range(0, 8, 2)]  # high, F5+
    drum_notes = [
        (36, 0, 0.5, 100), (42, 0.5, 0.5, 70), (36, 1, 0.5, 100), (38, 1.5, 0.5, 80),
        (36, 2, 0.5, 100), (42, 2.5, 0.5, 70), (36, 3, 0.5, 100), (42, 3.5, 0.5, 70),
        (36, 4, 0.5, 100), (38, 4.5, 0.5, 80), (36, 5, 0.5, 100), (42, 5.5, 0.5, 70),
        (36, 6, 0.5, 100), (42, 6.5, 0.5, 70), (36, 7, 0.5, 100), (42, 7.5, 0.5, 70),
    ]
    mid = mido.MidiFile(ticks_per_beat=TPB)

    t0 = mido.MidiTrack(); t0.append(mido.MetaMessage("set_tempo", tempo=TEMPO, time=0))
    t0.append(mido.Message("program_change", program=0, channel=0, time=0))
    t0.extend(_track(_note_events(melody_notes, channel=0), channel=0))
    mid.tracks.append(t0)

    t1 = mido.MidiTrack(); t1.append(mido.MetaMessage("set_tempo", tempo=TEMPO, time=0))
    t1.append(mido.Message("program_change", program=32, channel=1, time=0))  # bass
    t1.extend(_track(_note_events(bass_notes, channel=1), channel=1))
    mid.tracks.append(t1)

    t2 = mido.MidiTrack(); t2.append(mido.MetaMessage("set_tempo", tempo=TEMPO, time=0))
    t2.append(mido.Message("program_change", program=80, channel=2, time=0))  # lead
    t2.extend(_track(_note_events(lead_notes, channel=2), channel=2))
    mid.tracks.append(t2)

    t3 = mido.MidiTrack(); t3.append(mido.MetaMessage("set_tempo", tempo=TEMPO, time=0))
    t3.append(mido.Message("program_change", program=0, channel=9, time=0))
    t3.extend(_track(_note_events(drum_notes, channel=9), channel=9))
    mid.tracks.append(t3)
    return mid


def drums_only() -> mido.MidiFile:
    """Just a drum pattern (channel 9), to test the drum rail path."""
    hits = [
        (36, 0, 0.5, 100), (42, 0.5, 0.5, 70), (36, 1, 0.5, 100), (38, 1.5, 0.5, 80),
        (36, 2, 0.5, 100), (42, 2.5, 0.5, 70), (36, 3, 0.5, 100), (42, 3.5, 0.5, 70),
        (36, 4, 0.5, 100), (38, 4.5, 0.5, 80), (36, 5, 0.5, 100), (42, 5.5, 0.5, 70),
        (36, 6, 0.5, 100), (42, 6.5, 0.5, 70), (36, 7, 0.5, 100), (42, 7.5, 0.5, 70),
    ]
    mid = mido.MidiFile(ticks_per_beat=TPB)
    t = mido.MidiTrack(); t.append(mido.MetaMessage("set_tempo", tempo=TEMPO, time=0))
    t.append(mido.Message("program_change", program=0, channel=9, time=0))
    t.extend(_track(_note_events(hits, channel=9), channel=9))
    mid.tracks.append(t)
    return mid


def main() -> None:
    out = Path("eval_midi/ref_midis")
    _save(melody(), out / "melody.mid")
    _save(wide_range(), out / "wide_range.mid")
    _save(polyphonic(), out / "polyphonic.mid")
    _save(multitrack(), out / "multitrack.mid")
    _save(drums_only(), out / "drums_only.mid")


if __name__ == "__main__":
    main()
