"""Tests for the optional Basic Pitch transcriber integration.

These tests mock the subprocess/interpreter discovery so they pass whether or
not a basic-pitch venv is installed — the integration must degrade gracefully
when the optional AI transcription is unavailable.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from factorio_display.audio import basic_pitch_transcriber as bpt


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _save_valid_midi(path: str) -> None:
    """Write a minimal, mido-parseable MIDI file to *path*."""
    import mido

    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    track.append(mido.Message("note_on", note=60, velocity=100, time=0))
    track.append(mido.Message("note_off", note=60, velocity=0, time=480))
    mid.tracks.append(track)
    mid.save(path)


def test_unavailable_when_no_interpreter(monkeypatch):
    monkeypatch.setattr(bpt, "find_basic_pitch_python", lambda: None)
    assert bpt.basic_pitch_available() is False
    assert bpt.transcribe_audio("song.mp3") is None


def test_find_skips_missing_and_bad_interpreters(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # pylint: disable=unused-argument
        calls.append(list(cmd))
        if "good" in cmd[0]:
            return _FakeCompleted(0)
        return _FakeCompleted(1)

    good = str(tmp_path / "good_python.exe")
    Path(good).write_text("", encoding="utf-8")
    bad = str(tmp_path / "bad_python.exe")
    Path(bad).write_text("", encoding="utf-8")

    monkeypatch.setattr(bpt, "_candidate_paths", lambda: [bad, good])
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(bpt, "_cached_python", False)

    assert bpt.find_basic_pitch_python() == good
    assert len(calls) == 2


def test_transcribe_success_caches(monkeypatch, tmp_path):
    fake_midi = tmp_path / "song_basic_pitch.mid"
    _save_valid_midi(str(fake_midi))
    audio_file = tmp_path / "unique_song.mp3"
    audio_file.write_text("audio", encoding="utf-8")

    monkeypatch.setattr(bpt, "find_basic_pitch_python", lambda: "py.exe")
    monkeypatch.setattr(
        bpt, "_cached_python", "py.exe",
    )

    def fake_run(cmd, **kwargs):  # pylint: disable=unused-argument
        # cmd = [py, worker, audio_path, output_dir]
        out_dir = Path(cmd[3])
        out_dir.mkdir(parents=True, exist_ok=True)
        _save_valid_midi(str(out_dir / "unique_song_basic_pitch.mid"))
        return _FakeCompleted(0, stdout=str(out_dir / "unique_song_basic_pitch.mid"))

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = bpt.transcribe_audio(str(audio_file))
    assert result is not None
    assert Path(result).exists()
    assert result.endswith(".mid")

    # Second call should hit the cache (no subprocess).
    calls: list[list[str]] = []

    def fake_run_fail(cmd, **kwargs):  # pylint: disable=unused-argument
        calls.append(cmd)
        raise AssertionError("should not invoke subprocess on cache hit")

    monkeypatch.setattr(subprocess, "run", fake_run_fail)
    cached = bpt.transcribe_audio(str(audio_file))
    assert cached == result
    assert calls == []


def test_corrupt_cache_entry_is_discarded_and_retranscribed(monkeypatch, tmp_path):
    """A cached MIDI that exists but does not parse (the 4-byte ``MThd``
    stub from docs/eval-midi/findings.md §4.3) must be discarded and
    re-transcribed, not returned — returning it crashed the re-encode with
    ``mido.EOFError`` (no STFT fallback)."""
    audio_file = tmp_path / "song_corrupt_cache.mp3"
    audio_file.write_text("audio", encoding="utf-8")
    monkeypatch.setattr(bpt, "find_basic_pitch_python", lambda: "py.exe")
    monkeypatch.setattr(bpt, "_cached_python", "py.exe")

    # Seed the cache with a corrupt (unparseable) MIDI + its marker.
    ckey = bpt._cache_key(str(audio_file))
    corrupt = bpt._cache_dir_for(str(audio_file))
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(b"MThd")  # the broken 4-byte stub from the findings
    bpt._cache_put(ckey, str(corrupt))

    ran: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # pylint: disable=unused-argument
        ran.append(cmd)
        out_dir = Path(cmd[3])
        out_dir.mkdir(parents=True, exist_ok=True)
        _save_valid_midi(str(out_dir / "song_corrupt_cache_basic_pitch.mid"))
        return _FakeCompleted(0, stdout=str(out_dir / "song_corrupt_cache_basic_pitch.mid"))

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = bpt.transcribe_audio(str(audio_file))
    assert ran, "corrupt cache entry must trigger a re-transcription"
    assert result is not None
    assert Path(result).exists()
    assert result.endswith(".mid")


def test_transcribe_failure_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(bpt, "find_basic_pitch_python", lambda: "py.exe")
    monkeypatch.setattr(bpt, "_cached_python", "py.exe")

    def fake_run(cmd, **kwargs):  # pylint: disable=unused-argument
        return _FakeCompleted(1, stderr="model not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert bpt.transcribe_audio("song.mp3") is None
