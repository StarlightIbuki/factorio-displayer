from __future__ import annotations

import argparse
import math
import os
import statistics
import sys

import mido

from .pitch_mapping import (  # pylint: disable=relative-beyond-top-level
    MIDI_BASE,
    SPEAKER_COUNT,
    midi_to_pitch_index,
)

# --- 1. FACTORIO INSTRUMENT DEFINITIONS ---
FACTORIO_INSTRUMENTS = {
    'piano': {'min': 53, 'max': 100},          # F3-E7
    'bass': {'min': 41, 'max': 76},            # F2-E5
    'celesta': {'min': 77, 'max': 112},        # F5-E8
    'plucked': {'min': 65, 'max': 100},        # F4-E7
    'drum': {'min': 53, 'max': 88}             # F3-E6
}

# --- 2. AUTOMATED INSTRUMENT ROUTING ---
def map_gm_to_factorio(program, channel):  # pylint: disable=too-many-return-statements
    """Map a GM program number and channel to a Factorio instrument name."""
    if channel == 9:
        return 'drum'
    if 0 <= program <= 7:
        return 'piano'
    if 8 <= program <= 15:
        return 'celesta'
    if 24 <= program <= 31:
        return 'plucked'
    if 32 <= program <= 39:
        return 'bass'
    if 80 <= program <= 87:
        return 'bass'
    return 'piano'

# --- 3. CONTEXT-AWARE OCTAVE FOLDING ---
def fold_octaves(track_notes, target_instrument):
    """Fold notes to fit within the target instrument's range."""
    if not track_notes:
        return []

    target_range = FACTORIO_INSTRUMENTS[target_instrument]
    target_center = (target_range['min'] + target_range['max']) // 2

    pitches = [msg.note for msg in track_notes if msg.type == 'note_on' and msg.velocity > 0]
    if not pitches:
        return track_notes
    median_pitch = statistics.median(pitches)

    shift_amount = target_center - median_pitch
    octave_shift = round(shift_amount / 12) * 12

    folded_notes = []
    for msg in track_notes:
        if msg.type in ('note_on', 'note_off'):
            new_note = msg.note + octave_shift
            while new_note < target_range['min']: new_note += 12
            while new_note > target_range['max']: new_note -= 12
            folded_notes.append(msg.copy(note=int(new_note)))
        else:
            folded_notes.append(msg)

    return folded_notes

# --- 4. UNIFIED TIMING & DYNAMICS ENGINE ---
def process_timing(mid, min_note_gap_sec=0.06, chord_tolerance_sec=0.01, boost_melody=False):  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    """Process MIDI timing: normalize note gaps, detect melody, scale velocity."""
    new_mid = mido.MidiFile()
    new_mid.ticks_per_beat = mid.ticks_per_beat

    # Pre-scan: Find the track with the highest average pitch to act as the "Melody"
    melody_track_idx = -1
    if boost_melody:
        highest_avg_pitch = -1
        for i, track in enumerate(mid.tracks):
            pitches = [msg.note for msg in track if msg.type == 'note_on' and msg.velocity > 0]
            if pitches:
                avg = sum(pitches) / len(pitches)
                if avg > highest_avg_pitch:
                    highest_avg_pitch = avg
                    melody_track_idx = i

    for i, track in enumerate(mid.tracks):
        is_melody_track = (i == melody_track_idx)
        absolute_events = []

        absolute_tick = 0
        last_note_on_time = -1.0
        current_tempo = mido.bpm2tempo(120)
        dropped_notes: set[int] = set()  # orphaned note_offs (pruned short notes)

        # --- PASS 1: Map to Absolute Timeline ---
        for msg in track:
            scaled_time = int(msg.time)
            absolute_tick += scaled_time

            if msg.type == 'set_tempo':
                current_tempo = msg.tempo
                new_msg = msg.copy(tempo=int(msg.tempo), time=0)
                absolute_events.append({'tick': absolute_tick, 'msg': new_msg, 'order': 0})
                continue

            is_note_off = msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0)

            if is_note_off:
                if msg.note in dropped_notes:
                    dropped_notes.remove(msg.note)
                    continue
                new_msg = msg.copy(time=0)
                absolute_events.append({'tick': absolute_tick, 'msg': new_msg, 'order': 2})
                continue

            if msg.type == 'note_on' and msg.velocity > 0:
                time_in_sec = mido.tick2second(absolute_tick, mid.ticks_per_beat, current_tempo)
                time_since_last = time_in_sec - last_note_on_time

                is_chord = time_since_last <= chord_tolerance_sec

                # Prune notes that violate the gap (but preserve chords)
                if not is_chord and time_since_last < min_note_gap_sec:
                    dropped_notes.add(msg.note)
                    continue

                last_note_on_time = time_in_sec

                # Dynamic Velocity Scaling & Melody Boost
                base_velocity = msg.velocity
                if is_melody_track:
                    base_velocity = min(127, int(base_velocity * 1.5))  # 50% Volume Boost

                scaled_velocity = int((base_velocity / 127.0) * 100)  # Map to 0-100
                msg = msg.copy(velocity=max(1, scaled_velocity), time=0)

                absolute_events.append({'tick': absolute_tick, 'msg': msg, 'order': 1})
            else:
                new_msg = msg.copy(time=0)
                absolute_events.append({'tick': absolute_tick, 'msg': new_msg, 'order': 0})

        # --- PASS 2: Sort and Rebuild Delta Time ---
        absolute_events.sort(key=lambda e: (e['tick'], e['order']))

        new_track = mido.MidiTrack()
        new_mid.tracks.append(new_track)

        prev_tick = 0
        for event in absolute_events:
            msg = event['msg']
            delta_tick = event['tick'] - prev_tick
            msg.time = max(0, delta_tick)
            new_track.append(msg)
            prev_tick = event['tick']

    return new_mid

