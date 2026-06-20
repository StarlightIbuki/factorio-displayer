# factorio-display Architecture Notes

## Audio Pipeline

### pitch_mapping.py
48 pitches (12 semitones × 4 octaves) → (signal_name, quality) pairs

- Natural notes use own letter (F→signal-F); sharps offset +10 (F#→signal-P)
- Qualities encode octave: normal=Oct3, uncommon=Oct4, rare=Oct5, epic=Oct6
- MIDI_BASE=53 (F3), SPEAKER_COUNT=48

### encoder.py
tick→[48 loudness] → [12 packed] per tick → flat index→value → DC pages

- Packing: 7-bit shifts: `(l1<<21)|(l2<<14)|(l3<<7)|l4`
- TICKS_PER_PAGE=60, CELLS_PER_PAGE=720 (60×12)
- Quality-first cell_offset→signal interleaving: `signal_pool[off//nqual], quality[off%nqual]`
- Pool needs ≥144 base signals (720/5 qualities)
- Each page = 1 decider combinator, tick-gated (clock >= start AND clock <= end)

#### Encoder input format

`encode_audio_memory(tick_data, name, signal_pool, qualities)` where
`tick_data[tick][speaker_idx] = loudness` (0–100, ≤ 7 bits):

```
speaker_idx  0..47  = 12 semitones × 4 octaves (F3..E7, MIDI 53..100)
  idx % 12   semitone: 0=F, 1=F#, 2=G, …, 11=E
  idx // 12  octave:   0=Oct3(normal), 1=Oct4(uncommon), 2=Oct5(rare), 3=Oct6(epic)
```

Speaker grid:
```
 0=F3   1=F#3  2=G3   3=G#3  4=A3   5=A#3  6=B3   7=C4   8=C#4  9=D4  10=D#4 11=E4
12=F4  13=F#4 14=G4  15=G#4 16=A4  17=A#4 18=B4  19=C5  20=C#5 21=D5  22=D#5 23=E5
24=F5  25=F#5 26=G5  27=G#5 28=A5  29=A#5 30=B5  31=C6  32=C#6 33=D6  34=D#6 35=E6
36=F6  37=F#6 38=G6  39=G#6 40=A6  41=A#6 42=B6  43=C7  44=C#7 45=D7  46=D#7 47=E7
```

MIDI: `midi_to_pitch_index(note)` → speaker_idx (None if outside range).

Capabilities:
- Full polyphony: all 48 speakers can ring simultaneously at any tick.
- Any duration: set `loudness > 0` for as many consecutive ticks as needed.
- Loudness curves: vary value per tick (ADSR envelopes, crescendos, etc.).
- Overlapping notes: sum loudness values per tick BEFORE encoding (the encoder
  does no mixing — caller must combine simultaneous notes).

### player_blueprint.py
Compact adjacent layout — entity sizes: CC=1×1, SPK=1×1, DC=1×2, AC=1×2

| Y   | Entity           | Purpose                        |
|-----|------------------|--------------------------------|
| 22  | Mod AC (col 12)  | `clock % 60 → signal-M`        |
| 22  | Lookup CCs       | sub-tick entries (t=0→60)      |
| 20  | Match DCs        | `each==signal-M → signal=1`    |
| 18  | Match0 DCs       | `signal-M==0 ∧ each==60 → 1`   |
| 16  | Selector ACs     | `each(red)*each(green)→bell`   |
| 16  | Page port (col12)| Constant combinator input      |
| 14  | l1 AC            | `bell >> 21`                   |
| 12  | s2 AC            | `bell >> 14`                   |
| 10  | l2 AC            | `s2 & 127`                     |
| 8   | s3 AC            | `bell >> 7`                    |
| 6   | l3 AC            | `s3 & 127`                     |
| 4   | l4 AC            | `bell & 127`                   |
| 0-3 | Speakers (12×4)  | 48 programmable speakers       |

Total entities: 48 spk + 85 AC + 24 DC + 13 CC = 170

Match0 DCs handle sub_tick=0 (value-0 signals are dropped by Factorio).
CC t=0 entry uses value 60 (never 0); other entries use t (1..59).

## Combinator Conventions (draftsman)

```python
# Arithmetic combinator
ac.set_arithmetic_condition(
    first_operand=signal_name, operation="%",
    second_operand=60, output_signal="signal-M",
)

# Decider combinator
dc.Condition(first_signal="signal-each", comparator="=", second_signal="signal-M")
dc.Output(signal="signal-each", copy_count_from_input=False, constant=1)

# Programmable speaker
spk.volume_signal = {"name": "signal-F", "quality": "normal"}
spk.volume_controlled_by_signal = True
spk.allow_polyphony = True

# Constant combinator
cc.set_signal(slot, name, value, quality)

# Wiring
blueprint.add_circuit_connection("red", id1, id2, side_1="output", side_2="input")
```

## Test Structure

- `conftest.py`: `large_signal_pool` (144 signals), `sample_qualities` (5 tiers)
- `test_encoder.py`, `test_player_blueprint.py`, `test_pitch_mapping.py`
- Blueprint parsing: `Blueprint.from_string(bp_str)` → inspect `.entities`
- Filter entities: `[e for e in bp.entities if "name-fragment" in e.name]`

### validate_blueprint_via_logical(bp_str)

Round-trips a blueprint string through `from_draftsman` → `LogicalBlueprint`
and checks structural invariants:
- Every entity has a valid type
- No duplicate endpoints across same-color networks
- No cross-color contamination within a network
- All endpoint entity ids exist
- Returns `{\"entity_count\", \"network_count\", \"errors\"}`

Call this in every test that produces a blueprint string.

## Logical Blueprint DSL (preferred authoring format)

**Always prefer the Logical Blueprint DSL** when generating or modifying combinator circuitry.  The DSL is defined in `architecture.md` under "Logical Blueprint DSL" and implemented in `src/factorio_display/logical_blueprint.py`.

### When to use Logical Blueprint vs raw draftsman

| Situation | Use |
|-----------|-----|
| New memory encoding (audio/video pages) | `encode_audio_to_logical()` → LogicalBlueprint → TOML |
| New decoder/player circuitry | `build_audio_decoder_logical()` → LogicalBlueprint → TOML |
| Modifying existing combinator layout | Edit LogicalBlueprint TOML, then `to_draftsman()` |
| Adding new entity types to the DSL | Extend `LogicalEntity` + TOML ser/de in `logical_blueprint.py` |
| Quick one-off draftsman tweak | Raw draftsman is acceptable for trivial changes |

### Key rules

1. **Entities first, positions later.**  Build the entity graph and networks;
   defer tile coordinates to a layout pass.
2. **Use networks, not wires.**  Instead of `add_circuit_connection("red", a, b)`,
   do `lb.connect("red", Endpoint("a", "output"), Endpoint("b", "input"))`.
3. **TOML is the interchange format.**  The canonical serialisation is TOML
   (`to_toml()` / `from_toml()`).  The draftsman string is the export format.
4. **Validate after generation.**  Call `validate_blueprint_via_logical(bp_str)`
   from `tests/conftest.py` in every test that produces a blueprint string.
5. **Check for unexpected warnings.**  Use `assert_no_unexpected_warnings(recorded_warnings)`
   from `tests/conftest.py` when generating blueprints in tests.
6. **Silent pages are omitted.**  Don't create DC entities for all-zero pages.

## Multi-rail and instrument notes

- `INSTRUMENT_MIDI_BASES` in `pitch_mapping.py` defines the F-aligned MIDI base
  per instrument (piano=53, bass=41, celesta=65, plucked=65, drum=53).
- `RAIL_WIDTH = 13` (12 channel columns + 1 page_port column).
- `_build_rail()` uses `r{ri}_` prefix for entity ids; cross-rail wiring uses
  `r{ri}_ch0_match`, `r{ri}_ch11_sel`, etc.
- When `map_drums=True`, drum-kit rails use `GM_DRUM_MAP` for note→sound mapping
  instead of pitch→signal; drum speakers have fixed note names like "kick-1".
- `midi_to_multi_rail_tick_data()` auto-detects instruments from GM program
  changes and returns `(instruments, rail_data)`.


## Wire Colors

| Wire  | Carries                              |
|-------|--------------------------------------|
| RED   | Unified signal bus — clock, sub-tick, DC page data, display lamp signals, progress bar |
| GREEN | Audio decoder: CC lookup outputs, bell bus, intermediate signals |

In the video all-in-one blueprint, the RED wire carries everything —
the raw clock (self-loop), the modulo clock (sub-tick for DC gating),
DC outputs (colour data), display lamp inputs, and progress bar.

## Logical Blueprint DSL

The **Logical Blueprint** is an intermediate TOML-based representation that
separates *what* entities and circuit networks exist from *where* they are
placed and *how* they are physically wired.  It is the **preferred authoring
format** for all new blueprint generation code.

### Why a DSL?

Traditional draftsman code mixes three concerns:
1. **Entity definition** — type, signals, conditions, outputs.
2. **Layout** — tile positions, directions.
3. **Wiring** — explicit pairwise `add_circuit_connection` calls.

This makes it hard to reorder combinators, share connection patterns, or have
an LLM reason about the circuit topology.  The Logical Blueprint DSL untangles
these concerns.

### DSL Schema (TOML)

```toml
label = "Blueprint name"

# ── Entities ──────────────────────────────────────────────────────
[[entity]]
id = "unique_id"            # required; string, unique within the blueprint
type = "arithmetic-combinator"   # one of the four types below
position = [x, y]           # optional; [int, int] tile coords
direction = 0               # optional; 0=North, 4=East, 8=South, 12=West

# ── Arithmetic combinator ─────────────────────────────────────────
[[entity]]
id = "mod"
type = "arithmetic-combinator"
first_operand = "signal-clock"   # signal name (string) or int constant
operation = "%"                  # + - * / % << >> AND OR XOR
second_operand = 60              # signal name or int
output_signal = "signal-M"       # signal name
first_operand_wires = ["red"]    # optional; wire color filters
second_operand_wires = ["green"] # optional

# ── Decider combinator ────────────────────────────────────────────
[[entity]]
id = "dc_page0"
type = "decider-combinator"
[[entity.condition]]
  first = "signal-clock"     # signal name
  op = ">="                  # > < >= <= = !=
  constant = 0               # int constant (use this OR second_signal)
[[entity.condition]]
  first = "signal-clock"
  op = "<="
  constant = 59
[[entity.output]]
  signal = "iron-chest@normal"   # "name@quality" or just "name"
  copy_count = false
  constant = 12345

# ── Constant combinator ───────────────────────────────────────────
[[entity]]
id = "lut_ch0"
type = "constant-combinator"
[[entity.signal]]
  name = "iron-chest"
  quality = "normal"         # optional; defaults to "normal"
  value = 60

# ── Programmable speaker ──────────────────────────────────────────
[[entity]]
id = "spk_0"
type = "programmable-speaker"
instrument = "piano"
note = "F3"
vol_signal = "signal-F"      # signal name (without "signal-" prefix OK too)
vol_quality = "normal"       # quality tier
polyphony = true
circuit_enabled = true

# ── Networks ──────────────────────────────────────────────────────
[[network]]
id = "red_bus"               # unique network id
color = "red"                # "red" or "green" (power is out of scope)
endpoints = [
  "mod:output",
  "dc_page0:input",
  "dc_page1:input",
]

[[network]]
id = "green_bus"
color = "green"
endpoints = ["dc_page0:output", "dc_page1:output"]
```

### Core Concepts

**Endpoint** — `"entity_id:port"` where port is `"input"` (left side, reads operands/conditions) or `"output"` (right side, emits results).

**Network** — a virtual circuit network of a single colour.  Entities join by
their endpoints.  When two endpoints are connected, their networks are
**merged** (union-find).  Red and green networks are always kept separate.

**Entity** — a combinator or speaker with an `id`, `type`, and type-specific
properties.  `position` and `direction` are optional — filled in later by the
layout pass.

### Network Merge Semantics

```
connect("red", "a:output", "b:input")   → new network red_0 {a:out, b:in}
connect("red", "a:output", "c:input")   → extends red_0 {a:out, b:in, c:in}
connect("red", "c:output", "d:input")   → new network red_1 {c:out, d:in}
connect("red", "b:input",  "c:output")  → merges red_0 ∪ red_1 → 4 endpoints
```

### Recommended Pipeline

```
 tick_data ──→ encode_audio_to_logical() ──→ LogicalBlueprint (no positions)
                    │
   MIDI ──→ midi_to_tick_data() ─────────────┘
                    │
   LogicalBlueprint ──→ layout pass (assign tile positions)
                    │
   LogicalBlueprint ──→ to_draftsman() ──→ Blueprint string (0e…)
```

### API Reference

| Function | Purpose |
|----------|---------|
| `LogicalBlueprint(label)` | Create empty logical blueprint |
| `lb.add_entity(LogicalEntity(id, type, properties))` | Add an entity |
| `lb.connect(color, Endpoint(a, port_a), Endpoint(b, port_b))` | Join endpoints; merges networks |
| `to_toml(lb)` | Serialize to LLM-friendly TOML string |
| `from_toml(toml_str)` | Parse TOML back to LogicalBlueprint |
| `from_draftsman(bp)` | Convert draftsman Blueprint → LogicalBlueprint |
| `to_draftsman(lb)` | Convert LogicalBlueprint → draftsman Blueprint |
| `encode_audio_to_logical(tick_data, …)` | Audio memory → LogicalBlueprint (audio/encoder.py) |
| `build_audio_decoder_logical(name, instrument, …)` | Decoder circuit → LogicalBlueprint (audio/player_blueprint.py) |
| `compose_all_in_one(…)` | Merge sub-blueprints, assign layout, connect ports → LogicalBlueprint (composer.py) |

## All-in-One Composer

The **Composer** (`composer.py`) merges sub-blueprints (display, timer,
progress bar, video/audio memory, audio player) into a single self-contained
blueprint.

### Architecture

```
                          ┌─────────────────┐
                          │   Raw Timer     │  (AC self-loop, RED)
                          │ signal-clock+1  │
                          └────────┬────────┘
                                   │ RED (clock)
                          ┌────────▼────────┐
                          │   Mod Timer     │  (clock % (total_ticks+1), RED)
                          │  signal-clock   │
                          └────────┬────────┘
                                   │ RED (sub-tick)
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
            ┌────────────┐ ┌────────────┐ ┌─────────────┐
            │ DC Pages   │ │ Progress   │ │ Audio       │
            │ (tick-gated│ │ Bar Lamps  │ │ Player      │
            │  outputs)  │ │            │ │ (optional)  │
            └─────┬──────┘ └────────────┘ └─────────────┘
                  │ RED (colour data)
            ┌─────▼──────┐
            │  Display   │
            │ Lamp Grid  │
            │  28×26     │
            └────────────┘
```

### Layout

The composer packs all non-display components (timer, progress bar, memory
banks, audio player) in a vertical column to the **right** of the display.
This keeps clock/control signals within short wire distance.

The display lamp grid remains at the origin; all combinator components are
placed starting at `x = display_max_x + 2`.

### Network wiring

Networks from different sub-blueprints are connected **explicitly by role**
(not by proximity):

| Connection | Source | Target |
|-----------|--------|--------|
| Clock | Raw timer output (RED) | Mod timer input (RED) |
| Sub-tick | Mod timer output (RED) | DC page inputs (RED) |
| Colour data | DC page outputs (RED) | Display lamp inputs (RED) |
| Sub-tick | Mod timer output (RED) | Progress bar lamps (RED) |

### Timer

The timer subsystem consists of:
- **Raw timer**: arithmetic combinator `clock = clock + 1` self-looping on RED.
  Uses `signal-clock` as the output signal.
- **Mod timer**: arithmetic combinator `clock % (total_ticks + 1) → clock`.
  Reads clock from RED (via `first_operand_wires: ["red"]`), outputs to RED.
  The `+1` ensures tick *N* maps to *N* (not 0), avoiding a blank frame at
  the end of the video.

### Progress bar

A horizontal row of 10 small-lamps (configurable `length`).  Lamp *i* lights
when `signal-clock >= ceil((i+1) * total_ticks / length)`.  All lamps connect
to the mod timer output network on RED.

### Design Rules

- **Prefer LogicalBlueprint for new code.**  Build entities and networks first;
  defer positions and wiring to a layout pass.
- **Networks replace pairwise `add_circuit_connection`.**  Don't wire entities
  directly; add them to the same network and let the layout engine materialise
  the wires.
- **Silent pages are omitted.**  DCs with zero outputs (all-zero tick data) are
  skipped entirely — the entity is never created.
- **TOML is the canonical serialisation.**  It is human-readable, diffable,
  and LLM-friendly.  The draftsman string is the *export* format.

## Video Display Pipeline

The display is a W×H grid of small-lamps (RGB), each assigned a unique
`(signal_name, quality)` pair from the signal pool.

### player_blueprint.py (video)
`build_display(name, width, height)` — generates a plain lamp-grid blueprint.

- No power poles or substations — the user places power in-game.
- Every pixel (0..W-1, 0..H-1) is a lamp.
- Red-wire chaining across rows + rightmost column for circuit connectivity.
- Always generates dynamically — no pre-computed blueprint is used.
- Default display size is **28×26** (W×H).

### encoder.py (video)
`encode_frames(...)` — converts an iterable of RGB frames into a combinator
blueprint.

- Frames are resized to (total_w, total_h) with cv2.
- **Power warning**: if both W > 28 and H > 28, a warning is emitted about
  needing multiple substations.
- Each frame → one decider combinator (DC) with:
  - **Conditions**: clock-based tick-gating (`clock >= start AND clock <= end`).
  - **Outputs**: one per pixel — `signal = color_int` (RGB packed as `R<<16|G<<8|B`),
    only for non-black pixels.
- DCs are laid out in a **snake-grid** (cols = `isqrt(2*count-1)+1`, max 26),
  wired on a single **red** unified signal bus (both input and output sides).
- **Deduplication**: identical frames share one DC with merged tick ranges.
- **Adaptive mode**: near-duplicate frames are dropped, extending the previous
  frame's tick range.

#### Vertical chunk splitting

When `W × H` exceeds the available unique signal pairs (`len(pool) × len(qualities)`),
the display is split into **disconnected vertical chunks**:

- `chunk_height = available // W` (rounded down to integer).
- Each chunk is a full-width horizontal strip of the display.
- Each chunk gets its own `SignalMapping` and DC bank.
- Chunk DC grids are stacked vertically with margin rows — **no wiring between chunks**.
- The user places each chunk's lamp grid and wires independently.

### SignalMapping
`SignalMapping(width, height, qualities, signal_pool)` — maps every pixel
`(x, y)` to a unique `(signal_name, quality)` pair.  All W×H pixels are
mapped — there are no holes or cutouts.

### resolve_dimensions
`resolve_dimensions(source_w, source_h, width=None, height=None)` returns
`(total_w, total_h)`.  If both are specified, uses them exactly.  If neither
is specified, returns `(DISPLAY_WIDTH, DISPLAY_HEIGHT)` — the fixed 28×26
display grid.  If only one is specified, computes the other from the source
aspect ratio.
