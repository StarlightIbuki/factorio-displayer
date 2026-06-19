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

### player_blueprint.py
Compact adjacent layout — entity sizes: CC=1×1, SPK=1×1, DC=1×2, AC=1×2

| Y   | Entity           | Purpose                        |
|-----|------------------|--------------------------------|
| 20  | Lookup CCs       | All sub-tick entries per ch    |
| 20  | Mod AC (col 12)  | `clock % 60 → sub_tick`        |
| 18  | Match DCs        | `each==sub_tick → signal=1`    |
| 16  | Selector ACs     | `each(red)*each(green)→bell`   |
| 16  | Page port (col12)| Constant combinator input      |
| 14  | l1 AC            | `bell >> 21`                   |
| 12  | s2 AC            | `bell >> 14`                   |
| 10  | l2 AC            | `s2 & 127`                     |
| 8   | s3 AC            | `bell >> 7`                    |
| 6   | l3 AC            | `s3 & 127`                     |
| 4   | l4 AC            | `bell & 127`                   |
| 0-3 | Speakers (12×4)  | 48 programmable speakers       |

Total entities: 48 spk + 85 AC + 12 DC + 13 CC = 158

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
