#!/usr/bin/env python3
"""Audio music demo — generates Factorio blueprints showcasing the
full 48-speaker polyphonic audio pipeline.

Three demo sections: chromatic scale (full range test), a rich chord
progression with dynamic swells, and a 5-voice arrangement of
Beethoven's "Ode to Joy" demonstrating per-note duration control,
velocity envelopes, and full 4-octave polyphony.

Usage::

    python demos/demo_music.py                    # full demo
    python demos/demo_music.py -o demo.txt        # writes to file
    python demos/demo_music.py --scale-only       # chromatic scale only
    python demos/demo_music.py --melody-only      # Ode to Joy only
    python demos/demo_music.py --instrument bass  # bass instrument

In-game setup
-------------
1. Import both blueprints (audio memory + player).
2. Place the player blueprint (speakers + unpackers).
3. Place the memory blueprint nearby.
4. Connect both to a clock circuit (tick counter, 0..N).
5. Run the clock — the speakers will play the music.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running from repo root (demos/ → repo root → src/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from factorio_display.audio.pitch_mapping import (
    SPEAKER_COUNT,
    MIDI_BASE,
    midi_to_pitch_index,
)
from factorio_display.audio.encoder import encode_audio_memory
from factorio_display.audio.player_blueprint import build_audio_decoder
from factorio_display import CLOCK_SIGNAL, SIGNAL_POOL, QUALITIES


# ── melody definitions ─────────────────────────────────────────────────

TICKS_PER_BEAT = 30  # 30 game ticks per quarter note (~0.5s at 60 UPS)
VELOCITY = 80        # 0–100

QUARTER = TICKS_PER_BEAT        # 30 ticks
HALF = QUARTER * 2              # 60 ticks
WHOLE = QUARTER * 4             # 120 ticks
EIGHTH = QUARTER // 2           # 15 ticks
DOTTED_HALF = HALF + QUARTER    # 90 ticks


# ── track-based music system ───────────────────────────────────────────

def merge_tick_data(
    tracks: list[list[list[int]]],
    max_loudness: int = 100,
) -> list[list[int]]:
    """Merge multiple tick_data arrays by summing loudness values.

    Each track is ``tick→[48 loudness]``.  Overlapping simultaneous
    notes are summed and clipped to *max_loudness*.
    """
    if not tracks:
        return []
    max_ticks = max(len(t) for t in tracks)
    result: list[list[int]] = []
    for t in range(max_ticks):
        loudness = [0] * SPEAKER_COUNT
        for track in tracks:
            if t < len(track):
                for i, val in enumerate(track[t]):
                    if val:
                        loudness[i] = min(max_loudness, loudness[i] + val)
        result.append(loudness)
    return result


def _envelope_velocity(
    tick_in_note: int,
    duration: int,
    target_vel: int,
) -> int:
    """Shape velocity across a note's duration with attack/sustain/release.

    Gives each note a natural bloom (attack ramp-up) and gentle decay
    (release ramp-down), avoiding the mechanical on/off sound of flat
    velocity.  Short notes get a simple triangle shape.
    """
    if target_vel <= 0:
        return 0
    if duration <= 4:
        # Very short note: quick triangle — 80% → 100% → 50%
        mid = duration // 2
        if tick_in_note <= mid:
            frac = 0.8 + 0.2 * (tick_in_note / max(1, mid))
        else:
            frac = 1.0 - 0.5 * ((tick_in_note - mid) / max(1, duration - mid))
        return max(1, int(target_vel * frac))

    attack_len = min(3, duration // 5)
    release_len = min(4, duration // 4)

    if tick_in_note < attack_len:
        # Attack: ramp 70% → 100%
        frac = 0.70 + 0.30 * (tick_in_note / attack_len)
        return max(1, int(target_vel * frac))
    elif tick_in_note >= duration - release_len:
        # Release: ramp 100% → 55%
        progress = (tick_in_note - (duration - release_len)) / release_len
        frac = 1.0 - 0.45 * progress
        return max(1, int(target_vel * frac))
    else:
        # Sustain: hold at full velocity
        return target_vel


def voice_to_tick_data(
    notes: list[tuple[int, int, int, int]],
    label: str = "",
) -> list[list[int]]:
    """Convert a single voice of ``(midi_note, start_delay, duration, velocity)``
    into ``tick→[48 loudness]``.

    Each note gets an attack/sustain/release envelope via
    :func:`_envelope_velocity` so notes bloom naturally instead of
    cutting in/out abruptly.
    """
    from collections import defaultdict

    tick_notes: dict[int, dict[int, int]] = defaultdict(dict)
    current_tick = 0

    for midi_note, delay, duration, velocity in notes:
        current_tick += delay
        pitch_idx = midi_to_pitch_index(midi_note)
        if pitch_idx is None:
            if label:
                sys.stderr.write(
                    f"  [{label}] MIDI {midi_note} outside range, skipping\n"
                )
            continue
        for t in range(current_tick, current_tick + duration):
            tick_in_note = t - current_tick
            shaped_vel = _envelope_velocity(tick_in_note, duration, velocity)
            existing = tick_notes[t].get(pitch_idx, 0)
            tick_notes[t][pitch_idx] = max(existing, shaped_vel)

    if not tick_notes:
        return []

    max_tick = max(tick_notes.keys())
    tick_data: list[list[int]] = []
    for t in range(max_tick + 1):
        loudness = [0] * SPEAKER_COUNT
        for pitch_idx, vel in tick_notes[t].items():
            loudness[pitch_idx] = vel
        tick_data.append(loudness)

    return tick_data


def melody_to_tick_data(
    notes: list[tuple[int, int, int]] | list[tuple[int, int, int, int]],
    note_duration_ticks: int = 25,
) -> list[list[int]]:
    """Convert melody tuples to tick_data.  Handles both 3-tuple
    ``(midi, delay, velocity)`` and 4-tuple ``(midi, delay, duration, velocity)``
    formats.  3-tuples get *note_duration_ticks* as every note's duration."""
    if not notes:
        return []
    if len(notes[0]) == 4:
        return voice_to_tick_data(notes, "")  # type: ignore[arg-type]
    converted: list[tuple[int, int, int, int]] = [
        (midi, delay, note_duration_ticks, vel) for midi, delay, vel in notes  # type: ignore[misc]
    ]
    return voice_to_tick_data(converted)


