"""Drum onset detection from raw audio.

Basic Pitch (and the STFT fallback) only recover *pitched* notes — drums are
unpitched percussive transients, so they never appear in the melodic rails.
This module analyses the raw waveform directly: a short-window STFT is
converted to per-band spectral flux, onsets are detected independently in
three frequency bands, merged when they coincide, and classified by which
band has the strongest z-score.  The result is a per-game-tick drum rail
matching the 17-slot Factorio drum kit (``pitch_mapping.DRUM_KIT_NOTES``).

Frequency bands (tuned for a full mix, e.g. metal with bass guitar):

    kick  : 40-180 Hz     — the bass drum thump (bass guitar sustains, so its
                            spectral flux is low while the kick pulses)
    snare : 180-5000 Hz   — broadband backbeat (also picks up guitar/cymbal
                            transients, so it needs the highest threshold)
    hat   : 7000-16000 Hz — hi-hat / cymbals (almost nothing else sits here)

Each detected hit becomes a short transient in the drum rail — the loudness
is written at the onset tick and sustained over a few game ticks (a short
plateau + decay tail, see ``DRUM_DURATION_TICKS``) so the Factorio drum-kit
sample is audible rather than a 16.7 ms blip.
"""

from __future__ import annotations

import sys
from typing import Sequence

import numpy as np

from .pitch_mapping import DRUM_NOTE_TO_PITCH, SPEAKER_COUNT  # pylint: disable=relative-beyond-top-level

# Factorio game tick = 1/60 s
TICK_S: float = 1.0 / 60.0

# Which drum-kit sound each detected band maps to.
DEFAULT_SOUNDS: dict[str, str] = {
    "kick": "kick-1",
    "snare": "snare-1",
    "hat": "hat-1",
}

# Per-band adaptive-threshold multiplier (flux > median * k counts as a hit).
DEFAULT_THRESHOLDS: dict[str, float] = {
    "kick": 7.0,
    "snare": 7.0,
    "hat": 6.0,
}

# Minimum z-score (std-deviations above the band mean) for a hit to count.
# 2.0 keeps the accented drum groove (kick/snare/hat) without letting every
# guitar/bass transient become a spurious hit.
MIN_Z: float = 2.0

# Merge onsets that land within this many ticks of each other into one hit.
MERGE_WINDOW_TICKS: int = 3

# How long (in game ticks) each detected drum sound sustains in the rail.
# A single-tick hit is a 16.7 ms blip — the Factorio drum-kit sample is
# barely audible before the volume signal drops to 0.  Like sustained
# melodic notes, the speaker holds the sound while the volume is non-zero,
# so each hit gets a short plateau + linear decay tail.
DRUM_DURATION_TICKS: dict[str, int] = {
    "kick": 6,
    "snare": 5,
    "hat": 4,
}

# Leading ticks of each hit held at full loudness before the decay tail.
DRUM_HOLD_TICKS: int = 2


def _band_indices(sr: int, n_fft: int, lo: float, hi: float) -> np.ndarray:
    """FFT bin indices whose centre frequency lies in ``[lo, hi)`` Hz."""
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    return np.where((freqs >= lo) & (freqs < hi))[0]


