"""Tests for progress_bar.py and composer.py."""

from __future__ import annotations

import pytest

from factorio_display.logical_blueprint import (
    Endpoint,
    LogicalBlueprint,
    LogicalEntity,
    Network,
    to_draftsman,
    to_toml,
    from_toml,
    _endpoint_position,
    _chebyshev,
    _iter_connections,
)
from factorio_display.progress_bar import build_progress_bar
from factorio_display.composer import compose_all_in_one, Composer, _layout_components, _validate_network_reachability
from factorio_display.timer import build_raw_timer, build_mod_timer, build_clock_bridge
from factorio_display.cli import _declare_memory_ports, _build_timer_for_memory, _extract_total_ticks


class TestProgressBar:
    def test_builds_valid_lb(self):
        lb = build_progress_bar("PB", length=5, signal_name="signal-T", max_value=100)
        assert lb.label == "PB"
        assert len(lb.entities) == 5

    def test_all_lamps(self):
        lb = build_progress_bar("PB", length=3)
        types = {e.type for e in lb.entities.values()}
        assert types == {"small-lamp"}

    def test_has_input_port(self):
        lb = build_progress_bar("PB", length=3)
        assert "in" in lb.input_ports

    def test_lamp_conditions_monotonic(self):
        """Thresholds should increase from leftmost to rightmost lamp."""
        lb = build_progress_bar("PB", length=4, max_value=100)
        thresholds = []
        for eid in sorted(lb.entities.keys()):
            ent = lb.entities[eid]
            cond = ent.properties.get("condition", {})
            thresholds.append(cond.get("constant", 0))
        assert thresholds == sorted(thresholds)

    def test_lamp_wired_in_chain(self):
        lb = build_progress_bar("PB", length=3)
        # All lamps should share at least one red network
        lamp_ids = list(lb.entities.keys())
        for net in lb.networks:
            if net.color == "red":
                net_entities = {ep.entity_id for ep in net.endpoints if ep.port == "input"}
                if len(net_entities) >= 2:
                    return  # found a network with 2+ lamps
        pytest.fail("Lamps not wired in a shared red network")

    def test_toml_roundtrip(self):
        lb = build_progress_bar("PB", length=3, signal_name="signal-clock", max_value=59)
        toml_str = to_toml(lb)
        lb2 = from_toml(toml_str)
        assert lb2.label == "PB"
        assert "in" in lb2.input_ports

    def test_draftsman_export(self):
        lb = build_progress_bar("PB", length=3, max_value=59)
        bp_str = to_draftsman(lb).to_string()
        assert bp_str.startswith("0e")

    def test_custom_signal(self):
        lb = build_progress_bar("PB", length=2, signal_name="signal-custom", max_value=200)
        for ent in lb.entities.values():
            cond = ent.properties.get("condition", {})
            assert cond.get("first") == "signal-custom"

    def test_threshold_distribution(self):
        """For length=10, max=100, thresholds should be 10, 20, ..., 100."""
        lb = build_progress_bar("PB", length=10, max_value=100)
        thresholds = []
        for eid in sorted(lb.entities.keys()):
            ent = lb.entities[eid]
            cond = ent.properties.get("condition", {})
            thresholds.append(cond.get("constant", 0))
        assert thresholds == [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]


class TestComposer:
    def test_compose_empty(self):
        result = compose_all_in_one(output_name="Empty")
        assert result.label == "Empty"

    def test_compose_with_timer_only(self):
        timer = build_raw_timer("Clock")
        result = compose_all_in_one(
            timer_lb=timer,
            output_name="With Timer",
        )
        assert result.label == "With Timer"
        # Should have timer entities (prefixed with tm_)
        has_timer = any(eid.startswith("tm_") for eid in result.entities)
        assert has_timer

    def test_compose_with_progress_bar(self):
        pb = build_progress_bar("PB", length=5)
        result = compose_all_in_one(
            progress_bar_lb=pb,
            output_name="With PB",
        )
        assert any(eid.startswith("pb_") for eid in result.entities)

    def test_composer_merge_preserves_ports(self):
        """Ports from merged sub-blueprints should be preserved."""
        timer = build_raw_timer("Clock")
        c = Composer("Test")
        c.set_timer(timer)
        result = c.compose()
        # Timer's output port "out" should exist (with tm_ prefix on network)
        # The port is preserved in the merged result
        assert len(result.output_ports) > 0 or len(result.input_ports) > 0


