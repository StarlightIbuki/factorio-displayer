"""Tests for the typed service layer (factorio_display.service)."""

from __future__ import annotations

import pytest

from factorio_display import service


def test_media_config_to_argv_maps_all_groups() -> None:
    cfg = service.MediaConfig(
        inputs=["a.mp4"],
        name="Test",
        fps=30.0,
        adaptive=True,
        width=28,
        height=26,
        rail_mode="piano,bass",
        map_drums=False,
        power="none",
        progress_bar=True,
    )
    argv = cfg.to_argv()
    assert argv[0] == "encode"
    assert "--json" in argv
    assert argv[-1] == "a.mp4"
    assert "--fps" in argv and str(cfg.fps) in argv
    assert "--adaptive" in argv
    assert "--rail-mode" in argv and "piano,bass" in argv
    assert "--map-drums" not in argv
    assert "--power" in argv and "none" in argv
    assert "--progress-bar" in argv


def test_media_config_kind() -> None:
    assert service.MediaConfig(inputs=["song.mid"]).kind == "midi"
    assert service.MediaConfig(inputs=["clip.mp4"]).kind == "video"
    assert service.MediaConfig(inputs=["note.wav"]).kind == "audio"


def test_parse_media_json() -> None:
    payload = (
        '{"version": "0.1.0", "result": {"command": "encode", "blueprint": "0eNqjAAAA", '
        '"name": "M", "kind": "image", "dimensions": [4, 4], "total_ticks": 1, '
        '"entity_count": 3, "instruments": [], "warnings": ["Note: x"], "logs": "hi\\n"}}'
    )
    res = service.parse_media_json(payload)
    assert res.blueprint == "0eNqjAAAA"
    assert res.dimensions == (4, 4)
    assert res.total_ticks == 1
    assert res.entity_count == 3


def test_count_entities_none_for_non_blueprint() -> None:
    assert service.count_entities("not a blueprint") is None
    assert service.count_entities("") == 0


def test_export_display_returns_blueprint_with_count() -> None:
    res = service.export_display(service.DisplayConfig(name="D", width=8, height=8))
    assert res.format == "blueprint"
    assert res.blueprint.startswith("0eN")
    assert res.entity_count == 64


def test_export_audio_decoder_blueprint() -> None:
    res = service.export_audio_decoder(service.AudioDecoderConfig(instruments=["piano"]))
    assert res.format == "blueprint"
    assert res.blueprint.startswith("0eN")
    assert res.instruments == ["piano"]


def test_export_audio_decoder_logical() -> None:
    res = service.export_audio_decoder(
        service.AudioDecoderConfig(instruments=["piano"], format="logical")
    )
    assert res.format == "toml"
    assert "[[entity]]" in res.text


def test_export_logical() -> None:
    res = service.export_logical(service.LogicalConfig(instrument="piano"))
    assert res.format == "toml"
    assert "[[entity]]" in res.text


def test_decode_blueprint_roundtrip() -> None:
    display = service.export_display(service.DisplayConfig(width=4, height=4))
    res = service.decode_blueprint(display.blueprint)
    assert res.format == "yaml"
    assert "entity" in res.text or "entities" in res.text


def test_decode_blueprint_raises_on_garbage() -> None:
    with pytest.raises(Exception):
        service.decode_blueprint("definitely-not-a-blueprint")