# ── chromatic scale (ascending then descending) ────────────────────────

def chromatic_scale(
    velocity: int = 60,
    ticks_per_note: int = 15,
) -> list[tuple[int, int, int, int]]:
    """Generate a full 4-octave chromatic scale (48 notes up, 48 down).

    Returns ``(midi, start_delay, duration, velocity)`` tuples.
    """
    notes: list[tuple[int, int, int, int]] = []
    dur = max(6, ticks_per_note - 3)  # slightly detached

    for midi in range(MIDI_BASE, MIDI_BASE + SPEAKER_COUNT):
        delay = 0 if midi == MIDI_BASE else ticks_per_note
        notes.append((midi, delay, dur, velocity))

    for midi in range(MIDI_BASE + SPEAKER_COUNT - 1, MIDI_BASE - 1, -1):
        notes.append((midi, ticks_per_note, dur, velocity))

    return notes


# ── rich chord progression (I–vi–IV–V–I with dynamics) ─────────────────

def rich_chord_progression(
    base_velocity: int = 55,
    ticks_per_chord: int = 90,
) -> list[list[int]]:
    """F major → D minor → Bb major → C major → F major.

    Each chord is voiced across 3 octaves with a crescendo across
    the progression (velocity 40 → 85), demonstrating polyphony
    and per-tick dynamics.
    """
    # Voicings: root-position triads in octaves 3, 4, and 5
    chords: list[list[int]] = [
        [53, 60, 65, 69, 72, 77],   # F maj:  F3 C4 F4 A4 C5 F5
        [57, 62, 65, 69, 74, 77],   # D min:  A3 D4 F4 A4 D5 F5
        [58, 62, 65, 70, 74, 77],   # Bb maj: Bb3 D4 F4 Bb4 D5 F5
        [60, 64, 67, 72, 76, 79],   # C maj:  C4 E4 G4 C5 E5 G5
        [53, 60, 65, 69, 72, 77],   # F maj:  F3 C4 F4 A4 C5 F5
    ]
    velocities = [40, 50, 62, 75, 85]  # crescendo across chords

    tick_data: list[list[int]] = []
    current_tick = 0

    for chord_idx, (chord, vel) in enumerate(zip(chords, velocities)):
        # Each chord sustains for ticks_per_chord
        for t in range(current_tick, current_tick + ticks_per_chord):
            loudness = [0] * SPEAKER_COUNT
            for midi in chord:
                pi = midi_to_pitch_index(midi)
                if pi is not None:
                    loudness[pi] = vel
            tick_data.append(loudness)
        current_tick += ticks_per_chord

    return tick_data


