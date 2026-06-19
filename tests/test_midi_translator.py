"""Unit tests for midi_translator.py — MIDI → tick_data conversion."""

from __future__ import annotations

import sys
import io

import mido

from factorio_display.audio.midi_translator import (
    midi_to_multi_rail_tick_data,
    midi_to_tick_data,
)
from factorio_display.audio.pitch_mapping import (
    SPEAKER_COUNT,
    midi_to_pitch_index,
)


# ── helpers ────────────────────────────────────────────────────────────

def _make_midi(
    notes: list[tuple[int, int, int, int]],
    ticks_per_beat: int = 480,
    tempo: int = 500_000,  # 120 BPM
) -> mido.MidiFile:
    """Build a single-track MIDI from ``(note, velocity, start_tick, duration_ticks)`` tuples.

    *start_tick* and *duration_ticks* are in MIDI ticks.
    """
    mid = mido.MidiFile(ticks_per_beat=ticks_per_beat)
    track = mido.MidiTrack()
    mid.tracks.append(track)

    # Sort by start_tick, then by note for determinism
    events: list[tuple[int, str, mido.Message]] = []
    for note, velocity, start, duration in notes:
        events.append((
            start, "on",
            mido.Message("note_on", note=note, velocity=velocity, time=0),
        ))
        events.append((start + duration, "off", mido.Message("note_off", note=note, velocity=0, time=0)))
    events.sort(key=lambda e: (e[0], 0 if e[1] == "on" else 1))

    # Insert tempo at tick 0
    track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))

    prev_tick = 0
    for abs_tick, _, msg in events:
        msg.time = abs_tick - prev_tick
        track.append(msg)
        prev_tick = abs_tick

    return mid


def _midi_tick_to_game_tick(
    midi_tick: int, ticks_per_beat: int,
    tempo: int, game_ticks_per_beat: int,
) -> float:
    """Convert a MIDI absolute tick to a game tick (float)."""
    seconds = mido.tick2second(midi_tick, ticks_per_beat, tempo)
    # Re-derive game ticks: seconds * game_ticks_per_beat / seconds_per_beat
    seconds_per_beat = tempo / 1_000_000
    return seconds / seconds_per_beat * game_ticks_per_beat


# ── basic conversion ───────────────────────────────────────────────────

class TestMidiToTickDataBasic:
    def test_empty_midi(self):
        mid = mido.MidiFile()
        result = midi_to_tick_data(mid)
        assert result == []

    def test_single_note(self):
        """One note F3 (MIDI 53) at velocity 64 → pitch_idx 0, loudness ~50."""
        mid = _make_midi([(53, 64, 0, 480)])  # quarter note at 120bpm
        result = midi_to_tick_data(mid, ticks_per_beat=30)
        assert len(result) > 0
        # Check that pitch 0 has non-zero loudness during the note
        pitch_idx = midi_to_pitch_index(53)
        assert pitch_idx == 0
        # All values should be floats
        for tick in result:
            assert len(tick) == SPEAKER_COUNT
            for v in tick:
                assert isinstance(v, float)

    def test_velocity_to_loudness_range(self):
        """Velocity 127 → loudness ~100.0, velocity 1 → loudness ~0.79."""
        mid = _make_midi([(60, 127, 0, 480)])
        result = midi_to_tick_data(mid, ticks_per_beat=30, velocity_scale=1.0)
        # Find max loudness
        max_loud = max(max(t) for t in result)
        assert 99.0 <= max_loud <= 100.1

    def test_note_outside_range_is_globally_shifted(self):
        """MIDI 101 (F7) above E7=100 — optimal global shift -12 brings it to
        89 (F6), in range without per-note folding."""
        mid = _make_midi([(101, 100, 0, 480)])
        buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = buf
        try:
            result = midi_to_tick_data(mid, ticks_per_beat=30)
            log_output = buf.getvalue()
        finally:
            sys.stderr = old_stderr

        # Global shift should have been logged, NOT per-note folding
        assert "Global octave shift" in log_output, (
            f"Expected global shift log, got: {log_output!r}"
        )
        assert "folded" not in log_output.lower(), (
            f"Should NOT have per-note fold with global shift, got: {log_output!r}"
        )
        # After -12 shift: 101→89 (F6) → pitch index 36
        assert len(result) > 0
        has_active = any(t[36] > 0 for t in result)
        assert has_active, "Globally-shifted note should produce activity at pitch 36"

    def test_note_below_range_is_globally_shifted(self):
        """MIDI 52 (E3) below F3=53 — optimal global shift +12 brings it to
        64 (E4), in range without per-note folding."""
        mid = _make_midi([(52, 100, 0, 480)])
        buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = buf
        try:
            result = midi_to_tick_data(mid, ticks_per_beat=30)
            log_output = buf.getvalue()
        finally:
            sys.stderr = old_stderr

        assert "Global octave shift" in log_output, (
            f"Expected global shift log, got: {log_output!r}"
        )
        assert "folded" not in log_output.lower(), (
            f"Should NOT have per-note fold with global shift, got: {log_output!r}"
        )
        # After +12 shift: 52→64 (E4) → pitch index 11
        has_active = any(t[11] > 0 for t in result)
        assert has_active, "Folded-up note should produce activity at pitch 11"