# --- 5. MIDI �?TICK_DATA ENGINE (float-based) ---

REFERENCE_TEMPO = 500_000  # 120 BPM �?baseline for ticks_per_beat calibration


def _midi_tick_to_game_tick(
    midi_tick: int,
    ticks_per_beat_midi: int,
    tempo: int,
    game_ticks_per_beat: int,
) -> float:
    """Convert an absolute MIDI tick to a game tick (float).

    ``game_ticks_per_beat`` is calibrated at 120 BPM (0.5 s/beat).
    At ``game_ticks_per_beat=30``, one game tick = 1/60 s = 1 Factorio tick,
    giving real-time playback regardless of the MIDI's actual tempo.
    """
    seconds = mido.tick2second(midi_tick, ticks_per_beat_midi, tempo)
    # At reference 120 BPM: 1 beat = 0.5 s = game_ticks_per_beat game ticks
    # So 1 game tick = 0.5 / game_ticks_per_beat seconds
    # game_ticks = seconds / (0.5 / game_ticks_per_beat)
    #            = seconds * 2 * game_ticks_per_beat
    return seconds * 2.0 * game_ticks_per_beat


def _fold_note(note: int) -> tuple[int, str]:
    """Fold a MIDI note into the F3–E7 range (53�?00).

    Returns (folded_note, log_message).  If no folding was needed,
    log_message is empty.
    """
    if MIDI_BASE <= note <= MIDI_BASE + SPEAKER_COUNT - 1:
        return note, ""

    original = note
    while note < MIDI_BASE:
        note += 12
    while note > MIDI_BASE + SPEAKER_COUNT - 1:
        note -= 12

    # Map MIDI note numbers to note names for readable logging
    note_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

    def _name(m: int) -> str:
        return f"{note_names[m % 12]}{m // 12 - 1}"

    return note, f"Note MIDI {original} ({_name(original)}) folded �?MIDI {note} ({_name(note)})"


def _adsr_shape(
    tick_in_note: int,
    note_duration: int,
    peak_loudness: float,
    attack_ticks: int,
    decay_ticks: int,
    sustain_level: float,
    release_ticks: int,
) -> float:
    """Compute loudness at a given tick within a note using ADSR envelope.

    Phases:
    - Attack:  ramp 70% �?100% of peak_loudness  (0 .. attack_ticks)
    - Decay:   ramp 100% �?sustain_level          (attack .. attack+decay)
    - Sustain: hold at sustain_level               (attack+decay .. dur-release)
    - Release: ramp sustain_level �?0%            (dur-release .. dur)

    If the note is too short for full attack+release, phases are shortened
    proportionally so the shape still fits.
    """
    if note_duration <= 0:
        return 0.0

    total_env = attack_ticks + decay_ticks + release_ticks

    if total_env > 0 and note_duration < total_env:
        # Shorten proportionally
        scale = note_duration / total_env
        attack_ticks = max(0, int(attack_ticks * scale))
        decay_ticks = max(0, int(decay_ticks * scale))
        release_ticks = max(0, int(release_ticks * scale))

    release_start = note_duration - release_ticks

    # Release (comes first in priority �?last ticks of note)
    if release_ticks > 0 and tick_in_note >= release_start:
        progress = (tick_in_note - release_start) / max(1, release_ticks)
        frac = sustain_level * (1.0 - progress)
        return peak_loudness * max(0.0, frac)

    # Attack
    if attack_ticks > 0 and tick_in_note < attack_ticks:
        progress = tick_in_note / attack_ticks
        frac = 0.70 + 0.30 * progress  # 70% �?100%
        return peak_loudness * frac

    # Decay
    decay_start = attack_ticks
    decay_end = attack_ticks + decay_ticks
    if decay_ticks > 0 and tick_in_note < decay_end:
        progress = (tick_in_note - decay_start) / decay_ticks
        frac = 1.0 - (1.0 - sustain_level) * progress  # 100% �?sustain_level
        return peak_loudness * frac

    # Sustain
    return peak_loudness * sustain_level


