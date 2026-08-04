"""Pydantic request/response models for the factorio-display API.

These mirror the service-layer configs (one source of truth for options); the
service layer stays dependency-free, and the API converts Pydantic models to
:mod:`factorio_display.service` dataclasses.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EncodeOptions(BaseModel):
    """Options for the ``encode`` operation (video / audio / MIDI / image)."""

    name: str = "Media Data"
    # video
    skip: int = Field(1, ge=1)
    fps: float = Field(0.0, ge=0.0)
    adaptive: bool = False
    threshold: float = 0.01
    deduplicate: bool = False
    width: int | None = Field(None, ge=1)
    height: int | None = Field(None, ge=1)
    time_chunks: int = Field(1, ge=1)
    chunk_workers: int | None = Field(None, ge=1)
    deduplicate_cross: bool = False
    # audio / MIDI translation
    ticks_per_beat: int = Field(30, ge=1)
    boost_melody: float = 1.0
    velocity_scale: float = 1.0
    attack_ticks: int = Field(10, ge=0)
    decay_ticks: int = Field(10, ge=0)
    sustain_level: float = Field(1.0, ge=0.0, le=1.0)
    release_ticks: int = Field(10, ge=0)
    attack_curve: float = 1.0
    decay_curve: float = 1.0
    release_curve: float = 1.0
    rearticulation_ticks: int = Field(2, ge=0)
    rail_mode: str = "auto:0.05"
    map_drums: bool = True
    drum_gain: float = Field(0.25, ge=0.0, le=1.0)
    use_global_shift: bool = True
    # audio file (non-MIDI)
    use_basic_pitch: bool = True
    activation_threshold: float = 0.0
    midi_threshold: float = 0.05
    condense_midi: bool = True
    max_polyphony: int = Field(0, ge=0)
    # composition / output
    audio_only: bool = False
    no_audio: bool = False
    attach_player: bool = True
    power: str | None = Field("substation", pattern="^(small|medium|substation|none)$")
    progress_bar: bool = False
    use_cache: bool = True
    result_format: Literal["blueprint", "toml", "yaml", "json"] = "blueprint"
    # piecewise (chunked) output — the default.  ``all_in_one`` opts into the
    # legacy single merged blueprint (not recommended).
    all_in_one: bool = False
    book: bool = False
    no_book: bool = False
    output_dir: str | None = None

    def to_config_dict(self) -> dict:
        d = self.model_dump()
        d.pop("result_format", None)
        return d


class JobCreate(BaseModel):
    """Body for ``POST /api/v1/jobs`` (currently supports ``type=encode``)."""

    type: Literal["encode"] = "encode"
    inputs: list[str] = Field(default_factory=list, description="Upload ids to encode")
    options: EncodeOptions = Field(default_factory=EncodeOptions)
    callback_url: str | None = None


class DisplayRequest(BaseModel):
    name: str = "Video Display"
    width: int | None = Field(None, ge=1)
    height: int | None = Field(None, ge=1)
    power: str | None = Field("substation", pattern="^(small|medium|substation|none)$")


class AudioDecoderRequest(BaseModel):
    name: str = "Audio Decoder"
    instruments: list[str] = Field(default_factory=lambda: ["piano"])
    power: str | None = Field("substation", pattern="^(small|medium|substation|none)$")
    format: Literal["blueprint", "logical"] = "blueprint"


class LogicalRequest(BaseModel):
    name: str = "Audio Decoder"
    instrument: str = "piano"


class DecodeRequest(BaseModel):
    blueprint: str


class BugReportRequest(BaseModel):
    """Optional user-supplied context attached to a bug report."""

    comment: str = ""
    contact: str = ""


class UploadOut(BaseModel):
    upload_id: str
    name: str
    size_bytes: int
    media_type: str
    path: str
    created_at: float
    probe: dict | None = None


class BuildOut(BaseModel):
    blueprint: str = ""
    text: str = ""
    format: str = "blueprint"  # blueprint | toml | yaml
    name: str = ""
    entity_count: int | None = None
    instruments: list[str] = Field(default_factory=list)


class JobOut(BaseModel):
    job_id: str
    type: str
    name: str
    status: str
    progress: dict = Field(default_factory=dict)
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    result: dict | None = None
    result_url: str | None = None


class JobListOut(BaseModel):
    jobs: list[JobOut]
    total: int
    limit: int
    offset: int


class HealthOut(BaseModel):
    status: str = "ok"
    version: str
    workers: dict
    uptime_seconds: float


class CapabilitiesOut(BaseModel):
    version: str
    display: dict
    input_extensions: dict
    instruments: list[str]
    rail_modes: list[str]
    result_formats: list[str]
    power_types: list[str]
    auth: dict = Field(default_factory=dict)  # {"github": {...} | None}
