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

    # Red map: DC cell (input=red1 '0', output=red2 '1') => "01".
    # CC cell (no input '.', output red1 '0') => ".0".
    # AC input is green-only, so on the red map its input is '.'.
    #   .0
    #   01
    #   .2
    #   1.
    #   2.
    assert ".0" in text
    assert "01" in text
    assert ".2" in text
    assert "1." in text
    assert "2." in text

    # Green map: DC input on green1 '0', AC input on green1 '0'.
    assert "0." in text


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
