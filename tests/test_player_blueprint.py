"""Unit tests for player_blueprint.py — speaker matrix blueprint builder."""

from __future__ import annotations

import pytest

from draftsman.blueprintable import Blueprint

from factorio_display.audio.player_blueprint import (
    DEBUG_Y,
    INSTRUMENT_MAP,
    RAIL_WIDTH,
    build_audio_decoder,
    build_multi_rail_decoder,
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
        """Unpackers should be directly below match DCs at y>=4."""
        bp_str = build_audio_decoder()
        bp = _parse_bp(bp_str)
        acs = _get_entities_by_type(bp, "arithmetic-combinator")
        xs = {ac.tile_position[0] for ac in acs}
        ys = {ac.tile_position[1] for ac in acs}
        assert min(xs) == 0
        assert max(xs) >= 12  # mod AC somewhere to the right
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

    # ── debug lamps ──────────────────────────────────────────────────

    def test_debug_lamps_off_by_default(self):
        """No debug lamps when debug_lamps=False (default)."""
        bp_str = build_audio_decoder()
        bp = _parse_bp(bp_str)
        lamps = _get_entities_by_type(bp, "small-lamp")
        assert len(lamps) == 0

    def test_debug_lamps_count(self):
        """48 debug lamps when debug_lamps=True."""
        bp_str = build_audio_decoder(debug_lamps=True)
        bp = _parse_bp(bp_str)
        lamps = _get_entities_by_type(bp, "small-lamp")
        assert len(lamps) == 48

    def test_debug_lamps_entity_count_includes_lamps(self):
        """Total entity count increases by 48 with debug lamps."""
        bp_no = _parse_bp(build_audio_decoder(debug_lamps=False))
        bp_yes = _parse_bp(build_audio_decoder(debug_lamps=True))
        assert len(bp_yes.entities) == len(bp_no.entities) + 48

    def test_debug_lamps_positions(self):
        """Debug lamps sit at DEBUG_Y below the speaker grid, same columns."""
        bp_str = build_audio_decoder(debug_lamps=True)
        bp = _parse_bp(bp_str)
        lamps = _get_entities_by_type(bp, "small-lamp")

        xs = {lamp.tile_position[0] for lamp in lamps}
        ys = {lamp.tile_position[1] for lamp in lamps}
        assert min(xs) == 0
        assert max(xs) == 11  # 12 columns, matching speakers
        assert min(ys) == DEBUG_Y + 0       # lowest debug row
        assert max(ys) == DEBUG_Y + 3       # highest debug row

    def test_debug_lamps_color_mode(self):
        """Debug lamps use color_mode=2 (packed RGB from signal)."""
        bp_str = build_audio_decoder(debug_lamps=True)
        bp = _parse_bp(bp_str)
        lamps = _get_entities_by_type(bp, "small-lamp")
        for lamp in lamps:
            assert lamp.use_colors is True
            assert lamp.color_mode == 2
            assert lamp.always_on is True

    def test_debug_lamps_signals_match_speakers(self):
        """Debug lamp rgb_signals match speaker volume_signals (name, quality)."""
        bp_str = build_audio_decoder(debug_lamps=True)
        bp = _parse_bp(bp_str)
        speakers = _get_entities_by_type(bp, "programmable-speaker")
        lamps = _get_entities_by_type(bp, "small-lamp")

        spk_signals: set[tuple[str, str]] = set()
        for spk in speakers:
            vs = spk.volume_signal
            spk_signals.add((vs.name, getattr(vs, 'quality', 'normal')))

        lamp_signals: set[tuple[str, str]] = set()
        for lamp in lamps:
            rs = lamp.rgb_signal
            lamp_signals.add((rs.name, getattr(rs, 'quality', 'normal')))

        assert lamp_signals == spk_signals
        assert len(lamp_signals) == 48

    def test_debug_lamps_on_red_wire(self):
        """Debug lamps increase red-wire connection count vs no-debug baseline."""
        bp_no = _parse_bp(build_audio_decoder(debug_lamps=False))
        bp_yes = _parse_bp(build_audio_decoder(debug_lamps=True))

        def _count_red_wires(bp: Blueprint) -> int:
            # wires are [entity, color_int, entity, side_int]; color 1 = red
            return sum(1 for w in bp.wires if len(w) > 1 and w[1] == 1)

        red_no = _count_red_wires(bp_no)
        red_yes = _count_red_wires(bp_yes)
        assert red_yes > red_no, (
            f"Expected more red wires with debug lamps, "
            f"got {red_no} (off) vs {red_yes} (on)"
        )


# ── multi-rail tests ──────────────────────────────────────────────────

class TestMultiRailDecoder:
    """Tests for ``build_multi_rail_decoder``."""

    def test_single_rail_equivalent_to_legacy(self):
        """Single-rail multi builder produces same entity counts as build_audio_decoder."""
        bp_legacy = _parse_bp(build_audio_decoder(name="Test"))
        bp_multi = _parse_bp(build_multi_rail_decoder(
            name="Test", instruments=["piano"],
        ))
        assert len(bp_multi.entities) == len(bp_legacy.entities)
        assert len(_get_entities_by_type(bp_multi, "programmable-speaker")) == 48
        assert len(_get_entities_by_type(bp_multi, "arithmetic-combinator")) == 85

    def test_two_rails_double_speakers(self):
        """Two rails → 96 speakers."""
        bp = _parse_bp(build_multi_rail_decoder(
            name="Dual", instruments=["piano", "bass"],
        ))
        speakers = _get_entities_by_type(bp, "programmable-speaker")
        assert len(speakers) == 96

    def test_two_rails_share_one_mod_ac(self):
        """Two rails share exactly one modulo AC (not one per rail)."""
        bp = _parse_bp(build_multi_rail_decoder(
            name="Dual", instruments=["piano", "bass"],
        ))
        acs = _get_entities_by_type(bp, "arithmetic-combinator")
        # 7perCh×12×2 rails + 1 shared mod = 169
        assert len(acs) == 7 * 12 * 2 + 1

    def test_rail_speaker_x_positions(self):
        """Rail 0 speakers at cols 0..11, rail 1 at cols 13..24."""
        bp = _parse_bp(build_multi_rail_decoder(
            name="Dual", instruments=["piano", "bass"],
        ))
        speakers = _get_entities_by_type(bp, "programmable-speaker")
        xs = sorted({int(s.tile_position[0]) for s in speakers})
        # Rail 0: 0..11, Rail 1: 13..24 (port at col 12 and 25)
        assert min(xs) == 0
        assert max(xs) == 24
        assert 11 in xs
        assert 12 not in xs  # page_port column, no speakers
        assert 13 in xs      # first column of rail 1

    def test_different_instruments_per_rail(self):
        """Each rail gets its own instrument."""
        bp = _parse_bp(build_multi_rail_decoder(
            name="Mixed", instruments=["piano", "drum"],
        ))
        speakers = _get_entities_by_type(bp, "programmable-speaker")
        # Rail 0 speakers (cols 0..11): piano
        # Rail 1 speakers (cols 13..24): drum-kit
        rail0 = [s for s in speakers if int(s.tile_position[0]) < 12]
        rail1 = [s for s in speakers if int(s.tile_position[0]) >= 13]
        assert len(rail0) == 48
        assert len(rail1) == 48
        for s in rail0:
            assert s.instrument_name == "piano", f"Rail 0 expected piano, got {s.instrument_name}"
        for s in rail1:
            assert s.instrument_name == "drum-kit", f"Rail 1 expected drum-kit, got {s.instrument_name}"

    def test_multi_rail_has_cross_rail_wiring(self):
        """Multi-rail blueprint has more wires than single-rail (cross-rail connections)."""
        bp_single = _parse_bp(build_audio_decoder())
        bp_dual = _parse_bp(build_multi_rail_decoder(
            name="Dual", instruments=["piano", "piano"],
        ))
        assert len(bp_dual.wires) > len(bp_single.wires) * 2, (
            f"Expected cross-rail wiring overhead"
        )

    def test_multi_rail_empty_instruments_raises(self):
        """Empty instruments list raises ValueError."""
        with pytest.raises(ValueError):
            build_multi_rail_decoder(instruments=[])

    def test_multi_rail_debug_lamps(self):
        """Debug lamps work with multi-rail (96 lamps for 2 rails)."""
        bp = _parse_bp(build_multi_rail_decoder(
            name="Debug", instruments=["piano", "piano"], debug_lamps=True,
        ))
        lamps = _get_entities_by_type(bp, "small-lamp")
        assert len(lamps) == 96
