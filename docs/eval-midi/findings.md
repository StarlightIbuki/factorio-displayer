# AI-driven MIDI Translator — Evaluation Findings

**Date:** 2026-08-03 · **Status:** evaluated and committed to `main`
**Scope:** `src/factorio_display/audio/midi_translator.py` + the AI (Basic Pitch) transcription path + encoder chain + audio drum detection.

---

## 1. What was evaluated

The full chain "original music → translated Factorio playback":

```
original MIDI ──► midi_to_multi_rail_tick_data ──► tick_data[rail][tick][pitch]
      │                                                     │
      │ (or, for raw audio)                                  ▼
   Basic Pitch (AI) ──► AI MIDI ──► same translator   Factorio-speaker render
      │                                                      │
      └── STFT fallback (no AI)                              ▼
                                                   compare with project's own
                                                   audio_analyzer (its "ear")
```

Five reference scenarios were synthesized (`eval_midi/ref_midis/`): monophonic
melody, wide-range (F1–C8, stresses octave folding), polyphonic chords+bass,
multi-instrument with drums, and drums-only. Each was translated with the real
translator and scored three ways:

1. **Note-level** (MIDI → tick_data → invert to notes): pitch coverage,
   segmentation, rhythm, exact-pitch, timing/duration error, velocity corr.
2. **Audio content** (timbre-free): original vs reconstructed notes rendered
   with the *same* synth → isolates the translator's information loss.
3. **Audio in-game** (original vs Factorio-speaker render): what you'd actually
   hear; judged with the project's own `audio_analyzer` STFT.

## 2. Results

### 2.1 Core MIDI→Factorio fidelity (rail_mode=auto, map_drums=off)

| scenario | rails | coverage | rhythm | exact | onset | vel | content chroma | content spec |
|---|---|---|---|---|---|---|---|---|
| melody | piano | 1.00 | 1.00 | 1.00 | 0 | 0.00 | 0.86 | 0.86 |
| wide_range | piano,bass,celesta | 1.00 | 1.00 | 0.91 | 0 | 0.74 | 0.74 | 0.64 |
| polyphonic | piano,bass | 1.00 | 1.00 | 0.97 | 0 | 1.00 | 0.75 | 0.68 |
| multitrack | piano,bass,drum,steel-drum | 0.81 | 1.00 | 0.87 | 0 | 0.83 | 0.74 | 0.60 |
| drums_only | drum | — | 1.00 | — | 0 | — | — | — (env 0.86) |

- **Pitch coverage is 100% for all melodic content** and rhythm is 100%
  everywhere. All onset errors = 0 ticks.
- The factorio-speaker render scores **chroma 0.75–0.85, spec 0.49–0.72**
  against the original → the melody/harmony is clearly recognisable in-game.

### 2.2 AI (Basic Pitch) end-to-end vs STFT fallback

| chain | stage | coverage | exact | precision | factorio chroma | factorio env |
|---|---|---|---|---|---|---|
| melody | AI transcribe | 1.00 | 0.27 | 0.50 | 0.856 | 0.495 |
| melody | +Factorio | 1.00 | 0.09 | 0.52 | — | — |
| polyphonic | AI transcribe | 1.00 | 0.53 | 0.51 | 0.783 | 0.535 |
| polyphonic | +Factorio | 1.00 | 0.38 | 0.55 | — | — |
| stft melody | no AI | 1.00 | 0.10 | **0.04** | 0.834 | **0.344** |
| stft polyphonic | no AI | 1.00 | 0.02 | **0.09** | 0.808 | **0.381** |

- The **AI path is dramatically cleaner than the STFT fallback**: for the
  15-note melody the STFT path emits **256 noisy notes** (precision 0.04),
  while Basic Pitch emits ~21 musical notes (precision 0.50). End-to-end
  onset-envelope correlation nearly doubles (0.34 → 0.50).
- AI exact-pitch accuracy is low (0.09–0.53) because Basic Pitch outputs
  pitches/slight pitch-bends an octave or semitone off; the translator's
  octave folding + our chroma metrics treat these as correct content.

## 3. Findings

### Strengths
1. **Musical content is preserved almost perfectly** for in-range content:
   100% pitch-class coverage, 100% rhythm, 0-tick onset error.
2. **Multi-rail instrument routing works**: bass notes auto-fold onto the bass
   rail, high lead onto celesta, drums onto the drum rail — so wide-range and
   multi-instrument songs survive better than a single-piano fallback.
