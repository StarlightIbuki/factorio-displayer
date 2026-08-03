"""Comparison metrics for MIDI/tick_data fidelity evaluation.

All metrics are self-contained (numpy only) and reuse the project's own
``audio_analyzer`` STFT for the audio-domain comparisons, so the "ear"
that judges the translation is the same analysis the project uses.
"""

from __future__ import annotations

import mido
import numpy as np

from factorio_display.audio.audio_analyzer import audio_to_loudness

GAME_TICK_S = 1.0 / 60.0


# ── note-event extraction ──────────────────────────────────────────────

def extract_notes(mid: mido.MidiFile) -> list[dict]:
    """Extract note events ``{midi, start_tick, end_tick, vel, channel}``.

    *start_tick/end_tick* are game ticks (1/60 s) at 120 BPM reference.
    """
    notes: list[dict] = []
    for track in mid.tracks:
        abs_tick = 0
        cur_tempo = mido.bpm2tempo(120)
        active: dict[int, tuple[int, int, int]] = {}
        for msg in track:
            abs_tick += int(msg.time)
            if msg.type == "set_tempo":
                cur_tempo = msg.tempo
                continue
            if msg.type == "note_on" and msg.velocity > 0:
                active[msg.note] = (abs_tick, msg.velocity, getattr(msg, "channel", 0))
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                info = active.pop(msg.note, None)
                if info is None:
                    continue
                s_tick, vel, ch = info
                s_sec = mido.tick2second(s_tick, mid.ticks_per_beat, cur_tempo)
                e_sec = mido.tick2second(abs_tick, mid.ticks_per_beat, cur_tempo)
                notes.append({
                    "midi": msg.note,
                    "start": s_sec / GAME_TICK_S,
                    "end": e_sec / GAME_TICK_S,
                    "vel": vel,
                    "channel": ch,
                    "chroma": msg.note % 12,
                })
    return notes


def tickdata_to_notes(
    td: list[list[float]],
    midi_base: int,
    threshold: float = 1.0,
) -> list[dict]:
    """Reconstruct note events from a per-rail tick→loudness grid.

    Notes are contiguous runs of ticks where loudness > *threshold*.
    Returns ``{midi, start, end, vel, chroma}`` in game ticks.
    """
    num_ticks = len(td)
    active = [False] * 48
    start = [0] * 48
    vel = [0.0] * 48
    notes: list[dict] = []
    for tick in range(num_ticks):
        for p in range(48):
            v = td[tick][p] if p < len(td[tick]) else 0.0
            if v > threshold and not active[p]:
                active[p] = True
                start[p] = tick
                vel[p] = v
            elif v > threshold:
                vel[p] = max(vel[p], v)
            elif active[p]:
                notes.append({
                    "midi": midi_base + p,
                    "start": start[p],
                    "end": tick,
                    "vel": vel[p],
                    "chroma": (midi_base + p) % 12,
                })
                active[p] = False
                vel[p] = 0.0
    for p in range(48):
        if active[p]:
            notes.append({
                "midi": midi_base + p,
                "start": start[p],
                "end": num_ticks,
                "vel": vel[p],
                "chroma": (midi_base + p) % 12,
            })
    return notes


# ── note-level metrics ─────────────────────────────────────────────────

