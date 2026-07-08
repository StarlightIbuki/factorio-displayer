"""Unit tests for encoder.py — audio memory encoding pipeline."""

from __future__ import annotations

import pytest

from factorio_display.audio.encoder import (
    CELLS_PER_PAGE,
    CHANNELS_PER_TICK,
    TICKS_PER_PAGE,
    compute_page_layout,
    encode_audio_memory,
    encode_audio_to_logical,
    flatten_packed,
    loudness_to_packed,
    pack_four,
    unpack_four,
)
from factorio_display.audio.pitch_mapping import SPEAKER_COUNT


# ── pack / unpack ──────────────────────────────────────────────────────

class TestPackUnpack:
    def test_zeros(self):
        assert pack_four(0, 0, 0, 0) == 0
        assert unpack_four(0) == (0, 0, 0, 0)

    def test_max_values(self):
        """100 is the max loudness — fits in 7 bits (< 128)."""
        packed = pack_four(100, 100, 100, 100)
        assert unpack_four(packed) == (100, 100, 100, 100)

    def test_roundtrip_various(self):
        cases = [
            (0, 0, 0, 1),
            (0, 0, 1, 0),
            (0, 1, 0, 0),
            (1, 0, 0, 0),
            (50, 30, 80, 10),
            (99, 99, 99, 99),
            (0, 0, 0, 100),
            (100, 0, 0, 0),
            (100, 100, 100, 100),
        ]
        for l1, l2, l3, l4 in cases:
            packed = pack_four(l1, l2, l3, l4)
            result = unpack_four(packed)
            assert result == (l1, l2, l3, l4), f"Failed: {l1, l2, l3, l4} → {result}"

    def test_packing_order(self):
        """l1 occupies the highest bits (shift 21)."""
        # l1 << 21
        assert pack_four(5, 0, 0, 0) == 5 << 21
        # l2 << 14
        assert pack_four(0, 7, 0, 0) == 7 << 14
        # l3 << 7
        assert pack_four(0, 0, 3, 0) == 3 << 7
        # l4 is in the lowest bits
        assert pack_four(0, 0, 0, 9) == 9

    def test_packed_value_bounds(self):
        """Max packed = (100<<21)|(100<<14)|(100<<7)|100 = 210554060."""
        max_val = pack_four(100, 100, 100, 100)
        expected = (100 << 21) | (100 << 14) | (100 << 7) | 100
        assert max_val == expected
        # Fits in 32-bit signed int
        assert max_val < 2 ** 31

    def test_unpack_large_values_dont_overflow(self):
        """Values up to 127 round-trip correctly (7-bit fields)."""
        # 127 is the max for a 7-bit field
        packed = pack_four(127, 127, 127, 127)
        assert unpack_four(packed) == (127, 127, 127, 127)
        # 128 is too large for 7 bits — gets masked to 0
        packed_128 = pack_four(128, 0, 0, 0)
        # 128 << 21, masked on unpack to (128 & 127) = 0
        assert packed_128 == 128 << 21
        assert unpack_four(packed_128) == (0, 0, 0, 0)


# ── loudness_to_packed ─────────────────────────────────────────────────

class TestLoudnessToPacked:
    def test_one_silent_tick(self):
        data = [[0] * SPEAKER_COUNT]
        result = loudness_to_packed(data)
        assert len(result) == 1
        assert result[0] == [0] * CHANNELS_PER_TICK

    def test_one_full_volume_tick(self):
        data = [[100] * SPEAKER_COUNT]
        result = loudness_to_packed(data)
        assert len(result) == 1
        # Each packed int = (100<<21)|(100<<14)|(100<<7)|100
        expected = pack_four(100, 100, 100, 100)
        assert all(v == expected for v in result[0])

    def test_wrong_length_raises(self):
        with pytest.raises(ValueError, match="Expected 48"):
            loudness_to_packed([[1, 2, 3]])

    def test_multiple_ticks(self):
        data = [
            [i % 101 for i in range(SPEAKER_COUNT)] for _ in range(5)
        ]
        result = loudness_to_packed(data)
        assert len(result) == 5
        assert all(len(t) == CHANNELS_PER_TICK for t in result)


