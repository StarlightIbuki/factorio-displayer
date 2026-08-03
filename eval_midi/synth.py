"""Tiny numpy-only MIDI / tick_data → WAV synthesizer for evaluation.

Two renderers:
1. ``midi_to_wav``  — "musical" reference render: piano-ish tone per note.
2. ``tickdata_to_wav`` — faithful Factorio-speaker emulation: each of the
   48 pitches is a continuous tone whose amplitude tracks the per-tick
   loudness the decoder would drive (drum rails render as short hits).

Nothing here imports tensorflow; only numpy (+ mido for parsing).
"""

from __future__ import annotations

import mido
import numpy as np

from factorio_display.audio.pitch_mapping import (
    INSTRUMENT_MIDI_BASES,
    MIDI_BASE,
)

SR = 44100.0
GAME_TICK_S = 1.0 / 60.0


def midi_to_freq(midi_note: int) -> float:
    """Equal-tempered frequency (Hz) for a MIDI note number."""
    return 440.0 * (2.0 ** ((midi_note - 69) / 12.0))


# ── Reference "musical" renderer (MIDI → WAV) ──────────────────────────

def _piano_tone(midi_note: int, dur_s: float, vel: float, sr: float = SR) -> np.ndarray:
    """One piano-ish note: fundamental + harmonics with exponential decay."""
    n = max(1, int(dur_s * sr))
    t = np.linspace(0.0, dur_s, n, endpoint=False)
    f = midi_to_freq(midi_note)
    env = (1.0 - np.exp(-t * 250.0)) * np.exp(-t * 4.5)
    wave = (
        np.sin(2 * np.pi * f * t)
        + 0.5 * np.sin(2 * np.pi * 2 * f * t)
        + 0.25 * np.sin(2 * np.pi * 3 * f * t)
        + 0.12 * np.sin(2 * np.pi * 4 * f * t)
    )
    wave /= 1.87
    return (wave * env * (vel / 127.0)).astype(np.float64)


def _bass_tone(midi_note: int, dur_s: float, vel: float, sr: float = SR) -> np.ndarray:
    """A sine-heavy bass tone (used for low tracks)."""
    n = max(1, int(dur_s * sr))
    t = np.linspace(0.0, dur_s, n, endpoint=False)
    f = midi_to_freq(midi_note)
    env = (1.0 - np.exp(-t * 200.0)) * np.exp(-t * 3.0)
    wave = np.sin(2 * np.pi * f * t) + 0.3 * np.sin(2 * np.pi * 2 * f * t)
    wave /= 1.3
    return (wave * env * (vel / 127.0)).astype(np.float64)


def midi_to_wav(mid: mido.MidiFile, sr: float = SR, tone: str = "piano") -> np.ndarray:
    """Render a MIDI file to mono WAV samples at *sr*.

    *tone* selects the per-note timbre: ``"piano"`` (harmonic-rich, for the
    content comparison) or ``"sine"`` (pure fundamental, matching Factorio
    speakers — for a timbre-fair in-game comparison).  Drums (channel 9)
    render as short noise bursts.  Returns a 1-D float64 array.
    """
    seconds = max(mid.length, 0.05)
    total = int(seconds * sr) + int(sr)  # pad one second
    buf = np.zeros(total, dtype=np.float64)

    for track in mid.tracks:
        abs_tick = 0
        cur_tempo = mido.bpm2tempo(120)
        active: dict[int, tuple[int, int, int, bool]] = {}  # note -> (start_tick, vel, channel, is_drum)
        for msg in track:
            abs_tick += int(msg.time)
            if msg.type == "set_tempo":
                cur_tempo = msg.tempo
                continue
            if msg.type == "note_on" and msg.velocity > 0:
                active[msg.note] = (abs_tick, msg.velocity, getattr(msg, "channel", 0), False)
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                info = active.pop(msg.note, None)
                if info is None:
                    continue
                start_tick, vel, ch, _ = info
                start_s = mido.tick2second(start_tick, mid.ticks_per_beat, cur_tempo)
                end_s = mido.tick2second(abs_tick, mid.ticks_per_beat, cur_tempo)
                dur = max(end_s - start_s, 0.03)
                i0 = int(start_s * sr)
                if i0 >= total:
                    continue
                if ch == 9:
                    # drum: short noise burst
                    n = int(min(dur, 0.08) * sr)
                    seg = (np.random.default_rng(msg.note).standard_normal(n)
                           * np.exp(-np.linspace(0, 1, n) * 6.0))
                    seg *= vel / 127.0 * 0.6
                    end = min(total, i0 + n)
                    buf[i0:end] += seg[: end - i0]
                elif tone == "sine":
                    n = int(dur * sr)
                    t = np.linspace(0.0, dur, n, endpoint=False)
                    f = midi_to_freq(msg.note)
                    env = (1.0 - np.exp(-t * 250.0)) * np.exp(-t * 2.0)
                    seg = np.sin(2 * np.pi * f * t) * env * (vel / 127.0)
                    end = min(total, i0 + n)
                    buf[i0:end] += seg[: end - i0]
                else:
                    seg = _bass_tone(msg.note, dur, vel, sr) if 30 <= msg.note <= 45 else _piano_tone(msg.note, dur, vel, sr)
                    n = len(seg)
                    end = min(total, i0 + n)
                    buf[i0:end] += seg[: end - i0]

    # soft clip
    mx = np.max(np.abs(buf)) or 1.0
    if mx > 1.0:
        buf = buf / mx * 0.95
    return buf


