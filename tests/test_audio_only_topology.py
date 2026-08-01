"""Regression test for the audio-only encode topology assertion.

The merged blueprint (timer + audio memory + audio player) intentionally
contains many independent logical networks of the same colour (green clock
buses, red data networks, per-channel lookup chains).  A colour-global
connectivity check would report false positives; this test verifies the
network-aware assertion passes.
"""
from __future__ import annotations

import numpy as np

from factorio_display.audio.encoder import encode_audio_auto
from factorio_display.audio.player_blueprint import build_audio_decoder_logical
from factorio_display.cli import _build_timer_for_memory, _declare_memory_ports
from factorio_display.composer import PortConnection, compose
from factorio_display.logical_blueprint import (
    LogicalBlueprint,
    assert_wire_topology,
    from_draftsman,
    to_draftsman,
)


def _synthetic_audio_memory() -> LogicalBlueprint:
    """Build a tiny audio memory blueprint from a short sine tone.

    Mimics the CLI path: encode raw audio with ``attach_player=False``,
    convert to LogicalBlueprint, declare clock/data ports.
    """
    sample_rate = 22050
    duration = 0.25
    t = np.linspace(0.0, duration, int(sample_rate * duration), endpoint=False)
    tone = np.sin(2 * np.pi * 440.0 * t)
    # encode_audio_auto expects a file path; write a temporary WAV.
    import tempfile
    from pathlib import Path

    tmp_path = Path(tempfile.mktemp(suffix=".wav"))
    try:
        import wave

        max_val = np.iinfo(np.int16).max
        samples = (tone * max_val).astype(np.int16)
        with wave.open(str(tmp_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(samples.tobytes())

        bp_str = encode_audio_auto(str(tmp_path), attach_player=False)
        assert bp_str, "encode_audio_auto returned empty blueprint"
    finally:
        tmp_path.unlink(missing_ok=True)

    from draftsman.blueprintable import Blueprint

    audio_lb = from_draftsman(Blueprint.from_string(bp_str))
    audio_lb.label = "Audio Memory: Test"
    _declare_memory_ports(audio_lb, clock_color="green")
    return audio_lb


def test_audio_memory_plus_player_plus_player_passes_topology() -> None:
    """Merged timer + audio memory + audio player must not raise topology errors."""
    audio_lb = _synthetic_audio_memory()
    timer = _build_timer_for_memory(audio_lb)
    player_lb = build_audio_decoder_logical(
        name="Audio Player: Test",
        instrument="piano",
        clock_signal="signal-clock",
        map_drums=True,
    )

    components = [timer, audio_lb, player_lb]
    connections = [
        PortConnection("Timer", "clock", audio_lb.label, "clock"),
        PortConnection("Timer", "clock", player_lb.label, "clock"),
        PortConnection(audio_lb.label, "data", player_lb.label, "data"),
    ]

    result = compose(
        components=components,
        connections=connections,
        output_name="AudioTopologyTest",
        pole_type=None,
        use_cache=False,
    )

    final_bp = to_draftsman(result)
    # This used to raise AssertionError because of colour-global connectivity.
    assert_wire_topology(final_bp, label="AudioTopologyTest", lb=result)


def test_audio_composition_wires_within_factorio_limit() -> None:
    """Every circuit wire in the composed audio blueprint must be ≤ 9 tiles.

    Wires beyond Factorio's connection range are silently dropped when the
    blueprint is placed — this is what used to leave the audio memory
    output unconnected to the player's page-data selectors (the memory was
    stacked at the player's speaker row, ~14 tiles from the y=16 selectors).
    """
    audio_lb = _synthetic_audio_memory()
    timer = _build_timer_for_memory(audio_lb)
    player_lb = build_audio_decoder_logical(
        name="Audio Player: Test",
        instrument="piano",
        clock_signal="signal-clock",
        map_drums=True,
    )

    result = compose(
        components=[timer, audio_lb, player_lb],
        connections=[
            PortConnection("Timer", "clock", audio_lb.label, "clock"),
            PortConnection("Timer", "clock", player_lb.label, "clock"),
            PortConnection(audio_lb.label, "data", player_lb.label, "data"),
        ],
        output_name="AudioWireLimitTest",
        pole_type=None,
        use_cache=False,
    )

    final_bp = to_draftsman(result)
    assert final_bp.wires, "expected some wires"
    for w in final_bp.wires:
        e1 = w[0]()
        e2 = w[2]()
        p1 = e1.tile_position
        p2 = e2.tile_position
        d = max(abs(p1[0] - p2[0]), abs(p1[1] - p2[1]))
        assert d <= 9, (
            f"Wire {e1.id} ↔ {e2.id} spans {d} tiles (> 9 Factorio limit)"
        )


def _large_audio_memory() -> LogicalBlueprint:
    """Build a multi-hundred-page audio memory via the draftsman encoder
    (12-wide grid → many rows), like a real song."""
    from draftsman.blueprintable import Blueprint

    from factorio_display import QUALITIES, SIGNAL_POOL
    from factorio_display.audio.encoder import encode_audio_memory

    # 2400 ticks → 28800 cells → 40 pages → 4 rows in the 12-wide bank.
    ticks = 2400
    data = [[(i % 80) + 5 for i in range(48)] for _ in range(ticks)]
    bp_str = encode_audio_memory(
        data, "LargeAudio",
        signal_pool=list(SIGNAL_POOL), qualities=list(QUALITIES),
        clock_signal="signal-clock",
    )
    audio_lb = from_draftsman(Blueprint.from_string(bp_str))
    audio_lb.label = "Audio Memory: Large"
    _declare_memory_ports(audio_lb, clock_color="green")
    return audio_lb


def test_large_audio_composition_all_wires_short() -> None:
    """A multi-row audio-memory bank must still connect timer + player with
    all wires ≤ 9 tiles.

    This is the real-world failure: for large banks the generic composer
    bridged the memory to the player/timer using endpoints at the bank's
    extremes, producing 11–24 tile wires that Factorio silently drops —
    leaving the timer's clock unconnected to the decoder's mod AC and the
    memory unconnected to the page-data selectors.
    """
    from factorio_display.cli import _finalize_audio_composition, _restore_memory_prewiring

    audio_lb = _large_audio_memory()
    _restore_memory_prewiring(audio_lb)
    timer = _build_timer_for_memory(audio_lb)
    player_lb = build_audio_decoder_logical(
        name="Audio Player: Large",
        instrument="piano",
        clock_signal="signal-clock",
        map_drums=True,
    )

    result = compose(
        components=[timer, audio_lb, player_lb],
        connections=[
            PortConnection("Timer", "clock", audio_lb.label, "clock"),
            PortConnection("Timer", "clock", player_lb.label, "clock"),
            PortConnection(audio_lb.label, "data", player_lb.label, "data"),
        ],
        output_name="LargeAudioWireTest",
        pole_type=None,
        use_cache=False,
    )
    _finalize_audio_composition(result)

    final_bp = to_draftsman(result)
    assert_wire_topology(final_bp, label="LargeAudioWireTest", lb=result)
    assert final_bp.wires, "expected some wires"
    for w in final_bp.wires:
        e1 = w[0]()
        e2 = w[2]()
        p1 = e1.tile_position
        p2 = e2.tile_position
        d = max(abs(p1[0] - p2[0]), abs(p1[1] - p2[1]))
        assert d <= 9, (
            f"Wire {e1.id} ↔ {e2.id} spans {d} tiles (> 9 Factorio limit)"
        )

    # The timer's clock must reach the decoder's mod AC, and the memory must
    # reach the page-data selectors — the two wires the user saw missing.
    # They may be wired through the shared memory clock/data bus (same network),
    # so verify graph connectivity rather than a single direct wire.
    from collections import defaultdict

    color_of: dict[tuple[str, str], str] = {}
    adj: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for w in final_bp.wires:
        e1 = w[0]()
        e2 = w[2]()
        wt1 = w[1].value if hasattr(w[1], "value") else int(w[1])
        color = "red" if wt1 % 2 == 1 else "green"
        a = (e1.id, color)
        b = (e2.id, color)
        adj[a].add(b)
        adj[b].add(a)
        color_of[a] = color
        color_of[b] = color

    def _reachable(start: str, color: str) -> set[str]:
        seen: set[str] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for nb in adj.get((cur, color), set()):
                if nb[0] not in seen:
                    stack.append(nb[0])
        return seen

    timer_out = next(eid for eid in result.entities if eid.startswith("timer_")
                     and "bridge_clock" in eid)
    mod_id = next(eid for eid in result.entities
                  if eid.startswith("audio_player_") and eid.endswith("mod"))
    assert mod_id in _reachable(timer_out, "green"), (
        "timer clock does not reach decoder mod AC"
    )

    mem_dc = next(eid for eid in result.entities if "__ent" in eid)
    player_sels = [eid for eid in result.entities
                   if eid.startswith("audio_player_") and eid.endswith("_sel")]
    assert any(
        sel in _reachable(mem_dc, "red") for sel in player_sels
    ), "memory data does not reach decoder page-data selectors"


def test_compact_drum_audio_passes_topology() -> None:
    """Compact drum memory + player must compose without topology errors.

    A single-kick drum rail uses 1 cell/tick (60 cells/page) on the memory
    side and a 7-entity decoder (1 speaker + LUT + match + selector +
    unpacker + port + mod).  This must still wire up like a normal rail.
    """
    from factorio_display import QUALITIES, SIGNAL_POOL
    from factorio_display.audio.encoder import encode_audio_to_logical
    from factorio_display.audio.pitch_mapping import drum_grouping

    used = {0}  # kick-1 only
    data = [[0] * 48 for _ in range(90)]
    for t in range(90):
        data[t][0] = 60  # kick-1 every tick

    mem = encode_audio_to_logical(
        data, "Audio Memory: Drum", list(SIGNAL_POOL), list(QUALITIES),
        clock_signal="signal-clock", id_prefix="r0_",
        grouping=drum_grouping(used),
    )
    mem.label = "Audio Memory: Drum"

    timer = _build_timer_for_memory(mem)
    player_lb = build_audio_decoder_logical(
        name="Audio Player: Drum",
        instrument="drum",
        clock_signal="signal-clock",
        map_drums=True,
        active_drum_pitches=used,
    )

    result = compose(
        components=[timer, mem, player_lb],
        connections=[
            PortConnection("Timer", "clock", mem.label, "clock"),
            PortConnection("Timer", "clock", player_lb.label, "clock"),
            PortConnection(mem.label, "data", player_lb.label, "data"),
        ],
        output_name="CompactDrumTopologyTest",
        pole_type=None,
        use_cache=False,
    )
    final_bp = to_draftsman(result)
    assert_wire_topology(final_bp, label="CompactDrumTopologyTest", lb=result)
    # The drum decoder is tiny: exactly one drum-kit speaker (kick-1).
    speakers = [e for e in final_bp.entities
                if e.name == "programmable-speaker" and e.instrument_name == "drum-kit"]
    assert len(speakers) == 1, f"expected 1 drum speaker, got {len(speakers)}"
