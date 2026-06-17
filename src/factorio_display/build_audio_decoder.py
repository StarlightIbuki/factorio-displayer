"""Audio decoder blueprint builder — generates a Factorio tick-based
demultiplexer blueprint for programmable-speaker audio playback."""

from draftsman.blueprintable import Blueprint
from draftsman.entity import DeciderCombinator, new_entity


def build_audio_decoder(
    signals: list[str],
    clock_signal: str = "signal-clock",
    instrument_name: str = "programmable-speaker-instrument-piano",
) -> str:
    """Build a tick-based demultiplexer for audio playback in Factorio 2.0.

    1. Constant Combinator maps pool signals to integers representing tick indices.
    2. Decider Combinator filters based on the current tick (Each == signal-clock).
    3. Arithmetic Combinator normalizes the isolated tick signal to 1 (Each / Each).
    4. Arithmetic Combinator filters the memory input to map to the instrument
       (Each * Each -> instrument).
    """
    blueprint = Blueprint()
    blueprint.label = "Audio Decoder Unit"
    blueprint.icons = ["selector-combinator"]

    # 1. Constant Combinator (Maps Signals to Integers / Ticks)
    cc = new_entity("constant-combinator", id="audio_cc", tile_position=(0, 0))
    section = cc.add_section()
    for i, sig in enumerate(signals):
        # 1000 is the max constant combinator signals limit safely
        if i >= 1000:
            break
        section.set_signal(i, {"name": sig}, i + 1)
    blueprint.entities.append(cc)

    # 2. Decider Combinator (Compares Tick Input with #1)
    dc = new_entity("decider-combinator", id="audio_dc", tile_position=(1, 0))
    dc.conditions = [
        DeciderCombinator.Condition(
            first_signal={"name": "signal-each"},
            comparator="=",
            second_signal={"name": clock_signal},
        )
    ]
    dc.outputs = [
        DeciderCombinator.Output(
            signal={"name": "signal-each"}, copy_count_from_input=True
        )
    ]
    blueprint.entities.append(dc)

    # 3. Arithmetic Combinator (Normalize to 1)
    ac1 = new_entity("arithmetic-combinator", id="audio_ac1", tile_position=(2, 0))
    ac1.conditions = {
        "first_signal": {"name": "signal-each"},
        "operation": "/",
        "second_signal": {"name": "signal-each"},
        "output_signal": {"name": "signal-each"},
    }
    blueprint.entities.append(ac1)

    # 4. Arithmetic Combinator (Filter Memory Signal)
    ac2 = new_entity("arithmetic-combinator", id="audio_ac2", tile_position=(3, 0))
    ac2.conditions = {
        "first_signal": {"name": "signal-each"},
        "operation": "*",
        "second_signal": {"name": "signal-each"},
        "output_signal": {"name": instrument_name},
    }
    blueprint.entities.append(ac2)

    # Wiring logic
    blueprint.add_circuit_connection(
        "green", "audio_cc", "audio_dc", side_1="output", side_2="input"
    )
    blueprint.add_circuit_connection(
        "green", "audio_dc", "audio_ac1", side_1="output", side_2="input"
    )
    blueprint.add_circuit_connection(
        "green", "audio_ac1", "audio_ac2", side_1="output", side_2="input"
    )

    return blueprint.to_string()
