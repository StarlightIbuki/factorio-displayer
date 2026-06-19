"""Audio memory encoder — converts tick→loudness data into
Factorio audio-memory blueprint strings.

Pipeline
--------
1.  Accept ``tick → [48 loudness values]`` (0–100 each, ≤ 7 bits).
2.  Pack each group of 4 loudness values into one integer::

        packed = l4 + 100*l3 + 10000*l2 + 1000000*l1

    yielding ``tick → [12 packed integers]``.

3.  Flatten to a single index space::

        index = tick * 12 + offset   (offset = 0..11)

    producing ``index → packed_integer``.

4.  Encode via the integer2signal pool: each decider combinator stores
    ``PAGE_SIZE`` consecutive values on the pool's unique (name, quality)
    pairs.  Multiple pages are tick-gated so only one page outputs at a time.
"""

from __future__ import annotations

import math
import sys
from typing import Sequence

import mido

from .pitch_mapping import SPEAKER_COUNT


# ── packing / unpacking constants ──────────────────────────────────────

# 4 loudness values (0–100, ≤ 7 bits each) per packed integer.
# We use 7-bit packing so values 0–100 fit cleanly without carry ambiguity.
# Packed layout: (l1 << 21) | (l2 << 14) | (l3 << 7) | l4
CHANNELS_PER_TICK = SPEAKER_COUNT // 4  # = 12
PACK_SHIFT = 7               # bits per loudness value
PACK_MASK = (1 << PACK_SHIFT) - 1  # = 127
PACK_SHIFTS = [PACK_SHIFT * i for i in range(4)]  # [0, 7, 14, 21]
# Fixed page size — the decoder uses ``clock % 60`` for sub-tick indexing.
TICKS_PER_PAGE = 60
CELLS_PER_PAGE = TICKS_PER_PAGE * CHANNELS_PER_TICK  # = 720

def pack_four(l1: int, l2: int, l3: int, l4: int) -> int:
    """Pack four 0–100 loudness values into one integer using 7-bit shifts.

    ``l1`` is the most significant group (bits 21–27).
    """
    return (
        (l1 << PACK_SHIFTS[3])
        | (l2 << PACK_SHIFTS[2])
        | (l3 << PACK_SHIFTS[1])
        | l4
    )


def unpack_four(packed: int) -> tuple[int, int, int, int]:
    """Unpack a single packed integer into ``(l1, l2, l3, l4)``."""
    l4 = packed & PACK_MASK
    l3 = (packed >> PACK_SHIFTS[1]) & PACK_MASK
    l2 = (packed >> PACK_SHIFTS[2]) & PACK_MASK
    l1 = (packed >> PACK_SHIFTS[3]) & PACK_MASK
    return l1, l2, l3, l4


# ── tick data helpers ──────────────────────────────────────────────────

def loudness_to_packed(
    tick_data: Sequence[Sequence[int]],
) -> list[list[int]]:
    """Convert ``tick_data[tick][speaker_idx]`` → ``tick_data[tick][channel]``.

    Each inner list moves from 48 loudness values to 12 packed integers.

    Packing groups the SAME semitone across 4 octaves into one integer,
    matching the player's unpacker which routes all 4 sub-values to
    speakers of the same semitone in different octaves::

        Channel ch → semitone ch:  pitch[ch+0*12], pitch[ch+1*12],
                                    pitch[ch+2*12], pitch[ch+3*12]
    """
    SEMITONES = 12
    OCTAVES = 4
    result: list[list[int]] = []
    for tick_loudness in tick_data:
        if len(tick_loudness) != SPEAKER_COUNT:
            raise ValueError(
                f"Expected {SPEAKER_COUNT} loudness values per tick, "
                f"got {len(tick_loudness)}"
            )
        packed: list[int] = []
        for semitone in range(SEMITONES):
            packed.append(
                pack_four(
                    tick_loudness[semitone + 0 * SEMITONES],  # octave 3
                    tick_loudness[semitone + 1 * SEMITONES],  # octave 4
                    tick_loudness[semitone + 2 * SEMITONES],  # octave 5
                    tick_loudness[semitone + 3 * SEMITONES],  # octave 6
                )
            )
        result.append(packed)
    return result


def flatten_packed(
    packed_per_tick: list[list[int]],
) -> list[int]:
    """Flatten ``tick→[12 packed]`` into ``flat_index→packed_value``.

    ``flat_index = tick * 12 + offset``.
    """
    result: list[int] = []
    for tick_values in packed_per_tick:
        result.extend(tick_values)
    return result


