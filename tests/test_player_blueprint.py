"""Unit tests for player_blueprint.py — speaker matrix blueprint builder."""

from __future__ import annotations

import pytest

from draftsman.blueprintable import Blueprint

from factorio_display.audio.player_blueprint import (
    DEBUG_Y,
    INSTRUMENT_MAP,
    RAIL_WIDTH,
    _pitch_index_to_factorio_note,
    build_audio_decoder,
    build_audio_decoder_logical,
    build_multi_rail_decoder,
    build_multi_rail_decoder_logical,
)
from factorio_display.audio.pitch_mapping import (
    DRUM_KIT_NOTES,
    SPEAKER_COUNT,
    pitch_index_to_signal,
)
from factorio_display.video.player_blueprint import (
    build_display_logical,
)
from factorio_display import SIGNAL_POOL, QUALITIES


# ── helpers ────────────────────────────────────────────────────────────

def _parse_bp(bp_str: str) -> Blueprint:
    """Parse a blueprint string and return the Blueprint object."""
    return Blueprint.from_string(bp_str)


def _get_entities_by_type(bp: Blueprint, name_fragment: str) -> list:
    """Filter entities whose ``.name`` contains *name_fragment*."""
    return [e for e in bp.entities if name_fragment in e.name]


def _speaker_red_networks(bp_str: str) -> list[list[str]]:
    """Speaker entity ids, grouped by the red networks that contain them.

    After the cross-column merge every instrument's speakers must share
    exactly ONE red network (they used to be one network per column), while
    different instruments must stay on separate networks.
    """
    from factorio_display.logical_blueprint import from_draftsman  # pylint: disable=import-outside-toplevel
    lb = from_draftsman(_parse_bp(bp_str))
    nets = []
    for net in lb.networks:
        if net.color != "red":
            continue
        spk = [
            ep.entity_id for ep in net.endpoints
            if lb.entities.get(ep.entity_id) is not None
            and lb.entities[ep.entity_id].type == "programmable-speaker"
        ]
        if spk:
            nets.append(spk)
    return nets


