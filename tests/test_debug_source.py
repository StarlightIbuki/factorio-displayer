"""Tests for debug_source.py source-location tracking."""

from __future__ import annotations

import pytest

from factorio_display.debug_source import (
    entity_origin,
    entity_source,
    entity_traceback,
    format_entity_source,
    get_entity_origin,
    get_entity_source,
    is_trace_enabled,
    set_entity_origin,
    set_entity_source,
    set_trace_enabled,
    TRACE_ENABLED,
)


from factorio_display.logical_blueprint import (
    check_wire_topology,
    Endpoint,
    from_draftsman,
    from_toml,
    LogicalBlueprint,
    LogicalEntity,
    Network,
    to_draftsman,
    to_toml,
)


@pytest.fixture(autouse=True)
def restore_trace_enabled():
    """Force trace capture ON for capture-expecting tests and restore the
    global TRACE_ENABLED flag afterwards.

    The production default is OFF (debug metadata is a development aid);
    these tests verify the capture paths, so they opt in explicitly.
    ``TestTraceEnabledToggle`` shadows this fixture to exercise the raw
    default/toggle instead.
    """
    original = is_trace_enabled()
    set_trace_enabled(True)
    yield
    set_trace_enabled(original)


# ═══════════════════════════════════════════════════════════════════════
# Traceback capture
# ═══════════════════════════════════════════════════════════════════════

class TestEntityOrigin:
    def test_entity_source_returns_path_line(self):
        src = entity_source()
        assert isinstance(src, str)
        assert ":" in src
        parts = src.rsplit(":", 1)
        assert parts[1].isdigit()

    def test_entity_traceback_captures_callers(self):
        def inner():
            return entity_traceback(max_frames=3)

        tb = inner()
        assert isinstance(tb, tuple)
        assert len(tb) >= 1
        assert all(":" in frame for frame in tb)

    def test_entity_origin_contains_source_and_traceback(self):
        origin = entity_origin(max_frames=3)
        assert isinstance(origin.source, str)
        assert ":" in origin.source
        assert isinstance(origin.traceback, tuple)


# ═══════════════════════════════════════════════════════════════════════
# Attaching / retrieving origins
# ═══════════════════════════════════════════════════════════════════════

class TestSetEntityOrigin:
    def test_on_dict(self):
        d = {}
        set_entity_origin(d)
        assert "_debug_src" in d
        assert "_debug_traceback" in d
        assert isinstance(d["_debug_traceback"], list)

    def test_on_logical_entity(self):
        ent = LogicalEntity("ac", "arithmetic-combinator")
        set_entity_origin(ent)
        assert ent.properties["_debug_src"]
        assert isinstance(ent.properties["_debug_traceback"], list)

    def test_on_draftsman_entity(self):
        from draftsman.entity import new_entity

        de = new_entity("arithmetic-combinator", id="ac")
        set_entity_origin(de)
        assert de.tags["src"]
        assert isinstance(de.tags["traceback"], list)

    def test_get_entity_origin_returns_dataclass(self):
        ent = LogicalEntity("ac", "arithmetic-combinator")
        set_entity_origin(ent, entity_origin(max_frames=2))
        origin = get_entity_origin(ent)
        assert origin is not None
        assert origin.source == ent.properties["_debug_src"]
        assert origin.traceback == tuple(ent.properties["_debug_traceback"])

    def test_set_entity_source_keeps_existing_traceback(self):
        ent = LogicalEntity("ac", "arithmetic-combinator")
        set_entity_origin(ent)
        original_tb = get_entity_origin(ent).traceback
        set_entity_source(ent, "custom:42")
        origin = get_entity_origin(ent)
        assert origin.source == "custom:42"
        assert origin.traceback == original_tb


# ═══════════════════════════════════════════════════════════════════════
# LogicalBlueprint integration
# ═══════════════════════════════════════════════════════════════════════

class TestLogicalBlueprintOrigin:
    def test_add_entity_captures_origin(self):
        lb = LogicalBlueprint()
        lb.add_entity(LogicalEntity("a", "arithmetic-combinator"))
        origin = get_entity_origin(lb.entities["a"])
        assert origin is not None
        # add_entity delegates to set_entity_origin, so the immediate source
        # is debug_source.py; the traceback contains both logical_blueprint.py
        # and this test file.
        assert "debug_source.py" in origin.source
        assert any("logical_blueprint.py" in frame for frame in origin.traceback)
        assert any("test_debug_source.py" in frame for frame in origin.traceback)

    def test_add_network_captures_origin(self):
        lb = LogicalBlueprint()
        lb.add_network(Network("red_0", "red"))
        assert lb.networks[0]._debug_src is not None
        assert "logical_blueprint.py" in lb.networks[0]._debug_src

    def test_duplicate_entity_error_includes_source(self):
        lb = LogicalBlueprint()
        lb.add_entity(LogicalEntity("a", "arithmetic-combinator"))
        with pytest.raises(ValueError, match="created at"):
            lb.add_entity(LogicalEntity("a", "arithmetic-combinator"))

    def test_duplicate_network_error_includes_source(self):
        lb = LogicalBlueprint()
        lb.add_network(Network("red_0", "red"))
        with pytest.raises(ValueError, match="created at"):
            lb.add_network(Network("red_0", "red"))


# ═══════════════════════════════════════════════════════════════════════
# Draftsman propagation
# ═══════════════════════════════════════════════════════════════════════

