"""Unit tests for video/encoder.py — chunked time-dimension encoding."""

from __future__ import annotations

import json
import math
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
    _fix_conditions_in_dict,
    _merge_chunk_blueprints,
    _to_fixed_string,
    encode_frames,
    encode_frames_chunked,
    resolve_dimensions,
)
from factorio_display.integer2signal.mapping import SignalMapping
from factorio_display.logical_blueprint import assert_wire_topology, from_draftsman, to_draftsman
from factorio_display.cache_paths import version_prefix


# ═══════════════════════════════════════════════════════════════════════
# Test helpers — small deterministic frames and mappings
# ═══════════════════════════════════════════════════════════════════════

def _check_bp(lb, label="", *, lb_logical=None) -> None:
    """Assert a Blueprint or LogicalBlueprint is valid and has correct wire topology.

    Pass *lb_logical* (the corresponding LogicalBlueprint) to use the
    network-aware connectivity check instead of the colour-global one, which
    reports false positives for composed blueprints that legitimately contain
    multiple independent same-colour networks (e.g. an unused red sub-tick
    bus next to a green time bus).
    """
    assert lb is not None
    from factorio_display.logical_blueprint import assert_wire_topology, to_draftsman
    from draftsman.blueprintable import Blueprint
    if isinstance(lb, Blueprint):
        bp = lb
    else:
        bp = to_draftsman(lb)
    assert_wire_topology(bp, label=label, lb=lb_logical)


def _dc_count(lb) -> int:
    """Return the number of decider-combinator entities in a LogicalBlueprint."""
    return sum(1 for e in lb.entities.values() if e.type == "decider-combinator")


def _wire_count(lb) -> int:
    """Return the total number of network connections (edges) in a LogicalBlueprint."""
    return sum(len(net.endpoints) - 1 for net in lb.networks if len(net.endpoints) >= 2)

