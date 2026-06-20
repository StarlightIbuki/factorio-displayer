"""Unit tests for loudness_to_midi.py — loudness array → MIDI extraction."""

from __future__ import annotations

import io
import math

import mido
import pytest

from factorio_display.audio.loudness_to_midi import (
    loudness_to_midi,
    loudness_to_midi_file,
)


# ── helpers ────────────────────────────────────────────────────────────

def _empty_loudness(num_ticks: int) -> list[list[float]]:
    """Generate silent loudness array for *num_ticks* ticks (128 MIDI notes)."""
    return [[0.0] * 128 for _ in range(num_ticks)]


def _note_on_at(tick: int, midi_note: int, loudness: float = 0.8) -> list[list[float]]:
    """Generate a loudness array with a single note active at *tick*.

    Also adds sustain for a few ticks.
    """
    num_ticks = tick + 10
    result = [[0.0] * 128 for _ in range(num_ticks)]
    for t in range(tick, tick + 5):  # 5-tick note
        result[t][midi_note] = loudness
    return result


# ── loudness_to_midi ───────────────────────────────────────────────────

class TestLoudnessToMidi:
    def test_empty_input(self):
        mid = loudness_to_midi([])
        assert isinstance(mid, mido.MidiFile)
        # Should have at least one track
        assert len(mid.tracks) >= 1

    def test_all_silent_produces_no_notes(self):
        data = _empty_loudness(20)
        mid = loudness_to_midi(data)
        # Count note_on events
        note_ons = [
            msg for msg in mid.tracks[0]
            if msg.type == "note_on" and msg.velocity > 0
        ]
        assert len(note_ons) == 0

    def test_single_note_produces_one_note_on_off(self):
        data = _note_on_at(0, 60, loudness=0.8)  # C4
        mid = loudness_to_midi(data)

        note_ons = [
            msg for msg in mid.tracks[0]
            if msg.type == "note_on" and msg.velocity > 0
        ]
        note_offs = [
            msg for msg in mid.tracks[0]
            if msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0)
        ]
        assert len(note_ons) == 1
        assert len(note_offs) >= 1

    def test_single_note_correct_pitch(self):
        data = _note_on_at(0, 69, loudness=0.9)  # A4
        mid = loudness_to_midi(data)
        note_ons = [
            msg for msg in mid.tracks[0]
            if msg.type == "note_on" and msg.velocity > 0
        ]
        assert note_ons[0].note == 69

    def test_velocity_scales_with_loudness(self):
        """Loudness 0.5 → velocity ~64, loudness 1.0 → velocity ~127."""
        data_low = _note_on_at(0, 60, loudness=0.5)
        data_high = _note_on_at(0, 60, loudness=1.0)

        mid_low = loudness_to_midi(data_low)
        mid_high = loudness_to_midi(data_high)

        vel_low = [
            msg.velocity for msg in mid_low.tracks[0]
            if msg.type == "note_on" and msg.velocity > 0
        ][0]
        vel_high = [
            msg.velocity for msg in mid_high.tracks[0]
            if msg.type == "note_on" and msg.velocity > 0
        ][0]

        assert vel_low < vel_high
        # At loudness 0.5, velocity should be ~63-64
        assert 55 <= vel_low <= 75
        # At loudness 1.0, velocity should be ~127
        assert 120 <= vel_high <= 127

    def test_condense_merges_contiguous_notes(self):
        """A note sustained for 5 ticks should produce one note, not 5."""
        data = _note_on_at(0, 60, loudness=0.8)
        mid = loudness_to_midi(data, condense=True)

        note_ons = [
            msg for msg in mid.tracks[0]
            if msg.type == "note_on" and msg.velocity > 0
        ]
        assert len(note_ons) == 1

    def test_no_condense_produces_separate_notes(self):
        """Without condense, same-pitch notes separated by silence are separate."""
        # Note at tick 0-4, silence 5-9, note at 10-14
        data = _empty_loudness(15)
        for t in range(0, 5):
            data[t][60] = 0.8
        for t in range(10, 15):
            data[t][60] = 0.8

        mid = loudness_to_midi(data, condense=False)
        note_ons = [
            msg for msg in mid.tracks[0]
            if msg.type == "note_on" and msg.velocity > 0
        ]
        # Should produce 2 separate notes
        assert len(note_ons) == 2

    def test_activation_threshold_filters_low_loudness(self):
        """Loudness below threshold should not produce notes."""
        data = _note_on_at(0, 60, loudness=0.01)  # very quiet
        mid = loudness_to_midi(data, activation_threshold=0.05)
        note_ons = [
            msg for msg in mid.tracks[0]
            if msg.type == "note_on" and msg.velocity > 0
        ]
        assert len(note_ons) == 0

    def test_polyphonic_notes(self):
        """Multiple simultaneous notes should all appear in MIDI."""
        data = _empty_loudness(10)
        for t in range(0, 5):
            data[t][60] = 0.8  # C4
            data[t][64] = 0.7  # E4
            data[t][67] = 0.6  # G4

        mid = loudness_to_midi(data)
        note_ons = [
            msg for msg in mid.tracks[0]
            if msg.type == "note_on" and msg.velocity > 0
        ]
        assert len(note_ons) == 3

    def test_pitch_range_filtering(self):
        """Notes outside pitch_range should be excluded."""
        data = _empty_loudness(10)
        for t in range(0, 5):
            data[t][40] = 0.8  # E2 — below default
            data[t][60] = 0.8  # C4
            data[t][90] = 0.8  # F#6 — above default

        mid = loudness_to_midi(data, pitch_range=(50, 80))
        note_ons = [
            msg for msg in mid.tracks[0]
            if msg.type == "note_on" and msg.velocity > 0
        ]
        pitches = {msg.note for msg in note_ons}
        assert 60 in pitches
        assert 40 not in pitches
        assert 90 not in pitches

    def test_midi_file_is_valid(self):
        """Output should be a valid MIDI file readable by mido."""
        data = _note_on_at(0, 60, loudness=0.8)
        mid = loudness_to_midi(data)
        # Re-serialize and re-read to verify validity
        buf = io.BytesIO()
        mid.save(file=buf)
        buf.seek(0)
        re_read = mido.MidiFile(file=buf)
        assert len(re_read.tracks) >= 1

    def test_default_pitch_range_is_full(self):
        data = _note_on_at(0, 0, loudness=0.8)  # C-1 (MIDI 0)
        mid = loudness_to_midi(data)
        note_ons = [
            msg for msg in mid.tracks[0]
            if msg.type == "note_on" and msg.velocity > 0
        ]
        # MIDI 0 (C-1) should be allowed by default
        assert len(note_ons) == 1


# ── loudness_to_midi_file ──────────────────────────────────────────────

class TestLoudnessToMidiFile:
    def test_writes_to_path(self, tmp_path):
        """loudness_to_midi_file should write a .mid file."""
        out_path = tmp_path / "test_output.mid"
        data = _note_on_at(0, 60, loudness=0.8)
        loudness_to_midi_file(data, str(out_path))

        assert out_path.exists()
        # Verify it's a valid MIDI
        mid = mido.MidiFile(str(out_path))
        note_ons = [
            msg for msg in mid.tracks[0]
            if msg.type == "note_on" and msg.velocity > 0
        ]
        assert len(note_ons) >= 1

    def test_empty_creates_valid_file(self, tmp_path):
        out_path = tmp_path / "empty.mid"
        loudness_to_midi_file([], str(out_path))
        assert out_path.exists()
        mid = mido.MidiFile(str(out_path))
        assert len(mid.tracks) >= 1
