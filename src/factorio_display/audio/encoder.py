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
from pathlib import Path
from typing import Sequence

import mido

from ..cache_paths import (
    cache_json_get,
    cache_json_put,
    cache_key,
)  # pylint: disable=relative-beyond-top-level
from .pitch_mapping import SPEAKER_COUNT, drum_grouping  # pylint: disable=relative-beyond-top-level,import-outside-toplevel


def _audio_drums_enabled(kwargs: dict[str, object]) -> bool:
    """Whether audio drum detection is opted in.

    Accepts ``drums`` / ``audio_drums`` kwargs that are truthy and not the
    string ``"off"`` (so ``--drums off`` and ``--no-drums`` both disable).
    """
    for key in ("drums", "audio_drums"):
        val = kwargs.get(key)
        if val is not None:
            if isinstance(val, str):
                return val.lower() not in ("", "off", "false", "none", "0")
            return bool(val)
    return False


def _drum_rail_to_int(drum_rail: list[list[float]]) -> list[list[int]]:
    """Convert a float drum rail ``[tick][48]`` to clamped 0-100 ints."""
    return [
        [max(0, min(100, int(round(v)))) for v in tick]
        for tick in drum_rail
    ]


def _file_identity(path: str) -> str:
    """Return a stable identity for an input file: resolved path + mtime + size.

    Used to invalidate audio-analysis caches when the source file changes.
    """
    try:
        st = Path(path).stat()
        return f"{Path(path).resolve()}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        return path


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

# Drum rails only record each used drum's loudness (1 cell/tick), so their
# pages can cover far more ticks per DC than the 60-tick melodic pages —
# shrinking the drum memory from one DC per second to one per ~10 seconds.
# The exact value is clamped to the signal pool per rail (see cli.py).
DRUM_TICKS_PER_PAGE = 600

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


# ── loudness normalization ─────────────────────────────────────────

def normalize_tick_data(
    tick_data: list[list[float]],
    target_max: float = 100.0,
) -> list[list[float]]:
    """Scale all loudness values so the global peak does not exceed *target_max*.

    Returns the original data unchanged if the global maximum is already
    ≤ *target_max* or if every value is 0.
    """
    if not tick_data:
        return tick_data

    global_max = max(
        (v for tick in tick_data for v in tick),
        default=0.0,
    )

    if global_max <= 0.0 or global_max <= target_max:
        return tick_data

    scale = target_max / global_max
    sys.stderr.write(
        f"Audio normalize: global peak {global_max:.1f} → "
        f"{target_max:.0f} (scale={scale:.4f})\n"
    )

    return [[v * scale for v in tick] for tick in tick_data]


# ── tick data helpers ──────────────────────────────────────────────────