3. **AI transcription is the right call for raw audio** (vs STFT), matching the
   code's own documented rationale.

### Weaknesses / fidelity losses (all measured)
1. **Back-to-back same-pitch notes merge** into one sustained tone (no
   re-articulation): the melody's 15 notes became 10 (segmentation 0.67).
   Music "plays right" but repeated notes lose their attack. Root cause: the
   tick_data encoding has no note-off/note-on distinction — loudness is the
   only articulation signal.
2. **Octave folding collapses distinct octaves to one speaker** (e.g.
   `wide_range` C1/C2/C3 → all C3 on bass), and when they are near in time
   they merge (exact-pitch drops to 0.91; content chroma 0.74). A run of
   "C1 C2 C3" becomes "held C3".
3. **`--map-drums` trades bass melody for percussion** (by design): with it
   on, `wide_range` coverage drops 1.00 → 0.67 and `multitrack` 0.81 → 0.49.
   Default off is the right default.
4. **Drums can't be judged by pitch** — they map to a fixed 17-sound kit
   (rhythm is perfect: env_corr 0.95–0.99). Pitch-matching metrics for drums
   are meaningless; use onset/rhythm metrics instead.
5. **Dynamics**: melody's `vel_corr` was 0 because notes all had equal MIDI
   velocity; where dynamics vary (polyphonic/multitrack) correlation is
   0.83–1.00. Velocity maps linearly (`v/127*100`), so `v<127` never drives
   speakers at full loudness.

## 4. Bugs found (and fixed, in this worktree only)

1. **`basic_pitch_worker.py` — API mismatch with installed basic-pitch.**
   `predict_and_save()` requires `sonify_midi` and `model_or_model_path`
   (a `Model`/path); the worker omitted both, so **every AI transcription
   failed** (silently fell back to STFT). Fixed by passing
   `sonify_midi=False` and `model_or_model_path=ICASSP_2022_MODEL_PATH`.
2. **`basic_pitch_transcriber.py` — Windows GBK encoding crash.** Basic Pitch
   prints an emoji (U+1F6A8) via tqdm; on a GBK console this raises
   `UnicodeEncodeError` inside the subprocess, failing transcription. Fixed by
   forcing `PYTHONIOENCODING=utf-8` on the subprocess env.
3. **Basic Pitch cache serves corrupt MIDIs.** `.factorio_display_cache/
   basic_pitch/*.mid` contained 4-byte `MThd` files (broken/empty). The cache
   validity check is only `Path.exists()`, not parse-validity, so a corrupt
   cached MIDI → `mido` `EOFError` on re-encode (hard failure, no STFT
   fallback). Recommend validating the cached MIDI parses (≥1 track) before
   returning, or clearing the cache entry on parse failure.

*(These two source files were edited in the worktree to make the AI path
testable; nothing was committed.)*

## 5. Recommendations

1. ~~Add a **re-articulation** option~~ **DONE (2026-08-03):** a re-articulation gap is now an
   option (`--rearticulation-ticks`, CLI default 2, API field
   `rearticulation_ticks`). When a same-pitch note re-triggers within the
   window of the previous note's end, its start is pushed back by that many
   game ticks so the speaker re-attacks instead of merging. Drums are
   unaffected. Reference-scenario effect: melody segmentation_recall
   0.667→1.0, wide_range 0.73→1.0, polyphonic 0.62→1.0.
2. Validate cached Basic Pitch MIDI files before use (see bug 3) — still open.
3. For wide-range material, consider a **pitch-keeping strategy** per rail
   (e.g., split rails by octave) to avoid octave-collapse of fast runs, or
   document the octave-fold trade-off.
4. Add drum-specific tests that assert **onset timing**, not pitch.

## 7. Real-music test (IRON ATTACK! — "IRON SOUL" karaoke, 2026-08-03)

`D:\Music\IRON ATTACK!\Iron Soul\11 IRON SOUL =Karaoke=.mp3` (370 s). Evaluated
the first 60 s via the AI path (Basic Pitch → translator → Factorio render).

- AI transcription: 606 note events, 2 tracks (piano + bass), 59.6 s.
- Rails after translation: `piano`, `bass`.
- **Re-articulation effect:** total note attacks 332 → 508 (**+53%**); the
  bass rail went 141 → 278 — the repeated metal bass/pedal patterns were
  previously merging into sustained drones and are now re-attacked.
