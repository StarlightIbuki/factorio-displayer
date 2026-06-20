"""MIDI extraction from loudness arrays — converts a full-spectrum
``tick → loudness`` array into a MIDI file via heuristic note detection.

Algorithm (adapted from NFJones/audio-to-midi)
----------------------------------------------
1. Per tick, pitches with loudness > *activation_threshold* become
   candidate notes.
2. Track active notes: when a pitch crosses the threshold → ``note_on``;
   when it drops below → ``note_off``.
3. **Condense** (optional): merge consecutive same-pitch notes, using
   the maximum velocity across the merged segment.
4. **Top-N polyphony** (optional): only keep the *N* loudest notes per tick.
5. Write the resulting events to a ``mido.MidiFile``.

All internal velocity math uses ``float64``; only the final MIDI write
casts to ``int``.
"""

from __future__ import annotations

from typing import Optional, Sequence

import mido
import numpy as np

# Factorio game tick → MIDI tick conversion.
# We use 480 ticks per beat at 120 BPM, so 1 game tick (1/60 s) = 8 MIDI ticks.
MIDI_TICKS_PER_BEAT: int = 480
GAME_TICKS_PER_BEAT: int = 30  # at reference 120 BPM
MIDI_TICKS_PER_GAME_TICK: int = MIDI_TICKS_PER_BEAT // GAME_TICKS_PER_BEAT  # = 16

# Default activation threshold: notes quieter than 5 % of max are dropped.
DEFAULT_ACTIVATION_THRESHOLD: float = 0.05

# Full MIDI note range.
MIDI_NOTE_COUNT: int = 128


