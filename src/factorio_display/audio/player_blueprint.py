"""Audio player blueprint builder — generates a Factorio
programmable-speaker matrix blueprint for 48-note polyphonic playback.

Decoder pipeline (top → bottom, y descending)
----------------------------------------------
  y=22   Modulo AC: sub_tick = clock % 60  (AC, 1×2)
  y=22   Lookup CCs (cols 0..11): all sub-ticks   (CC, 1×1)
  y=20   Match DCs: each(green) == sub_tick(red) → signal=1  (DC, 1×2)
  y=16   Page port + Selector ACs: each(red)*each(green) → bell  (AC, 1×2)
  y=14   Unpacker: l1 = bell >> 21  (AC, 1×2)
  y=12   Unpacker: s2 = bell >> 14  (AC, 1×2)
  y=10   Unpacker: l2 = s2 & 127    (AC, 1×2)
  y=8    Unpacker: s3 = bell >> 7   (AC, 1×2)
  y=6    Unpacker: l3 = s3 & 127    (AC, 1×2)
  y=4    Unpacker: l4 = bell & 127  (AC, 1×2)
  y=0    Speakers: 48 programmable speakers (4 rows, y=0..3)

All entities are adjacent vertically — no wasted tile rows.
Entity sizes: CC=1×1, speaker=1×1, DC=1×2, AC=1×2.

Multi-rail layout
-----------------
Each rail occupies a 13-column block (cols 0..12 for rail 0).
Rails are placed side by side: rail R uses cols (R*13) .. (R*13+12).
One shared modulo AC sits at the rightmost column of the last rail.
Red (sub_tick + page data) and green (clock) buses chain across all rails.

Signal conventions
------------------
- RED wire   = page data bus + sub_tick distribution
- GREEN wire = CC lookup outputs, bell bus, translator outputs
"""

from __future__ import annotations

from typing import Callable

from draftsman.blueprintable import Blueprint
from draftsman.entity import new_entity

from .pitch_mapping import (
    DRUM_KIT_NOTES,
    drum_grouping,
    iter_speaker_signals,
    pitch_index_to_signal,
    SPEAKER_COUNT,
)
from .midi_translator import speaker_count_for  # pylint: disable=relative-beyond-top-level

INSTRUMENT_MAP: dict[str, str] = {
    "piano": "piano",
    "bass": "bass",
    "lead": "lead",
    "saw": "saw",
    "square": "square",
    "celesta": "celesta",
    "vibraphone": "vibraphone",
    "plucked": "plucked",
    "steel-drum": "steel-drum",
    "drum": "drum-kit",
}

# MIDI base per instrument — the F-aligned start of each 4-octave speaker window.
# Each rail's 48 speakers cover midi_base .. midi_base+47.  The melodic
# instruments spread across octaves (synths low, vibraphone high) so separate
# tracks together cover far more of the song than piano alone.
INSTRUMENT_MIDI_BASES: dict[str, int] = {
    "piano": 53,       # F3-E7  (53-100), matches instrument range exactly
    "bass": 41,        # F2-E6  (41-88),  covers bass range F2-E5 (41-76)
    "lead": 41,        # F2-E6  (41-88),  covers lead range F2-E5 (41-76)
    "saw": 41,         # F2-E6  (41-88),  covers saw range F2-E5 (41-76)
    "square": 41,      # F2-E6  (41-88),  covers square range F2-E5 (41-76)
    "steel-drum": 53,  # F3-E7  (53-100), covers steel-drum range F3-E6 (53-88)
    "celesta": 77,     # F5-E9  (77-124), matches celesta range F5-E8 (77-112)
    "vibraphone": 77,  # F5-E9  (77-124), covers vibraphone range F5-E8 (77-112)
    "plucked": 65,     # F4-E7  (65-112), matches plucked range exactly
    "drum": 53,        # F3-E7  (53-100), covers drum range F3-E6 (53-88)
}

_MIDI_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

RAIL_WIDTH = 13  # 12 channel columns + 1 page_port column


def _extend_instrument_in_place(
    inst_data: Any,
    entity_name: str,
    name: str,
    missing_notes: list[str],
) -> None:
    """Append *missing_notes* to the existing *name* instrument entry.

    Unlike :func:`draftsman.data.instruments.add_instrument` — which appends
    a *brand-new* instrument entry at a fresh index (12+), breaking Factorio's
    fixed 0-11 instrument IDs — this extends the existing entry's note list
    and updates Draftsman's lookup tables in place, so the instrument keeps
    its original index (piano=3, bass=4, celesta=8, plucked=10, drum-kit=2).
    """
    raw = inst_data.raw
    entries = raw.get(entity_name, [])
    idx = None
    for i, e in enumerate(entries):
        if isinstance(e, dict) and e.get("name") == name:
            idx = i
            break
    if idx is None:
        return

    notes = entries[idx].setdefault("notes", [])
    existing_names = {
        n["name"] for n in notes if isinstance(n, dict) and "name" in n
    }
    new_notes = [n for n in missing_notes if n not in existing_names]
    if not new_notes:
        return

    notes.extend({"name": n} for n in new_notes)

    # Draftsman's note indices are 0..len(notes)-1 within the instrument.
    start = len(notes) - len(new_notes)

    io = dict(inst_data.index_of.get(entity_name, {}).get(name, {"self": idx}))
    io["self"] = idx
    for k, note in enumerate(new_notes, start=start):
        io[note] = k
    inst_data.index_of.setdefault(entity_name, {})[name] = io

    no = dict(inst_data.name_of.get(entity_name, {}).get(idx, {"self": name}))
    no["self"] = name
    for k, note in enumerate(new_notes, start=start):
        no[k] = note
    inst_data.name_of.setdefault(entity_name, {})[idx] = no


