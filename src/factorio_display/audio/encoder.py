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

from .pitch_mapping import SPEAKER_COUNT  # pylint: disable=relative-beyond-top-level


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
) -> list[list[int]]:
    """Convert ``tick_data[tick][speaker_idx]`` → ``tick_data[tick][channel]``.

    Each inner list moves from 48 loudness values to 12 packed integers.

    Packing groups the SAME semitone across 4 octaves into one integer,
    matching the player's unpacker which routes all 4 sub-values to
    speakers of the same semitone in different octaves::

        Channel ch → semitone ch:  pitch[ch+0*12], pitch[ch+1*12],
                                    pitch[ch+2*12], pitch[ch+3*12]
    """
    semitone_count = 12
    result: list[list[int]] = []
    for tick_loudness in tick_data:
        if len(tick_loudness) != SPEAKER_COUNT:
            raise ValueError(
                f"Expected {SPEAKER_COUNT} loudness values per tick, "
                f"got {len(tick_loudness)}"
            )
        packed: list[int] = []
        for semitone in range(semitone_count):
            packed.append(
                pack_four(
                    tick_loudness[semitone + 0 * semitone_count],  # octave 3
                    tick_loudness[semitone + 1 * semitone_count],  # octave 4
                    tick_loudness[semitone + 2 * semitone_count],  # octave 5
                    tick_loudness[semitone + 3 * semitone_count],  # octave 6
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
    from draftsman.blueprintable import Blueprint  # pylint: disable=import-outside-toplevel
    from draftsman.constants import Direction  # pylint: disable=import-outside-toplevel
    from draftsman.entity import DeciderCombinator, new_entity  # pylint: disable=import-outside-toplevel

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

    # 4. Collect non-empty pages — defer grid layout until wiring
    #    quality-first interleaving:
    #      signal_pool[cell_offset // num_qual] × qualities[cell_offset % num_qual]
    own_blueprint = blueprint is None
    if own_blueprint:
        blueprint = Blueprint()
        blueprint.label = f"Audio Memory: {output_name}"

    # Each entry: (dc_id, page_idx, tick_start, conditions, outputs)
    PageEntry = tuple[str, int, int, list, list]
    non_empty: list[PageEntry] = []

    for page_idx in range(page_count):
        dc_id = f"{id_prefix}ap{page_idx}"

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
    if ext not in ("mid", "midi"):
        sys.stderr.write(f"Audio auto-encode: unsupported format: {path}\n")
        return ""

    from .. import SIGNAL_POOL, QUALITIES, CLOCK_SIGNAL  # pylint: disable=relative-beyond-top-level,import-outside-toplevel
    from .midi_translator import midi_to_multi_rail_tick_data  # pylint: disable=relative-beyond-top-level,import-outside-toplevel
    from .player_blueprint import build_multi_rail_decoder  # pylint: disable=relative-beyond-top-level

    # Gather midi_translator kwargs
    midi_kwargs: dict[str, object] = {}
    for key in (
        "ticks_per_beat", "boost_melody", "velocity_scale",
        "attack_ticks", "decay_ticks", "sustain_level", "release_ticks",
        "attack_curve", "decay_curve", "release_curve",
        "processed_midi_path",
        "use_global_shift",
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

    # ── Determine instruments via rail_mode ─────────────────────────
    rail_mode: str = str(kwargs.get("rail_mode", "piano"))
    attach_player = bool(kwargs.get("attach_player", True))
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

    # Allowed kwargs for midi_to_multi_rail_tick_data
    _multi_kwargs = {k: v for k, v in midi_kwargs.items()
                     if k in ("ticks_per_beat", "boost_melody", "velocity_scale",
                              "attack_ticks", "decay_ticks", "sustain_level",
                              "release_ticks", "attack_curve", "decay_curve",
                              "release_curve", "use_global_shift")}

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
                instruments = [instruments[best]]  # type: ignore[index] — will be overridden below
                # Need to re-fetch just that one
                instruments = [instruments[0]]
    else:
        # Comma-separated instrument names: "piano,bass,drum"
        instruments = [s.strip() for s in rail_mode.split(",") if s.strip()]
        if not instruments:
            instruments = ["piano"]

    if not instruments:
        sys.stderr.write("No instruments selected.\n")
        return ""

    sys.stderr.write(
        f"Using {len(instruments)} rail(s): {', '.join(instruments)}\n"
    )

    # ── Generate tick_data per rail ──────────────────────────────────
    int_data_list: list[list[list[int]]] = []

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

    if not int_data_list or not any(any(any(v for v in tick) for tick in td) for td in int_data_list):
        sys.stderr.write("No notes found in MIDI.\n")
        return ""

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
        RAIL_WIDTH, MOD_Y, TICKS_PER_PAGE, SUB_TICK_SIG,
        INSTRUMENT_MIDI_BASES, _build_rail, _RailEndpoints,
    )

    with _cl.redirect_stdout(_io.StringIO()):
        combined = Blueprint()
        combined.label = f"Audio: {path}"

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

        # ── Shared modulo AC at the last rail's port column ──────
        # Same X as the last rail's page_port, different Y — no overlap.
        mod_x = (num_rails - 1) * RAIL_WIDTH + RAIL_WIDTH - 1
        ac_mod = new_entity("arithmetic-combinator", id="mod",
                            tile_position=(mod_x, MOD_Y))
        ac_mod.set_arithmetic_condition(
            first_operand=CLOCK_SIGNAL, operation="%",
            second_operand=TICKS_PER_PAGE, output_signal=SUB_TICK_SIG,
        )
        combined.entities.append(ac_mod)

        # ── Cross-rail wiring: sub_tick + clock only ─────────────
        # Sub-tick (red): shared mod → all rails' match DCs
        import warnings as _w3
        last_ep = endpoints[-1]
        with _w3.catch_warnings():
            _w3.simplefilter("ignore")
            combined.add_circuit_connection(
                "red", "mod", last_ep.first_match_id,
                side_1="output", side_2="input",
            )
            combined.add_circuit_connection(
                "red", "mod", last_ep.first_match0_id,
                side_1="output", side_2="input",
            )
            for ri in range(num_rails - 1, 0, -1):
                prev = endpoints[ri - 1]
                combined.add_circuit_connection(
                    "red", f"r{ri}_ch0_match", prev.first_match_id,
                    side_1="input", side_2="input",
                )
                combined.add_circuit_connection(
                    "red", f"r{ri}_ch0_match0", prev.first_match0_id,
                    side_1="input", side_2="input",
                )

            # Clock (green): chain ports across rails → mod
            for ri in range(num_rails - 1):
                combined.add_circuit_connection(
                    "green", endpoints[ri].port_id, endpoints[ri + 1].port_id,
                    side_1="input", side_2="input",
                )
            combined.add_circuit_connection(
                "green", endpoints[-1].port_id, "mod",
                side_1="input", side_2="input",
            )

    return combined.to_string()