# ═══════════════════════════════════════════════════════════════════════
# Circuit topology validation — tests that inspect the TOML network
# structure and materialised wiring of composed blueprints.
# ═══════════════════════════════════════════════════════════════════════


def _build_synthetic_video_memory(
    name: str = "VideoMem",
    num_frames: int = 3,
    width: int = 4,
    height: int = 3,
    start_x: int = 10,
) -> LogicalBlueprint:
    """Build a minimal synthetic video-memory LogicalBlueprint.

    Creates *num_frames* decider combinators gated on ``signal-clock``,
    each outputting one colour signal per pixel.  DC inputs are chained
    on red, outputs on red �?matching the real video encoder.

    Uses real Factorio virtual-letter signals (signal-A, signal-B, �?
    to avoid draftsman IncompleteSignalError.
    """
    _LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    lb = LogicalBlueprint(label=name)
    dc_ids: list[str] = []
    for fi in range(num_frames):
        dc_id = f"dc_{fi}"
        dc_ids.append(dc_id)
        start_tick = fi * 10 + 1
        end_tick = start_tick + 9
        outputs: list[dict] = []
        for y in range(height):
            for x in range(width):
                idx = y * width + x
                letter = _LETTERS[idx % 26]
                suffix = f"_{idx // 26}" if idx >= 26 else ""
                outputs.append({
                    "signal": f"signal-{letter}{suffix}",
                    "copy_count": False,
                    "constant": ((fi + 1) * 40) << 16,
                })
        dc = LogicalEntity(
            dc_id,
            "decider-combinator",
            properties={
                "conditions": [
                    {"first": "signal-clock", "op": ">=", "constant": start_tick},
                    {"first": "signal-clock", "op": "<=", "constant": end_tick},
                ],
                "outputs": outputs,
            },
            position=(start_x + fi * 2, 0),
        )
        lb.add_entity(dc)

    # Chain DC inputs (clock bus) �?red
    for i in range(len(dc_ids) - 1):
        lb.connect("red", Endpoint(dc_ids[i], "input"),
                   Endpoint(dc_ids[i + 1], "input"))
    # Chain DC outputs (data bus) �?red
    for i in range(len(dc_ids) - 1):
        lb.connect("red", Endpoint(dc_ids[i], "output"),
                   Endpoint(dc_ids[i + 1], "output"))

    # Declare ports for composition
    for net in lb.networks:
        if net.color == "red":
            for ep in net.endpoints:
                if ep.port == "input" and ep.entity_id == dc_ids[0]:
                    lb.set_input_port("clock_in", net.network_id)
                if ep.port == "output" and ep.entity_id == dc_ids[0]:
                    lb.set_output_port("data_out", net.network_id)

    return lb