def _assert_valid_audio_topology(bp_str: str) -> None:
    """Round-trip *bp_str* through the logical model and assert the
    materialised wires are placeable (degree ≤ 2 per port per colour,
    connected per network, within the 9-tile reach)."""
    from factorio_display.logical_blueprint import (  # pylint: disable=import-outside-toplevel
        assert_wire_topology,
        from_draftsman,
        to_draftsman,
    )
    lb = from_draftsman(_parse_bp(bp_str))
    assert_wire_topology(to_draftsman(lb), label="audio-topology", lb=lb)


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

    def test_entity_counts(self, audio_decoder_bp_str, audio_decoder_bp):
        """48 speakers + 85 ACs + 12 DCs + 13 CCs = 158 entities."""
        bp = audio_decoder_bp
        speakers = _get_entities_by_type(bp, "programmable-speaker")
        ariths = _get_entities_by_type(bp, "arithmetic-combinator")
        dcs = _get_entities_by_type(bp, "decider-combinator")
        ccs = _get_entities_by_type(bp, "constant-combinator")
        assert len(speakers) == 48
        assert len(ariths) == 85     # 7perCh×12 + 1mod
        assert len(dcs) == 12        # 1perCh (match only)
        assert len(ccs) == 13        # 1perCh lookup + 1 port
        assert len(bp.entities) == 158

        # ── logical-blueprint validation ──────────────────────────
        from conftest import validate_blueprint_via_logical  # pylint: disable=import-outside-toplevel
        result = validate_blueprint_via_logical(audio_decoder_bp_str)
        assert result["errors"] == [], (
            f"Logical-blueprint validation failed: {result['errors']}"
        )
        assert result["entity_count"] == 158

    def test_unpacker_positions_compact(self, audio_decoder_bp):
        """Unpackers should be directly below match DCs at y>=4."""
        bp = audio_decoder_bp
        acs = _get_entities_by_type(bp, "arithmetic-combinator")
        xs = {ac.tile_position[0] for ac in acs}
        ys = {ac.tile_position[1] for ac in acs}
        assert min(xs) == 0
        assert max(xs) >= 12  # mod AC somewhere to the right
        assert min(ys) >= 4   # speakers end at y=3

    def test_all_speaker_signals_unique(self, audio_decoder_bp):
        """Each speaker must have a unique (signal_name, quality) pair."""
        bp = audio_decoder_bp
        speakers = _get_entities_by_type(bp, "programmable-speaker")

        seen: set[tuple[str, str]] = set()
        for spk in speakers:
            vs = spk.volume_signal
            # volume_signal is a SignalID namedtuple after parsing
            pair = (vs.name, getattr(vs, 'quality', 'normal'))
            assert pair not in seen, f"Duplicate speaker signal: {pair}"
            seen.add(pair)
        assert len(seen) == 48

    def test_speaker_signals_match_pitch_mapping(self, audio_decoder_bp):
        """The volume_signal names on speakers must match pitch_index_to_signal names.

        Since entity IDs are not preserved through blueprint serialization,
        we compare the set of (name, quality) pairs.
        """
        bp = audio_decoder_bp
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

    def test_speakers_have_polyphony(self, audio_decoder_bp):
        """All speakers should have polyphony disabled (single-note per speaker)."""
        bp = audio_decoder_bp
        speakers = _get_entities_by_type(bp, "programmable-speaker")
        for spk in speakers:
            assert spk.allow_polyphony is True

    def test_speakers_use_volume_mode(self, audio_decoder_bp):
        """Circuit mode should be 'volume' (volume controlled by signal)."""
        bp = audio_decoder_bp
        speakers = _get_entities_by_type(bp, "programmable-speaker")
        for spk in speakers:
            assert spk.volume_controlled_by_signal is True

    def test_speaker_grid_dimensions(self, audio_decoder_bp):
        """Speakers should span 12 columns × 4 rows (starting at SPK_Y)."""
        from factorio_display.audio.player_blueprint import SPK_Y  # pylint: disable=import-outside-toplevel
        bp = audio_decoder_bp
        speakers = _get_entities_by_type(bp, "programmable-speaker")

        xs = {spk.tile_position[0] for spk in speakers}
        ys = {spk.tile_position[1] for spk in speakers}
        assert min(xs) == 0
        assert max(xs) == 11  # 12 columns
        assert min(ys) == SPK_Y
        assert max(ys) == SPK_Y + 3   # 4 rows

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

    def test_circuit_wiring_exists(self, audio_decoder_bp_str, audio_decoder_bp):
        """The speaker grid should have red-wire connections."""
        bp = audio_decoder_bp
        # Draftsman stores wires as a list of wire connection tuples
        assert len(bp.wires) > 0, "Expected at least one wire connection"

        # ── logical-blueprint validation ──────────────────────────
        from conftest import validate_blueprint_via_logical  # pylint: disable=import-outside-toplevel
        result = validate_blueprint_via_logical(audio_decoder_bp_str)
        assert result["errors"] == [], f"Validation errors: {result['errors']}"
        assert result["network_count"] > 0

    def test_all_speakers_share_one_red_network(self, audio_decoder_bp_str):
        """All 48 speakers of one instrument sit on a single red network.

        Each column used to carry its own independent red speaker network;
        they are now bridged into one per-instrument network — fewer
        networks, and a single probe point shows the whole instrument.
        """
        nets = _speaker_red_networks(audio_decoder_bp_str)
        assert len(nets) == 1, (
            f"Expected one speaker red network, got {len(nets)}"
        )
        assert len(nets[0]) == 48, f"Expected 48 speakers, got {len(nets[0])}"
        _assert_valid_audio_topology(audio_decoder_bp_str)

    def test_lut_cc_values_are_nonzero(self, audio_decoder_bp):
        """All lookup CC entries must have non-zero values.

        Factorio drops signals with value 0 from the circuit network, so the
        lookup CC stores 1..60 (never 0).  Sub-tick 0 (page tick 0) is left
        silent: with no match0 row, the normal match DC (each == signal-M)
        selects ticks 1..59 and tick 0 simply plays nothing.
        """
        bp = audio_decoder_bp
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

    def test_debug_lamps_off_by_default(self, audio_decoder_bp):
        """No debug lamps when debug_lamps=False (default)."""
        bp = audio_decoder_bp
        lamps = _get_entities_by_type(bp, "small-lamp")
        assert len(lamps) == 0

    def test_debug_lamps_count(self, audio_decoder_debug_bp):
        """48 debug lamps when debug_lamps=True."""
        bp = audio_decoder_debug_bp
        lamps = _get_entities_by_type(bp, "small-lamp")
        assert len(lamps) == 48

    def test_debug_lamps_entity_count_includes_lamps(self, audio_decoder_bp, audio_decoder_debug_bp):
        """Total entity count increases by 48 with debug lamps."""
        bp_no = audio_decoder_bp
        bp_yes = audio_decoder_debug_bp
        assert len(bp_yes.entities) == len(bp_no.entities) + 48

    def test_debug_lamps_positions(self, audio_decoder_debug_bp):
        """Debug lamps sit at DEBUG_Y below the speaker grid, same columns."""
        bp = audio_decoder_debug_bp
        lamps = _get_entities_by_type(bp, "small-lamp")

        xs = {lamp.tile_position[0] for lamp in lamps}
        ys = {lamp.tile_position[1] for lamp in lamps}
        assert min(xs) == 0
        assert max(xs) == 11  # 12 columns, matching speakers
        assert min(ys) == DEBUG_Y + 0       # lowest debug row
        assert max(ys) == DEBUG_Y + 3       # highest debug row

    def test_debug_lamps_color_mode(self, audio_decoder_debug_bp):
        """Debug lamps use color_mode=2 (packed RGB from signal)."""
        bp = audio_decoder_debug_bp
        lamps = _get_entities_by_type(bp, "small-lamp")
        for lamp in lamps:
            assert lamp.use_colors is True
            assert lamp.color_mode == 2
            assert lamp.always_on is True

    def test_debug_lamps_signals_match_speakers(self, audio_decoder_debug_bp):
        """Debug lamp rgb_signals match speaker volume_signals (name, quality)."""
        bp = audio_decoder_debug_bp
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

    def test_debug_lamps_on_red_wire(self, audio_decoder_bp, audio_decoder_debug_bp):
        """Debug lamps increase red-wire connection count vs no-debug baseline."""
        bp_no = audio_decoder_bp
        bp_yes = audio_decoder_debug_bp

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

    def test_single_rail_equivalent_to_legacy(self, audio_decoder_bp):
        """Single-rail multi builder produces same entity counts as build_audio_decoder."""
        bp_legacy = audio_decoder_bp
        bp_multi = _parse_bp(build_multi_rail_decoder(
            name="Test", instruments=["piano"],
        ))
        assert len(bp_multi.entities) == len(bp_legacy.entities)
        assert len(_get_entities_by_type(bp_multi, "programmable-speaker")) == 48
        assert len(_get_entities_by_type(bp_multi, "arithmetic-combinator")) == 85

    def test_two_rails_speaker_counts(self):
        """Piano rail keeps 48 speakers; a bass rail needs only its real
        range (F2-E5 = 36) — so two rails → 84 speakers."""
        bp_str = build_multi_rail_decoder(
            name="Dual", instruments=["piano", "bass"],
        )
        bp = _parse_bp(bp_str)
        speakers = _get_entities_by_type(bp, "programmable-speaker")
        assert len(speakers) == 48 + 36

        # ── logical-blueprint validation ──────────────────────────
        from conftest import validate_blueprint_via_logical  # pylint: disable=import-outside-toplevel
        result = validate_blueprint_via_logical(bp_str)
        assert result["errors"] == [], f"Validation errors: {result['errors']}"
        assert result["entity_count"] == len(bp.entities)

    def test_bass_rail_uses_real_range_speakers(self):
        """A bass rail needs only 36 physical speakers (its real range).

        The generic 48-speaker grid matches piano (F3-E7, 4 octaves).  The
        other melodic instruments have a 3-octave real range (36 notes), so
        they place 36 speakers — the missing 4th octave is never driven by
        the routing, so its speakers (and the top lamp row) are not placed.
        """
        bp = _parse_bp(build_multi_rail_decoder(
            name="Bass", instruments=["bass"],
        ))
        speakers = _get_entities_by_type(bp, "programmable-speaker")
        assert len(speakers) == 36
        # Bass has no 4th-octave row (y == SPK_Y, the top speaker row).
        ys = {int(s.tile_position[1]) for s in speakers}
        assert min(ys) >= 3, f"bass should only use 3 octave rows, got {sorted(ys)}"

    def test_two_rails_share_one_mod_ac(self):
        """Two rails share exactly one modulo AC (not one per rail)."""
        bp_str = build_multi_rail_decoder(
            name="Dual", instruments=["piano", "bass"],
        )
        bp = _parse_bp(bp_str)
        acs = _get_entities_by_type(bp, "arithmetic-combinator")
        # piano: selector + 6 unpackers = 7/ch × 12; bass: selector + 5
        # unpackers (3 octaves, no l4) = 6/ch × 12; + 1 shared mod
        assert len(acs) == 7 * 12 + 6 * 12 + 1

        # ── logical-blueprint validation ──────────────────────────
        from conftest import validate_blueprint_via_logical  # pylint: disable=import-outside-toplevel
        result = validate_blueprint_via_logical(bp_str)
        assert result["errors"] == [], f"Validation errors: {result['errors']}"

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
        # Drum rail (melodic fallback) uses its real range (F3-E6 = 36)
        assert len(rail1) == 36
        for s in rail0:
            assert s.instrument_name == "piano", f"Rail 0 expected piano, got {s.instrument_name}"
        for s in rail1:
            assert s.instrument_name == "drum-kit", f"Rail 1 expected drum-kit, got {s.instrument_name}"

    def test_instruments_have_separate_speaker_networks(self):
        """Speakers of different instruments must NOT share a red network.

        Within a rail all speakers are bridged into one network; across
        rails (instruments) they must stay separate so one instrument's
        activity never bleeds into another.
        """
        bp_str = build_multi_rail_decoder(
            name="Separate", instruments=["piano", "bass"],
        )
        nets = _speaker_red_networks(bp_str)
        assert len(nets) == 2, (
            f"Expected one speaker network per instrument, got {len(nets)}"
        )
        assert sorted(len(n) for n in nets) == [36, 48], (
            f"Unexpected speaker distribution: {[len(n) for n in nets]}"
        )
        _assert_valid_audio_topology(bp_str)

    def test_multi_rail_has_cross_rail_wiring(self, audio_decoder_bp):
        """Multi-rail blueprint has more wires than single-rail (cross-rail connections)."""
        bp_single = audio_decoder_bp
        bp_dual = _parse_bp(build_multi_rail_decoder(
            name="Dual", instruments=["piano", "piano"],
        ))
        assert len(bp_dual.wires) >= len(bp_single.wires) * 2, (
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

    def test_no_unexpected_warnings_single_rail(self):
        """Single-rail decoder generation should not produce unexpected warnings."""
        import warnings
        from conftest import assert_no_unexpected_warnings  # pylint: disable=import-outside-toplevel

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            build_audio_decoder()
        assert_no_unexpected_warnings(w)

    def test_no_unexpected_warnings_multi_rail(self):
        """Multi-rail decoder generation should not produce unexpected warnings."""
        import warnings
        from conftest import assert_no_unexpected_warnings  # pylint: disable=import-outside-toplevel

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            build_multi_rail_decoder(instruments=["piano"])
        assert_no_unexpected_warnings(w)


# ── logical-blueprint tests ───────────────────────────────────────────

class TestLogicalBlueprintDecoder:
    """Tests for ``build_audio_decoder_logical``."""

    def test_drum_kit_note_names_with_map_drums(self):
        """Drum-kit rail with map_drums=True uses drum-kit note names."""
        lb = build_audio_decoder_logical(
            name="Drum Test",
            instrument="drum",
            map_drums=True,
        )
        speakers = [e for e in lb.entities.values()
                     if e.type == "programmable-speaker"]
        # Standalone drum fallback uses the drum's real range (F3-E6 = 36).
        assert len(speakers) == 36
        for spk in speakers:
            pitch = int(spk.entity_id.rsplit("_", 1)[1])
            note = spk.properties.get("note", "")
            if pitch < len(DRUM_KIT_NOTES):
                assert note == DRUM_KIT_NOTES[pitch], (
                    f"Speaker {pitch}: expected {DRUM_KIT_NOTES[pitch]!r}, got {note!r}"
                )
            else:
                # Remaining speakers use placeholder (first drum note)
                assert note == DRUM_KIT_NOTES[0], (
                    f"Speaker {pitch}: expected placeholder {DRUM_KIT_NOTES[0]!r}, got {note!r}"
                )

    def test_drum_kit_note_names_without_map_drums(self):
        """Drum-kit rail with map_drums=False uses MIDI note names."""
        lb = build_audio_decoder_logical(
            name="Drum Test",
            instrument="drum",
            map_drums=False,
        )
        speakers = [e for e in lb.entities.values()
                     if e.type == "programmable-speaker"]
        # Standalone drum fallback uses the drum's real range (F3-E6 = 36).
        assert len(speakers) == 36
        for spk in speakers:
            pitch = int(spk.entity_id.rsplit("_", 1)[1])
            note = spk.properties.get("note", "")
            expected = _pitch_index_to_factorio_note(pitch)
            assert note == expected, (
                f"Speaker {pitch}: expected {expected!r}, got {note!r}"
            )

    def test_piano_uses_midi_note_names(self):
        """Piano rail always uses MIDI note names (regardless of map_drums)."""
        for map_drums in (True, False):
            lb = build_audio_decoder_logical(
                name="Piano Test",
                instrument="piano",
                map_drums=map_drums,
            )
            speakers = [e for e in lb.entities.values()
                         if e.type == "programmable-speaker"]
            assert len(speakers) == 48
            for spk in speakers:
                pitch = int(spk.entity_id.rsplit("_", 1)[1])
                note = spk.properties.get("note", "")
                expected = _pitch_index_to_factorio_note(pitch)
                assert note == expected, (
                    f"Speaker {pitch} (map_drums={map_drums}): "
                    f"expected {expected!r}, got {note!r}"
                )

    def test_logical_speakers_share_one_red_network(self):
        """Logical audio decoder: all 48 speakers sit on one red network."""
        from factorio_display.logical_blueprint import (  # pylint: disable=import-outside-toplevel
            assert_wire_topology,
            to_draftsman,
        )
        lb = build_audio_decoder_logical(name="L", instrument="piano")
        nets = []
        for net in lb.networks:
            if net.color != "red":
                continue
            spk = [
                ep.entity_id for ep in net.endpoints
                if lb.entities.get(ep.entity_id) is not None
                and lb.entities[ep.entity_id].type == "programmable-speaker"
            ]
            if spk:
                nets.append(spk)
        assert len(nets) == 1, f"Expected one speaker network, got {len(nets)}"
        assert len(nets[0]) == 48
        # Materialises with valid (≤2-wire/port, ≤9-tile) wiring.
        assert_wire_topology(to_draftsman(lb), label="logical-topology", lb=lb)

    def test_logical_multi_instruments_separate(self):
        """Multi-rail logical decoder keeps each instrument's speakers on its
        own red network (piano 48, bass 36)."""
        lb = build_multi_rail_decoder_logical(
            name="M", instruments=["piano", "bass"],
        )
        nets = []
        for net in lb.networks:
            if net.color != "red":
                continue
            spk = [
                ep.entity_id for ep in net.endpoints
                if lb.entities.get(ep.entity_id) is not None
                and lb.entities[ep.entity_id].type == "programmable-speaker"
            ]
            if spk:
                nets.append(spk)
        assert len(nets) == 2, f"Expected 2 speaker networks, got {len(nets)}"
        assert sorted(len(n) for n in nets) == [36, 48]

    def test_compact_drum_rail_only_builds_used_types(self):
        """A drum rail with active_drum_pitches builds only used drum types.

        Drums are a fixed set of sounds (not 48 pitches): a rail that only
        plays kick-1 stores a raw tick→volume cell — one speaker, one LUT,
        one match DC, one selector (no unpacker), port and mod.
        """
        lb = build_audio_decoder_logical(
            name="Drum Test",
            instrument="drum",
            map_drums=True,
            active_drum_pitches={0},
        )
        # page port + mod + LUT + match + sel + speaker = 6 (no unpacker)
        assert len(lb.entities) == 6
        speakers = [e for e in lb.entities.values()
                     if e.type == "programmable-speaker"]
        assert len(speakers) == 1
        assert speakers[0].properties["note"] == "kick-1"
        assert speakers[0].properties["instrument"] == "drum-kit"
        dcs = [e for e in lb.entities.values() if e.type == "decider-combinator"]
        ccs = [e for e in lb.entities.values() if e.type == "constant-combinator"]
        acs = [e for e in lb.entities.values() if e.type == "arithmetic-combinator"]
        assert len(dcs) == 1   # match
        assert len(ccs) == 2   # page port + LUT
        assert len(acs) == 2   # mod + selector (no unpacker)

    def test_drum_with_data_is_compact_even_without_map_drums(self):
        """A drum rail with active_drum_pitches uses the compact per-drum
        layout even when map_drums=False.

        ``map_drums`` only controls whether below-range melodic notes route
        into a kick drum — an existing drum rail must still use the compact
        per-used-drum cells.  Regression: without this, a large drum
        ``ticks_per_page`` overflowed the LUT signal pool when map_drums=False.
        """
        lb = build_audio_decoder_logical(
            name="Drum Test",
            instrument="drum",
            map_drums=False,
            active_drum_pitches={1, 2, 5},
            ticks_per_page=303,
        )
        speakers = sorted(
            (e for e in lb.entities.values() if e.type == "programmable-speaker"),
            key=lambda e: e.entity_id,
        )
        # 3 used drum types → 3 raw cells, 3 speakers, no unpackers
        assert [e.properties["note"] for e in speakers] == [
            "kick-2", "snare-1", "hat-1",
        ]
        acs = [e for e in lb.entities.values() if e.type == "arithmetic-combinator"]
        assert len(acs) == 4  # mod + 3 selectors (no unpackers)

    def test_compact_drum_three_types(self):
        """Three used drums use raw cells: 3 speakers, no unpackers."""
        lb = build_audio_decoder_logical(
            name="Drum Test",
            instrument="drum",
            map_drums=True,
            active_drum_pitches={0, 2, 5},
        )
        speakers = sorted(
            (e for e in lb.entities.values() if e.type == "programmable-speaker"),
            key=lambda e: e.entity_id,
        )
        assert [e.properties["note"] for e in speakers] == [
            "kick-1", "snare-1", "hat-1",
        ]
        # 3 raw channels (LUT+match+sel each) + mod + port, no unpackers
        acs = [e for e in lb.entities.values() if e.type == "arithmetic-combinator"]
        assert len(acs) == 4  # mod + 3 selectors
        assert len(lb.entities) == 14  # 3*(LUT+match+sel+spk) + port + mod

    def test_player_connector_wired_into_clock_and_data(self):
        """Split-mode connector block: the bottom-left block is ``CCA>``
        (``conn`` + ``conn_label`` CCs + east-facing mod AC on the CONN_Y
        row).  There is NO separate page port — ``conn`` rides BOTH the
        green clock (time) bus and the red data bus; the ``conn_label``
        marker CC is isolated (never wired)."""
        from factorio_display.logical_blueprint import Endpoint, to_draftsman

        lb = build_audio_decoder_logical(name="Conn", instrument="piano", connectors=True)
        # No page_port — the block is conn (data/clock entry) + marker + mod.
        ids = {e.entity_id for e in lb.entities.values()}
        assert "page_port" not in ids
        conn = lb.entities["conn"]
        assert conn.type == "constant-combinator"
        assert conn.position == (0, 25)
        assert lb.entities["conn_label"].position == (1, 25)
        # The mod AC sits east-facing at (2, 25) in the same connector block.
        mod = next(e for e in lb.entities.values() if e.entity_id == "mod")
        assert mod.position == (2, 25)
        assert mod.direction == 4  # east-facing pre-rotation

        green_ok = red_ok = False
        for net in lb.networks:
            has_conn = Endpoint("conn", "input") in net.endpoints
            if net.color == "green" and has_conn:
                green_ok = True
            if net.color == "red" and has_conn:
                red_ok = True
        assert green_ok, "conn must join the green clock (time) bus"
        assert red_ok, "conn must join the red data bus"

        # Marker CC must NOT be wired into any network.
        for net in lb.networks:
            assert Endpoint("conn_label", "input") not in net.endpoints

        # Materialises to a valid blueprint with the connector present.
        bp = to_draftsman(lb)
        assert bp.to_string().startswith("0eN")


# ── display builder tests ─────────────────────────────────────────────

class TestBuildDisplayLogical:
    """Tests for ``build_display_logical`` — the video lamp-grid builder."""

    def test_normal_display(self):
        """A display within pool limits creates a single lamp grid."""
        lb = build_display_logical(name="Test", width=10, height=6)
        lamps = [e for e in lb.entities.values() if e.type == "small-lamp"]
        assert len(lamps) == 60  # 10×6

        # Should have exactly one "data" input port
        data_ports = [p for p, n in lb.input_ports.items() if p == "data"]
        assert len(data_ports) == 1

    def test_large_display_chunks(self):
        """A display exceeding the signal pool is split into vertical chunks."""
        # 62×35 requires 434 base signals; SIGNAL_POOL has 182
        lb = build_display_logical(name="Large", width=62, height=35)

        lamps = [e for e in lb.entities.values() if e.type == "small-lamp"]
        assert len(lamps) == 62 * 35  # all pixels present

        # Should have multiple "data" input ports (one per chunk)
        data_ports = [p for p, n in lb.input_ports.items() if p.startswith("data")]
        assert len(data_ports) >= 2, f"Expected chunked data ports, got {data_ports}"

    def test_large_display_chunked_networks_independent(self):
        """Each chunk's lamp network is independent (no cross-chunk wiring)."""
        lb = build_display_logical(name="Independent", width=62, height=35)

        # Collect lamp IDs per chunk by checking which data port they're on
        data_ports = sorted(
            [p for p in lb.input_ports if p.startswith("data")],
            key=lambda p: int(p.split("_")[1]) if "_" in p else 0,
        )

        for port_name in data_ports:
            net_id = lb.input_ports[port_name]
            net = next(n for n in lb.networks if n.network_id == net_id)
            lamp_eps = [ep for ep in net.endpoints if "lamp" in ep.entity_id]
            assert len(lamp_eps) > 0, f"Port {port_name} has no lamps"

    def test_large_display_chunk_positions_non_overlapping(self):
        """Each chunk's lamps occupy distinct Y-ranges."""
        lb = build_display_logical(name="Positions", width=62, height=35)
        lamps = [e for e in lb.entities.values() if e.type == "small-lamp"]
        positions = [(e.position[0], e.position[1]) for e in lamps]
        # All positions should be unique
        assert len(positions) == len(set(positions)), "Duplicate lamp positions"

    def test_large_display_connectors_wired(self):
        """Split-mode connector CCs: a wired data connector + isolated label per chunk."""
        from factorio_display.logical_blueprint import to_draftsman

        lb = build_display_logical(name="Connectors", width=62, height=35, connectors=True)

        data_ports = sorted(
            [p for p in lb.input_ports if p.startswith("data")],
            key=lambda p: int(p.split("_")[1]) if "_" in p else 0,
        )
        assert len(data_ports) >= 2, "expected a chunked display"

        for ci, port_name in enumerate(data_ports):
            cc_id = f"cc_c{ci}_data"
            label_id = f"cc_c{ci}_label"
            assert cc_id in lb.entities, f"missing connector CC {cc_id}"
            assert label_id in lb.entities, f"missing label CC {label_id}"
            # Connector must be on the chunk's red network (the data bus).
            net = next(n for n in lb.networks if n.network_id == lb.input_ports[port_name])
            assert any(ep.entity_id == cc_id for ep in net.endpoints), (
                f"connector {cc_id} not on the data bus"
            )
            # Label must NOT be wired anywhere.
            label_in_any_net = any(
                any(ep.entity_id == label_id for ep in n.endpoints)
                for n in lb.networks
            )
            assert not label_in_any_net, f"label {label_id} must be isolated"

        # Serialisation must succeed (fast path).
        bp = to_draftsman(lb)
        assert bp is not None
        assert len(bp.entities) == len(lb.entities)

    def test_display_connector_wired_within_reach(self):
        """The per-chunk display connector must be wired to a *nearby* lamp
        (within Factorio's 9-tile circuit-wire reach).

        Regression: the connector sits at x=ch_w (right of the top row) but
        used to be wired to the top-LEFT lamp (x=0) — a wire spanning the
        whole chunk width that Factorio silently drops, leaving the connector
        disconnected in-game.
        """
        from factorio_display.logical_blueprint import to_draftsman

        lb = build_display_logical(name="Reach", width=62, height=35, connectors=True)
        bp = to_draftsman(lb)

        pos = {
            e.id: (int(e.tile_position.x), int(e.tile_position.y))
            for e in bp.entities
            if getattr(e, "id", None) and hasattr(e, "tile_position")
        }
        conn_ids = {
            eid for eid, e in lb.entities.items()
            if e.type == "constant-combinator"
            and eid.startswith("cc_c") and eid.endswith("_data")
        }
        assert len(conn_ids) >= 2, "expected per-chunk display connectors"

        too_far: list[str] = []
        for w in bp.wires:
            e1 = w[0]()
            e2 = w[2]()
            id1, id2 = getattr(e1, "id", None), getattr(e2, "id", None)
            if not (id1 in conn_ids or id2 in conn_ids):
                continue
            p1, p2 = pos.get(id1), pos.get(id2)
            if p1 is None or p2 is None:
                continue
            d = max(abs(p1[0] - p2[0]), abs(p1[1] - p2[1]))
            if d > 9:
                too_far.append(f"{id1} ↔ {id2} ({d} tiles)")
        assert not too_far, (
            "display connector wires exceed Factorio's 9-tile reach:\n"
            + "\n".join(too_far)
        )