class TestDraftsmanOriginPropagation:
    def test_to_draftsman_copies_origin_to_tags(self):
        lb = LogicalBlueprint()
        ent = LogicalEntity(
            "a",
            "arithmetic-combinator",
            properties={
                "first_operand": "signal-A",
                "operation": "+",
                "second_operand": 1,
                "output_signal": "signal-B",
            },
        )
        set_entity_origin(ent)
        lb.add_entity(ent)
        bp = to_draftsman(lb)
        de = next(e for e in bp.entities if getattr(e, "id", None) == "a")
        assert de.tags.get("src")
        assert isinstance(de.tags.get("traceback"), list)

    def test_from_draftsman_preserves_origin_in_properties(self):
        from draftsman.blueprintable import Blueprint
        from draftsman.entity import new_entity

        bp = Blueprint()
        de = new_entity("arithmetic-combinator", id="ac")
        de.tags["src"] = "foo.py:10"
        de.tags["traceback"] = ["foo.py:10:make", "bar.py:20:build"]
        bp.entities.append(de)

        lb = from_draftsman(bp)
        ent = lb.entities["ac"]
        assert ent.properties["_debug_src"] == "foo.py:10"
        assert ent.properties["_debug_traceback"] == [
            "foo.py:10:make",
            "bar.py:20:build",
        ]


# ═══════════════════════════════════════════════════════════════════════
# TOML round-trip
# ═══════════════════════════════════════════════════════════════════════

class TestTOMLOriginRoundTrip:
    def test_toml_roundtrip_preserves_origin(self):
        lb = LogicalBlueprint()
        ent = LogicalEntity("a", "arithmetic-combinator")
        set_entity_origin(ent)
        lb.add_entity(ent)

        toml_str = to_toml(lb)
        lb2 = from_toml(toml_str)

        origin = get_entity_origin(lb2.entities["a"])
        assert origin is not None
        assert origin.source == lb.entities["a"].properties["_debug_src"]


# ═══════════════════════════════════════════════════════════════════════
# Topology assertions include source info
# ═══════════════════════════════════════════════════════════════════════

class TestTopologySourceInErrors:
    def test_check_topology_multi_network_endpoint_includes_source(self):
        lb = LogicalBlueprint()
        lb.add_entity(LogicalEntity("a", "arithmetic-combinator"))
        for i in range(3):
            lb.add_network(
                Network(f"red_{i}", "red", endpoints={Endpoint("a", "output")})
            )
        errors = lb.check_topology()
        assert any("src=" in e for e in errors)

    def test_check_wire_topology_self_loop_includes_source(self):
        from draftsman.blueprintable import Blueprint
        from draftsman.entity import new_entity

        bp = Blueprint()
        de = new_entity("arithmetic-combinator", id="ac")
        de.tags["src"] = "maker.py:7"
        bp.entities.append(de)
        # Add a self-loop wire on the same port (invalid).
        # Wire connector IDs: 1=input red, 2=input green, 3=output red, 4=output green.
        bp.wires.append((lambda: de, 1, lambda: de, 1))

        errors = check_wire_topology(bp)
        assert errors
        assert any("maker.py:7" in e for e in errors)

    def test_format_entity_source_includes_traceback(self):
        ent = LogicalEntity("a", "arithmetic-combinator")
        set_entity_origin(ent, entity_origin(max_frames=2))
        text = format_entity_source(ent)
        assert "src=" in text


# ═══════════════════════════════════════════════════════════════════════
# Global trace toggle
# ═══════════════════════════════════════════════════════════════════════

class TestTraceEnabledToggle:
    @pytest.fixture(autouse=True)
    def restore_trace_enabled(self):
        """Shadow the module fixture: the toggle tests exercise the raw
        default and explicit toggles — do not force tracing on."""
        yield

    def test_trace_defaults_to_disabled(self):
        """Debug source/trace metadata must be OFF by default: shipping it
        bloats every blueprint (~200 B of internal file-path tags per
        entity) and leaks project paths.  (Regression: the default was
        enabled.)
        """
        from factorio_display.debug_source import _load_trace_enabled_from_env
        assert _load_trace_enabled_from_env() is False

    def test_set_trace_enabled_returns_previous_value(self):
        set_trace_enabled(True)
        previous = set_trace_enabled(False)
        assert previous is True
        assert is_trace_enabled() is False

    def test_set_entity_origin_is_noop_when_disabled(self):
        set_trace_enabled(False)
        ent = LogicalEntity("a", "arithmetic-combinator")
        set_entity_origin(ent)
        assert get_entity_origin(ent) is None

    def test_add_entity_does_not_capture_origin_when_disabled(self):
        set_trace_enabled(False)
        lb = LogicalBlueprint()
        lb.add_entity(LogicalEntity("a", "arithmetic-combinator"))
        assert get_entity_origin(lb.entities["a"]) is None

    def test_add_network_does_not_capture_origin_when_disabled(self):
        set_trace_enabled(False)
        lb = LogicalBlueprint()
        lb.add_network(Network("red_0", "red"))
        assert lb.networks[0]._debug_src is None
        assert lb.networks[0]._debug_traceback is None

    def test_enabling_trace_restores_capture(self):
        set_trace_enabled(False)
        set_trace_enabled(True)
        ent = LogicalEntity("a", "arithmetic-combinator")
        set_entity_origin(ent)
        assert get_entity_origin(ent) is not None