# ── polyphony / overlap ────────────────────────────────────────────────

class TestMidiToTickDataPolyphony:
    def test_two_simultaneous_notes_sum(self):
        """Two notes at same time: loudness values add as floats."""
        # Two notes: F3 (53) and A3 (57), both velocity 64
        mid = _make_midi([
            (53, 64, 0, 480),
            (57, 64, 0, 480),
        ])
        result = midi_to_tick_data(mid, ticks_per_beat=30, velocity_scale=1.0)
        # Each velocity 64 → ~50 loudness, summed → ~100
        pitch_f = midi_to_pitch_index(53)
        pitch_a = midi_to_pitch_index(57)
        assert pitch_f is not None and pitch_a is not None
        for tick in result:
            # Both should have equal loudness and not exceed 100
            if tick[pitch_f] > 0:
                assert tick[pitch_a] > 0
                # Each should be ~50 (64/127*100 ≈ 50.4)
                assert 48 <= tick[pitch_f] <= 52, f"Expected ~50, got {tick[pitch_f]}"
                assert 48 <= tick[pitch_a] <= 52, f"Expected ~50, got {tick[pitch_a]}"

    def test_overlapping_notes_at_different_times(self):
        """One note starts at tick 0, second starts halfway through."""
        mid = _make_midi([
            (60, 100, 0, 480),       # C4, full duration
            (64, 100, 240, 240),     # E4, starts halfway
        ])
        result = midi_to_tick_data(mid, ticks_per_beat=30)
        pitch_c = midi_to_pitch_index(60)
        pitch_e = midi_to_pitch_index(64)
        assert pitch_c is not None and pitch_e is not None

        # Find where E4 starts
        found_overlap = False
        for tick in result:
            if tick[pitch_c] > 0 and tick[pitch_e] > 0:
                found_overlap = True
                break
        assert found_overlap, "Notes should overlap for some ticks"

    def test_float_precision_accumulation(self):
        """Multiple simultaneous notes should sum precisely as floats."""
        notes = [(53 + i, 127, 0, 480) for i in range(0, 12, 2)]  # 6 notes
        mid = _make_midi(notes)
        result = midi_to_tick_data(mid, ticks_per_beat=30, velocity_scale=1.0)

        # Each note: 127/127*100 = 100.0, 6 notes → 600.0 (clipped to 100 later by caller)
        mid_tick = len(result) // 2
        total = sum(result[mid_tick])
        # 6 notes × 100 ≈ 600 (float sum, no clipping here)
        assert total > 500, f"Expected ~600 float sum, got {total}"


# ── melody detection ───────────────────────────────────────────────────

class TestMidiToTickDataMelody:
    def test_melody_boost(self):
        """Track with higher average pitch should get boosted velocity."""
        mid = _make_midi([
            (72, 100, 0, 480),   # C5 — higher → melody
            (60, 100, 0, 480),   # C4 — lower → not melody
        ])
        result_no_boost = midi_to_tick_data(mid, ticks_per_beat=30, boost_melody=1.0)
        result_boosted = midi_to_tick_data(mid, ticks_per_beat=30, boost_melody=1.5)

        pitch_72 = midi_to_pitch_index(72)
        pitch_60 = midi_to_pitch_index(60)
        assert pitch_72 is not None and pitch_60 is not None

        # With boost, C5 should be louder than without
        max_no_boost = max(t[pitch_72] for t in result_no_boost)
        max_boosted = max(t[pitch_72] for t in result_boosted)
        assert max_boosted > max_no_boost, (
            f"Melody boost should increase velocity: {max_no_boost} → {max_boosted}"
        )

    def test_no_boost_when_melody_disabled(self):
        """When boost_melody=1.0, no boost should apply."""
        mid = _make_midi([
            (80, 100, 0, 480),
            (60, 100, 0, 480),
        ])
        result = midi_to_tick_data(mid, ticks_per_beat=30, boost_melody=1.0)
        pitch_80 = midi_to_pitch_index(80)
        pitch_60 = midi_to_pitch_index(60)
        assert pitch_80 is not None and pitch_60 is not None
        # Both should have same loudness (both velocity 100)
        max_80 = max(t[pitch_80] for t in result)
        max_60 = max(t[pitch_60] for t in result)
        # Allow small float difference
        assert abs(max_80 - max_60) < 0.5, (
            f"Without boost, loudness should be equal: {max_80} vs {max_60}"
        )


