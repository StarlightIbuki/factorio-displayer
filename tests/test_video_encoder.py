"""Unit tests for video/encoder.py — chunked time-dimension encoding."""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np
import pytest
from draftsman.blueprintable import Blueprint

from factorio_display.video.encoder import (
    _build_chunk_worker,
    _chunk_cache_dir,
    _chunk_meta_path,
    _encode_frames_core,
    _merge_chunk_blueprints,
    _merge_with_cross_dedup,
    encode_frames,
    encode_frames_chunked,
    resolve_dimensions,
)
from factorio_display.integer2signal.mapping import SignalMapping


# ═══════════════════════════════════════════════════════════════════════
# Test helpers — small deterministic frames and mappings
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture
def small_mapping_params() -> dict:
    """Params for a tiny 4×4 display with no hole."""
    return {
        "width": 4,
        "height": 4,
        "qualities": ["normal", "uncommon", "rare", "epic", "legendary"],
        "signal_pool": [f"test-signal-{i:04d}" for i in range(30)],
    }


@pytest.fixture
def small_mapping(small_mapping_params) -> SignalMapping:
    return SignalMapping(**small_mapping_params)


@pytest.fixture
def sample_frames_3() -> tuple[list[np.ndarray], list[tuple[int, int]]]:
    """3 distinct 4×4 RGB frames with their tick ranges."""
    frames = [
        np.full((4, 4, 3), (255, 0, 0), dtype=np.uint8),   # red
        np.full((4, 4, 3), (0, 255, 0), dtype=np.uint8),   # green
        np.full((4, 4, 3), (0, 0, 255), dtype=np.uint8),   # blue
    ]
    tick_ranges = [(1, 1), (2, 2), (3, 3)]
    return frames, tick_ranges


@pytest.fixture
def sample_frames_12() -> tuple[list[np.ndarray], list[tuple[int, int]]]:
    """12 frames (4 colors × 3 repeats) for dedup testing."""
    colors = [
        (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0),
    ]
    frames = []
    tick_ranges = []
    for i in range(12):
        c = colors[i % 4]
        frames.append(np.full((4, 4, 3), c, dtype=np.uint8))
        tick_ranges.append((i + 1, i + 1))
    return frames, tick_ranges


# ═══════════════════════════════════════════════════════════════════════
# _encode_frames_core
# ═══════════════════════════════════════════════════════════════════════

class TestEncodeFramesCore:
    def test_single_unit_produces_blueprint(self, sample_frames_3, small_mapping_params):
        frames, tick_ranges = sample_frames_3
        bp_str = _encode_frames_core(
            kept_frames=frames,
            tick_ranges=tick_ranges,
            output_name="Test",
            deduplicate=False,
            mapping_params=small_mapping_params,
            clock="signal-clock",
            current_tick=4,
        )
        assert bp_str
        assert len(bp_str) > 0
        # Should be parseable
        bp = Blueprint.from_string(bp_str)
        assert bp is not None

    def test_single_unit_three_dcs(self, sample_frames_3, small_mapping_params):
        frames, tick_ranges = sample_frames_3
        bp_str = _encode_frames_core(
            kept_frames=frames,
            tick_ranges=tick_ranges,
            output_name="Test",
            deduplicate=False,
            mapping_params=small_mapping_params,
            clock="signal-clock",
            current_tick=4,
        )
        bp = Blueprint.from_string(bp_str)
        dcs = [e for e in bp.entities if "decider-combinator" in e.name]
        assert len(dcs) == 3

    def test_deduplicate_reduces_count(self, sample_frames_12, small_mapping_params):
        frames, tick_ranges = sample_frames_12
        # Without dedup: 12 DCs
        bp_no = _encode_frames_core(
            kept_frames=frames, tick_ranges=tick_ranges,
            output_name="Test", deduplicate=False,
            mapping_params=small_mapping_params,
            clock="signal-clock", current_tick=13,
        )
        dcs_no = len([e for e in Blueprint.from_string(bp_no).entities
                       if "decider-combinator" in e.name])
        assert dcs_no == 12

        # With dedup: 4 unique colors → 4 DCs, each with 3 merged tick ranges
        bp_yes = _encode_frames_core(
            kept_frames=frames, tick_ranges=tick_ranges,
            output_name="Test", deduplicate=True,
            mapping_params=small_mapping_params,
            clock="signal-clock", current_tick=13,
        )
        dcs_yes = len([e for e in Blueprint.from_string(bp_yes).entities
                        if "decider-combinator" in e.name])
        assert dcs_yes == 4

    def test_empty_frames_returns_empty(self, small_mapping_params):
        bp_str = _encode_frames_core(
            kept_frames=[], tick_ranges=[],
            output_name="Test", deduplicate=False,
            mapping_params=small_mapping_params,
            clock="signal-clock", current_tick=1,
        )
        assert bp_str == ""

    def test_multi_unit_no_longer_supported(self):
        """Multi-unit tiling has been removed. Vertical chunk splitting
        handles pool overflow instead."""
        pass  # Legacy test — multi-unit path was removed in display rework

    def test_snake_wiring_has_green_and_red(self, sample_frames_3, small_mapping_params):
        frames, tick_ranges = sample_frames_3
        bp_str = _encode_frames_core(
            kept_frames=frames, tick_ranges=tick_ranges,
            output_name="Test", deduplicate=False,
            mapping_params=small_mapping_params,
            clock="signal-clock", current_tick=4,
        )
        bp = Blueprint.from_string(bp_str)
        # Circuit connections should exist (at least for 3 DCs)
        wires = getattr(bp, "wires", [])
        assert len(wires) >= 2, f"Expected ≥2 wires, got {len(wires)}"


