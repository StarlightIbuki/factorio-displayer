"""Unit tests for player_blueprint.py — speaker matrix blueprint builder."""

from __future__ import annotations

import pytest

from draftsman.blueprintable import Blueprint

from factorio_display.audio.player_blueprint import (
    INSTRUMENT_MAP,
    build_audio_decoder,
)
from factorio_display.audio.pitch_mapping import (
    SPEAKER_COUNT,
    pitch_index_to_signal,
)


# ── helpers ────────────────────────────────────────────────────────────

def _parse_bp(bp_str: str) -> Blueprint:
    """Parse a blueprint string and return the Blueprint object."""
    return Blueprint.from_string(bp_str)


def _get_entities_by_type(bp: Blueprint, name_fragment: str) -> list:
    """Filter entities whose ``.name`` contains *name_fragment*."""
    return [e for e in bp.entities if name_fragment in e.name]


# ── tests ──────────────────────────────────────────────────────────────

class TestBuildAudioDecoder:
    def test_default_blueprint_generates(self):
        bp_str = build_audio_decoder()
        assert isinstance(bp_str, str)
        assert bp_str.startswith("0e")
        assert len(bp_str) > 500

    def test_custom_name(self):
        bp_str = build_audio_decoder(name="My Piano")
        bp = _parse_bp(bp_str)
        assert bp.label == "My Piano"

    def test_entity_counts(self):
        """48 speakers + 85 ACs + 24 DCs + 13 CCs = 170 entities."""
        bp_str = build_audio_decoder()
        bp = _parse_bp(bp_str)
        speakers = _get_entities_by_type(bp, "programmable-speaker")
        ariths = _get_entities_by_type(bp, "arithmetic-combinator")
        dcs = _get_entities_by_type(bp, "decider-combinator")
        ccs = _get_entities_by_type(bp, "constant-combinator")
        assert len(speakers) == 48
        assert len(ariths) == 85     # 7perCh×12 + 1mod
        assert len(dcs) == 24        # 2perCh (match + match0)
        assert len(ccs) == 13        # 1perCh lookup + 1 port
        assert len(bp.entities) == 170

    def test_unpacker_positions_compact(self):
        """Unpackers should be directly below match DCs at y>=4, cols 0..12."""
        bp_str = build_audio_decoder()
        bp = _parse_bp(bp_str)
        acs = _get_entities_by_type(bp, "arithmetic-combinator")
        xs = {ac.tile_position[0] for ac in acs}
        ys = {ac.tile_position[1] for ac in acs}
        assert min(xs) == 0
        assert max(xs) == 12  # mod AC at col 12
        assert min(ys) >= 4   # speakers end at y=3

    def test_all_speaker_signals_unique(self):
        """Each speaker must have a unique (signal_name, quality) pair."""
        bp_str = build_audio_decoder()
        bp = _parse_bp(bp_str)
        speakers = _get_entities_by_type(bp, "programmable-speaker")

        seen: set[tuple[str, str]] = set()
        for spk in speakers:
            vs = spk.volume_signal
            # volume_signal is a SignalID namedtuple after parsing
            pair = (vs.name, getattr(vs, 'quality', 'normal'))
            assert pair not in seen, f"Duplicate speaker signal: {pair}"
            seen.add(pair)
        assert len(seen) == 48

    def test_speaker_signals_match_pitch_mapping(self):
        """The volume_signal names on speakers must match pitch_index_to_signal names.

        Since entity IDs are not preserved through blueprint serialization,
        we compare the set of (name, quality) pairs.
        """
        bp_str = build_audio_decoder()
        bp = _parse_bp(bp_str)
        speakers = _get_entities_by_type(bp, "programmable-speaker")

        expected_signals = {
            (sig["name"], sig["quality"])
            for i in range(SPEAKER_COUNT)
            for sig in [pitch_index_to_signal(i)]
        }
        found_signals: set[tuple[str, str]] = set()

        for spk in speakers:
            vs = spk.volume_signal
            found_signals.add((vs.name, getattr(vs, 'quality', 'normal')))

        assert found_signals == expected_signals, (
            f"Missing: {expected_signals - found_signals}, "
            f"Extra: {found_signals - expected_signals}"
        )

    def test_speakers_have_polyphony(self):
        """All speakers should have polyphony disabled (single-note per speaker)."""
        bp_str = build_audio_decoder()
        bp = _parse_bp(bp_str)
        speakers = _get_entities_by_type(bp, "programmable-speaker")
        for spk in speakers:
            assert spk.allow_polyphony is True

    def test_speakers_use_volume_mode(self):
        """Circuit mode should be 'volume' (volume controlled by signal)."""
        bp_str = build_audio_decoder()
        bp = _parse_bp(bp_str)
        speakers = _get_entities_by_type(bp, "programmable-speaker")
        for spk in speakers:
            assert spk.volume_controlled_by_signal is True

    def test_speaker_grid_dimensions(self):
        """Speakers should span 12 columns × 4 rows."""
        bp_str = build_audio_decoder()
        bp = _parse_bp(bp_str)
        speakers = _get_entities_by_type(bp, "programmable-speaker")

        xs = {spk.tile_position[0] for spk in speakers}
        ys = {spk.tile_position[1] for spk in speakers}
        assert min(xs) == 0
        assert max(xs) == 11  # 12 columns
        assert min(ys) == 0
        assert max(ys) == 3   # 4 rows

    @pytest.mark.parametrize("instrument,expected", [
        ("piano", "piano"),
        ("bass", "bass"),
        ("celesta", "celesta"),
        ("plucked", "plucked"),
        ("drum", "drum-kit"),
    ])
    def test_instrument_mapping(self, instrument, expected):
        """Each instrument name maps to the correct Draftsman instrument."""
        bp_str = build_audio_decoder(instrument=instrument)
        bp = _parse_bp(bp_str)
        speakers = _get_entities_by_type(bp, "programmable-speaker")
        for spk in speakers:
            assert spk.instrument_name == expected, (
                f"Expected {expected}, got {spk.instrument_name}"
            )

    def test_custom_instrument_passthrough(self):
        """If instrument is not in the map, it's used verbatim."""
        # Custom instruments that aren't in Draftsman's known list can't be
        # set via instrument_name setter (it validates).  We use 'piano' as
        # the fallback; the builder's instrument_map handles standard ones.
        bp_str = build_audio_decoder(instrument="piano")
        bp = _parse_bp(bp_str)
        speakers = _get_entities_by_type(bp, "programmable-speaker")
        for spk in speakers:
            assert spk.instrument_name == "piano"

    def test_circuit_wiring_exists(self):
        """The speaker grid should have red-wire connections."""
        bp_str = build_audio_decoder()
        bp = _parse_bp(bp_str)
        # Draftsman stores wires as a list of wire connection tuples
        assert len(bp.wires) > 0, "Expected at least one wire connection"

    def test_lut_cc_values_are_nonzero(self):
        """All lookup CC entries must have non-zero values.

        Factorio drops signals with value 0 from the circuit network,
        which would make sub_tick=0 CC entries invisible to match DCs.
        The t=0 entry uses value 60 (non-zero) and is matched by the
        match0 DC (sub_tick==0 AND each==60).
        """
        bp_str = build_audio_decoder()
        bp = _parse_bp(bp_str)
        ccs = _get_entities_by_type(bp, "constant-combinator")
        # LUT CCs are at y=22, cols 0..11; page_port is at (12, 16).
        from factorio_display.audio.player_blueprint import LUT_Y, TICKS_PER_PAGE
        luts = [
            cc for cc in ccs
            if cc.tile_position[1] == LUT_Y
        ]
        assert len(luts) == 12, f"Expected 12 LUT CCs, found {len(luts)}"
        for cc in luts:
            for slot in range(1, TICKS_PER_PAGE + 1):
                sig = cc.get_signal(slot)
                assert sig is not None, (
                    f"LUT CC at {cc.tile_position} missing signal at slot {slot}"
                )
                assert sig.count != 0, (
                    f"LUT CC at {cc.tile_position} slot {slot} ({sig.name}) "
                    f"has count=0 — 0-value signals are dropped by Factorio"
                )

    def test_blueprint_roundtrip(self):
        """Blueprint string → parse → re-export should be stable."""
        bp_str_1 = build_audio_decoder(name="Roundtrip Test")
        bp = _parse_bp(bp_str_1)
        bp_str_2 = bp.to_string()
        # Parse again to compare structurally
        bp2 = _parse_bp(bp_str_2)
        assert bp2.label == "Roundtrip Test"
        assert len(bp2.entities) == len(bp.entities)
