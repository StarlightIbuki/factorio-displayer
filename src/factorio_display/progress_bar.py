"""Progress bar logical module — a row of small-lamps that light up
proportionally to a signal's value relative to a maximum.

The progress bar is a horizontal row of *length* small-lamps.  Lamp *i*
(0-indexed, leftmost) lights when ``signal >= ceil((i+1) * max_value / length)``.

All lamps are chained on the red wire.  The bar exposes an input port for
the progress signal.
"""

from __future__ import annotations

import math

from .logical_blueprint import Endpoint, LogicalBlueprint, LogicalEntity, Network


def build_progress_bar(
    name: str = "Progress Bar",
    length: int = 10,
    signal_name: str = "signal-clock",
    max_value: int = 100,
    y_row: int = 0,
) -> LogicalBlueprint:
    """Build a horizontal progress bar of *length* small-lamps.

    Parameters
    ----------
    name : str
        Blueprint label.
    length : int
        Number of lamps in the bar.
    signal_name : str
        The signal to monitor (e.g. ``"signal-T"`` for raw tick, or
        ``"signal-M"`` for sub-tick / modulo value).
    max_value : int
        The value at which all lamps are lit (100% progress).
    y_row : int
        Y-coordinate for the lamp row (all lamps share the same Y).

    Returns
    -------
    LogicalBlueprint
        With input port ``"in"`` carrying the signal network.

    Ports
    -----
    input: "in"
        Red network carrying *signal_name*.
    """
    lb = LogicalBlueprint(label=name)
    base_id = name.lower().replace(" ", "_").replace(" ", "_")

    # ── Create lamps ────────────────────────────────────────────
    lamp_ids: list[str] = []
    for i in range(length):
        # Threshold: lamp i lights when signal >= fraction of max
        threshold = math.ceil((i + 1) * max_value / length)
        lamp_id = f"{base_id}_l{i}"
        lamp = LogicalEntity(
            lamp_id,
            "small-lamp",
            properties={
                "use_colors": False,
                "always_on": False,
                "circuit_enabled": True,
                "condition": {
                    "first": signal_name,
                    "op": ">=",
                    "constant": threshold,
                },
            },
            position=(i, y_row),
        )
        lb.add_entity(lamp)
        lamp_ids.append(lamp_id)

    # ── Wire lamps in a chain (red) ─────────────────────────────
    for i in range(length - 1):
        lb.connect("red", Endpoint(lamp_ids[i], "input"), Endpoint(lamp_ids[i + 1], "input"))

    # ── Input port ─────────────────────────────────────────────
    if lamp_ids:
        # The input network is the red chain on the first lamp's input
        in_net = Network(
            network_id="red_in",
            color="red",
            endpoints={Endpoint(lamp_ids[0], "input")},
        )
        lb.add_network(in_net)
        lb.set_input_port("in", "red_in")

    return lb
