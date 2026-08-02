"""Typed service layer for factorio-display.

Decouples the encode/build operations from both the argparse CLI and the HTTP
API by exposing typed configuration objects and result types.  The CLI
(:mod:`factorio_display.cli`) and the web API (:mod:`factorio_display.api`)
both build on this layer, so option defaults and output shapes live in exactly
one place.

Design notes
------------
- The long-running :class:`MediaConfig` ``encode`` operation executes the
  existing, battle-tested CLI pipeline **in a subprocess** with ``--json``
  output.  This avoids capturing the process-global ``sys.stdout``/``sys.stderr``
  inside a multi-threaded web server and gives crash isolation (a worker that
  dies cannot take the server down).
- The fast, stateless builders (display / audio decoder / logical / decode)
  run **in-process** and return result objects directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ═══════════════════════════════════════════════════════════════════════
# Result types
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class BuildResult:
    """Result of a fast, stateless builder (display / decoder / logical / decode)."""

    text: str = ""
    name: str = ""
    format: str = "blueprint"  # blueprint | toml | yaml
    entity_count: int | None = None
    instruments: list[str] = field(default_factory=list)
    logs: str = ""

    @property
    def blueprint(self) -> str:
        return self.text if self.format == "blueprint" else ""


@dataclass
class MediaResult:
    """Structured result of an encode operation (video / audio / MIDI / image)."""

    blueprint: str = ""
    name: str = "Media Data"
    kind: str = "video"  # video | audio | midi | image | combined | unknown
    dimensions: tuple[int, int] | None = None
    total_ticks: int = 0
    entity_count: int | None = None
    instruments: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    logs: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "blueprint": self.blueprint,
            "name": self.name,
            "kind": self.kind,
            "dimensions": list(self.dimensions) if self.dimensions else None,
            "total_ticks": self.total_ticks,
            "entity_count": self.entity_count,
            "instruments": self.instruments,
            "warnings": self.warnings,
            "artifacts": self.artifacts,
            "logs": self.logs,
        }


# ═══════════════════════════════════════════════════════════════════════
# Media / encode config — mirrors `factorio-display encode`
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class MediaConfig:
    """Options for the ``encode`` operation (video / audio / MIDI / image).

    Field names mirror the CLI flags of ``factorio-display encode``.
    """

    # inputs + naming
    inputs: list[str] = field(default_factory=list)
    name: str = "Media Data"

    # video
    skip: int = 1
    fps: float = 0.0
    adaptive: bool = False
    threshold: float = 0.01
    deduplicate: bool = False
    width: int | None = None
    height: int | None = None
    time_chunks: int = 1
    chunk_workers: int | None = None
    output_chunks_dir: str | None = None
    deduplicate_cross: bool = False

    # audio / MIDI translation
    ticks_per_beat: int = 30
    boost_melody: float = 1.0
    velocity_scale: float = 1.0
    attack_ticks: int = 10
    decay_ticks: int = 10
    sustain_level: float = 1.0
    release_ticks: int = 10
    attack_curve: float = 1.0
    decay_curve: float = 1.0
    release_curve: float = 1.0
    rail_mode: str = "auto:0.05"
    map_drums: bool = True
    drum_gain: float = 0.25
    use_global_shift: bool = True

    # audio file (non-MIDI)
    use_basic_pitch: bool = True
    activation_threshold: float = 0.0
    midi_threshold: float = 0.05
    condense_midi: bool = True
    max_polyphony: int = 0

    # composition / output
    audio_only: bool = False
    no_audio: bool = False
    attach_player: bool = True
    power: str | None = "substation"  # small | medium | substation | none
    progress_bar: bool = False
    use_cache: bool = True
    debug_toml_dir: str | None = None
    output_midi_path: str | None = None
    processed_midi_path: str | None = None
    debug_json_path: str | None = None
    output_path: str | None = None  # CLI-only: write blueprint to a file

    def to_argv(self, *, include_json: bool = True) -> list[str]:
        """Build the ``factorio-display encode`` argv (after ``python -m factorio_display``)."""
        a: list[str] = ["encode"]
        if include_json:
            a.append("--json")
        a += ["--name", self.name]
        # video
        a += ["--skip", str(self.skip)]
        a += ["--fps", str(self.fps)]
        if self.adaptive:
            a.append("--adaptive")
        a += ["--threshold", str(self.threshold)]
        if self.deduplicate:
            a.append("--deduplicate")
        if self.width is not None:
            a += ["--width", str(self.width)]
        if self.height is not None:
            a += ["--height", str(self.height)]
        a += ["--time-chunks", str(self.time_chunks)]
        if self.chunk_workers is not None:
            a += ["--chunk-workers", str(self.chunk_workers)]
        if self.output_chunks_dir:
            a += ["--output-chunks", self.output_chunks_dir]
        if self.deduplicate_cross:
            a.append("--deduplicate-cross")
        # audio / MIDI
        a += ["--ticks-per-beat", str(self.ticks_per_beat)]
        a += ["--boost-melody", str(self.boost_melody)]
        a += ["--velocity-scale", str(self.velocity_scale)]
        a += ["--attack-ticks", str(self.attack_ticks)]
        a += ["--decay-ticks", str(self.decay_ticks)]
        a += ["--sustain-level", str(self.sustain_level)]
        a += ["--release-ticks", str(self.release_ticks)]
        a += ["--attack-curve", str(self.attack_curve)]
        a += ["--decay-curve", str(self.decay_curve)]
        a += ["--release-curve", str(self.release_curve)]
        a += ["--rail-mode", self.rail_mode]
        if self.map_drums:
            a.append("--map-drums")
        a += ["--drum-gain", str(self.drum_gain)]
        if not self.use_global_shift:
            a.append("--no-global-shift")
        # audio file (non-MIDI)
        if not self.use_basic_pitch:
            a.append("--no-ai-transcribe")
        a += ["--activation-threshold", str(self.activation_threshold)]
        a += ["--midi-threshold", str(self.midi_threshold)]
        if not self.condense_midi:
            a.append("--no-condense")
        a += ["--max-polyphony", str(self.max_polyphony)]
        # composition / output
        if self.audio_only:
            a.append("--audio-only")
        if self.no_audio:
            a.append("--no-audio")
        if not self.attach_player:
            a.append("--no-attach-player")
        if self.power is not None:
            a += ["--power", self.power]
        if self.progress_bar:
            a.append("--progress-bar")
        if not self.use_cache:
            a.append("--no-cache")
        if self.debug_toml_dir:
            a += ["--debug-toml", self.debug_toml_dir]
        if self.output_midi_path:
            a += ["--output-midi", self.output_midi_path]
        if self.processed_midi_path:
            a += ["--processed-midi", self.processed_midi_path]
        if self.debug_json_path:
            a += ["--debug-json", self.debug_json_path]
        if self.output_path:
            a += ["-o", self.output_path]
        a += list(self.inputs)
        return a

    @property
    def kind(self) -> str:
        """Classify the primary input (video / audio / midi / image / unknown)."""
        from .cli import _classify_input  # pylint: disable=import-outside-toplevel

        for p in self.inputs:
            k = _classify_input(p)
            if k != "unknown":
                return k
        return "unknown"


# ═══════════════════════════════════════════════════════════════════════
# Fast builder configs
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class DisplayConfig:
    name: str = "Video Display"
    width: int | None = None
    height: int | None = None
    power: str | None = "substation"


@dataclass
class AudioDecoderConfig:
    name: str = "Audio Decoder"
    instruments: list[str] = field(default_factory=lambda: ["piano"])
    power: str | None = "substation"
    format: str = "blueprint"  # blueprint | logical
    map_drums: bool = True


@dataclass
class LogicalConfig:
    name: str = "Audio Decoder"
    instrument: str = "piano"


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def count_entities(blueprint_str: str) -> int | None:
    """Return the entity count of a Factorio blueprint string, or None if unparseable.

    Blueprint strings are the compressed form (e.g. ``0eNq...``).  TOML/YAML
    logical dumps cannot be counted this way and return None.
    """
    s = (blueprint_str or "").strip()
    if not s:
        return 0
    if not s.startswith("0") or len(s) < 50:
        return None
    try:
        from draftsman.blueprintable import Blueprint  # type: ignore[import-untyped]
        bp = Blueprint.from_string(s)
        return len(bp.entities)
    except Exception:  # pylint: disable=broad-exception-caught
        return None


_TICK_RE = re.compile(r"over (\d+) tick", re.IGNORECASE)
_TICK_RE2 = re.compile(r"\b(\d+) ticks?\b", re.IGNORECASE)


def _extract_ticks_from_logs(logs: str) -> int:
    ticks: list[int] = []
    for m in _TICK_RE.finditer(logs):
        ticks.append(int(m.group(1)))
    if ticks:
        return max(ticks)
    for m in _TICK_RE2.finditer(logs):
        ticks.append(int(m.group(1)))
    return max(ticks) if ticks else 0


_WARN_RE = re.compile(r"^(?:Warning|Note|  \[audio\]|\[debug\])", re.MULTILINE)


def _extract_warnings(logs: str) -> list[str]:
    return [ln.strip() for ln in logs.splitlines() if _WARN_RE.match(ln.strip())]


def parse_media_json(text: str) -> MediaResult:
    """Parse the ``--json`` envelope emitted by the CLI encode pipeline."""
    import json

    data = json.loads(text)
    result: dict[str, Any] = data.get("result", {}) if isinstance(data, dict) else {}
    blueprint = str(result.get("blueprint", "") or "")
    logs = str(result.get("logs", "") or "")
    dims = result.get("dimensions")
    return MediaResult(
        blueprint=blueprint,
        name=str(result.get("name", "") or ""),
        kind=str(result.get("kind", "unknown") or "unknown"),
        dimensions=tuple(dims) if isinstance(dims, list) and len(dims) == 2 else None,
        total_ticks=int(result.get("total_ticks", 0) or 0),
        entity_count=result.get("entity_count"),
        instruments=list(result.get("instruments", []) or []),
        warnings=list(result.get("warnings", []) or _extract_warnings(logs)),
        artifacts=list(result.get("artifacts", []) or []),
        logs=logs,
    )


# ═══════════════════════════════════════════════════════════════════════
# Fast, stateless builders (run in-process)
# ═══════════════════════════════════════════════════════════════════════


def export_display(cfg: DisplayConfig) -> BuildResult:
    """Build the physical video display grid blueprint."""
    from .video.player_blueprint import build_display  # pylint: disable=import-outside-toplevel
    from .video.encoder import _to_fixed_string  # pylint: disable=import-outside-toplevel

    bp = build_display(name=cfg.name, width=cfg.width, height=cfg.height)
    text = _to_fixed_string(bp)
    return BuildResult(
        text=text,
        name=cfg.name,
        format="blueprint",
        entity_count=count_entities(text),
    )


def export_audio_decoder(cfg: AudioDecoderConfig) -> BuildResult:
    """Build the audio decoder blueprint (or logical TOML)."""
    from . import CLOCK_SIGNAL  # pylint: disable=import-outside-toplevel
    from .logical_blueprint import to_toml  # pylint: disable=import-outside-toplevel

    if cfg.format == "logical":
        from .audio.player_blueprint import (  # pylint: disable=import-outside-toplevel
            build_multi_rail_decoder_logical,
        )
        lb = build_multi_rail_decoder_logical(
            name=cfg.name,
            instruments=cfg.instruments,
            clock_signal=CLOCK_SIGNAL,
            map_drums=cfg.map_drums,
        )
        return BuildResult(text=to_toml(lb), name=cfg.name, format="toml", instruments=list(cfg.instruments))

    from .audio.player_blueprint import build_multi_rail_decoder  # pylint: disable=import-outside-toplevel

    # build_multi_rail_decoder returns the final blueprint string directly.
    text = build_multi_rail_decoder(
        name=cfg.name,
        instruments=cfg.instruments,
        clock_signal=CLOCK_SIGNAL,
        map_drums=cfg.map_drums,
    )
    return BuildResult(
        text=text,
        name=cfg.name,
        format="blueprint",
        entity_count=count_entities(text),
        instruments=list(cfg.instruments),
    )


def export_logical(cfg: LogicalConfig) -> BuildResult:
    """Export the audio decoder as a logical blueprint (TOML)."""
    from . import CLOCK_SIGNAL  # pylint: disable=import-outside-toplevel
    from .audio.player_blueprint import (  # pylint: disable=import-outside-toplevel
        build_audio_decoder_logical,
    )
    from .logical_blueprint import to_toml  # pylint: disable=import-outside-toplevel

    lb = build_audio_decoder_logical(
        name=cfg.name,
        instrument=cfg.instrument,
        clock_signal=CLOCK_SIGNAL,
        map_drums=True,
    )
    return BuildResult(text=to_toml(lb), name=cfg.name, format="toml")


def decode_blueprint(bp_string: str) -> BuildResult:
    """Convert a blueprint string into logical YAML."""
    from .logical_blueprint import blueprint_string_to_yaml  # pylint: disable=import-outside-toplevel

    yaml_text = blueprint_string_to_yaml((bp_string or "").strip())
    return BuildResult(text=yaml_text, name="Decoded Blueprint", format="yaml")