# ═══════════════════════════════════════════════════════════════════════
# Ode to Joy — rich 5-voice polyphonic arrangement in F major
# ═══════════════════════════════════════════════════════════════════════
#
# Per-note durations (4-tuple format: midi, start_delay, duration, velocity):
#   Q  = 22   quarter, slightly detached (piano articulation)
#   H  = 52   half note
#   W  = 112  whole note
#   DH = 82   dotted half
#   E  = 10   eighth, staccato
#   EL = 13   eighth, legato
#   QL = 28   quarter, legato (nearly connected)
#
# Phrase dynamics: each 4-bar phrase has a crescendo arc (p→mf→f→mf→p).
# The overall piece builds from mp (bar 1) to ff (bars 13-14) then
# decrescendos to p at the final chord.
#
# fmt: off

# Note durations — tuned for natural connection between notes.
# Gap = beat_ticks - duration:  Q gap=2 (7%), H gap=3 (5%), W gap=3 (2.5%).
Q  = 28      # quarter, nearly legato  (gap: 2 ticks)
H  = 57      # half note               (gap: 3 ticks)
W  = 117     # whole note              (gap: 3 ticks)
DH = 87      # dotted half             (gap: 3 ticks)
E  = 12      # eighth, light staccato  (gap: 3 ticks)
EL = 14      # eighth, legato          (gap: 1 tick)
QL = 29      # quarter, fully legato   (gap: 1 tick)

ODE_MELODY: list[tuple[int, int, int, int]] = [
    # ── bars 1-2: Freu-de schö-ner Göt-ter-fun-ken ────────────
    # phrase crescendo 60→72, then decrescendo 72→42
    (65, 0, Q, 60), (65, QUARTER, Q, 63), (67, QUARTER, Q, 66),
    (69, QUARTER, Q, 70),
    (69, QUARTER, Q, 72), (67, QUARTER, Q, 68), (65, QUARTER, Q, 64),
    (64, QUARTER, Q, 58),
    (62, QUARTER, Q, 56), (62, QUARTER, Q, 60), (64, QUARTER, Q, 66),
    (65, QUARTER, Q, 62),
    (65, QUARTER, Q, 55), (64, QUARTER, Q, 48), (64, HALF, H, 42),
    # ── bars 3-4: Toch-ter aus E-ly-si-um ─────────────────────
    (65, 0, Q, 64), (65, QUARTER, Q, 68), (67, QUARTER, Q, 72),
    (69, QUARTER, Q, 76),
    (69, QUARTER, Q, 78), (67, QUARTER, Q, 74), (65, QUARTER, Q, 70),
    (64, QUARTER, Q, 64),
    (62, QUARTER, Q, 62), (62, QUARTER, Q, 66), (64, QUARTER, Q, 72),
    (65, QUARTER, Q, 68),
    (64, QUARTER, Q, 62), (62, QUARTER, Q, 54), (62, WHOLE, W, 44),
    # ── bars 5-6: Wir be-tre-ten feu-er-trun-ken ──────────────
    (65, 0, Q, 72), (65, QUARTER, Q, 76), (67, QUARTER, Q, 80),
    (69, QUARTER, Q, 84),
    (69, QUARTER, Q, 86), (67, QUARTER, Q, 82), (65, QUARTER, Q, 78),
    (64, QUARTER, Q, 72),
    (62, QUARTER, Q, 70), (62, QUARTER, Q, 74), (64, QUARTER, Q, 80),
    (65, QUARTER, Q, 76),
    (65, QUARTER, Q, 70), (64, QUARTER, Q, 60), (64, HALF, H, 50),
    # ── bars 7-8: Himml-ische, dein Hei-lig-tum! ──────────────
    (65, 0, Q, 80), (65, QUARTER, Q, 84), (67, QUARTER, Q, 88),
    (69, QUARTER, Q, 92),
    (69, QUARTER, Q, 94), (67, QUARTER, Q, 90), (65, QUARTER, Q, 86),
    (64, QUARTER, Q, 80),
    (62, QUARTER, Q, 78), (62, QUARTER, Q, 82), (64, QUARTER, Q, 88),
    (65, QUARTER, Q, 86),
    (64, QUARTER, Q, 80), (62, QUARTER, Q, 68), (62, WHOLE, W, 52),
]

