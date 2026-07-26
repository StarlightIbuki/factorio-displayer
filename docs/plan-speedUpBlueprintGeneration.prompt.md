## Plan: Speed Up Blueprint Generation

Profile and optimize the major bottlenecks in blueprint generation: (1) O(n³) network materialization for large lamp grids, (2) per-pixel Python iteration in frame encoding, (3) SHA-256 hashing per frame, (4) linear tempo lookup in MIDI processing, (5) Draftsman entity construction overhead. The biggest win is fixing the lamp-grid wiring materializer which runs in O(n³) time for n = W×H lamps.

---


### Phase 1: Optimize MIDI Processing

**Steps**
1. Replace `_get_tempo_at` linear scan with binary search (`bisect`). The tempo map is pre-sorted. Every note event calls this — for a typical MIDI with 1000+ notes, this saves ~O(log T) vs O(T) per lookup. *Depends on: none, parallel with Phase 1*
1. Pre-allocate `tick_data` as a flat `array('d')` or `np.ndarray` of shape `(num_ticks, 48)` for faster writes in the inner ADSR loop. Currently it's `list[list[float]]` which has Python object overhead per tick row. *Depends on: none, parallel with Phase 1*

### Phase 2: Reduce Draftsman Entity Construction Overhead

**Steps**
1. In `_to_draftsman_impl`, batch entity construction: Draftsman's `new_entity()` and property setters do validation. For types without complex settings (small-lamp with pre-set color_signal), skip individual `set_circuit_condition` calls when they match defaults. *Depends on: none, parallel with Phase 1*
1. Profile `Blueprint.to_string()` — if it's a bottleneck for large blueprints, consider writing the blueprint string directly from the LogicalBlueprint without going through Draftsman (as an opt-in fast path). This is a larger change; evaluate after other optimizations. *Depends on all above*

### Phase 3: Caching & Parallelism

**Steps**
1. The video encoder already has pickle caching for resized frames. Extend this to cache the final `_encode_frames_core` output keyed by (frame hashes, mapping params). *Depends on: Phase 2 step 5 (hash change)*
1. For multi-rail MIDI encoding, process each rail's tick_data building in parallel using `concurrent.futures`. Each rail is independent after note collection. *Depends on: Phase 3*

---

**Relevant files**
- `src/factorio_display/logical_blueprint.py` — `_wire_horizontal_first` (line ~1047), `_to_draftsman_impl` (line ~1252)
- `src/factorio_display/video/encoder.py` — `_encode_frames_core` (line ~117), `_encode_frames_logical` (line ~281), `encode_frames` (line ~827)
- `src/factorio_display/audio/midi_translator.py` — `_get_tempo_at` (line ~250), `midi_to_tick_data` (line ~320), `midi_to_multi_rail_tick_data` (line ~560)
- `src/factorio_display/video/player_blueprint.py` — `build_display_logical` (line ~40)
- `tests/test_perf.py` — existing perf thresholds to update

**Verification**
1. Run `pytest tests/test_perf.py -v` — verify no regression below existing thresholds
2. Add a specific test: `build_display_logical(64, 48)` + `to_draftsman` must complete in < 2s (currently likely much slower)
3. Add a test: `_encode_frames_core` with 100 frames of 32×24 must stay under current threshold
4. Manual: encode a 1-minute video at 128×72 and measure wall-clock time before/after
5. Manual: encode Ode to Joy MIDI and measure wall-clock time before/after

**Decisions**
- Lamp grid wiring: use a specialized fast path rather than fixing the generic algorithm, since grid wiring has a known structure
- Frame dedup hash: use Python `hash()` instead of SHA-256 (collision risk is negligible for in-process dedup)
- Skip `Blueprint.to_string()` optimization for now — Draftsman is external; revisit if profiling shows it's a bottleneck after other fixes

**Further Considerations**
1. The `_wire_horizontal_first` fast path could be extended to any network where endpoints form a grid — detect by checking if positions form a contiguous rectangle.
2. For very large displays (>200×200), consider chunking the display into sub-grids that are independently wired, reducing per-network endpoint count.
3. The `_encode_frames_chunked` path uses `ProcessPoolExecutor` — verify that the chunking threshold is appropriate (currently splits when total_pixels > available signals).
