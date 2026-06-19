"""Pitch-to-signal mapping for the 48-speaker audio matrix.

Maps 48 pitches (12 semitones × 4 octaves) to unique (signal_name, quality) pairs.
The mapping uses alphabet signals for natural notes and +10 offset for sharps,
with Space Age quality tiers encoding the octave.

Semitone layout (starting from F)::

    F  F#  G  G#  A  A#  B  C  C#  D  D#  E
    ↓   ↓   ↓   ↓   ↓   ↓  ↓  ↓   ↓   ↓   ↓  ↓
    F   P   G   Q   A   K  B  C   M   D   N   E
"""

from __future__ import annotations

from typing import Iterator

# 4-octave range: octaves 3, 4, 5, 6
AUDIO_OCTAVES: list[int] = [3, 4, 5, 6]

# 5 quality tiers — only the first 4 are used for the 4-octave speaker matrix
AUDIO_QUALITIES: list[str] = ["normal", "uncommon", "rare", "epic"]

# MIDI note number of the lowest pitch (F3 = MIDI 53)
MIDI_BASE: int = 53

# Number of pitches per instrument (12 semitones × 4 octaves)
SPEAKER_COUNT: int = 48

# MIDI base per Factorio instrument — the F-aligned start of each 4-octave window
INSTRUMENT_MIDI_BASES: dict[str, int] = {
    "piano": 53,     # F3-E7  (53-100)
    "bass": 41,      # F2-E6  (41-88)
    "celesta": 65,   # F4-E7  (65-112)
    "plucked": 65,   # F4-E7  (65-100)
    "drum": 53,      # F3-E7  (53-100) — only first 17 slots used when map_drums
}

# GM drum note → Factorio drum-kit note name mapping
# Maps standard GM percussion MIDI notes (35–81) to Factorio's 17 drum-kit sounds.
# When ``--map-drums`` is on, low drum notes use this instead of octave folding.
GM_DRUM_MAP: dict[int, str] = {
    # Extended low range (some MIDI files use 24-34 for drums)
    24: "kick-1",       # Low C
    25: "snare-1",      # Low C#
    26: "kick-2",       # Low D
    27: "snare-2",      # Low D#
    28: "snare-3",      # Low E
    29: "kick-1",       # Low F
    30: "hat-1",        # Low F#
    31: "hat-2",        # Low G
    32: "perc-1",       # Low G#
    33: "perc-2",       # Low A
    34: "crash",        # Low A#
    # Standard GM drum map (35-81)
    35: "kick-1",       # Acoustic Bass Drum
    36: "kick-2",       # Bass Drum 1
    37: "snare-3",      # Side Stick → snare-3
    38: "snare-1",      # Acoustic Snare
    39: "clap",         # Hand Clap
    40: "snare-2",      # Electric Snare
    41: "perc-1",       # Low Floor Tom
    42: "hat-1",        # Closed Hi-hat
    43: "perc-2",       # High Floor Tom
    44: "hat-1",        # Pedal Hi-hat → hat-1
    45: "perc-1",       # Low Tom
    46: "hat-2",        # Open Hi-hat
    47: "perc-2",       # Low-Mid Tom
    48: "perc-1",       # Hi-Mid Tom
    49: "crash",        # Crash Cymbal 1
    50: "perc-2",       # High Tom
    51: "hat-2",        # Ride Cymbal 1 → hat-2
    52: "reverse-cymbal",  # Chinese Cymbal
    53: "hat-2",        # Ride Bell → hat-2
    54: "high-q",       # Tambourine
    55: "crash",        # Splash Cymbal → crash
    56: "cowbell",      # Cowbell
    57: "crash",        # Crash Cymbal 2
    58: "fx",           # Vibraslap → fx
    59: "hat-2",        # Ride Cymbal 2 → hat-2
    69: "shaker",       # Cabasa
    70: "shaker",       # Maracas
    75: "clap",         # Claves → clap
    80: "triangle",     # Mute Triangle
    81: "triangle",     # Open Triangle
}

