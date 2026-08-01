# Composed Audio Layout Redesign — Notes for Future Work

> Status: **abandoned / parked**. This documents the positioning redesign that was
> attempted and reverted, so a future attempt can start from a clear understanding
> instead of re-deriving it.

## What the feature was supposed to be

The composed audio blueprint (`factorio-display encode <audio>`, the
`power_type is not None` path in `cli.py`) merges:

- a **timer** (raw clock + mod timer + green clock bridge),
- one **memory bank** per instrument rail (decider-combinator pages),
- a **multi-rail decoder/player** (speakers + unpackers + selectors + LUTs +
  match deciders + per-rail page-port CC and mod AC).

`_finalize_audio_composition(lb)` in `cli.py` post-processes the merged layout so
every circuit wire stays within Factorio's 9-tile limit and the geometry is
deterministic.

## The user's requirement (the target layout)

Each rail should be a **horizontal bar**, laid out left → right as:

```
[speakers | decoder ACs/DCs | page-port CC | memory bank]
```

- The **memory bank starts 1 tile right of the page-port CC** (the CC is at the
  right edge of the decoder).
- The memory bank **shares the same row length (column count) as the decoder**,
  i.e. `cells_per_tick` (the number of decoder channels — 12 for a full piano,
  1 for a single-kick drum). This is what makes "the width of the bar" always
  match the decoder *no matter how many speakers the track has*.
- The memory **grows right from the CC and wraps** to grow longer in the
  direction perpendicular to the CC row's length (i.e. down in Y). Rows of
  `cells_per_tick` pages, row-major snake.
- Tracks (bars) are **stacked vertically**, spaced so consecutive memory banks
  stay within wire range.
- **No poles are needed** for realistic (long) songs.
- The timer "holds a place just like a track" (a slot at the top).

## History / what was tried

| Commit / state | What it did | Why it was wrong |
|---|---|---|
| `bb7304d` (base) | Compact drum rails (raw cells), no port_on_left. Classic port-on-right player, memory right of port in a 12-wide grid. | The memory grew TALL (12 wide, grew in Y) and the decoder attached perpendicular; user said "we are getting x and y the other way around". |
| `596f662` (parked) | `port_on_left=True` player: [memory \| decoder] per track, tracks stacked under the timer. | Wrong direction — memory ended up on the LEFT, decoder on the RIGHT, opposite of the requirement. |
| WIP (in stash `positioning-redesign-wip`) | Attempted: long horizontal 2-row memory on the left, poles bridging the clock bus between stacked tracks, timer as a top track. | Also wrong — user clarified memory must be on the RIGHT of the CC and grow right/wrap at the decoder's width, with NO poles. |

The stash (`git stash list`, message `positioning-redesign-wip`) contains the
last WIP: `_finalize_audio_composition` rewritten with a long-bar memory,
`_bridge_with_poles` (pole insertion), `_nearest_free` (collision nudge), and
timer placement in the empty right column.

## Correct implementation notes (for the future attempt)

1. **Revert to port-on-right** — the composed path should call
   `build_multi_rail_decoder_logical(..., port_on_left=False)` (the classic
   decoder: channels at `x = 0..cells_per_tick-1`, page-port CC at `x = 12`).
   Do NOT use `port_on_left=True`.
2. **Memory layout** — in `_finalize_audio_composition`, place each rail's pages
   right of its port with `cols = cells_per_tick`:
   ```python
   cols = _rail_cells_per_tick(ri)          # number of _sel channels
   for idx, eid in enumerate(mem_ri):
       col = idx % cols
       row = idx // cols
       m_ent.position = (pp_x + 1 + col, pp_y + row * 2)   # pp_x = 12, pp_y = port y
   ```
   This makes a piano bar 12 wide and a drum bar 1 wide.
3. **Stacking** — `port_y_ri = port_y_prev + max(24, 2*(rows_prev-1) + 2)` so
   decoders never overlap (~24 tall) AND consecutive memory banks stay within
   wire range (rows ≥ 8 → no poles; `_bridge_with_poles` covers tiny songs).
4. **Timer** — park its green clock output (the bridge AC) 2 tiles above track
   0's memory first row, in the empty column right of the decoder
   (`x = 13`, `y = track0_y - 2`) so the clock feed is a short wire (no pole).
5. **Clock/data buses** — row-snake over all memory inputs / outputs, splice the
   per-rail mod (`12, port_y+6`) and page port (`12, port_y`) in, splice the
   timer, then `_bridge_with_poles` as a safety net (must account for 1×2
   combinator footprints when finding free tiles).

## Constraints learned the hard way

- Factorio drops circuit wires > 9 tiles; `assert_wire_topology` enforces this.
- A combinator's input/output port allows at most 2 connections — splicing the
  timer into a pair (degree 2) is fine, but wiring it to a degree-2 cell makes a
  degree-3 overflow.
- Combinators are 1×2 tiles; any free-tile search must reserve `(x, y+1)` too.
- Power poles carry circuit wires fine in Draftsman
  (`bp.add_circuit_connection("green", a, pole, ...)` works, side → `"input"`).

## Where the code lives

- `src/factorio_display/cli.py` → `_finalize_audio_composition`
- `src/factorio_display/audio/player_blueprint.py` → `_build_rail_logical`,
  `build_multi_rail_decoder_logical` (port_on_left param)
- `src/factorio_display/audio/encoder.py` → `_layout_and_prewire_audio_bank`
  (currently caps memory at 12 columns via `min(12, ceil(sqrt(n)))`)
- `tests/test_audio_only_topology.py` → `test_large_audio_composition_all_wires_short`