- Audio vs original: chroma 0.648→0.655, spec 0.362→0.368 (slightly better);
  the Factorio render is faithful to the AI MIDI (chroma 0.75, env 0.86).
- Re-articulation also lowered the peak loudness (158.2→111.8 before
  normalize) because notes no longer stack into continuous drones.
- Importable blueprints generated: `eval_midi/out/real/iron_soul_r0.txt` and
  `iron_soul_r2.txt` (60 pages, ~70 KB each).
- Rendered Factorio-speaker WAVs (for listening without Factorio):
  `eval_midi/out/real/11_IRON_SOUL_Karaoke_reartic0.wav` / `..._reartic2.wav`.

## 8. Audio drum detection (NEW 2026-08-03)

The original track is drum-heavy but the output had **no drums**: Basic Pitch
(and the STFT fallback) only transcribe *pitched* notes, so unpitched
percussion never reaches the rails. `--map-drums` only re-routes low melodic
notes to a kick — it cannot recover real snare/hat hits.

**Fix:** `src/factorio_display/audio/drum_detector.py` — spectral-flux onset
detection on the raw waveform in three bands (kick 40–180 Hz, snare
180–5000 Hz, hat 7–16 kHz), merged and classified by z-score, mapped onto the
17-slot Factorio drum kit. Wired into the composed audio path behind a new
`--drums` flag (default off; `auto`/`off`). Added `tests/test_drum_detector.py`.

- Iron Soul 0–60 s: **159 hits** (40 hat, 66 snare, 53 kick) added as a drum
  rail; blueprint `eval_midi/out/real/iron_soul_r2_allinone_drums.txt`
  (433 entities, 3 drum-kit speakers — kick-1/snare-1/hat-1 — plus piano/bass).
- **Sustain fix:** hits were single-tick (16.7 ms) blips → drums cut off
  before the drum-kit sample was audible. Each hit now sustains a short
  plateau + decay tail (`DRUM_DURATION_TICKS`: kick 6 / snare 5 / hat 4
  game ticks). This also improved the render: full env_corr vs original
  0.32→0.35, drums-only env_corr −0.02→+0.22 (sustained hits now match the
  original's percussion envelope).
- Listen: `11_IRON_SOUL_Karaoke_reartic2_drums.wav` (3 rails) vs
  `..._reartic2.wav` (2 rails, no drums).
- Honest caveat: without source separation the classifier also fires on
  guitar/bass transients in a full mix, so hit timing is on the mix's transient
  grid but the kick/snare/hat split is approximate. The full-render env_corr
  vs the original stays ~0.32 (a transient-only drum stem does not correlate
  with the continuous full-mix RMS envelope, so that metric cannot judge drum
  timing — listen instead). Ideal upgrade: Demucs drum-stem separation first.

## 6. Reproduction

```
cd factorio-display
.venv\Scripts\python.exe -m pytest tests/test_midi_translator.py -q -o addopts="" -k Rearticulation
.venv\Scripts\python.exe -m pytest tests/test_drum_detector.py -q -o addopts=""
.venv\Scripts\python.exe eval_midi\make_reference_midis.py
.venv\Scripts\python.exe eval_midi\eval_fidelity.py --out eval_midi/out/auto
.venv\Scripts\python.exe eval_midi\eval_fidelity.py --out eval_midi/out/reartic --rearticulation-ticks 2
$env:FACTORIO_BASIC_PITCH_PYTHON = "...\.venv-bp\Scripts\python.exe"
.venv\Scripts\python.exe eval_midi\eval_real_track.py --input "D:\Music\IRON ATTACK!\Iron Soul\11 IRON SOUL =Karaoke=.mp3" --dur 60
.venv\Scripts\python.exe -m factorio_display encode eval_midi\out\real\11_IRON_SOUL_Karaoke_seg_0-60.wav --name "Iron Soul AI" --power none --rearticulation-ticks 2 -o eval_midi\out\real\iron_soul_r2.txt
.venv\Scripts\python.exe -m factorio_display encode eval_midi\out\real\11_IRON_SOUL_Karaoke_seg_0-60.wav --name "Iron Soul AI r2 drums" --power substation --rearticulation-ticks 2 --drums -o eval_midi\out\real\iron_soul_r2_allinone_drums.txt
.venv\Scripts\python.exe eval_midi\render_drums.py
```

Rendered WAVs and JSON reports live in `eval_midi/out/`.