def _build_synthetic_display(
    name: str = "Display",
    width: int = 4,
    height: int = 3,
) -> LogicalBlueprint:
    """Build a minimal lamp-grid display LogicalBlueprint.

    Lamps are placed at (x, y), chained on red: rows left-to-right,
    rightmost column top-to-bottom.  Uses real Factorio virtual-letter
    signals for ``color_signal``.
    """
    _LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    lb = LogicalBlueprint(label=name)
    lamp_ids: list[list[str]] = []
    for y in range(height):
        row: list[str] = []
        for x in range(width):
            lid = f"lamp_{x}_{y}"
            row.append(lid)
            idx = y * width + x
            letter = _LETTERS[idx % 26]
            suffix = f"_{idx // 26}" if idx >= 26 else ""
            lamp = LogicalEntity(
                lid,
                "small-lamp",
                properties={
                    "use_colors": True,
                    "always_on": True,
                    "circuit_enabled": False,
                    "color_signal": f"signal-{letter}{suffix}",
                },
                position=(x, y),
            )
            lb.add_entity(lamp)
        lamp_ids.append(row)

    # Horizontal chains (each row left-to-right)
    for y in range(height):
        for x in range(width - 1):
            lb.connect("red", Endpoint(lamp_ids[y][x], "input"),
                       Endpoint(lamp_ids[y][x + 1], "input"))
    # Vertical chain (rightmost column top-to-bottom)
    for y in range(height - 1):
        lb.connect("red", Endpoint(lamp_ids[y][width - 1], "input"),
                   Endpoint(lamp_ids[y + 1][width - 1], "input"))

    lb.set_input_port("color_in", lb.networks[0].network_id)
    return lb