# ── velocity scaling ───────────────────────────────────────────────────

class TestMidiToTickDataVelocityScale:
    def test_scale_half(self):
        """velocity_scale=0.5 should halve all loudness values."""
        mid = _make_midi([(60, 100, 0, 480)])
        result_full = midi_to_tick_data(mid, ticks_per_beat=30, velocity_scale=1.0)
        result_half = midi_to_tick_data(mid, ticks_per_beat=30, velocity_scale=0.5)

        max_full = max(max(t) for t in result_full)
        max_half = max(max(t) for t in result_half)
        assert abs(max_half - max_full / 2) < 1.0, (
            f"Half scale: expected ~{max_full/2}, got {max_half}"
        )

    def test_scale_zero_produces_silence(self):
        """velocity_scale=0 should produce all zeros."""
        mid = _make_midi([(60, 127, 0, 480)])
        result = midi_to_tick_data(mid, ticks_per_beat=30, velocity_scale=0.0)
        for tick in result:
            assert all(v == 0.0 for v in tick)


# ── tempo handling ─────────────────────────────────────────────────────

class TestMidiToTickDataTempo:
    def test_default_tempo(self):
        """Without set_tempo, defaults to 120 BPM (500000 µs/beat)."""
        mid = mido.MidiFile(ticks_per_beat=480)
        track = mido.MidiTrack()
        mid.tracks.append(track)
        track.append(mido.Message("note_on", note=60, velocity=100, time=0))
        track.append(mido.Message("note_off", note=60, velocity=0, time=480))

        result = midi_to_tick_data(mid, ticks_per_beat=30)
        # 480 MIDI ticks at 120 BPM = 1 beat = 30 game ticks
        assert len(result) >= 28, f"Expected ~30 ticks, got {len(result)}"

    def test_double_tempo(self):
        """At 240 BPM, a quarter note is 0.25s → half the game ticks of 120 BPM.

        Game ticks are time-based (seconds × 2 × ticks_per_beat), so faster
        tempo = fewer game ticks for the same number of MIDI ticks.
        This ensures real-time Factorio playback regardless of tempo.
        """
        mid_120 = _make_midi([(60, 100, 0, 480)], tempo=500_000)  # 120 BPM
        mid_240 = _make_midi([(60, 100, 0, 480)], tempo=250_000)  # 240 BPM
        result_120 = midi_to_tick_data(mid_120, ticks_per_beat=30)
        result_240 = midi_to_tick_data(mid_240, ticks_per_beat=30)
        # 120 BPM: 0.5s → 30 ticks; 240 BPM: 0.25s → 15 ticks
        assert len(result_240) == len(result_120) // 2, (
            f"240 BPM (0.25s) should be half the ticks of 120 BPM (0.5s): "
            f"120BPM={len(result_120)}, 240BPM={len(result_240)}"
        )


# ── ticks_per_beat ─────────────────────────────────────────────────────

class TestMidiToTickDataTicksPerBeat:
    def test_higher_ticks_per_beat(self):
        """ticks_per_beat=60 doubles game tick count vs default 30."""
        mid = _make_midi([(60, 100, 0, 480)])
        result_30 = midi_to_tick_data(mid, ticks_per_beat=30)
        result_60 = midi_to_tick_data(mid, ticks_per_beat=60)
        # Result should be roughly 2x length
        assert abs(len(result_60) - 2 * len(result_30)) <= 2, (
            f"60 tpb: {len(result_60)} ticks, 30 tpb: {len(result_30)} ticks"
        )


# ── output shape ───────────────────────────────────────────────────────

