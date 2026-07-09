"""Tests for logical_blueprint.py — intermediate blueprint representation."""

from __future__ import annotations

import pytest

from factorio_display.logical_blueprint import (
    blueprint_string_to_yaml,
    Endpoint,
    from_blueprint_string,
    LogicalBlueprint,
    LogicalEntity,
    Network,
    from_draftsman,
    from_toml,
    to_draftsman,
    to_toml,
)


# ═══════════════════════════════════════════════════════════════════════
# Endpoint
# ═══════════════════════════════════════════════════════════════════════

class TestEndpoint:
    def test_valid_port(self):
        ep = Endpoint("mod", "output")
        assert ep.entity_id == "mod"
        assert ep.port == "output"

    def test_invalid_port_raises(self):
        with pytest.raises(ValueError, match="Invalid port"):
            Endpoint("mod", "left")

    def test_to_string(self):
        assert Endpoint("mod", "output").to_string() == "mod:output"
        assert Endpoint("mod", "input").to_string() == "mod:input"

    def test_from_string(self):
        ep = Endpoint.from_string("mod:output")
        assert ep.entity_id == "mod"
        assert ep.port == "output"

    def test_from_string_invalid(self):
        with pytest.raises(ValueError):
            Endpoint.from_string("mod:left")
        with pytest.raises(ValueError):
            Endpoint.from_string("mod")


# ═══════════════════════════════════════════════════════════════════════
# LogicalBlueprint basics
# ═══════════════════════════════════════════════════════════════════════

