from __future__ import annotations

from factorio_display.cli import (
    _build_timer_for_memory,
    _connect_data_ports,
    _should_process_audio,
)
from factorio_display.composer import PortConnection, _layout_components
from factorio_display.logical_blueprint import Endpoint, LogicalBlueprint, LogicalEntity, Network, to_draftsman


def _make_single_frame_video_memory() -> LogicalBlueprint:
    lb = LogicalBlueprint(label="Video Memory: Test")
    dc = LogicalEntity(
        "gate_1",
        "decider-combinator",
        properties={
            "conditions": [{"first": "signal-clock", "op": "=", "constant": 0}],
            "outputs": [{"signal": "signal-A", "copy_count": False, "constant": 1}],
        },
        position=(0, 0),
    )
    lb.add_entity(dc)
    lb.add_network(Network("red_clock", "red", {Endpoint("gate_1", "input")}))
    lb.add_network(Network("red_data", "red", {Endpoint("gate_1", "output")}))
    lb.set_input_port("clock", "red_clock")
    lb.set_output_port("data", "red_data")
    return lb


def _make_multi_single_tick_video_memory() -> LogicalBlueprint:
    lb = LogicalBlueprint(label="Video Memory: TestMulti")
    for i in range(3):
        dc = LogicalEntity(
            f"gate_{i + 1}",
            "decider-combinator",
            properties={
                "conditions": [{"first": "signal-clock", "op": "=", "constant": i}],
                "outputs": [{"signal": "signal-A", "copy_count": False, "constant": i + 1}],
            },
            position=(i * 2, 0),
        )
        lb.add_entity(dc)

    lb.add_network(Network(
        "red_clock", "red", {Endpoint("gate_1", "input"), Endpoint("gate_2", "input"), Endpoint("gate_3", "input")},
    ))
    lb.add_network(Network(
        "red_data", "red", {Endpoint("gate_1", "output"), Endpoint("gate_2", "output"), Endpoint("gate_3", "output")},
    ))
    lb.set_input_port("clock", "red_clock")
    lb.set_output_port("data", "red_data")
    return lb


def test_should_process_audio_image_only_is_false() -> None:
    assert _should_process_audio(videos=[], standalone_audios=[], no_audio=False) is False


def test_should_process_audio_video_or_standalone_audio() -> None:
    assert _should_process_audio(videos=["clip.mp4"], standalone_audios=[], no_audio=False) is True
    assert _should_process_audio(videos=[], standalone_audios=["song.wav"], no_audio=False) is True
    assert _should_process_audio(videos=["clip.mp4"], standalone_audios=["song.wav"], no_audio=True) is False


def test_single_frame_timer_uses_mod_one_and_no_constant_kick() -> None:
    memory_lb = _make_single_frame_video_memory()
    timer = _build_timer_for_memory(memory_lb)

    arith = [e for e in timer.entities.values() if e.type == "arithmetic-combinator"]
    consts = [e for e in timer.entities.values() if e.type == "constant-combinator"]

    # Raw timer + subtick modulo AC only.
    assert len(arith) == 2
    assert len(consts) == 0

    subtick_mod = next(e for e in arith if e.entity_id.endswith("subtick_mod"))
    assert subtick_mod.properties["operation"] == "%"
    assert subtick_mod.properties["second_operand"] == 1


def test_multi_single_tick_frames_set_timer_interval_from_equals() -> None:
    memory_lb = _make_multi_single_tick_video_memory()
    timer = _build_timer_for_memory(memory_lb)

    arith = [e for e in timer.entities.values() if e.type == "arithmetic-combinator"]
    subtick_mod = next(e for e in arith if e.entity_id.endswith("subtick_mod"))

    # Frames at ticks 0,1,2 require modulo interval 3.
    assert subtick_mod.properties["operation"] == "%"
    assert subtick_mod.properties["second_operand"] == 3


def test_timer_ports_reference_live_networks() -> None:
    memory_lb = _make_multi_single_tick_video_memory()
    timer = _build_timer_for_memory(memory_lb)

    for port_name, net_id in timer.input_ports.items():
        assert any(net.network_id == net_id for net in timer.networks), (
            f"Timer input port {port_name!r} points to missing network {net_id!r}"
        )
    for port_name, net_id in timer.output_ports.items():
        assert any(net.network_id == net_id for net in timer.networks), (
            f"Timer output port {port_name!r} points to missing network {net_id!r}"
        )