ODE_BASS: list[tuple[int, int, int, int]] = [
    # ── bars 1-2: dotted rhythm bass (F major → C major) ──────
    (53, 0, DH, 50), (53, QUARTER + HALF, Q, 48),            # F3  (dotted half + quarter)
    (60, QUARTER, H, 48),                                      # C4
    (60, QUARTER + HALF, Q, 50), (60, HALF, H, 48),           # C4
    (60, QUARTER, Q, 48),                                      # C4
    (62, QUARTER, W, 48),                                      # D4
    (58, QUARTER, H, 48), (60, QUARTER, Q, 50),               # Bb3 C4
    (53, QUARTER, H, 48),                                      # F3
    # ── bars 3-4 ──────────────────────────────────────────────
    (53, 0, DH, 50), (53, QUARTER + HALF, Q, 48),             # F3
    (60, QUARTER, H, 48),                                      # C4
    (60, QUARTER + HALF, Q, 50), (60, HALF, H, 48),           # C4
    (60, QUARTER, Q, 48),                                      # C4
    (62, QUARTER, W, 48),                                      # D4
    (58, QUARTER, H, 48), (60, QUARTER, Q, 50),               # Bb3 C4
    (62, QUARTER, H, 48),                                      # D4
    # ── bars 5-6: louder, walking bass ────────────────────────
    (53, 0, DH, 55), (53, QUARTER + HALF, Q, 53),             # F3
    (60, QUARTER, H, 53),                                      # C4
    (60, QUARTER + HALF, Q, 55), (60, HALF, H, 53),           # C4
    (60, QUARTER, Q, 53),                                      # C4
    (62, QUARTER, W, 53),                                      # D4
    (58, QUARTER, H, 53), (60, QUARTER, Q, 55),               # Bb3 C4
    (53, QUARTER, H, 53),                                      # F3
    # ── bars 7-8 ──────────────────────────────────────────────
    (53, 0, DH, 60), (53, QUARTER + HALF, Q, 58),             # F3
    (60, QUARTER, H, 58),                                      # C4
    (60, QUARTER + HALF, Q, 60), (60, HALF, H, 58),           # C4
    (60, QUARTER, Q, 58),                                      # C4
    (62, QUARTER, W, 58),                                      # D4
    (58, QUARTER, H, 58), (60, QUARTER, Q, 60),               # Bb3 C4
    (62, QUARTER, H, 58),                                      # D4
]

ODE_ALTO: list[tuple[int, int, int, int]] = [
    # ── bars 1-2: soft, sustained chord tones (octave 5) ──────
    (69, 0, DH, 36), (69, QUARTER + HALF, Q, 34),             # A4
    (72, QUARTER, H, 34),                                      # C5
    (72, QUARTER + HALF, Q, 36), (72, HALF, H, 34),           # C5
    (72, QUARTER, Q, 34),                                      # C5
    (69, QUARTER, W, 34),                                      # A4
    (70, QUARTER, H, 34), (72, QUARTER, QL, 36),              # Bb4 C5
    (69, QUARTER, H, 34),                                      # A4
    # ── bars 3-4: eighth-note movement in harmony ─────────────
    (69, 0, H, 38), (69, HALF, Q, 38), (69, QUARTER, E, 40), # A4 A4 A4(stacc)
    (72, EIGHTH, Q, 40),                                       # C5
    (72, QUARTER, H, 38),                                      # C5
    (72, HALF, Q, 40), (72, QUARTER, E, 42),                  # C5 C5(stacc)
    (69, EIGHTH, W, 38),                                       # A4
    (70, QUARTER, H, 38), (72, QUARTER, QL, 40),              # Bb4 C5
    (74, QUARTER, H, 38),                                      # D5
    # ── bars 5-6 ──────────────────────────────────────────────
    (69, 0, DH, 45), (69, QUARTER + HALF, Q, 43),             # A4
    (72, QUARTER, H, 43),                                      # C5
    (72, QUARTER + HALF, Q, 45), (72, HALF, H, 43),           # C5
    (72, QUARTER, Q, 43),                                      # C5
    (69, QUARTER, W, 43),                                      # A4
    (70, QUARTER, H, 43), (72, QUARTER, QL, 45),              # Bb4 C5
    (69, QUARTER, H, 43),                                      # A4
    # ── bars 7-8: wider voicing, eighth-note pulse ────────────
    (69, 0, H, 52), (69, HALF, Q, 52), (69, QUARTER, E, 54), # A4 A4 A4(stacc)
    (72, EIGHTH, Q, 54),                                       # C5
    (72, QUARTER, H, 52),                                      # C5
    (72, HALF, Q, 54), (72, QUARTER, E, 56),                  # C5 C5(stacc)
    (69, EIGHTH, W, 52),                                       # A4
    (70, QUARTER, H, 52), (72, QUARTER, QL, 54),              # Bb4 C5
    (74, QUARTER, H, 52),                                      # D5
]

