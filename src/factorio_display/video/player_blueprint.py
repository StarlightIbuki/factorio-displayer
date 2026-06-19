"""Display blueprint builder — generates a Factorio lamp-display blueprint string."""

from draftsman.blueprintable import Blueprint
from draftsman.entity import new_entity

from .. import (
    CLOCK_SIGNAL,
    DISPLAY_BLUEPRINT,
    DISPLAY_HEIGHT,
    DISPLAY_WIDTH,
    HOLE_BOTTOM_RIGHT,
    HOLE_TOP_LEFT,
    QUALITIES,
)
from ..integer2signal.pool import get_filtered_pool
from ..integer2signal.mapping import SignalMapping


def build_display(
    name: str = "Video Display",
    width: int | None = None,
    height: int | None = None,
) -> str:
    """Build a lamp-grid display blueprint.

    If *width* and *height* match the baked defaults, returns the pre-computed
    ``DISPLAY_BLUEPRINT``.  Otherwise builds a fresh blueprint with the custom
    dimensions (using the baked hole position and qualities).
    """
    w = width if width is not None else DISPLAY_WIDTH
    h = height if height is not None else DISPLAY_HEIGHT

    if w == DISPLAY_WIDTH and h == DISPLAY_HEIGHT and DISPLAY_BLUEPRINT:
        return DISPLAY_BLUEPRINT

    hole_tl = HOLE_TOP_LEFT
    hole_br = HOLE_BOTTOM_RIGHT

    pool = get_filtered_pool(CLOCK_SIGNAL)
    mapping = SignalMapping(w, h, hole_tl, hole_br, QUALITIES, pool)

    blueprint = Blueprint()
    blueprint.label = name

    lamp_grid: list[list[str | None]] = [[None for _ in range(w)] for _ in range(h)]

    for (x, y), sig in mapping.iter_pixels():
        lamp_id = f"lamp_{x}_{y}"
        lamp = new_entity("small-lamp", id=lamp_id, tile_position=(x, y))
        lamp.always_on = True
        lamp.circuit_enable_disable = False
        lamp.use_colors = True
        lamp.color_mode = 2
        lamp.rgb_signal = {"name": sig["name"], "quality": sig["quality"]}
        blueprint.entities.append(lamp)
        lamp_grid[y][x] = lamp_id

    blueprint.entities.append(
        new_entity(
            "substation",
            id="substation_main",
            tile_position=(hole_tl[0], hole_tl[1]),
            quality="legendary",
        )
    )

    # Horizontal wiring — chain lamps across each row, skipping holes
    for y in range(h):
        for x in range(w - 1):
            curr_id = lamp_grid[y][x]
            next_x = x + 1
            while next_x < w and lamp_grid[y][next_x] is None:
                next_x += 1
            if next_x < w and curr_id:
                blueprint.add_circuit_connection("red", curr_id, lamp_grid[y][next_x])

    # Vertical wiring at the rightmost column
    for y in range(h - 1):
        if lamp_grid[y][w - 1] and lamp_grid[y + 1][w - 1]:
            blueprint.add_circuit_connection("red", lamp_grid[y][w - 1], lamp_grid[y + 1][w - 1])

    return blueprint.to_string()