class TestMidiToTickDataShape:
    def test_all_ticks_have_48_elements(self):
        """Every tick must be a list of exactly 48 floats."""
        mid = _make_midi([
            (53, 64, 0, 480),
            (77, 80, 240, 240),
        ])
        result = midi_to_tick_data(mid, ticks_per_beat=30)
        assert len(result) > 0
        for i, tick in enumerate(result):
            assert len(tick) == SPEAKER_COUNT, f"Tick {i} has {len(tick)} elements"
            for v in tick:
                assert isinstance(v, float), f"Tick {i} has non-float: {type(v)}"

    def test_silence_at_start_and_end(self):
        """Ticks before first note and after last note should be silent."""
        mid = _make_midi([(60, 100, 480, 480)])  # starts at MIDI tick 480
        result = midi_to_tick_data(mid, ticks_per_beat=30)
        # First few game ticks should be all zeros
        silence_count = 0
        for tick in result:
            if all(v == 0.0 for v in tick):
                silence_count += 1
            else:
                break
        assert silence_count > 0, "Should have silence before first note"


# ── processed MIDI output ──────────────────────────────────────────────

class TestMidiToTickDataProcessedMidi:
    def test_emit_processed_midi(self, tmp_path):
        """When processed_midi_path is given, a .mid file should be written."""
        mid = _make_midi([(60, 100, 0, 480)])
        out_path = tmp_path / "processed.mid"
        result = midi_to_tick_data(mid, ticks_per_beat=30, processed_midi_path=str(out_path))
        assert len(result) > 0
        assert out_path.exists()
        # Verify it's a valid MIDI file
        reloaded = mido.MidiFile(str(out_path))
        assert len(reloaded.tracks) > 0


# ── ADSR envelope ──────────────────────────────────────────────────────