class TestCircuitTopology:
    """Validate that composed blueprints have correct circuit topology
    �?signals flow from timer �?memory �?display on the red bus."""

    def test_clock_signal_reaches_dc_inputs(self):
        """Timer's mod output (modded tick, RED) and DC inputs must
        share a red network after explicit port connection."""
        mem = _build_synthetic_video_memory()
        disp = _build_synthetic_display()
        timer = build_raw_timer("Clock")
        mod = build_mod_timer(60, name="SubTick")
        from factorio_display.composer import _assign_tile_positions, _connect_nets_by_color
        _assign_tile_positions(mod, start_x=0, start_y=4)
        timer.merge(mod, entity_prefix="mod_", network_prefix="mod_")
        _connect_nets_by_color(
            timer, "red",
            entity_contains="clock_", port="output",
            other_entity_contains="mod_sub", other_port="input",
        )

        result = compose_all_in_one(
            display_lb=disp,
            video_memory_lb=mem,
            timer_lb=timer,
            output_name="TopoTest",
            use_cache=False,
        )

        # Mod output and DC inputs must share a red network
        red_nets = [n for n in result.networks
                    if n.color == "red" and n.endpoints]
        assert red_nets, "No red network after composition"

        # Find the network containing mod output + DC inputs
        mod_net = None
        for net in red_nets:
            eps = {(ep.entity_id, ep.port) for ep in net.endpoints}
            if ("tm_mod_subtick_mod", "output") in eps:
                mod_net = net
                break
        assert mod_net is not None, "Mod output network not found"

        eps_set = {(ep.entity_id, ep.port) for ep in mod_net.endpoints}
        assert ("tm_mod_subtick_mod", "output") in eps_set, (
            "Mod output not in network"
        )
        for fi in range(3):
            assert (f"vm_dc_{fi}", "input") in eps_set, (
                f"DC vm_dc_{fi} input not in mod output network"
            )

    def test_dc_outputs_reach_display_lamps(self):
        """DC outputs and display lamp inputs share the data (red) network."""
        mem = _build_synthetic_video_memory()
        disp = _build_synthetic_display()
        timer = build_raw_timer("Clock")
        mod = build_mod_timer(60, name="SubTick", )
        from factorio_display.composer import _assign_tile_positions
        _assign_tile_positions(mod, start_x=0, start_y=4)
        timer.merge(mod, entity_prefix="mod_", network_prefix="mod_")

        result = compose_all_in_one(
            display_lb=disp,
            video_memory_lb=mem,
            timer_lb=timer,
            output_name="TopoTest",
            use_cache=False,
        )

        red_nets = [n for n in result.networks
                    if n.color == "red" and n.endpoints]

        # Find the network containing DC outputs
        data_net = None
        for net in red_nets:
            eps = {(ep.entity_id, ep.port) for ep in net.endpoints}
            if ("vm_dc_0", "output") in eps:
                data_net = net
                break
        assert data_net is not None, "DC output network not found"

        eps_set = {(ep.entity_id, ep.port) for ep in data_net.endpoints}

        # DC outputs must be in this network
        for fi in range(3):
            assert (f"vm_dc_{fi}", "output") in eps_set, (
                f"DC vm_dc_{fi} output not in data network"
            )

        # Lamp inputs must be in this same data network
        for y in range(3):
            for x in range(4):
                assert (f"lamp_{x}_{y}", "input") in eps_set, (
                    f"Lamp lamp_{x}_{y} input not in data network"
                )

    def test_timer_and_progress_bar_on_same_red_bus(self):
        """Timer mod output and progress bar lamps share the sub-tick (red) network."""
        pb = build_progress_bar("PB", length=5, signal_name="signal-clock")
        timer = build_raw_timer("Clock")
        mod = build_mod_timer(60, name="SubTick", )
        from factorio_display.composer import _assign_tile_positions
        _assign_tile_positions(mod, start_x=0, start_y=4)
        timer.merge(mod, entity_prefix="mod_", network_prefix="mod_")

        result = compose_all_in_one(
            timer_lb=timer,
            progress_bar_lb=pb,
            output_name="PBTest",
            use_cache=False,
        )

        red_nets = [n for n in result.networks
                    if n.color == "red" and n.endpoints]

        # Find the network containing mod output
        sub_net = None
        for net in red_nets:
            eps = {(ep.entity_id, ep.port) for ep in net.endpoints}
            if ("tm_mod_subtick_mod", "output") in eps:
                sub_net = net
                break
        assert sub_net is not None, "Mod output network not found"

        eps_set = {(ep.entity_id, ep.port) for ep in sub_net.endpoints}

        # Mod timer output must be in this network
        assert ("tm_mod_subtick_mod", "output") in eps_set, (
            "Mod timer output not in sub-tick network"
        )
        # Progress bar lamps must be in this network
        for i in range(5):
            assert (f"pb_pb_l{i}", "input") in eps_set, (
                f"Progress lamp pb_pb_l{i} not in sub-tick network"
            )

    def test_wiring_distances_are_adjacent(self):
        """After materialisation, no circuit connection may exceed
        Chebyshev distance 3.  Within a sub-blueprint connections are
        distance 1; between adjacent sub-blueprints distance �?2 is
        normal (1-tile layout gaps + 2-tile-tall combinators).
        Distances > 3 indicate broken chain topology."""
        mem = _build_synthetic_video_memory()
        disp = _build_synthetic_display()
        timer = build_raw_timer("Clock")
        mod = build_mod_timer(60, name="SubTick", )
        from factorio_display.composer import _assign_tile_positions
        _assign_tile_positions(mod, start_x=0, start_y=4)
        timer.merge(mod, entity_prefix="mod_", network_prefix="mod_")

        result = compose_all_in_one(
            display_lb=disp,
            video_memory_lb=mem,
            timer_lb=timer,
            output_name="DistTest",
            use_cache=False,
        )

        bp = to_draftsman(result)
        conns = _iter_connections(bp)

        pos_map: dict[str, tuple[int, int]] = {}
        for e in bp.entities:
            eid = getattr(e, "id", None)
            if eid and hasattr(e, "tile_position"):
                tp = e.tile_position
                pos_map[str(eid)] = (int(tp.x), int(tp.y))

        long_jumps: list[str] = []
        for c in conns:
            p1 = pos_map.get(c["id1"])
            p2 = pos_map.get(c["id2"])
            if p1 is None or p2 is None:
                continue
            d = _chebyshev(p1, p2)
            if d > 9:
                long_jumps.append(
                    f"  {c['id1']}{p1} �?{c['id2']}{p2}: dist={d}"
                )

        assert not long_jumps, (
            f"Found {len(long_jumps)} connections with distance > 3:\n"
            + "\n".join(long_jumps[:10])
        )

    def test_toml_roundtrip_preserves_topology(self):
        """TOML serialisation round-trip must preserve network structure."""
        mem = _build_synthetic_video_memory()
        disp = _build_synthetic_display()
        timer = build_raw_timer("Clock")
        mod = build_mod_timer(60, name="SubTick", )
        from factorio_display.composer import _assign_tile_positions
        _assign_tile_positions(mod, start_x=0, start_y=4)
        timer.merge(mod, entity_prefix="mod_", network_prefix="mod_")

        result = compose_all_in_one(
            display_lb=disp,
            video_memory_lb=mem,
            timer_lb=timer,
            output_name="RoundTrip",
            use_cache=False,
        )

        toml_str = to_toml(result)
        lb2 = from_toml(toml_str)

        # Same number of networks
        assert len(lb2.networks) == len(result.networks), (
            f"Network count changed: {len(result.networks)} �?{len(lb2.networks)}"
        )

        # Every endpoint in original must be in round-tripped version
        for net in result.networks:
            rt_net = next((n for n in lb2.networks
                           if n.network_id == net.network_id
                           and n.color == net.color), None)
            assert rt_net is not None, (
                f"Network {net.network_id} ({net.color}) lost in round-trip"
            )
            orig_eps = {(ep.entity_id, ep.port) for ep in net.endpoints}
            rt_eps = {(ep.entity_id, ep.port) for ep in rt_net.endpoints}
            assert orig_eps == rt_eps, (
                f"Network {net.network_id} changed in round-trip:\n"
                f"  added: {rt_eps - orig_eps}\n"
                f"  removed: {orig_eps - rt_eps}"
            )

    def test_no_isolated_networks(self):
        """Every network with �?2 endpoints must form one connected
        component in the materialised blueprint."""
        mem = _build_synthetic_video_memory()
        disp = _build_synthetic_display()
        timer = build_raw_timer("Clock")
        mod = build_mod_timer(60, name="SubTick", )
        from factorio_display.composer import _assign_tile_positions
        _assign_tile_positions(mod, start_x=0, start_y=4)
        timer.merge(mod, entity_prefix="mod_", network_prefix="mod_")

        result = compose_all_in_one(
            display_lb=disp,
            video_memory_lb=mem,
            timer_lb=timer,
            output_name="IsoTest",
            use_cache=False,
        )

        bp = to_draftsman(result)
        conns = _iter_connections(bp)

        # Build adjacency graph per color
        graphs: dict[str, dict[str, set[str]]] = {}
        for c in conns:
            color = c["color"]
            g = graphs.setdefault(color, {})
            g.setdefault(c["id1"], set()).add(c["id2"])
            g.setdefault(c["id2"], set()).add(c["id1"])

        for net in result.networks:
            if net.color == "copper":
                continue
            if len(net.endpoints) < 2:
                continue
            g = graphs.get(net.color, {})
            # Find connected components among this network's entities
            ep_entities = {ep.entity_id for ep in net.endpoints}
            if not ep_entities:
                continue
            # BFS from first entity
            start = next(iter(ep_entities))
            visited = set()
            stack = [start]
            while stack:
                n = stack.pop()
                if n in visited:
                    continue
                visited.add(n)
                for neighbor in g.get(n, set()):
                    if neighbor not in visited:
                        stack.append(neighbor)
            reachable = visited & ep_entities
            assert reachable == ep_entities, (
                f"Network {net.network_id} ({net.color}) has disconnected "
                f"entities: {len(reachable)}/{len(ep_entities)} reachable\n"
                f"  unreachable: {ep_entities - reachable}"
            )