def midi_to_tick_data(
    mid: mido.MidiFile,
    ticks_per_beat: int = 30,
    boost_melody: float = 1.0,
    velocity_scale: float = 1.0,
    processed_midi_path: str | None = None,
    attack_ticks: int = 0,
    decay_ticks: int = 0,
    sustain_level: float = 1.0,
    release_ticks: int = 0,
) -> list[list[float]]:
    """Convert a MIDI file to per-tick loudness data for all 48 speakers.

    Returns ``tick_data[tick][speaker_idx] = loudness`` as floats (0.0�?00.0+).
    The caller is responsible for clipping and rounding to int.

    Parameters
    ----------
    mid : mido.MidiFile
        The parsed MIDI file.
    ticks_per_beat : int
        Game ticks per quarter note, calibrated at 120 BPM reference.
        At 30 (default), one game tick = 1/60 s = 1 Factorio tick,
        giving real-time playback at any tempo.
    boost_melody : float
        Multiplier applied to the melody track's velocity (default 1.0 = off).
    velocity_scale : float
        Global multiplier applied to all loudness values (default 1.0).
    processed_midi_path : str | None
        If given, writes an octave-folded .mid file for preview in any player.
    attack_ticks : int
        ADSR attack duration in game ticks (ramp 70%�?00%).
    decay_ticks : int
        ADSR decay duration in game ticks (ramp 100%→sustain_level).
    sustain_level : float
        ADSR sustain level as fraction of peak (0.0�?.0, default 1.0).
    release_ticks : int
        ADSR release duration in game ticks (ramp sustain_level�?%).
    """
    if not mid.tracks:
        return []

    # ── Build global tempo map (tempo events are global across tracks) ──
    # tempo_map: sorted list of (absolute_midi_tick, tempo_us_per_beat)
    tempo_events: list[tuple[int, int]] = []
    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += int(msg.time)
            if msg.type == "set_tempo":
                tempo_events.append((abs_tick, msg.tempo))
    tempo_events.sort(key=lambda e: e[0])
    if not tempo_events:
        tempo_events = [(0, mido.bpm2tempo(120))]

    def _get_tempo_at(midi_tick: int) -> int:
        """Return the tempo (µs/beat) in effect at *midi_tick*."""
        current = tempo_events[0][1]
        for t, tempo in tempo_events:
            if t <= midi_tick:
                current = tempo
            else:
                break
        return current

    # ── Pre-scan: melody detection ──────────────────────────────────
    melody_track_idx = -1
    if boost_melody != 1.0:
        highest_avg_pitch = -1.0
        for i, track in enumerate(mid.tracks):
            pitches = [
                msg.note for msg in track
                if msg.type == "note_on" and msg.velocity > 0
            ]
            if pitches:
                avg = sum(pitches) / len(pitches)
                if avg > highest_avg_pitch:
                    highest_avg_pitch = avg
                    melody_track_idx = i

    # ── Collect note events from all tracks ─────────────────────────
    # Each note event: (pitch_idx, start_game_tick, end_game_tick, loudness)
    all_notes: list[tuple[int, float, float, float]] = []
    fold_logged: set[str] = set()  # avoid duplicate log messages

    for track_idx, track in enumerate(mid.tracks):
        is_melody = (track_idx == melody_track_idx)

        absolute_midi_tick = 0
        active_notes: dict[int, tuple[float, float]] = {}  # note �?(start_game_tick, loudness)

        for msg in track:
            absolute_midi_tick += int(msg.time)

            if msg.type == "set_tempo":
                continue  # handled by global tempo map

            if msg.type == "note_on" and msg.velocity > 0:
                tempo = _get_tempo_at(absolute_midi_tick)
                start_game_tick = _midi_tick_to_game_tick(
                    absolute_midi_tick, mid.ticks_per_beat,
                    tempo, ticks_per_beat,
                )

                # Compute float loudness from velocity
                velocity = float(msg.velocity)
                if is_melody:
                    velocity = min(127.0, velocity * boost_melody)
                loudness = velocity / 127.0 * 100.0 * velocity_scale

                # Octave folding
                folded_note, log_msg = _fold_note(msg.note)
                if log_msg and log_msg not in fold_logged:
                    fold_logged.add(log_msg)
                    sys.stderr.write(f"[midi_translator] {log_msg}\n")

                pitch_idx = midi_to_pitch_index(folded_note)
                if pitch_idx is None:
                    # Shouldn't happen after folding, but guard
                    continue

                active_notes[msg.note] = (start_game_tick, loudness)

            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                entry = active_notes.pop(msg.note, None)
                if entry is None:
                    continue
                start_game_tick, loudness = entry
                tempo = _get_tempo_at(absolute_midi_tick)
                end_game_tick = _midi_tick_to_game_tick(
                    absolute_midi_tick, mid.ticks_per_beat,
                    tempo, ticks_per_beat,
                )

                # Fold the note for pitch_idx
                folded_note, _ = _fold_note(msg.note)
                pitch_idx = midi_to_pitch_index(folded_note)
                if pitch_idx is None:
                    continue

                all_notes.append((pitch_idx, start_game_tick, end_game_tick, loudness))

            # Non-note messages: skip (they're not relevant for tick_data)

    if not all_notes:
        return []

    # ── Determine tick range ────────────────────────────────────────
    max_game_tick = max(n[2] for n in all_notes)
    num_ticks = int(math.ceil(max_game_tick))

    # ── Build tick_data by summing active notes' loudness ───────────
    use_adsr = attack_ticks > 0 or decay_ticks > 0 or sustain_level < 1.0 or release_ticks > 0
    tick_data: list[list[float]] = [[0.0] * SPEAKER_COUNT for _ in range(num_ticks)]

    for pitch_idx, start_t, end_t, loudness in all_notes:
        start_i = int(start_t)
        end_i = int(math.ceil(end_t))
        note_duration = end_i - start_i
        if note_duration <= 0:
            continue

        for tick in range(max(0, start_i), min(num_ticks, end_i)):
            tick_in_note = tick - start_i

            if use_adsr:
                shaped = _adsr_shape(
                    tick_in_note, note_duration, loudness,
                    attack_ticks, decay_ticks, sustain_level, release_ticks,
                )
            else:
                shaped = loudness

            if shaped > 0.0:
                tick_data[tick][pitch_idx] += shaped

    # ── Emit processed MIDI for preview ─────────────────────────────
    if processed_midi_path is not None:
        _emit_processed_midi(mid, processed_midi_path)

    return tick_data


