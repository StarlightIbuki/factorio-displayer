"""Optional Basic Pitch integration for non-MIDI audio.

Basic Pitch (Spotify) transcribes raw audio into musical MIDI notes — far
better than the STFT→loudness→note path for loud/dense audio.  It requires
TensorFlow, which has **no Python 3.14 wheels**, so it cannot run inside the
main interpreter.  Instead we shell out to a separate interpreter that has
``basic-pitch`` installed (e.g. the project's ``.venv-bp`` with Python
3.11/3.13).

This module keeps Basic Pitch **optional**: every function returns ``None`` /
``False`` cleanly when no basic-pitch interpreter can be found, and the
encoder falls back to the built-in STFT analysis.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ..cache_paths import cache_namespace_dir


def _file_identity(path: str) -> str:
    """Stable identity for an input file: resolved path + mtime + size."""
    try:
        st = Path(path).stat()
        return f"{Path(path).resolve()}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        return path

# Environment variable that overrides the interpreter used for transcription.
_ENV_VAR = "FACTORIO_BASIC_PITCH_PYTHON"

# Relative candidate locations for a basic-pitch venv.
_DEFAULT_CANDIDATES = (
    ".venv-bp/Scripts/python.exe",  # Windows
    ".venv-bp/bin/python",          # POSIX
)

_worker_path = Path(__file__).with_name("basic_pitch_worker.py")

_cached_python: str | None | bool = False  # False = not yet probed, None = none


def _candidate_paths() -> list[str]:
    """Return candidate absolute interpreter paths to probe."""
    result: list[str] = []
    env = os.environ.get(_ENV_VAR)
    if env:
        result.append(env)
    # Repo root is two levels above this package (src/factorio_display/audio/…).
    root = Path(__file__).resolve().parents[2]
    for rel in _DEFAULT_CANDIDATES:
        result.append(str(root / rel))
    # Reuse the interpreter already running the app if it can import basic_pitch.
    result.append(sys.executable)
    return result


def basic_pitch_available() -> bool:
    """Return True when a usable basic-pitch interpreter is available."""
    return find_basic_pitch_python() is not None


def find_basic_pitch_python() -> str | None:
    """Locate a Python interpreter that can import ``basic_pitch``."""
    global _cached_python  # pylint: disable=global-statement
    if _cached_python is not False:
        return _cached_python

    for cand in _candidate_paths():
        if not cand:
            continue
        if not Path(cand).exists():
            continue
        try:
            probe = subprocess.run(
                [cand, "-c", "import basic_pitch"],
                capture_output=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            _cached_python = cand
            return cand

    _cached_python = None
    return None


def transcribe_audio(audio_path: str, *, cache: bool = True) -> str | None:
    """Transcribe *audio_path* to a MIDI file via Basic Pitch.

    Returns the path to the generated ``.mid`` file, or None when Basic Pitch
    is unavailable or the transcription fails.  The MIDI is cached under the
    audio-cache namespace keyed by file identity, so re-encodes skip the
    neural net.
    """
    py = find_basic_pitch_python()
    if py is None:
        return None

    # Cache the produced MIDI path.
    _ckey = _cache_key(audio_path)
    cached = _cache_get(_ckey)
    if cached is not None:
        if Path(cached).exists():
            sys.stderr.write(f"Using cached Basic Pitch MIDI: {cached}\n")
            return cached
        _cache_del(_ckey)

    try:
        tmpdir = tempfile.mkdtemp(prefix="fd_basic_pitch_")
        # Basic Pitch prints progress/warnings containing non-ASCII (e.g. the
        # U+1F6A8 emoji in its tqdm output).  On Windows the default console
        # codepage (GBK/cp936) cannot encode those, raising UnicodeEncodeError
        # inside the subprocess.  Force UTF-8 I/O so transcription succeeds.
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        run = subprocess.run(
            [py, str(_worker_path), audio_path, tmpdir],
            capture_output=True, text=True, timeout=600, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"Basic Pitch subprocess error: {exc}\n")
        return None

    if run.returncode != 0:
        sys.stderr.write(
            f"Basic Pitch transcription failed ({run.returncode}): "
            f"{(run.stderr or run.stdout or '').strip()[:500]}\n"
        )
        return None

    midi_path = (run.stdout or "").strip().splitlines()[-1] if run.stdout else ""
    if not midi_path or not Path(midi_path).exists():
        sys.stderr.write("Basic Pitch produced no MIDI output.\n")
        return None

    # Move into a stable cache location so the temp dir can be discarded.
    if cache:
        stable = _cache_dir_for(audio_path)
        stable.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(midi_path, stable)
        except OSError:
            return midi_path
        _cache_put(_ckey, str(stable))
        sys.stderr.write(f"Basic Pitch transcribed: {stable}\n")
        return str(stable)

    sys.stderr.write(f"Basic Pitch transcribed: {midi_path}\n")
    return midi_path


# ── tiny cache helpers (audio-cache namespace) ─────────────────────────

_CACHE_NS = cache_namespace_dir("basic_pitch")


def _cache_key(audio_path: str) -> str:
    return hashlib.sha256(
        f"{_file_identity(audio_path)}|worker={_worker_path.name}".encode()
    ).hexdigest()[:24]


def _cache_dir_for(audio_path: str) -> Path:
    return _CACHE_NS / f"{_cache_key(audio_path)}.mid"


def _cache_get(key: str) -> str | None:
    if not _CACHE_NS.exists():
        return None
    marker = _CACHE_NS / f"{key}.txt"
    if marker.exists():
        return marker.read_text(encoding="utf-8").strip() or None
    return None


def _cache_put(key: str, path: str) -> None:
    _CACHE_NS.mkdir(parents=True, exist_ok=True)
    (_CACHE_NS / f"{key}.txt").write_text(path, encoding="utf-8")


def _cache_del(key: str) -> None:
    marker = _CACHE_NS / f"{key}.txt"
    if marker.exists():
        marker.unlink()