class TestLogicalBlueprint:
    def test_add_entity(self):
        lb = LogicalBlueprint()
        lb.add_entity(LogicalEntity("mod", "arithmetic-combinator"))
        assert "mod" in lb.entities

    def test_add_duplicate_entity_raises(self):
        lb = LogicalBlueprint()
        lb.add_entity(LogicalEntity("mod", "arithmetic-combinator"))
        with pytest.raises(ValueError, match="Duplicate"):
            lb.add_entity(LogicalEntity("mod", "arithmetic-combinator"))

    def test_connect_creates_new_network(self):
        lb = LogicalBlueprint()
        lb.add_entity(LogicalEntity("mod", "arithmetic-combinator"))
        lb.add_entity(LogicalEntity("match", "decider-combinator"))
        net = lb.connect("red", Endpoint("mod", "output"), Endpoint("match", "input"))
        assert net.color == "red"
        assert len(net.endpoints) == 2
        assert Endpoint("mod", "output") in net.endpoints
        assert Endpoint("match", "input") in net.endpoints
        assert len(lb.networks) == 1

    def test_connect_extends_existing_network(self):
        """Adding a new endpoint to an existing network extends it."""
        lb = LogicalBlueprint()
        lb.add_entity(LogicalEntity("a", "arithmetic-combinator"))
        lb.add_entity(LogicalEntity("b", "arithmetic-combinator"))
        lb.add_entity(LogicalEntity("c", "arithmetic-combinator"))
        # a:output and b:input join network red_0
        lb.connect("red", Endpoint("a", "output"), Endpoint("b", "input"))
        # Now connect a:output (already in red_0) to c:input → extends red_0
        lb.connect("red", Endpoint("a", "output"), Endpoint("c", "input"))
        assert len(lb.networks) == 1
        assert len(lb.networks[0].endpoints) == 3

    def test_connect_merges_networks(self):
        """Connecting endpoints from two existing networks merges them."""
        lb = LogicalBlueprint()
        for eid in ("a", "b", "c", "d"):
            lb.add_entity(LogicalEntity(eid, "arithmetic-combinator"))
        # Network red_0: a:output ↔ b:input
        lb.connect("red", Endpoint("a", "output"), Endpoint("b", "input"))
        # Network red_1: a:output (already in red_0) ↔ c:input → extends red_0
        lb.connect("red", Endpoint("a", "output"), Endpoint("c", "input"))
        # Network red_1 (new): c:output ↔ d:input
        lb.connect("red", Endpoint("c", "output"), Endpoint("d", "input"))
        assert len(lb.networks) == 2  # red_0 (a:out,b:in,c:in) + red_1 (c:out,d:in)
        # Merge by connecting b:input (in red_0) to c:output (in red_1)
        lb.connect("red", Endpoint("b", "input"), Endpoint("c", "output"))
        assert len(lb.networks) == 1
        assert len(lb.networks[0].endpoints) == 5  # a:out, b:in, c:in, c:out, d:in

    def test_connect_same_network_noop(self):
        lb = LogicalBlueprint()
        lb.add_entity(LogicalEntity("a", "arithmetic-combinator"))
        lb.add_entity(LogicalEntity("b", "arithmetic-combinator"))
        lb.connect("red", Endpoint("a", "output"), Endpoint("b", "input"))
        lb.connect("red", Endpoint("a", "output"), Endpoint("b", "input"))
        assert len(lb.networks) == 1
        assert len(lb.networks[0].endpoints) == 2

    def test_red_and_green_networks_separate(self):
        lb = LogicalBlueprint()
        lb.add_entity(LogicalEntity("a", "arithmetic-combinator"))
        lb.add_entity(LogicalEntity("b", "arithmetic-combinator"))
        lb.connect("red", Endpoint("a", "output"), Endpoint("b", "input"))
        lb.connect("green", Endpoint("a", "output"), Endpoint("b", "input"))
        assert len(lb.networks) == 2
        colors = {n.color for n in lb.networks}
        assert colors == {"red", "green"}

    def test_endpoints_of(self):
        lb = LogicalBlueprint()
        lb.add_entity(LogicalEntity("a", "arithmetic-combinator"))
        lb.add_entity(LogicalEntity("b", "arithmetic-combinator"))
        lb.connect("red", Endpoint("a", "output"), Endpoint("b", "input"))
        lb.connect("green", Endpoint("a", "output"), Endpoint("b", "output"))
        ep_set = lb.endpoints_of("a", "output")
        assert len(ep_set) == 2

    def test_place_relative_preserves_offsets(self):
        lb = LogicalBlueprint()
        lb.add_entity(LogicalEntity("a", "arithmetic-combinator", position=(10, 20)))
        lb.add_entity(LogicalEntity("b", "arithmetic-combinator", position=(13, 24)))

        lb.place_relative(origin_x=0, origin_y=0)

        assert lb.entities["a"].position == (0, 0)
        assert lb.entities["b"].position == (3, 4)

    def test_place_relative_can_assign_unpositioned(self):
        lb = LogicalBlueprint()
        lb.add_entity(LogicalEntity("a", "arithmetic-combinator", position=(5, 7)))
        lb.add_entity(LogicalEntity("b", "arithmetic-combinator"))

        lb.place_relative(origin_x=0, origin_y=0, assign_unpositioned=True)

        assert lb.entities["a"].position == (0, 0)
        assert lb.entities["b"].position == (0, 2)

    def test_merge_with_position_offset(self):
        src = LogicalBlueprint()
        src.add_entity(LogicalEntity("x", "arithmetic-combinator", position=(1, 2)))
        src.add_entity(LogicalEntity("y", "arithmetic-combinator"))

        dst = LogicalBlueprint()
        dst.merge(src, entity_prefix="m_", network_prefix="m_", position_offset=(10, -3))

        assert dst.entities["m_x"].position == (11, -1)
        assert dst.entities["m_y"].position is None

    def test_connect_merge_preserves_prewired_with_single_bridge(self):
        lb = LogicalBlueprint()
        for eid in ("a0", "a1", "b0", "b1"):
            lb.add_entity(LogicalEntity(eid, "arithmetic-combinator"))

        net_a = Network(
            "red_a",
            "red",
            endpoints={Endpoint("a0", "input"), Endpoint("a1", "input")},
            prewired_pairs=[(Endpoint("a0", "input"), Endpoint("a1", "input"))],
        )
        net_b = Network(
            "red_b",
            "red",
            endpoints={Endpoint("b0", "input"), Endpoint("b1", "input")},
            prewired_pairs=[(Endpoint("b0", "input"), Endpoint("b1", "input"))],
        )
        lb.add_network(net_a)
        lb.add_network(net_b)

        lb.connect("red", Endpoint("a0", "input"), Endpoint("b0", "input"))

        assert len(lb.networks) == 1
        merged_net = lb.networks[0]
        assert merged_net.prewired_pairs is not None
        pairs = merged_net.prewired_pairs
        assert len(pairs) == 3

        as_set = {frozenset((p[0].to_string(), p[1].to_string())) for p in pairs}
        assert frozenset(("a0:input", "a1:input")) in as_set
        assert frozenset(("b0:input", "b1:input")) in as_set
        # Exactly one cross-subnetwork bridge
        cross = [
            p for p in as_set
            if (any(s.startswith("a") for s in p) and any(s.startswith("b") for s in p))
        ]
        assert len(cross) == 1

    def test_connect_merge_rebuilds_missing_prewire_and_adds_single_bridge(self):
        lb = LogicalBlueprint()
        for eid in ("a0", "a1", "b0", "b1"):
            lb.add_entity(LogicalEntity(eid, "arithmetic-combinator"))

        lb.add_network(Network(
            "red_a",
            "red",
            endpoints={Endpoint("a0", "input"), Endpoint("a1", "input")},
            prewired_pairs=[(Endpoint("a0", "input"), Endpoint("a1", "input"))],
        ))
        # No prewired pairs on B: merge logic should rebuild internal pairs
        # for B, then add exactly one cross-network bridge.
        lb.add_network(Network(
            "red_b",
            "red",
            endpoints={Endpoint("b0", "input"), Endpoint("b1", "input")},
        ))

        lb.connect("red", Endpoint("a0", "input"), Endpoint("b0", "input"))

        assert len(lb.networks) == 1
        merged = lb.networks[0]
        assert merged.prewired_pairs is not None
        pairs = merged.prewired_pairs
        assert len(pairs) == 3

        as_set = {frozenset((p[0].to_string(), p[1].to_string())) for p in pairs}
        assert frozenset(("a0:input", "a1:input")) in as_set
        assert frozenset(("b0:input", "b1:input")) in as_set
        cross = [
            p for p in as_set
            if (any(s.startswith("a") for s in p) and any(s.startswith("b") for s in p))
        ]
        assert len(cross) == 1


