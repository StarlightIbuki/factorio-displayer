"""Unit tests for audio_analyzer.py — audio → loudness array conversion."""

from __future__ import annotations

import math

import numpy as np
import pytest

from factorio_display.audio.audio_analyzer import (
    TICK_DURATION_S,
    _freq_to_midi_note,
    _generate_note_freq_ranges,
    audio_to_loudness,
    fold_to_game_range,
    make_tone,
    make_chord,
)


# ── note frequency ranges ──────────────────────────────────────────────

class TestFreqToMidiNote:
    def test_a4_is_69(self):
        """A4 = 440 Hz → MIDI 69."""
        assert _freq_to_midi_note(440.0) == 69

    def test_c4_is_60(self):
        """C4 = 261.63 Hz → MIDI 60."""
        assert _freq_to_midi_note(261.63) == 60

    def test_low_boundary(self):
        """C0 ≈ 8.18 Hz → MIDI 0."""
        assert _freq_to_midi_note(8.18) == 0

    def test_high_boundary(self):
        """G9 ≈ 12543 Hz → MIDI 127."""
        assert _freq_to_midi_note(12543.0) == 127

    def test_out_of_range_below_returns_0(self):
        """Frequencies below C0 map to MIDI 0."""
        assert _freq_to_midi_note(1.0) == 0

    def test_out_of_range_above_returns_127(self):
        """Frequencies above G9 map to MIDI 127."""
        assert _freq_to_midi_note(20000.0) == 127


class TestNoteFreqRanges:
    def test_all_128_notes(self):
        ranges = _generate_note_freq_ranges()
        assert len(ranges) == 128
        for midi_note, (low, center, high) in enumerate(ranges):
            assert low <= center <= high
            # Center should be near the theoretical frequency
            expected_center = 440.0 * 2 ** ((midi_note - 69) / 12.0)
            assert abs(center - expected_center) / expected_center < 0.001

    def test_adjacent_ranges_meet(self):
        ranges = _generate_note_freq_ranges()
        for i in range(127):
            # high of note i should equal low of note i+1 (within float tolerance)
            assert abs(ranges[i][2] - ranges[i + 1][0]) < 1e-9


# ── tone generation helpers ─────────────────────────────────────────────

class TestMakeTone:
    def test_440hz_sine(self):
        samples = make_tone(440.0, duration_s=0.1, sample_rate=44100, amplitude=1.0)
        assert len(samples) == 4410
        assert samples.dtype == np.float64
        assert -1.0 <= samples.min() <= 1.0
        assert -1.0 <= samples.max() <= 1.0

    def test_silence(self):
        samples = make_tone(440.0, duration_s=0.1, sample_rate=44100, amplitude=0.0)
        assert np.all(samples == 0.0)

    def test_amplitude_scale(self):
        a = make_tone(440.0, duration_s=0.05, sample_rate=44100, amplitude=0.5)
        b = make_tone(440.0, duration_s=0.05, sample_rate=44100, amplitude=1.0)
        # Both should be valid sine waves; a should have roughly half the peak
        assert abs(a.max() - 0.5) < 0.01
        assert abs(b.max() - 1.0) < 0.01


class TestMakeChord:
    def test_two_notes(self):
        samples = make_chord(
            [440.0, 554.37],  # A4 + C#5
            duration_s=0.1,
            sample_rate=44100,
            amplitudes=[0.5, 0.5],
        )
        assert len(samples) == 4410
        assert samples.dtype == np.float64

    def test_amplitudes_default_to_equal(self):
        samples = make_chord([440.0, 660.0], duration_s=0.05, sample_rate=44100)
        # Should not error, default amplitudes should be used
        assert len(samples) == 2205


# ── STFT-based audio analysis ───────────────────────────────────────────

