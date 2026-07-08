from __future__ import annotations

from factorio_display.cli import (
    _build_timer_for_memory,
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
