"""Power supply placement — adds electric poles / substations to a
logical blueprint to power all combinators, speakers, and lamps.

Legendary-quality pole ranges (supply_area_distance):
- small-electric-pole: 15
- medium-electric-pole: 19
- substation: 28 (2×2 footprint)

.. attention::

   Power supply placement is **not yet implemented**.  The previous
   algorithm placed poles outside the bounding box at corners and edges,
   but produced unconnected poles and wasted corner placements.

   Redesign plan (TODO):

   1. Poles on the same line with touching ranges ARE connected
      (Factorio uses Chebyshev distance for both supply and pole-to-pole
      connection — overlapping or touching supply areas create a
      connected network).
   2. Place poles to form a **single connected copper graph** covering
      all powered entities, minimizing pole count.
   3. Prioritize the **top edge** over the bottom edge (poles above
      the display shield fewer pixels from the game camera perspective).
   4. Allow pole placement **inside** the lamp grid if necessary
      (only for the lamp matrix; never override combinators/speakers).
   5. Ensure at least one pole is within 28 tiles of the blueprint
      border so the player can connect external power.
"""

from __future__ import annotations

from dataclasses import dataclass

from .logical_blueprint import LogicalBlueprint


# ── Pole profiles ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class PoleProfile:
    """Characteristics of a power pole type at legendary quality."""
    entity_type: str        # "small-electric-pole", "medium-electric-pole", "substation"
    supply_range: int       # supply_area_distance
    width: int = 1          # tiles wide
    height: int = 1         # tiles tall


POLE_PROFILES: dict[str, PoleProfile] = {
    "small": PoleProfile("small-electric-pole", 15),
    "medium": PoleProfile("medium-electric-pole", 19),
    "substation": PoleProfile("substation", 28, width=2, height=2),
}


def add_power_to_logical(
    lb: LogicalBlueprint,
    pole_type: str = "substation",
    quality: str = "legendary",
    exclude_positions: set[tuple[int, int]] | None = None,
) -> list[tuple[int, int]]:
    """Add power poles to *lb* to cover all powered entities.

    **Not yet implemented** — returns an empty list.  See the module
    docstring for the redesign plan.
    """
    # TODO: Implement power supply placement per the redesign plan.
    #       1. Compute bounding box of all powered entities.
    #       2. Place poles covering all powered entities with minimal count,
    #          forming a single connected copper graph.
    #       3. Prioritize top edge over bottom; allow inside lamp grid.
    #       4. Ensure at least one pole is reachable from blueprint border
    #          (≤ 28 tiles to edge).
    #       5. Add poles as LogicalEntity(small/medium-electric-pole/substation)
    #          and connect them on a copper Network.
    return []


def punch_display_for_power(
    lb: LogicalBlueprint,
    pole_type: str = "substation",
    quality: str = "legendary",
) -> list[tuple[int, int]]:
    """Remove minimal lamps from a display grid and insert power poles.

    **Not yet implemented** — returns an empty list.
    """
    # TODO: Implement hole-punching variant per redesign plan.
    return []


def compute_powered_bounding_box(
    lb: LogicalBlueprint,
) -> tuple[int, int, int, int] | None:
    """Return (min_x, min_y, max_x, max_y) of powered entities, or None if none."""
    positions: set[tuple[int, int]] = set()
    for ent in lb.entities.values():
        if ent.type in (
            "arithmetic-combinator", "decider-combinator", "constant-combinator",
            "programmable-speaker", "small-lamp",
        ) and ent.position is not None:
            positions.add(ent.position)
    if not positions:
        return None
    xs = [p[0] for p in positions]
    ys = [p[1] for p in positions]
    return min(xs), min(ys), max(xs), max(ys)