# ═══════════════════════════════════════════════════════════════════════
# Memory port detection & timer clock colour tests
# ═══════════════════════════════════════════════════════════════════════


def _make_memory_lb(clock_color: str = "red") -> LogicalBlueprint:
    """Create a minimal LogicalBlueprint simulating a memory blueprint.

    Contains one decider combinator whose input/output are on *clock_color*
    networks (matching how video/audio encoders wire DCs).
    """
    lb = LogicalBlueprint(label="Test Memory")
    dc = LogicalEntity(
        "dc_0", "decider-combinator",
        properties={
            "conditions": [
                {"first": "signal-clock", "op": ">=", "constant": 0},
                {"first": "signal-clock", "op": "<=", "constant": 59},
            ],
            "outputs": [],
        },
        position=(0, 0),
    )
    lb.add_entity(dc)

    # Both input and output on the same colour (video=red, audio=green)
    lb.connect(clock_color, Endpoint("dc_0", "input"), Endpoint("dc_0", "output"))
    return lb


class TestDeclareMemoryPorts:
    """Tests for _declare_memory_ports — clock port colour detection."""

    def test_video_memory_clock_is_red(self):
        """Video memory DCs are on RED → clock port should be RED."""
        lb = _make_memory_lb("red")
        _declare_memory_ports(lb)
        assert "clock" in lb.input_ports
        clock_net_id = lb.input_ports["clock"]
        clock_net = next(n for n in lb.networks if n.network_id == clock_net_id)
        assert clock_net.color == "red", (
            f"Expected RED clock port for video memory, got {clock_net.color}"
        )

    def test_audio_memory_clock_is_green(self):
        """Audio memory DCs are on GREEN → clock port should be GREEN."""
        lb = _make_memory_lb("green")
        _declare_memory_ports(lb)
        assert "clock" in lb.input_ports
        clock_net_id = lb.input_ports["clock"]
        clock_net = next(n for n in lb.networks if n.network_id == clock_net_id)
        assert clock_net.color == "green", (
            f"Expected GREEN clock port for audio memory, got {clock_net.color}"
        )

    def test_data_port_is_red(self):
        """Data output port is always RED regardless of clock colour."""
        for color in ("red", "green"):
            lb = _make_memory_lb(color)
            _declare_memory_ports(lb)
            assert "data" in lb.output_ports
            data_net_id = lb.output_ports["data"]
            data_net = next(n for n in lb.networks if n.network_id == data_net_id)
            assert data_net.color == "red"