# ── Factorio speaker emulation (tick_data → WAV) ───────────────────────

def tickdata_to_wav(
    rail_data: list[list[list[float]]],
    instruments: list[str],
    sr: float = SR,
) -> np.ndarray:
    """Render per-rail tick→loudness data as Factorio speakers would sound.

    ``rail_data[r][tick][pitch]`` is the loudness (0–100) the decoder drives
    on rail *r*, speaker *pitch*.  Melodic speakers play a **continuous-phase
    sine** whose amplitude steps once per game tick (60 Hz) — mirroring a
    real speaker fed a per-tick volume; drum rails are percussive hits
    (attack tick only, then a short decay).

    Returns a mono float64 array.
    """
    num_ticks = max((len(td) for td in rail_data), default=0)
    if num_ticks == 0:
        return np.zeros(1, dtype=np.float64)
    total_samples = int(num_ticks * GAME_TICK_S * sr) + int(sr)
    buf = np.zeros(total_samples, dtype=np.float64)
    samples_per_tick = GAME_TICK_S * sr

    for ri, td in enumerate(rail_data):
        base = INSTRUMENT_MIDI_BASES.get(instruments[ri], MIDI_BASE)
        is_drum = "drum" in instruments[ri].lower()
        n_ticks = len(td)
        for p in range(48):
            amp = np.array([td[t][p] if p < len(td[t]) else 0.0 for t in range(n_ticks)],
                           dtype=np.float64) / 100.0
            if not np.any(amp > 0):
                continue
            f = midi_to_freq(base + p)
            n = min(total_samples, int(n_ticks * samples_per_tick))
            t = np.linspace(0.0, n / sr, n, endpoint=False)
            phase = 2 * np.pi * f * t
            if is_drum:
                # percussive: only the attack tick's amplitude, short decay
                ticks_idx = (np.arange(n) / samples_per_tick).astype(int)
                hit = np.zeros(n, dtype=np.float64)
                last = -1
                for tick in range(n_ticks):
                    if amp[tick] <= 0:
                        continue
                    i0 = int(tick * samples_per_tick)
                    i1 = min(n, i0 + int(0.08 * sr))
                    seg_t = np.linspace(0, 1, i1 - i0, endpoint=False)
                    seg = np.sin(phase[i0:i1] * 0.5) * np.exp(-seg_t * 8.0) * amp[tick] * 0.7
                    hit[i0:i1] += seg
                buf[:n] += hit
            else:
                # continuous-phase sine, per-tick stepped amplitude
                ticks_idx = (np.arange(n) / samples_per_tick).astype(int)
                ticks_idx = np.minimum(ticks_idx, n_ticks - 1)
                env = amp[ticks_idx]
                buf[:n] += np.sin(phase + np.pi * 0.25) * env

    mx = np.max(np.abs(buf)) or 1.0
    if mx > 1.0:
        buf = buf / mx * 0.95
    return buf


def write_wav(samples: np.ndarray, path: str, sr: float = SR) -> None:
    """Write mono float64 samples to a WAV file via soundfile."""
    import soundfile as sf
    sf.write(path, samples, int(sr))


def notes_to_wav(notes: list[dict], sr: float = SR) -> np.ndarray:
    """Render reconstructed note dicts ``{midi,start,end,vel}`` (game ticks)
    with the SAME piano tone as :func:`midi_to_wav`, so content differences
    (pitch folding / merging / timing / dynamics) are isolated from timbre."""
    last = max((n["end"] for n in notes), default=0)
    total = int((last + 5) * GAME_TICK_S * sr) + int(sr)
    buf = np.zeros(total, dtype=np.float64)
    for n in notes:
        i0 = int(n["start"] * GAME_TICK_S * sr)
        dur = max((n["end"] - n["start"]) * GAME_TICK_S, 0.03)
        seg = _piano_tone(n["midi"], dur, n["vel"], sr)
        end = min(total, i0 + len(seg))
        buf[i0:end] += seg[: end - i0]
    mx = np.max(np.abs(buf)) or 1.0
    if mx > 1.0:
        buf = buf / mx * 0.95
    return buf


def pad_audio(samples: np.ndarray, target_len: int) -> np.ndarray:
    """Zero-pad (or truncate) *samples* to exactly *target_len* samples."""
    if len(samples) < target_len:
        out = np.zeros(target_len, dtype=np.float64)
        out[: len(samples)] = samples
        return out
    return samples[:target_len]
