# Factorio Displayer

A command-line tool designed to encode media (videos, gifs, images, and audio tracks) into Factorio memory blueprints, allowing you to build massive animated screens and programmable speakers directly in-game. Designed with Factorio 2.0 and Draftsman.

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
    --attack-ticks 2 --decay-ticks 4 --sustain-level 0.8 --release-ticks 6 \
    -o song_audio.txt
```

| Option | Default | Description |
|--------|---------|-------------|
| `--ticks-per-beat` | `30` | Game ticks per quarter note (30 = real-time at any tempo) |
| `--boost-melody` | `1.0` | Velocity multiplier for the highest-pitch track (1.5 = 50% boost) |
| `--velocity-scale` | `1.0` | Global velocity multiplier |
| `--attack-ticks` | `0` | ADSR attack ramp duration (70%→100%) |
| `--decay-ticks` | `0` | ADSR decay ramp duration (100%→sustain) |
| `--sustain-level` | `1.0` | ADSR sustain level (0.0–1.0) |
| `--release-ticks` | `0` | ADSR release ramp duration (sustain→0%) |
| `--debug-json` | — | Dump raw tick_data as JSON for inspection |
| `--processed-midi` | — | Save octave-folded MIDI for preview in any player |
| `-o`, `--output` | — | Write blueprint to file instead of stdout |

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
3.  **Match DCs:** `each == signal-M → signal=1` — for sub-ticks 1–59, the cell whose signal matches the current sub-tick outputs 1. A separate set of match0 DCs handles sub-tick 0 (since Factorio drops 0-value signals).
4.  **Selector ACs:** `each(red) × each(green) → bell` — multiplies the memory page data (red wire) by the match output (green wire), isolating the packed integer for the current sub-tick onto the `bell` bus.
5.  **Unpacker AC chain (6 per channel):** Extracts the four 7-bit loudness values from the packed `bell` signal via bit-shifts and masks:
    - `l1 = bell >> 21`
    - `s2 = bell >> 14` → `l2 = s2 & 127`
    - `s3 = bell >> 7` → `l3 = s3 & 127`
    - `l4 = bell & 127`
6.  **Speakers:** 48 programmable speakers (4 rows × 12 columns), each listening on its assigned `(signal, quality)` pair with `allow_polyphony=True`.

**Total entities:** 48 spk + 85 AC + 24 DC + 13 CC = 170.  
**Wire colors:** RED = page data bus + sub_tick distribution, GREEN = CC lookup outputs + bell bus.

### Audio Normalization

Black MIDIs and dense orchestrations can produce summed loudness values far exceeding the 0–100 range used by the 7-bit packing scheme. By default, the encoder applies **global peak normalization**: after all notes are mixed, the entire tick dataset is linearly scaled so the loudest peak hits exactly 100. This preserves relative dynamics (a note 2× louder stays 2× louder) instead of hard-clipping everything above 100 to the same ceiling.

## Roadmap

1. **All-in-one blueprint** — generate a single, fully-wired blueprint that includes the display/audio decoder, memory banks, and clock combinator, ready to place with zero manual wiring.
2. **Blueprint book output** — pack chunked memory blueprints into a Factorio blueprint book, with separated player blueprints for easy in-game organization.
3. **More instruments & 5-octave support** — extend beyond the current 4-octave (F3–E7) range to a full 5-octave speaker matrix, and add support for more Factorio instrument prototypes.
4. **Better display layout** — move power poles to the edges of the display grid (both sides) instead of placing them in the middle, for cleaner tiling when building large multi-unit screens.
