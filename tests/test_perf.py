"""Performance regression tests for factorio-display.

These tests measure the wall-clock time of key blueprint-building
operations and assert that they stay within acceptable thresholds.

Run with:  pytest tests/test_perf.py -v
Skip with: pytest -m "not perf"
"""

from __future__ import annotations

import time

import pytest

from draftsman.blueprintable import Blueprint


# ═══════════════════════════════════════════════════════════════════════
# Thresholds (seconds) — tuned for a typical dev laptop.
# These are *ceilings*: any operation crossing its threshold is a
# regression that needs investigation.
# ═══════════════════════════════════════════════════════════════════════

# build_audio_decoder() constructs 170 entities + wiring + serialisation.
AUDIO_DECODER_BUILD_MAX = 1.0

# Blueprint.from_string() parses a ~170-entity blueprint string.
AUDIO_DECODER_PARSE_MAX = 0.4

# Full validate_blueprint_via_logical() round-trip (parse + from_draftsman).
AUDIO_DECODER_VALIDATE_MAX = 1.5

# _encode_frames_core with 3 tiny 4×4 frames.
VIDEO_ENCODE_3_FRAMES_MAX = 1.5

# compose_all_in_one (video-only, simplest path) — cached result expected.
COMPOSE_VIDEO_CACHED_MAX = 0.05

# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _time_it(func, *args, **kwargs) -> tuple[float, object]:
    """Call *func* and return (elapsed_seconds, result)."""
    t0 = time.perf_counter()
    result = func(*args, **kwargs)
    elapsed = time.perf_counter() - t0
    return elapsed, result


# ═══════════════════════════════════════════════════════════════════════
# Audio decoder performance
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.perf
class TestAudioDecoderPerf:
    """Performance of ``build_audio_decoder`` and related operations."""

    def test_build_audio_decoder_speed(self):
        """build_audio_decoder() must complete within threshold."""
        from factorio_display.audio.player_blueprint import build_audio_decoder

        elapsed, bp_str = _time_it(build_audio_decoder, name="PerfTest")
        assert bp_str.startswith("0e"), "Blueprint string should start with 0e"
        assert elapsed < AUDIO_DECODER_BUILD_MAX, (
            f"build_audio_decoder() took {elapsed:.3f}s, "
            f"threshold={AUDIO_DECODER_BUILD_MAX}s"
        )

    def test_parse_blueprint_speed(self, audio_decoder_bp_str):
        """Blueprint.from_string() must complete within threshold."""
        elapsed, bp = _time_it(Blueprint.from_string, audio_decoder_bp_str)
        assert bp is not None
        assert elapsed < AUDIO_DECODER_PARSE_MAX, (
            f"Blueprint.from_string() took {elapsed:.3f}s, "
            f"threshold={AUDIO_DECODER_PARSE_MAX}s"
        )

    def test_validate_blueprint_via_logical_speed(self, audio_decoder_bp_str):
        """Full logical validation round-trip must complete within threshold."""
        from conftest import validate_blueprint_via_logical

        elapsed, result = _time_it(
            validate_blueprint_via_logical, audio_decoder_bp_str,
        )
        assert result["errors"] == [], f"Validation errors: {result['errors']}"
        assert elapsed < AUDIO_DECODER_VALIDATE_MAX, (
            f"validate_blueprint_via_logical() took {elapsed:.3f}s, "
            f"threshold={AUDIO_DECODER_VALIDATE_MAX}s"
        )


# ═══════════════════════════════════════════════════════════════════════
# Video encoder performance
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.perf
class TestVideoEncoderPerf:
    """Performance of video encoding operations."""

    @pytest.fixture
    def tiny_frames_and_params(self) -> tuple:
        """3 distinct 4×4 RGB frames with mapping params."""
        import numpy as np
        frames = [
            np.full((4, 4, 3), (255, 0, 0), dtype=np.uint8),
            np.full((4, 4, 3), (0, 255, 0), dtype=np.uint8),
            np.full((4, 4, 3), (0, 0, 255), dtype=np.uint8),
        ]
        tick_ranges = [(1, 1), (2, 2), (3, 3)]
        mapping_params = {
            "width": 4,
            "height": 4,
            "qualities": ["normal", "uncommon", "rare", "epic", "legendary"],
            "signal_pool": [f"test-signal-{i:04d}" for i in range(30)],
        }
        return frames, tick_ranges, mapping_params

    def test_encode_frames_core_speed(self, tiny_frames_and_params):
        """_encode_frames_core must complete within threshold."""
        from factorio_display.video.encoder import _encode_frames_core

        frames, tick_ranges, mapping_params = tiny_frames_and_params
        elapsed, bp_str = _time_it(
            _encode_frames_core,
            kept_frames=frames,
            tick_ranges=tick_ranges,
            output_name="PerfTest",
            deduplicate=False,
            mapping_params=mapping_params,
            clock="signal-clock",
            current_tick=4,
        )
        assert bp_str, "Blueprint string must not be empty"
        assert elapsed < VIDEO_ENCODE_3_FRAMES_MAX, (
            f"_encode_frames_core took {elapsed:.3f}s, "
            f"threshold={VIDEO_ENCODE_3_FRAMES_MAX}s"
        )