def _patch_instrument_notes() -> None:
    """Extend Draftsman's instrument note lists to cover the full 48-note
    speaker window for each instrument we use — *in place*, preserving each
    instrument's original Factorio index (0-11).

    Draftsman validates note names against each instrument's prototype note
    list and silently rejects (sets to ``None``) any name not found.
    Factorio itself allows any pitch on any instrument, so extending the note
    list is safe — it just teaches Draftsman about the notes we need.

    .. note::
       Uses :func:`_extend_instrument_in_place` (not ``add_instrument``).
       ``add_instrument`` appends a duplicate instrument at index 12+, which
       Factorio does not recognise — speakers pointing at it silently fall
       back to the default "alarm" sound.
    """
    import draftsman.data.instruments as _inst_data

    _INST_NAMES = {"piano", "bass", "lead", "saw", "square",
                    "celesta", "vibraphone", "plucked", "steel-drum", "drum-kit"}

    raw = _inst_data.raw
    ps_list = raw.get("programmable-speaker")
    if not isinstance(ps_list, list):
        return

    for inst_entry in ps_list:
        name = inst_entry.get("name", "")
        if name not in _INST_NAMES:
            continue

        existing: set[str] = {
            n["name"] for n in inst_entry.get("notes", [])
            if isinstance(n, dict) and "name" in n
        }

        if name == "drum-kit":
            # Drum-rail speakers use drum-sound note names, but the
            # map_drums=False fallback (build_audio_decoder(instrument="drum"))
            # uses pitch notes — teach Draftsman both.
            midi_base = INSTRUMENT_MIDI_BASES.get(name, 53)
            all_needed = list(DRUM_KIT_NOTES) + [
                _pitch_index_to_factorio_note(pid, midi_base=midi_base)
                for pid in range(SPEAKER_COUNT)
            ]
        else:
            midi_base = INSTRUMENT_MIDI_BASES.get(name, 53)
            all_needed = [
                _pitch_index_to_factorio_note(pid, midi_base=midi_base)
                for pid in range(SPEAKER_COUNT)
            ]

        missing = [n for n in all_needed if n not in existing]
        if not missing:
            continue  # already complete

        _extend_instrument_in_place(
            _inst_data, "programmable-speaker", name, missing,
        )



def _pitch_index_to_factorio_note(pitch_idx: int, midi_base: int = 53) -> str:
    """Convert a pitch index (0–47) to a Factorio note name like 'F3', 'C#4'.

    *midi_base* is the MIDI note number of pitch index 0 (the lowest F in
    the rail's 4-octave window).  Defaults to 53 (F3) for backward compat.
    """
    midi = midi_base + pitch_idx
    octave = midi // 12 - 1
    semitone = midi % 12
    return f"{_MIDI_NOTE_NAMES[semitone]}{octave}"


# Patch Draftsman's instrument data before any entity creation.
_patch_instrument_notes()


# ═══════════════════════════════════════════════════════════════════════
# Layout constants (all Y positions) — compact, no gaps
# ═══════════════════════════════════════════════════════════════════════

TICKS_PER_PAGE = 60                      # decoder uses clock % 60

PORT_X = 12         # page input port X (relative to rail origin)
PORT_Y = 18         # page input port Y — same row as selectors
MOD_X = 12          # modulo AC X (relative to rail origin, or absolute for shared)
MOD_Y = 24          # modulo AC Y — separate row above LUT to avoid overlap
LUT_Y = 22          # lookup CCs Y
MATCH_Y = 20        # match DCs Y (each == sub_tick)
SEL_Y = 18          # selector ACs Y + page port
UNP_L1_Y = 16       # l1 = bell >> 21
UNP_S2_Y = 14       # s2 = bell >> 14
UNP_L2_Y = 12       # l2 = s2 & 127
UNP_S3_Y = 10       # s3 = bell >> 7
UNP_L3_Y = 8        # l3 = s3 & 127
UNP_L4_Y = 6        # l4 = bell & 127
SPK_Y = 2           # speaker grid (4 rows: 2..5)
DEBUG_Y = -2        # debug lamp row offset below speakers (4 rows: -2..1)

SUB_TICK_SIG = "signal-M"
BELL_SIG = "signal-B"


# ═══════════════════════════════════════════════════════════════════════
# Rail builder — builds one 48-speaker decoder column block
# ═══════════════════════════════════════════════════════════════════════

# Return type for _build_rail: dict of wiring endpoint IDs
class _RailEndpoints:
    """Wiring endpoints produced by ``_build_rail`` for cross-rail chaining."""
    def __init__(self):
        self.first_match_id: str = ""     # ch11_match (receives sub_tick on red)
        self.first_sel_id: str = ""       # ch11_sel (receives page data on red)
        self.last_sel_id: str = ""        # ch0_sel (for daisy-chain out)
        self.port_id: str = ""            # page_port CC
        self.mod_id: str = ""             # modulo AC (clock % 60)
        self.last_speaker_id: str = ""    # last speaker in red chain (for debug bridge)
        self.first_dbg_id: str = ""       # first debug lamp (for cross-rail bridge)
        self.speaker_ids: dict[tuple[int, int], str] = {}
        self.col_speakers: dict[int, list[str]] = {}