class TestAdsrEnvelope:
    def test_attack_ramp(self):
        """With attack_ticks=3, the first few ticks should ramp up from ~70% to 100%."""
        mid = _make_midi([(60, 100, 0, 480)])  # ~30 game ticks
        result = midi_to_tick_data(mid, ticks_per_beat=30,
                                   attack_ticks=3)  # 3-tick attack
        pitch = midi_to_pitch_index(60)
        assert pitch is not None

        # Find first active tick
        active = [(i, t[pitch]) for i, t in enumerate(result) if t[pitch] > 0]
        assert len(active) >= 5, f"Expected sustained note, got {len(active)} active ticks"

        # First tick should be less than sustain (attack starts at ~70%)
        first_loud = active[0][1]
        peak_loud = max(t[pitch] for t in result)
        assert first_loud < peak_loud, (
            f"Attack ramp: first={first_loud:.1f} should be < peak={peak_loud:.1f}"
        )
        # First tick should be ~70% of peak
        assert 0.60 <= first_loud / peak_loud <= 0.85, (
            f"Attack start should be ~70% of peak, got {first_loud / peak_loud:.2f}"
        )

    def test_release_ramp(self):
        """With release_ticks=3, the last few ticks should ramp down."""
        mid = _make_midi([(60, 100, 0, 480)])
        result = midi_to_tick_data(mid, ticks_per_beat=30,
                                   release_ticks=3)
        pitch = midi_to_pitch_index(60)
        assert pitch is not None

        active = [(i, t[pitch]) for i, t in enumerate(result) if t[pitch] > 0]
        assert len(active) >= 5

        # Last tick should be less than sustain
        last_loud = active[-1][1]
        peak_loud = max(t[pitch] for t in result)
        assert last_loud < peak_loud, (
            f"Release ramp: last={last_loud:.1f} should be < peak={peak_loud:.1f}"
        )

    def test_sustain_level(self):
        """With sustain_level=0.5, after attack/decay, loudness should drop to ~50%."""
        mid = _make_midi([(60, 100, 0, 960)])  # longer note for clear sustain
        result = midi_to_tick_data(mid, ticks_per_beat=30,
                                   attack_ticks=2, decay_ticks=2,
                                   sustain_level=0.5, release_ticks=3)
        pitch = midi_to_pitch_index(60)
        assert pitch is not None

        active = [(i, t[pitch]) for i, t in enumerate(result) if t[pitch] > 0]
        # Middle section (after attack+decay, before release) should be ~50% of peak
        mid_start = len(active) // 3
        mid_end = 2 * len(active) // 3
        mid_loudness = [t[pitch] for t in result[mid_start:mid_end]]
        if mid_loudness:
            avg_mid = sum(mid_loudness) / len(mid_loudness)
            peak = max(t[pitch] for t in result)
            assert 0.40 <= avg_mid / peak <= 0.65, (
                f"Sustain should be ~50% of peak: avg={avg_mid:.1f}, peak={peak:.1f}"
            )

    def test_short_note_no_crash(self):
        """Very short notes should not crash with ADSR (fewer ticks than attack+release)."""
        mid = _make_midi([(60, 100, 0, 120)])  # very short note
        result = midi_to_tick_data(mid, ticks_per_beat=30,
                                   attack_ticks=5, release_ticks=5)
        # Should not crash and should produce output
        assert len(result) > 0

    def test_no_adsr_is_flat(self):
        """Without ADSR params, all ticks of a note should have same loudness."""
        mid = _make_midi([(60, 100, 0, 480)])
        result = midi_to_tick_data(mid, ticks_per_beat=30)
        pitch = midi_to_pitch_index(60)
        assert pitch is not None
        active_loudness = [t[pitch] for t in result if t[pitch] > 0]
        # All should be identical (flat envelope)
        assert len(set(active_loudness)) == 1, (
            f"Without ADSR, all active ticks should have same loudness, got {set(active_loudness)}"
        )

    def test_curve_1_is_linear(self):
        """With curve=1.0, ADSR should produce same values as old linear behavior."""
        mid = _make_midi([(60, 100, 0, 480)])
        result_old = midi_to_tick_data(
            mid, ticks_per_beat=30,
            attack_ticks=3, release_ticks=3,
        )
        result_curve = midi_to_tick_data(
            mid, ticks_per_beat=30,
            attack_ticks=3, release_ticks=3,
            attack_curve=1.0, decay_curve=1.0, release_curve=1.0,
        )
        pitch = midi_to_pitch_index(60)
        assert pitch is not None
        old_loudness = [t[pitch] for t in result_old]
        curve_loudness = [t[pitch] for t in result_curve]
        assert len(old_loudness) == len(curve_loudness)
        for i, (old, cur) in enumerate(zip(old_loudness, curve_loudness)):
            assert abs(old - cur) < 0.01, (
                f"Curve=1.0 should match linear at tick {i}: old={old:.3f} curve={cur:.3f}"
            )

    def test_attack_curve_concave(self):
        """attack_curve=0.5 (concave): fast initial rise, slow approach to peak.

        Concave (exp<1) means progress**exp rises faster early (after t=0),
        so tick 1 is HIGHER than linear's tick 1,
        and the peak is higher (reaches closer to 1.0 by end of attack).
        Tick 0 is unchanged (progress=0 → 0**anything = 0).
        """
        mid = _make_midi([(60, 100, 0, 960)])  # long note for clear attack
        result_lin = midi_to_tick_data(
            mid, ticks_per_beat=30,
            attack_ticks=5, release_ticks=0,
            attack_curve=1.0, release_curve=1.0,
        )
        result_concave = midi_to_tick_data(
            mid, ticks_per_beat=30,
            attack_ticks=5, release_ticks=0,
            attack_curve=0.5, release_curve=1.0,
        )
        pitch = midi_to_pitch_index(60)
        assert pitch is not None

        lin_vals = [t[pitch] for t in result_lin[:5]]
        cav_vals = [t[pitch] for t in result_concave[:5]]

        # Tick 0: same (progress=0)
        assert abs(lin_vals[0] - cav_vals[0]) < 0.01
        # Tick 1+: concave rises faster
        assert cav_vals[1] > lin_vals[1], (
            f"Concave tick 1 should be higher: "
            f"linear={lin_vals[1]:.3f}, concave={cav_vals[1]:.3f}"
        )
        # Concave peak should be higher (reaches closer to 1.0)
        assert max(cav_vals) > max(lin_vals), (
            f"Concave peak should be higher: "
            f"linear_peak={max(lin_vals):.3f}, concave_peak={max(cav_vals):.3f}"
        )

    def test_attack_curve_convex(self):
        """attack_curve=2.0 (convex): slow initial rise, fast finish.

        Convex (exp>1) means progress**exp rises slower early, so
        tick 1 is LOWER than linear's tick 1, and the peak is lower.
        Tick 0 is unchanged (progress=0 → 0**anything = 0).
        """
        mid = _make_midi([(60, 100, 0, 960)])
        result_lin = midi_to_tick_data(
            mid, ticks_per_beat=30,
            attack_ticks=5, release_ticks=0,
            attack_curve=1.0, release_curve=1.0,
        )
        result_convex = midi_to_tick_data(
            mid, ticks_per_beat=30,
            attack_ticks=5, release_ticks=0,
            attack_curve=2.0, release_curve=1.0,
        )
        pitch = midi_to_pitch_index(60)
        assert pitch is not None

        lin_vals = [t[pitch] for t in result_lin[:5]]
        cvx_vals = [t[pitch] for t in result_convex[:5]]

        # Tick 0: same (progress=0)
        assert abs(lin_vals[0] - cvx_vals[0]) < 0.01
        # Tick 1+: convex rises slower
        assert cvx_vals[1] < lin_vals[1], (
            f"Convex tick 1 should be lower: "
            f"linear={lin_vals[1]:.3f}, convex={cvx_vals[1]:.3f}"
        )
        # Convex peak should be lower (doesn't reach 1.0 by end of attack)
        assert max(cvx_vals) < max(lin_vals), (
            f"Convex peak should be lower: "
            f"linear_peak={max(lin_vals):.3f}, convex_peak={max(cvx_vals):.3f}"
        )

    def test_release_curve_concave(self):
        """release_curve=0.5 (concave): fast initial drop, stays lower throughout.

        Concave (exp<1): progress**0.5 > progress (for 0<progress<1),
        so (1 - progress**0.5) < (1 - progress) — values drop FASTER
        and are LOWER throughout the release phase.
        """
        mid = _make_midi([(60, 100, 0, 480)])
        result_lin = midi_to_tick_data(
            mid, ticks_per_beat=30,
            attack_ticks=0, release_ticks=4,
            release_curve=1.0,
        )
        result_concave = midi_to_tick_data(
            mid, ticks_per_beat=30,
            attack_ticks=0, release_ticks=4,
            release_curve=0.5,
        )
        pitch = midi_to_pitch_index(60)
        assert pitch is not None

        lin_vals = [t[pitch] for t in result_lin]
        cav_vals = [t[pitch] for t in result_concave]
        assert len(lin_vals) == len(cav_vals)
        # Check last 4 ticks (release phase): concave should be lower throughout
        for i in range(len(lin_vals) - 4, len(lin_vals)):
            assert cav_vals[i] < lin_vals[i] + 0.01, (
                f"Concave release tick {i} should be < linear: "
                f"linear={lin_vals[i]:.3f}, concave={cav_vals[i]:.3f}"
            )

    def test_release_curve_convex(self):
        """release_curve=2.0 (convex): holds volume longer, all release ticks higher.

        Convex (exp>1): progress**2.0 < progress (for 0<progress<1),
        so (1 - progress**2.0) > (1 - progress) — values stay HIGHER
        throughout the release phase.  This models natural damping where
        the sound fades slowly at first, then drops quickly near the end
        (which would be visible with more release ticks).
        """
        mid = _make_midi([(60, 100, 0, 480)])
        result_lin = midi_to_tick_data(
            mid, ticks_per_beat=30,
            attack_ticks=0, release_ticks=4,
            release_curve=1.0,
        )
        result_convex = midi_to_tick_data(
            mid, ticks_per_beat=30,
            attack_ticks=0, release_ticks=4,
            release_curve=2.0,
        )
        pitch = midi_to_pitch_index(60)
        assert pitch is not None

        lin_vals = [t[pitch] for t in result_lin]
        cvx_vals = [t[pitch] for t in result_convex]
        assert len(lin_vals) == len(cvx_vals)
        # Convex: all release ticks should be higher (holds volume)
        release_start = len(lin_vals) - 4
        for i in range(release_start, len(lin_vals)):
            assert cvx_vals[i] > lin_vals[i] - 0.01, (
                f"Convex release tick {i} should be >= linear: "
                f"linear={lin_vals[i]:.3f}, convex={cvx_vals[i]:.3f}"
            )