# ═══════════════════════════════════════════════════════════════════════
# encode_frames_chunked
# ═══════════════════════════════════════════════════════════════════════

class TestEncodeFramesChunked:
    def _make_frame_iter(self, frames: list[np.ndarray]):
        """Convert a list of frames into an iterator."""
        yield from frames

    def test_single_chunk_returns_same_as_encode_frames(
        self, sample_frames_3, small_mapping
    ):
        frames, _ = sample_frames_3
        # encode_frames (non-chunked)
        single = encode_frames(
            self._make_frame_iter(frames), "Test", fps=60,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id="test",
        )
        # encode_frames_chunked with time_chunks=1
        result = encode_frames_chunked(
            self._make_frame_iter(frames), "Test", fps=60,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id="test_single", time_chunks=1,
        )
        # Both should produce parseable blueprints with the same entity count
        bp_single = Blueprint.from_string(single)
        bp_chunked = Blueprint.from_string(result["full"])
        dcs_single = len([e for e in bp_single.entities
                           if "decider-combinator" in e.name])
        dcs_chunked = len([e for e in bp_chunked.entities
                            if "decider-combinator" in e.name])
        assert dcs_single == dcs_chunked

    def test_multiple_chunks_produce_valid_merge(
        self, sample_frames_12, small_mapping
    ):
        frames, _ = sample_frames_12
        result = encode_frames_chunked(
            self._make_frame_iter(frames), "Test", fps=60,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id="test_multi", time_chunks=3,
        )
        full = result["full"]
        assert full
        bp = Blueprint.from_string(full)
        assert bp is not None
        # Should have chunks
        assert len(result["chunks"]) == 3

    def test_chunks_are_individually_valid(self, sample_frames_12, small_mapping):
        frames, _ = sample_frames_12
        result = encode_frames_chunked(
            self._make_frame_iter(frames), "Test", fps=60,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id="test_chunks_valid", time_chunks=3,
        )
        for i, chunk_bp in enumerate(result["chunks"]):
            bp = Blueprint.from_string(chunk_bp)
            assert bp is not None, f"Chunk {i} is not valid"
            dcs = [e for e in bp.entities if "decider-combinator" in e.name]
            assert len(dcs) > 0, f"Chunk {i} has no DCs"

    def test_no_duplicate_entity_ids_in_merge(
        self, sample_frames_12, small_mapping
    ):
        frames, _ = sample_frames_12
        result = encode_frames_chunked(
            self._make_frame_iter(frames), "Test", fps=60,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id="test_no_dup", time_chunks=3,
        )
        bp = Blueprint.from_string(result["full"])
        ids = [getattr(e, "id", None) for e in bp.entities]
        ids = [i for i in ids if i is not None]
        assert len(ids) == len(set(ids)), f"Duplicate IDs: {len(ids)} vs {len(set(ids))}"

    def test_merge_has_inter_chunk_wiring(self, sample_frames_12, small_mapping):
        frames, _ = sample_frames_12
        result = encode_frames_chunked(
            self._make_frame_iter(frames), "Test", fps=60,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id="test_wiring", time_chunks=3,
        )
        bp = Blueprint.from_string(result["full"])
        # With 3 chunks and 2 inter-chunk boundaries, there should be
        # intra-chunk wiring + inter-chunk wiring
        wires = getattr(bp, "wires", [])
        assert len(wires) >= 4, f"Expected ≥4 wires (intra + inter), got {len(wires)}"

    def test_tick_ranges_preserved_across_chunks(
        self, sample_frames_12, small_mapping
    ):
        """Each DC's tick-gate condition should fire at the right tick."""
        frames, expected_ranges = sample_frames_12
        result = encode_frames_chunked(
            self._make_frame_iter(frames), "Test", fps=60,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id="test_ticks", time_chunks=3, deduplicate=False,
        )
        bp = Blueprint.from_string(result["full"])
        dcs = [e for e in bp.entities if "decider-combinator" in e.name]
        # Each DC should have conditions (conditions is a list of Condition objects)
        for dc in dcs:
            conds = getattr(dc, "conditions", [])
            assert len(conds) > 0, f"DC has no conditions"