# ═══════════════════════════════════════════════════════════════════════
# TOML round-trip
# ═══════════════════════════════════════════════════════════════════════

class TestTomlRoundTrip:
    """Test that LogicalBlueprint → TOML → LogicalBlueprint preserves data."""

    def test_empty(self):
        lb = LogicalBlueprint(label="test")
        toml_str = to_toml(lb)
        lb2 = from_toml(toml_str)
        assert lb2.label == "test"
        assert len(lb2.entities) == 0
        assert len(lb2.networks) == 0

    def test_ac_roundtrip(self):
        lb = LogicalBlueprint(label="AC test")
        lb.add_entity(
            LogicalEntity(
                "mod",
                "arithmetic-combinator",
                properties={
                    "first_operand": "signal-clock",
                    "operation": "%",
                    "second_operand": 60,
                    "output_signal": "signal-M",
                },
                position=(12, 22),
            )
        )
        toml_str = to_toml(lb)
        lb2 = from_toml(toml_str)
        assert lb2.label == "AC test"
        ent = lb2.entities["mod"]
        assert ent.type == "arithmetic-combinator"
        assert ent.properties["first_operand"] == "signal-clock"
        assert ent.properties["operation"] == "%"
        assert ent.properties["second_operand"] == 60
        assert ent.properties["output_signal"] == "signal-M"
        assert ent.position == (12, 22)

    def test_dc_roundtrip(self):
        lb = LogicalBlueprint()
        lb.add_entity(
            LogicalEntity(
                "dc0",
                "decider-combinator",
                properties={
                    "conditions": [
                        {"first": "signal-each", "op": "=", "second_signal": "signal-M"},
                    ],
                    "outputs": [
                        {"signal": "signal-each", "copy_count": False, "constant": 1},
                    ],
                },
                position=(0, 20),
            )
        )
        toml_str = to_toml(lb)
        lb2 = from_toml(toml_str)
        ent = lb2.entities["dc0"]
        assert ent.type == "decider-combinator"
        assert ent.properties["conditions"][0]["first"] == "signal-each"
        assert ent.properties["conditions"][0]["op"] == "="
        assert ent.properties["conditions"][0]["second_signal"] == "signal-M"
        assert ent.properties["outputs"][0]["signal"] == "signal-each"
        assert ent.properties["outputs"][0]["constant"] == 1
        assert ent.position == (0, 20)

    def test_cc_roundtrip(self):
        lb = LogicalBlueprint()
        lb.add_entity(
            LogicalEntity(
                "lut",
                "constant-combinator",
                properties={
                    "signals": [
                        {"name": "signal-A", "quality": "normal", "value": 60},
                        {"name": "signal-A", "quality": "uncommon", "value": 1},
                    ],
                },
            )
        )
        toml_str = to_toml(lb)
        lb2 = from_toml(toml_str)
        ent = lb2.entities["lut"]
        sigs = ent.properties["signals"]
        assert len(sigs) == 2
        assert sigs[0]["name"] == "signal-A"
        assert sigs[0]["quality"] == "normal"
        assert sigs[0]["value"] == 60

    def test_spk_roundtrip(self):
        lb = LogicalBlueprint()
        lb.add_entity(
            LogicalEntity(
                "spk_0",
                "programmable-speaker",
                properties={
                    "instrument": "piano",
                    "note": "F3",
                    "vol_signal": "signal-F",
                    "vol_quality": "normal",
                    "polyphony": True,
                },
            )
        )
        toml_str = to_toml(lb)
        lb2 = from_toml(toml_str)
        ent = lb2.entities["spk_0"]
        assert ent.properties["instrument"] == "piano"
        assert ent.properties["note"] == "F3"
        assert ent.properties["vol_signal"] == "signal-F"

    def test_network_roundtrip(self):
        lb = LogicalBlueprint()
        lb.add_entity(LogicalEntity("a", "arithmetic-combinator"))
        lb.add_entity(LogicalEntity("b", "arithmetic-combinator"))
        lb.connect("red", Endpoint("a", "output"), Endpoint("b", "input"))
        toml_str = to_toml(lb)
        lb2 = from_toml(toml_str)
        assert len(lb2.networks) == 1
        net = lb2.networks[0]
        assert net.color == "red"
        assert len(net.endpoints) == 2
        ep_strs = {ep.to_string() for ep in net.endpoints}
        assert "a:output" in ep_strs
        assert "b:input" in ep_strs

    def test_entity_ids_are_strings_in_toml(self):
        """Numeric ids should survive the round-trip as strings."""
        lb = LogicalBlueprint()
        lb.add_entity(LogicalEntity("0", "arithmetic-combinator"))
        toml_str = to_toml(lb)
        lb2 = from_toml(toml_str)
        assert "0" in lb2.entities

    def test_multi_condition_dc(self):
        """Decider with AND conditions."""
        lb = LogicalBlueprint()
        lb.add_entity(
            LogicalEntity(
                "dc",
                "decider-combinator",
                properties={
                    "conditions": [
                        {"first": "signal-clock", "op": ">=", "constant": 0},
                        {"first": "signal-clock", "op": "<=", "constant": 59},
                    ],
                    "outputs": [],
                },
            )
        )
        toml_str = to_toml(lb)
        lb2 = from_toml(toml_str)
        conds = lb2.entities["dc"].properties["conditions"]
        assert len(conds) == 2
        assert conds[0]["first"] == "signal-clock"
        assert conds[0]["op"] == ">="
        assert conds[0]["constant"] == 0
        assert conds[1]["constant"] == 59