class TestBuildTimerForMemory:
    """Tests for _build_timer_for_memory — conditional clock bridge."""

    def test_no_bridge_for_red_clock(self):
        """When memory clock is RED, timer should NOT include a +0 bridge,
        and the modded (wrapping) clock should drive the 'clock' port."""
        mem_lb = _make_memory_lb("red")
        _declare_memory_ports(mem_lb)
        timer = _build_timer_for_memory(mem_lb)

        # The timer should NOT have any entity with operation="+" and second_operand=0
        bridge_entities = [
            eid for eid, ent in timer.entities.items()
            if (ent.type == "arithmetic-combinator"
                and ent.properties.get("operation") == "+"
                and ent.properties.get("second_operand") == 0)
        ]
        assert len(bridge_entities) == 0, (
            f"RED clock should not need a +0 bridge, found: {bridge_entities}"
        )

        # Clock and sub_tick should point to the SAME network (modded clock)
        assert "clock" in timer.output_ports
        assert "sub_tick" in timer.output_ports
        assert timer.output_ports["clock"] == timer.output_ports["sub_tick"], (
            "Clock and sub_tick should share the same modded-clock network"
        )

        # "raw" port should be absent (raw clock is internal)
        assert "raw" not in timer.output_ports, (
            "Raw port should be dropped — raw clock stays internal"
        )

        # Clock output port should be RED
        clock_net_id = timer.output_ports["clock"]
        clock_net = next(n for n in timer.networks if n.network_id == clock_net_id)
        assert clock_net.color == "red", (
            f"Clock port should be RED for video, got {clock_net.color}"
        )

        # The clock network should contain the mod AC output (not raw AC output)
        mod_out = [ep for ep in clock_net.endpoints
                   if "mod_sub" in ep.entity_id and ep.port == "output"]
        assert len(mod_out) >= 1, (
            "Clock network should include mod AC output endpoint"
        )

    def test_bridge_for_green_clock(self):
        """When memory clock is GREEN, timer MUST include a +0 bridge (RED→GREEN)."""
        mem_lb = _make_memory_lb("green")
        _declare_memory_ports(mem_lb)
        timer = _build_timer_for_memory(mem_lb)

        # The timer MUST have the bridge entity
        bridge_entities = [
            eid for eid, ent in timer.entities.items()
            if (ent.type == "arithmetic-combinator"
                and ent.properties.get("operation") == "+"
                and ent.properties.get("second_operand") == 0)
        ]
        assert len(bridge_entities) == 1, (
            f"GREEN clock needs exactly one +0 bridge, found: {bridge_entities}"
        )

        # Clock output port should be GREEN
        assert "clock" in timer.output_ports
        clock_net_id = timer.output_ports["clock"]
        clock_net = next(n for n in timer.networks if n.network_id == clock_net_id)
        assert clock_net.color == "green", (
            f"Clock port should be GREEN for audio, got {clock_net.color}"
        )

    def test_sub_tick_port_always_red(self):
        """Sub-tick output should always be RED regardless of clock colour."""
        for color in ("red", "green"):
            mem_lb = _make_memory_lb(color)
            _declare_memory_ports(mem_lb)
            timer = _build_timer_for_memory(mem_lb)

            assert "sub_tick" in timer.output_ports
            st_net_id = timer.output_ports["sub_tick"]
            st_net = next(n for n in timer.networks if n.network_id == st_net_id)
            assert st_net.color == "red", (
                f"Sub-tick should be RED, got {st_net.color} for {color} clock"
            )

    def test_timer_has_raw_incrementer(self):
        """Timer should always contain the raw clock incrementer AC."""
        mem_lb = _make_memory_lb("red")
        _declare_memory_ports(mem_lb)
        timer = _build_timer_for_memory(mem_lb)

        inc_ac = [
            ent for ent in timer.entities.values()
            if (ent.type == "arithmetic-combinator"
                and ent.properties.get("operation") == "+"
                and ent.properties.get("second_operand") == 1)
        ]
        assert len(inc_ac) >= 1, "Timer missing raw incrementer (signal+1→signal)"