# ── flatten_packed ─────────────────────────────────────────────────────

class TestFlattenPacked:
    def test_empty(self):
        assert not flatten_packed([])

    def test_one_tick(self):
        packed = [[10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120]]
        flat = flatten_packed(packed)
        assert flat == [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120]

    def test_flatten_index_formula(self):
        """Verify flat[tick * 12 + offset] == packed[tick][offset]."""
        packed = [
            [t * 100 + o for o in range(CHANNELS_PER_TICK)]
            for t in range(4)
        ]
        flat = flatten_packed(packed)
        for t in range(4):
            for o in range(CHANNELS_PER_TICK):
                assert flat[t * CHANNELS_PER_TICK + o] == packed[t][o]


# ── page layout ────────────────────────────────────────────────────────

class TestComputePageLayout:
    def test_exact_fill(self):
        """720 cells = exactly 1 page."""
        pc, cpp, _ = compute_page_layout(720, 200, 5)
        assert pc == 1
        assert cpp == CELLS_PER_PAGE  # always 720
        assert _ == TICKS_PER_PAGE  # always 60

    def test_partial_page(self):
        """100 cells still needs 1 page."""
        pc, cpp, _ = compute_page_layout(100, 200, 5)
        assert pc == 1
        assert cpp == CELLS_PER_PAGE

    def test_two_pages(self):
        """721 cells needs 2 pages."""
        pc, cpp, _ = compute_page_layout(721, 200, 5)
        assert pc == 2
        assert cpp == CELLS_PER_PAGE

    def test_zero_cells(self):
        pc, cpp, _ = compute_page_layout(0, 200, 5)
        assert pc == 0
        assert cpp == CELLS_PER_PAGE

    def test_many_pages(self):
        pc, cpp, _ = compute_page_layout(7200, 200, 5)
        assert pc == 10
        assert cpp == CELLS_PER_PAGE


class TestTicksPerPage:
    def test_constant(self):
        """TICKS_PER_PAGE is the constant 60 used by decoder."""
        assert TICKS_PER_PAGE == 60

    def test_cells_per_page(self):
        """60 ticks × 12 channels = 720 cells."""
        assert CELLS_PER_PAGE == 720


# ── encode_audio_memory integration ────────────────────────────────────