def note_level_metrics(ref: list[dict], rec: list[dict], onset_tol: float = 3.0) -> dict:
    """Compare reference notes vs reconstructed notes.

    Matching is greedy: each reference note tries to match an unmatched
    reconstructed note with the same chroma whose onset is within
    *onset_tol* game ticks (nearest first).  Reports:

      segmentation_recall = matched / len(ref)   (re-articulations kept;
                              back-to-back same-pitch repeats merged by the
                              translator count as one → this drops)
      coverage            = ref notes whose chroma is played at their onset
                              by SOME reconstructed note (robust to merging)
      rhythm_recall       = ref notes whose onset coincides with ANY
                              reconstructed note onset (pitch-agnostic — how
                              drums should be judged)
      precision           = matched / len(rec)
      chroma_acc          = fraction of matched notes with identical chroma
      exact_acc           = fraction of matched notes with identical octave too
      onset_err           = median |onset delta| over matched (game ticks)
      dur_err             = median |duration delta| over matched (game ticks)
      vel_corr            = Pearson correlation of matched velocities
    """
    if not ref:
        return {"segmentation_recall": 0.0, "coverage": 0.0, "rhythm_recall": 0.0,
                "precision": 0.0, "chroma_acc": 0.0, "exact_acc": 0.0,
                "onset_err": 0.0, "dur_err": 0.0, "vel_corr": 0.0}
    used = [False] * len(rec)
    matched = 0
    chroma_ok = 0
    exact_ok = 0
    onset_errs: list[float] = []
    dur_errs: list[float] = []
    vel_pairs: list[tuple[float, float]] = []
    covered = 0
    rhythm = 0
    for rn in ref:
        # pitch coverage: some rec note with same chroma whose span contains
        # this onset (robust to back-to-back same-pitch merging / folding
        # collapse — the merged note still plays the right pitch at that time)
        if any(
            rc["chroma"] == rn["chroma"]
            and rc["start"] - onset_tol <= rn["start"] <= rc["end"] + onset_tol
            for rc in rec
        ):
            covered += 1
        # rhythm: any rec note's span contains this onset, pitch-agnostic
        if any(
            rc["start"] - onset_tol <= rn["start"] <= rc["end"] + onset_tol
            for rc in rec
        ):
            rhythm += 1
        # greedy chroma match (articulation-level)
        cands = [
            (i, abs(rec[i]["start"] - rn["start"]))
            for i in range(len(rec))
            if not used[i] and rec[i]["chroma"] == rn["chroma"]
            and abs(rec[i]["start"] - rn["start"]) <= onset_tol
        ]
        if not cands:
            continue
        best_i, best_d = min(cands, key=lambda c: c[1])
        used[best_i] = True
        matched += 1
        r = rec[best_i]
        if r["chroma"] == rn["chroma"]:
            chroma_ok += 1
        if r["midi"] == rn["midi"]:
            exact_ok += 1
        onset_errs.append(best_d)
        dur_errs.append(abs((r["end"] - r["start"]) - (rn["end"] - rn["start"])))
        vel_pairs.append((rn["vel"], r["vel"]))

    vel_corr = 0.0
    if len(vel_pairs) >= 2:
        a = np.array([v[0] for v in vel_pairs], dtype=np.float64)
        b = np.array([v[1] for v in vel_pairs], dtype=np.float64)
        if np.std(a) > 0 and np.std(b) > 0:
            vel_corr = float(np.corrcoef(a, b)[0, 1])

    return {
        "segmentation_recall": matched / len(ref),
        "coverage": covered / len(ref),
        "rhythm_recall": rhythm / len(ref),
        "precision": matched / len(rec) if rec else 0.0,
        "chroma_acc": (chroma_ok / matched) if matched else 0.0,
        "exact_acc": (exact_ok / matched) if matched else 0.0,
        "onset_err": float(np.median(onset_errs)) if onset_errs else 0.0,
        "dur_err": float(np.median(dur_errs)) if dur_errs else 0.0,
        "vel_corr": vel_corr,
        "n_matched": matched,
        "n_ref": len(ref),
        "n_rec": len(rec),
    }


# ── audio-domain metrics (uses project's own analyzer) ─────────────────

def _stft_loudness(samples: np.ndarray, sr: int = 44100) -> np.ndarray:
    """128-note loudness grid via the project's own audio_analyzer.

    Returns ``array[tick][128]`` in [0,1].
    """
    ld = audio_to_loudness(samples, sr)
    return np.asarray(ld, dtype=np.float64)