def loudness_to_midi(
    loudness_data: Sequence[Sequence[float]],
    *,
    activation_threshold: float = DEFAULT_ACTIVATION_THRESHOLD,
    condense: bool = True,
    pitch_range: tuple[int, int] = (0, 127),
    max_polyphony: int = 0,
    min_note_ticks: int = 1,
    bpm: int = 120,
) -> mido.MidiFile:
    """Convert a full-spectrum loudness array to a ``mido.MidiFile``.

    Parameters
    ----------
    loudness_data : sequence of sequence of float
        ``loudness[tick][midi_note]`` where ``midi_note`` is 0..127.
        Each value should be in [0.0, 1.0].
    activation_threshold : float
        Minimum loudness (0.0–1.0) for a note to be considered active.
        Default 0.05.
    condense : bool
        If True, merge contiguous same-pitch notes into one sustained note.
        Default True.
    pitch_range : tuple[int, int]
        ``(min_midi, max_midi)`` inclusive.  Notes outside this range are
        dropped.  Default (0, 127).
    max_polyphony : int
        If > 0, only keep the *N* loudest notes per tick.  0 = unlimited.
    min_note_ticks : int
        Drop notes shorter than this many game ticks.  Default 1.
    bpm : int
        Beats per minute for the output MIDI file.  Default 120.

    Returns
    -------
    mido.MidiFile
        A MIDI file with one track containing the extracted notes.
        Tempo is set so that 1 game tick = 1 MIDI time division unit
        at the given *bpm*.
    """
    mid = mido.MidiFile(ticks_per_beat=MIDI_TICKS_PER_BEAT)

    track = mido.MidiTrack()
    mid.tracks.append(track)

    # Set tempo so that 1 beat = 60/bpm seconds, and 1 game tick
    # = (1/60)s = (bpm / 3600) beats = MIDI_TICKS_PER_BEAT * bpm / 3600 MIDI ticks.
    # With 480 TPQN at 120 BPM: 1 beat = 480 ticks = 0.5 s → 1 game tick = 16 ticks.
    # tempo = microseconds per beat = 60_000_000 / bpm
    tempo = mido.bpm2tempo(bpm)
    track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))

    if not loudness_data:
        return mid

    num_ticks = len(loudness_data)

    # Per-MIDI-note state: is_active, velocity_accumulator
    # Use float for velocity to avoid precision loss.
    note_active: list[bool] = [False] * MIDI_NOTE_COUNT
    note_velocity: list[float] = [0.0] * MIDI_NOTE_COUNT  # running max velocity
    note_start_tick: list[int] = [0] * MIDI_NOTE_COUNT  # game tick when note_on fired

    # Collect events as (game_tick, type, note, velocity)
    # We'll convert to MIDI delta ticks later.
    events: list[tuple[int, str, int, float]] = []  # (tick, "on"|"off", note, velocity)

    min_pitch, max_pitch = pitch_range

    for tick_idx in range(num_ticks):
        tick_loudness = list(loudness_data[tick_idx])

        # Determine which notes are active this tick
        active_this_tick: list[int] = []
        for note in range(min_pitch, max_pitch + 1):
            if note >= len(tick_loudness):
                break
            loudness = tick_loudness[note]
            if loudness >= activation_threshold:
                active_this_tick.append(note)

        # Top-N polyphony filter
        if max_polyphony > 0 and len(active_this_tick) > max_polyphony:
            active_this_tick.sort(
                key=lambda n: tick_loudness[n], reverse=True,
            )
            active_this_tick = active_this_tick[:max_polyphony]

        active_set = set(active_this_tick)

        # Note-off for notes that were active but aren't this tick
        for note in range(min_pitch, max_pitch + 1):
            if note_active[note] and note not in active_set:
                # Fire note_off
                events.append((
                    tick_idx, "off", note,
                    note_velocity[note],
                ))
                note_active[note] = False
                note_velocity[note] = 0.0

        # Note-on for newly active notes
        for note in active_this_tick:
            if not note_active[note]:
                if condense:
                    # Start a new note — record start tick
                    note_start_tick[note] = tick_idx
                    note_velocity[note] = tick_loudness[note]
                    note_active[note] = True
                    # Don't fire note_on yet — defer until note_off
                else:
                    events.append((
                        tick_idx, "on", note,
                        tick_loudness[note],
                    ))
                    note_active[note] = True
                    note_velocity[note] = tick_loudness[note]
            else:
                # Already active — update max velocity if condensing
                if condense:
                    note_velocity[note] = max(
                        note_velocity[note], tick_loudness[note],
                    )

    # Terminate any notes still active at end
    for note in range(min_pitch, max_pitch + 1):
        if note_active[note]:
            events.append((
                num_ticks, "off", note, note_velocity[note],
            ))

    # ── Condense: emit note_on events at start_tick ──────────────────
    if condense and events:
        # Gather note_off events to find note durations
        note_off_events: dict[int, list[tuple[int, float]]] = {}
        for tick, etype, note, velocity in events:
            if etype == "off":
                note_off_events.setdefault(note, []).append((tick, velocity))

        # Build condensed events: for each note, find contiguous active
        # regions from the loudness data directly.
        condensed: list[tuple[int, str, int, float]] = []

        for note in range(min_pitch, max_pitch + 1):
            i = 0
            while i < num_ticks:
                # Skip silent ticks
                if i >= len(loudness_data) or (
                    note >= len(loudness_data[i])
                ):
                    i += 1
                    continue
                loudness = loudness_data[i][note] if note < len(loudness_data[i]) else 0.0
                if loudness < activation_threshold:
                    i += 1
                    continue

                # Found start of a note
                start = i
                max_vel = loudness
                i += 1
                while i < num_ticks:
                    loudness = (
                        loudness_data[i][note]
                        if note < len(loudness_data[i])
                        else 0.0
                    )
                    if loudness < activation_threshold:
                        break
                    max_vel = max(max_vel, loudness)
                    i += 1
                end = i  # first silent tick after the note

                duration = end - start
                if duration >= min_note_ticks:
                    condensed.append((start, "on", note, max_vel))
                    condensed.append((end, "off", note, max_vel))

        events = condensed

    # ── Sort events: by tick, then "off" before "on" at same tick ────
    events.sort(key=lambda e: (e[0], 0 if e[1] == "off" else 1))

    # ── Convert game ticks → MIDI delta ticks ────────────────────────
    prev_midi_tick = 0
    for game_tick, etype, note, velocity_float in events:
        midi_abs_tick = game_tick * MIDI_TICKS_PER_GAME_TICK
        delta = max(0, midi_abs_tick - prev_midi_tick)

        vel_int = max(1, min(127, int(round(velocity_float * 127.0))))

        if etype == "on":
            track.append(mido.Message(
                "note_on", note=note, velocity=vel_int, time=delta,
            ))
        else:
            track.append(mido.Message(
                "note_off", note=note, velocity=0, time=delta,
            ))
        prev_midi_tick = midi_abs_tick

    return mid


def loudness_to_midi_file(
    loudness_data: Sequence[Sequence[float]],
    output_path: str,
    **kwargs,
) -> None:
    """Convert a loudness array to a MIDI file and save it to disk.

    Parameters
    ----------
    loudness_data : sequence of sequence of float
        As in :func:`loudness_to_midi`.
    output_path : str
        Path to write the ``.mid`` file.
    **kwargs
        Forwarded to :func:`loudness_to_midi`.
    """
    mid = loudness_to_midi(loudness_data, **kwargs)
    mid.save(output_path)
