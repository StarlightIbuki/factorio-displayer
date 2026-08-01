from __future__ import annotations

import argparse
import math
import os
import statistics
import sys

import mido

from .pitch_mapping import (  # pylint: disable=relative-beyond-top-level
    DRUM_KIT_NOTES,
    DRUM_NOTE_TO_PITCH,
    GM_DRUM_MAP,
    INSTRUMENT_MIDI_BASES,
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

# Very low notes that fall BELOW a melodic instrument's playable range
# (e.g. MIDI < 53 for piano) cannot be played by that instrument.  When they
# repeat like a beat, ``--map-drums`` routes them to a single low drum to
# simulate the beat instead of dropping them or folding them up into melody.
LOW_BEAT_DRUM = "kick-1"

# --- 2. AUTOMATED INSTRUMENT ROUTING ---
def map_gm_to_factorio(program, channel):  # pylint: disable=too-many-return-statements
    """Map a GM program number and channel to a Factorio instrument name."""
    if channel == 9:
        return 'drum'
    # GM2 percussion kits live at programs 120-127 (and some files use 128).
    if 120 <= program <= 128:
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

# --- 5. MIDI -> TICK_DATA ENGINE (float-based) ---

REFERENCE_TEMPO = 500_000  # 120 BPM -> baseline for ticks_per_beat calibration


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


def _fold_note(note: int, instrument: str = "piano", global_shift: int = 0) -> tuple[int, str]:
    """Fold a MIDI note into the 4-octave range of *instrument*.

    *global_shift* is applied first (in semitones, always a multiple of 12),
    then individual octave folding is done if the note is still out of range.

    Returns (folded_note, log_message).  If no folding was needed,
    log_message is empty.
    """
    midi_base = INSTRUMENT_MIDI_BASES.get(instrument, MIDI_BASE)
    note_min = midi_base
    note_max = midi_base + SPEAKER_COUNT - 1

    original = note
    if global_shift:
        note += global_shift

    if note_min <= note <= note_max:
        return note, ""

    while note < note_min:
        note += 12
    while note > note_max:
        note -= 12

    # Map MIDI note numbers to note names for readable logging
    note_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

    def _name(m: int) -> str:
        return f"{note_names[m % 12]}{m // 12 - 1}"

    return note, (
        f"Note MIDI {original} ({_name(original)}) folded -> "
        f"MIDI {note} ({_name(note)}) [{instrument}]"
    )


def find_optimal_octave_shift(notes: list[int], instrument: str) -> int:
    """Find the octave shift (multiple of 12) that maximises notes in range.

    For a list of MIDI note numbers and a target Factorio instrument, this
    returns the octave shift *k* such that ``note + k*12`` falls inside the
    instrument's 4-octave range for the largest number of unique notes.

    Tie-breaking: prefers *k=0*, then the smallest absolute value.
    Returns 0 (no shift) for empty inputs.
    """
    if not notes:
        return 0

    midi_base = INSTRUMENT_MIDI_BASES.get(instrument, MIDI_BASE)
    note_min = midi_base
    note_max = midi_base + SPEAKER_COUNT - 1

    # Consider octave shifts from -5 to +5 (covering extreme MIDI ranges)
    best_shift = 0
    best_count = -1
    for k in range(-5, 6):
        shift = k * 12
        count = sum(1 for n in notes if note_min <= n + shift <= note_max)
        if count > best_count:
            best_count = count
            best_shift = shift
        elif count == best_count:
            # Tie-break: prefer 0, then smaller absolute shift
            if best_shift == 0:
                continue
            if shift == 0:
                best_shift = 0
            elif abs(shift) < abs(best_shift):
                best_shift = shift

    return best_shift


def _adsr_shape(
    tick_in_note: int,
    note_duration: int,
    peak_loudness: float,
    attack_ticks: int,
    decay_ticks: int,
    sustain_level: float,
    release_ticks: int,
    attack_curve: float = 1.0,
    decay_curve: float = 1.0,
    release_curve: float = 1.0,
) -> float:
    """Compute loudness at a given tick within a note using ADSR envelope.

    Phases (with optional power-curve shaping):
    - Attack:  ramp 70% → 100% of peak_loudness  (0 .. attack_ticks)
    - Decay:   ramp 100% → sustain_level         (attack .. attack+decay)
    - Sustain: hold at sustain_level             (attack+decay .. dur-release)
    - Release: ramp sustain_level → 0%           (dur-release .. dur)

    Each phase uses ``progress ** curve_exp`` interpolation:
      curve_exp > 1.0  → gentle start, fast finish (convex)
      curve_exp = 1.0  → linear (backward-compatible default)
      curve_exp < 1.0  → fast start, gentle finish (concave)

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

    # Release (comes first in priority — last ticks of note)
    if release_ticks > 0 and tick_in_note >= release_start:
        progress = (tick_in_note - release_start) / max(1, release_ticks)
        shaped = progress ** max(0.01, release_curve)
        frac = sustain_level * (1.0 - shaped)
        return peak_loudness * max(0.0, frac)

    # Attack
    if attack_ticks > 0 and tick_in_note < attack_ticks:
        progress = tick_in_note / attack_ticks
        shaped = progress ** max(0.01, attack_curve)
        frac = 0.70 + 0.30 * shaped  # 70% → 100%
        return peak_loudness * frac

    # Decay
    decay_start = attack_ticks
    decay_end = attack_ticks + decay_ticks
    if decay_ticks > 0 and tick_in_note < decay_end:
        progress = (tick_in_note - decay_start) / decay_ticks
        shaped = progress ** max(0.01, decay_curve)
        frac = 1.0 - (1.0 - sustain_level) * shaped  # 100% → sustain_level
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
    attack_curve: float = 1.0,
    decay_curve: float = 1.0,
    release_curve: float = 1.0,
    use_global_shift: bool = True,
) -> list[list[float]]:
    """Convert a MIDI file to per-tick loudness data for all 48 speakers.

    Returns ``tick_data[tick][speaker_idx] = loudness`` as floats (0.0–100.0+).
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
        ADSR attack duration in game ticks (ramp 70%→100%).
    decay_ticks : int
        ADSR decay duration in game ticks (ramp 100%→sustain_level).
    sustain_level : float
        ADSR sustain level as fraction of peak (0.0–1.0, default 1.0).
    release_ticks : int
        ADSR release duration in game ticks (ramp sustain_level→0%).
    attack_curve : float
        Power-curve exponent for attack phase (>1=gentle, <1=snappy).
    decay_curve : float
        Power-curve exponent for decay phase (>1=gentle, <1=snappy).
    release_curve : float
        Power-curve exponent for release phase (>1=gentle, <1=snappy).
    use_global_shift : bool
        If True (default), compute an optimal octave shift that minimises
        the number of notes needing per-note folding. If False, use only
        per-note octave folding.
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

    # ── Pre-scan: optimal global octave shift ─────────────────────
    # Collect all unique note pitches across all tracks to find the
    # octave shift that minimises per-note folding.
    global_shift = 0
    if use_global_shift:
        all_note_pitches: list[int] = []
        for track in mid.tracks:
            for msg in track:
                if msg.type == "note_on" and msg.velocity > 0:
                    all_note_pitches.append(msg.note)
        global_shift = find_optimal_octave_shift(all_note_pitches, "piano")
        if global_shift != 0:
            sys.stderr.write(
                f"[midi_translator] Global octave shift: {global_shift:+d} semitones "
                f"({global_shift // 12:+d} octaves) [piano]\n"
            )

    # ── Collect note events from all tracks ─────────────────────────
    # Each note event: (pitch_idx, start_game_tick, end_game_tick, loudness)
    all_notes: list[tuple[int, float, float, float]] = []
    fold_logged: set[str] = set()  # avoid duplicate log messages

    for track_idx, track in enumerate(mid.tracks):
        is_melody = (track_idx == melody_track_idx)

        absolute_midi_tick = 0
        active_notes: dict[int, tuple[float, float]] = {}  # note -> (start_game_tick, loudness)

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

                # Octave folding (with optimal global shift)
                folded_note, log_msg = _fold_note(msg.note, global_shift=global_shift)
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
                folded_note, _ = _fold_note(msg.note, global_shift=global_shift)
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
                    attack_curve, decay_curve, release_curve,
                )
            else:
                shaped = loudness

            if shaped > 0.0:
                tick_data[tick][pitch_idx] += shaped

    # ── Emit processed MIDI for preview ─────────────────────────────
    if processed_midi_path is not None:
        _emit_processed_midi(mid, processed_midi_path)

    return tick_data