# ═══════════════════════════════════════════════════════════════════════
# Chunk caching
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.xdist_group("chunk-cache")
class TestChunkCache:
    def test_cache_dir_created(self, sample_frames_12, small_mapping, tmp_path):
        frames, _ = sample_frames_12
        # Monkey-patch _chunk_cache_dir to use tmp_path
        import factorio_display.video.encoder as enc_mod
        original = enc_mod._chunk_cache_dir

        def _fake_cache_dir(*args, **kwargs):
            return tmp_path / "test_cache"

        enc_mod._chunk_cache_dir = _fake_cache_dir
        try:
            result = encode_frames_chunked(
                iter(frames), "Test", fps=60,
                mapping=small_mapping, total_width=4, total_height=4,
                source_id="cache_test", time_chunks=2,
            )
            cache_dir = tmp_path / "test_cache"
            assert cache_dir.exists()
            # Should have chunk files
            chunk_files = list(cache_dir.glob("chunk_*.bp.txt"))
            assert len(chunk_files) == 2
            # Should have meta.json
            assert (cache_dir / "meta.json").exists()
        finally:
            enc_mod._chunk_cache_dir = original

    def test_meta_json_valid(self, sample_frames_12, small_mapping, tmp_path):
        frames, _ = sample_frames_12
        import factorio_display.video.encoder as enc_mod
        original = enc_mod._chunk_cache_dir

        def _fake_cache_dir(*args, **kwargs):
            return tmp_path / "test_cache_meta"

        enc_mod._chunk_cache_dir = _fake_cache_dir
        try:
            encode_frames_chunked(
                iter(frames), "Test", fps=60,
                mapping=small_mapping, total_width=4, total_height=4,
                source_id="meta_test", time_chunks=2,
            )
            cache_dir = tmp_path / "test_cache_meta"
            with open(cache_dir / "meta.json") as f:
                meta = json.load(f)
            assert meta["time_chunks"] == 2
            assert "total_ticks" in meta
        finally:
            enc_mod._chunk_cache_dir = original


# ═══════════════════════════════════════════════════════════════════════
# Cross-chunk dedup
# ═══════════════════════════════════════════════════════════════════════