ODE_TENOR: list[tuple[int, int, int, int]] = [
    # ── bars 1-4: tacet (silent) ──────────────────────────────
    # ── bars 5-6: enters with inner harmony ───────────────────
    (60, 16 * QUARTER, DH, 48),                                # C4
    (60, QUARTER + HALF, Q, 46),                                # C4
    (64, QUARTER, H, 46),                                      # E4
    (64, QUARTER + HALF, Q, 48), (64, HALF, H, 46),           # E4
    (64, QUARTER, Q, 46),                                      # E4
    (65, QUARTER, W, 46),                                      # F4
    (62, QUARTER, H, 46), (64, QUARTER, Q, 48),               # D4 E4
    (60, QUARTER, H, 46),                                      # C4
    # ── bars 7-8: fuller, with eighth-note passing tones ──────
    (60, 0, DH, 55), (60, QUARTER + HALF, Q, 53),             # C4
    (64, QUARTER, H, 53),                                      # E4
    (64, QUARTER + HALF, Q, 55), (64, HALF, H, 53),           # E4
    (64, QUARTER, Q, 53),                                      # E4
    (65, QUARTER, W, 53),                                      # F4
    (62, QUARTER, H, 53), (64, QUARTER, QL, 55),              # D4 E4 (legato)
    (65, QUARTER, H, 53),                                      # F4
]

# High-register sparkle — octave 6 doubling + bell-like staccato
ODE_SPARKLE: list[tuple[int, int, int, int]] = [
    # ── bars 1-4: tacet ───────────────────────────────────────
    # ── bars 5-6: octave-6 doubling, staccato ─────────────────
    (89, 16 * QUARTER, E, 42), (89, QUARTER, E, 42),
    (91, QUARTER, E, 44), (93, QUARTER, E, 46),
    (93, QUARTER, E, 46), (91, QUARTER, E, 44),
    (89, QUARTER, E, 42), (88, QUARTER, E, 40),
    (86, QUARTER, E, 38), (86, QUARTER, E, 40),
    (88, QUARTER, E, 44), (89, QUARTER, E, 44),
    (89, QUARTER, E, 40), (88, QUARTER, E, 36),
    (88, HALF, H, 32),
    # ── bars 7-8: bell-like staccato, brighter ────────────────
    (89, 0, E, 52), (89, QUARTER, E, 52),
    (91, QUARTER, E, 54), (93, QUARTER, E, 56),
    (93, QUARTER, E, 56), (91, QUARTER, E, 54),
    (89, QUARTER, E, 52), (88, QUARTER, E, 50),
    (86, QUARTER, E, 48), (86, QUARTER, E, 50),
    (88, QUARTER, E, 54), (89, QUARTER, E, 54),
    (88, QUARTER, E, 50), (86, QUARTER, E, 42),
    (86, WHOLE, W, 36),
]
# fmt: on


def ode_to_joy_tick_data(
    note_duration: int = 25,  # noqa: ARG001 — kept for CLI compat
) -> list[list[int]]:
    """Build the full 5-voice Ode to Joy arrangement.

    Voices span all 4 octaves (F3–E7), demonstrating the full
    48-speaker range with progressive layering:
      bars 1-4:  melody + bass + alto        (3 voices, octaves 3-5)
      bars 5-8:  add tenor + sparkle         (5 voices, FULL RANGE)

    Each note has its own duration: quarters ~22 ticks, halves ~52,
    wholes ~112.  Phrase-level crescendo/decrescendo arcs shape
    every 4-bar phrase.  Inner voices use dotted rhythms and
    eighth-note movement for rhythmic variety.

    Returns merged tick_data ready for encoding.
    """
    tracks = [
        voice_to_tick_data(ODE_MELODY, "melody"),
        voice_to_tick_data(ODE_BASS, "bass"),
        voice_to_tick_data(ODE_ALTO, "alto"),
        voice_to_tick_data(ODE_TENOR, "tenor"),
        voice_to_tick_data(ODE_SPARKLE, "sparkle"),
    ]
    return merge_tick_data(tracks)