class TestEncodeAudioMemory:
    @pytest.fixture(autouse=True)
    def _pool_and_qual(self, large_signal_pool, sample_qualities):  # pylint: disable=attribute-defined-outside-init
        """Inject signal pool and qualities as instance attributes."""
        self.pool = large_signal_pool
        self.qual = sample_qualities

    def test_empty_data_returns_empty_string(self):
        result = encode_audio_memory([], "test", self.pool, self.qual)
        assert result == ""

    def test_single_silent_tick(self):
        data = [[0] * SPEAKER_COUNT]
        result = encode_audio_memory(data, "test", self.pool, self.qual)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_blueprint_is_valid_string(self):
        data = [
            [(t + i) % 101 for i in range(SPEAKER_COUNT)]
            for t in range(20)
        ]
        bp = encode_audio_memory(data, "Test", self.pool, self.qual)
        assert bp.startswith("0e")
        from draftsman.blueprintable import Blueprint  # pylint: disable=import-outside-toplevel
        parsed = Blueprint.from_string(bp)
        assert parsed.label == "Audio Memory: Test"
        entities = parsed.entities
        dc_count = sum(1 for e in entities if "decider-combinator" in e.name)
        # 20 ticks fit in 1 page (60 ticks/page)
        assert dc_count == 1

        # ── logical-blueprint validation ──────────────────────────
        from conftest import validate_blueprint_via_logical  # pylint: disable=import-outside-toplevel
        result = validate_blueprint_via_logical(bp, require_wiring=False)
        assert result["errors"] == [], (
            f"Logical-blueprint validation failed: {result['errors']}"
        )
        assert result["entity_count"] == dc_count

    def test_page_count(self):
        """50 ticks fits in 1 page; 70 ticks needs 2 pages."""
        from conftest import validate_blueprint_via_logical  # pylint: disable=import-outside-toplevel

        # 50 ticks → 1 page
        data_50 = [[(t + i) % 101 for i in range(SPEAKER_COUNT)] for t in range(50)]
        bp = encode_audio_memory(data_50, "Test", self.pool, self.qual)
        from draftsman.blueprintable import Blueprint  # pylint: disable=import-outside-toplevel
        parsed = Blueprint.from_string(bp)
        dc_count = sum(1 for e in parsed.entities if "decider-combinator" in e.name)
        assert dc_count == 1  # 50 < 60

        result = validate_blueprint_via_logical(bp, require_wiring=False)
        assert result["errors"] == [], f"Validation errors: {result['errors']}"
        assert result["entity_count"] == 1

        # 70 ticks → 2 pages
        data_70 = [[(t + i) % 101 for i in range(SPEAKER_COUNT)] for t in range(70)]
        bp2 = encode_audio_memory(data_70, "Test", self.pool, self.qual)
        parsed2 = Blueprint.from_string(bp2)
        dc_count2 = sum(1 for e in parsed2.entities if "decider-combinator" in e.name)
        assert dc_count2 == 2  # ceil(70/60) = 2

        result2 = validate_blueprint_via_logical(bp2)
        assert result2["errors"] == [], f"Validation errors: {result2['errors']}"

    def test_no_unexpected_warnings(self):
        """Encoding audio memory should not produce unexpected warnings."""
        import warnings
        from conftest import assert_no_unexpected_warnings  # pylint: disable=import-outside-toplevel

        data = [
            [(t + i) % 101 for i in range(SPEAKER_COUNT)]
            for t in range(10)
        ]
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            encode_audio_memory(data, "Test", self.pool, self.qual)
        assert_no_unexpected_warnings(w)


class TestEncodeAudioToLogical:
    @pytest.fixture(autouse=True)
    def _pool_and_qual(self, large_signal_pool, sample_qualities):  # pylint: disable=attribute-defined-outside-init
        self.pool = large_signal_pool
        self.qual = sample_qualities

    def test_square_positions_and_prewired_buses(self):
        # 130 ticks -> 1560 cells -> 3 pages, all non-silent.
        data = [[(i % 100) + 1 for i in range(SPEAKER_COUNT)] for _ in range(130)]
        lb = encode_audio_to_logical(data, "TestLogical", self.pool, self.qual)

        dcs = [e for e in lb.entities.values() if e.type == "decider-combinator"]
        assert len(dcs) == 3
        xs = [e.position[0] for e in dcs if e.position is not None]
        ys = [e.position[1] for e in dcs if e.position is not None]
        assert min(xs) == 0 and max(xs) <= 1
        assert min(ys) == 0 and max(ys) <= 2

        green_nets = [n for n in lb.networks if n.color == "green"]
        red_nets = [n for n in lb.networks if n.color == "red"]
        assert green_nets and red_nets
        assert any(n.prewired_pairs is not None for n in green_nets)
        assert any(n.prewired_pairs is not None for n in red_nets)

    def test_declares_clock_and_data_ports(self):
        data = [[(i % 100) + 1 for i in range(SPEAKER_COUNT)] for _ in range(130)]
        lb = encode_audio_to_logical(data, "TestLogicalPorts", self.pool, self.qual)

        assert "clock" in lb.input_ports
        assert "data" in lb.output_ports

        clock_net = next(n for n in lb.networks if n.network_id == lb.input_ports["clock"])
        data_net = next(n for n in lb.networks if n.network_id == lb.output_ports["data"])
        assert clock_net.color == "green"
        assert data_net.color == "red"