# ── page layout ────────────────────────────────────────────────────────


def compute_page_layout(
    total_cells: int,
    num_base_signals: int,
    num_qualities: int,
) -> tuple[int, int, int]:
    """Return ``(page_count, cells_per_page, ticks_per_page)``.

    Pages are fixed at *CELLS_PER_PAGE* (=720) cells each, matching the
    decoder's ``clock % 60`` sub-tick selection.  The caller must ensure
    ``num_base_signals * num_qualities >= CELLS_PER_PAGE``.
    """
    cells_per_page = CELLS_PER_PAGE
    page_count = math.ceil(total_cells / cells_per_page) if total_cells > 0 else 0
    return page_count, cells_per_page, TICKS_PER_PAGE


# ── main encoder entry point ───────────────────────────────────────────


def encode_audio_memory(
    tick_data: Sequence[Sequence[int]],
    output_name: str,
    signal_pool: list[str],
    qualities: list[str],
    clock_signal: str = "signal-clock",
) -> str:
    """Encode tick→loudness data into an audio-memory blueprint string.

    Each decider combinator page covers up to ``cells_per_page`` flat
    indices using ALL ``signal_pool × qualities`` unique signal pairs.
    The DC is gated by a tick-range condition (like the video encoder).
    Multiple pages are chained; only one page outputs at a time.

    Decoder formula::
        page_idx   = tick // ticks_per_page
        in_page    = tick  % ticks_per_page
        flat_idx   = page_idx * cells_per_page + in_page * 12 + channel
        signal_idx = flat_idx % cells_per_page
    """
    from draftsman.blueprintable import Blueprint
    from draftsman.constants import Direction
    from draftsman.entity import DeciderCombinator, new_entity

    if not tick_data:
        sys.stderr.write("No audio data to encode.\n")
        return ""

    num_base = len(signal_pool)
    num_qual = len(qualities)

    if num_base == 0:
        raise ValueError("signal_pool must not be empty")

    # The pool must have enough signals to fill one page (720 cells).
    needed_base = math.ceil(CELLS_PER_PAGE / num_qual)
    if num_base < needed_base:
        raise ValueError(
            f"signal_pool has {num_base} base signals × {num_qual} qualities = "
            f"{num_base * num_qual} unique pairs, but {CELLS_PER_PAGE} are needed "
            f"for a {TICKS_PER_PAGE}-tick page.  Need at least {needed_base} base signals."
        )

    # 1. Pack
    packed = loudness_to_packed(list(tick_data))
    total_ticks = len(packed)

    # 2. Flatten
    flat: list[int] = []
    for tick_vals in packed:
        flat.extend(tick_vals)
    total_cells = len(flat)

    # 3. Page layout — fixed at CELLS_PER_PAGE (=720) cells per page
    page_count = math.ceil(total_cells / CELLS_PER_PAGE)

    sys.stderr.write(
        f"Audio: {total_ticks} ticks → {total_cells} cells → "
        f"{page_count} page(s) "
        f"({CELLS_PER_PAGE} cells/page = {needed_base} base × {num_qual} qual, "
        f"{TICKS_PER_PAGE} ticks/page).\n"
    )

    # 4. Build blueprint — snake-grid layout
    blueprint = Blueprint()
    blueprint.label = f"Audio Memory: {output_name}"

    cols = max(1, math.isqrt(max(0, 2 * page_count - 1)) + 1) if page_count > 0 else 1
    rows = (page_count + cols - 1) // cols

    dc_grid: dict[tuple[int, int], str] = {}

    # Pre-compute cell_offset → (signal_name, quality) using
    # quality-first interleaving:
    #   signal_pool[cell_offset // num_qual] × qualities[cell_offset % num_qual]

    for page_idx in range(page_count):
        col = page_idx % cols
        row = page_idx // cols
        dc_id = f"ap{page_idx}"
        tile_row = row * 2

        # Tick range for this page
        page_start_cell = page_idx * CELLS_PER_PAGE
        page_end_cell = page_start_cell + CELLS_PER_PAGE
        tick_start = page_start_cell // CHANNELS_PER_TICK
        tick_end = (min(page_end_cell, total_cells) - 1) // CHANNELS_PER_TICK

        # Condition: clock >= tick_start AND clock <= tick_end
        conditions = [
            DeciderCombinator.Condition(
                first_signal={"name": clock_signal},
                comparator=">=",
                constant=tick_start,
            ),
            DeciderCombinator.Condition(
                first_signal={"name": clock_signal},
                comparator="<=",
                constant=tick_end,
                compare_type="and",
            ),
        ]

        # Outputs: one per signal×quality pair for every cell in this page
        outputs: list = []
        for cell_offset in range(CELLS_PER_PAGE):
            flat_idx = page_start_cell + cell_offset
            if flat_idx >= total_cells:
                break  # past end of real data; remaining slots stay silent

            value = flat[flat_idx]
            if value == 0:
                continue

            signal_idx = cell_offset // num_qual
            quality_idx = cell_offset % num_qual
            signal_name = signal_pool[signal_idx]
            quality = qualities[quality_idx]

            outputs.append(
                DeciderCombinator.Output(
                    signal={"name": signal_name, "quality": quality},
                    copy_count_from_input=False,
                    constant=value,
                )
            )

        dc = new_entity(
            "decider-combinator",
            id=dc_id,
            tile_position=(col, tile_row),
            direction=Direction.SOUTH,
        )
        dc.conditions = conditions
        dc.outputs = outputs
        blueprint.entities.append(dc)
        dc_grid[(row, col)] = dc_id

    # Wire pages — green for clock/input bus, red for output bus
    prev_id: str | None = None
    for r in range(rows):
        col_iter = range(cols) if r % 2 == 0 else range(cols - 1, -1, -1)
        for c in col_iter:
            dc_id = dc_grid.get((r, c))
            if dc_id is None:
                continue
            if prev_id is not None:
                blueprint.add_circuit_connection(
                    "green", prev_id, dc_id, side_1="input", side_2="input",
                )
                blueprint.add_circuit_connection(
                    "red", prev_id, dc_id, side_1="output", side_2="output",
                )
            prev_id = dc_id

    sys.stderr.write(
        f"Audio memory: {page_count} DCs, {total_cells} cells, "
        f"{total_ticks} ticks.\n"
    )
    return blueprint.to_string()