# ═══════════════════════════════════════════════════════════════════════
# Composer performance (cached path)
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.perf
class TestComposerPerf:
    """Performance of ``compose_all_in_one`` — cached path only."""

    @pytest.fixture(scope="class")
    def _composer_fixtures(self):
        """Build the sub-blueprints needed for compose_all_in_one (class-scoped)."""
        from factorio_display.composer import compose_all_in_one
        from factorio_display.logical_blueprint import LogicalBlueprint, from_draftsman
        from factorio_display.video.encoder import _encode_frames_core
        from factorio_display.video.player_blueprint import build_display
        from factorio_display.timer import build_raw_timer, build_mod_timer
        from factorio_display.progress_bar import build_progress_bar
        from factorio_display.composer import _assign_tile_positions, _connect_nets_by_color
        import numpy as np

        # ── Display ───────────────────────────────────────────────
        bp_str = build_display("PerfDisplay", width=10, height=10)
        bp = Blueprint.from_string(bp_str)
        display_lb = from_draftsman(bp)

        # ── Video memory ──────────────────────────────────────────
        frames = [
            np.full((10, 10, 3), (255, 0, 0), dtype=np.uint8),
        ]
        tick_ranges = [(0, 0)]
        mapping_params = {
            "width": 10, "height": 10,
            "qualities": ["normal", "uncommon", "rare", "epic", "legendary"],
            "signal_pool": [f"perf-sig-{i:04d}" for i in range(150)],
        }
        video_bp_str = _encode_frames_core(
            kept_frames=frames, tick_ranges=tick_ranges,
            output_name="PerfVideo", deduplicate=False,
            mapping_params=mapping_params,
            clock="signal-clock", current_tick=1,
        )
        video_memory_lb = from_draftsman(Blueprint.from_string(video_bp_str))

        # ── Timer ─────────────────────────────────────────────────
        timer_lb = LogicalBlueprint(label="PerfTimer")
        raw = build_raw_timer("PerfClock")
        mod = build_mod_timer(60, name="PerfSubTick")
        _assign_tile_positions(mod, start_x=0, start_y=4)
        timer_lb.merge(raw)
        timer_lb.merge(mod, entity_prefix="mod_", network_prefix="mod_")
        _connect_nets_by_color(
            timer_lb, "red",
            entity_contains="perfclock", port="output",
            other_entity_contains="perfsubtick", other_port="input",
        )

        # ── Progress bar ──────────────────────────────────────────
        progress_lb = build_progress_bar(
            "PerfPB", length=10, signal_name="signal-clock", max_value=59,
        )

        return {
            "display_lb": display_lb,
            "video_memory_lb": video_memory_lb,
            "timer_lb": timer_lb,
            "progress_lb": progress_lb,
        }

    def test_compose_video_cached_speed(self, _composer_fixtures):
        """Cached compose_all_in_one should be near-instant (cache hit)."""
        import uuid
        from factorio_display.composer import compose_all_in_one

        fixtures = _composer_fixtures
        tag = uuid.uuid4().hex[:8]
        cache_parts = ("perf", "video", tag)

        # First call: populates cache
        compose_all_in_one(
            display_lb=fixtures["display_lb"],
            video_memory_lb=fixtures["video_memory_lb"],
            timer_lb=fixtures["timer_lb"],
            progress_bar_lb=fixtures["progress_lb"],
            pole_type=None,
            output_name="PerfCached",
            use_cache=True,
            cache_key_parts=cache_parts,
        )

        # Second call: should hit cache
        elapsed, result = _time_it(
            compose_all_in_one,
            display_lb=fixtures["display_lb"],
            video_memory_lb=fixtures["video_memory_lb"],
            timer_lb=fixtures["timer_lb"],
            progress_bar_lb=fixtures["progress_lb"],
            pole_type=None,
            output_name="PerfCached",
            use_cache=True,
            cache_key_parts=cache_parts,
        )

        assert result.label == "PerfCached"
        assert len(result.entities) > 0
        assert elapsed < COMPOSE_VIDEO_CACHED_MAX, (
            f"Cached compose_all_in_one took {elapsed:.3f}s, "
            f"threshold={COMPOSE_VIDEO_CACHED_MAX}s"
        )


# ═══════════════════════════════════════════════════════════════════════
# No add_section print leak
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.perf
class TestNoDebugPrintLeak:
    """Verify that the ``print("add_section")`` leak is patched."""

    def test_add_section_does_not_print(self, capsys):
        """Building an audio decoder must not produce stdout from draftsman."""
        from factorio_display.audio.player_blueprint import build_audio_decoder

        build_audio_decoder(name="NoPrintTest")
        captured = capsys.readouterr()
        assert "add_section" not in captured.out, (
            "draftsman's add_section debug print leaked to stdout"
        )
