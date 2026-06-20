"""Tests for timer.py â?raw, mod, and repeater logical modules."""



from __future__ import annotations



import pytest



from factorio_display.logical_blueprint import (

    LogicalBlueprint,

    LogicalEntity,

    to_draftsman,

    to_toml,

    from_toml,

)

from factorio_display.timer import build_raw_timer, build_mod_timer, build_repeater





class TestRawTimer:

    def test_builds_valid_lb(self):

        lb = build_raw_timer("Test Timer", output_signal="signal-T")

        assert lb.label == "Test Timer"

        assert len(lb.entities) == 2  # kick CC + inc AC

        assert len(lb.networks) >= 1



    def test_has_output_port(self):

        lb = build_raw_timer("T")

        assert "out" in lb.output_ports



    def test_entities_are_combinators(self):

        lb = build_raw_timer("T")

        types = {e.type for e in lb.entities.values()}

        assert types == {"arithmetic-combinator", "constant-combinator"}



    def test_self_loop_wiring(self):

        """The incrementer AC should have its output wired back to input."""

        lb = build_raw_timer("T")

        # Find the incrementer AC

        inc_ac = next(e for e in lb.entities.values() if e.type == "arithmetic-combinator")

        ep_input = lb.endpoints_of(inc_ac.entity_id, "input")

        ep_output = lb.endpoints_of(inc_ac.entity_id, "output")

        # Both input and output should share at least one network (the self-loop)

        common = ep_input & ep_output

        assert len(common) >= 1, "AC output not wired back to input"



    def test_toml_roundtrip(self):

        lb = build_raw_timer("T")

        toml_str = to_toml(lb)

        lb2 = from_toml(toml_str)

        assert lb2.label == "T"

        assert "out" in lb2.output_ports



    def test_draftsman_export(self):

        lb = build_raw_timer("T")

        bp_str = to_draftsman(lb).to_string()

        assert bp_str.startswith("0e")



    def test_custom_signal(self):

        lb = build_raw_timer("T", output_signal="signal-clock")

        cc = next(e for e in lb.entities.values() if e.type == "constant-combinator")

        assert cc.properties["signals"][0]["name"] == "signal-clock"

        ac = next(e for e in lb.entities.values() if e.type == "arithmetic-combinator")

        assert ac.properties["output_signal"] == "signal-clock"





class TestModTimer:

    def test_builds_valid_lb(self):

        lb = build_mod_timer(60, name="Mod", input_signal="signal-T")

        assert len(lb.entities) == 1

        assert len(lb.networks) == 2



    def test_has_input_and_output_ports(self):

        lb = build_mod_timer(60, name="Mod")

        assert "in" in lb.input_ports

        assert "out" in lb.output_ports



    def test_operation_is_modulo(self):

        lb = build_mod_timer(42, name="Mod")

        ac = next(e for e in lb.entities.values())

        assert ac.properties["operation"] == "%"

        assert ac.properties["second_operand"] == 42



    def test_toml_roundtrip(self):

        lb = build_mod_timer(30, name="Mod")

        toml_str = to_toml(lb)

        lb2 = from_toml(toml_str)

        assert lb2.label == "Mod"

        assert "in" in lb2.input_ports

        assert "out" in lb2.output_ports



    def test_draftsman_export(self):

        lb = build_mod_timer(60, name="Mod")

        bp_str = to_draftsman(lb).to_string()

        assert bp_str.startswith("0e")





class TestRepeater:

    def test_builds_basic_repeater(self):

        lb = build_repeater("Rep", constant=1024, output_signal="signal-R")

        assert lb.label == "Rep"

        # kick CC + ramp AC

        assert len(lb.entities) == 2



    def test_has_output_port(self):

        lb = build_repeater("Rep")

        assert "out" in lb.output_ports



    def test_builds_repeater_with_mod(self):

        lb = build_repeater("Rep", constant=1024, mod=6000)

        # kick CC + ramp AC + mod AC

        assert len(lb.entities) == 3



    def test_constant_correct(self):

        lb = build_repeater("Rep", constant=2048)

        ramp_ac = next(

            e for e in lb.entities.values()

            if e.type == "arithmetic-combinator" and e.properties.get("operation") == "+"

        )

        assert ramp_ac.properties["second_operand"] == 2048



    def test_toml_roundtrip(self):

        lb = build_repeater("Rep", constant=1024, mod=6000)

        toml_str = to_toml(lb)

        lb2 = from_toml(toml_str)

        assert lb2.label == "Rep"

        assert "out" in lb2.output_ports



    def test_draftsman_export(self):

        lb = build_repeater("Rep")

        bp_str = to_draftsman(lb).to_string()

        assert bp_str.startswith("0e")

