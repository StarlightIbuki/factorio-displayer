"""Timer logical modules — clock generators for the all-in-one blueprint.

Provides three timer types as :class:`~logical_blueprint.LogicalBlueprint`:

- **raw timer**: self-looping AC that increments a signal by 1 each tick.
- **mod timer**: AC that computes ``signal % interval`` (e.g., sub-tick).
- **repeater**: self-looping AC that increments by a large constant; the
  consumer divides to control the effective tick rate.  Optional modulo
  for cyclic repeat.

Each timer exposes named ports for composition.
"""

from __future__ import annotations

from .logical_blueprint import Endpoint, LogicalBlueprint, LogicalEntity, Network


def build_raw_timer(
    name: str = "Raw Timer",
    output_signal: str = "signal-clock",
    clock_signal: str = "signal-clock",
) -> LogicalBlueprint:
    """Build a raw clock timer that increments *output_signal* by 1 each tick.

    The timer is a single arithmetic combinator wired output→input on red.
    A constant combinator provides the initial ``output_signal = 1`` pulse.

    Ports
    -----
    output: "out"
        The red network carrying *output_signal* (incrementing value).

    Parameters
    ----------
    name : str
        Blueprint label.
    output_signal : str
        Signal name to use for the timer output (default ``"signal-clock"``).
    clock_signal : str
        Reserved for future tick-gating; not actively used by the raw timer.
    """
    lb = LogicalBlueprint(label=name)

    # ── Kick-start CC ──────────────────────────────────────────
    cc = LogicalEntity(
        f"{name.lower().replace(' ', '_')}_kick",
        "constant-combinator",
        properties={
            "signals": [
                {"name": output_signal, "value": 1},
            ],
        },
        position=(0, 0),
    )
    lb.add_entity(cc)

    # ── Incrementer AC ─────────────────────────────────────────
    ac = LogicalEntity(
        f"{name.lower().replace(' ', '_')}_inc",
        "arithmetic-combinator",
        properties={
            "first_operand": output_signal,
            "operation": "+",
            "second_operand": 1,
            "output_signal": output_signal,
        },
        position=(0, 2),
    )
    lb.add_entity(ac)

    # Wire: CC:output → AC:input (red, kick-start)
    # Wire: AC:output → AC:input (red, self-loop)
    lb.connect("red", Endpoint(cc.entity_id, "output"), Endpoint(ac.entity_id, "input"))
    lb.connect("red", Endpoint(ac.entity_id, "output"), Endpoint(ac.entity_id, "input"))

    # ── Output port ────────────────────────────────────────────
    out_net = lb.connect("red", Endpoint(ac.entity_id, "output"), Endpoint(cc.entity_id, "output"))
    lb.set_output_port("out", out_net.network_id)

    return lb


def build_mod_timer(
    interval: int,
    *,
    name: str = "Mod Timer",
    input_signal: str = "signal-clock",
    output_signal: str = "signal-clock",
) -> LogicalBlueprint:
    """Build a modulo timer: ``output_signal = input_signal % interval``.

    The modulo AC reads *input_signal* from the **red** wire and writes
    *output_signal* to the **red** wire.  Input and output are separate
    red networks that get merged when connected to the raw timer —
    different signal names prevent collisions on the shared bus.

    Ports
    -----
    input: "in"
        **Red** network carrying *input_signal* (the raw tick value).
    output: "out"
        **Red** network carrying *output_signal* (sub-tick value in
        0..interval-1).

    Parameters
    ----------
    name : str
        Blueprint label.
    input_signal : str
        Signal to read for the raw tick count (default ``"signal-clock"``).
    interval : int
        Modulo divisor (default 60 for sub-tick indexing).
    output_signal : str
        Signal to write the modulo result to.
    """
    lb = LogicalBlueprint(label=name)

    # ── Modulo AC ──────────────────────────────────────────────
    # Reads clock from red, writes sub-tick to red.
    ac = LogicalEntity(
        f"{name.lower().replace(' ', '_')}_mod",
        "arithmetic-combinator",
        properties={
            "first_operand": input_signal,
            "operation": "%",
            "second_operand": interval,
            "output_signal": output_signal,
            "first_operand_wires": ["red"],
        },
        position=(0, 0),
    )
    lb.add_entity(ac)

    # ── Input / output ports (both red) ────────────────────────
    in_net = Network(
        network_id="red_0", color="red",
        endpoints=[Endpoint(ac.entity_id, "input")],
    )
    out_net = Network(
        network_id="red_1", color="red",
        endpoints=[Endpoint(ac.entity_id, "output")],
    )
    lb.add_network(in_net)
    lb.add_network(out_net)

    lb.set_input_port("in", "red_0")
    lb.set_output_port("out", "red_1")

    return lb