# ═══════════════════════════════════════════════════════════════════════
# Draftsman ↔ LogicalBlueprint round-trip
# ═══════════════════════════════════════════════════════════════════════

class TestDraftsmanRoundTrip:
    """Test that Blueprint → LogicalBlueprint → Blueprint preserves structure."""

    def test_simple_ac_roundtrip(self):
        from draftsman.blueprintable import Blueprint
        from draftsman.entity import new_entity

        bp = Blueprint()
        bp.label = "AC test"
        ac = new_entity(
            "arithmetic-combinator",
            id="mod",
            tile_position=(12, 22),
        )
        ac.set_arithmetic_condition(
            first_operand="signal-clock",
            operation="%",
            second_operand=60,
            output_signal="signal-M",
        )
        bp.entities.append(ac)

        lb = from_draftsman(bp)
        assert lb.label == "AC test"
        assert "mod" in lb.entities
        ent = lb.entities["mod"]
        assert ent.type == "arithmetic-combinator"
        assert ent.properties["first_operand"] == "signal-clock"
        assert ent.properties["operation"] == "%"
        assert ent.properties["second_operand"] == 60
        assert ent.properties["output_signal"] == "signal-M"
        assert ent.position == (12, 22)

        # Convert back
        bp2 = to_draftsman(lb)
        assert bp2.label == "AC test"
        assert len(bp2.entities) == 1

        # Connectivity validation
        from conftest import validate_logical_connectivity
        vresult = validate_logical_connectivity(lb)
        assert vresult["errors"] == [], f"Connectivity errors: {vresult['errors']}"

    def test_simple_cc_roundtrip(self):
        from draftsman.blueprintable import Blueprint
        from draftsman.entity import new_entity

        bp = Blueprint()
        cc = new_entity("constant-combinator", id="lut", tile_position=(0, 22))
        cc.set_signal(0, "signal-A", 60, "normal")
        cc.set_signal(1, "signal-A", 1, "uncommon")
        bp.entities.append(cc)

        lb = from_draftsman(bp)
        ent = lb.entities["lut"]
        sigs = ent.properties["signals"]
        assert len(sigs) == 2
        assert sigs[0]["name"] == "signal-A"
        assert sigs[0]["value"] == 60
        assert sigs[0]["quality"] == "normal"

        bp2 = to_draftsman(lb)
        assert len(bp2.entities) == 1

        # Connectivity validation
        from conftest import validate_logical_connectivity
        vresult = validate_logical_connectivity(lb)
        assert vresult["errors"] == [], f"Connectivity errors: {vresult['errors']}"

    def test_simple_dc_roundtrip(self):
        from draftsman.blueprintable import Blueprint
        from draftsman.entity import new_entity

        bp = Blueprint()
        dc = new_entity("decider-combinator", id="match", tile_position=(0, 20))
        dc.conditions = [
            dc.Condition(
                first_signal="signal-each",
                comparator="=",
                second_signal="signal-M",
            ),
        ]
        dc.outputs = [
            dc.Output(
                signal="signal-each",
                copy_count_from_input=False,
                constant=1,
            ),
        ]
        bp.entities.append(dc)

        lb = from_draftsman(bp)
        ent = lb.entities["match"]
        assert ent.type == "decider-combinator"
        conds = ent.properties["conditions"]
        assert conds[0]["first"] == "signal-each"
        assert conds[0]["op"] == "="
        assert conds[0]["second_signal"] == "signal-M"
        outs = ent.properties["outputs"]
        assert outs[0]["signal"] == "signal-each"
        assert outs[0]["constant"] == 1

        bp2 = to_draftsman(lb)
        assert len(bp2.entities) == 1

        # Connectivity validation
        from conftest import validate_logical_connectivity
        vresult = validate_logical_connectivity(lb)
        assert vresult["errors"] == [], f"Connectivity errors: {vresult['errors']}"

    def test_simple_spk_roundtrip(self):
        from draftsman.blueprintable import Blueprint
        from draftsman.entity import new_entity

        bp = Blueprint()
        spk = new_entity("programmable-speaker", id="spk_0", tile_position=(0, 0))
        spk.instrument_name = "piano"
        spk.note_name = "F3"
        spk.volume_signal = {"name": "signal-F", "quality": "normal"}
        spk.volume_controlled_by_signal = True
        spk.allow_polyphony = True
        spk.circuit_enabled = True
        spk.set_circuit_condition(
            first_operand="signal-no-entry", comparator="=", second_operand=0,
        )
        bp.entities.append(spk)

        lb = from_draftsman(bp)
        ent = lb.entities["spk_0"]
        assert ent.properties["instrument"] == "piano"
        assert ent.properties["note"] == "F3"
        assert ent.properties["vol_signal"] == "signal-F"
        assert ent.properties["vol_quality"] == "normal"
        assert ent.properties["polyphony"] is True

        bp2 = to_draftsman(lb)
        assert len(bp2.entities) == 1

        # Connectivity validation
        from conftest import validate_logical_connectivity
        vresult = validate_logical_connectivity(lb)
        assert vresult["errors"] == [], f"Connectivity errors: {vresult['errors']}"

    def test_connections_roundtrip(self):
        from draftsman.blueprintable import Blueprint
        from draftsman.entity import new_entity

        bp = Blueprint()
        bp.label = "Wired"
        ac = new_entity("arithmetic-combinator", id="mod", tile_position=(4, 22))
        ac.set_arithmetic_condition(
            first_operand="signal-clock", operation="%",
            second_operand=60, output_signal="signal-M",
        )
        bp.entities.append(ac)

        dc = new_entity("decider-combinator", id="match", tile_position=(0, 20))
        dc.conditions = [
            dc.Condition(
                first_signal="signal-each", comparator="=",
                second_signal="signal-M",
            ),
        ]
        dc.outputs = [
            dc.Output(signal="signal-each", copy_count_from_input=False, constant=1),
        ]
        bp.entities.append(dc)

        bp.add_circuit_connection(
            "red", "mod", "match",
            side_1="output", side_2="input",
        )

        lb = from_draftsman(bp)
        assert len(lb.networks) == 1
        net = lb.networks[0]
        assert net.color == "red"
        assert len(net.endpoints) == 2

        bp2 = to_draftsman(lb)
        assert len(bp2.entities) == 2
        # Check that the connection was reconstructed
        conns = _get_connections_list(bp2)
        assert len(conns) == 1

        # Connectivity validation
        from conftest import validate_logical_connectivity
        vresult = validate_logical_connectivity(lb)
        assert vresult["errors"] == [], f"Connectivity errors: {vresult['errors']}"

    def test_from_blueprint_string(self):
        from draftsman.blueprintable import Blueprint
        from draftsman.entity import new_entity

        bp = Blueprint()
        ac = new_entity("arithmetic-combinator", id="mod", tile_position=(1, 2))
        ac.set_arithmetic_condition(
            first_operand="signal-clock",
            operation="%",
            second_operand=60,
            output_signal="signal-M",
        )
        bp.entities.append(ac)

        lb = from_blueprint_string(bp.to_string())
        assert len(lb.entities) == 1
        ent = next(iter(lb.entities.values()))
        assert ent.type == "arithmetic-combinator"
        assert ent.position == (1, 2)

    def test_blueprint_string_to_yaml(self):
        from draftsman.blueprintable import Blueprint
        from draftsman.entity import new_entity

        bp = Blueprint()
        bp.label = "YamlTest"
        cc = new_entity("constant-combinator", id="lut", tile_position=(0, 0))
        cc.set_signal(0, "signal-A", 5, "normal")
        bp.entities.append(cc)

        yaml_text = blueprint_string_to_yaml(bp.to_string())
        assert 'label: "YamlTest"' in yaml_text
        assert 'type: "constant-combinator"' in yaml_text

    def test_toml_to_draftsman(self):
        """Build a LogicalBlueprint from TOML, convert to draftsman."""
        toml_str = '''\
label = "From TOML"

[[entity]]
id = "mod"
type = "arithmetic-combinator"
first_operand = "signal-clock"
operation = "%"
second_operand = 60
output_signal = "signal-M"
position = [0, 22]

[[entity]]
id = "match"
type = "decider-combinator"
position = [0, 20]
[[entity.condition]]
first = "signal-each"
op = "="
second_signal = "signal-M"
[[entity.output]]
signal = "signal-each"
copy_count = false
constant = 1

[[network]]
id = "red_0"
color = "red"
endpoints = ["mod:output", "match:input"]
'''
        lb = from_toml(toml_str)
        bp = to_draftsman(lb)
        assert bp.label == "From TOML"
        assert len(bp.entities) == 2
        conns = _get_connections_list(bp)
        assert len(conns) >= 1

        # Connectivity validation
        from conftest import validate_logical_connectivity
        vresult = validate_logical_connectivity(lb)
        assert vresult["errors"] == [], f"Connectivity errors: {vresult['errors']}"


def _get_connections_list(bp):
    """Helper: extract circuit connections from a draftsman Blueprint."""
    wires = getattr(bp, "wires", None)
    if wires is None:
        return []
    return list(wires)
