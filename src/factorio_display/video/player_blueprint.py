"""Display blueprint builder — generates a Factorio lamp-display blueprint string."""

from draftsman.blueprintable import Blueprint
from draftsman.entity import new_entity

# pylint: disable=relative-beyond-top-level — valid intra-package imports
from .. import (
    CLOCK_SIGNAL,
    DISPLAY_HEIGHT,
    DISPLAY_WIDTH,
    QUALITIES,
)
from ..integer2signal.pool import get_filtered_pool
from ..integer2signal.mapping import SignalMapping
from ..logical_blueprint import Endpoint, LogicalBlueprint, LogicalEntity
# pylint: enable=relative-beyond-top-level


def build_display(  # pylint: disable=too-many-locals
    name: str = "Video Display",
    width: int | None = None,
    height: int | None = None,
) -> str:
    """Build a lamp-grid display blueprint string.

    Always generates the display dynamically — no pre-computed blueprint
    is used.  Custom dimensions produce a fresh blueprint.

    No power poles or substations are placed — the user supplies power in-game.
    """
    from ..logical_blueprint import to_draftsman
    lb = build_display_logical(name=name, width=width, height=height)
    return to_draftsman(lb).to_string()


def build_display_logical(  # pylint: disable=too-many-locals
    name: str = "Video Display",
    width: int | None = None,
    height: int | None = None,
) -> LogicalBlueprint:
    """Build a lamp-grid display LogicalBlueprint.

    Returns a :class:`LogicalBlueprint` with ``input_ports={"data": ...}``
    for the colour signal bus.
    """
    w = width if width is not None else DISPLAY_WIDTH
    h = height if height is not None else DISPLAY_HEIGHT

    pool = get_filtered_pool(CLOCK_SIGNAL)
    mapping = SignalMapping(w, h, QUALITIES, pool)

    lb = LogicalBlueprint(label=name)

    lamp_grid: list[list[str | None]] = [[None for _ in range(w)] for _ in range(h)]

    for (x, y), sig in mapping.iter_pixels():
        lamp_id = f"lamp_{x}_{y}"
        sig_str = sig["name"]
        if sig.get("quality") and sig["quality"] != "normal":
            sig_str = f"{sig['name']}@{sig['quality']}"
        lamp = LogicalEntity(
            lamp_id,
            "small-lamp",
            properties={
                "use_colors": True,
                "always_on": True,
                "circuit_enabled": False,
                "color_signal": sig_str,
            },
            position=(x, y),
        )
        lb.add_entity(lamp)
        lamp_grid[y][x] = lamp_id

    # Horizontal wiring — chain lamps across each row
    for y in range(h):
        for x in range(w - 1):
            curr_id = lamp_grid[y][x]
            next_id = lamp_grid[y][x + 1]
            if curr_id and next_id:
                lb.connect("red", Endpoint(curr_id, "input"),
                          Endpoint(next_id, "input"))

    # Vertical wiring at the rightmost column
    for y in range(h - 1):
        if lamp_grid[y][w - 1] and lamp_grid[y + 1][w - 1]:
            lb.connect("red", Endpoint(lamp_grid[y][w - 1], "input"),
                      Endpoint(lamp_grid[y + 1][w - 1], "input"))

    # Declare data input port (first lamp's network)
    if lamp_grid[0][0]:
        for net in lb.networks:
            if net.color == "red" and Endpoint(lamp_grid[0][0], "input") in net.endpoints:
                lb.set_input_port("data", net.network_id)
                break

    return lb