def build_clock_bridge(
    name: str = "Clock Bridge",
    clock_signal: str = "signal-clock",
) -> LogicalBlueprint:
    """Build a RED→GREEN clock bridge combinator.

    The raw timer outputs *clock_signal* on the **red** wire (self-loop).
    Many downstream components (modulo AC, video memory DCs) expect the
    clock on the **green** wire (clock bus convention).  This bridge
    copies *clock_signal* from red to green via an arithmetic combinator:

        signal-clock + 0 → signal-clock   (reads red, writes green)

    Ports
    -----
    input: "in"
        **Red** network carrying *clock_signal* (connect to raw timer output).
    output: "out"
        **Green** network carrying *clock_signal* (connect to mod timer
        input and video memory DC green bus).
    """
    lb = LogicalBlueprint(label=name)
    base_id = name.lower().replace(" ", "_")

    ac = LogicalEntity(
        f"{base_id}_ac",
        "arithmetic-combinator",
        properties={
            "first_operand": clock_signal,
            "operation": "+",
            "second_operand": 0,
            "output_signal": clock_signal,
            "first_operand_wires": ["red"],
        },
        position=(0, 0),
    )
    lb.add_entity(ac)

    in_net = Network(
        network_id="red_0", color="red",
        endpoints=[Endpoint(ac.entity_id, "input")],
    )
    out_net = Network(
        network_id="green_0", color="green",
        endpoints=[Endpoint(ac.entity_id, "output")],
    )
    lb.add_network(in_net)
    lb.add_network(out_net)

    lb.set_input_port("in", "red_0")
    lb.set_output_port("out", "green_0")

    return lb


def build_repeater(
    name: str = "Repeater",
    constant: int = 1024,
    output_signal: str = "signal-R",
    mod: int | None = None,
) -> LogicalBlueprint:
    """Build a repeating ramp timer: ``output += constant`` each tick.

    The output increments by *constant* every tick (self-looping AC wired
    output→input).  The external consumer divides *output_signal* by an
    appropriate divisor to control the effective tick rate.

    If *mod* is set, a second AC applies ``output_signal % mod`` so the
    ramp resets after reaching *mod*.

    Ports
    -----
    output: "out"
        Red network carrying *output_signal*.

    Parameters
    ----------
    name : str
        Blueprint label.
    constant : int
        Value added each tick (default 1024; allows fine-grained rate control).
    output_signal : str
        Signal name for the ramp output.
    mod : int | None
        If set, apply ``output_signal % mod`` so the ramp repeats.
    """
    lb = LogicalBlueprint(label=name)
    base_id = name.lower().replace(" ", "_")

    # ── Kick-start CC ──────────────────────────────────────────
    cc = LogicalEntity(
        f"{base_id}_kick",
        "constant-combinator",
        properties={
            "signals": [
                {"name": output_signal, "value": constant},
            ],
        },
        position=(0, 0),
    )
    lb.add_entity(cc)

    # ── Ramp AC ────────────────────────────────────────────────
    ramp_ac = LogicalEntity(
        f"{base_id}_ramp",
        "arithmetic-combinator",
        properties={
            "first_operand": output_signal,
            "operation": "+",
            "second_operand": constant,
            "output_signal": output_signal,
        },
        position=(0, 2),
    )
    lb.add_entity(ramp_ac)

    lb.connect("red", Endpoint(cc.entity_id, "output"), Endpoint(ramp_ac.entity_id, "input"))
    lb.connect("red", Endpoint(ramp_ac.entity_id, "output"), Endpoint(ramp_ac.entity_id, "input"))

    if mod is not None:
        # ── Modulo AC ──────────────────────────────────────────
        mod_ac = LogicalEntity(
            f"{base_id}_mod",
            "arithmetic-combinator",
            properties={
                "first_operand": output_signal,
                "operation": "%",
                "second_operand": mod,
                "output_signal": output_signal,
            },
            position=(0, 4),
        )
        lb.add_entity(mod_ac)
        lb.connect("red", Endpoint(ramp_ac.entity_id, "output"), Endpoint(mod_ac.entity_id, "input"))
        out_entity = mod_ac
    else:
        out_entity = ramp_ac

    # ── Output port ────────────────────────────────────────────
    out_net = Network(
        network_id="red_out",
        color="red",
        endpoints=[Endpoint(out_entity.entity_id, "output")],
    )
    lb.add_network(out_net)
    lb.set_output_port("out", "red_out")

    return lb