# Factorio drum-kit note names in their internal index order (0..16)
DRUM_KIT_NOTES: list[str] = [
    "kick-1", "kick-2",
    "snare-1", "snare-2", "snare-3",
    "hat-1", "hat-2",
    "fx", "high-q",
    "perc-1", "perc-2",
    "crash", "reverse-cymbal",
    "clap", "shaker", "cowbell", "triangle",
]

# Reverse: drum note name → pitch_index within the drum rail
DRUM_NOTE_TO_PITCH: dict[str, int] = {
    name: i for i, name in enumerate(DRUM_KIT_NOTES)
}


def _semitone_to_letter(semitone: int) -> str:
    """Convert a chromatic semitone index (0=F, 1=F#, …, 11=E) to a signal letter.

    Natural notes use their own letter (F→F, G→G, etc.).
    Accidentals/sharps add +10 to the natural note's letter (F→P, G→Q, etc.).
    """
    # Natural note letters for the 7 white keys starting from F
    natural_letters = ["F", "G", "A", "B", "C", "D", "E"]
    # Which semitone indices are natural (0=F, 2=G, 4=A, 6=B, 7=C, 9=D, 11=E)
    natural_semitones = [0, 2, 4, 6, 7, 9, 11]
    # Sharp semitone indices (1=F#, 3=G#, 5=A#, 8=C#, 10=D#)
    sharp_semitones = [1, 3, 5, 8, 10]

    if semitone in natural_semitones:
        idx = natural_semitones.index(semitone)
        return natural_letters[idx]
    if semitone in sharp_semitones:
        # Find the natural note below this sharp
        base_semitone = semitone - 1
        idx = natural_semitones.index(base_semitone)
        base_letter = natural_letters[idx]
        # Shift +10 in alphabet
        base_ord = ord(base_letter)
        return chr(base_ord + 10)
    else:
        raise ValueError(f"Invalid semitone: {semitone}")


def _letter_to_signal_name(letter: str) -> str:
    """Convert a letter to a Factorio virtual signal name, e.g. 'F' → 'signal-F'."""
    return f"signal-{letter}"


def midi_to_pitch_index(midi_note: int, midi_base: int = MIDI_BASE) -> int | None:
    """Convert a MIDI note number to a 0-based pitch index (0–47).

    Returns None if the note is outside the 4-octave range starting at *midi_base*.
    MIDI 53 (F3) → 0, MIDI 100 (E7) → 47 (default piano range).
    """
    if midi_note < midi_base or midi_note >= midi_base + SPEAKER_COUNT:
        return None
    return midi_note - midi_base


def pitch_index_to_signal(pitch_index: int) -> dict[str, str]:
    """Convert a 0-based pitch index (0–47) to its (name, quality) pair.

    Returns a dict with keys ``"name"`` and ``"quality"`` suitable for
    passing to draftsman entity constructors.
    """
    if pitch_index < 0 or pitch_index >= SPEAKER_COUNT:
        raise ValueError(f"Pitch index {pitch_index} out of range [0, {SPEAKER_COUNT - 1}]")

    semitone = pitch_index % 12          # 0=F … 11=E
    octave_idx = pitch_index // 12       # 0=Oct3, 1=Oct4, 2=Oct5, 3=Oct6

    letter = _semitone_to_letter(semitone)
    quality = AUDIO_QUALITIES[octave_idx]

    return {"name": _letter_to_signal_name(letter), "quality": quality}


def iter_speaker_signals() -> Iterator[tuple[int, dict[str, str]]]:
    """Yield (pitch_index, signal_dict) for all 48 speakers."""
    for i in range(SPEAKER_COUNT):
        yield i, pitch_index_to_signal(i)


# ---------------------------------------------------------------------------
# Build a lookup from (signal_name, quality) → pitch_index for the decoder
# ---------------------------------------------------------------------------

def _signal_key(name: str, quality: str) -> str:
    return f"{name}|{quality}"


def build_reverse_map() -> dict[str, int]:
    """Return a dict mapping ``"signal-X|quality"`` → pitch_index."""
    result: dict[str, int] = {}
    for i in range(SPEAKER_COUNT):
        sig = pitch_index_to_signal(i)
        key = _signal_key(sig["name"], sig["quality"])
        result[key] = i
    return result
