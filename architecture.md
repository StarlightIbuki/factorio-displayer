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

## Wire Colors

| Wire  | Carries                              |
|-------|--------------------------------------|
| RED   | Page data bus + sub_tick distribution|
| GREEN | CC lookup outputs, bell bus, intermediate signals |
