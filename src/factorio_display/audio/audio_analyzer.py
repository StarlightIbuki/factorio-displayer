"""Audio analyzer — converts arbitrary audio (WAV/FLAC/OGG/MP3) into
a full-spectrum loudness array suitable for both MIDI export and
Factorio memory encoding.

Pipeline
--------
1. Read PCM samples from an audio file (via ``soundfile``).
2. Compute STFT with overlapping Hann windows (window = 4 game ticks
   ≈ 66.7 ms, hop = 1 tick).
3. Map each FFT frequency bin to the closest MIDI note (0–127) and sum
   magnitudes per note per tick.
4. Normalise to [0.0, 1.0] per tick.

All internal values are ``float64`` to avoid precision loss.
"""

from __future__ import annotations

import math
import sys
from typing import Optional, Sequence

import numpy as np

# ── constants ──────────────────────────────────────────────────────────

# One Factorio tick = 1/60 s ≈ 16.667 ms
TICK_DURATION_S: float = 1.0 / 60.0

# STFT window: 4 ticks (~66.7 ms).  Longer window gives better frequency
# resolution (~15 Hz at 44100 Hz) so adjacent semitones are distinguishable.
FFT_WINDOW_TICKS: int = 4
FFT_WINDOW_S: float = FFT_WINDOW_TICKS * TICK_DURATION_S

# Hop = 1 tick → output at game tick rate (60 Hz).
# Overlap = (window - hop) / window = 3/4 = 75 %.
HOP_S: float = TICK_DURATION_S

# Number of MIDI notes (0–127).
MIDI_NOTE_COUNT: int = 128

# MIDI note for A4 (440 Hz).
MIDI_A4: int = 69

# Factorio game range: F3 (MIDI 53) to E7 (MIDI 100).
GAME_MIDI_MIN: int = 53
GAME_MIDI_MAX: int = 100
GAME_PITCH_COUNT: int = 48  # 12 semitones × 4 octaves


# ── frequency → MIDI note mapping ──────────────────────────────────────


def _generate_note_freq_ranges() -> list[tuple[float, float, float]]:
    """Return a list of ``(low, center, high)`` frequency ranges for MIDI notes 0–127.

    Adjacent ranges partition the frequency spectrum so every frequency
    maps to exactly one MIDI note.  Center = theoretical pitch frequency;
    low = midpoint between previous and current center;
    high = midpoint between current and next center.
    """
    # Center frequencies: 440 * 2^((n - 69) / 12)
    centers = np.array(
        [440.0 * (2.0 ** ((n - MIDI_A4) / 12.0)) for n in range(MIDI_NOTE_COUNT)],
        dtype=np.float64,
    )

    ranges: list[tuple[float, float, float]] = []
    for n in range(MIDI_NOTE_COUNT):
        if n == 0:
            low = centers[0] / (2.0 ** (1.0 / 24.0))  # half semitone below
        else:
            low = (centers[n] + centers[n - 1]) / 2.0

        if n == MIDI_NOTE_COUNT - 1:
            high = centers[-1] * (2.0 ** (1.0 / 24.0))  # half semitone above
        else:
            high = (centers[n] + centers[n + 1]) / 2.0

        ranges.append((float(low), float(centers[n]), float(high)))

    return ranges


# Pre-compute at module load.
_NOTE_FREQ_RANGES: list[tuple[float, float, float]] = _generate_note_freq_ranges()


def _freq_to_midi_note(freq: float) -> int:
    """Map a frequency in Hz to the closest MIDI note number (0–127).

    Uses the pre-computed frequency partition so every frequency maps
    to exactly one note.  Values below the lowest range return 0; values
    above the highest return 127.
    """
    if freq <= _NOTE_FREQ_RANGES[0][2]:
        return 0
    if freq >= _NOTE_FREQ_RANGES[-1][0]:
        return 127
    for n, (low, _center, high) in enumerate(_NOTE_FREQ_RANGES):
        if low <= freq <= high:
            return n
    # Fallback — should not happen with contiguous ranges.
    return int(round(12.0 * math.log2(freq / 440.0) + MIDI_A4))


# ── test tone generation (for unit tests) ──────────────────────────────


def make_tone(
    freq: float,
    duration_s: float,
    sample_rate: int = 44100,
    amplitude: float = 1.0,
) -> np.ndarray:
    """Generate a sine wave tone as a 1-D float64 array."""
    num_samples = int(duration_s * sample_rate)
    t = np.linspace(0.0, duration_s, num_samples, endpoint=False, dtype=np.float64)
    return (amplitude * np.sin(2.0 * math.pi * freq * t)).astype(np.float64)


