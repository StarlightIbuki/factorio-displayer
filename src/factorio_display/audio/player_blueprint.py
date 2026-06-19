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

Signal conventions
------------------
- RED wire   = page data bus + sub_tick distribution
- GREEN wire = CC lookup outputs, bell bus, translator outputs
"""

from __future__ import annotations

from draftsman.blueprintable import Blueprint
from draftsman.entity import new_entity

from .pitch_mapping import (
    SPEAKER_COUNT,
    iter_speaker_signals,
    pitch_index_to_signal,
)

INSTRUMENT_MAP: dict[str, str] = {
    "piano": "piano",
    "bass": "bass",
    "celesta": "celesta",
    "plucked": "plucked",
    "drum": "drum-kit",
}

_MIDI_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _pitch_index_to_factorio_note(pitch_idx: int) -> str:
    from .pitch_mapping import MIDI_BASE
    midi = MIDI_BASE + pitch_idx
    octave = midi // 12 - 1
    semitone = midi % 12
    return f"{_MIDI_NOTE_NAMES[semitone]}{octave}"


# ═══════════════════════════════════════════════════════════════════════
# Layout constants (all Y positions) — compact, no gaps
# ═══════════════════════════════════════════════════════════════════════

TICKS_PER_PAGE = 60                      # decoder uses clock % 60

PORT_X = 12         # page input port X
PORT_Y = 16         # page input port Y — same row as selectors
MOD_X = 12          # modulo AC X (single AC, clock % 60 → sub_tick)
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


# ═══════════════════════════════════════════════════════════════════════
# Main builder
# ═══════════════════════════════════════════════════════════════════════

def build_audio_decoder(
    name: str = "Audio Decoder",
    instrument: str = "piano",
    clock_signal: str = "signal-clock",
    signal_pool: list[str] | None = None,
    qualities: list[str] | None = None,
) -> str:
    from .. import SIGNAL_POOL, QUALITIES

    if signal_pool is None:
        signal_pool = list(SIGNAL_POOL)
    if qualities is None:
        qualities = list(QUALITIES)

    num_base = len(signal_pool)
    num_qual = len(qualities)

    blueprint = Blueprint()
    blueprint.label = name

    instrument_proto = INSTRUMENT_MAP.get(
        instrument.lower().replace("programmable-speaker-instrument-", ""),
        instrument,
    )

    # ── Page input port (col 12, same row as selectors) ─────────────
    port = new_entity("constant-combinator", id="page_port",
                      tile_position=(PORT_X, PORT_Y))
    port.set_signal(0, "signal-info", 1)
    blueprint.entities.append(port)

    # ── Modulo: sub_tick = clock % 60  (single AC, 0..59) ──────────
    # t=0 (value 0) is handled by a separate match0 DC per channel
    # because Factorio drops 0-value signals from the circuit network.
    sub_tick_sig = "signal-M"

    ac_mod = new_entity("arithmetic-combinator", id="mod",
                        tile_position=(MOD_X, MOD_Y))
    ac_mod.set_arithmetic_condition(
        first_operand=clock_signal, operation="%",
        second_operand=TICKS_PER_PAGE, output_signal=sub_tick_sig,
    )
    blueprint.entities.append(ac_mod)

    # ── Speakers ───────────────────────────────────────────────────────
    speaker_ids: dict[tuple[int, int], str] = {}
    col_speakers: dict[int, list[str]] = {c: [] for c in range(12)}

    for pitch_idx, sig in iter_speaker_signals():
        col = pitch_idx % 12
        row = SPK_Y + (3 - pitch_idx // 12)  # row 0=oct6 at SPK_Y, row 3=oct3 at SPK_Y+3
        spk_id = f"spk_{pitch_idx}"
        spk = new_entity("programmable-speaker", id=spk_id,
                         tile_position=(col, row))
        spk.instrument_name = instrument_proto
        spk.note_name = _pitch_index_to_factorio_note(pitch_idx)
        spk.volume_signal = {"name": sig["name"], "quality": sig["quality"]}
        spk.volume_controlled_by_signal = True
        spk.allow_polyphony = True
        spk.circuit_enabled = True
        spk.set_circuit_condition(
            first_operand="signal-no-entry", comparator="=", second_operand=0,
        )

        blueprint.entities.append(spk)
        speaker_ids[(col, row)] = spk_id
        col_speakers[col].append(spk_id)

    # ── Per-channel pipeline ───────────────────────────────────────────

    for ch in range(12):
        base_id = f"ch{ch}"
        col = ch

        # -- Single lookup CC with all sub-tick entries (0-based, 0..59) --
        # t=0 uses value 60 (never 0) so the match0 DC can detect it.
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

        # -- Match DC: each == sub_tick → signal=1  (handles sub_tick 1..59)
        #    CC outputs arrive on GREEN, sub_tick arrives on RED
        dc = new_entity("decider-combinator", id=f"{base_id}_match",
                        tile_position=(col, MATCH_Y))
        dc.conditions = [
            dc.Condition(
                first_signal="signal-each", comparator="=",
                second_signal=sub_tick_sig,
            )
        ]
        dc.outputs = [
            dc.Output(signal="signal-each", copy_count_from_input=False, constant=1)
        ]
        blueprint.entities.append(dc)

        # -- Match0 DC: sub_tick==0 ∧ each==60 → signal=1  (t=0 fallback)
        dc0 = new_entity("decider-combinator", id=f"{base_id}_match0",
                         tile_position=(col, MATCH0_Y))
        dc0.conditions = [
            dc0.Condition(
                first_signal=sub_tick_sig, comparator="=", constant=0,
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

        # CC output → both match DCs (green)
        blueprint.add_circuit_connection("green", f"{base_id}_lut", f"{base_id}_match")
        blueprint.add_circuit_connection("green", f"{base_id}_lut", f"{base_id}_match0")

        # -- Selector AC: each(red) * each(green) → bell --
        bell_sig = "signal-B"
        ac_sel = new_entity("arithmetic-combinator", id=f"{base_id}_sel",
                            tile_position=(col, SEL_Y))
        ac_sel.set_arithmetic_condition(
            first_operand="signal-each", first_operand_wires={"red"},
            operation="*",
            second_operand="signal-each", second_operand_wires={"green"},
            output_signal=bell_sig,
        )
        blueprint.entities.append(ac_sel)

        # Wire both match DCs → selector AC (GREEN — translator output)
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

        def _ac(uid, y, first_op, op, second_op, out):
            ac = new_entity("arithmetic-combinator", id=f"{base_id}_{uid}",
                            tile_position=(col, y))
            ac.set_arithmetic_condition(
                first_operand=first_op, operation=op,
                second_operand=second_op, output_signal=out,
            )
            blueprint.entities.append(ac)
            return f"{base_id}_{uid}"

        uid_l1 = _ac("l1", UNP_L1_Y, bell_sig, ">>", 21, spk_sigs[0])
        uid_s2 = _ac("s2", UNP_S2_Y, bell_sig, ">>", 14, "signal-5")
        uid_l2 = _ac("l2", UNP_L2_Y, "signal-5", "AND", 127, spk_sigs[1])
        uid_s3 = _ac("s3", UNP_S3_Y, bell_sig, ">>", 7, "signal-6")
        uid_l3 = _ac("l3", UNP_L3_Y, "signal-6", "AND", 127, spk_sigs[2])
        uid_l4 = _ac("l4", UNP_L4_Y, bell_sig, "AND", 127, spk_sigs[3])

        out_order = [uid_l1, uid_l2, uid_l3, uid_l4]

        # ── Green wiring ──────────────────────────────────────────────
        # Bell bus: selector output → l1_in → s2_in → s3_in → l4_in
        #   (input-to-input so the original bell reaches every stage)
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

        # Intermediate: s2_out → l2_in, s3_out → l3_in
        blueprint.add_circuit_connection(
            "green", uid_s2, uid_l2,
            side_1="output", side_2="input",
        )
        blueprint.add_circuit_connection(
            "green", uid_s3, uid_l3,
            side_1="output", side_2="input",
        )

        # Red chain: l1→l2→l3→l4 output side → then l4 → first speaker
        for i in range(len(out_order) - 1):
            blueprint.add_circuit_connection(
                "red", out_order[i], out_order[i + 1],
                side_1="output", side_2="output",
            )
        first_spk = col_speakers[ch][0]
        blueprint.add_circuit_connection(
            "red", out_order[-1], first_spk,
            side_1="output", side_2="input",
        )

    # ── Global wiring ──────────────────────────────────────────────────

    # Speaker grid: daisy-chain red horizontally + vertically
    for row_off in range(4):
        row = SPK_Y + row_off
        for c in range(11):
            curr = speaker_ids.get((c, row))
            nxt = speaker_ids.get((c + 1, row))
            if curr and nxt:
                blueprint.add_circuit_connection("red", curr, nxt)
    for row_off in range(3):
        row = SPK_Y + row_off
        curr = speaker_ids.get((11, row))
        nxt = speaker_ids.get((11, row + 1))
        if curr and nxt:
            blueprint.add_circuit_connection("red", curr, nxt)

    # Sub-tick on RED: mod(col 12) → ch11_match → … → ch0_match (red, input side)
    blueprint.add_circuit_connection(
        "red", "mod", "ch11_match",
        side_1="output", side_2="input",
    )
    for ch in range(11, 0, -1):
        blueprint.add_circuit_connection(
            "red", f"ch{ch}_match", f"ch{ch-1}_match",
            side_1="input", side_2="input",
        )
    # Same sub-tick distribution for match0 DCs
    blueprint.add_circuit_connection(
        "red", "mod", "ch11_match0",
        side_1="output", side_2="input",
    )
    for ch in range(11, 0, -1):
        blueprint.add_circuit_connection(
            "red", f"ch{ch}_match0", f"ch{ch-1}_match0",
            side_1="input", side_2="input",
        )

    # Page data: page_port (col 12) → ch11_sel → … → ch0_sel (RED input side)
    blueprint.add_circuit_connection(
        "red", "page_port", "ch11_sel",
    )
    for ch in range(11, 0, -1):
        blueprint.add_circuit_connection(
            "red", f"ch{ch}_sel", f"ch{ch-1}_sel",
            side_1="input", side_2="input",
        )

    return blueprint.to_string()