def _chroma(loud: np.ndarray) -> np.ndarray:
    """12-bin chroma (pitch-class) per tick, normalised per tick."""
    n_tick, n_note = loud.shape
    chroma = np.zeros((n_tick, 12), dtype=np.float64)
    for n in range(n_note):
        chroma[:, n % 12] += loud[:, n]
    # per-tick normalise to unit sum
    sums = chroma.sum(axis=1, keepdims=True)
    sums[sums == 0] = 1.0
    return chroma / sums


def _rms_envelope(samples: np.ndarray, sr: int = 44100, hop: int = 735) -> np.ndarray:
    """RMS envelope at ~60 Hz (one value per game tick)."""
    n = len(samples)
    frames = max(1, n // hop)
    env = np.zeros(frames, dtype=np.float64)
    for i in range(frames):
        seg = samples[i * hop : (i + 1) * hop]
        env[i] = np.sqrt(np.mean(seg ** 2)) if seg.size else 0.0
    return env


def _dtw_norm(a: np.ndarray, b: np.ndarray) -> float:
    """Normalised DTW distance between two sequences (numpy-only)."""
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return 0.0
    # local cost = 1 - cosine (0..1)
    cost = np.zeros((n + 1, m + 1))
    cost[0, :] = np.inf
    cost[:, 0] = np.inf
    cost[0, 0] = 0.0
    a = a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-9)
    b = b / (np.linalg.norm(b, axis=1, keepdims=True) + 1e-9)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d = 1.0 - float(np.dot(a[i - 1], b[j - 1]))
            cost[i, j] = d + min(cost[i - 1, j], cost[i, j - 1], cost[i - 1, j - 1])
    return float(cost[n, m] / (n + m))


def audio_metrics(ref: np.ndarray, trans: np.ndarray, sr: int = 44100) -> dict:
    """Compare reference and translated audio via the project's own ear.

    Returns chroma cosine similarity (frame-mean), chroma DTW distance,
    RMS-onset envelope correlation, and the frame-wise spectral cosine
    similarity of the 128-note loudness grids.
    """
    if ref.size == 0 or trans.size == 0:
        return {"chroma_cos": 0.0, "chroma_dtw": 1.0, "env_corr": 0.0, "spec_cos": 0.0}
    rl = _stft_loudness(ref, sr)
    tl = _stft_loudness(trans, sr)
    if rl.shape[0] == 0 or tl.shape[0] == 0:
        return {"chroma_cos": 0.0, "chroma_dtw": 1.0, "env_corr": 0.0, "spec_cos": 0.0}

    rc = _chroma(rl)
    tc = _chroma(tl)

    # frame-wise cosine on chroma
    n = min(len(rc), len(tc))
    cos = np.array([
        float(np.dot(rc[i], tc[i]) / (np.linalg.norm(rc[i]) * np.linalg.norm(tc[i]) + 1e-9))
        for i in range(n)
    ])
    chroma_cos = float(np.mean(cos))

    chroma_dtw = _dtw_norm(rc, tc)

    renv = _rms_envelope(ref, sr)
    tenv = _rms_envelope(trans, sr)
    n2 = min(len(renv), len(tenv))
    if n2 >= 4 and np.std(renv[:n2]) > 0 and np.std(tenv[:n2]) > 0:
        env_corr = float(np.corrcoef(renv[:n2], tenv[:n2])[0, 1])
    else:
        env_corr = 0.0

    # spectral cosine similarity of 128-note grids (frame mean, aligned)
    rl_n = rl[:n] / (np.linalg.norm(rl[:n], axis=1, keepdims=True) + 1e-9)
    tl_n = tl[:n] / (np.linalg.norm(tl[:n], axis=1, keepdims=True) + 1e-9)
    spec_cos = float(np.mean(np.sum(rl_n * tl_n, axis=1)))

    return {
        "chroma_cos": chroma_cos,
        "chroma_dtw": chroma_dtw,
        "env_corr": env_corr,
        "spec_cos": spec_cos,
        "ref_ticks": len(rl),
        "trans_ticks": len(tl),
    }