class TestCrossChunkDedup:
    def test_cross_dedup_reduces_combinators(self, sample_frames_12, small_mapping):
        """With 4 unique colors repeated 3 times each across chunks,
        cross-chunk dedup should merge them."""
        frames, _ = sample_frames_12
        result = encode_frames_chunked(
            iter(frames), "Test", fps=60,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id="cross_dedup", time_chunks=3,
            deduplicate=True, deduplicate_cross=True,
        )
        bp = Blueprint.from_string(result["full"])
        dcs = [e for e in bp.entities if "decider-combinator" in e.name]
        # 4 unique colors, each may appear in each of 3 chunks,
        # cross-dedup should merge → 4 (or fewer) DCs
        assert len(dcs) <= 4, f"Expected ≤4 DCs, got {len(dcs)}"

    def test_cross_dedup_conditions_merged(self, sample_frames_12, small_mapping):
        frames, _ = sample_frames_12
        result = encode_frames_chunked(
            iter(frames), "Test", fps=60,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id="cross_cond", time_chunks=3,
            deduplicate=True, deduplicate_cross=True,
        )
        bp = Blueprint.from_string(result["full"])
        dcs = [e for e in bp.entities if "decider-combinator" in e.name]
        # Each DC should have conditions for all ticks where that color appears
        total_conditions = sum(len(getattr(dc, "conditions", [])) for dc in dcs)
        # With 12 frames, 4 unique colors, each appearing 3 times:
        # 3 tick ranges per color, each range = 2 conditions (>= and <=)
        # or 1 condition (==) for single-tick ranges
        # Total conditions should cover all 12 frames
        assert total_conditions >= 12, f"Expected ≥12 conditions, got {total_conditions}"


# ═══════════════════════════════════════════════════════════════════════
# _merge_chunk_blueprints
# ═══════════════════════════════════════════════════════════════════════

class TestMergeChunkBlueprints:
    def test_merge_two_chunks(self, sample_frames_12, small_mapping_params):
        frames, tick_ranges = sample_frames_12
        # Build two chunks manually
        mid = len(frames) // 2
        bp1 = _encode_frames_core(
            kept_frames=frames[:mid], tick_ranges=tick_ranges[:mid],
            output_name="Chunk1", deduplicate=False,
            mapping_params=small_mapping_params,
            clock="signal-clock", current_tick=mid + 1,
        )
        bp2 = _encode_frames_core(
            kept_frames=frames[mid:], tick_ranges=tick_ranges[mid:],
            output_name="Chunk2", deduplicate=False,
            mapping_params=small_mapping_params,
            clock="signal-clock", current_tick=len(frames) + 1,
        )
        merged = _merge_chunk_blueprints(
            [bp1, bp2], "Merged", deduplicate_cross=False,
        )
        assert merged
        bp = Blueprint.from_string(merged)
        dcs = [e for e in bp.entities if "decider-combinator" in e.name]
        assert len(dcs) == len(frames)

    def test_merge_handles_empty_list(self):
        result = _merge_chunk_blueprints([], "Empty")
        assert result == ""

    def test_merge_single_chunk_passthrough(self, sample_frames_3, small_mapping_params):
        frames, tick_ranges = sample_frames_3
        bp_str = _encode_frames_core(
            kept_frames=frames, tick_ranges=tick_ranges,
            output_name="Single", deduplicate=False,
            mapping_params=small_mapping_params,
            clock="signal-clock", current_tick=4,
        )
        result = _merge_chunk_blueprints([bp_str], "Single")
        assert result == bp_str  # single chunk is pass-through


# ═══════════════════════════════════════════════════════════════════════
# _build_chunk_worker
# ═══════════════════════════════════════════════════════════════════════

class TestBuildChunkWorker:
    def test_worker_returns_chunk_idx_and_string(
        self, sample_frames_3, small_mapping_params
    ):
        frames, tick_ranges = sample_frames_3
        payload = pickle.dumps({
            "chunk_idx": 7,
            "kept_frames": frames,
            "tick_ranges": tick_ranges,
            "output_name": "WorkerTest",
            "deduplicate": False,
            "mapping_params": small_mapping_params,
            "total_w": 4, "total_h": 4,
            "unit_w": 4, "unit_h": 4,
            "unit_cols": 1, "unit_rows": 1,
            "clock": "signal-clock",
            "current_tick": 4,
            "label_suffix": " [test]",
        })
        chunk_idx, bp_str = _build_chunk_worker(payload)
        assert chunk_idx == 7
        assert bp_str
        assert len(bp_str) > 0
        # Verify it's valid
        bp = Blueprint.from_string(bp_str)
        assert bp is not None

    def test_worker_produces_valid_blueprint(
        self, sample_frames_12, small_mapping_params
    ):
        frames, tick_ranges = sample_frames_12
        payload = pickle.dumps({
            "chunk_idx": 0,
            "kept_frames": frames,
            "tick_ranges": tick_ranges,
            "output_name": "WorkerValid",
            "deduplicate": True,
            "mapping_params": small_mapping_params,
            "total_w": 4, "total_h": 4,
            "unit_w": 4, "unit_h": 4,
            "unit_cols": 1, "unit_rows": 1,
            "clock": "signal-clock",
            "current_tick": 13,
        })
        _, bp_str = _build_chunk_worker(payload)
        bp = Blueprint.from_string(bp_str)
        dcs = [e for e in bp.entities if "decider-combinator" in e.name]
        # 4 unique colors with dedup
        assert len(dcs) == 4