# ── multi-rail tests ──────────────────────────────────────────────────

class TestMultiRailMidi:
    """Tests for ``midi_to_multi_rail_tick_data``."""

    @staticmethod
    def _make_midi_with_channel(
        notes: list[tuple[int, int, int, int, int]],  # (note, vel, start, dur, channel)
        ticks_per_beat: int = 480,
        tempo: int = 500_000,
    ) -> mido.MidiFile:
        """Build a single-track MIDI with channel info."""
        mid = mido.MidiFile(ticks_per_beat=ticks_per_beat)
        track = mido.MidiTrack()
        mid.tracks.append(track)
        events = []
        for note, velocity, start, duration, channel in notes:
            events.append((
                start, "on",
                mido.Message("note_on", note=note, velocity=velocity, time=0, channel=channel),
            ))
            events.append((
                start + duration, "off",
                mido.Message("note_off", note=note, velocity=0, time=0, channel=channel),
            ))
        events.sort(key=lambda e: (e[0], 0 if e[1] == "on" else 1))
        track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))
        prev_tick = 0
        for abs_tick, _, msg in events:
            msg.time = abs_tick - prev_tick
            track.append(msg)
            prev_tick = abs_tick
        return mid

    def test_drum_channel_separate_rail(self):
        """Channel 9 notes go to a drum rail, channel 0 to piano."""
        mid = self._make_midi_with_channel([
            (60, 100, 0, 480, 0),    # piano
            (36, 80, 0, 240, 9),     # drum kick
        ])
        instruments, rail_data = midi_to_multi_rail_tick_data(mid)
        assert len(instruments) >= 1
        assert 'drum' in instruments
        # Drum rail should have activity
        drum_ri = instruments.index('drum')
        assert any(any(t) for t in rail_data[drum_ri]), "Drum rail should have activity"

    def test_multi_instrument_auto_detect(self):
        """Different program changes → different instrument rails."""
        mid = mido.MidiFile(ticks_per_beat=480)
        # Track 0: piano (program 0)
        t0 = mido.MidiTrack()
        t0.append(mido.Message("program_change", program=0, channel=0, time=0))
        t0.append(mido.Message("note_on", note=60, velocity=100, channel=0, time=0))
        t0.append(mido.Message("note_off", note=60, velocity=0, channel=0, time=480))
        # Track 1: bass (program 32)
        t1 = mido.MidiTrack()
        t1.append(mido.Message("program_change", program=32, channel=1, time=0))
        t1.append(mido.Message("note_on", note=41, velocity=80, channel=1, time=0))
        t1.append(mido.Message("note_off", note=41, velocity=0, channel=1, time=480))
        mid.tracks.extend([t0, t1])

        instruments, rail_data = midi_to_multi_rail_tick_data(mid)
        assert len(instruments) == 2
        assert set(instruments) == {"piano", "bass"}

    def test_single_rail_when_all_same_instrument(self):
        """All notes on same instrument → single rail."""
        mid = self._make_midi_with_channel([
            (60, 100, 0, 480, 0),
            (64, 80, 0, 480, 0),
        ])
        instruments, rail_data = midi_to_multi_rail_tick_data(mid)
        assert len(instruments) == 1
        assert instruments[0] == "piano"

    def test_empty_midi(self):
        """Empty MIDI returns empty lists."""
        mid = mido.MidiFile()
        instruments, rail_data = midi_to_multi_rail_tick_data(mid)
        assert instruments == []
        assert rail_data == []