def _stft_magnitudes(
    samples: np.ndarray, sr: int, n_fft: int = 2048, hop_s: float = TICK_S,
) -> np.ndarray:
    """Hann-windowed STFT magnitudes, one row per hop (game tick by default)."""
    hop = max(1, int(hop_s * sr))
    frames: list[np.ndarray] = []
    i, n = 0, len(samples)
    while i + n_fft <= n:
        seg = samples[i:i + n_fft] * np.hanning(n_fft)
        frames.append(np.abs(np.fft.rfft(seg)))
        i += hop
    if not frames:
        return np.zeros((0, n_fft // 2 + 1))
    return np.stack(frames)


def _smooth(x: np.ndarray, w: int = 3) -> np.ndarray:
    """Moving-average smoothing (same length as *x*)."""
    kernel = np.ones(w) / w
    return np.convolve(x, kernel, mode="same")


def detect_drum_events(
    samples: np.ndarray,
    sr: int,
    *,
    n_fft: int = 2048,
    hop_s: float = TICK_S,
    thresholds: dict[str, float] | None = None,
    min_z: float = MIN_Z,
    merge_window: int = MERGE_WINDOW_TICKS,
    spread: int = 2,
) -> list[tuple[int, str, float]]:
    """Detect drum hits in *samples*.

    Returns a list of ``(tick, band, loudness)`` where *tick* is a game tick
    index and *loudness* is in ``[0, 100]``.  Bands are ``"kick"``/``"snare"``/
    ``"hat"`` (see module docstring).
    """
    mag = _stft_magnitudes(samples, sr, n_fft=n_fft, hop_s=hop_s)
    if mag.shape[0] == 0:
        return []

    thresh = dict(DEFAULT_THRESHOLDS if thresholds is None else thresholds)
    bins = {
        "kick": _band_indices(sr, n_fft, 40, 180),
        "snare": _band_indices(sr, n_fft, 180, 5000),
        "hat": _band_indices(sr, n_fft, 7000, 16000),
    }

    # Per-band energy → half-wave-rectified spectral flux → smooth.
    energy: dict[str, np.ndarray] = {}
    sflux: dict[str, np.ndarray] = {}
    for band, idx in bins.items():
        if idx.size == 0:
            energy[band] = np.zeros(mag.shape[0])
            sflux[band] = np.zeros(mag.shape[0])
            continue
        e = mag[:, idx].sum(axis=1)
        energy[band] = e
        f = np.maximum(0.0, np.diff(e, prepend=e[0]))
        sflux[band] = _smooth(f)

    # Independent onset peaks per band.
    band_peaks: dict[str, list[int]] = {}
    for band, ser in sflux.items():
        thr = float(np.median(ser)) * thresh.get(band, 7.0)
        peaks = [
            t for t in range(spread, len(ser) - spread)
            if ser[t] > thr and ser[t] == ser[t - spread:t + spread + 1].max()
        ]
        band_peaks[band] = peaks

    # Merge peaks that land within *merge_window* ticks of each other.
    all_ticks = sorted({t for peaks in band_peaks.values() for t in peaks})
    clusters: list[list[int]] = []
    for t in all_ticks:
        if clusters and t - clusters[-1][-1] <= merge_window:
            clusters[-1].append(t)
        else:
            clusters.append([t])

    hits: list[tuple[int, str, float]] = []
    for cl in clusters:
        # Representative tick: the one with the strongest TOTAL spectral flux.
        # (Using the median can land in a silent gap between two close hits and
        # wrongly zero out the whole cluster's z-scores.)
        t = max(cl, key=lambda tt: sum(sflux[b][tt] for b in sflux))
        z: dict[str, float] = {}
        for band, ser in sflux.items():
            mu = float(np.mean(ser))
            sd = float(np.std(ser)) + 1e-9
            z[band] = (ser[t] - mu) / sd
        winner = max(z, key=z.get)
        if z[winner] < min_z:
            continue
        ser = sflux[winner]
        loud = max(30.0, min(100.0, 100.0 * ser[t] / (float(ser.max()) + 1e-9)))
        hits.append((t, winner, round(loud, 1)))

    return hits


def events_to_drum_rail(
    events: Sequence[tuple[int, str, float]],
    num_ticks: int,
    *,
    sounds: dict[str, str] | None = None,
) -> list[list[float]]:
    """Convert detected events to a ``[tick][SPEAKER_COUNT]`` drum rail.

    Only the 17 drum-kit slots (indices 0-16) are used; the remaining
    ``SPEAKER_COUNT - 17`` pitched slots stay at 0 so the rail matches the
    melodic rails' width.
    """
    sounds = dict(DEFAULT_SOUNDS if sounds is None else sounds)
    rail: list[list[float]] = [
        [0.0] * SPEAKER_COUNT for _ in range(num_ticks)
    ]
    for tick, band, loud in events:
        if not (0 <= tick < num_ticks):
            continue
        name = sounds.get(band)
        if name is None:
            continue
        slot = DRUM_NOTE_TO_PITCH.get(name)
        if slot is None:
            continue
        # Sustain the hit over a few ticks (see DRUM_DURATION_TICKS) so the
        # drum-kit sample is audible in-game instead of a single-tick blip.
        # Overlapping hits keep the louder value via max().
        dur = DRUM_DURATION_TICKS.get(band, 4)
        tail = max(1, dur - DRUM_HOLD_TICKS)
        for i in range(dur):
            t = tick + i
            if t >= num_ticks:
                break
            if i < DRUM_HOLD_TICKS:
                v = loud
            else:
                v = loud * (1.0 - (i - DRUM_HOLD_TICKS + 1) / tail)
            if v > rail[t][slot]:
                rail[t][slot] = v
    return rail


def detect_drum_rail(
    samples: np.ndarray,
    sr: int,
    *,
    min_hits: int = 30,
    **kwargs,
) -> list[list[float]] | None:
    """Convenience: detect drums and return a drum rail, or None when the
    clip is too quiet/unpercussive to warrant a drum rail (fewer than
    *min_hits* hits — avoids adding fake drums to melodic-only audio)."""
    events = detect_drum_events(samples, sr, **kwargs)
    if len(events) < min_hits:
        sys.stderr.write(
            f"[drums] only {len(events)} hits detected "
            f"(< {min_hits}) — skipping drum rail\n"
        )
        return None
    num_ticks = int(round(len(samples) / sr / TICK_S)) + 1
    return events_to_drum_rail(events, num_ticks)


def detect_drum_rail_from_file(
    path: str,
    *,
    min_hits: int = 30,
    **kwargs,
) -> list[list[float]] | None:
    """Read an audio file and return a drum rail (or None)."""
    from .audio_analyzer import read_audio_file  # pylint: disable=relative-beyond-top-level,import-outside-toplevel

    samples, sr = read_audio_file(path)
    return detect_drum_rail(samples, sr, min_hits=min_hits, **kwargs)
