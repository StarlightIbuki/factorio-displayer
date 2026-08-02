# Factorio Displayer

A command-line tool designed to encode media (videos, gifs, images, and audio tracks) into Factorio memory blueprints, allowing you to build massive animated screens and programmable speakers directly in-game. Designed with Factorio 2.0 and Draftsman.

## Web App

Try it online: **<https://StarlightIbuki.github.io/factorio-displayer/>**

The web app lets you add media in the browser, edit a timeline (trim / crop /
audio, incl. MIDI), and generate a blueprint through the hosted API at
`https://factorio.qvq.moe:60012` (sign in with GitHub, or use an access token).
The frontend defaults to a local backend when present and falls back to the
public one automatically.

### Run the web app locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e '.[web,audio]'
.\.venv\Scripts\python.exe -m factorio_display server --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/> — the frontend auto-detects the local backend.
See `docs/deploy.md` for the full deployment guide (GitHub Pages frontend +
HTTPS high-port API, GitHub OAuth, access tokens).

## Demo

[The game save](https://github.com/StarlightIbuki/factorio-displayer/releases/download/alpha/Bad.Apple.demo.zip)

## Usage

Factorio Displayer runs via a CLI command that generates raw blueprint strings, which you can pipe straight into your clipboard (e.g., `| Set-Clipboard` on Windows or `| pbcopy` on macOS). 

### 1. Export the Physical Display Grid
Generates the base blueprint of your physical video display containing wired lamps.
```bash
factorio-display export-display --width 28 --height 28
```

### 2. Export the Audio Decoder

Generates the blueprint unit used to demultiplex and interpret the memory audio signal for programmable speakers.

```bash
factorio-display export-audio --instrument piano
```

Supported instruments: `piano`, `bass`, `celesta`, `plucked`, `drum`.

### 3. Encode Media

Converts a video file, GIF, or image sequence into Decider Combinator "Memory" arrays. MIDI files (`.mid`) are auto-detected and routed to the audio pipeline.

```bash
# Video
factorio-display encode ./bad_apple.mp4 --name "Bad Apple Frame Data" --adaptive --fps 30

# Audio (auto-detected from extension)
factorio-display encode ./song.mid --ticks-per-beat 30
```

_Tip: Use `--adaptive` and `--deduplicate` to save massive amounts of combinators by skipping idle frames and recycling identical frames._

### 4. Encode Audio (MIDI)

Dedicated subcommand for encoding `.mid` files with full control over translation parameters.

```bash
factorio-display encode-audio ./song.mid \
    --ticks-per-beat 30 \
    --boost-melody 1.5 \
    --attack-ticks 10 --decay-ticks 10 --sustain-level 0.8 --release-ticks 10 \
    -o song_audio.txt
```

| Option | Default | Description |
|--------|---------|-------------|
| `--ticks-per-beat` | `30` | Game ticks per quarter note (30 = real-time at any tempo) |
| `--boost-melody` | `1.0` | Velocity multiplier for the melody track (1.5 = 50% boost) |
| `--velocity-scale` | `1.0` | Global loudness multiplier |
| `--attack-ticks` | `10` | ADSR attack ramp duration in game ticks (70%→100%, 0 = off) |
| `--decay-ticks` | `10` | ADSR decay ramp duration in game ticks (100%→sustain, 0 = off) |
| `--sustain-level` | `1.0` | ADSR sustain level 0.0–1.0 (default 1.0 = no decay) |
| `--release-ticks` | `10` | ADSR release ramp duration in game ticks (sustain→0%, 0 = off) |
| `--attack-curve` | `1.0` | ADSR attack power-curve exponent (>1=gentle start, <1=snappy, 1=linear) |
| `--decay-curve` | `1.0` | ADSR decay power-curve exponent (>1=gentle, <1=snappy, 1=linear) |
| `--release-curve` | `1.0` | ADSR release power-curve exponent (>1=gentle, <1=snappy, 1=linear) |
| `--no-global-shift` | — | Disable optimal global octave shift; use only per-note folding |
| `--rail-mode` | `piano` | Multi-rail mode: `piano`, `all`, `auto[:threshold]`, or comma-separated instruments |
| `--map-drums` | (on) | Map GM drum notes (CH9, notes 24–81) to Factorio drum-kit sounds |
| `--no-attach-player` | — | Output audio memory pages only, without the player decoder blueprint |
| `--instruments` | — | Deprecated alias for `--rail-mode` |
| `--debug-json` | — | Dump raw tick_data as JSON for inspection |
| `--processed-midi` | — | Save octave-folded MIDI for preview in any player |
| `-o`, `--output` | — | Write blueprint to file instead of stdout |

### ADSR Envelope

The encoder applies a per-note ADSR (Attack-Decay-Sustain-Release) envelope to shape each note's loudness over time. All durations are in **game ticks** (60 ticks = 1 second at UPS 60).

```
loudness
  ^
  |     /\
  |    /  \______
  |   /           \
  |  /             \
  | /               \
  +----------------------> time (ticks)
     A   D    S     R
```

| Phase | Range | Behavior |
|-------|-------|----------|
| **Attack** | 0 → attack_ticks | Ramps from 70% to 100% of peak loudness |
| **Decay** | attack → attack+decay | Ramps from 100% down to sustain_level |
| **Sustain** | attack+decay → duration−release | Holds at sustain_level × peak |
| **Release** | duration−release → duration | Ramps from sustain_level down to 0% |

Each phase supports **power-curve shaping** via `--attack-curve`, `--decay-curve`, and `--release-curve`:
- `> 1.0` — gentle start, fast finish (convex, sounds "plucked")
- `= 1.0` — linear ramp (default)
- `< 1.0` — fast start, gentle finish (concave, sounds "bowed")

If the note is shorter than attack+decay+release, phases are shortened proportionally. Set all ADSR options to `0` and `--sustain-level 1.0` to disable the envelope entirely.

## Logical Blueprint Format

`factorio-display` supports an intermediate representation called the **Logical Blueprint** — a TOML-based format that describes *what* entities and circuit networks exist without committing to physical positions or explicit pairwise wiring.

This format is designed to be **LLM-friendly**: easy for language models to parse, generate, and modify.

### Key Concepts

- **Entity**: A combinator or speaker with an `id`, `type`, and type-specific properties. `position` and `direction` are optional.
- **Network**: A named virtual circuit network (`red` or `green`). Entities join a network via their endpoints (`"entity_id:port"`). When two endpoints are connected, their networks are merged (union-find semantics). Red, green, and power networks are always kept separate.
- **Endpoint**: A specific connection point on an entity — `"input"` (left side, where operands/conditions are read) or `"output"` (right side, where results are emitted).

### Pipeline

1. **Generate** a `LogicalBlueprint` (entities + networks, no positions).
2. **Layout** — assign tile positions and expand each `[[network]]` into short pairwise wires.
3. **Materialise** — convert to a draftsman `Blueprint` for final Factorio export.

### Example (Audio Decoder)

```toml
label = "Audio Decoder"

[[entity]]
id = "mod"
type = "arithmetic-combinator"
first_operand = "signal-clock"
operation = "%"
second_operand = 60
output_signal = "signal-M"

[[entity]]
id = "ch0_lut"
type = "constant-combinator"
[[entity.signal]]
  name = "iron-chest"
  quality = "normal"
  value = 60

[[entity]]
id = "ch0_match"
type = "decider-combinator"
[[entity.condition]]
  first = "signal-each"
  op = "="
  second_signal = "signal-M"
[[entity.output]]
  signal = "signal-each"
  copy_count = false
  constant = 1

[[entity]]
id = "spk_0"
type = "programmable-speaker"
instrument = "piano"
note = "F3"
vol_signal = "signal-F"
vol_quality = "normal"
polyphony = true

[[network]]
id = "red_0"
color = "red"
endpoints = ["mod:output", "ch0_match:input"]

[[network]]
id = "green_0"
color = "green"
endpoints = ["ch0_lut:output", "ch0_match:input"]
```

### CLI Usage

```bash
# Export the audio decoder as a logical blueprint (TOML)
factorio-display export-logical --instrument piano --name "My Decoder"

# Export via the existing export-audio command
factorio-display export-audio --instrument piano --format logical

# Encode a MIDI file to logical format
factorio-display encode-audio song.mid --format logical

# Convert blueprint string text to logical YAML
factorio-display blueprint-to-yaml blueprint.txt -o blueprint.yaml
```

### Programmatic API

```python
from factorio_display.logical_blueprint import (
    LogicalBlueprint, LogicalEntity, Endpoint, to_toml, from_toml,
  from_draftsman, from_blueprint_string, to_draftsman,
  to_yaml, blueprint_string_to_yaml,
)

# Build programmatically
lb = LogicalBlueprint(label="My Blueprint")
lb.add_entity(LogicalEntity("mod", "arithmetic-combinator", properties={
    "first_operand": "signal-clock", "operation": "%",
    "second_operand": 60, "output_signal": "signal-M",
}))
lb.add_entity(LogicalEntity("dc", "decider-combinator", properties={
    "conditions": [{"first": "signal-each", "op": "=", "second_signal": "signal-M"}],
    "outputs": [{"signal": "signal-each", "copy_count": False, "constant": 1}],
}))
lb.connect("red", Endpoint("mod", "output"), Endpoint("dc", "input"))

# Serialize to TOML
print(to_toml(lb))

# Parse from TOML
lb2 = from_toml(toml_string)

# Convert to/from draftsman Blueprint
bp = to_draftsman(lb)
lb3 = from_draftsman(bp)
lb4 = from_blueprint_string(bp.to_string())

# Serialize to YAML
print(to_yaml(lb))

# Convert blueprint string directly to logical YAML
print(blueprint_string_to_yaml(bp.to_string()))
```

## How It Works in Factorio

This project maps complex arrays of binary data (pixels, sound pitches, and timing) into Factorio's circuit network by exploiting the new mechanics introduced in Factorio 2.0 Space Age.

### Video Display Implementation

In Factorio, a color display must process an X/Y grid of RGB pixels.

1.  **Signal Multiplexing:** We don't have enough colored wires to assign one per pixel. Instead, each physical pixel on the display is statically assigned a _unique Factorio signal identity_ (e.g., "iron-plate" at "legendary" quality). This mapping is precomputed and distributed.
    
2.  **The Display Screen:** Thousands of small lamps form the screen. Every single lamp is permanently wired to the same network but is configured to only read the specific signal/quality pair assigned to its coordinates.
    
3.  **The Memory Banks:** We use an array of Decider Combinators acting as Read-Only Memory (ROM). Each combinator represents a single frame of video. The condition of the Decider Combinator checks if the global signal-clock matches its designated frame number.
    
4.  **Playback:** A global clock counts upward (+1 per tick). When the clock matches a frame's assigned time index, that specific Decider Combinator activates and dumps a massive payload of signals onto the red wire. The lamps instantly decode their specific signals from this payload and light up with the correct RGB value, effectively refreshing the screen 60 times a second.
    

### Audio Player Implementation

The audio decoder drives a 48-speaker matrix (12 semitones × 4 octaves, F3–E7) using a compact combinator layout with zero wasted tile rows. Each speaker is mapped to a unique `(signal_name, quality)` pair — natural notes use letter signals (F→`signal-F`), sharps offset by +10 (F#→`signal-P`), and Space Age quality tiers encode octave.

**Decoder pipeline (top → bottom):**

1.  **Modulo AC:** `clock % 60 → signal-M` — produces a sub-tick index (0–59) that cycles every 60 ticks.
2.  **Lookup CCs:** 12 constant combinators store the packed audio data for all 720 cells of the current page, keyed by sub-tick.
3.  **Match DCs:** `each == signal-M → signal=1` — the cell whose signal matches the current sub-tick outputs 1. Sub-tick 0 of each page plays nothing (its CC value would be 0, which Factorio drops from the network), so each page's first tick is silent by design.
4.  **Selector ACs:** `each(red) × each(green) → bell` — multiplies the memory page data (red wire) by the match output (green wire), isolating the packed integer for the current sub-tick onto the `bell` bus.
5.  **Unpacker AC chain (6 per channel):** Extracts the four 7-bit loudness values from the packed `bell` signal via bit-shifts and masks:
    - `l1 = bell >> 21`
    - `s2 = bell >> 14` → `l2 = s2 & 127`
    - `s3 = bell >> 7` → `l3 = s3 & 127`
    - `l4 = bell & 127`
6.  **Speakers:** 48 programmable speakers (4 rows × 12 columns), each listening on its assigned `(signal, quality)` pair with `allow_polyphony=True`.

**Total entities:** 48 spk + 85 AC + 12 DC + 13 CC = 158.  
**Wire colors:** RED = page data bus + sub_tick distribution, GREEN = CC lookup outputs + bell bus.

### Audio Normalization

Black MIDIs and dense orchestrations can produce summed loudness values far exceeding the 0–100 range used by the 7-bit packing scheme. By default, the encoder applies **global peak normalization**: after all notes are mixed, the entire tick dataset is linearly scaled so the loudest peak hits exactly 100. This preserves relative dynamics (a note 2× louder stays 2× louder) instead of hard-clipping everything above 100 to the same ceiling.

### Global Pitch Shift

MIDI files often use note ranges outside the 4-octave F3–E7 speaker matrix. The encoder handles this with two complementary strategies:

1. **Optimal global octave shift** (default, on): Before translating notes, the encoder scans all pitches across the entire MIDI file and finds the octave shift (multiple of ±12 semitones) that brings the largest number of unique notes into range. This is applied as a single bulk transposition — e.g., shifting a C4–C6 piece down by one octave to fit perfectly. Use `--no-global-shift` to disable.

2. **Per-note octave folding** (always on): Any note still outside range after the global shift is individually folded up/down by octaves until it fits. The encoder logs each folded note to stderr.

Together, these strategies ensure maximum note fidelity: the global shift aligns the sonic register with the speaker matrix, and per-note folding catches the remaining outliers.

## Roadmap

1. **All-in-one blueprint** — generate a single, fully-wired blueprint that includes the display/audio decoder, memory banks, and clock combinator, ready to place with zero manual wiring.
2. **Blueprint book output** — pack chunked memory blueprints into a Factorio blueprint book, with separated player blueprints for easy in-game organization.
3. **More instruments & 5-octave support** — extend beyond the current 4-octave (F3–E7) range to a full 5-octave speaker matrix, and add support for more Factorio instrument prototypes.
4. **Better display layout** — move power poles to the edges of the display grid (both sides) instead of placing them in the middle, for cleaner tiling when building large multi-unit screens.
