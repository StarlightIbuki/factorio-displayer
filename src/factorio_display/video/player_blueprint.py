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
# pylint: enable=relative-beyond-top-level


def build_display(  # pylint: disable=too-many-locals
    name: str = "Video Display",
    width: int | None = None,
    height: int | None = None,
) -> str:
    """Build a lamp-grid display blueprint.

    Always generates the display dynamically — no pre-computed blueprint
    is used.  Custom dimensions produce a fresh blueprint.

    No power poles or substations are placed — the user supplies power in-game.
    """
    w = width if width is not None else DISPLAY_WIDTH
    h = height if height is not None else DISPLAY_HEIGHT

    pool = get_filtered_pool(CLOCK_SIGNAL)
    mapping = SignalMapping(w, h, QUALITIES, pool)

    blueprint = Blueprint()
    blueprint.label = name

    lamp_grid: list[list[str | None]] = [[None for _ in range(w)] for _ in range(h)]

    for (x, y), sig in mapping.iter_pixels():
        lamp_id = f"lamp_{x}_{y}"
        lamp = new_entity("small-lamp", id=lamp_id, tile_position=(x, y))
        lamp.always_on = True
        lamp.circuit_enabled = False
        lamp.use_colors = True
        lamp.color_mode = 2
        lamp.rgb_signal = {"name": sig["name"], "quality": sig["quality"]}
        blueprint.entities.append(lamp)
        lamp_grid[y][x] = lamp_id

    # Horizontal wiring — chain lamps across each row
    for y in range(h):
        for x in range(w - 1):
            curr_id = lamp_grid[y][x]
            next_id = lamp_grid[y][x + 1]
            if curr_id and next_id:
                blueprint.add_circuit_connection("red", curr_id, next_id)

    # Vertical wiring at the rightmost column
    for y in range(h - 1):
        if lamp_grid[y][w - 1] and lamp_grid[y + 1][w - 1]:
            blueprint.add_circuit_connection("red", lamp_grid[y][w - 1], lamp_grid[y + 1][w - 1])

    return blueprint.to_string()