# ── auto-detect convenience (MIDI → encoder bridge) ───────────────────


def encode_audio_auto(
    path: str,
    **kwargs: object,
) -> str:
    """Auto-detect audio format and encode to an audio-memory blueprint.

    Currently supports ``.mid`` / ``.midi`` files via :func:`midi_to_tick_data`.

    Keyword arguments are forwarded to :func:`midi_to_tick_data`:
    ``ticks_per_beat``, ``boost_melody``, ``velocity_scale``,
    ``attack_ticks``, ``decay_ticks``, ``sustain_level``, ``release_ticks``,
    ``processed_midi_path``, ``debug_json_path``.
    """
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext not in ("mid", "midi"):
        sys.stderr.write(f"Audio auto-encode: unsupported format: {path}\n")
        return ""

    from .. import SIGNAL_POOL, QUALITIES, CLOCK_SIGNAL
    from .midi_translator import midi_to_tick_data

    # Gather midi_translator kwargs
    midi_kwargs: dict[str, object] = {}
    for key in (
        "ticks_per_beat", "boost_melody", "velocity_scale",
        "attack_ticks", "decay_ticks", "sustain_level", "release_ticks",
        "processed_midi_path",
    ):
        if key in kwargs:
            midi_kwargs[key] = kwargs[key]

    sys.stderr.write(f"Loading MIDI: {path}\n")
    mid = mido.MidiFile(path)
    sys.stderr.write(
        f"  {len(mid.tracks)} track(s), "
        f"ticks_per_beat={mid.ticks_per_beat}, "
        f"length={mid.length:.1f}s\n"
    )

    float_data = midi_to_tick_data(mid, **midi_kwargs)  # type: ignore[arg-type]

    # ── debug JSON dump ──────────────────────────────────────────────
    debug_json_path = kwargs.get("debug_json_path")
    if debug_json_path and isinstance(debug_json_path, str):
        import json
        # Round to 3 decimal places for readability
        json_data = [[round(v, 3) for v in tick] for tick in float_data]
        with open(debug_json_path, "w") as f:
            json.dump(json_data, f)
        sys.stderr.write(f"Debug JSON written to: {debug_json_path}\n")

    # Round float → int, clip to 0..100
    int_data: list[list[int]] = [
        [max(0, min(100, int(round(v)))) for v in tick]
        for tick in float_data
    ]

    signal_pool = list(SIGNAL_POOL)
    qualities = list(QUALITIES)

    if not signal_pool:
        sys.stderr.write("Warning: SIGNAL_POOL is empty, audio encoding may fail.\n")

    return encode_audio_memory(
        int_data,
        output_name=path,
        signal_pool=signal_pool,
        qualities=qualities,
        clock_signal=CLOCK_SIGNAL,
    )