def _build_rail(
    blueprint: Blueprint,
    rail_idx: int,
    rail_x: int,
    instrument: str,
    signal_pool: list[str],
    qualities: list[str],
    debug_lamps: bool,
    midi_base: int = 53,
    map_drums: bool = False,
    ticks_per_page: int = TICKS_PER_PAGE,
    speaker_count: int | None = None,
) -> _RailEndpoints:
    """Build one rail's decoder pipeline at the given X offset.

    The rail places ``speaker_count`` physical speakers (pitch indices
    0..speaker_count-1) in a 12-column grid of octave rows.  Most melodic
    instruments have a 3-octave real range (36 notes) so they only get 36
    speakers; piano (F3-E7, 4 octaves) gets the full 48.

    Returns endpoint IDs for cross-rail wiring.
    """
    ep = _RailEndpoints()
    num_qual = len(qualities)
    instrument_proto = INSTRUMENT_MAP.get(
        instrument.lower().replace("programmable-speaker-instrument-", ""),
        instrument,
    )

    n_speakers = (
        speaker_count if speaker_count is not None
        else speaker_count_for(instrument)
    )
    # 12 semitone columns; rows = octaves.  n_speakers is 48 (4 rows) or
    # 36 (3 rows) — the top row's speakers are never driven for 36-note
    # instruments, so they are not placed.
    lanes = (n_speakers + 11) // 12

    prefix = f"r{rail_idx}_"

    # ── Page input port ────────────────────────────────────────────
    port_id = f"{prefix}page_port"
    port = new_entity("constant-combinator", id=port_id,
                      tile_position=(rail_x + PORT_X, PORT_Y))
    blueprint.entities.append(port)
    ep.port_id = port_id

    # ── Speakers ───────────────────────────────────────────────────
    for pitch_idx, sig in iter_speaker_signals():
        if pitch_idx >= n_speakers:
            break  # only the instrument's real-range speakers are placed
        col = rail_x + (pitch_idx % 12)
        row = SPK_Y + (3 - pitch_idx // 12)
        spk_id = f"{prefix}spk_{pitch_idx}"
        spk = new_entity("programmable-speaker", id=spk_id,
                         tile_position=(col, row))
        spk.instrument_name = instrument_proto
        is_drum = instrument_proto == "drum-kit" and map_drums
        if is_drum:
            if pitch_idx < len(DRUM_KIT_NOTES):
                spk.note_name = DRUM_KIT_NOTES[pitch_idx]
            else:
                spk.note_name = DRUM_KIT_NOTES[0]  # placeholder (never played)
        else:
            spk.note_name = _pitch_index_to_factorio_note(pitch_idx, midi_base=midi_base)
        spk.volume_signal = {"name": sig["name"], "quality": sig["quality"]}
        spk.volume_controlled_by_signal = True
        spk.allow_polyphony = True
        spk.circuit_enabled = True
        spk.set_circuit_condition(
            first_operand="signal-no-entry", comparator="=", second_operand=0,
        )
        blueprint.entities.append(spk)
        ep.speaker_ids[(col, row)] = spk_id
        if (pitch_idx % 12) not in ep.col_speakers:
            ep.col_speakers[pitch_idx % 12] = []
        ep.col_speakers[pitch_idx % 12].append(spk_id)

    # ── Debug lamps (optional) ─────────────────────────────────────
    dbg_lamp_ids: dict[tuple[int, int], str] = {}
    if debug_lamps:
        for pitch_idx, sig in iter_speaker_signals():
            if pitch_idx >= n_speakers:
                break
            col = rail_x + (pitch_idx % 12)
            lamp_row = DEBUG_Y + (3 - pitch_idx // 12)
            dbg_id = f"{prefix}dbg_{pitch_idx}"
            lamp = new_entity("small-lamp", id=dbg_id,
                             tile_position=(col, lamp_row))
            lamp.always_on = True
            lamp.circuit_enable_disable = False
            lamp.use_colors = True
            lamp.color_mode = 2
            lamp.rgb_signal = {"name": sig["name"], "quality": sig["quality"]}
            blueprint.entities.append(lamp)
            dbg_lamp_ids[(col, lamp_row)] = dbg_id

    # ── Per-channel pipeline ───────────────────────────────────────
    for ch in range(12):
        base_id = f"{prefix}ch{ch}"
        col = rail_x + ch

        # -- Lookup CC --
        cc = new_entity("constant-combinator", id=f"{base_id}_lut",
                        tile_position=(col, LUT_Y))
        slot = 0
        for t in range(ticks_per_page):
            cell_offset = t * 12 + ch
            sig_idx = cell_offset // num_qual
            qual_idx = cell_offset % num_qual
            # Sub-tick 0 silent: stored value = page size (out of range).
            value = ticks_per_page if t == 0 else t
            cc.set_signal(slot, signal_pool[sig_idx], value, qualities[qual_idx])
            slot += 1
        blueprint.entities.append(cc)

        # -- Match DC --
        dc = new_entity("decider-combinator", id=f"{base_id}_match",
                        tile_position=(col, MATCH_Y))
        dc.conditions = [
            dc.Condition(
                first_signal="signal-each", comparator="=",
                second_signal=SUB_TICK_SIG,
            )
        ]
        dc.outputs = [
            dc.Output(signal="signal-each", copy_count_from_input=False, constant=1)
        ]
        blueprint.entities.append(dc)

        # CC → match DC (green)
        blueprint.add_circuit_connection("green", f"{base_id}_lut", f"{base_id}_match")

        # -- Selector AC --
        ac_sel = new_entity("arithmetic-combinator", id=f"{base_id}_sel",
                            tile_position=(col, SEL_Y))
        ac_sel.set_arithmetic_condition(
            first_operand="signal-each", first_operand_wires={"red"},
            operation="*",
            second_operand="signal-each", second_operand_wires={"green"},
            output_signal=BELL_SIG,
        )
        blueprint.entities.append(ac_sel)

        # Match DC → selector AC (green)
        blueprint.add_circuit_connection(
            "green", f"{base_id}_match", f"{base_id}_sel",
            side_1="output", side_2="input",
        )

        # -- Unpacker chain (up to 6 ACs — one per octave lane present) --
        spk_sigs = [pitch_index_to_signal(ch + oct * 12) for oct in range(lanes)]

        def _ac(uid, y, first_op, op, second_op, out, *, _bid=base_id):
            ac = new_entity("arithmetic-combinator", id=f"{_bid}_{uid}",
                            tile_position=(col, y))
            ac.set_arithmetic_condition(
                first_operand=first_op, operation=op,
                second_operand=second_op, output_signal=out,
            )
            blueprint.entities.append(ac)
            return f"{_bid}_{uid}"

        uid_l1 = _ac("l1", UNP_L1_Y, BELL_SIG, ">>", 21, spk_sigs[0])
        uid_s2 = _ac("s2", UNP_S2_Y, BELL_SIG, ">>", 14, "signal-5")
        uid_l2 = _ac("l2", UNP_L2_Y, "signal-5", "AND", 127, spk_sigs[1])
        uid_s3 = _ac("s3", UNP_S3_Y, BELL_SIG, ">>", 7, "signal-6")
        uid_l3 = _ac("l3", UNP_L3_Y, "signal-6", "AND", 127, spk_sigs[2])
        out_order = [uid_l1, uid_l2, uid_l3]
        if lanes >= 4:
            uid_l4 = _ac("l4", UNP_L4_Y, BELL_SIG, "AND", 127, spk_sigs[3])
            out_order.append(uid_l4)

        # Green wiring
        blueprint.add_circuit_connection(
            "green", f"{base_id}_sel", uid_l1,
            side_1="output", side_2="input",
        )
        blueprint.add_circuit_connection(
            "green", uid_l1, uid_s2,
            side_1="input", side_2="input",
        )
        blueprint.add_circuit_connection(
            "green", uid_s2, uid_s3,
            side_1="input", side_2="input",
        )
        if lanes >= 4:
            blueprint.add_circuit_connection(
                "green", uid_s3, uid_l4,
                side_1="input", side_2="input",
            )
        blueprint.add_circuit_connection(
            "green", uid_s2, uid_l2,
            side_1="output", side_2="input",
        )
        blueprint.add_circuit_connection(
            "green", uid_s3, uid_l3,
            side_1="output", side_2="input",
        )

        # Red chain: l1→l2→l3→l4 output side, then down the column's four
        # speakers.  Column-local red network keeps every speaker ≤ 2 red
        # wires and all wires ≤ 4 tiles (a cross-column grid snake would
        # over-connect the column-head speakers beyond Factorio's limit).
        for i in range(len(out_order) - 1):
            blueprint.add_circuit_connection(
                "red", out_order[i], out_order[i + 1],
                side_1="output", side_2="output",
            )
        col_spks = ep.col_speakers[ch]  # ascending pitch: y=3,2,1,0
        blueprint.add_circuit_connection(
            "red", out_order[-1], col_spks[0],
            side_1="output", side_2="input",
        )
        for i in range(len(col_spks) - 1):
            blueprint.add_circuit_connection(
                "red", col_spks[i], col_spks[i + 1],
                side_1="input", side_2="input",
            )

    # ── Per-rail internal wiring ───────────────────────────────────
    # NOTE: no cross-column speaker grid — each column's four speakers are
    # chained to its unpacker outputs above.

    # Debug lamp grid wiring
    if debug_lamps:
        for row_off in range(4):
            lamp_row = DEBUG_Y + row_off
            for c in range(rail_x, rail_x + 11):
                curr = dbg_lamp_ids.get((c, lamp_row))
                nxt = dbg_lamp_ids.get((c + 1, lamp_row))
                if curr and nxt:
                    blueprint.add_circuit_connection("red", curr, nxt)
        for row_off in range(3):
            lamp_row = DEBUG_Y + row_off
            curr = dbg_lamp_ids.get((rail_x + 11, lamp_row))
            nxt = dbg_lamp_ids.get((rail_x + 11, lamp_row + 1))
            if curr and nxt:
                blueprint.add_circuit_connection("red", curr, nxt)
        # Bridge speaker red bus → debug lamp grid (column by column)
        for c_off in range(12):
            spk_bottom = ep.speaker_ids.get((rail_x + c_off, SPK_Y + 0))
            dbg_bottom = dbg_lamp_ids.get((rail_x + c_off, DEBUG_Y + 0))
            if spk_bottom and dbg_bottom:
                blueprint.add_circuit_connection("red", spk_bottom, dbg_bottom)
        ep.first_dbg_id = dbg_lamp_ids.get((rail_x, DEBUG_Y + 3), "")

    # Sub-tick on RED within this rail: ch11_match → … → ch0_match
    # (wired outside this function — only the first/last IDs are needed)
    blueprint.add_circuit_connection(
        "red", f"{prefix}ch11_match", f"{prefix}ch10_match",
        side_1="input", side_2="input",
    )
    for ch in range(10, 0, -1):
        blueprint.add_circuit_connection(
            "red", f"{prefix}ch{ch}_match", f"{prefix}ch{ch-1}_match",
            side_1="input", side_2="input",
        )

    # Page data within this rail (red): port → ch11_sel → … → ch0_sel
    blueprint.add_circuit_connection(
        "red", port_id, f"{prefix}ch11_sel",
    )
    for ch in range(11, 0, -1):
        blueprint.add_circuit_connection(
            "red", f"{prefix}ch{ch}_sel", f"{prefix}ch{ch-1}_sel",
            side_1="input", side_2="input",
        )

    # Record endpoints
    ep.first_match_id = f"{prefix}ch11_match"
    ep.first_sel_id = f"{prefix}ch11_sel"
    ep.last_sel_id = f"{prefix}ch0_sel"
    ep.last_speaker_id = ep.speaker_ids.get((rail_x + 11, SPK_Y + 0), "")

    return ep


# ═══════════════════════════════════════════════════════════════════════
# Public builders
# ═══════════════════════════════════════════════════════════════════════

def build_audio_decoder(  # pylint: disable=too-many-locals
    name: str = "Audio Decoder",
    instrument: str = "piano",
    clock_signal: str = "signal-clock",
    signal_pool: list[str] | None = None,
    qualities: list[str] | None = None,
    debug_lamps: bool = False,
) -> str:
    """Build a single-rail 48-speaker audio decoder blueprint.

    This is a convenience wrapper around the multi-rail builder for
    the common single-instrument case.

    Parameters
    ----------
    debug_lamps : bool
        When True, places 48 small-lamps below the speaker grid that
        glow blue proportionally to each speaker's volume signal.
    """
    return build_multi_rail_decoder(
        name=name,
        instruments=[instrument],
        clock_signal=clock_signal,
        signal_pool=signal_pool,
        qualities=qualities,
        debug_lamps=debug_lamps,
    )


def build_multi_rail_decoder(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    name: str = "Audio Decoder",
    instruments: list[str] | None = None,
    clock_signal: str = "signal-clock",
    signal_pool: list[str] | None = None,
    qualities: list[str] | None = None,
    debug_lamps: bool = False,
    map_drums: bool = False,
    blueprint: "Blueprint | None" = None,
    x_offset: int = 0,
) -> str:
    """Build a multi-rail audio decoder blueprint.

    Each entry in *instruments* creates a complete 48-speaker decoder
    rail placed side by side. Rails share one modulo AC and have their
    red/green buses daisy-chained across all rails.

    Parameters
    ----------
    instruments : list[str]
        Instrument name per rail.  Length determines number of rails.
        Defaults to ``["piano"]`` (single rail).
    debug_lamps : bool
        When True, places debug lamps below each rail's speaker grid.
    """
    from .. import SIGNAL_POOL, QUALITIES  # pylint: disable=relative-beyond-top-level,import-outside-toplevel

    if instruments is None:
        instruments = ["piano"]
    if signal_pool is None:
        signal_pool = list(SIGNAL_POOL)
    if qualities is None:
        qualities = list(QUALITIES)

    num_rails = len(instruments)
    if num_rails == 0:
        raise ValueError("instruments must not be empty")

    own_blueprint = blueprint is None
    if own_blueprint:
        blueprint = Blueprint()
        blueprint.label = name

    # ── Build all rails ────────────────────────────────────────────
    endpoints: list[_RailEndpoints] = []
    for ri in range(num_rails):
        rail_x = ri * RAIL_WIDTH + x_offset
        inst = instruments[ri]
        midi_base = INSTRUMENT_MIDI_BASES.get(
            inst.lower().replace("programmable-speaker-instrument-", ""),
            53,  # fallback to piano range
        )
        ep = _build_rail(
            blueprint, ri, rail_x,
            inst, signal_pool, qualities,
            debug_lamps,
            midi_base=midi_base,
            map_drums=map_drums,
        )
        endpoints.append(ep)

    # ── One shared modulo AC ───────────────────────────────────────
    # Placed to the right of the last rail so sub-tick red wiring
    # stays within the 9-tile Factorio wire distance limit.
    mod_x = x_offset + num_rails * RAIL_WIDTH + 1
    ac_mod = new_entity("arithmetic-combinator", id="mod",
                        tile_position=(mod_x, MOD_Y))
    ac_mod.set_arithmetic_condition(
        first_operand=clock_signal, operation="%",
        second_operand=TICKS_PER_PAGE, output_signal=SUB_TICK_SIG,
    )
    blueprint.entities.append(ac_mod)

    # ── Cross-rail wiring ──────────────────────────────────────────
    # Sub-tick (red): mod → last rail's ch11_match → … → first rail's ch0_match
    last_ep = endpoints[-1]
    blueprint.add_circuit_connection(
        "red", "mod", last_ep.first_match_id,
        side_1="output", side_2="input",
    )
    # Chain sub_tick from rail R to rail R-1
    for ri in range(num_rails - 1, 0, -1):
        prev = endpoints[ri - 1]
        cur = endpoints[ri]
        # Connect ch0_match of rail R to ch11_match of rail R-1
        blueprint.add_circuit_connection(
            "red", f"r{ri}_ch0_match", prev.first_match_id,
            side_1="input", side_2="input",
        )

    # Page data (red): chain selectors across rails (last → first)
    for ri in range(num_rails - 1, 0, -1):
        prev = endpoints[ri - 1]
        cur = endpoints[ri]
        blueprint.add_circuit_connection(
            "red", cur.last_sel_id, prev.first_sel_id,
            side_1="input", side_2="input",
        )

    # Port green bus: connect each port to the shared mod AC if within
    # 9-tile wire distance.  More distant ports get clock through the
    # external clock source connected by the memory encoder.
    for ep in endpoints:
        port_ent = next((e for e in blueprint.entities if getattr(e, "id", None) == ep.port_id), None)
        if port_ent is not None:
            px = getattr(port_ent, "tile_position", (0, 0))[0]
            py = getattr(port_ent, "tile_position", (0, 0))[1]
            if max(abs(px - mod_x), abs(py - MOD_Y)) <= 9:
                blueprint.add_circuit_connection(
                    "green", ep.port_id, "mod",
                    side_1="input", side_2="input",
                )

    # Clock green bus: mod receives clock from an external source
    # (connected by the memory encoder when combining blueprints)

    if own_blueprint:
        return blueprint.to_string()
    # When embedded, return the first port id for cross-connection
    return endpoints[0].port_id


# ═══════════════════════════════════════════════════════════════════════
# Logical-blueprint builders
# ═══════════════════════════════════════════════════════════════════════


def _build_rail_logical(
    lb: "LogicalBlueprint",  # noqa: F821
    prefix: str,
    rail_x: int,
    instrument: str,
    signal_pool: list[str],
    qualities: list[str],
    map_drums: bool,
    midi_base: int,
    clock_signal: str,
    active_drum_pitches: set[int] | None = None,
    ticks_per_page: int = TICKS_PER_PAGE,
    speaker_count: int | None = None,
) -> "_RailEndpoints":  # noqa: F821
    """Build one rail's 48-speaker decoder into *lb* as logical entities/networks.

    *active_drum_pitches* — for a drum rail, only these pitch slots get a
    speaker (the drum types the song actually uses).  When ``None`` the full
    48-speaker grid is emitted (used by the standalone exporters, which have
    no audio data to know which drums are used).

    Entity ids are prefixed with *prefix* (e.g. ``"r0_"``) and positions are
    offset by *rail_x* so multiple rails can sit side by side.  Returns the
    cross-rail endpoint ids.
    """
    from ..logical_blueprint import Endpoint, LogicalEntity  # pylint: disable=relative-beyond-top-level,import-outside-toplevel

    num_qual = len(qualities)
    instrument_proto = INSTRUMENT_MAP.get(
        instrument.lower().replace("programmable-speaker-instrument-", ""),
        instrument,
    )
    ep = _RailEndpoints()

    # ── Layout: cells per tick + which speakers each cell drives ──
    # A rail is a row of *cells_per_tick* decoder channels; every channel
    # unpacks one packed cell (up to 4 lane loudnesses) into up to 4
    # speakers.  The generic 48-speaker rail uses 12 cells/tick (one per
    # semitone, lanes = the 4 octaves).  A compact drum rail uses only the
    # drum TYPES the song actually plays, packed 4-per-cell — so a song with
    # a single kick uses 1 cell/tick, 1 speaker and a tiny decoder.
    # A drum rail always plays the compact per-used-drum cells whenever the
    # song's data is available (active_drum_pitches), regardless of the
    # ``map_drums`` flag — that flag only controls whether below-range melodic
    # notes route INTO a kick drum, not how an existing drum rail sounds.
    # The 48-grid fallback is only for standalone exports (no audio data).
    is_drum = instrument_proto == "drum-kit" and (
        map_drums or active_drum_pitches is not None
    )
    if is_drum and active_drum_pitches is not None:
        grouping = drum_grouping(active_drum_pitches)
        cells_per_tick = len(grouping)
        channels: list[list[tuple[int, int]]] = [
            [(lane, p) for lane, p in enumerate(cell) if p is not None]
            for cell in grouping
        ]
    else:
        # Melodic rail: 12 semitone columns; only the octave lanes that have
        # physical speakers (piano = 4, the 36-note instruments = 3) — the
        # missing top octave's speakers are never driven, so they are not
        # placed.  The memory still stores 12 cells/tick (lane 3 = 0), so the
        # decoder reads the same cells it always did.
        n_speakers = (
            speaker_count if speaker_count is not None
            else speaker_count_for(instrument)
        )
        cells_per_tick = 12
        channels = [
            [(oct_idx, ch + oct_idx * 12) for oct_idx in range(4)
             if ch + oct_idx * 12 < n_speakers]
            for ch in range(12)
        ]

    # The page port and mod AC always sit at the rail's rightmost column
    # (rail_x + PORT_X).  A compact rail right-aligns its channels so the
    # last channel is directly under the port/mod — keeping port → ch0_sel
    # and mod → ch0_match short (Factorio silently drops wires > 9 tiles)
    # and leaving the shared memory column (pp_x+1) untouched for all rails.
    port_x = rail_x + PORT_X
    channel_x = rail_x + PORT_X - cells_per_tick  # channels at channel_x + c

    # ── Page input port ──────────────────────────────────────────
    port_id = f"{prefix}page_port"
    # The page port's red output is connected to the upstream audio memory
    # data bus, so it must not emit any non-audio signal.  Keep it as an
    # empty constant combinator; its input side still receives the clock
    # (green) from the timer.
    lb.add_entity(LogicalEntity(
        entity_id=port_id,
        type="constant-combinator",
        properties={"signals": []},
        position=(port_x, PORT_Y),
    ))
    ep.port_id = port_id

    # ── Modulo AC: clock % 60 → signal-M ─────────────────────────
    mod_id = f"{prefix}mod"
    lb.add_entity(LogicalEntity(
        entity_id=mod_id,
        type="arithmetic-combinator",
        properties={
            "first_operand": clock_signal,
            "operation": "%",
            "second_operand": ticks_per_page,
            "output_signal": "signal-M",
        },
        position=(port_x, MOD_Y),
    ))
    ep.mod_id = mod_id

    # ── Per-channel pipeline + speakers (cells_per_tick channels) ──
    speaker_ids: dict[int, str] = {}
    col_speakers: dict[int, list[str]] = {c: [] for c in range(cells_per_tick)}

    for c, ch_lanes in enumerate(channels):
        base_id = f"{prefix}ch{c}"
        col = channel_x + c

        def _add_ac(uid: str, first_op: str, op: str, second_op: int | str, out: str, y: int) -> str:
            ac_id = f"{base_id}_{uid}"
            lb.add_entity(LogicalEntity(
                entity_id=ac_id,
                type="arithmetic-combinator",
                properties={
                    "first_operand": first_op,
                    "operation": op,
                    "second_operand": second_op,
                    "output_signal": out,
                },
                position=(col, y),
            ))
            return ac_id

        def _fmt(s: dict[str, str]) -> str:
            return f"{s['name']}@{s['quality']}"

        # ── Unpacker layout / selector output ─────────────────
        # Drums have no pitch, so a single-volume cell is stored RAW: the
        # selector outputs the drum's own signal directly (each(red) *
        # each(green) → drum_signal) and the speaker reads it with no
        # unpacker at all — every bit of the cell is the tick→volume.  Only
        # packed cells (13+ drum types share cells) need the lane-unpacker
        # chain below.
        raw = len(ch_lanes) == 1
        sel_out_signal = BELL_SIG
        ac_specs: list[tuple[str, str, str, int | str, str]] = []  # (uid, first, op, second, out)
        lane_out: dict[int, str] = {}    # lane -> AC emitting the lane signal
        lane_pass: dict[int, str] = {}   # lane -> AC that reads bell directly
        if raw:
            sel_out_signal = _fmt(pitch_index_to_signal(ch_lanes[0][1]))
        else:
            for lane, pitch_idx in sorted(ch_lanes):
                sig_str = _fmt(pitch_index_to_signal(pitch_idx))
                if lane == 0:
                    ac_specs.append(("l1", BELL_SIG, ">>", 21, sig_str))
                    lane_out[0] = "l1"
                    lane_pass[0] = "l1"
                elif lane == 1:
                    ac_specs.append(("s2", BELL_SIG, ">>", 14, "signal-5"))
                    ac_specs.append(("l2", "signal-5", "AND", 127, sig_str))
                    lane_out[1] = "l2"
                    lane_pass[1] = "s2"
                elif lane == 2:
                    ac_specs.append(("s3", BELL_SIG, ">>", 7, "signal-6"))
                    ac_specs.append(("l3", "signal-6", "AND", 127, sig_str))
                    lane_out[2] = "l3"
                    lane_pass[2] = "s3"
                else:
                    ac_specs.append(("l4", BELL_SIG, "AND", 127, sig_str))
                    lane_out[3] = "l4"
                    lane_pass[3] = "l4"

        n_ac = len(ac_specs)
        # Speakers sit just below the last AC (or below the selector for a
        # raw cell) so no dead space is left when lanes are unused.
        first_spk_y = (UNP_L1_Y - 2 * (n_ac - 1) - 1) if n_ac else (SEL_Y - 1)

        # Lookup CC
        cc_id = f"{base_id}_lut"
        cc_signals: list[dict] = []
        for t in range(ticks_per_page):
            cell_offset = t * cells_per_tick + c
            sig_idx = cell_offset // num_qual
            qual_idx = cell_offset % num_qual
            # Sub-tick 0 is deliberately silent: its stored value is the page
            # size (out of the ``clock % ticks_per_page`` range), so the match
            # DC never selects it.  All other values are 1..page-1 (non-zero,
            # since Factorio drops 0-value signals).
            value = ticks_per_page if t == 0 else t
            cc_signals.append({
                "name": signal_pool[sig_idx],
                "value": value,
                "quality": qualities[qual_idx],
            })
        lb.add_entity(LogicalEntity(
            entity_id=cc_id,
            type="constant-combinator",
            properties={"signals": cc_signals},
            position=(col, LUT_Y),
        ))

        # Match DC (handles sub_tick 1..59)
        match_id = f"{base_id}_match"
        lb.add_entity(LogicalEntity(
            entity_id=match_id,
            type="decider-combinator",
            properties={
                "conditions": [
                    {"first": "signal-each", "op": "=", "second_signal": "signal-M"},
                ],
                "outputs": [
                    {"signal": "signal-each", "copy_count": False, "constant": 1},
                ],
            },
            position=(col, MATCH_Y),
        ))

        # Selector AC: each(red) * each(green) → drum signal (raw) or bell
        sel_id = f"{base_id}_sel"
        lb.add_entity(LogicalEntity(
            entity_id=sel_id,
            type="arithmetic-combinator",
            properties={
                "first_operand": "signal-each",
                "first_operand_wires": ["red"],
                "operation": "*",
                "second_operand": "signal-each",
                "second_operand_wires": ["green"],
                "output_signal": sel_out_signal,
            },
            position=(col, SEL_Y),
        ))

        # Unpacker ACs (only for packed cells, stacked below the selector)
        ac_id: dict[str, str] = {}
        if not raw:
            for i, (uid, first_op, op, second_op, out) in enumerate(ac_specs):
                ac_id[uid] = _add_ac(uid, first_op, op, second_op, out, UNP_L1_Y - 2 * i)

        # Speakers (beneath the unpackers / selector, one row per lane)
        for lane, pitch_idx in sorted(ch_lanes):
            sig = pitch_index_to_signal(pitch_idx)
            row = first_spk_y - lane
            spk_id = f"{prefix}spk_{pitch_idx}"
            if is_drum:
                # Drums are a fixed set of 17 sounds; a standalone drum grid
                # (no audio data) pads the extra slots with kick-1 placeholders.
                note_name = (
                    DRUM_KIT_NOTES[pitch_idx]
                    if pitch_idx < len(DRUM_KIT_NOTES)
                    else DRUM_KIT_NOTES[0]
                )
            else:
                note_name = _pitch_index_to_factorio_note(
                    pitch_idx, midi_base=midi_base,
                )
            lb.add_entity(LogicalEntity(
                entity_id=spk_id,
                type="programmable-speaker",
                properties={
                    "instrument": instrument_proto,
                    "note": note_name,
                    "vol_signal": sig["name"],
                    "vol_quality": sig["quality"],
                    "polyphony": True,
                    "circuit_enabled": True,
                },
                position=(col, row),
            ))
            speaker_ids[pitch_idx] = spk_id
            col_speakers[c].append(spk_id)

        # ── Per-channel networks ──────────────────────────────
        # CC → match DC (green)
        lb.connect("green", Endpoint(cc_id, "output"), Endpoint(match_id, "input"))

        # Match DC → selector AC (green)
        lb.connect("green", Endpoint(match_id, "output"), Endpoint(sel_id, "input"))

        if raw:
            # Selector → speaker: the volume flows straight through (red).
            lb.connect("red", Endpoint(sel_id, "output"), Endpoint(col_speakers[c][0], "input"))
        else:
            # Bell passthrough green chain (input side): sel → l1 → s2 → s3 → l4
            passthrough_order = [lane_pass[l] for l in range(4) if l in lane_pass]
            if passthrough_order:
                lb.connect("green", Endpoint(sel_id, "output"), Endpoint(ac_id[passthrough_order[0]], "input"))
                for i in range(len(passthrough_order) - 1):
                    lb.connect("green", Endpoint(ac_id[passthrough_order[i]], "input"), Endpoint(ac_id[passthrough_order[i + 1]], "input"))
            # Unpacker green: s2 output → l2 input, s3 output → l3 input
            if 1 in lane_out:
                lb.connect("green", Endpoint(ac_id[lane_pass[1]], "output"), Endpoint(ac_id[lane_out[1]], "input"))
            if 2 in lane_out:
                lb.connect("green", Endpoint(ac_id[lane_pass[2]], "output"), Endpoint(ac_id[lane_out[2]], "input"))

            # Red output chain: lane outputs (l1→l2→l3→l4, present ones) then
            # down the column's speakers.  Each column is an independent red
            # network, so every wire stays short and no speaker exceeds
            # Factorio's 2-wire per-port limit.
            out_order = [ac_id[lane_out[l]] for l in range(4) if l in lane_out]
            for i in range(len(out_order) - 1):
                lb.connect("red", Endpoint(out_order[i], "output"), Endpoint(out_order[i + 1], "output"))
            col_spks = col_speakers[c]  # ascending lane: higher y → lower
            if col_spks:
                lb.connect("red", Endpoint(out_order[-1], "output"), Endpoint(col_spks[0], "input"))
                for i in range(len(col_spks) - 1):
                    lb.connect("red", Endpoint(col_spks[i], "input"), Endpoint(col_spks[i + 1], "input"))

    # ── Cross-channel networks ─────────────────────────────────
    # Sub-tick red bus: ch{C-1}_match → … → ch0_match
    for ch in range(cells_per_tick - 1, 0, -1):
        lb.connect("red", Endpoint(f"{prefix}ch{ch}_match", "input"), Endpoint(f"{prefix}ch{ch-1}_match", "input"))

    # Page data red bus: port → ch{C-1}_sel → … → ch0_sel
    lb.connect("red", Endpoint(port_id, "output"), Endpoint(f"{prefix}ch{cells_per_tick - 1}_sel", "input"))
    for ch in range(cells_per_tick - 1, 0, -1):
        lb.connect("red", Endpoint(f"{prefix}ch{ch}_sel", "input"), Endpoint(f"{prefix}ch{ch-1}_sel", "input"))

    # Mod → last match (red, sub_tick injection)
    lb.connect("red", Endpoint(mod_id, "output"), Endpoint(f"{prefix}ch{cells_per_tick - 1}_match", "input"))

    # Clock green bus: all ports → mod
    lb.connect("green", Endpoint(port_id, "input"), Endpoint(mod_id, "input"))

    ep.first_match_id = f"{prefix}ch{cells_per_tick - 1}_match"
    ep.last_sel_id = f"{prefix}ch0_sel"
    return ep


def build_audio_decoder_logical(
    name: str = "Audio Decoder",
    instrument: str = "piano",
    clock_signal: str = "signal-clock",
    signal_pool: list[str] | None = None,
    qualities: list[str] | None = None,
    map_drums: bool = False,
    active_drum_pitches: set[int] | None = None,
    ticks_per_page: int = TICKS_PER_PAGE,
) -> "LogicalBlueprint":  # noqa: F821
    """Build a single-rail 48-speaker audio decoder as a
    :class:`LogicalBlueprint` (no positions, networks instead of wires).

    This is the logical-format counterpart of :func:`build_audio_decoder`.
    Use :func:`to_draftsman` to materialise it into a draftsman
    ``Blueprint`` with positions and physical wiring.

    Parameters
    ----------
    name : str
        Label for the blueprint.
    instrument : str
        Factorio instrument name (piano, bass, celesta, plucked, drum).
    clock_signal : str
        Name of the clock signal.
    signal_pool : list[str] | None
        Base signal names.  Defaults to the project pool.
    qualities : list[str] | None
        Quality tiers.  Defaults to the project qualities.
    map_drums : bool
        When True and instrument is ``"drum"``, speakers use drum-kit
        note names (kick-1, snare-1, …) instead of MIDI note names.

    Returns
    -------
    LogicalBlueprint
    """
    from .. import SIGNAL_POOL, QUALITIES  # pylint: disable=relative-beyond-top-level,import-outside-toplevel
    from ..logical_blueprint import Endpoint, LogicalBlueprint  # pylint: disable=relative-beyond-top-level,import-outside-toplevel

    if signal_pool is None:
        signal_pool = list(SIGNAL_POOL)
    if qualities is None:
        qualities = list(QUALITIES)

    lb = LogicalBlueprint(label=name)
    _build_rail_logical(
        lb, "", 0, instrument, signal_pool, qualities,
        map_drums, INSTRUMENT_MIDI_BASES.get(instrument, 53), clock_signal,
        active_drum_pitches=active_drum_pitches,
        ticks_per_page=ticks_per_page,
    )

    for net in lb.networks:
        if net.color == "red" and Endpoint("page_port", "output") in net.endpoints:
            lb.set_input_port("data", net.network_id)
        elif net.color == "green" and Endpoint("page_port", "input") in net.endpoints:
            lb.set_input_port("clock", net.network_id)

    return lb


def build_multi_rail_decoder_logical(
    name: str = "Audio Decoder",
    instruments: list[str] | None = None,
    clock_signal: str = "signal-clock",
    signal_pool: list[str] | None = None,
    qualities: list[str] | None = None,
    map_drums: bool = False,
    active_drum_pitches: list[set[int] | None] | None = None,
    ticks_per_page: list[int] | None = None,
) -> "LogicalBlueprint":  # noqa: F821
    """Build a **multi-rail** 48-speaker-per-rail audio decoder as a
    :class:`LogicalBlueprint`.

    One rail is built per instrument, side by side (each 13 columns wide).
    Every rail has its own mod AC (``clock % 60``) reading the **shared**
    green clock, its own page-data red bus, and its own 48 speakers.

    *active_drum_pitches* — one entry per rail (``None`` = full grid).  For
    a drum rail this is the set of pitch slots (0..16 = the 17 Factorio drum
    types) that the song actually uses, so only those speakers are placed.

    Ports
    -----
    - ``"clock"`` — shared green clock bus (all rails).
    - ``"data_0"``, ``"data_1"``, … — per-rail red page-data input bus.
    """
    from .. import SIGNAL_POOL, QUALITIES  # pylint: disable=relative-beyond-top-level,import-outside-toplevel
    from ..logical_blueprint import Endpoint, LogicalBlueprint  # pylint: disable=relative-beyond-top-level,import-outside-toplevel

    if instruments is None or not instruments:
        instruments = ["piano"]
    if signal_pool is None:
        signal_pool = list(SIGNAL_POOL)
    if qualities is None:
        qualities = list(QUALITIES)

    lb = LogicalBlueprint(label=name)
    rail_info: list[_RailEndpoints] = []
    for ri, inst in enumerate(instruments):
        info = _build_rail_logical(
            lb, f"r{ri}_", ri * RAIL_WIDTH, inst, signal_pool, qualities,
            map_drums, INSTRUMENT_MIDI_BASES.get(
                inst.lower().replace("programmable-speaker-instrument-", ""), 53,
            ),
            clock_signal,
            active_drum_pitches=(
                active_drum_pitches[ri] if active_drum_pitches else None
            ),
            ticks_per_page=(ticks_per_page[ri] if ticks_per_page else TICKS_PER_PAGE),
        )
        rail_info.append(info)

    # ── Share one green clock bus across all rails ──────────────
    # Each rail's page_port input (and mod input) green network must merge so
    # the single "clock" port feeds every rail.
    for ri in range(1, len(rail_info)):
        lb.connect("green", Endpoint(f"r{ri}_page_port", "input"), Endpoint(f"r{ri-1}_page_port", "input"))

    # ── Declare ports ────────────────────────────────────────────
    for net in lb.networks:
        if net.color == "green" and Endpoint("r0_page_port", "input") in net.endpoints:
            lb.set_input_port("clock", net.network_id)
        for ri, info in enumerate(rail_info):
            if net.color == "red" and Endpoint(info.port_id, "output") in net.endpoints:
                lb.set_input_port(f"data_{ri}", net.network_id)

    return lb