def loudness_to_packed(
    tick_data: Sequence[Sequence[int]],
    grouping: Sequence[Sequence[int | None]] | None = None,
) -> list[list[int]]:
    """Convert ``tick_data[tick][speaker_idx]`` → ``tick_data[tick][cell]``.

    Each inner list moves from loudness values to packed integers: every
    cell packs up to 4 loudnesses (one per lane, 7 bits each) into one
    integer.  *grouping* is a per-cell list of 4 pitch indices (``None`` =
    silent lane); when omitted it uses the generic 48-speaker layout that
    packs the SAME semitone across 4 octaves into one integer, matching the
    player's unpacker which routes all 4 sub-values to speakers of the same
    semitone in different octaves::

        Channel ch → semitone ch:  pitch[ch+0*12], pitch[ch+1*12],
                                    pitch[ch+2*12], pitch[ch+3*12]

    A compact drum rail passes its own grouping.  A cell with a single lane
    (a raw tick→volume drum cell) is stored directly — no packing, so every
    bit encodes the volume and the decoder needs no unpacker.
    """
    if grouping is None:
        grouping = [
            [semitone + octave * 12 for octave in range(4)]
            for semitone in range(12)
        ]
    result: list[list[int]] = []
    for tick_loudness in tick_data:
        if len(tick_loudness) != SPEAKER_COUNT:
            raise ValueError(
                f"Expected {SPEAKER_COUNT} loudness values per tick, "
                f"got {len(tick_loudness)}"
            )
        packed: list[int] = []
        for lanes in grouping:
            if len(lanes) == 1:
                # Raw tick→volume cell: the cell value IS the loudness.
                packed.append(tick_loudness[lanes[0]] if lanes[0] is not None else 0)
            else:
                packed.append(
                    pack_four(
                        tick_loudness[lanes[0]] if lanes[0] is not None else 0,
                        tick_loudness[lanes[1]] if lanes[1] is not None else 0,
                        tick_loudness[lanes[2]] if lanes[2] is not None else 0,
                        tick_loudness[lanes[3]] if lanes[3] is not None else 0,
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
    _num_base_signals: int,
    _num_qualities: int,
) -> tuple[int, int, int]:
    """Return ``(page_count, cells_per_page, ticks_per_page)``.

    Pages are fixed at *CELLS_PER_PAGE* (=720) cells each, matching the
    decoder's ``clock % 60`` sub-tick selection.
    """
    cells_per_page = CELLS_PER_PAGE
    page_count = math.ceil(total_cells / cells_per_page) if total_cells > 0 else 0
    return page_count, cells_per_page, TICKS_PER_PAGE


def _layout_and_prewire_audio_bank(
    lb: "LogicalBlueprint",  # noqa: F821
    dc_ids: list[str],
    *,
    connectors: bool = False,
    connector_label: str | None = None,
    fragment_index: int | None = None,
) -> None:
    """Assign compact positions and deterministic internal bus prewiring.

    When *connectors* is True (split-output mode), the whole bank is rotated
    90° CCW — matching the player's orientation — so the page grid becomes a
    horizontal strip, and constant-combinator connectors are placed at the
    LEFT and RIGHT ends of the strip.  Each connector is wired into BOTH the
    green clock (time) bus and the red output (data) bus and carries
    *connector_label* at value 1 with the CC "Output" toggle OFF
    (``enabled=False``) — visible on the map but adding nothing to either
    bus, used for in-game wiring to the timer (left) and player (right).
    A non-wired series-label CC carries ``fragment_index + 1``.

    The connectors are attached BEFORE the prewired pairs are set: joining a
    new endpoint to a pre-wired network wipes its prewired pairs, so the
    full deterministic wiring (snake + connector pairs) is assigned last.
    """
    from ..logical_blueprint import Endpoint  # pylint: disable=import-outside-toplevel

    n = len(dc_ids)
    if n == 0:
        return

    # Cap at 12 columns so a multi-rail memory bank stays within the 13-tile
    # rail spacing and never overlaps the neighbouring rail's player (a
    # sqrt-based width grows to 15+ columns for 200+ pages).
    cols = min(12, max(1, math.ceil(math.sqrt(n))))
    rows = math.ceil(n / cols)

    # ── Place DCs ───────────────────────────────────────────────
    # All-in-one: north-facing grid ``(col, row*2)``.  Split mode: rotate the
    # whole grid 90° CCW (matching the player) so each north 1×2 DC at
    # ``(col, row*2)`` becomes a west 2×1 DC at ``(-(row*2+2), col)`` — the
    # bank is now a horizontal strip ~``2*rows`` tiles wide × ``cols`` tall,
    # aligned with the rotated player.
    for idx, dc_id in enumerate(dc_ids):
        ent = lb.entities.get(dc_id)
        if ent is None:
            continue
        row, col = idx // cols, idx % cols
        if connectors:
            ent.position = (-(row * 2 + 2), col)
            ent.direction = 12  # west (90° CCW from north)
        else:
            ent.position = (col, row * 2)

    input_anchor = Endpoint(dc_ids[0], "input")
    output_anchor = Endpoint(dc_ids[0], "output")

    # ── Connectors (split mode only) — attach FIRST ─────────────
    conn_eps: tuple[str, str, str, str] | None = None
    if connectors and connector_label:
        conn_eps = _attach_rotated_connectors(
            lb, dc_ids, cols, rows, input_anchor, output_anchor,
            connector_label, fragment_index,
        )

    def _conn_pairs(color: str) -> list[tuple[Endpoint, Endpoint]]:
        if conn_eps is None:
            return []
        left, right, left_dc, right_dc = conn_eps
        if color == "green":
            return [
                (Endpoint(left, "input"), Endpoint(left_dc, "input")),
                (Endpoint(right, "input"), Endpoint(right_dc, "input")),
            ]
        return [
            (Endpoint(left, "input"), Endpoint(left_dc, "output")),
            (Endpoint(right, "input"), Endpoint(right_dc, "output")),
        ]

    if n <= 1:
        # A single page has no snake bus, but the connector CCs still join
        # the (explicitly declared) clock/data networks.
        for net in lb.networks:
            if net.color == "green" and input_anchor in net.endpoints:
                net.prewired_pairs = _conn_pairs("green")
            elif net.color == "red" and output_anchor in net.endpoints:
                net.prewired_pairs = _conn_pairs("red")
        return

    # ── Snake bus pairs (grid order — rotation is rigid, wires stay short) ─
    snake_ids: list[str] = []
    for row in range(rows):
        row_start = row * cols
        row_end = min(row_start + cols, n)
        row_ids = dc_ids[row_start:row_end]
        if row % 2 == 1:
            row_ids = list(reversed(row_ids))
        snake_ids.extend(row_ids)

    in_pairs = [
        (Endpoint(snake_ids[i], "input"), Endpoint(snake_ids[i + 1], "input"))
        for i in range(len(snake_ids) - 1)
    ]
    out_pairs = [
        (Endpoint(snake_ids[i], "output"), Endpoint(snake_ids[i + 1], "output"))
        for i in range(len(snake_ids) - 1)
    ]

    for net in lb.networks:
        if net.color == "green" and input_anchor in net.endpoints:
            net.prewired_pairs = list(in_pairs) + _conn_pairs("green")
        elif net.color == "red" and output_anchor in net.endpoints:
            net.prewired_pairs = list(out_pairs) + _conn_pairs("red")


def _attach_rotated_connectors(
    lb: "LogicalBlueprint",  # noqa: F821
    dc_ids: list[str],
    cols: int,
    rows: int,
    input_anchor: "Endpoint",  # noqa: F821
    output_anchor: "Endpoint",  # noqa: F821
    connector_label: str,
    fragment_index: int | None,
) -> tuple[str, str, str, str]:
    """Add left/right connector CCs to a 90°-CCW-rotated audio-memory bank.

    The strip's DC grid spans post columns ``-(rows*2) .. -2`` on row 0; the
    LEFT connector sits one tile outside the left end and the RIGHT connector
    one tile outside the right end.  Each joins the green clock (time) bus
    and the red output (data) bus via the nearest DC on that row.

    Returns ``(left_id, right_id, left_dc, right_dc)`` so the caller can fold
    the connector wires into the buses' prewired pairs.
    """
    from ..logical_blueprint import Endpoint, LogicalEntity  # pylint: disable=import-outside-toplevel

    first_dc = dc_ids[0]
    left_id = f"{first_dc}_ccL"
    right_id = f"{first_dc}_ccR"
    lb.add_entity(LogicalEntity(
        left_id, "constant-combinator",
        properties={
            "signals": [{"name": connector_label, "value": 1}],
            "enabled": False,
        },
        position=(-(rows * 2 + 1), 0),  # left of the strip's top row
    ))
    lb.add_entity(LogicalEntity(
        right_id, "constant-combinator",
        properties={
            "signals": [{"name": connector_label, "value": 1}],
            "enabled": False,
        },
        position=(0, 0),  # right of the strip's top row
    ))
    if fragment_index is not None:
        lb.add_entity(LogicalEntity(
            f"{first_dc}_label", "constant-combinator",
            properties={
                "signals": [{"name": "signal-info", "value": fragment_index + 1}],
                "enabled": False,
            },
            position=(0, 1),  # just right of the strip, below the right connector
        ))
    # Nearest DCs on the strip's top row: right = first DC (post tile (-2, 0),
    # spanning tiles -2..-1), left = the DC from the last grid row / first
    # column → post tile (-(rows*2), 0) spanning -(rows*2)..-(rows*2+1).
    right_dc = first_dc
    left_dc = dc_ids[(rows - 1) * cols] if rows > 1 else first_dc
    # Join the green clock (time) bus — via the nearest DC's input.
    lb.connect("green", Endpoint(left_id, "input"), Endpoint(left_dc, "input"))
    lb.connect("green", Endpoint(right_id, "input"), Endpoint(right_dc, "input"))
    # Join the red data bus — via the nearest DC's output.
    lb.connect("red", Endpoint(left_id, "input"), Endpoint(left_dc, "output"))
    lb.connect("red", Endpoint(right_id, "input"), Endpoint(right_dc, "output"))
    return left_id, right_id, left_dc, right_dc


# ── main encoder entry point ───────────────────────────────────────────


def encode_audio_memory(
    tick_data: Sequence[Sequence[int]],
    output_name: str,
    signal_pool: list[str],
    qualities: list[str],
    clock_signal: str = "signal-clock",
    blueprint: "Blueprint | None" = None,  # noqa: F821
    y_offset: int = 0,
    x_offset: int = 0,
    id_prefix: str = "",
    grouping: Sequence[Sequence[int | None]] | None = None,
    ticks_per_page: int = TICKS_PER_PAGE,
) -> str:
    """Encode tick→loudness data into an audio-memory blueprint string.

    Each decider combinator page covers up to ``cells_per_page`` flat
    indices using ALL ``signal_pool × qualities`` unique signal pairs.
    The DC is gated by a tick-range condition (like the video encoder).
    Multiple pages are chained; only one page outputs at a time.

    *grouping* (optional) selects the per-tick cell packing — see
    :func:`loudness_to_packed`.  A compact drum rail passes only the drum
    types the song uses, so its pages hold far fewer cells.

    Decoder formula::
        page_idx   = tick // ticks_per_page
        in_page    = tick  % ticks_per_page
        flat_idx   = page_idx * cells_per_page + in_page * cells_per_tick + cell
        signal_idx = flat_idx % cells_per_page
    """
    from draftsman.blueprintable import Blueprint  # pylint: disable=import-outside-toplevel
    from draftsman.constants import Direction  # pylint: disable=import-outside-toplevel
    from draftsman.entity import DeciderCombinator, new_entity  # pylint: disable=import-outside-toplevel

    if not tick_data:
        sys.stderr.write("No audio data to encode.\n")
        return ""

    num_base = len(signal_pool)
    num_qual = len(qualities)
    cells_per_tick = len(grouping) if grouping is not None else CHANNELS_PER_TICK
    cells_per_page = ticks_per_page * cells_per_tick

    if num_base == 0:
        raise ValueError("signal_pool must not be empty")

    # The pool must have enough signals to fill one page.
    needed_base = math.ceil(cells_per_page / num_qual)
    if num_base < needed_base:
        raise ValueError(
            f"signal_pool has {num_base} base signals × {num_qual} qualities = "
            f"{num_base * num_qual} unique pairs, but {cells_per_page} are needed "
            f"for a {ticks_per_page}-tick page.  Need at least {needed_base} base signals."
        )

    # 1. Pack
    packed = loudness_to_packed(list(tick_data), grouping=grouping)
    total_ticks = len(packed)

    # 2. Flatten
    flat: list[int] = []
    for tick_vals in packed:
        flat.extend(tick_vals)
    total_cells = len(flat)

    # 3. Page layout — fixed at cells_per_page cells per page
    page_count = math.ceil(total_cells / cells_per_page)

    sys.stderr.write(
        f"Audio: {total_ticks} ticks → {total_cells} cells → "
        f"{page_count} page(s) "
        f"({cells_per_page} cells/page = {needed_base} base × {num_qual} qual, "
        f"{ticks_per_page} ticks/page).\n"
    )

    # 4. Collect non-empty pages — defer grid layout until wiring
    #    quality-first interleaving:
    #      signal_pool[cell_offset // num_qual] × qualities[cell_offset % num_qual]
    own_blueprint = blueprint is None
    if own_blueprint:
        blueprint = Blueprint()
        blueprint.label = f"Audio Memory: {output_name}"
        from ..logical_blueprint import _set_blueprint_icon  # pylint: disable=import-outside-toplevel
        _set_blueprint_icon(blueprint, "constant-combinator")

    # Each entry: (dc_id, page_idx, tick_start, conditions, outputs)
    PageEntry = tuple[str, int, int, list, list]
    non_empty: list[PageEntry] = []

    for page_idx in range(page_count):
        dc_id = f"{id_prefix}ap{page_idx}"

        # Tick range for this page
        page_start_cell = page_idx * cells_per_page
        page_end_cell = page_start_cell + cells_per_page
        tick_start = page_start_cell // cells_per_tick
        tick_end = (min(page_end_cell, total_cells) - 1) // cells_per_tick

        # Condition: clock >= tick_start AND clock <= tick_end
        # Draftsman's Condition.compare_type defaults to "or" and to_dict()
        # omits default values; Factorio then joins the two conditions with
        # OR, making every DC fire on every tick.  Set "and" explicitly on
        # BOTH conditions so the range gates correctly.
        conditions = [
            DeciderCombinator.Condition(
                first_signal={"name": clock_signal},
                comparator=">=",
                constant=tick_start,
                compare_type="and",
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
        for cell_offset in range(cells_per_page):
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

        # Skip entirely silent pages — no DC needed
        if not outputs:
            continue

        non_empty.append((dc_id, page_idx, tick_start, conditions, outputs))

    created_count = len(non_empty)

    # 5. Layout: place non-empty DCs sequentially with no gaps
    MAX_COLS = 12  # align with 12-channel width
    cols = min(MAX_COLS, max(1, created_count)) if created_count > 0 else 1
    rows = (created_count + cols - 1) // cols

    dc_ids: list[str] = []  # ordered by grid position (snake order)

    for seq_idx, (dc_id, page_idx, tick_start, conditions, outputs) in enumerate(non_empty):
        col = seq_idx % cols
        row = seq_idx // cols
        tile_row = row * 2

        dc = new_entity(
            "decider-combinator",
            id=dc_id,
            tile_position=(col + x_offset, tile_row + y_offset),
            direction=Direction.NORTH,
        )
        dc.conditions = conditions
        dc.outputs = outputs
        blueprint.entities.append(dc)
        dc_ids.append(dc_id)

    # 6. Wire pages — green for clock/input bus, red for output bus
    #    Build snake-order list matching the sequential grid placement.
    snake_order: list[str] = []
    for r in range(rows):
        col_iter = range(cols) if r % 2 == 0 else range(cols - 1, -1, -1)
        for c in col_iter:
            seq_idx = r * cols + c
            if seq_idx < len(dc_ids):
                snake_order.append(dc_ids[seq_idx])

    for i in range(1, len(snake_order)):
        blueprint.add_circuit_connection(
            "green", snake_order[i - 1], snake_order[i],
            side_1="input", side_2="input",
        )
        blueprint.add_circuit_connection(
            "red", snake_order[i - 1], snake_order[i],
            side_1="output", side_2="output",
        )

    sys.stderr.write(
        f"Audio memory: {created_count}/{page_count} DCs (skipped {page_count - created_count} "
        f"silent), {total_cells} cells, {total_ticks} ticks.\n"
    )
    if own_blueprint:
        return blueprint.to_string()
    # When embedding into an existing blueprint, return the id of the
    # last placed DC for cross-connection by the caller.
    return snake_order[-1] if snake_order else ""


# ── auto-detect convenience (MIDI → encoder bridge) ───────────────────


def encode_audio_auto(
    path: str,
    **kwargs: object,
) -> str:
    """Auto-detect audio format and encode to an audio-memory blueprint.

    Currently supports ``.mid`` / ``.midi`` files via :func:`midi_to_tick_data`.

    Keyword arguments
    -----------------
    attach_player : bool
        If True (default), build the player decoder into the same blueprint
        above the memory pages, producing a single self-contained blueprint.
    instruments : list[str] | None
        Override instrument detection. One entry per rail.
    ticks_per_beat, boost_melody, velocity_scale,
    attack_ticks, decay_ticks, sustain_level, release_ticks,
    attack_curve, decay_curve, release_curve,
    processed_midi_path, debug_json_path
        Forwarded to :func:`midi_to_tick_data`.
    """
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""

    # Audio file formats (non-MIDI): WAV, FLAC, OGG, MP3, etc.
    _AUDIO_EXTS = {"wav", "flac", "ogg", "aiff", "aif", "au", "caf", "mp3", "mp4", "m4a", "aac", "wma"}

    if ext in _AUDIO_EXTS:
        # AI-driven transcription (optional Basic Pitch) is preferred for
        # non-MIDI audio: it produces real musical notes instead of the dense
        # FFT-noise the STFT path yields for loud tracks.  Falls back to the
        # built-in STFT analysis when Basic Pitch is unavailable or disabled.
        if bool(kwargs.get("use_basic_pitch", True)):
            from .basic_pitch_transcriber import transcribe_audio  # pylint: disable=import-outside-toplevel,relative-beyond-top-level
            midi_path = transcribe_audio(path)
            if midi_path is not None:
                sys.stderr.write(f"Using Basic Pitch transcription: {midi_path}\n")
                return _encode_midi(midi_path, **kwargs)
        return _encode_audio_file(path, **kwargs)

    if ext not in ("mid", "midi"):
        sys.stderr.write(f"Audio auto-encode: unsupported format: {path}\n")
        return ""

    return _encode_midi(path, **kwargs)


def _audio_rails(
    path: str,
    kwargs: dict[str, object],
) -> tuple[list[str], list[list[list[int]]]]:
    """Return ``(instruments, int_data_list)`` for any audio/MIDI input.

    Used by the composed audio path so it can build one memory bank and one
    player rail per instrument.  MIDI and Basic-Pitch transcriptions go
    through :func:`_midi_rails`; other audio falls back to the built-in STFT
    analysis (single piano rail).
    """
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    _AUDIO_EXTS = {"wav", "flac", "ogg", "aiff", "aif", "au", "caf", "mp3", "mp4", "m4a", "aac", "wma"}

    if ext in ("mid", "midi"):
        return _midi_rails(path, kwargs)

    if ext in _AUDIO_EXTS:
        instruments: list[str] = []
        int_data_list: list[list[list[int]]] = []

        if bool(kwargs.get("use_basic_pitch", True)):
            from .basic_pitch_transcriber import transcribe_audio  # pylint: disable=import-outside-toplevel,relative-beyond-top-level
            midi_path = transcribe_audio(path)
            if midi_path is not None:
                instruments, int_data_list = _midi_rails(midi_path, kwargs)

        if not int_data_list:
            # Built-in STFT → single piano rail.
            from .audio_analyzer import (  # pylint: disable=import-outside-toplevel,relative-beyond-top-level
                audio_file_to_loudness, fold_loudness_array,
            )
            full_loudness = audio_file_to_loudness(
                path, activation_threshold=float(kwargs.get("activation_threshold", 0.0)),
            )
            if not full_loudness:
                return [], []
            game_loudness = fold_loudness_array(full_loudness)
            global_max = max((v for tick in game_loudness for v in tick), default=0.0)
            scale = 100.0 / global_max if global_max > 0 else 1.0
            int_data = [
                [max(0, min(100, int(round(v * scale)))) for v in tick]
                for tick in game_loudness
            ]
            instruments = ["piano"]
            int_data_list = [int_data]

        # Audio drum detection: Basic Pitch (and the STFT fallback) only
        # transcribe *pitched* notes, so real kick/snare/hat hits are missing.
        # When the user opts in (--drums), recover them from the raw waveform
        # and append a drum rail — everything downstream (drum grouping,
        # drum-kit decoder) already knows how to handle a "drum" rail.
        if instruments and int_data_list and _audio_drums_enabled(kwargs):
            from .drum_detector import (  # pylint: disable=import-outside-toplevel,relative-beyond-top-level
                detect_drum_rail_from_file,
            )
            drum_rail = detect_drum_rail_from_file(path)
            if drum_rail:
                instruments.append("drum")
                int_data_list.append(_drum_rail_to_int(drum_rail))
                sys.stderr.write(
                    f"[drums] added drum rail ({len(drum_rail)} ticks)\n"
                )

        # Align every rail to the same number of ticks so the memory banks
        # (and the composed clock) line up.
        max_ticks = max((len(td) for td in int_data_list), default=0)
        if max_ticks:
            int_data_list = [
                td + [[0] * len(td[0]) for _ in range(max_ticks - len(td))]
                for td in int_data_list
            ]
        return instruments, int_data_list

    return [], []


def _midi_rails(
    path: str,
    kwargs: dict[str, object],
) -> tuple[list[str], list[list[list[int]]]]:
    """Translate a MIDI file (or Basic Pitch transcription) into rails.

    Returns ``(instruments, int_data_list)`` where *instruments* is one entry
    per rail and ``int_data_list[ri][tick][pitch]`` is the 0-100 loudness grid
    for rail *ri*.  Shared by :func:`_encode_midi` and the composed audio path
    (so the composed path can build one memory bank + player rail per
    instrument).
    """
    from .midi_translator import midi_to_multi_rail_tick_data  # pylint: disable=relative-beyond-top-level,import-outside-toplevel

    # Gather midi_translator kwargs
    midi_kwargs: dict[str, object] = {}
    for key in (
        "ticks_per_beat", "boost_melody", "velocity_scale",
        "attack_ticks", "decay_ticks", "sustain_level", "release_ticks",
        "attack_curve", "decay_curve", "release_curve",
        "processed_midi_path",
        "use_global_shift", "rearticulation_ticks",
    ):
        if key in kwargs:
            midi_kwargs[key] = kwargs[key]

    from .._unicode_io import mido_open  # pylint: disable=import-outside-toplevel,relative-beyond-top-level

    sys.stderr.write(f"Loading MIDI: {path}\n")
    mid = mido_open(path)
    sys.stderr.write(
        f"  {len(mid.tracks)} track(s), "
        f"ticks_per_beat={mid.ticks_per_beat}, "
        f"length={mid.length:.1f}s\n"
    )

    # ── Determine instruments via rail_mode ─────────────────────────
    rail_mode: str = str(kwargs.get("rail_mode", "piano"))
    map_drums = bool(kwargs.get("map_drums", False))

    # Parse rail_mode: "piano", "all", "auto:0.05", or "piano,bass"
    # Backward compat: accept "instruments" kwarg too
    if "instruments" in kwargs:
        inst_val = kwargs["instruments"]
        if isinstance(inst_val, list):
            rail_mode = ",".join(inst_val)
        elif isinstance(inst_val, str):
            rail_mode = inst_val

    instruments: list[str] = []
    threshold: float = 0.05  # default threshold for auto mode
    multi_data: list[list[list[float]]] = []
    int_data_list: list[list[list[int]]] = []

    # Allowed kwargs for midi_to_multi_rail_tick_data
    _multi_kwargs = {k: v for k, v in midi_kwargs.items()
                     if k in ("ticks_per_beat", "boost_melody", "velocity_scale",
                              "attack_ticks", "decay_ticks", "sustain_level",
                              "release_ticks", "attack_curve", "decay_curve",
                              "release_curve", "use_global_shift",
                              "rearticulation_ticks")}

    # ── Cache: reuse the translated tick data when inputs are unchanged ──
    # The key covers the source file identity plus every translation option
    # that affects the output, so any change invalidates the cache.  This
    # skips the expensive MIDI → tick-data translation on re-encodes.
    _mkw_sorted = ",".join(
        f"{k}={_multi_kwargs.get(k)}" for k in sorted(_multi_kwargs)
    )
    _ckey = cache_key(
        "midi_tickdata", _file_identity(path), rail_mode,
        f"map_drums={map_drums}",
        f"nt={kwargs.get('normalize_target', 100.0)}",
        _mkw_sorted,
    )
    _cached = cache_json_get("audio_encode", _ckey)

    if _cached is not None:
        instruments = list(_cached["instruments"])
        int_data_list = _cached["data"]
        sys.stderr.write(
            f"Using {len(instruments)} rail(s) from cache: {', '.join(instruments)}\n"
        )
    else:
        if rail_mode == "piano":
            # Default: single piano rail, ignore everything else
            instruments = ["piano"]
        elif rail_mode == "all":
            # Use all detected instruments
            instruments, multi_data = midi_to_multi_rail_tick_data(
                mid, **_multi_kwargs, map_drums=map_drums,  # type: ignore[arg-type]
            )
        elif rail_mode.startswith("auto"):
            # auto[:threshold] — auto-detect, filter below threshold
            if ":" in rail_mode:
                try:
                    threshold = float(rail_mode.split(":", 1)[1])
                except ValueError:
                    pass
            instruments, multi_data = midi_to_multi_rail_tick_data(
                mid, **_multi_kwargs, map_drums=map_drums,  # type: ignore[arg-type]
            )
            # Filter: drop rails with too few note events vs total
            if len(instruments) > 1:
                # Count note events per rail from multi_data
                total_ticks = max((len(td) for td in multi_data), default=0)
                kept_instruments: list[str] = []
                kept_data: list[list[list[float]]] = []
                for ri, inst in enumerate(instruments):
                    td = multi_data[ri]
                    active_ticks = sum(1 for tick in td if any(v > 0 for v in tick))
                    ratio = active_ticks / max(1, total_ticks)
                    if ratio >= threshold:
                        kept_instruments.append(inst)
                        kept_data.append(td)
                    else:
                        sys.stderr.write(
                            f"Dropping rail '{inst}' ({active_ticks}/{total_ticks} "
                            f"active ticks, {ratio:.1%} < {threshold:.1%})\n"
                        )
                if kept_instruments:
                    instruments = kept_instruments
                    multi_data = kept_data
                # If everything got filtered, keep the most active one
                if not instruments:
                    best = max(range(len(multi_data)), key=lambda i: sum(
                        1 for t in multi_data[i] if any(v > 0 for v in t)
                    ))
                    instruments = [instruments[best]]  # type: ignore[index]
                    instruments = [instruments[0]]
        else:
            # Comma-separated instrument names: "piano,bass,drum"
            instruments = [s.strip() for s in rail_mode.split(",") if s.strip()]
            if not instruments:
                instruments = ["piano"]

        if not instruments:
            sys.stderr.write("No instruments selected.\n")
            return [], []

        sys.stderr.write(
            f"Using {len(instruments)} rail(s): {', '.join(instruments)}\n"
        )

        # ── Generate tick_data per rail ──────────────────────────
        if multi_data:
            # Data already came from multi-rail translator
            for float_data in multi_data:
                normalize_target = float(kwargs.get("normalize_target", 100.0))
                float_data = normalize_tick_data(float_data, target_max=normalize_target)
                int_data = [
                    [max(0, min(100, int(round(v)))) for v in tick]
                    for tick in float_data
                ]
                int_data_list.append(int_data)
        else:
            # Need to generate tick_data for manual instruments
            # For single piano, use the simple translator; for multi, use multi-rail
            if len(instruments) == 1 and instruments[0] == "piano" and not map_drums:
                from .midi_translator import midi_to_tick_data  # pylint: disable=relative-beyond-top-level,import-outside-toplevel
                float_data = midi_to_tick_data(mid, **midi_kwargs)  # type: ignore[arg-type]
                normalize_target = float(kwargs.get("normalize_target", 100.0))
                float_data = normalize_tick_data(float_data, target_max=normalize_target)
                int_data_list.append([
                    [max(0, min(100, int(round(v)))) for v in tick]
                    for tick in float_data
                ])
            else:
                # Use multi-rail translator for manual instruments
                all_inst, all_data = midi_to_multi_rail_tick_data(
                    mid, **_multi_kwargs, map_drums=map_drums,  # type: ignore[arg-type]
                )
                # Pick only the requested instruments
                for inst in instruments:
                    if inst in all_inst:
                        ri = all_inst.index(inst)
                        float_data = all_data[ri]
                    else:
                        # Instrument not found in MIDI, create empty data
                        max_ticks = max((len(td) for td in all_data), default=0)
                        float_data = [[0.0] * 48 for _ in range(max_ticks)] if max_ticks > 0 else []
                    normalize_target = float(kwargs.get("normalize_target", 100.0))
                    float_data = normalize_tick_data(float_data, target_max=normalize_target)
                    int_data_list.append([
                        [max(0, min(100, int(round(v)))) for v in tick]
                        for tick in float_data
                    ])

    # ── Persist the freshly-computed tick data (pre-gain, normalized) ──
    if _cached is None:
        cache_json_put("audio_encode", _ckey, {
            "instruments": instruments,
            "data": int_data_list,
        })

    # ── Per-rail gain: drums are percussive, sit low in the mix and sound
    # more dominating to the ear than pitched notes, so they get a smaller
    # gain to stay level with (not mask) the melody — a typical note sits
    # around 25, so drums are scaled to match that rather than riding at
    # the 100 peak.  Applied AFTER cache retrieval so the cached data stays
    # pre-gain and the gain is never double-applied on a cache hit.
    drum_gain = float(kwargs.get("drum_gain", 0.25))
    if drum_gain != 1.0:
        for ri, inst in enumerate(instruments):
            if "drum" in inst.lower():
                int_data_list[ri] = [
                    [max(0, min(100, int(round(v * drum_gain)))) for v in tick]
                    for tick in int_data_list[ri]
                ]

    return instruments, int_data_list


def _encode_midi(
    path: str,
    **kwargs: object,
) -> str:
    """Encode a MIDI file (or a Basic Pitch transcription) into a blueprint.

    Shared by the ``.mid`` / ``.midi`` input path and the AI-transcription
    path (Basic Pitch produces a MIDI that is then fed through here).
    """
    from .. import SIGNAL_POOL, QUALITIES, CLOCK_SIGNAL  # pylint: disable=relative-beyond-top-level,import-outside-toplevel
    from .player_blueprint import build_multi_rail_decoder  # pylint: disable=relative-beyond-top-level

    instruments, int_data_list = _midi_rails(path, kwargs)
    if not int_data_list or not any(any(any(v for v in tick) for tick in td) for td in int_data_list):
        sys.stderr.write("No notes found in MIDI.\n")
        return ""

    attach_player = bool(kwargs.get("attach_player", True))
    map_drums = bool(kwargs.get("map_drums", False))

    # ── debug JSON dump ──────────────────────────────────────────────
    debug_json_path = kwargs.get("debug_json_path")
    if debug_json_path and isinstance(debug_json_path, str):
        import json  # pylint: disable=import-outside-toplevel
        json_data = [
            [[round(v, 3) for v in tick] for tick in td]
            for td in int_data_list
        ]
        with open(debug_json_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f)
        sys.stderr.write(f"Debug JSON written to: {debug_json_path}\n")

    signal_pool = list(SIGNAL_POOL)
    qualities = list(QUALITIES)

    if not signal_pool:
        sys.stderr.write("Warning: SIGNAL_POOL is empty, audio encoding may fail.\n")

    num_rails = len(instruments)

    # Compact drum rails: a drum track only records each used drum type's
    # loudness (0-100 ceiling) — there is no pitch dimension.  Store just
    # those raw tick→volume cells (one per used drum type) instead of the
    # 12-cell-per-tick pitched layout, so the drum memory stays tiny.
    active_drum_pitches: list[set[int] | None] = []
    for ri, inst in enumerate(instruments):
        if "drum" in inst.lower():
            active_drum_pitches.append({
                p for p in range(SPEAKER_COUNT)
                if any(td[p] > 0 for td in int_data_list[ri])
            })
        else:
            active_drum_pitches.append(None)

    def _drum_grouping(ri: int) -> object | None:
        ap = active_drum_pitches[ri]
        return drum_grouping(ap) if ap is not None else None

    def _rail_ticks_per_page(ri: int) -> int:
        """Per-rail page size — drums use large pages (fewer DCs)."""
        if "drum" in instruments[ri].lower():
            grp = _drum_grouping(ri)
            cpt = len(grp) if grp else 1
            max_page = (len(signal_pool) * len(qualities)) // max(1, cpt)
            return max(TICKS_PER_PAGE, min(DRUM_TICKS_PER_PAGE, max_page))
        return TICKS_PER_PAGE

    if not attach_player:
        # Memory-only: encode each rail, concatenate
        parts: list[str] = []
        for ri in range(num_rails):
            mem = encode_audio_memory(
                int_data_list[ri],
                output_name=f"{path}_r{ri}",
                signal_pool=signal_pool,
                qualities=qualities,
                clock_signal=CLOCK_SIGNAL,
                grouping=_drum_grouping(ri),
                ticks_per_page=_rail_ticks_per_page(ri),
            )
            if mem:
                parts.append(mem)
        return "\n".join(parts)

    # ── Combined blueprint: player + memory ─────────────────────────
    import io as _io
    import contextlib as _cl
    from draftsman.blueprintable import Blueprint
    from draftsman.entity import new_entity
    from .player_blueprint import (
        RAIL_WIDTH, MOD_Y, SUB_TICK_SIG,
        INSTRUMENT_MIDI_BASES, _build_rail, _RailEndpoints,
    )

    with _cl.redirect_stdout(_io.StringIO()):
        combined = Blueprint()
        combined.label = f"Audio: {path}"
        from ..logical_blueprint import _set_blueprint_icon  # pylint: disable=import-outside-toplevel
        _set_blueprint_icon(combined, "constant-combinator")

        # Build rails one at a time: player + memory per rail, side by side.
        # Memory sits directly above its player at the same X offset.
        endpoints: list[_RailEndpoints] = []
        mem_last_ids: list[str] = []

        for ri in range(num_rails):
            rail_x = ri * RAIL_WIDTH
            inst = instruments[ri]
            midi_base = INSTRUMENT_MIDI_BASES.get(
                inst.lower().replace("programmable-speaker-instrument-", ""), 53,
            )

            # Build player rail
            ep = _build_rail(
                combined, ri, rail_x,
                inst, signal_pool, qualities,
                debug_lamps=False,
                midi_base=midi_base,
                map_drums=map_drums,
                ticks_per_page=_rail_ticks_per_page(ri),
            )
            endpoints.append(ep)

            # Find max Y within this rail's X range to place memory above
            rail_max_y = 0.0
            for e in combined.entities:
                try:
                    pos = e.tile_position
                    y = float(pos[1]) if hasattr(pos, '__getitem__') else float(pos.y) if hasattr(pos, 'y') else 0.0
                    x = float(pos[0]) if hasattr(pos, '__getitem__') else float(pos.x) if hasattr(pos, 'x') else 999.0
                    if rail_x <= x < rail_x + RAIL_WIDTH:
                        rail_max_y = max(rail_max_y, y)
                except (TypeError, ValueError, IndexError):
                    pass

            MEM_GAP = 4
            memory_y = int(rail_max_y) + MEM_GAP

            # Build memory pages above this rail's player (same X offset)
            last_id = encode_audio_memory(
                int_data_list[ri],
                output_name=f"{path}_r{ri}",
                signal_pool=signal_pool,
                qualities=qualities,
                clock_signal=CLOCK_SIGNAL,
                blueprint=combined,
                y_offset=memory_y,
                x_offset=rail_x,
                id_prefix=f"r{ri}_",
                grouping=_drum_grouping(ri),
                ticks_per_page=_rail_ticks_per_page(ri),
            )
            if isinstance(last_id, str) and last_id:
                mem_last_ids.append(last_id)
                # Wire memory → this rail's player (same X, short vertical hop)
                # Each rail is independent — no cross-rail page data sharing.
                import warnings as _w2
                with _w2.catch_warnings():
                    _w2.simplefilter("ignore")
                    combined.add_circuit_connection(
                        "green", last_id, ep.port_id,
                        side_1="input", side_2="input",
                    )
                    combined.add_circuit_connection(
                        "red", last_id, f"r{ri}_ch11_sel",
                        side_1="output", side_2="input",
                    )

        # ── Per-rail modulo ACs ────────────────────────────────────
        # Each rail's page size may differ (drums use large pages), so every
        # rail gets its own mod at its own port column feeding only its own
        # match DCs (its own sub-tick red bus).
        import warnings as _w3
        with _w3.catch_warnings():
            _w3.simplefilter("ignore")
            for ri in range(num_rails):
                mod_x = ri * RAIL_WIDTH + RAIL_WIDTH - 1
                mod_id = f"mod_{ri}"
                ac_mod = new_entity("arithmetic-combinator", id=mod_id,
                                    tile_position=(mod_x, MOD_Y))
                ac_mod.set_arithmetic_condition(
                    first_operand=CLOCK_SIGNAL, operation="%",
                    second_operand=_rail_ticks_per_page(ri),
                    output_signal=SUB_TICK_SIG,
                )
                combined.entities.append(ac_mod)
                combined.add_circuit_connection(
                    "red", mod_id, endpoints[ri].first_match_id,
                    side_1="output", side_2="input",
                )

            # Clock (green): chain ports across rails; each port → its own mod
            for ri in range(num_rails - 1):
                combined.add_circuit_connection(
                    "green", endpoints[ri].port_id, endpoints[ri + 1].port_id,
                    side_1="input", side_2="input",
                )
            for ri in range(num_rails):
                combined.add_circuit_connection(
                    "green", endpoints[ri].port_id, f"mod_{ri}",
                    side_1="input", side_2="input",
                )

    return combined.to_string()


# ── audio-file encoding (non-MIDI: WAV/FLAC/OGG/MP3 → blueprint) ─────


def _encode_audio_file(
    path: str,
    **kwargs: object,
) -> str:
    """Encode an audio file (WAV/FLAC/OGG/MP3) into an audio-memory blueprint.

    Pipeline: read audio → STFT → full-spectrum loudness → (MIDI export) →
    octave fold → encode memory → blueprint.

    Keyword arguments
    -----------------
    output_midi : str | None
        If set, export a MIDI file to this path before octave folding.
    attach_player : bool
        If True (default), build the player decoder into the same blueprint.
    activation_threshold : float
        STFT activation threshold (default 0.0).
    midi_activation_threshold : float
        MIDI extraction activation threshold (default 0.05).
    condense_midi : bool
        Condense contiguous MIDI notes (default True).
    max_polyphony : int
        Max simultaneous MIDI notes (0 = unlimited).
    normalize_target : float
        Target max loudness for encoding (default 100.0).
    """
    from .. import SIGNAL_POOL, QUALITIES, CLOCK_SIGNAL  # pylint: disable=import-outside-toplevel
    from .audio_analyzer import (
        audio_file_to_loudness, fold_loudness_array,
    )  # pylint: disable=import-outside-toplevel
    from .loudness_to_midi import loudness_to_midi_file  # pylint: disable=import-outside-toplevel

    attach_player = bool(kwargs.get("attach_player", True))
    output_midi = kwargs.get("output_midi")
    activation_threshold = float(kwargs.get("activation_threshold", 0.0))
    midi_activation_threshold = float(kwargs.get("midi_activation_threshold", 0.05))
    condense_midi = bool(kwargs.get("condense_midi", True))
    max_polyphony = int(kwargs.get("max_polyphony", 0))
    normalize_target = float(kwargs.get("normalize_target", 100.0))

    # ── Cache: reuse the STFT → normalized tick-data analysis ──
    # The key covers the source file identity plus every analysis option
    # that affects the result, so any change invalidates the cache.  MIDI
    # export needs the pre-fold full spectrum, so it bypasses the cache.
    _ckey = cache_key(
        "audio_file_tickdata", _file_identity(path),
        f"at={activation_threshold}", f"nt={normalize_target}",
    )
    int_data: list[list[int]] | None = None
    if not (output_midi and isinstance(output_midi, str)):
        _cached = cache_json_get("audio_encode", _ckey)
        if isinstance(_cached, list):
            int_data = _cached

    if int_data is None:
        # 1. Read audio → full-spectrum loudness (128 MIDI notes)
        sys.stderr.write(f"Loading audio: {path}\n")
        full_loudness = audio_file_to_loudness(
            path, activation_threshold=activation_threshold,
        )

        if not full_loudness:
            sys.stderr.write("No audio data extracted.\n")
            return ""

        total_ticks = len(full_loudness)
        sys.stderr.write(
            f"  {total_ticks} ticks ({total_ticks / 60:.1f}s) extracted.\n"
        )

        # 2. Export MIDI (before octave folding — full MIDI range)
        if output_midi and isinstance(output_midi, str):
            sys.stderr.write(f"Exporting MIDI to: {output_midi}\n")
            loudness_to_midi_file(
                full_loudness, output_midi,
                activation_threshold=midi_activation_threshold,
                condense=condense_midi,
                max_polyphony=max_polyphony if max_polyphony > 0 else 0,
            )

        # 3. Fold to game range (F3–E7, 48 pitches)
        game_loudness = fold_loudness_array(full_loudness)

        # 4. Scale to 0–100 int for the encoder
        # Normalize: find global max, scale to normalize_target
        global_max = 0.0
        for tick in game_loudness:
            for v in tick:
                if v > global_max:
                    global_max = v
        scale = normalize_target / global_max if global_max > 0 else 1.0

        int_data = []
        for tick in game_loudness:
            int_tick = [max(0, min(100, int(round(v * scale)))) for v in tick]
            int_data.append(int_tick)

        if not (output_midi and isinstance(output_midi, str)):
            cache_json_put("audio_encode", _ckey, int_data)
    else:
        sys.stderr.write(
            f"Loaded {len(int_data)} ticks of audio from cache: {path}\n"
        )

    signal_pool = list(SIGNAL_POOL)
    qualities = list(QUALITIES)

    if not signal_pool:
        sys.stderr.write("Warning: SIGNAL_POOL is empty, audio encoding may fail.\n")

    # 5. Encode to blueprint
    if not attach_player:
        return encode_audio_memory(
            int_data,
            output_name=path,
            signal_pool=signal_pool,
            qualities=qualities,
            clock_signal=CLOCK_SIGNAL,
        )

    # Combined blueprint: player + memory
    from .player_blueprint import build_audio_decoder  # pylint: disable=import-outside-toplevel
    from draftsman.blueprintable import Blueprint  # pylint: disable=import-outside-toplevel

    player_str = build_audio_decoder(
        name=f"Audio Decoder: {path}",
        instrument="piano",
        clock_signal=CLOCK_SIGNAL,
    )

    mem_str = encode_audio_memory(
        int_data,
        output_name=path,
        signal_pool=signal_pool,
        qualities=qualities,
        clock_signal=CLOCK_SIGNAL,
    )

    if not mem_str:
        return player_str

    return player_str + "\n" + mem_str


# ── logical-blueprint encoder ─────────────────────────────────────────


def encode_audio_to_logical(
    tick_data: Sequence[Sequence[int]],
    output_name: str,
    signal_pool: list[str],
    qualities: list[str],
    clock_signal: str = "signal-clock",
    id_prefix: str = "",
    grouping: Sequence[Sequence[int | None]] | None = None,
    ticks_per_page: int = TICKS_PER_PAGE,
    *,
    connectors: bool = False,
    connector_label: str | None = None,
    fragment_index: int | None = None,
) -> "LogicalBlueprint":  # noqa: F821
    """Encode tick→loudness data into a :class:`LogicalBlueprint`.

    This is the logical-format variant of :func:`encode_audio_memory`.
    Instead of building a draftsman ``Blueprint`` with hard-coded
    positions and pairwise wires, it returns a ``LogicalBlueprint``
    where each DC page is a ``[[entity]]`` and the red / green buses
    are ``[[network]]`` entries.  Positions are left unset — a separate
    layout pass assigns them later.

    Parameters
    ----------
    tick_data : sequence of sequence of int
        ``tick_data[tick][speaker_idx] = loudness`` (0–100).
    output_name : str
        Label for the blueprint.
    signal_pool : list[str]
        Base signal names for the integer→signal encoding.
    qualities : list[str]
        Quality tiers.
    clock_signal : str
        Name of the clock signal.
    id_prefix : str
        Prefix for entity ids (e.g. ``"r0_"`` for rail 0).
    grouping : sequence of sequence of int|None, optional
        Per-tick cell packing (see :func:`loudness_to_packed`).  A compact
        drum rail passes only the drum types used, shrinking the memory.

    Returns
    -------
    LogicalBlueprint
        The logical blueprint with entities and networks, no positions.
    """
    from ..logical_blueprint import Endpoint, LogicalBlueprint, LogicalEntity, Network  # pylint: disable=relative-beyond-top-level,import-outside-toplevel

    if not tick_data:
        return LogicalBlueprint(label=f"Audio Memory: {output_name}")

    num_base = len(signal_pool)
    num_qual = len(qualities)
    cells_per_tick = len(grouping) if grouping is not None else CHANNELS_PER_TICK
    cells_per_page = ticks_per_page * cells_per_tick

    if num_base == 0:
        raise ValueError("signal_pool must not be empty")

    needed_base = math.ceil(cells_per_page / num_qual)
    if num_base < needed_base:
        raise ValueError(
            f"signal_pool has {num_base} base signals × {num_qual} qualities = "
            f"{num_base * num_qual} unique pairs, but {cells_per_page} are needed "
            f"for a {ticks_per_page}-tick page.  Need at least {needed_base} base signals."
        )

    # 1. Pack
    packed = loudness_to_packed(list(tick_data), grouping=grouping)
    total_ticks = len(packed)

    # 2. Flatten
    flat: list[int] = []
    for tick_vals in packed:
        flat.extend(tick_vals)
    total_cells = len(flat)

    # 3. Page layout
    page_count = math.ceil(total_cells / cells_per_page)

    sys.stderr.write(
        f"Audio (logical): {total_ticks} ticks → {total_cells} cells → "
        f"{page_count} page(s) "
        f"({cells_per_page} cells/page = {needed_base} base × {num_qual} qual, "
        f"{ticks_per_page} ticks/page).\n"
    )

    lb = LogicalBlueprint(label=f"Audio Memory: {output_name}")
    lb.icon = "constant-combinator"  # constant-combinator icon in the book

    dc_ids: list[str] = []
    created_count = 0

    for page_idx in range(page_count):
        dc_id = f"{id_prefix}ap{page_idx}"

        page_start_cell = page_idx * cells_per_page
        page_end_cell = page_start_cell + cells_per_page
        tick_start = page_start_cell // cells_per_tick
        tick_end = (min(page_end_cell, total_cells) - 1) // cells_per_tick

        # Build conditions and outputs
        # Both conditions carry compare_type="and": Draftsman's Condition
        # default is "or" and missing compare_type is omitted, so Factorio
        # would OR the range and fire every DC on every tick (noise).
        conditions = [
            {"first": clock_signal, "op": ">=", "constant": tick_start,
             "compare_type": "and"},
            {"first": clock_signal, "op": "<=", "constant": tick_end,
             "compare_type": "and"},
        ]

        outputs: list[dict] = []
        for cell_offset in range(cells_per_page):
            flat_idx = page_start_cell + cell_offset
            if flat_idx >= total_cells:
                break

            value = flat[flat_idx]
            if value == 0:
                continue

            signal_idx = cell_offset // num_qual
            quality_idx = cell_offset % num_qual
            signal_name = signal_pool[signal_idx]
            quality = qualities[quality_idx]

            outputs.append({
                "signal": f"{signal_name}@{quality}",
                "copy_count": False,
                "constant": value,
            })

        if not outputs:
            continue  # skip silent pages

        entity = LogicalEntity(
            entity_id=dc_id,
            type="decider-combinator",
            properties={
                "conditions": conditions,
                "outputs": outputs,
            },
        )
        lb.add_entity(entity)
        dc_ids.append(dc_id)
        created_count += 1

    # Wire all inputs together (green) and all outputs together (red)
    if len(dc_ids) >= 2:
        first_id = dc_ids[0]
        for dc_id in dc_ids[1:]:
            lb.connect("green", Endpoint(first_id, "input"), Endpoint(dc_id, "input"))
        first_out = dc_ids[0]
        for dc_id in dc_ids[1:]:
            lb.connect("red", Endpoint(first_out, "output"), Endpoint(dc_id, "output"))
    elif len(dc_ids) == 1:
        # A single page has no connect() calls to form a bus — create the
        # red/green networks explicitly so the clock/data ports exist for the
        # composer to merge (a tiny clip or a single-drum rail can be just
        # one page).
        lb.add_network(Network(
            network_id=f"{id_prefix}red", color="red",
            endpoints={Endpoint(dc_ids[0], "output")},
        ))
        lb.add_network(Network(
            network_id=f"{id_prefix}green", color="green",
            endpoints={Endpoint(dc_ids[0], "input")},
        ))

    _layout_and_prewire_audio_bank(
        lb, dc_ids,
        connectors=connectors,
        connector_label=connector_label,
        fragment_index=fragment_index,
    )

    # Declare ports when the shared buses exist.
    if dc_ids:
        first_input_ep = Endpoint(dc_ids[0], "input")
        for net in lb.networks:
            if net.color == "green" and first_input_ep in net.endpoints:
                lb.set_input_port("clock", net.network_id)
                break
        first_output_ep = Endpoint(dc_ids[0], "output")
        for net in lb.networks:
            if net.color == "red" and first_output_ep in net.endpoints:
                lb.set_output_port("data", net.network_id)
                break

    sys.stderr.write(
        f"Audio memory (logical): {created_count}/{page_count} DCs "
        f"(skipped {page_count - created_count} silent), "
        f"{total_cells} cells, {total_ticks} ticks.\n"
    )

    return lb


def _rotate_player_90_ccw(d: dict) -> None:
    """Rotate a player-piece blueprint dict 90 degrees counter-clockwise.

    The player is laid out as a vertical strip (speakers on top, connector
    at the bottom-right); the user places it next to the (270° CCW rotated)
    memory banks after a 90° CCW rotation, so we bake that rotation in:
    every entity's position is rotated ``(x, y) -> (-y, x)`` and every facing
    combinator's direction is rotated one step counter-clockwise (north ->
    west, east -> north, ...).  The connector constant combinators are
    re-oriented to keep facing NORTH, so they align with the memory /
    display connector CCs.
    """
    for entity in d.get("blueprint", {}).get("entities", []):
        pos = entity.get("position") or {}
        x, y = pos.get("x", 0.0), pos.get("y", 0.0)
        pos["x"], pos["y"] = -y, x
        if entity.get("name") == "constant-combinator":
            entity["direction"] = 0  # keep facing north (aligns with display CCs)
        elif entity.get("name") in (
            "decider-combinator",
            "arithmetic-combinator",
            "selector-combinator",
        ):
            # Rotate the facing 90 degrees counter-clockwise.  Draftsman
            # omits the default (north) direction from the dict, so always
            # write it.
            entity["direction"] = (int(entity.get("direction", 0)) + 12) % 16


def _player_string(bp: Any) -> str:
    """Serialise a split-output player piece (with the 90° CCW rotation)."""
    from draftsman.utils import JSON_to_string  # pylint: disable=import-outside-toplevel

    d = bp.to_dict()
    _rotate_player_90_ccw(d)
    return JSON_to_string(d)


def encode_audio_split(
    int_data_list: Sequence[Sequence[Sequence[int]]],
    instruments: Sequence[str],
    output_name: str,
    signal_pool: list[str],
    qualities: list[str],
    clock_signal: str = "signal-clock",
    map_drums: bool = False,
    active_drum_pitches: Sequence[set[int] | None] | None = None,
    rail_ticks_per_page: Sequence[int] | None = None,
    rail_groupings: Sequence[Sequence[Sequence[int | None]] | None] | None = None,
) -> dict:
    """Encode audio rails into independently-wireable pieces (split output).

    Returns one **player** blueprint (with a bottom-edge connector CC per
    rail) plus one **memory** piece per rail (with connector CCs on both
    ends).  Every connector joins the green clock (time) bus and the red
    data bus; the user wires matching connectors in game to feed the clock
    and page data from memory to the player.

    Returns ``{"player": str, "pieces": [(label, str), ...], "num_rails": int}``.
    """
    from ..logical_blueprint import to_draftsman  # pylint: disable=import-outside-toplevel
    from .player_blueprint import (  # pylint: disable=import-outside-toplevel
        _rail_marker_signal,
        build_multi_rail_decoder_logical,
    )

    num_rails = len(instruments)
    if num_rails == 0:
        raise ValueError("instruments must not be empty")

    player_lb = build_multi_rail_decoder_logical(
        name=f"{output_name} Player",
        instruments=list(instruments),
        clock_signal=clock_signal,
        map_drums=map_drums,
        active_drum_pitches=list(active_drum_pitches) if active_drum_pitches else None,
        ticks_per_page=list(rail_ticks_per_page) if rail_ticks_per_page else None,
        connectors=True,
    )
    player_str = _player_string(to_draftsman(player_lb))

    pieces: list[tuple[str, str]] = []
    for ri, int_data in enumerate(int_data_list):
        grouping = rail_groupings[ri] if rail_groupings else None
        mem = encode_audio_to_logical(
            int_data, f"{output_name} r{ri}",
            signal_pool=signal_pool,
            qualities=qualities,
            clock_signal=clock_signal,
            id_prefix=f"r{ri}_",
            grouping=grouping,
            ticks_per_page=(
                rail_ticks_per_page[ri] if rail_ticks_per_page else TICKS_PER_PAGE
            ),
            connectors=True,
            connector_label=_rail_marker_signal(ri),
            fragment_index=ri,
        )
        mem_str = to_draftsman(mem).to_string()
        pieces.append((f"memory_r{ri}", mem_str))

    total_ticks = max(
        (len(td) - 1 for td in int_data_list if td), default=0,
    )
    return {
        "player": player_str,
        "pieces": pieces,
        "num_rails": num_rails,
        "total_ticks": total_ticks,
    }