class TestAudioToLoudness:
    def test_empty_input(self):
        result = audio_to_loudness(
            np.array([], dtype=np.float64), sample_rate=44100,
        )
        assert result == []

    def test_short_input_less_than_window(self):
        """Input shorter than one FFT window should still produce output."""
        short = np.zeros(100, dtype=np.float64)
        result = audio_to_loudness(short, sample_rate=44100)
        # Should produce at least 1 tick
        assert len(result) >= 1

    def test_sine_wave_detects_correct_pitch(self):
        """A 440 Hz sine wave (A4, MIDI 69) should produce peak at MIDI 69."""
        samples = make_tone(440.0, duration_s=0.5, sample_rate=44100, amplitude=1.0)
        result = audio_to_loudness(samples, sample_rate=44100)

        assert len(result) > 0
        # Every tick should have 128 float entries
        for tick in result:
            assert len(tick) == 128
            assert all(isinstance(v, float) for v in tick)

        # Find the MIDI note with the highest average loudness
        avg_per_note = [0.0] * 128
        for tick in result:
            for note, loudness in enumerate(tick):
                avg_per_note[note] += loudness
        max_note = max(range(128), key=lambda n: avg_per_note[n])
        # Should be near MIDI 69 (A4). Allow ±1 semitone for FFT resolution.
        assert abs(max_note - 69) <= 1, f"Expected peak near 69, got {max_note}"

    def test_sine_wave_loudness_positive(self):
        """Non-silent audio should produce non-zero loudness at some pitch."""
        samples = make_tone(440.0, duration_s=0.3, sample_rate=44100, amplitude=0.8)
        result = audio_to_loudness(samples, sample_rate=44100)
        # At least one tick should have non-zero loudness
        max_loudness = max(max(t) for t in result)
        assert max_loudness > 0.0

    def test_silence_produces_zero_loudness(self):
        """Silent audio should produce near-zero loudness."""
        samples = np.zeros(4410, dtype=np.float64)  # 0.1s @ 44100
        result = audio_to_loudness(samples, sample_rate=44100)
        max_loudness = max(max(t) for t in result)
        assert max_loudness < 0.01  # Should be effectively zero

    def test_output_tick_count(self):
        """Number of output ticks should roughly match duration / TICK_DURATION_S."""
        duration_s = 1.0
        samples = make_tone(440.0, duration_s=duration_s, sample_rate=44100, amplitude=1.0)
        result = audio_to_loudness(samples, sample_rate=44100)
        expected_ticks = int(duration_s / TICK_DURATION_S)
        # Allow ±4 ticks for window edge effects (STFT with 4-tick window).
        assert abs(len(result) - expected_ticks) <= 4, \
            f"Expected ~{expected_ticks} ticks, got {len(result)}"

    def test_chord_detects_multiple_pitches(self):
        """A chord should have significant energy at multiple MIDI notes."""
        # C major: C4 (261.63), E4 (329.63), G4 (392.00)
        samples = make_chord(
            [261.63, 329.63, 392.00],
            duration_s=0.3,
            sample_rate=44100,
            amplitudes=[0.5, 0.5, 0.5],
        )
        result = audio_to_loudness(samples, sample_rate=44100)

        # Sum loudness across all ticks per note
        total_per_note = [0.0] * 128
        for tick in result:
            for note, loudness in enumerate(tick):
                total_per_note[note] += loudness

        # C4=60, E4=64, G4=67 should all have significant energy
        # Find the top 3 notes
        top_notes = sorted(range(128), key=lambda n: total_per_note[n], reverse=True)[:6]
        # At least 2 of {60, 64, 67} should be in the top 6
        found = sum(1 for n in [60, 64, 67] if n in top_notes)
        assert found >= 2, f"Expected chord notes in top 6, got top notes: {top_notes}"

    def test_stereo_is_downmixed_to_mono(self):
        """Stereo input should be handled gracefully (produce valid output)."""
        # 2-channel stereo: shape (samples, 2)
        stereo = np.zeros((4410, 2), dtype=np.float64)
        stereo[:, 0] = make_tone(440.0, duration_s=0.1, sample_rate=44100, amplitude=0.5)
        result = audio_to_loudness(stereo, sample_rate=44100)
        # Should produce valid output
        assert len(result) > 0
        max_loudness = max(max(t) for t in result)
        assert max_loudness > 0.0


# ── 4-octave folding ────────────────────────────────────────────────────

class TestFoldToGameRange:
    def test_f3_stays_at_0(self):
        """MIDI 53 (F3) → pitch_idx 0."""
        assert fold_to_game_range({53: 0.8})[0] == pytest.approx(0.8)

    def test_e7_stays_at_47(self):
        """MIDI 100 (E7) → pitch_idx 47."""
        assert fold_to_game_range({100: 0.5})[47] == pytest.approx(0.5)

    def test_a4_maps_to_correct_index(self):
        """MIDI 69 (A4) → pitch_idx 16 (since 69-53=16)."""
        result = fold_to_game_range({69: 0.9})
        assert result[16] == pytest.approx(0.9)
        # All other indices should be zero
        assert sum(1 for v in result if v > 0) == 1

    def test_out_of_range_low_folds_up(self):
        """MIDI 40 (E2) below F3 → folded up to E3 (MIDI 52) → pitch_idx -1... 
        Actually MIDI 52 is below F3(53), so it folds again to 64(E4) → pitch_idx 11."""
        result = fold_to_game_range({40: 0.7})
        # Should fold up by octaves until in range
        # 40+12=52 (still below 53), 52+12=64 (in range) → 64-53=11
        assert result[11] == pytest.approx(0.7)

    def test_out_of_range_high_folds_down(self):
        """MIDI 108 (C8) above E7(100) → folded down to C7(96) → pitch_idx 43."""
        result = fold_to_game_range({108: 0.6})
        # 108-12=96 (in range) → 96-53=43
        assert result[43] == pytest.approx(0.6)

    def test_multiple_notes_sum_at_same_pitch(self):
        """Two notes that fold to the same pitch should sum."""
        # MIDI 53 (F3) → pitch_idx 0
        # MIDI 65 (F4) → pitch_idx 12
        # Both are F, different octaves. After folding: 53→0, 65→12. They don't overlap.
        # Let's test with actual overlap: MIDI 53 (F3) and MIDI 113... 
        # 113 is out of range, folds down: 113-12=101(is 101>100? E7=100, so 101>100)
        # 101-12=89 → pitch_idx 36. So no overlap with 53.
        # Use MIDI 53 and MIDI 53+48=101: 101-48=53 wait, 101-12=89...
        # Actually use two notes 1 octave apart that still fold to the same:
        # 53(F3)→0, any note that folds to F: 41(F2)+12=53→0
        result = fold_to_game_range({53: 0.3, 41: 0.4})
        # 41+12=53→0, so both fold to 0 with sum 0.7
        assert result[0] == pytest.approx(0.7)

    def test_empty_input(self):
        result = fold_to_game_range({})
        assert len(result) == 48
        assert all(v == 0.0 for v in result)

    def test_all_zero(self):
        result = fold_to_game_range({60: 0.0, 72: 0.0})
        assert all(v == 0.0 for v in result)

    def test_clamped_to_1_0(self):
        """Loudness values above 1.0 should be clamped."""
        result = fold_to_game_range({69: 2.5})
        assert result[16] == pytest.approx(1.0)

    def test_returns_float_list(self):
        result = fold_to_game_range({60: 0.5})
        assert len(result) == 48
        assert all(isinstance(v, float) for v in result)