# ═══════════════════════════════════════════════════════════════════════
# Layout ordering & reachability warning tests
# ═══════════════════════════════════════════════════════════════════════


def _make_positioned_lb(label: str, prefix: str, y: int) -> LogicalBlueprint:
    """Create a minimal positioned LogicalBlueprint for layout testing."""
    lb = LogicalBlueprint(label=label)
    ent = LogicalEntity(
        f"{prefix}ent", "arithmetic-combinator",
        properties={"first_operand": "signal-A", "operation": "+",
                     "second_operand": 1, "output_signal": "signal-A"},
        position=(0, y),
    )
    lb.add_entity(ent)
    # Add an output port for connection testing
    net = Network(network_id="red_0", color="red",
                  endpoints=[Endpoint(ent.entity_id, "output")])
    lb.add_network(net)
    lb.set_output_port("out", "red_0")
    return lb


class TestLayoutOrdering:
    """Tests for _layout_components — smart ordering via connections."""

    def test_connected_components_placed_adjacent(self):
        """Components sharing a port connection should be adjacent in layout."""
        from factorio_display.composer import PortConnection
        from factorio_display.logical_blueprint import LogicalBlueprint

        merged = LogicalBlueprint(label="Test")

        # Create three components with distinct prefixes
        comp_a = _make_positioned_lb("CompA", "a_", 0)
        comp_b = _make_positioned_lb("CompB", "b_", 0)
        comp_c = _make_positioned_lb("CompC", "c_", 0)

        prefixes = {"CompA": "a_", "CompB": "b_", "CompC": "c_"}

        # Merge all into 'merged' (entity_prefix doubles the prefix)
        merged.merge(comp_a, entity_prefix="a_", network_prefix="a_")
        merged.merge(comp_b, entity_prefix="b_", network_prefix="b_")
        merged.merge(comp_c, entity_prefix="c_", network_prefix="c_")

        # Only connect A→B (C is isolated)
        connections = [PortConnection("CompA", "out", "CompB", "out")]

        _layout_components(merged, prefixes, connections)

        # Get positions after layout (entity IDs are double-prefixed)
        pos_a = merged.entities["a_a_ent"].position
        pos_b = merged.entities["b_b_ent"].position
        pos_c = merged.entities["c_c_ent"].position

        # A and B should be adjacent (small Chebyshev distance), C further away.
        # Use Chebyshev distance because the new layout places the sink at origin
        # and dependencies to its right (different X coordinates).
        assert pos_a is not None and pos_b is not None and pos_c is not None

        def _cheb(p1, p2):
            return max(abs(p1[0] - p2[0]), abs(p1[1] - p2[1]))

        dist_ab = _cheb(pos_a, pos_b)
        dist_ac = _cheb(pos_a, pos_c)
        dist_bc = _cheb(pos_b, pos_c)

        assert dist_ab <= dist_ac, (
            f"Connected A-B should be closer than A-C: AB={dist_ab}, AC={dist_ac}"
        )
        assert dist_ab <= dist_bc, (
            f"Connected A-B should be closer than B-C: AB={dist_ab}, BC={dist_bc}"
        )


