"""Tests for the ASCII-art blueprint renderer (ascii_render.py)."""

from __future__ import annotations

from draftsman.blueprintable import Blueprint
from draftsman.constants import Direction
from draftsman.entity import new_entity


def _make_test_blueprint() -> Blueprint:
    """Small blueprint exercising every glyph + red/green overlap.

    Layout (all on row y=0):

        C  D>  <A  L  S
        0  2   5   8  10

    Wiring:
      red1  : CC(out) -- DC(in)
      red2  : DC(out) -- Lamp(in)
      red3  : AC(out) -- Speaker(in)
      green1: DC(in)  -- AC(in)      <-- DC input rides BOTH red1 and green1
    """
    bp = Blueprint()
    cc = new_entity("constant-combinator", id="cc", tile_position=(0, 0))
    dc = new_entity(
        "decider-combinator", id="dc", tile_position=(2, 0), direction=Direction.EAST
    )
    ac = new_entity(
        "arithmetic-combinator", id="ac", tile_position=(5, 0), direction=Direction.WEST
    )
    lamp = new_entity("small-lamp", id="lamp", tile_position=(8, 0))
    spk = new_entity("programmable-speaker", id="spk", tile_position=(10, 0))
    for e in (cc, dc, ac, lamp, spk):
        bp.entities.append(e)

    bp.add_circuit_connection("red", "cc", "dc", "input", "input")
    bp.add_circuit_connection("red", "dc", "lamp", "output", "input")
    bp.add_circuit_connection("red", "ac", "spk", "output", "input")
    bp.add_circuit_connection("green", "dc", "ac", "input", "input")
    return bp


def _map_rows(text: str, label: str) -> dict[int, str]:
    """Extract ``{y: row-content}`` from a wiring-map section by header label."""
    import re

    marker = f"=== Wiring - {label}"
    rest = text[text.index(marker) + len(marker):]
    end = rest.find("=== Wiring -")
    if end != -1:
        rest = rest[:end]
    rows: dict[int, str] = {}
    for line in rest.splitlines():
        m = re.match(r"^ *(-?\d+) (.*)$", line)
        if m:
            rows[int(m.group(1))] = m.group(2)
    return rows


def test_north_decider_wires_on_correct_halves() -> None:
    """A north-facing decider's red (output) wire renders on the TOP half
    (the anchor tile) and its green (input) wire on the BOTTOM half, and
    1x1 entities (constant combinators, lamps) render at their anchor."""
    from factorio_display.ascii_render import render_blueprint

    bp = Blueprint()
    cc = new_entity("constant-combinator", id="cc", tile_position=(0, 2))
    dc = new_entity(
        "decider-combinator", id="dc", tile_position=(2, 0),
        direction=Direction.NORTH,
    )
    lamp = new_entity("small-lamp", id="lamp", tile_position=(4, 2))
    for e in (cc, dc, lamp):
        bp.entities.append(e)
    bp.add_circuit_connection("green", "cc", "dc", "input", "input")
    bp.add_circuit_connection("red", "dc", "lamp", "output", "input")

    text = render_blueprint(bp)
    red = _map_rows(text, "RED connection map 1")
    green = _map_rows(text, "GREEN connection map 1")
    # Decider anchor at (2,0): output/red on the top half (2,0), input/green
    # on the bottom half (2,1); 1x1 entities at their own anchors.
    assert red[0][2] == "0", "red (output) should sit on the decider's TOP half"
    assert green[1][2] == "0", "green (input) should sit on the decider's BOTTOM half"
    assert red[2][4] == "0", "lamp (1x1) should render at its anchor, unshifted"
    assert green[2][0] == "0", "constant combinator should render at its anchor"


def test_entity_map_glyphs() -> None:
    from factorio_display.ascii_render import render_blueprint

    text = render_blueprint(_make_test_blueprint())
    # Glyph letters + facing markers
    assert "D>" in text      # decider, east-facing
    assert "<A" in text      # arithmetic, west-facing
    assert "C" in text       # constant combinator
    assert "L" in text       # lamp
    assert "S" in text       # speaker
    assert "Blueprint entities" in text


def test_wiring_red_and_green_on_separate_maps() -> None:
    from factorio_display.ascii_render import render_blueprint

    text = render_blueprint(_make_test_blueprint())

    # Both colour maps are present and labelled.
    assert "RED connection map 1" in text
    assert "GREEN connection map 1" in text

    # Legends identify colour per network.
    assert "'0' = red network" in text
    assert "'0' = green network" in text

    # Red map grid row (entities at y=0, columns 0..10):
    #   CC '0'  |  DC '0'(in)+'1'(out)  |  AC '2'(out)  |  L '1'  |  S '2'
    # Wires render at the actual port tile: the east-facing DC's red OUTPUT
    # (net 1) sits one tile EAST of its INPUT (net 0), so the two chars are
    # adjacent at x=2,3; the west-facing AC's red output sits on its OWN
    # (leftmost) tile at x=5, right next to its input tile at x=6.
    assert "0 01 2  1 2" in text

    # Green map grid row: DC and AC both ride green1 '0'; other entities '.'.
    # The AC's green input rides its EAST tile (x=6), one right of the DC.
    assert ". 0   0 . ." in text


def test_render_from_blueprint_string() -> None:
    from factorio_display.ascii_render import render_blueprint

    bp = _make_test_blueprint()
    text = render_blueprint(bp.to_string())
    assert "D>" in text
    assert "RED connection map 1" in text


def test_extra_connection_map_reuses_chars() -> None:
    """More than 62 networks of one colour spawn an extra map reusing chars."""
    from factorio_display.ascii_render import render_blueprint

    bp = Blueprint()
    for i in range(63):
        cc = new_entity("constant-combinator", id=f"cc{i}", tile_position=(i * 3, 0))
        lamp = new_entity("small-lamp", id=f"l{i}", tile_position=(i * 3, 4))
        bp.entities.append(cc)
        bp.entities.append(lamp)
        bp.add_circuit_connection("red", f"cc{i}", f"l{i}", "input", "input")

    text = render_blueprint(bp)
    assert "RED connection map 1" in text
    assert "RED connection map 2" in text


def test_no_wires_reports_empty() -> None:
    from factorio_display.ascii_render import render_blueprint

    bp = Blueprint()
    bp.entities.append(
        new_entity("small-lamp", id="lamp", tile_position=(0, 0))
    )
    text = render_blueprint(bp)
    assert "no circuit networks found" in text


def test_unknown_entity_renders_dot() -> None:
    from factorio_display.ascii_render import render_blueprint

    bp = Blueprint()
    bp.entities.append(
        new_entity("transport-belt", id="belt", tile_position=(0, 0))
    )
    text = render_blueprint(bp)
    assert "." in text
