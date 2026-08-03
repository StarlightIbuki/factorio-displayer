"""Tests for the audio drum detector (spectral-flux kick/snare/hat detection)."""
from __future__ import annotations

import numpy as np
import pytest

from factorio_display.audio.drum_detector import (
    DEFAULT_SOUNDS,
    detect_drum_events,
    detect_drum_rail,
    events_to_drum_rail,
)
from factorio_display.audio.pitch_mapping import (
    DRUM_KIT_NOTES,
    DRUM_NOTE_TO_PITCH,
    SPEAKER_COUNT,
)

SR = 44100


def _drum_signal(dur_s: float = 2.0) -> tuple[np.ndarray, list[int]]:
    """Build a synthetic signal: kick (60 Hz) + snare (1 kHz) + hat (10 kHz)
    hits at ticks 30, 60, 90 (0.5 s, 1.0 s, 1.5 s), each a decaying tone in
    its own frequency band so the classifier can separate them."""
    n = int(dur_s * SR)
    t = np.arange(n) / SR
    sig = np.zeros(n, dtype=np.float64)
    expected = [30, 60, 90]

    def hit(onset_s: float, freq: float) -> None:
        i0 = int(onset_s * SR)
        i1 = min(n, i0 + int(0.08 * SR))
        idx = np.arange(i0, i1)
        sig[idx] += np.sin(2 * np.pi * freq * (idx / SR)) * \
            np.exp(-np.arange(len(idx)) / (0.02 * SR)) * 1.0

    hit(0.5, 60.0)    # kick  -> 40-180 Hz band
    hit(1.0, 1000.0)  # snare -> 180-5000 Hz band
    hit(1.5, 10000.0) # hat   -> 7000-16000 Hz band
    return sig, expected


class TestDetectDrumEvents:
    def test_detects_known_hits(self):
        sig, expected = _drum_signal()
        events = detect_drum_events(sig, SR, min_z=1.0)
        ticks = {t for t, _, _ in events}
        # each synthetic hit must be caught at (within ~2 ticks of) its tick
        for want in expected:
            assert any(abs(t - want) <= 2 for t in ticks), (want, events)

    def test_pure_tone_no_hits(self):
        # A sustained 440 Hz sine has flat spectral flux → no onsets.
        t = np.arange(int(1.0 * SR)) / SR
        sig = 0.8 * np.sin(2 * np.pi * 440.0 * t)
        events = detect_drum_events(sig, SR)
        assert events == []

    def test_silence_no_hits(self):
        events = detect_drum_events(np.zeros(int(1.0 * SR)), SR)
        assert events == []


class TestEventsToDrumRail:
    def test_shape_and_slots(self):
        events = [(0, "kick", 80.0), (10, "snare", 70.0), (20, "hat", 60.0)]
        rail = events_to_drum_rail(events, num_ticks=30)
        assert len(rail) == 30
        assert all(len(tick) == SPEAKER_COUNT for tick in rail)
        kick = DRUM_NOTE_TO_PITCH[DEFAULT_SOUNDS["kick"]]
        snare = DRUM_NOTE_TO_PITCH[DEFAULT_SOUNDS["snare"]]
        hat = DRUM_NOTE_TO_PITCH[DEFAULT_SOUNDS["hat"]]
        assert rail[0][kick] == 80.0
        assert rail[10][snare] == 70.0
        assert rail[20][hat] == 60.0

    def test_hit_sustains_multiple_ticks(self):
        """A hit must last more than one tick (the drum sample is otherwise a
        too-short blip in-game)."""
        from factorio_display.audio.drum_detector import (
            DRUM_DURATION_TICKS,
            DRUM_HOLD_TICKS,
        )

        events = [(5, "kick", 100.0)]
        rail = events_to_drum_rail(events, num_ticks=20)
        kick = DRUM_NOTE_TO_PITCH[DEFAULT_SOUNDS["kick"]]
        # plateau at full loudness for the hold, then a decay tail
        for i in range(DRUM_HOLD_TICKS):
            assert rail[5 + i][kick] == 100.0
        # decays to zero by the end of the duration
        dur = DRUM_DURATION_TICKS["kick"]
        assert rail[5 + dur - 1][kick] < 100.0
        assert rail[5 + dur][kick] == 0.0

    def test_out_of_range_ignored(self):
        rail = events_to_drum_rail([(9999, "kick", 80.0)], num_ticks=10)
        assert all(v == 0 for v in rail[0])

    def test_sound_names_are_valid(self):
        for band, name in DEFAULT_SOUNDS.items():
            assert name in DRUM_KIT_NOTES
            assert name in DRUM_NOTE_TO_PITCH


class TestDetectDrumRail:
    def test_min_hits_gate(self):
        # Sparse signal → fewer than min_hits → None.
        t = np.arange(int(1.0 * SR)) / SR
        sig = np.zeros_like(t)
        i0 = int(0.5 * SR)
        sig[i0:i0 + 1000] = 1.0  # single burst
        rail = detect_drum_rail(sig, SR, min_hits=50)
        assert rail is None