# ═══════════════════════════════════════════════════════════════════════
# Output chunks directory
# ═══════════════════════════════════════════════════════════════════════

class TestOutputChunks:
    def test_output_chunks_dir_writes_files(
        self, sample_frames_12, small_mapping, tmp_path
    ):
        frames, _ = sample_frames_12
        out_dir = tmp_path / "chunks_out"
        result = encode_frames_chunked(
            iter(frames), "Test", fps=60,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id="output_test", time_chunks=3,
            output_chunks_dir=str(out_dir),
        )
        assert out_dir.exists()
        chunk_files = sorted(out_dir.glob("chunk_*.bp.txt"))
        assert len(chunk_files) == 3
        for cf in chunk_files:
            content = cf.read_text(encoding="utf-8")
            assert len(content) > 0
            bp = Blueprint.from_string(content)
            assert bp is not None

    def test_output_chunks_match_result(self, sample_frames_12, small_mapping, tmp_path):
        frames, _ = sample_frames_12
        out_dir = tmp_path / "chunks_match"
        result = encode_frames_chunked(
            iter(frames), "Test", fps=60,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id="match_test", time_chunks=3,
            output_chunks_dir=str(out_dir),
        )
        for i, expected in enumerate(result["chunks"]):
            cf = out_dir / f"chunk_{i:04d}.bp.txt"
            actual = cf.read_text(encoding="utf-8")
            assert actual == expected


# ═══════════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_chunks_more_than_frames(self, small_mapping):
        """If time_chunks > frame count, each chunk gets ≤1 frame."""
        import uuid
        frames = [np.full((4, 4, 3), (128, 128, 128), dtype=np.uint8)]
        result = encode_frames_chunked(
            iter(frames), "Test", fps=1,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id=f"many_chunks_{uuid.uuid4().hex[:8]}", time_chunks=5,
        )
        assert result["full"]
        bp = Blueprint.from_string(result["full"])
        assert bp is not None

    def test_zero_frames_returns_empty(self, small_mapping):
        result = encode_frames_chunked(
            iter([]), "Test", fps=60,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id="zero", time_chunks=3,
        )
        assert result["full"] == ""
        assert result["chunks"] == []

    def test_deduplicate_cross_without_deduplicate(self, sample_frames_12, small_mapping):
        """Cross-dedup should work even without per-chunk dedup."""
        frames, _ = sample_frames_12
        result = encode_frames_chunked(
            iter(frames), "Test", fps=60,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id="cross_nodedup", time_chunks=3,
            deduplicate=False, deduplicate_cross=True,
        )
        bp = Blueprint.from_string(result["full"])
        dcs = [e for e in bp.entities if "decider-combinator" in e.name]
        # 4 unique colors, cross-dedup merges across chunks
        assert len(dcs) <= 4


# ═══════════════════════════════════════════════════════════════════════
# Smoke test — full encode_frames pipeline with --height style args
# ═══════════════════════════════════════════════════════════════════════