# ── main ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate PoC Factorio audio blueprints"
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Write blueprints to file instead of stdout",
    )
    parser.add_argument(
        "--scale-only",
        action="store_true",
        help="Only generate the chromatic scale test",
    )
    parser.add_argument(
        "--melody-only",
        action="store_true",
        help="Only generate Ode to Joy (4-voice polyphonic)",
    )
    parser.add_argument(
        "--instrument",
        default="piano",
        help="Factorio instrument (piano, bass, celesta, plucked, drum)",
    )
    parser.add_argument(
        "--velocity",
        type=int,
        default=80,
        help="Note velocity 1–100 (default: 80)",
    )
    parser.add_argument(
        "--ticks-per-beat",
        type=int,
        default=30,
        help="Game ticks per quarter note (default: 30)",
    )
    parser.add_argument(
        "--note-duration",
        type=int,
        default=25,
        help="Ticks each note sustains (default: 25)",
    )
    parser.add_argument(
        "--player-only",
        action="store_true",
        help="Only output the player (speaker) blueprint, no memory",
    )
    parser.add_argument(
        "--memory-only",
        action="store_true",
        help="Only output the audio memory blueprint, no player",
    )
    args = parser.parse_args()

    # Build sections — each is (name, data, is_precomputed_tick_data)
    section_defs: list[tuple[str, object, bool]] = []

    if args.scale_only:
        section_defs = [
            ("Chromatic Scale", chromatic_scale(velocity=args.velocity), False),
        ]
    elif args.melody_only:
        section_defs = [
            ("Ode to Joy — 4-Voice Polyphonic",
             ode_to_joy_tick_data(args.note_duration), True),
        ]
    else:
        section_defs = [
            ("Chromatic Scale", chromatic_scale(velocity=args.velocity), False),
            ("Chord Progression I-vi-IV-V-I",
             rich_chord_progression(
                 base_velocity=args.velocity // 2,
                 ticks_per_chord=args.ticks_per_beat * 3,
             ), True),
            ("Ode to Joy — 4-Voice Polyphonic",
             ode_to_joy_tick_data(args.note_duration), True),
        ]

    out_lines: list[str] = []
    out_lines.append("=" * 60)
    out_lines.append("  Factorio Audio PoC — Blueprint Export")
    out_lines.append(f"  Instrument: {args.instrument}")
    out_lines.append("=" * 60)
    out_lines.append("")

    # ── Player blueprint ───────────────────────────────────────────────
    if not args.memory_only:
        out_lines.append("--- PLAYER BLUEPRINT (speaker matrix + unpackers) ---")
        player_bp = build_audio_decoder(
            name=f"Audio Player - {args.instrument.title()}",
            instrument=args.instrument,
            clock_signal=CLOCK_SIGNAL,
        )
        out_lines.append(player_bp)
        out_lines.append("")

    # ── Memory blueprints ──────────────────────────────────────────────
    if not args.player_only:
        for section_name, data, is_tick_data in section_defs:
            if is_tick_data:
                tick_data = data  # type: ignore[assignment]
                note_info = f"multi-track polyphonic"
            else:
                notes = data  # type: ignore[assignment]
                tick_data = melody_to_tick_data(
                    notes,  # type: ignore[arg-type]
                    note_duration_ticks=args.note_duration,
                )
                note_info = f"{len(notes)} note events"  # type: ignore[arg-type]

            if not tick_data:
                sys.stderr.write(f"Skipping '{section_name}': no valid notes.\n")
                continue

            out_lines.append(f"--- AUDIO MEMORY: {section_name} ---")
            out_lines.append(
                f"# {len(tick_data)} ticks, {note_info}"
            )

            memory_bp = encode_audio_memory(
                tick_data,
                section_name,
                signal_pool=SIGNAL_POOL,
                qualities=QUALITIES,
                clock_signal=CLOCK_SIGNAL,
            )
            out_lines.append(memory_bp)
            out_lines.append("")

    # ── Output ─────────────────────────────────────────────────────────
    output_text = "\n".join(out_lines)

    if args.output:
        Path(args.output).write_text(output_text, encoding="utf-8")
        sys.stderr.write(f"Written to {args.output}\n")
    else:
        sys.stdout.write(output_text)


if __name__ == "__main__":
    main()