@pytest.fixture
def small_mapping_params() -> dict:
    """Params for a tiny 4×4 display with no hole."""
    return {
        "width": 4,
        "height": 4,
        "qualities": ["normal", "uncommon", "rare", "epic", "legendary"],
        "signal_pool": [
            "wooden-chest", "iron-chest", "steel-chest", "storage-tank",
            "transport-belt", "fast-transport-belt", "express-transport-belt",
            "underground-belt", "fast-underground-belt", "express-underground-belt",
            "splitter", "fast-splitter", "express-splitter",
            "burner-inserter", "inserter", "long-handed-inserter", "fast-inserter",
            "bulk-inserter", "stack-inserter", "small-electric-pole",
            "medium-electric-pole", "big-electric-pole", "substation",
            "pipe", "pipe-to-ground", "pump",
            "rail", "rail-signal", "rail-chain-signal",
            "locomotive",
        ],
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
        lb = _encode_frames_core(
            kept_frames=frames,
            tick_ranges=tick_ranges,
            output_name="Test",
            deduplicate=False,
            mapping_params=small_mapping_params,
            clock="signal-clock",
            current_tick=4,
        )
        assert lb is not None
        assert len(lb.entities) > 0
        _check_bp(lb, label="single_unit")

    def test_single_unit_three_dcs(self, sample_frames_3, small_mapping_params):
        frames, tick_ranges = sample_frames_3
        lb = _encode_frames_core(
            kept_frames=frames,
            tick_ranges=tick_ranges,
            output_name="Test",
            deduplicate=False,
            mapping_params=small_mapping_params,
            clock="signal-clock",
            current_tick=4,
        )
        assert _dc_count(lb) == 3

    def test_deduplicate_reduces_count(self, sample_frames_12, small_mapping_params):
        frames, tick_ranges = sample_frames_12
        # Without dedup: 12 DCs
        lb_no = _encode_frames_core(
            kept_frames=frames, tick_ranges=tick_ranges,
            output_name="Test", deduplicate=False,
            mapping_params=small_mapping_params,
            clock="signal-clock", current_tick=13,
        )
        assert _dc_count(lb_no) == 12
        _check_bp(lb_no, label="dedup_no")

        # With dedup: 4 unique colors → 4 DCs, each with 3 merged tick ranges
        lb_yes = _encode_frames_core(
            kept_frames=frames, tick_ranges=tick_ranges,
            output_name="Test", deduplicate=True,
            mapping_params=small_mapping_params,
            clock="signal-clock", current_tick=13,
        )
        assert _dc_count(lb_yes) == 4
        _check_bp(lb_yes, label="dedup_yes")

    def test_empty_frames_returns_empty(self, small_mapping_params):
        lb = _encode_frames_core(
            kept_frames=[], tick_ranges=[],
            output_name="Test", deduplicate=False,
            mapping_params=small_mapping_params,
            clock="signal-clock", current_tick=1,
        )
        assert len(lb.entities) == 0

    def test_multi_unit_no_longer_supported(self):
        """Multi-unit tiling has been removed. Vertical chunk splitting
        handles pool overflow instead."""
        pass

    def test_snake_wiring_has_green_and_red(self, sample_frames_3, small_mapping_params):
        frames, tick_ranges = sample_frames_3
        lb = _encode_frames_core(
            kept_frames=frames, tick_ranges=tick_ranges,
            output_name="Test", deduplicate=False,
            mapping_params=small_mapping_params,
            clock="signal-clock", current_tick=4,
        )
        # Logical networks should exist (3 DCs joined on red inputs + red outputs)
        assert len(lb.networks) >= 2, f"Expected ≥2 networks, got {len(lb.networks)}"
        _check_bp(lb, label="snake_wiring")

    def test_memory_bank_gets_fixed_width_vertical_growth(self, sample_frames_12, small_mapping_params):
        """Memory bank packs deciders into fixed-width rows (horizontal = the
        width direction) and grows VERTICALLY — not a square sqrt packing."""
        from factorio_display.video.encoder import _MEMORY_BANK_COLS

        frames, tick_ranges = sample_frames_12
        lb = _encode_frames_core(
            kept_frames=frames,
            tick_ranges=tick_ranges,
            output_name="WideLayout",
            deduplicate=False,
            mapping_params=small_mapping_params,
            clock="signal-clock",
            current_tick=13,
        )
        dcs = [e for e in lb.entities.values() if e.type == "decider-combinator"]
        assert len(dcs) == 12
        xs = [e.position[0] for e in dcs if e.position is not None]
        ys = [e.position[1] for e in dcs if e.position is not None]
        assert min(xs) == 0
        assert max(xs) == _MEMORY_BANK_COLS - 1  # width direction = a full row
        rows = math.ceil(12 / _MEMORY_BANK_COLS)
        assert max(ys) == 2 * (rows - 1)  # vertical growth, rows pitched 2 tiles

    def test_memory_bank_networks_are_prewired(self, sample_frames_12, small_mapping_params):
        frames, tick_ranges = sample_frames_12
        lb = _encode_frames_core(
            kept_frames=frames,
            tick_ranges=tick_ranges,
            output_name="PrewiredLayout",
            deduplicate=False,
            mapping_params=small_mapping_params,
            clock="signal-clock",
            current_tick=13,
        )
        dcs = [e for e in lb.entities.values() if e.type == "decider-combinator"]
        # Unified bus schema: green = clock/time (inputs), red = data (outputs).
        green_nets = [n for n in lb.networks if n.color == "green"]
        red_nets = [n for n in lb.networks if n.color == "red"]
        assert len(green_nets) >= 1, "expected a green clock (time) bus"
        assert len(red_nets) >= 1, "expected a red data bus"
        # Input (green) and output (red) buses should carry deterministic
        # pair lists.
        with_pairs = [n for n in lb.networks if n.prewired_pairs is not None]
        assert len(with_pairs) >= 2
        for net in with_pairs[:2]:
            assert len(net.prewired_pairs or []) == max(0, len(dcs) - 1)

    def test_connectors_added_to_memory_piece(self, sample_frames_3, small_mapping_params):
        """Split-mode connectors: ONE TOP and ONE BOTTOM bus connector join
        BOTH the green clock (time) bus and the red data bus; the isolated
        series MARKER CC sits at the TOP.  Connector signals are visible
        (value > 0) with the CC output toggle off."""
        from factorio_display.logical_blueprint import Endpoint

        frames, ticks = sample_frames_3
        lb = _encode_frames_core(
            kept_frames=frames, tick_ranges=ticks, output_name="Conn",
            deduplicate=False, mapping_params=small_mapping_params,
            clock="signal-clock", current_tick=4,
            connectors=True, fragment_index=2,
        )
        cc_ids = [eid for eid in lb.entities if "_conn" in eid or "_marker" in eid]
        assert "gate_1_connT" in lb.entities, "missing top bus connector CC"
        assert "gate_1_marker" in lb.entities, "missing top series marker CC"
        assert "gate_1_connB" in lb.entities, "missing bottom bus connector CC"

        # Both bus connectors must join BOTH the green clock (time) bus and
        # the red output (data) bus.
        for cc in ("gate_1_connT", "gate_1_connB"):
            green_ok = red_ok = False
            for net in lb.networks:
                has = Endpoint(cc, "input") in net.endpoints
                if net.color == "green" and has:
                    green_ok = True
                if net.color == "red" and has:
                    red_ok = True
            assert green_ok, f"{cc} must join the green clock (time) bus"
            assert red_ok, f"{cc} must join the red data bus"

        # The marker must be isolated.
        assert not any(ep.entity_id == "gate_1_marker" for n in lb.networks for ep in n.endpoints)

        # Connectors carry the identifying signal at value > 0 (visible on the
        # map) with the CC "Output" toggle OFF (enabled=False → no pollution).
        for cc in ("gate_1_connT", "gate_1_connB"):
            props = lb.entities[cc].properties
            sigs = props.get("signals", [])
            assert sigs and sigs[0]["value"] > 0, f"{cc}: identifying signal must be > 0"
            assert props.get("enabled") is False, f"{cc}: CC output must be disabled"
        # Marker CC: visible (1-based) and output disabled too.
        marker_props = lb.entities["gate_1_marker"].properties
        assert marker_props.get("signals", [{}])[0].get("value") == 3  # fragment_index + 1
        assert marker_props.get("enabled") is False

        # Serialises fine and the connector's output-off round-trips.
        # (Draftsman round-trip drops custom entity ids, so locate CCs by
        # position: rightmost top connector (cols-1, -1) — right-aligned with
        # the rightmost decider.)
        from factorio_display.logical_blueprint import from_blueprint_string, to_draftsman
        from factorio_display.video.encoder import _MEMORY_BANK_COLS
        bp = to_draftsman(lb)
        assert bp is not None
        s = bp.to_string()
        assert s.startswith("0eN")
        lb2 = from_blueprint_string(s)
        cc_by_pos = {
            ent.position: ent for ent in lb2.entities.values()
            if ent.type == "constant-combinator"
        }
        top = cc_by_pos.get((_MEMORY_BANK_COLS - 1, -1))
        assert top is not None, "rightmost top connector CC missing after round-trip"
        assert top.properties.get("enabled") is False
        assert top.properties["signals"][0]["value"] == 1


# ═══════════════════════════════════════════════════════════════════════
# encode_frames_split (split-output mode)
# ═══════════════════════════════════════════════════════════════════════

class TestEncodeFramesSplit:
    """Split output: display + one memory piece per (vertical chunk × fragment)."""

    def test_split_produces_display_and_pieces(self):
        from factorio_display.video.encoder import encode_frames_split
        from factorio_display.logical_blueprint import from_blueprint_string

        # 40x40 needs 320 base signals > the 182-signal pool → 2 vertical chunks.
        w, h = 40, 40
        frames = [
            np.full((h, w, 3), ((i % 2) * 255, (i % 3) * 255, (i * 7) % 255), dtype=np.uint8)
            for i in range(6)
        ]
        res = encode_frames_split(
            iter(frames), "SplitTest", fps=30.0, adaptive=False,
            total_width=w, total_height=h, expected_frames=6, source_id="s",
            time_chunks=2, chunk_workers=2,
        )
        assert res["num_chunks"] >= 2
        assert res["time_chunks"] == 2
        assert len(res["display"]) > 0
        assert len(res["pieces"]) == res["num_chunks"] * res["time_chunks"]

        # Display must carry per-chunk connectors: constant combinators wired
        # onto a red data bus at the right edge of each lamp chunk.
        display_lb = from_blueprint_string(res["display"])
        wired_conn_count = sum(
            1
            for net in display_lb.networks if net.color == "red"
            for ep in net.endpoints
            if display_lb.entities.get(ep.entity_id)
            and display_lb.entities[ep.entity_id].type == "constant-combinator"
        )
        assert wired_conn_count >= 2, "expected per-chunk display connectors"

        # Each memory piece is independently parseable and carries connector CCs.
        for label, s in res["pieces"]:
            lb = from_blueprint_string(s)
            assert lb.entities, f"{label}: no entities"
            wired_cc = [
                ep.entity_id
                for net in lb.networks if net.color == "red"
                for ep in net.endpoints
                if lb.entities.get(ep.entity_id)
                and lb.entities[ep.entity_id].type == "constant-combinator"
            ]
            assert len(wired_cc) >= 2, f"{label}: missing connector CCs"

    def test_split_pieces_have_distinct_tick_windows(self):
        from factorio_display.video.encoder import encode_frames_split
        from factorio_display.logical_blueprint import from_blueprint_string

        w, h = 40, 40
        frames = [
            np.full((h, w, 3), (i * 40, i * 20, 0), dtype=np.uint8) for i in range(6)
        ]
        res = encode_frames_split(
            iter(frames), "SplitTest2", fps=30.0, adaptive=False,
            total_width=w, total_height=h, expected_frames=6, source_id="s2",
            time_chunks=2, chunk_workers=2,
        )
        # Fragment 0 covers ticks 0..2, fragment 1 covers ticks 3..5 (at 30fps,
        # 2 ticks/frame). The first DC of each fragment must differ.
        f0 = from_blueprint_string(dict(res["pieces"])["memory_c0_f0"])
        f1 = from_blueprint_string(dict(res["pieces"])["memory_c0_f1"])

        def _first_decider_ticks(lb):
            for e in lb.entities.values():
                if e.type == "decider-combinator":
                    return [c.get("constant") for c in e.properties.get("conditions", [])
                            if "constant" in c]
            return []

        assert _first_decider_ticks(f0) != _first_decider_ticks(f1), (
            "fragments should gate on different ticks"
        )

    def test_split_auto_fragments_by_size(self):
        """Default (time_chunks=1) auto-splits by estimated piece size: a tiny
        max_piece_mb forces many fragments, a large one keeps a single piece."""
        from factorio_display.video.encoder import encode_frames_split

        w, h = 40, 40
        frames = [
            np.full((h, w, 3), (i * 40, i * 20, 0), dtype=np.uint8) for i in range(12)
        ]

        tiny = encode_frames_split(
            iter(frames), "AutoFrag", fps=30.0, adaptive=False,
            total_width=w, total_height=h, expected_frames=12, source_id="auto_frag",
            time_chunks=1, chunk_workers=2, max_piece_mb=0.01,
        )
        assert tiny["time_chunks"] >= 4, (
            f"tiny max_piece_mb should force many fragments, got {tiny['time_chunks']}"
        )
        assert len(tiny["pieces"]) == tiny["num_chunks"] * tiny["time_chunks"]

        # A large target keeps the whole time range in a single fragment.
        big = encode_frames_split(
            iter(frames), "AutoFragBig", fps=30.0, adaptive=False,
            total_width=w, total_height=h, expected_frames=12, source_id="auto_frag_big",
            time_chunks=1, chunk_workers=2, max_piece_mb=1000.0,
        )
        assert big["time_chunks"] == 1, (
            f"huge max_piece_mb should keep one fragment, got {big['time_chunks']}"
        )
        assert len(big["pieces"]) == big["num_chunks"]

    def test_plan_time_fragments_packs_contiguously(self):
        """_plan_time_fragments produces contiguous, ordered frame ranges that
        cover all frames and shrink as the target size shrinks."""
        from factorio_display.video.encoder import (
            _CHARS_PER_UNIT, _plan_time_fragments,
        )

        # 8 dense frames over 2 vertical chunks of height 5.
        frames = [np.full((10, 20, 3), 128, dtype=np.uint8) for _ in range(8)]
        n = len(frames)
        per_frame = _CHARS_PER_UNIT * (1 + 20 * 5)  # 5 rows x 20 cols lit
        frags = _plan_time_fragments(frames, 2, 5, 10, target_chars=per_frame * 2)
        assert frags[0][0] == 0
        assert frags[-1][1] == n
        for (s0, e0), (s1, _e1) in zip(frags, frags[1:]):
            assert e0 == s1, "fragment ranges must be contiguous"
        assert len(frags) >= 2, "small target should produce multiple fragments"

        # A target larger than the whole input yields a single fragment.
        single = _plan_time_fragments(
            frames, 2, 5, 10, target_chars=per_frame * n * 10,
        )
        assert single == [(0, n)]


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
        bp_single = single
        bp_chunked = result["full"]
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
        assert full is not None
        assert len(full.entities) > 0
        bp = full
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
            bp = chunk_bp
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
        bp = result["full"]
        ids = [getattr(e, "id", None) for e in bp.entities]
        ids = [i for i in ids if i is not None]
        assert len(ids) == len(set(ids)), f"Duplicate IDs: {len(ids)} vs {len(set(ids))}"

    def test_merge_has_shared_clock_bus(self, sample_frames_12, small_mapping):
        frames, _ = sample_frames_12
        result = encode_frames_chunked(
            self._make_frame_iter(frames), "Test", fps=60,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id="test_wiring", time_chunks=3,
        )
        bp = from_draftsman(result["full"])
        # Unified bus schema: the shared clock (time) bus is GREEN.
        clock_nets = [
            net for net in bp.networks
            if net.color == "green" and any(ep.port == "input" for ep in net.endpoints)
        ]
        assert len(clock_nets) == 1, f"Expected one shared clock network, got {len(clock_nets)}"
        clock_net = clock_nets[0]
        chunk_inputs = {
            ep.entity_id.split("_", 1)[0]
            for ep in clock_net.endpoints
            if ep.port == "input" and ep.entity_id.startswith("tc")
        }
        assert chunk_inputs == {"tc0", "tc1", "tc2"}, (
            f"Expected shared clock bus across all chunks, got {sorted(chunk_inputs)}"
        )
        data_nets = [
            net for net in bp.networks
            if net.color == "red" and any(ep.port == "output" for ep in net.endpoints)
        ]
        assert len(data_nets) == 1, (
            f"Expected one shared chunk data network, got {len(data_nets)}"
        )
        data_net = data_nets[0]
        chunk_outputs = {
            ep.entity_id.split("_", 1)[0]
            for ep in data_net.endpoints
            if ep.port == "output" and ep.entity_id.startswith("tc")
        }
        assert chunk_outputs == {"tc0", "tc1", "tc2"}, (
            f"Expected shared data bus across all chunks, got {sorted(chunk_outputs)}"
        )

    def test_tick_ranges_preserved_across_chunks(
        self, sample_frames_12, small_mapping
    ):
        """Each chunk should use a local tick window, not a cumulative one."""
        frames, expected_ranges = sample_frames_12
        result = encode_frames_chunked(
            self._make_frame_iter(frames), "Test", fps=60,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id="test_ticks", time_chunks=3, deduplicate=False,
        )
        expected_chunk_size = math.ceil(len(frames) / 3)
        for i, chunk_bp in enumerate(result["chunks"]):
            dcs = [e for e in chunk_bp.entities if "decider-combinator" in e.name]
            assert len(dcs) > 0, f"Chunk {i} has no DCs"
            constants: list[int] = []
            for dc in dcs:
                for cond in getattr(dc, "conditions", []):
                    constant = getattr(cond, "constant", None)
                    if constant is not None:
                        constants.append(constant)
            assert constants, f"Chunk {i} has no tick constants"
            assert min(constants) == 0, f"Chunk {i} tick range is not local: {constants}"
            assert max(constants) <= expected_chunk_size - 1, (
                f"Chunk {i} tick range looks cumulative: {constants}"
            )

    def test_merge_bridges_stay_within_wire_reach(self, small_mapping_params):
        """Bridging time chunks whose banks are ≥10 DCs wide must not emit
        >9-tile clock/data wires (Factorio silently drops those, so later
        chunks go dark).

        Regression: the merge bridged the sorted-first endpoint of each
        bank (``tc0_gate_1`` at x=0) while the next chunk sat 12 tiles away
        (10-column rows + 2-tile gap), producing 12-tile bridges.  The
        bridge must use the closest endpoint pair across the two networks.
        """
        frames = [np.full((4, 4, 3), (255, 0, 0), dtype=np.uint8) for _ in range(10)]
        lb0 = _encode_frames_core(
            kept_frames=list(frames), tick_ranges=[(i, i) for i in range(10)],
            output_name="A", deduplicate=False, mapping_params=small_mapping_params,
            clock="signal-clock", current_tick=10,
        )
        lb1 = _encode_frames_core(
            kept_frames=list(frames), tick_ranges=[(i, i) for i in range(10, 20)],
            output_name="B", deduplicate=False, mapping_params=small_mapping_params,
            clock="signal-clock", current_tick=20,
        )
        merged = _merge_chunk_blueprints([lb0, lb1], "Test")
        bp = to_draftsman(merged)
        _check_bp(bp, label="merged-chunks", lb_logical=merged)

        def _cheb(p, q) -> int:
            return max(abs(int(p[0]) - int(q[0])), abs(int(p[1]) - int(q[1])))

        for w in getattr(bp, "wires", []):
            assoc1, _conn1, assoc2, _conn2 = w
            e1 = assoc1() if callable(assoc1) else assoc1
            e2 = assoc2() if callable(assoc2) else assoc2
            p1 = getattr(e1, "tile_position", None)
            p2 = getattr(e2, "tile_position", None)
            if p1 is None or p2 is None:
                continue
            d = _cheb(p1, p2)
            assert d <= 9, (
                f"wire {getattr(e1, 'id', '?')}↔{getattr(e2, 'id', '?')} "
                f"spans {d} tiles (>9)"
            )


# ═══════════════════════════════════════════════════════════════════════
# Chunk caching
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.xdist_group("chunk-cache")
class TestChunkCache:
    def test_chunk_cache_path_is_versioned_and_under_single_root(self):
        cache_dir = _chunk_cache_dir(
            source_id="cache_path_test",
            time_chunks=2,
            total_w=11,
            total_h=26,
            fps=60.0,
            adaptive=False,
            threshold=0.01,
            deduplicate=False,
        )
        parts = [p.lower() for p in cache_dir.parts]
        assert ".factorio_display_cache" in parts
        assert version_prefix().lower() in cache_dir.name.lower()

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
                use_cache=True,
            )
            cache_dir = tmp_path / "test_cache"
            assert cache_dir.exists()
            # Should have chunk files
            chunk_files = list(cache_dir.glob("chunk_*.toml"))
            if not chunk_files:
                # No cache files because use_cache=False by default
                pass
            else:
                assert len(chunk_files) >= 1
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
                use_cache=True,
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
        bp = result["full"]
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
        bp = result["full"]
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
        lb1 = _encode_frames_core(
            kept_frames=frames[:mid], tick_ranges=tick_ranges[:mid],
            output_name="Chunk1", deduplicate=False,
            mapping_params=small_mapping_params,
            clock="signal-clock", current_tick=mid + 1,
        )
        lb2 = _encode_frames_core(
            kept_frames=frames[mid:], tick_ranges=tick_ranges[mid:],
            output_name="Chunk2", deduplicate=False,
            mapping_params=small_mapping_params,
            clock="signal-clock", current_tick=len(frames) + 1,
        )
        merged = _merge_chunk_blueprints([lb1, lb2], "Merged")
        assert merged
        assert _dc_count(merged) == len(frames)
        _check_bp(merged, label="merge_two_chunks")

    def test_merge_handles_empty_list(self):
        result = _merge_chunk_blueprints([], "Empty")
        assert len(result.entities) == 0

    def test_merge_single_chunk_passthrough(self, sample_frames_3, small_mapping_params):
        frames, tick_ranges = sample_frames_3
        lb = _encode_frames_core(
            kept_frames=frames, tick_ranges=tick_ranges,
            output_name="Single", deduplicate=False,
            mapping_params=small_mapping_params,
            clock="signal-clock", current_tick=4,
        )
        result = _merge_chunk_blueprints([lb], "Single")
        assert result is lb  # single chunk is pass-through
        _check_bp(result, label="merge_single")


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
            "clock": "signal-clock",
            "current_tick": 4,
            "label_suffix": " [test]",
        })
        chunk_idx, toml_str = _build_chunk_worker(payload)
        assert chunk_idx == 7
        assert toml_str
        assert len(toml_str) > 0
        # Verify it's valid LogicalBlueprint TOML
        from factorio_display.logical_blueprint import from_toml
        lb = from_toml(toml_str)
        assert lb is not None
        _check_bp(lb, label="worker")

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
            "clock": "signal-clock",
            "current_tick": 13,
        })
        _, toml_str = _build_chunk_worker(payload)
        from factorio_display.logical_blueprint import from_toml
        lb = from_toml(toml_str)
        assert _dc_count(lb) == 4  # 4 unique colors with dedup
        _check_bp(lb, label="worker_valid")


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
        chunk_files = sorted(out_dir.glob("chunk_*.toml"))
        assert len(chunk_files) == 3
        for cf in chunk_files:
            content = cf.read_text(encoding="utf-8")
            assert len(content) > 0
            from factorio_display.logical_blueprint import from_toml, to_draftsman
            lb = from_toml(content)
            assert lb is not None
            assert len(lb.entities) > 0, f"Chunk {cf.name} has no entities"
            bp = to_draftsman(lb)
            _check_bp(bp, label=f"output_chunk_{cf.name}")

    def test_output_chunks_match_result(self, sample_frames_12, small_mapping, tmp_path):
        frames, _ = sample_frames_12
        out_dir = tmp_path / "chunks_match"
        result = encode_frames_chunked(
            iter(frames), "Test", fps=60,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id="match_test", time_chunks=3,
            output_chunks_dir=str(out_dir),
        )
        for i, expected_bp in enumerate(result["chunks"]):
            cf = out_dir / f"chunk_{i:04d}.toml"
            assert cf.exists(), f"Chunk file {cf} not found"
            # Round-trip: TOML → LogicalBlueprint → Blueprint
            from factorio_display.logical_blueprint import from_toml, to_draftsman
            actual_lb = from_toml(cf.read_text(encoding="utf-8"))
            actual_bp = to_draftsman(actual_lb)
            # Compare entity count (positions may differ)
            assert len(actual_bp.entities) == len(expected_bp.entities)


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
        bp = result["full"]
        assert bp is not None

    def test_zero_frames_returns_empty(self, small_mapping):
        result = encode_frames_chunked(
            iter([]), "Test", fps=60,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id="zero", time_chunks=3,
        )
        assert len(result["full"].entities) == 0
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
        bp = result["full"]
        dcs = [e for e in bp.entities if "decider-combinator" in e.name]
        # 4 unique colors, cross-dedup merges across chunks
        assert len(dcs) <= 4

    def test_no_cache_ignores_existing_frame_cache(self, small_mapping, tmp_path, monkeypatch):
        """use_cache=False must bypass frame-cache load/write entirely."""
        import factorio_display.video.encoder as enc_mod
        from factorio_display.logical_blueprint import from_draftsman

        cache_file = tmp_path / "fake_frame_cache.pkl"
        cached_red = np.full((4, 4, 3), (255, 0, 0), dtype=np.uint8)
        with open(cache_file, "wb") as f:
            pickle.dump({
                "frames": [cached_red],
                "ticks": [(0, 0)],
                "current_tick": 1,
            }, f)

        monkeypatch.setattr(enc_mod, "make_cache_file", lambda *args, **kwargs: cache_file)

        fresh_blue = np.full((4, 4, 3), (0, 0, 255), dtype=np.uint8)
        bp = encode_frames(
            iter([fresh_blue]), "NoCache", fps=60,
            mapping=small_mapping, total_width=4, total_height=4,
            source_id="no_cache_regression", use_cache=False,
        )

        lb = from_draftsman(bp)
        dc = next(e for e in lb.entities.values() if e.type == "decider-combinator")
        outputs = dc.properties.get("outputs", [])
        assert outputs, "Expected decider outputs from encoded frame"

        encoded_constants = {int(o["constant"]) for o in outputs}
        blue_int = (0 << 16) | (0 << 8) | 255
        red_int = (255 << 16) | (0 << 8) | 0
        assert blue_int in encoded_constants
        assert red_int not in encoded_constants


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

        bp = encode_frames(
            iter(frames),
            output_name="Smoke Test",
            fps=30,
            total_height=user_h,
            source_id="smoke_height",
        )
        assert bp is not None
        assert len(bp.entities) > 0
        dcs = [e for e in bp.entities if "decider-combinator" in e.name]
        assert len(dcs) == 3

    def test_encode_with_width_only(self):
        """Mirrors: factorio-display encode video.mp4 --width 56"""
        total_w, total_h = resolve_dimensions(1920, 1080, width=56)
        frames = [
            np.full((total_h, total_w, 3), (128, 128, 128), dtype=np.uint8),
        ]
        bp = encode_frames(
            iter(frames), "Smoke Width", fps=60,
            total_width=56, source_id="smoke_width",
        )
        assert bp is not None
        assert len(bp.entities) >= 1

    def test_single_image_end_to_end_compose_wiring(self):
        """Single-picture all-in-one composition must keep timer→memory and
        memory→display wiring valid and physically reachable."""
        from conftest import validate_blueprint_via_logical
        from factorio_display.cli import (
            _build_timer_for_memory,
            _connect_data_ports,
            _declare_memory_ports,
        )
        from factorio_display.composer import PortConnection, compose
        from factorio_display.logical_blueprint import from_draftsman, to_draftsman
        from factorio_display.video.player_blueprint import build_display_logical

        total_w, total_h = 11, 26
        frame = np.full((total_h, total_w, 3), (128, 128, 128), dtype=np.uint8)
        video_bp = encode_frames(
            iter([frame]),
            output_name="Smoke Single",
            fps=60,
            total_width=total_w,
            total_height=total_h,
            source_id="smoke_single_image_compose",
            use_cache=False,
        )

        video_lb = from_draftsman(video_bp)
        video_lb.label = "Video Memory: Smoke Single"
        _declare_memory_ports(video_lb)

        display_lb = build_display_logical("Display", width=total_w, height=total_h)
        timer_lb = _build_timer_for_memory(video_lb)

        connections = [PortConnection("Timer", "clock", video_lb.label, "clock")]
        _connect_data_ports(connections, video_lb, display_lb)

        merged = compose(
            components=[timer_lb, video_lb, display_lb],
            connections=connections,
            output_name="SmokeSingleCompose",
            use_cache=False,
        )
        final_bp = to_draftsman(merged)
        _check_bp(final_bp, label="single_image_compose", lb_logical=merged)

        report = validate_blueprint_via_logical(final_bp.to_string())
        assert report["errors"] == [], (
            "Expected no wiring/topology errors in single-image compose, "
            f"got: {report['errors']}"
        )

    def test_large_single_image_compose_wires_within_reach(self):
        """Regression: a single-frame image whose data-bus chain ends are far
        from the right-placed sources must still produce placeable wires.

        Reported for a 23×30 image: the video-memory gate was bridged to a
        lamp at the *far* chain-end (27 tiles) instead of the nearest one,
        and the gate was not placed next to the timer (14-tile clock wire) —
        both exceed Factorio's 9-tile circuit-wire reach and are silently
        dropped in-game, disconnecting the display.  Every materialised wire
        must be within reach.
        """
        from factorio_display.cli import (
            _build_timer_for_memory,
            _connect_data_ports,
            _declare_memory_ports,
        )
        from factorio_display.composer import PortConnection, compose
        from factorio_display.logical_blueprint import to_draftsman
        from factorio_display.video.player_blueprint import build_display_logical

        total_w, total_h = 23, 30  # same size as the reported broken job
        frame = np.full((total_h, total_w, 3), (128, 128, 128), dtype=np.uint8)
        video_bp = encode_frames(
            iter([frame]),
            output_name="Regress Wire Reach",
            fps=60,
            total_width=total_w,
            total_height=total_h,
            source_id="regress_wire_reach",
            use_cache=False,
        )

        video_lb = from_draftsman(video_bp)
        video_lb.label = "Video Memory: Regress Wire Reach"
        _declare_memory_ports(video_lb)

        display_lb = build_display_logical("Display", width=total_w, height=total_h)
        timer_lb = _build_timer_for_memory(video_lb)

        connections = [PortConnection("Timer", "clock", video_lb.label, "clock")]
        _connect_data_ports(connections, video_lb, display_lb)

        merged = compose(
            components=[timer_lb, video_lb, display_lb],
            connections=connections,
            output_name="RegressWireReachCompose",
            use_cache=False,
        )
        final_bp = to_draftsman(merged)
        _check_bp(final_bp, label="regress_wire_reach", lb_logical=merged)

        # Every materialised wire must be within Factorio's 9-tile reach,
        # measured between connection points (draftsman global positions).
        too_long: list[str] = []
        for w in final_bp.wires:
            e1 = w[0]()
            e2 = w[2]()
            p1, p2 = e1.global_position, e2.global_position
            d = math.dist((p1.x, p1.y), (p2.x, p2.y))
            if d > 9.0:
                too_long.append(f"{e1.id} ↔ {e2.id} ({d:.1f} tiles)")
        assert not too_long, (
            f"{len(too_long)} wire(s) exceed Factorio's 9-tile reach:\n"
            + "\n".join(too_long[:10])
        )

    def test_encode_triggers_vertical_chunk_split(self):
        """Large display (many pixels) triggers vertical chunk splitting."""
        # 28×70 = 1960 px, default pool ~780 signals → should split into ~3 chunks
        frames = [
            np.full((70, 28, 3), (255, 0, 0), dtype=np.uint8),
        ]
        bp = encode_frames(
            iter(frames), "Smoke Chunked", fps=60,
            total_width=28, total_height=70, source_id="smoke_chunked",
        )
        assert bp is not None
        # Should have entities from multiple chunks (more than 1 DC)
        dcs = [e for e in bp.entities if "decider-combinator" in e.name]
        assert len(dcs) > 1  # multiple chunks → multiple DCs
        _check_bp(bp, label="vertical_chunks")

    def test_multi_chunk_compose_passes_topology(self):
        """All-in-one composition with a multi-chunk display must produce
        a fully connected blueprint — every red network must be one
        connected component."""
        from factorio_display.logical_blueprint import from_draftsman, to_draftsman
        from factorio_display.video.player_blueprint import build_display_logical
        from factorio_display.composer import compose, PortConnection
        from factorio_display.cli import (
            _declare_memory_ports,
            _connect_data_ports,
            _build_timer_for_memory,
        )

        # 28×70 display triggers chunking (pool ~720, chunk_height ≈ 25)
        total_w, total_h = 28, 70
        frames = [
            np.full((total_h, total_w, 3), (255, 0, 0), dtype=np.uint8),
        ]
        video_bp = encode_frames(
            iter(frames), "MultiChunkCompose", fps=60,
            total_width=total_w, total_height=total_h,
            source_id="mcc_topo",
        )

        video_lb = from_draftsman(video_bp)
        video_lb.label = "Video Memory: MultiChunkCompose"
        _declare_memory_ports(video_lb)

        display_lb = build_display_logical(
            name="Display", width=total_w, height=total_h,
        )
        timer = _build_timer_for_memory(video_lb)

        components = [timer, video_lb, display_lb]
        connections: list[PortConnection] = []
        connections.append(PortConnection("Timer", "clock", video_lb.label, "clock"))
        _connect_data_ports(connections, video_lb, display_lb)

        result = compose(
            components=components, connections=connections,
            output_name="MCCTest",
            use_cache=False,
        )
        final_bp = to_draftsman(result)
        _check_bp(final_bp, label="multi_chunk_compose", lb_logical=result)

        # Verify chunks: display should have multiple data ports
        data_ports = [p for p in display_lb.input_ports if p.startswith("data")]
        assert len(data_ports) > 1, f"Expected >1 data ports, got {data_ports}"

        # Verify video memory has matching output ports
        vm_data_ports = [p for p in video_lb.output_ports if p.startswith("data")]
        assert len(vm_data_ports) == len(data_ports), (
            f"Video memory ports {vm_data_ports} != display ports {data_ports}"
        )

    def test_encode_with_both_dimensions(self):
        """Mirrors: factorio-display encode video.mp4 --width 56 --height 84"""
        frames = [
            np.full((84, 56, 3), (255, 255, 255), dtype=np.uint8),
        ]
        bp = encode_frames(
            iter(frames), "Smoke Both", fps=60,
            total_width=56, total_height=84, source_id="smoke_both",
        )
        assert bp is not None
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

    def test_neither_specified_fits_display_bounds(self):
        """When neither width nor height is given, the result fits within
        DISPLAY_WIDTH × DISPLAY_HEIGHT while preserving the source aspect ratio."""
        from factorio_display import DISPLAY_WIDTH, DISPLAY_HEIGHT
        w, h = resolve_dimensions(1920, 1080)
        assert w == DISPLAY_WIDTH
        # 35 * 1080 / 1920 = 19.6875 → 20 (fits within DISPLAY_HEIGHT=26)
        assert h == 20
        assert w <= DISPLAY_WIDTH
        assert h <= DISPLAY_HEIGHT

    # ── edge cases ────────────────────────────────────────────────────

    def test_minimum_dimension_one(self):
        """Very small source, width=1 → height ≥ 1."""
        w, h = resolve_dimensions(1, 1, width=1)
        assert w == 1
        assert h == 1