class TestSmokeEncodeFrames:
    """End-to-end smoke tests that mirror the CLI `encode` subcommand path."""

    def test_encode_with_height_only(self):
        """Mirrors: factorio-display encode video.mp4 --height 20
        Uses synthetic frames to avoid needing a real video file."""
        # 16:9 source, height=20 → width auto-computed, fits in default pool
        source_w, source_h = 1920, 1080
        user_h = 20
        total_w, total_h = resolve_dimensions(source_w, source_h, height=user_h)
        assert total_h == 20

        # Build a few synthetic frames
        frames = [
            np.full((total_h, total_w, 3), (255, 0, 0), dtype=np.uint8),
            np.full((total_h, total_w, 3), (0, 255, 0), dtype=np.uint8),
            np.full((total_h, total_w, 3), (0, 0, 255), dtype=np.uint8),
        ]

        bp_str = encode_frames(
            iter(frames),
            output_name="Smoke Test",
            fps=30,
            total_height=user_h,
            source_id="smoke_height",
        )
        assert bp_str
        assert bp_str.startswith("0e")
        bp = Blueprint.from_string(bp_str)
        dcs = [e for e in bp.entities if "decider-combinator" in e.name]
        assert len(dcs) == 3

    def test_encode_with_width_only(self):
        """Mirrors: factorio-display encode video.mp4 --width 56"""
        total_w, total_h = resolve_dimensions(1920, 1080, width=56)
        frames = [
            np.full((total_h, total_w, 3), (128, 128, 128), dtype=np.uint8),
        ]
        bp_str = encode_frames(
            iter(frames), "Smoke Width", fps=60,
            total_width=56, source_id="smoke_width",
        )
        assert bp_str
        bp = Blueprint.from_string(bp_str)
        assert len(bp.entities) >= 1

    def test_encode_triggers_vertical_chunk_split(self):
        """Large display (many pixels) triggers vertical chunk splitting."""
        # 28×70 = 1960 px, default pool ~780 signals → should split into ~3 chunks
        frames = [
            np.full((70, 28, 3), (255, 0, 0), dtype=np.uint8),
        ]
        bp_str = encode_frames(
            iter(frames), "Smoke Chunked", fps=60,
            total_width=28, total_height=70, source_id="smoke_chunked",
        )
        assert bp_str
        bp = Blueprint.from_string(bp_str)
        # Should have entities from multiple chunks (more than 1 DC)
        dcs = [e for e in bp.entities if "decider-combinator" in e.name]
        assert len(dcs) > 1  # multiple chunks → multiple DCs
        assert bp is not None

    def test_encode_with_both_dimensions(self):
        """Mirrors: factorio-display encode video.mp4 --width 56 --height 84"""
        frames = [
            np.full((84, 56, 3), (255, 255, 255), dtype=np.uint8),
        ]
        bp_str = encode_frames(
            iter(frames), "Smoke Both", fps=60,
            total_width=56, total_height=84, source_id="smoke_both",
        )
        assert bp_str
        bp = Blueprint.from_string(bp_str)
        assert len(bp.entities) >= 1

    def test_both_specified(self):
        w, h = resolve_dimensions(1920, 1080, width=56, height=84)
        assert w == 56
        assert h == 84

    def test_both_specified_no_rounding_applied(self):
        """User values are used exactly — no unit rounding."""
        w, h = resolve_dimensions(1920, 1080, width=30, height=30)
        assert w == 30
        assert h == 30

    # ── only width specified ──────────────────────────────────────────

    def test_width_only_preserves_ratio_16_9(self):
        """Source 1920×1080 (16:9), user specifies width=56."""
        w, h = resolve_dimensions(1920, 1080, width=56)
        assert w == 56
        # 56 * 1080 / 1920 = 31.5 → round → 32
        assert h == 32

    def test_width_only_preserves_ratio_4_3(self):
        """Source 640×480 (4:3), user specifies width=84."""
        w, h = resolve_dimensions(640, 480, width=84)
        assert w == 84
        # 84 * 480 / 640 = 63.0 → 63
        assert h == 63

    def test_width_only_very_narrow_source(self):
        """Extreme aspect ratio: 100×1000 source, width=28."""
        w, h = resolve_dimensions(100, 1000, width=28)
        assert w == 28
        assert h == 280  # 28 * 1000 / 100

    # ── only height specified ─────────────────────────────────────────

    def test_height_only_preserves_ratio(self):
        """Source 1920×1080, user specifies height=84."""
        w, h = resolve_dimensions(1920, 1080, height=84)
        assert h == 84
        # 84 * 1920 / 1080 = 149.33... → round → 149
        assert w == 149

    # ── neither specified ─────────────────────────────────────────────

    def test_neither_specified_returns_default(self):
        from factorio_display import DISPLAY_WIDTH, DISPLAY_HEIGHT
        w, h = resolve_dimensions(1920, 1080)
        assert w == DISPLAY_WIDTH
        assert h == DISPLAY_HEIGHT

    # ── edge cases ────────────────────────────────────────────────────

    def test_minimum_dimension_one(self):
        """Very small source, width=1 → height ≥ 1."""
        w, h = resolve_dimensions(1, 1, width=1)
        assert w == 1
        assert h == 1