# ── optimal global octave shift tests ─────────────────────────────────

class TestFindOptimalOctaveShift:
    """Tests for ``find_optimal_octave_shift``."""

    def test_empty_notes_returns_zero(self):
        from factorio_display.audio.midi_translator import find_optimal_octave_shift
        assert find_optimal_octave_shift([], "piano") == 0

    def test_all_in_range_no_shift(self):
        """All notes already in piano range → shift=0."""
        from factorio_display.audio.midi_translator import find_optimal_octave_shift
        notes = [60, 64, 67, 72]  # C4, E4, G4, C5 — all in 53-100
        assert find_optimal_octave_shift(notes, "piano") == 0

    def test_mostly_above_range_shift_down(self):
        """Most notes above piano range → shift down."""
        from factorio_display.audio.midi_translator import find_optimal_octave_shift
        # F5-E7 (77-100) fits; E8 (112) doesn't
        notes = [77, 80, 84, 88, 92, 96, 100, 112]
        # With shift -12: 65,68,72,76,80,84,88,100 — all in range (53-100)
        assert find_optimal_octave_shift(notes, "piano") == -12

    def test_mostly_below_range_shift_up(self):
        """Most notes below piano range → shift up."""
        from factorio_display.audio.midi_translator import find_optimal_octave_shift
        # All notes in octave 2 (36-47), below F3=53
        notes = [36, 40, 43, 47]
        # Shift +24: 60,64,67,71 — all in range
        assert find_optimal_octave_shift(notes, "piano") == 24

    def test_tie_prefers_zero(self):
        """When multiple shifts have same count, prefer 0."""
        from factorio_display.audio.midi_translator import find_optimal_octave_shift
        # Half in range at shift=0, half at shift=-12 — tie, prefer 0
        notes = [60, 64, 67, 72,  # in range at shift=0
                  89, 93, 96, 100]  # also in range at shift=0
        assert find_optimal_octave_shift(notes, "piano") == 0

    def test_tie_prefers_smaller_abs_shift(self):
        """When tied and 0 not an option, prefer smaller |shift|."""
        from factorio_display.audio.midi_translator import find_optimal_octave_shift
        # All notes at MIDI 101-112 → shift -12 gives 89-100 (all in range)
        # shift -24 gives 77-88 (all in range too) — -12 wins (smaller |shift|)
        notes = list(range(101, 113))
        assert find_optimal_octave_shift(notes, "piano") == -12

    def test_celesta_range(self):
        """Celesta range F4-E7 (65-112) has different optimal shifts."""
        from factorio_display.audio.midi_translator import find_optimal_octave_shift
        # C3-E3 (48-52) — well below celesta range
        notes = [48, 50, 52]
        # Shift +24: 72,74,76 — in celesta range
        assert find_optimal_octave_shift(notes, "celesta") == 24

    def test_bass_range(self):
        """Bass range F2-E5 (41-88)."""
        from factorio_display.audio.midi_translator import find_optimal_octave_shift
        # C5-E6 (72-88) — upper end of bass range
        notes = [72, 76, 79, 84, 88]
        # At shift=0: all in range → 0
        assert find_optimal_octave_shift(notes, "bass") == 0

    def test_degenerate_single_note(self):
        """Single note: shift should bring it into range if possible."""
        from factorio_display.audio.midi_translator import find_optimal_octave_shift
        # MIDI 112 (E8) way above piano range 53-100
        # -12→100 (E7, in range), -24→88 (E6, in range)
        # Both get count=1, tie-breaker picks -12 (smaller |shift|)
        assert find_optimal_octave_shift([112], "piano") == -12


