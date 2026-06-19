"""Unit tests for pitch_mapping.py — signal encoding for the 48-speaker matrix."""

from __future__ import annotations

import pytest

from factorio_display.audio.pitch_mapping import (
    AUDIO_OCTAVES,
    AUDIO_QUALITIES,
    MIDI_BASE,
    SPEAKER_COUNT,
    _semitone_to_letter,
    build_reverse_map,
    iter_speaker_signals,
    midi_to_pitch_index,
    pitch_index_to_signal,
)


# ── constants ──────────────────────────────────────────────────────────

class TestConstants:
    def test_speaker_count(self):
        assert SPEAKER_COUNT == 48

    def test_midi_base(self):
        """F3 = MIDI 53."""
        assert MIDI_BASE == 53
        # F3 (MIDI 53) → pitch index 0
        assert midi_to_pitch_index(53) == 0
        # E7 (MIDI 100) → pitch index 47
        assert midi_to_pitch_index(100) == 47

    def test_audio_octaves(self):
        assert AUDIO_OCTAVES == [3, 4, 5, 6]

    def test_audio_qualities(self):
        assert AUDIO_QUALITIES == ["normal", "uncommon", "rare", "epic"]
        assert len(AUDIO_QUALITIES) == 4


# ── semitone → letter ──────────────────────────────────────────────────

class TestSemitoneToLetter:
    """Verify the 12-note chromatic mapping starting from F."""

    # (semitone, expected_letter, description)
    CASES = [
        (0, "F", "F natural"),
        (1, "P", "F# → F+10"),
        (2, "G", "G natural"),
        (3, "Q", "G# → G+10"),
        (4, "A", "A natural"),
        (5, "K", "A# → A+10"),
        (6, "B", "B natural"),
        (7, "C", "C natural"),
        (8, "M", "C# → C+10"),
        (9, "D", "D natural"),
        (10, "N", "D# → D+10"),
        (11, "E", "E natural"),
    ]

    @pytest.mark.parametrize("semitone,expected,_desc", CASES)
    def test_semitone_mapping(self, semitone, expected, _desc):
        assert _semitone_to_letter(semitone) == expected

    def test_invalid_semitone_raises(self):
        with pytest.raises(ValueError):
            _semitone_to_letter(-1)
        with pytest.raises(ValueError):
            _semitone_to_letter(12)
        with pytest.raises(ValueError):
            _semitone_to_letter(99)


# ── pitch_index → signal ───────────────────────────────────────────────

class TestPitchIndexToSignal:
    def test_first_pitch(self):
        """Pitch 0 = F3 = signal-F, normal."""
        sig = pitch_index_to_signal(0)
        assert sig == {"name": "signal-F", "quality": "normal"}

    def test_octave_boundaries(self):
        """Every 12 pitches advances one quality tier."""
        # Pitch 0–11  → octave 3 (normal)
        assert pitch_index_to_signal(0)["quality"] == "normal"
        assert pitch_index_to_signal(11)["quality"] == "normal"
        # Pitch 12–23 → octave 4 (uncommon)
        assert pitch_index_to_signal(12)["quality"] == "uncommon"
        assert pitch_index_to_signal(23)["quality"] == "uncommon"
        # Pitch 24–35 → octave 5 (rare)
        assert pitch_index_to_signal(24)["quality"] == "rare"
        # Pitch 36–47 → octave 6 (epic)
        assert pitch_index_to_signal(36)["quality"] == "epic"
        assert pitch_index_to_signal(47)["quality"] == "epic"

    def test_last_pitch(self):
        """Pitch 47 = E7 = signal-E, epic."""
        sig = pitch_index_to_signal(47)
        assert sig == {"name": "signal-E", "quality": "epic"}

    def test_out_of_range_raises(self):
        with pytest.raises(ValueError, match="out of range"):
            pitch_index_to_signal(-1)
        with pytest.raises(ValueError, match="out of range"):
            pitch_index_to_signal(48)

    def test_all_48_speakers_unique(self):
        """Every (name, quality) pair must be unique across 48 pitches."""
        seen: set[tuple[str, str]] = set()
        for i in range(SPEAKER_COUNT):
            sig = pitch_index_to_signal(i)
            pair = (sig["name"], sig["quality"])
            assert pair not in seen, f"Duplicate signal at pitch {i}: {pair}"
            seen.add(pair)
        assert len(seen) == 48

    def test_signal_names_are_valid(self):
        """All signal names should start with 'signal-'."""
        for i in range(SPEAKER_COUNT):
            sig = pitch_index_to_signal(i)
            assert sig["name"].startswith("signal-"), f"Bad name at {i}: {sig['name']}"
            # Letter should be uppercase A–Z
            letter = sig["name"].split("-")[1]
            assert len(letter) == 1
            assert "A" <= letter <= "Z"


# ── midi_to_pitch_index ────────────────────────────────────────────────

class TestMidiToPitchIndex:
    def test_below_range(self):
        assert midi_to_pitch_index(0) is None
        assert midi_to_pitch_index(52) is None

    def test_above_range(self):
        assert midi_to_pitch_index(101) is None
        assert midi_to_pitch_index(127) is None

    def test_in_range(self):
        assert midi_to_pitch_index(53) == 0   # F3
        assert midi_to_pitch_index(65) == 12  # F4
        assert midi_to_pitch_index(77) == 24  # F5
        assert midi_to_pitch_index(89) == 36  # F6
        assert midi_to_pitch_index(100) == 47  # E7


# ── iter_speaker_signals ───────────────────────────────────────────────

class TestIterSpeakerSignals:
    def test_yields_48_items(self):
        items = list(iter_speaker_signals())
        assert len(items) == 48

    def test_indices_are_sequential(self):
        indices = [idx for idx, _ in iter_speaker_signals()]
        assert indices == list(range(48))

    def test_all_signals_have_both_keys(self):
        for _, sig in iter_speaker_signals():
            assert "name" in sig
            assert "quality" in sig


# ── reverse map ────────────────────────────────────────────────────────

class TestReverseMap:
    def test_roundtrip(self):
        rev = build_reverse_map()
        assert len(rev) == 48

        for i in range(SPEAKER_COUNT):
            sig = pitch_index_to_signal(i)
            key = f"{sig['name']}|{sig['quality']}"
            assert rev[key] == i

    def test_idempotent(self):
        a = build_reverse_map()
        b = build_reverse_map()
        assert a == b