def test_connect_data_ports_sorts_chunk_indices_numerically() -> None:
    video_lb = LogicalBlueprint(label="Video Memory")
    display_lb = LogicalBlueprint(label="Display")

    for i in range(12):
        video_dc = LogicalEntity(f"video_dc_{i}", "decider-combinator", position=(i, 0))
        display_dc = LogicalEntity(f"display_dc_{i}", "decider-combinator", position=(i, 0))
        video_lb.add_entity(video_dc)
        display_lb.add_entity(display_dc)

        video_net = Network(f"red_video_{i}", "red", {Endpoint(video_dc.entity_id, "output")})
        display_net = Network(f"red_display_{i}", "red", {Endpoint(display_dc.entity_id, "input")})
        video_lb.add_network(video_net)
        display_lb.add_network(display_net)
        video_lb.set_output_port(f"data_{i}", video_net.network_id)
        display_lb.set_input_port(f"data_{i}", display_net.network_id)

    connections: list[PortConnection] = []
    _connect_data_ports(connections, video_lb, display_lb)

    assert [c.from_port for c in connections] == [f"data_{i}" for i in range(12)]
    assert [c.to_port for c in connections] == [f"data_{i}" for i in range(12)]


def test_layout_places_sources_near_sink_for_compact_bridges() -> None:
    sink = LogicalBlueprint(label="Display")
    sink.add_entity(LogicalEntity("sink_ent", "arithmetic-combinator", position=(0, 0)))

    src = LogicalBlueprint(label="Video")
    src.add_entity(LogicalEntity("src_ent", "arithmetic-combinator", position=(0, 0)))

    merged = LogicalBlueprint(label="Merged")
    merged.merge(sink, entity_prefix="display_", network_prefix="display_")
    merged.merge(src, entity_prefix="video_", network_prefix="video_")

    prefixes = {"Display": "display_", "Video": "video_"}
    _layout_components(
        merged,
        prefixes,
        connections=[PortConnection("Video", "out", "Display", "in")],
    )

    sink_pos = merged.entities["display_sink_ent"].position
    src_pos = merged.entities["video_src_ent"].position
    assert sink_pos is not None and src_pos is not None
    assert max(abs(src_pos[0] - sink_pos[0]), abs(src_pos[1] - sink_pos[1])) <= 8, (
        f"Expected source near sink, got src={src_pos}, sink={sink_pos}"
    )


def test_assemble_book_builds_valid_blueprint_book() -> None:
    """The auto-book (small outputs) assembles pieces into a parseable book."""
    from factorio_display.cli import _assemble_book
    from factorio_display.service import DisplayConfig, export_display
    from draftsman.blueprintable import BlueprintBook

    s1 = export_display(DisplayConfig(name="A", width=2, height=2)).blueprint
    s2 = export_display(DisplayConfig(name="B", width=2, height=2)).blueprint
    book = _assemble_book([("a", s1), ("b", s2)], "TestBook")
    assert book, "book assembly produced no output"
    parsed = BlueprintBook.from_string(book)
    assert len(parsed.blueprints) == 2


def test_json_envelope_carries_split_pieces_and_book() -> None:
    """The piecewise JSON envelope exposes pieces + book to the web API."""
    import json
    from types import SimpleNamespace

    from factorio_display.cli import _json_envelope

    env_out = json.dumps({"split_envelope": {
        "blueprint": "0eB",
        "pieces": [
            {"label": "display", "blueprint": "0eD"},
            {"label": "memory_c0_f0", "blueprint": "0eM"},
        ],
        "book": "0eB",
    }})
    args = SimpleNamespace(
        command="encode", input_paths=["x.mp4"], name="N",
        width=10, height=10, rail_mode="auto:0.05", instruments=None,
    )
    envelope = _json_envelope(args, env_out, "", 0)
    result = envelope["result"]
    assert result["split"] is True
    assert result["piece_count"] == 2
    assert [p["label"] for p in result["pieces"]] == ["display", "memory_c0_f0"]
    assert result["book"] == "0eB"


def test_json_envelope_on_error_is_structured(monkeypatch, capsys) -> None:
    """``--json`` must emit a JSON error envelope (never a raw traceback)
    when a subcommand raises — the web API's subprocess runner parses this."""
    import json
    import sys

    import pytest

    from factorio_display import cli

    # On Windows main() re-reads the real command line via GetCommandLineW,
    # which would clobber the monkeypatched argv — neutralise it.
    monkeypatch.setattr(cli, "_fix_argv_encoding", lambda: None)
    monkeypatch.setattr(
        sys, "argv",
        ["factorio-display", "blueprint-to-yaml", "--json", "not-a-blueprint"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["result"]["blueprint"] == ""
    # The error text lands in the envelope's logs instead of a traceback.
    assert envelope["result"]["logs"]


def test_audio_pieces_from_combined_tick_data() -> None:
    """Video+sound piecewise: combined tick data → player + memory pieces."""
    from types import SimpleNamespace

    from factorio_display.cli import _build_audio_pieces_from_tick_data

    tick_data = [[(i + t) % 60 for i in range(48)] for t in range(120)]
    args = SimpleNamespace(name="VS", map_drums=True, rail_mode="auto:0.05", instruments=None)
    pieces = _build_audio_pieces_from_tick_data(tick_data, args, "auto:0.05")
    labels = [lbl for lbl, _ in pieces]
    assert labels == ["player", "memory_r0"], labels
    for _, s in pieces:
        assert s.startswith("0eN")