# ── multi-rail MIDI translation ────────────────────────────────────

def midi_to_multi_rail_tick_data(
    mid: mido.MidiFile,
    ticks_per_beat: int = 30,
    boost_melody: float = 1.0,
    velocity_scale: float = 1.0,
    attack_ticks: int = 0,
    decay_ticks: int = 0,
    sustain_level: float = 1.0,
    release_ticks: int = 0,
    attack_curve: float = 1.0,
    decay_curve: float = 1.0,
    release_curve: float = 1.0,
    map_drums: bool = False,
    use_global_shift: bool = True,
) -> tuple[list[str], list[list[list[float]]]]:
    """Convert a MIDI file to per-rail tick→loudness data.

    Auto-detects instruments from MIDI program changes and channel 9 drums.
    Returns ``(instruments, rail_data)`` where ``rail_data[r][tick][pitch]``
    gives the loudness for rail *r*, game tick *tick*, pitch index *pitch*.

    Each rail gets its own 48-speaker tick_data array folded to that
    instrument's range.  If only one instrument is detected, returns a
    single-rail list.

    use_global_shift : bool
        If True (default), compute per-instrument optimal octave shifts.
    """
    if not mid.tracks:
        return [], []

    # ── Tempo map (shared across rails) ────────────────────────────
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
        current = tempo_events[0][1]
        for t, tempo in tempo_events:
            if t <= midi_tick:
                current = tempo
            else:
                break
        return current

    # ── Pre-scan: determine instrument per channel ──────────────────
    channel_instrument: dict[int, str] = {}
    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += int(msg.time)
            ch = getattr(msg, 'channel', -1)
            if ch == 9:
                channel_instrument[ch] = 'drum'
            elif msg.type == 'program_change' and ch >= 0 and ch not in channel_instrument:
                channel_instrument[ch] = map_gm_to_factorio(msg.program, ch)

    # Default: any channel without a program change is piano
    for ch in range(16):
        if ch not in channel_instrument:
            channel_instrument[ch] = 'piano'
    # Channel 9 is always drum
    channel_instrument[9] = 'drum'

    # ── Collect note events per channel → rail ──────────────────────
    # Build a mapping: (channel) → rail_idx
    channel_rail: dict[int, int] = {}
    rail_instruments: list[str] = []
    # Scan all tracks once to discover which channels have notes
    channels_with_notes: set[int] = set()
    for track in mid.tracks:
        for msg in track:
            ch = getattr(msg, 'channel', -1)
            if msg.type == 'note_on' and msg.velocity > 0 and ch >= 0:
                channels_with_notes.add(ch)

    for ch in sorted(channels_with_notes):
        inst = channel_instrument.get(ch, 'piano')
        if inst not in rail_instruments:
            # Check if we already have this instrument; merge if so
            rail_instruments.append(inst)
        channel_rail[ch] = rail_instruments.index(inst)

    num_rails = len(rail_instruments)
    if num_rails == 0:
        return [], []

    # Re-index: merge duplicate instruments
    inst_to_rail: dict[str, int] = {}
    compact_instruments: list[str] = []
    old_to_new: dict[int, int] = {}
    for ri, inst in enumerate(rail_instruments):
        if inst not in inst_to_rail:
            inst_to_rail[inst] = len(compact_instruments)
            compact_instruments.append(inst)
        old_to_new[ri] = inst_to_rail[inst]

    # When map_drums is on, dedicated percussion channels (channel 9, or a
    # channel whose program is a GM percussion kit) are classified 'drum'
    # above and routed to a drum rail by the channel_instrument logic below.
    #
    # In addition, for *melodic* channels we only treat notes that fall BELOW
    # the instrument's lowest playable pitch (e.g. MIDI < 53 for piano) as
    # drums.  Those very low notes cannot be played by the instrument and
    # typically form a repeating bass/kick beat, so they are routed to a
    # low drum (kick-1) to simulate the beat.
    #
    # We deliberately do NOT split notes from the GM drum range (24-81) that
    # are still inside the instrument's range: that range overlaps melodic
    # instruments, so a piano/bass track would otherwise be misclassified and
    # fabricated into fake kick/snare/triangle sounds out of melody notes.
    drum_channel_offset = 100  # virtual channel IDs for low-beat drum rails
    if map_drums:
        for ch in sorted(channels_with_notes):
            inst = channel_instrument.get(ch, 'piano')
            if inst == 'drum':
                continue  # already a dedicated percussion channel
            inst_min = FACTORIO_INSTRUMENTS[inst]['min']
            has_low_beat = False
            for track in mid.tracks:
                for msg in track:
                    if (getattr(msg, 'channel', -1) == ch
                            and msg.type == 'note_on' and msg.velocity > 0
                            and msg.note < inst_min):
                        has_low_beat = True
                        break
                if has_low_beat:
                    break
            if has_low_beat:
                virt_ch = drum_channel_offset + ch
                channel_instrument[virt_ch] = 'drum'
                channels_with_notes.add(virt_ch)
                sys.stderr.write(
                    f"[midi_translator] Channel {ch} ({inst}) has notes below "
                    f"its range (<{inst_min}) — routing them to a low drum "
                    f"({LOW_BEAT_DRUM}) to simulate the beat\n"
                )

    # Rebuild channel_rail with possibly new drum channels
    channel_rail = {}
    rail_instruments = []
    for ch in sorted(channels_with_notes):
        inst = channel_instrument.get(ch, 'piano')
        if inst not in rail_instruments:
            rail_instruments.append(inst)
        channel_rail[ch] = rail_instruments.index(inst)

    # Deduplicate instrument list
    inst_to_rail = {}
    compact_instruments: list[str] = []
    old_to_new: dict[int, int] = {}
    for ri, inst in enumerate(rail_instruments):
        if inst not in inst_to_rail:
            inst_to_rail[inst] = len(compact_instruments)
            compact_instruments.append(inst)
        old_to_new[ri] = inst_to_rail[inst]
    rail_instruments = compact_instruments
    num_rails = len(rail_instruments)

    # Remap channel_rail
    for ch in list(channel_rail.keys()):
        channel_rail[ch] = old_to_new[channel_rail[ch]]

    # ── Melody detection (per instrument group) ─────────────────────
    melody_channel: dict[int, int] = {}  # inst_idx → channel
    if boost_melody != 1.0:
        inst_pitches: dict[int, list[tuple[int, float]]] = {ri: [] for ri in range(num_rails)}
        for track in mid.tracks:
            abs_tick = 0
            for msg in track:
                abs_tick += int(msg.time)
                ch = getattr(msg, 'channel', -1)
                if msg.type == 'note_on' and msg.velocity > 0 and ch in channel_rail:
                    ri = channel_rail[ch]
                    inst_pitches[ri].append(msg.note)
        for ri, pitches in inst_pitches.items():
            if pitches:
                # Find the channel with highest avg pitch for this instrument
                # (simplified: just mark it for boost)
                melody_channel[ri] = max(
                    set(ch for ch, cr in channel_rail.items() if cr == ri),
                    key=lambda c: c,
                )

    # ── Pre-scan: optimal global octave shift per instrument ─────
    # Collect unique note pitches per non-drum rail to find the
    # octave shift that minimises per-note folding for each instrument.
    rail_global_shifts: dict[int, int] = {}
    if use_global_shift:
        rail_note_pitches: dict[int, list[int]] = {ri: [] for ri in range(num_rails)}
        for track in mid.tracks:
            for msg in track:
                ch = getattr(msg, 'channel', -1)
                if msg.type == 'note_on' and msg.velocity > 0 and ch in channel_rail:
                    ri = channel_rail[ch]
                    inst = rail_instruments[ri]
                    if inst != 'drum' and not (map_drums
                                               and channel_instrument.get(ch) != 'drum'
                                               and msg.note < FACTORIO_INSTRUMENTS[
                                                   channel_instrument.get(ch, 'piano')
                                               ]['min']):
                        rail_note_pitches[ri].append(msg.note)
        for ri, pitches in rail_note_pitches.items():
            shift = find_optimal_octave_shift(pitches, rail_instruments[ri])
            rail_global_shifts[ri] = shift
            if shift != 0:
                sys.stderr.write(
                    f"[midi_translator] Global octave shift: {shift:+d} semitones "
                    f"({shift // 12:+d} octaves) [{rail_instruments[ri]}]\n"
                )

    # ── Collect note events per rail (by channel) ───────────────────
    rail_notes: list[list[tuple[int, float, float, float]]] = [
        [] for _ in range(num_rails)
    ]
    fold_logged: set[str] = set()

    for track in mid.tracks:
        # Track per-channel active notes within this track pass
        active_notes: dict[int, dict[int, tuple[float, float]]] = {}  # ch → note → (start, loudness)
        absolute_midi_tick = 0

        for msg in track:
            absolute_midi_tick += int(msg.time)

            if msg.type == 'set_tempo':
                continue

            ch = getattr(msg, 'channel', -1)
            # Route very-low notes (below the instrument's playable range) to
            # the virtual low-beat drum channel when map_drums is on.
            if (map_drums
                    and hasattr(msg, 'note')
                    and channel_instrument.get(ch) != 'drum'
                    and msg.note < FACTORIO_INSTRUMENTS[
                        channel_instrument.get(ch, 'piano')
                    ]['min']):
                virt_ch = drum_channel_offset + ch
                if virt_ch in channel_rail:
                    ch = virt_ch

            if ch not in channel_rail:
                continue

            ri = channel_rail[ch]
            inst = rail_instruments[ri]
            is_melody = melody_channel.get(ri) == ch
            is_drum_mapped = inst == 'drum'

            if ch not in active_notes:
                active_notes[ch] = {}

            if msg.type == 'note_on' and msg.velocity > 0:
                tempo = _get_tempo_at(absolute_midi_tick)
                start_game_tick = _midi_tick_to_game_tick(
                    absolute_midi_tick, mid.ticks_per_beat,
                    tempo, ticks_per_beat,
                )
                velocity = float(msg.velocity)
                if is_melody:
                    velocity = min(127.0, velocity * boost_melody)
                loudness = velocity / 127.0 * 100.0 * velocity_scale

                if is_drum_mapped:
                    if ch >= drum_channel_offset:
                        # Virtual low-beat channel: simulate the beat with a
                        # single low drum (kick), not a full GM drum map.
                        pitch_idx = DRUM_NOTE_TO_PITCH[LOW_BEAT_DRUM]
                    else:
                        # Dedicated percussion channel: GM note → drum-kit name
                        drum_name = GM_DRUM_MAP.get(msg.note)
                        if drum_name is None:
                            continue  # unmapped drum note, skip
                        pitch_idx = DRUM_NOTE_TO_PITCH.get(drum_name)
                        if pitch_idx is None:
                            continue
                else:
                    folded_note, log_msg = _fold_note(
                        msg.note, instrument=inst,
                        global_shift=rail_global_shifts.get(ri, 0),
                    )
                    if log_msg and log_msg not in fold_logged:
                        fold_logged.add(log_msg)
                        sys.stderr.write(f"[midi_translator] {log_msg}\n")
                    pitch_idx = midi_to_pitch_index(folded_note)
                    if pitch_idx is None:
                        continue

                active_notes[ch][msg.note] = (start_game_tick, loudness)

            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                entry = active_notes[ch].pop(msg.note, None)
                if entry is None:
                    continue
                start_game_tick, loudness = entry
                tempo = _get_tempo_at(absolute_midi_tick)
                end_game_tick = _midi_tick_to_game_tick(
                    absolute_midi_tick, mid.ticks_per_beat,
                    tempo, ticks_per_beat,
                )
                if is_drum_mapped:
                    if ch >= drum_channel_offset:
                        # Virtual low-beat channel: single low drum (kick)
                        pitch_idx = DRUM_NOTE_TO_PITCH[LOW_BEAT_DRUM]
                    else:
                        drum_name = GM_DRUM_MAP.get(msg.note)
                        if drum_name is None:
                            continue
                        pitch_idx = DRUM_NOTE_TO_PITCH.get(drum_name)
                        if pitch_idx is None:
                            continue
                else:
                    folded_note, _ = _fold_note(
                        msg.note, instrument=inst,
                        global_shift=rail_global_shifts.get(ri, 0),
                    )
                    pitch_idx = midi_to_pitch_index(folded_note)
                    if pitch_idx is None:
                        continue

                rail_notes[ri].append((pitch_idx, start_game_tick, end_game_tick, loudness))

    # ── Build per-rail tick_data ────────────────────────────────────
    all_notes_flat = [n for rn in rail_notes for n in rn]
    if not all_notes_flat:
        return rail_instruments, [[[0.0] * SPEAKER_COUNT]]

    max_game_tick = max(n[2] for n in all_notes_flat)
    num_ticks = int(math.ceil(max_game_tick))
    use_adsr = (
        attack_ticks > 0 or decay_ticks > 0
        or sustain_level < 1.0 or release_ticks > 0
    )

    def _build_one_rail(ri: int) -> list[list[float]]:
        """Build tick_data for a single rail (thread-safe, no shared state)."""
        td: list[list[float]] = [[0.0] * SPEAKER_COUNT for _ in range(num_ticks)]
        for pitch_idx, start_t, end_t, loudness in rail_notes[ri]:
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
                        attack_curve, decay_curve, release_curve,
                    )
                else:
                    shaped = loudness
                if shaped > 0.0:
                    td[tick][pitch_idx] += shaped
        # For drum rails, cap per-pitch loudness to prevent volume stacking
        # when many GM notes map to the same Factorio drum sound.
        if rail_instruments[ri] == 'drum':
            for tick in range(num_ticks):
                for p in range(SPEAKER_COUNT):
                    if td[tick][p] > 100.0:
                        td[tick][p] = 100.0
        return td

    # Process rails in parallel — each rail is fully independent.
    if num_rails > 1:
        import concurrent.futures
        rail_data = [None] * num_rails
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(num_rails, 4)) as executor:
            future_to_ri = {
                executor.submit(_build_one_rail, ri): ri
                for ri in range(num_rails)
            }
            for future in concurrent.futures.as_completed(future_to_ri):
                ri = future_to_ri[future]
                rail_data[ri] = future.result()
    else:
        rail_data = [_build_one_rail(0)]

    return rail_instruments, rail_data


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

    from .._unicode_io import mido_save  # pylint: disable=import-outside-toplevel,relative-beyond-top-level

    mido_save(out, path)
    sys.stderr.write(f"[midi_translator] Processed MIDI saved to: {path}\n")


# --- MAIN PIPELINE ---
def translate_to_factorio(input_file, output_file, boost=False):
    from .._unicode_io import mido_open  # pylint: disable=import-outside-toplevel,relative-beyond-top-level

    print(f"Loading {input_file}...")
    mid = mido_open(input_file)

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

    from .._unicode_io import mido_save  # pylint: disable=import-outside-toplevel,relative-beyond-top-level

    mido_save(processed_mid, output_file)
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