class TestGlobalShiftIntegration:
    """Tests for global octave shift in ``midi_to_tick_data`` and
    ``midi_to_multi_rail_tick_data``."""

    def test_mixed_notes_get_optimal_shift(self):
        """Mix of in-range and out-of-range notes → optimal shift logged,
        no per-note folds needed if all fit after shift."""
        # Most notes at C5-E7 (72-100, in piano range), one at E8 (112)
        mid = _make_midi([
            (72, 100, 0, 480),
            (76, 100, 0, 480),
            (80, 100, 0, 480),
            (84, 100, 0, 480),
            (112, 100, 0, 480),  # E8, out of range
        ])
        buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = buf
        try:
            result = midi_to_tick_data(mid, ticks_per_beat=30)
            log_output = buf.getvalue()
        finally:
            sys.stderr = old_stderr

        assert len(result) > 0
        assert "Global octave shift" in log_output
        # With -12 shift: 60,64,68,72,100 — all in range, no per-note folds
        assert "folded" not in log_output.lower(), (
            f"Optimised shift should avoid per-note folds: {log_output!r}"
        )

    def test_multi_rail_per_instrument_shift(self):
        """Each instrument rail gets its own optimal global shift."""
        mid = mido.MidiFile(ticks_per_beat=480)
        # Track 0: bass (program 32), notes in C5-C6 (72-84)
        t0 = mido.MidiTrack()
        t0.append(mido.Message("program_change", program=32, channel=0, time=0))
        for n in [72, 76, 79, 84]:
            t0.append(mido.Message("note_on", note=n, velocity=100, channel=0, time=0))
            t0.append(mido.Message("note_off", note=n, velocity=0, channel=0, time=480))
        # Track 1: celesta (program 8), notes in C3-C4 (48-60)
        t1 = mido.MidiTrack()
        t1.append(mido.Message("program_change", program=8, channel=1, time=0))
        for n in [48, 52, 55, 60]:
            t1.append(mido.Message("note_on", note=n, velocity=100, channel=1, time=0))
            t1.append(mido.Message("note_off", note=n, velocity=0, channel=1, time=480))
        mid.tracks.extend([t0, t1])

        buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = buf
        try:
            instruments, rail_data = midi_to_multi_rail_tick_data(mid)
            log_output = buf.getvalue()
        finally:
            sys.stderr = old_stderr

        assert len(instruments) == 2
        # Bass range (41-88): C5-C6 already fits → shift=0
        # Celesta range (65-112): C3-C4 needs +24 → shift=+24 logged
        assert "Global octave shift" in log_output
        # Celesta should have a +24 shift logged
        assert "[celesta]" in log_output

    def test_drum_rail_skips_global_shift(self):
        """Drum rails should not get global shift (they use GM_DRUM_MAP)."""
        mid = TestMultiRailMidi._make_midi_with_channel([
            (36, 80, 0, 240, 9),   # drum kick on ch9
        ])
        buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = buf
        try:
            instruments, rail_data = midi_to_multi_rail_tick_data(mid)
            log_output = buf.getvalue()
        finally:
            sys.stderr = old_stderr

        assert 'drum' in instruments
        # No global shift should be logged for drum
        assert "[drum]" not in log_output or "Global octave shift" not in log_output
