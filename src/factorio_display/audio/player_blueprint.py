"""Audio player blueprint builder — generates a Factorio
programmable-speaker matrix blueprint for 48-note polyphonic playback.

Decoder pipeline (top → bottom, y descending)
----------------------------------------------
  y=22   Modulo AC: sub_tick = clock % 60  (AC, 1×2)
  y=22   Lookup CCs (cols 0..11): all sub-ticks   (CC, 1×1)
  y=20   Match DCs: each(green) == sub_tick(red) → signal=1  (DC, 1×2)
  y=18   Match0 DCs: sub_tick==0 ∧ each==60 → signal=1  (DC, 1×2) — t=0 fallback
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
    iter_speaker_signals,
    pitch_index_to_signal,
    SPEAKER_COUNT,
)

INSTRUMENT_MAP: dict[str, str] = {
    "piano": "piano",
    "bass": "bass",
    "celesta": "celesta",
    "plucked": "plucked",
    "drum": "drum-kit",
}

# MIDI base per instrument — the F-aligned start of each 4-octave speaker window.
# Each rail's 48 speakers cover midi_base .. midi_base+47.
INSTRUMENT_MIDI_BASES: dict[str, int] = {
    "piano": 53,     # F3-E7  (53-100), matches instrument range exactly
    "bass": 41,      # F2-E6  (41-88),  covers bass range F2-E5 (41-76)
    "celesta": 65,   # F4-E7  (65-112), best overlap with celesta F5-E8 (77-112)
    "plucked": 65,   # F4-E7  (65-100), matches plucked range exactly
    "drum": 53,      # F3-E7  (53-100), covers drum range F3-E6 (53-88)
}

_MIDI_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

RAIL_WIDTH = 13  # 12 channel columns + 1 page_port column


def _pitch_index_to_factorio_note(pitch_idx: int, midi_base: int = 53) -> str:
    """Convert a pitch index (0–47) to a Factorio note name like 'F3', 'C#4'.

    *midi_base* is the MIDI note number of pitch index 0 (the lowest F in
    the rail's 4-octave window).  Defaults to 53 (F3) for backward compat.
    """
    midi = midi_base + pitch_idx
    octave = midi // 12 - 1
    semitone = midi % 12
    return f"{_MIDI_NOTE_NAMES[semitone]}{octave}"


# ═══════════════════════════════════════════════════════════════════════
# Layout constants (all Y positions) — compact, no gaps
# ═══════════════════════════════════════════════════════════════════════

TICKS_PER_PAGE = 60                      # decoder uses clock % 60

PORT_X = 12         # page input port X (relative to rail origin)
PORT_Y = 16         # page input port Y — same row as selectors
MOD_X = 12          # modulo AC X (relative to rail origin, or absolute for shared)
MOD_Y = 22          # modulo AC Y
LUT_Y = 22          # lookup CCs Y
MATCH_Y = 20        # match DCs Y (each == sub_tick)
MATCH0_Y = 18       # match0 DCs Y (sub_tick==0 ∧ each==60 — t=0 fallback)
SEL_Y = 16          # selector ACs Y + page port
UNP_L1_Y = 14       # l1 = bell >> 21
UNP_S2_Y = 12       # s2 = bell >> 14
UNP_L2_Y = 10       # l2 = s2 & 127
UNP_S3_Y = 8        # s3 = bell >> 7
UNP_L3_Y = 6        # l3 = s3 & 127
UNP_L4_Y = 4        # l4 = bell & 127
SPK_Y = 0           # speaker grid (4 rows: 0..3)
DEBUG_Y = -4        # debug lamp row offset below speakers (4 rows: -4..-1)

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
        self.first_match0_id: str = ""    # ch11_match0 (receives sub_tick on red)
        self.first_sel_id: str = ""       # ch11_sel (receives page data on red)
        self.last_sel_id: str = ""        # ch0_sel (for daisy-chain out)
        self.port_id: str = ""            # page_port CC
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
) -> _RailEndpoints:
    """Build one rail's 48-speaker decoder pipeline at the given X offset.

    Returns endpoint IDs for cross-rail wiring.
    """
    ep = _RailEndpoints()
    num_qual = len(qualities)
    instrument_proto = INSTRUMENT_MAP.get(
        instrument.lower().replace("programmable-speaker-instrument-", ""),
        instrument,
    )

    prefix = f"r{rail_idx}_"

    # ── Page input port ────────────────────────────────────────────
    port_id = f"{prefix}page_port"
    port = new_entity("constant-combinator", id=port_id,
                      tile_position=(rail_x + PORT_X, PORT_Y))
    blueprint.entities.append(port)
    ep.port_id = port_id

    # ── Speakers ───────────────────────────────────────────────────
    for pitch_idx, sig in iter_speaker_signals():
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
        for t in range(TICKS_PER_PAGE):
            cell_offset = t * 12 + ch
            sig_idx = cell_offset // num_qual
            qual_idx = cell_offset % num_qual
            value = 60 if t == 0 else t
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

        # -- Match0 DC --
        dc0 = new_entity("decider-combinator", id=f"{base_id}_match0",
                         tile_position=(col, MATCH0_Y))
        dc0.conditions = [
            dc0.Condition(
                first_signal=SUB_TICK_SIG, comparator="=", constant=0,
            ),
            dc0.Condition(
                first_signal="signal-each", comparator="=", constant=60,
                compare_type="and",
            ),
        ]
        dc0.outputs = [
            dc0.Output(signal="signal-each", copy_count_from_input=False, constant=1)
        ]
        blueprint.entities.append(dc0)

        # CC → both match DCs (green)
        blueprint.add_circuit_connection("green", f"{base_id}_lut", f"{base_id}_match")
        blueprint.add_circuit_connection("green", f"{base_id}_lut", f"{base_id}_match0")

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

        # Match DCs → selector AC (green)
        blueprint.add_circuit_connection(
            "green", f"{base_id}_match", f"{base_id}_sel",
            side_1="output", side_2="input",
        )
        blueprint.add_circuit_connection(
            "green", f"{base_id}_match0", f"{base_id}_sel",
            side_1="output", side_2="input",
        )

        # -- Unpacker chain (6 ACs) --
        spk_sigs = [pitch_index_to_signal(ch + oct * 12) for oct in range(4)]

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
        uid_l4 = _ac("l4", UNP_L4_Y, BELL_SIG, "AND", 127, spk_sigs[3])

        out_order = [uid_l1, uid_l2, uid_l3, uid_l4]

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

        # Red chain: l1→l2→l3→l4 output side → l4 → first speaker
        for i in range(len(out_order) - 1):
            blueprint.add_circuit_connection(
                "red", out_order[i], out_order[i + 1],
                side_1="output", side_2="output",
            )
        first_spk = ep.col_speakers[ch][0]
        blueprint.add_circuit_connection(
            "red", out_order[-1], first_spk,
            side_1="output", side_2="input",
        )

    # ── Per-rail internal wiring ───────────────────────────────────
    # Speaker grid: daisy-chain red horizontally + vertically
    for row_off in range(4):
        row = SPK_Y + row_off
        for c in range(rail_x, rail_x + 11):
            curr = ep.speaker_ids.get((c, row))
            nxt = ep.speaker_ids.get((c + 1, row))
            if curr and nxt:
                blueprint.add_circuit_connection("red", curr, nxt)
    for row_off in range(3):
        row = SPK_Y + row_off
        curr = ep.speaker_ids.get((rail_x + 11, row))
        nxt = ep.speaker_ids.get((rail_x + 11, row + 1))
        if curr and nxt:
            blueprint.add_circuit_connection("red", curr, nxt)

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
    # Same for match0
    blueprint.add_circuit_connection(
        "red", f"{prefix}ch11_match0", f"{prefix}ch10_match0",
        side_1="input", side_2="input",
    )
    for ch in range(10, 0, -1):
        blueprint.add_circuit_connection(
            "red", f"{prefix}ch{ch}_match0", f"{prefix}ch{ch-1}_match0",
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
    ep.first_match0_id = f"{prefix}ch11_match0"
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
    # Placed at the right of the first rail's page_port.
    mod_x = PORT_X + 1
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
    blueprint.add_circuit_connection(
        "red", "mod", last_ep.first_match0_id,
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
        blueprint.add_circuit_connection(
            "red", f"r{ri}_ch0_match0", prev.first_match0_id,
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

    # Port green bus: connect each port to the shared mod AC (center point)
    # then mod distributes clock to all ports within reach.
    # Avoids direct port-to-port wiring which can exceed 9-tile reach.
    for ep in endpoints:
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