def _emit_processed_midi(mid: mido.MidiFile, path: str) -> None:
    """Write an octave-folded version of the MIDI for user preview."""
    out = mido.MidiFile(ticks_per_beat=mid.ticks_per_beat)

    for track in mid.tracks:
        new_track = mido.MidiTrack()
        out.tracks.append(new_track)
        for msg in track:
            if msg.type in ("note_on", "note_off"):
                folded, _ = _fold_note(msg.note)
                new_track.append(msg.copy(note=folded))
            else:
                new_track.append(msg)

    out.save(path)
    sys.stderr.write(f"[midi_translator] Processed MIDI saved to: {path}\n")


# --- MAIN PIPELINE ---
def translate_to_factorio(input_file, output_file, boost=False):
    print(f"Loading {input_file}...")
    mid = mido.MidiFile(input_file)

    print(f"Processing Timing... (Boost: {boost})")
    mid = process_timing(mid, boost_melody=boost)

    processed_mid = mido.MidiFile()
    processed_mid.ticks_per_beat = mid.ticks_per_beat

    for track in mid.tracks:
        new_track = mido.MidiTrack()
        processed_mid.tracks.append(new_track)

        current_instrument = 'piano'
        for msg in track:
            if msg.type == 'program_change':
                current_instrument = map_gm_to_factorio(msg.program, msg.channel)
            elif hasattr(msg, 'channel') and msg.channel == 9:
                current_instrument = 'drum'

        folded_track = fold_octaves(track, current_instrument)
        for msg in folded_track:
            new_track.append(msg)

    processed_mid.save(output_file)
    print(f"Translation complete. Saved to {output_file}.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert MIDI for Factorio Blueprinting.")
    parser.add_argument("input", help="Path to the input MIDI file")
    parser.add_argument("output", help="Path to save the output MIDI file")
    parser.add_argument("--boost-melody", action="store_true", help="Auto-detect highest track and boost volume by 1.5x")

    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"Error: Could not find file '{args.input}'")
    else:
        translate_to_factorio(args.input, args.output, boost=args.boost_melody)