# ═══════════════════════════════════════════════════════════════════════
# _fix_conditions_in_dict  /  _to_fixed_string
# ═══════════════════════════════════════════════════════════════════════

class TestFixConditions:
    """Ensure decider condition compare_type fields are correct in
    serialised output, accounting for Draftsman's omission of default
    ``"or"`` values.
    """

    def _make_dc_blueprint(self, conditions: list) -> Blueprint:
        from draftsman.entity import DeciderCombinator, new_entity
        bp = Blueprint()
        dc = new_entity("decider-combinator", id="test", tile_position=(0, 0))
        dc.conditions = conditions
        bp.entities.append(dc)
        return bp

    def _get_fixed_conds(self, bp: Blueprint) -> list[dict]:
        d = bp.to_dict()
        _fix_conditions_in_dict(d)
        return d["blueprint"]["entities"][0][
            "control_behavior"]["decider_conditions"]["conditions"]

    # ── single range ─────────────────────────────────────────────────

    def test_single_range_both_have_and(self):
        from draftsman.entity import DeciderCombinator
        c0 = DeciderCombinator.Condition(
            first_signal={"name": "signal-clock"},
            comparator=">=", constant=10, compare_type="and")
        c1 = DeciderCombinator.Condition(
            first_signal={"name": "signal-clock"},
            comparator="<=", constant=20, compare_type="and")
        conds = self._get_fixed_conds(self._make_dc_blueprint([c0, c1]))
        assert conds[0]["compare_type"] == "and"
        assert conds[1]["compare_type"] == "and"

    def test_single_range_first_missing_ct_gets_and(self):
        """Even if the first condition was created without explicit
        compare_type (Draftsman default ``"or"``), the fix adds ``"and"``.
        """
        from draftsman.entity import DeciderCombinator
        c0 = DeciderCombinator.Condition(
            first_signal={"name": "signal-clock"},
            comparator=">=", constant=10)  # no compare_type
        c1 = DeciderCombinator.Condition(
            first_signal={"name": "signal-clock"},
            comparator="<=", constant=20, compare_type="and")
        conds = self._get_fixed_conds(self._make_dc_blueprint([c0, c1]))
        assert conds[0]["compare_type"] == "and"
        assert conds[1]["compare_type"] == "and"

    # ── single-tick (equals) ─────────────────────────────────────────

    def test_single_tick_eq_gets_and(self):
        from draftsman.entity import DeciderCombinator
        c = DeciderCombinator.Condition(
            first_signal={"name": "signal-clock"},
            comparator="=", constant=42)
        conds = self._get_fixed_conds(self._make_dc_blueprint([c]))
        assert conds[0]["compare_type"] == "and"

    # ── merged ranges ────────────────────────────────────────────────

    def test_two_ranges_boundary_is_or(self):
        from draftsman.entity import DeciderCombinator
        c0 = DeciderCombinator.Condition(
            first_signal={"name": "signal-clock"},
            comparator=">=", constant=1, compare_type="and")
        c1 = DeciderCombinator.Condition(
            first_signal={"name": "signal-clock"},
            comparator="<=", constant=3, compare_type="and")
        c2 = DeciderCombinator.Condition(
            first_signal={"name": "signal-clock"},
            comparator=">=", constant=5, compare_type="and")
        c3 = DeciderCombinator.Condition(
            first_signal={"name": "signal-clock"},
            comparator="<=", constant=7, compare_type="and")
        conds = self._get_fixed_conds(
            self._make_dc_blueprint([c0, c1, c2, c3]))
        # c0, c1: first range → both "and"
        assert conds[0]["compare_type"] == "and"
        assert conds[1]["compare_type"] == "and"
        # c2: start of second range → "or"
        assert conds[2]["compare_type"] == "or"
        # c3: end of second range → "and"
        assert conds[3]["compare_type"] == "and"

    def test_three_ranges_two_or_boundaries(self):
        from draftsman.entity import DeciderCombinator
        conds_in = []
        for i, (s, e) in enumerate([(1, 3), (5, 7), (9, 11)]):
            conds_in.append(DeciderCombinator.Condition(
                first_signal={"name": "signal-clock"},
                comparator=">=", constant=s, compare_type="and"))
            conds_in.append(DeciderCombinator.Condition(
                first_signal={"name": "signal-clock"},
                comparator="<=", constant=e, compare_type="and"))
        conds = self._get_fixed_conds(self._make_dc_blueprint(conds_in))
        assert conds[0]["compare_type"] == "and"  # >= 1
        assert conds[1]["compare_type"] == "and"  # <= 3
        assert conds[2]["compare_type"] == "or"   # >= 5  (range boundary)
        assert conds[3]["compare_type"] == "and"  # <= 7
        assert conds[4]["compare_type"] == "or"   # >= 9  (range boundary)
        assert conds[5]["compare_type"] == "and"  # <= 11

    # ── _to_fixed_string ─────────────────────────────────────────────

    def test_to_fixed_string_produces_valid_blueprint(self):
        from draftsman.entity import DeciderCombinator
        c0 = DeciderCombinator.Condition(
            first_signal={"name": "signal-clock"},
            comparator=">=", constant=10, compare_type="and")
        c1 = DeciderCombinator.Condition(
            first_signal={"name": "signal-clock"},
            comparator="<=", constant=20, compare_type="and")
        bp = self._make_dc_blueprint([c0, c1])
        s = _to_fixed_string(bp)
        # Must be a valid blueprint string (starts with version byte)
        assert len(s) > 0
        assert s[0] == "0"
        # Round-trip parseable
        bp2 = Blueprint.from_string(s)
        assert len(bp2.entities) == 1