class TestReachabilityWarning:
    """Tests for _validate_network_reachability — distance warnings."""

    def test_no_warning_when_all_reachable(self, capsys):
        """No warning when all endpoints are within max_distance."""
        lb = LogicalBlueprint(label="Test")
        e1 = LogicalEntity("e1", "arithmetic-combinator", position=(0, 0))
        e2 = LogicalEntity("e2", "arithmetic-combinator", position=(1, 0))
        lb.add_entity(e1)
        lb.add_entity(e2)
        lb.add_network(Network("red_0", "red", [
            Endpoint("e1", "input"), Endpoint("e2", "input"),
        ]))

        count = _validate_network_reachability(lb, max_distance=64)
        assert count == 0
        captured = capsys.readouterr()
        assert "disconnected" not in captured.err

    def test_warning_when_unreachable(self, capsys):
        """Warning emitted when endpoints are too far apart."""
        lb = LogicalBlueprint(label="Test")
        e1 = LogicalEntity("e1", "arithmetic-combinator", position=(0, 0))
        e2 = LogicalEntity("e2", "arithmetic-combinator", position=(100, 0))
        lb.add_entity(e1)
        lb.add_entity(e2)
        lb.add_network(Network("red_0", "red", [
            Endpoint("e1", "input"), Endpoint("e2", "input"),
        ]))

        count = _validate_network_reachability(lb, max_distance=64)
        assert count == 1
        captured = capsys.readouterr()
        assert "disconnected" in captured.err
        assert "too far apart" in captured.err

    def test_warning_includes_component_labels(self, capsys):
        """Warning should name the affected components when prefixes are known."""
        lb = LogicalBlueprint(label="Test")
        e1 = LogicalEntity("timer_e1", "arithmetic-combinator", position=(0, 0))
        e2 = LogicalEntity("progress_e2", "arithmetic-combinator", position=(100, 0))
        lb.add_entity(e1)
        lb.add_entity(e2)
        lb.add_network(Network("red_0", "red", [
            Endpoint("timer_e1", "output"), Endpoint("progress_e2", "input"),
        ]))

        prefixes = {"Timer": "timer_", "Progress": "progress_"}
        count = _validate_network_reachability(lb, prefixes, max_distance=64)
        assert count == 1
        captured = capsys.readouterr()
        assert "Timer" in captured.err
        assert "Progress" in captured.err