def make_chord(
    freqs: list[float],
    duration_s: float,
    sample_rate: int = 44100,
    amplitudes: Optional[list[float]] = None,
) -> np.ndarray:
    """Generate a chord (sum of sine waves) as a 1-D float64 array."""
    if amplitudes is None:
        amplitudes = [1.0 / len(freqs)] * len(freqs)
    num_samples = int(duration_s * sample_rate)
    result = np.zeros(num_samples, dtype=np.float64)
    t = np.linspace(0.0, duration_s, num_samples, endpoint=False, dtype=np.float64)
    for f, a in zip(freqs, amplitudes):
        result += (a * np.sin(2.0 * math.pi * f * t)).astype(np.float64)
    return result


# ── main analysis ──────────────────────────────────────────────────────


def audio_to_loudness(
    samples: np.ndarray,
    sample_rate: int,
    *,
    activation_threshold: float = 0.0,
) -> list[list[float]]:
    """Convert PCM audio samples to a full-spectrum loudness array.

    Parameters
    ----------
    samples : np.ndarray
        1-D (mono) or 2-D (stereo, shape ``(N, channels)``) float64 array.
    sample_rate : int
        Sample rate in Hz (e.g. 44100, 48000).
    activation_threshold : float
        Magnitudes below this fraction of the per-tick maximum are zeroed.
        Default 0.0 (no thresholding).

    Returns
    -------
    list[list[float]]
        ``loudness[tick][midi_note]`` where ``midi_note`` is 0..127.
        Each value is in [0.0, 1.0].  Length = number of game ticks
        covered by the audio.
    """
    if samples.size == 0:
        return []

    # Downmix stereo → mono
    if samples.ndim > 1:
        samples = samples.mean(axis=1).astype(np.float64)
    else:
        samples = samples.astype(np.float64, copy=False)

    # STFT parameters
    window_samples = max(1, int(FFT_WINDOW_S * sample_rate))
    hop_samples = max(1, int(HOP_S * sample_rate))

    # Pad to at least one window
    if len(samples) < window_samples:
        padded = np.zeros(window_samples, dtype=np.float64)
        padded[:len(samples)] = samples
        samples = padded

    num_frames = max(1, (len(samples) - window_samples) // hop_samples + 1)

    # Hann window
    window = np.hanning(window_samples).astype(np.float64)

    # Pre-compute which MIDI note each FFT bin maps to (for the positive
    # frequencies only — bins 0..N/2).
    n_fft_bins = window_samples // 2 + 1
    bin_freqs = np.fft.rfftfreq(window_samples, d=1.0 / sample_rate)
    bin_to_note = np.array([_freq_to_midi_note(f) for f in bin_freqs], dtype=np.int32)

    # Accumulate magnitude per MIDI note per frame.
    # Use a list of numpy arrays for speed, then convert.
    loudness_frames: list[list[float]] = []

    for frame_idx in range(num_frames):
        start = frame_idx * hop_samples
        end = start + window_samples
        chunk = samples[start:end] * window

        # Real FFT
        spectrum = np.abs(np.fft.rfft(chunk))

        # Accumulate into MIDI note bins (128 notes)
        note_magnitudes = np.zeros(MIDI_NOTE_COUNT, dtype=np.float64)
        for bin_idx in range(len(spectrum)):
            note = bin_to_note[bin_idx]
            note_magnitudes[note] += spectrum[bin_idx]

        # Normalise per frame to [0, 1]
        frame_max = note_magnitudes.max()
        if frame_max > 0:
            note_magnitudes /= frame_max

        # Apply activation threshold.  ``activation_threshold`` is a fraction
        # of the per-tick peak, and ``note_magnitudes`` is already normalised
        # to [0, 1] — so compare against the threshold directly.  (Previously
        # the code compared normalised values against ``frame_max * threshold``,
        # a raw-magnitude scale ≫1, which zeroed EVERY note whenever the
        # threshold was > 0 — silencing all audio.)
        if activation_threshold > 0:
            note_magnitudes[note_magnitudes < activation_threshold] = 0.0

        loudness_frames.append([float(v) for v in note_magnitudes])

    return loudness_frames


# ── 4-octave folding for Factorio ──────────────────────────────────────


def fold_to_game_range(
    full_loudness: dict[int, float] | Sequence[float],
) -> list[float]:
    """Fold a full-spectrum loudness vector (128 MIDI notes) down to the
    Factorio 4-octave game range (48 pitches, F3–E7).

    Notes outside F3–E7 (MIDI 53–100) are octave-shifted into range.
    Multiple source notes that fold to the same target pitch are **summed**
    and clamped to [0.0, 1.0].

    Parameters
    ----------
    full_loudness : dict[int, float] | Sequence[float]
        Mapping from MIDI note (0–127) to loudness (0.0–1.0), or a
        sequence of 128 floats indexed by MIDI note.

    Returns
    -------
    list[float]
        48 values, one per game pitch (0 = F3 … 47 = E7).
    """
    result = [0.0] * GAME_PITCH_COUNT

    if isinstance(full_loudness, dict):
        items = full_loudness.items()
    else:
        items = enumerate(full_loudness)

    for midi_note, loudness in items:
        if loudness <= 0.0:
            continue
        folded = midi_note
        while folded < GAME_MIDI_MIN:
            folded += 12
        while folded > GAME_MIDI_MAX:
            folded -= 12
        if GAME_MIDI_MIN <= folded <= GAME_MIDI_MAX:
            pitch_idx = folded - GAME_MIDI_MIN
            result[pitch_idx] = min(1.0, result[pitch_idx] + loudness)

    return result


def fold_loudness_array(
    full_data: list[list[float]],
) -> list[list[float]]:
    """Fold every tick of a full-spectrum loudness array to game range.

    Parameters
    ----------
    full_data : list[list[float]]
        ``loudness[tick][midi_note]`` where midi_note in 0..127.

    Returns
    -------
    list[list[float]]
        ``loudness[tick][pitch_idx]`` where pitch_idx in 0..47.
    """
    return [fold_to_game_range(tick) for tick in full_data]


# ── audio file I/O ──────────────────────────────────────────────────────

# File extensions supported by soundfile (lossless/lossy via libsndfile).
_SF_EXTENSIONS: set[str] = {
    "wav", "flac", "ogg", "aiff", "aif", "au", "caf",
    "mp3", "mp4", "m4a", "aac", "wma",
}


def _sf_read_unicode(path: str, dtype: str = "float64"):
    """Call ``sf.read(path, ...)`` with a Unicode-safe fallback on Windows.

    On some Windows builds libsndfile cannot open files whose path
    contains non-ASCII characters.  When that happens we copy the file
    to a temporary ASCII-named location and read from there.
    """
    import soundfile as sf  # pylint: disable=import-outside-toplevel

    try:
        return sf.read(path, dtype=dtype, always_2d=False)
    except sf.LibsndfileError:
        # Only bother with the temp-file fallback on Windows for
        # paths that actually contain non-ASCII characters.
        if sys.platform != "win32" or path.isascii():
            raise
        import os
        import tempfile
        import shutil
        ext = path.rsplit(".", 1)[-1] if "." in path else "tmp"
        tmp_path = os.path.join(
            tempfile.gettempdir(),
            f"fd_audio_{os.getpid()}.{ext}",
        )
        try:
            shutil.copy2(path, tmp_path)
            return sf.read(tmp_path, dtype=dtype, always_2d=False)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def read_audio_file(path: str) -> tuple[np.ndarray, int]:
    """Read an audio file and return ``(samples_float64, sample_rate)``.

    Uses ``soundfile`` for all supported formats (WAV, FLAC, OGG, MP3, etc.).
    Samples are always returned as float64 mono (stereo is downmixed).
    """
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""

    if ext in _SF_EXTENSIONS:
        data, sr = _sf_read_unicode(path)
        return data, int(sr)

    raise ValueError(
        f"Unsupported audio format: .{ext}. "
        f"Supported: {', '.join(sorted(_SF_EXTENSIONS))}"
    )


def audio_file_to_loudness(
    path: str, **kwargs,
) -> list[list[float]]:
    """Read an audio file and convert to a full-spectrum loudness array.

    Convenience wrapper: ``read_audio_file`` + ``audio_to_loudness``.

    Parameters
    ----------
    path : str
        Path to an audio file (WAV, FLAC, OGG, MP3, etc.).
    **kwargs
        Forwarded to :func:`audio_to_loudness`.

    Returns
    -------
    list[list[float]]
        ``loudness[tick][midi_note]`` where midi_note in 0..127.
    """
    samples, sr = read_audio_file(path)
    sys.stderr.write(
        f"Audio: {len(samples) / sr:.1f}s, "
        f"sample_rate={sr}, "
        f"channels={'mono' if samples.ndim == 1 else samples.shape[1]}\n"
    )
    return audio_to_loudness(samples, sr, **kwargs)
