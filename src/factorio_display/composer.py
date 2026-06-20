"""All-in-one blueprint composer — merges display, audio player, memory banks,
timer, and progress bar into a single self-contained blueprint.

The composer handles:
- Merging sub-blueprints with id prefixes
- Layout assignment (placing sub-blueprints relative to each other)
- Port connection (wiring timer → memory → player → display)
- Power supply placement
- Caching of intermediate results
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Sequence

from .logical_blueprint import (
    Endpoint,
    LogicalBlueprint,
    LogicalEntity,
    Network,
    to_toml,
    from_toml,
    _endpoint_position,
    _chebyshev,
    _find_closest_pair,
)
from .timer import build_raw_timer, build_mod_timer, build_repeater
from .progress_bar import build_progress_bar
# Power supply is not yet implemented — see power.py docstring for redesign plan.
# from .power import add_power_to_logical, punch_display_for_power


# ── Internal helpers ───────────────────────────────────────────────────


def _connect_nets_by_color(
    lb: LogicalBlueprint,
    color: str,
    entity_contains: str,
    port: str,
    other_entity_contains: str,
    other_port: str,
) -> bool:
    """Connect two single-endpoint networks of the same *color* by
    finding endpoints whose entity ids contain the given substrings.

    Returns True if a connection was made.
    """
    # Find the network containing the first endpoint
    net_a: Network | None = None
    ep_a: Endpoint | None = None
    for net in lb.networks:
        if net.color != color:
            continue
        for ep in net.endpoints:
            if entity_contains in ep.entity_id and ep.port == port:
                net_a = net
                ep_a = ep
                break
        if net_a is not None:
            break

    # Find the network containing the second endpoint
    net_b: Network | None = None
    ep_b: Endpoint | None = None
    for net in lb.networks:
        if net.color != color:
            continue
        for ep in net.endpoints:
            if other_entity_contains in ep.entity_id and ep.port == other_port:
                net_b = net
                ep_b = ep
                break
        if net_b is not None:
            break

    if ep_a is not None and ep_b is not None:
        lb.connect(color, ep_a, ep_b)
        return True
    return False


def _connect_networks(
    lb: LogicalBlueprint,
    net_id_a: str,
    net_id_b: str,
) -> bool:
    """Connect two networks in *lb* by their network ids.
    The closest pair of endpoints is used as the bridge.
    Returns True if a connection was made."""
    net_a = next((n for n in lb.networks if n.network_id == net_id_a), None)
    net_b = next((n for n in lb.networks if n.network_id == net_id_b), None)
    if not net_a or not net_b or not net_a.endpoints or not net_b.endpoints:
        return False
    pair = _find_closest_pair(net_a.endpoints, net_b.endpoints, lb)
    if pair is None:
        return False
    lb.connect(net_a.color, pair[0], pair[1])
    return True


# ── Cache ──────────────────────────────────────────────────────────────

_CACHE_DIR = Path(".blueprint_cache")


def _cache_key(*parts: str) -> str:
    """Deterministic cache key from string parts."""
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
    return h


def _cache_path(key: str) -> Path:
    return _CACHE_DIR / f"{key}.toml"


def cache_put(lb: LogicalBlueprint, *key_parts: str) -> None:
    """Cache a logical blueprint to disk."""
    _CACHE_DIR.mkdir(exist_ok=True)
    path = _cache_path(_cache_key(*key_parts))
    path.write_text(to_toml(lb), encoding="utf-8")


def cache_get(*key_parts: str) -> LogicalBlueprint | None:
    """Retrieve a cached logical blueprint, or None if not found."""
    path = _cache_path(_cache_key(*key_parts))
    if path.exists():
        return from_toml(path.read_text(encoding="utf-8"))
    return None


# ── Layout engine ──────────────────────────────────────────────────────


def _entity_bounding_box(lb: LogicalBlueprint) -> tuple[int, int, int, int]:
    """Return (min_x, min_y, max_x, max_y) of all positioned entities in *lb*."""
    xs: list[int] = []
    ys: list[int] = []
    for ent in lb.entities.values():
        if ent.position is not None:
            xs.append(ent.position[0])
            ys.append(ent.position[1])
    if not xs:
        return 0, 0, 0, 0
    return min(xs), min(ys), max(xs), max(ys)


def _shift_positions(lb: LogicalBlueprint, dx: int, dy: int) -> None:
    """Offset all entity positions in *lb* by (dx, dy)."""
    for ent in lb.entities.values():
        if ent.position is not None:
            x, y = ent.position
            ent.position = (x + dx, y + dy)


def _assign_tile_positions(lb: LogicalBlueprint, start_x: int = 0, start_y: int = 0) -> None:
    """Assign tile positions to all entities in *lb*, relocating to (start_x, start_y).

    Entities that already have positions are shifted relative to their
    current bounding-box origin.  Entities without positions are placed
    in a vertical column starting from the shifted origin.
    """
    # Determine the current origin of positioned entities
    xs: list[int] = []
    ys: list[int] = []
    for ent in lb.entities.values():
        if ent.position is not None:
            xs.append(ent.position[0])
            ys.append(ent.position[1])

    if xs:
        origin_x, origin_y = min(xs), min(ys)
        dx = start_x - origin_x
        dy = start_y - origin_y
        for ent in lb.entities.values():
            if ent.position is not None:
                x, y = ent.position
                ent.position = (x + dx, y + dy)
    else:
        # No positioned entities — start from scratch
        origin_x, origin_y = start_x, start_y

    # Place any remaining unpositioned entities below the last positioned one
    max_y = max((ent.position[1] for ent in lb.entities.values() if ent.position is not None),
                default=start_y - 2)
    y = max_y + 2
    for ent in lb.entities.values():
        if ent.position is None:
            ent.position = (start_x, y)
            y += 2  # combinators are 2 tiles tall


def _would_create_self_loop(
    lb: LogicalBlueprint,
    ep_a: Endpoint,
    ep_b: Endpoint,
    color: str,
) -> bool:
    """Return True if merging the networks containing *ep_a* and *ep_b*
    on *color* would create a NEW self-loop — i.e. both input and output
    of the same entity ending up in the merged network, where that was
    NOT already the case in either individual network."""
    idx_a = lb._find_network(color, ep_a)
    idx_b = lb._find_network(color, ep_b)
    if idx_a is None or idx_b is None or idx_a == idx_b:
        return False

    net_a = lb.networks[idx_a]
    net_b = lb.networks[idx_b]

    # Collect entity→ports from each network individually
    def _entity_ports(net: Network) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for ep in net.endpoints:
            result.setdefault(ep.entity_id, set()).add(ep.port)
        return result

    ports_a = _entity_ports(net_a)
    ports_b = _entity_ports(net_b)

    # A self-loop already existing in one network is fine —
    # we only care about NEW self-loops created by the merge.
    for eid, ports in ports_b.items():
        if eid not in ports_a:
            continue  # entity not in net_a, no new self-loop possible
        # Entity is in both networks — check if merging creates a new
        # input+output combination that didn't exist in net_a alone
        combined = ports_a[eid] | ports
        if "input" in combined and "output" in combined:
            # Only a problem if net_a didn't already have both
            if "input" not in ports_a[eid] or "output" not in ports_a[eid]:
                return True

    return False


class Composer:
    """Builds the all-in-one blueprint from sub-blueprint components.

    Usage::

        c = Composer(output_name="My Media")
        c.set_display(display_lb)
        c.set_audio_player(player_lb)
        c.set_video_memory(video_mem_lb)
        c.set_audio_memory(audio_mem_lb)
        c.set_timer(timer_lb)
        c.set_progress_bar(progress_lb)
        c.set_power(pole_type="substation")
        result = c.compose()
    """

    def __init__(self, output_name: str = "All-in-One Media Player"):
        self.output_name = output_name
        self._display: LogicalBlueprint | None = None
        self._audio_player: LogicalBlueprint | None = None
        self._video_memory: LogicalBlueprint | None = None
        self._audio_memory: LogicalBlueprint | None = None
        self._timer: LogicalBlueprint | None = None
        self._progress_bar: LogicalBlueprint | None = None
        self._pole_type: str | None = None

    def set_display(self, lb: LogicalBlueprint) -> None:
        """Set the display (lamp grid) sub-blueprint."""
        self._display = lb

    def set_audio_player(self, lb: LogicalBlueprint) -> None:
        """Set the audio player (decoder + speakers) sub-blueprint."""
        self._audio_player = lb

    def set_video_memory(self, lb: LogicalBlueprint) -> None:
        """Set the video memory (DC pages) sub-blueprint."""
        self._video_memory = lb

    def set_audio_memory(self, lb: LogicalBlueprint) -> None:
        """Set the audio memory (DC pages) sub-blueprint."""
        self._audio_memory = lb

    def set_timer(self, lb: LogicalBlueprint) -> None:
        """Set the timer sub-blueprint (repeater + modulo)."""
        self._timer = lb

    def set_progress_bar(self, lb: LogicalBlueprint) -> None:
        """Set the progress bar sub-blueprint."""
        self._progress_bar = lb

    def set_power(self, pole_type: str) -> None:
        """Set power pole type: ``"small"``, ``"medium"``, or ``"substation"``."""
        self._pole_type = pole_type

    def compose(self) -> LogicalBlueprint:
        """Merge all sub-blueprints, assign layout, connect ports, and add power.

        Returns
        -------
        LogicalBlueprint
            The fully composed all-in-one blueprint with positions assigned.
        """
        merged = LogicalBlueprint(label=self.output_name)

        # ── 1. Merge sub-blueprints ──────────────────────────────
        # Merge in order: display first (keeps its positions),
        # then others placed relative to display bounds.

        display_bounds = (0, 0, 0, 0)

        if self._display is not None:
            merged.merge(self._display)
            display_bounds = _entity_bounding_box(merged)

        # Place timer and progress bar ABOVE the display
        timer_lb = self._timer
        progress_lb = self._progress_bar

        # Build default timer if none provided
        if timer_lb is None:
            # Default: raw timer (clock on RED self-loop) + mod timer
            # (reads RED, outputs signal-M on RED).  Different signal
            # names prevent collisions on the shared RED bus.
            #   y=0  raw CC kick   raw AC inc  (self-loop)
            #   y=4  mod AC        (reads clock, outputs %60)

            timer_lb = LogicalBlueprint(label="Timer")
            raw = build_raw_timer("Raw Clock")
            mod = build_mod_timer(60, name="Sub Tick")  # fallback when no encoder tick count  # fallback when no content

            _assign_tile_positions(mod, start_x=0, start_y=4)

            timer_lb.merge(raw)
            timer_lb.merge(mod, entity_prefix="mod_", network_prefix="mod_")

            # Wire: raw output (RED) → mod input (RED)
            _connect_nets_by_color(
                timer_lb, "red",
                entity_contains="raw_clock", port="output",
                other_entity_contains="mod_sub", other_port="input",
            )

        if progress_lb is None and self._display is not None:
            # Default progress bar: 10 lamps
            progress_lb = build_progress_bar(
                "Progress", length=10, signal_name="signal-clock", max_value=59,
            )

        min_x, min_y, max_x, max_y = display_bounds

        # ── 2. Layout: pack non-display components compactly ──────
        # All combinator/speaker/logic components (timer, progress
        # bar, memory banks, audio player) are packed in a vertical
        # column to the RIGHT of the display.  This keeps them within
        # a few tiles of each other so clock/control signals only need
        # short wires.  The display lamp grid stays at its position.
        #
        # Layout:
        #   [Display]  |  [timer block]
        #   (lamps)    |  [progress bar]
        #              |  [video memory DCs]
        #              |  [audio memory DCs]
        #              |  [audio player]

        # Collect components in logical order: timer first (clock source),
        # then progress bar, then memory banks, then audio player.
        components: list[tuple[str, LogicalBlueprint | None, str, str]] = [
            ("timer", timer_lb, "tm_", "tm_"),
            ("progress", progress_lb, "pb_", "pb_"),
            ("video_memory", self._video_memory, "vm_", "vm_"),
            ("audio_memory", self._audio_memory, "am_", "am_"),
            ("audio_player", self._audio_player, "ap_", "ap_"),
        ]

        # Start the column to the right of the display, aligned to
        # the display's top edge (so the timer is level with the first
        # row of lamps).
        col_x = max_x + 2
        next_y = min_y

        for _name, comp_lb, ent_prefix, net_prefix in components:
            if comp_lb is None:
                continue
            _assign_tile_positions(comp_lb, start_x=col_x, start_y=next_y)
            comp_bounds = _entity_bounding_box(comp_lb)
            merged.merge(comp_lb, entity_prefix=ent_prefix, network_prefix=net_prefix)
            # Next component goes below this one
            next_y = comp_bounds[3] + 2  # max_y + gap

        # ── 3. Connect ports between sub-blueprints ──────────────
        # Explicitly wire networks by their role (clock, data, sub_tick),
        # NOT by proximity.  Networks stay isolated unless explicitly
        # connected here.
        #
        # Strategy: find networks by the entities they contain, then
        # connect the right pairs.

        # Locate key networks in the merged blueprint
        subtick_net_id: str | None = None  # mod output (modded tick, signal-clock)
        dc_input_net_id: str | None = None  # DC input side
        dc_output_net_id: str | None = None  # DC output side
        lamp_net_id: str | None = None  # display lamp network
        pb_net_id: str | None = None  # progress bar lamp network

        for net in merged.networks:
            if net.color != "red" or not net.endpoints:
                continue
            for ep in net.endpoints:
                eid = ep.entity_id
                # Mod timer output: entity contains "sub" and "mod"
                if "sub" in eid and "mod" in eid and ep.port == "output":
                    subtick_net_id = net.network_id
                # DC input: entity starts with "vm_"
                if eid.startswith("vm_") and ep.port == "input":
                    dc_input_net_id = net.network_id
                # DC output: entity starts with "vm_"
                if eid.startswith("vm_") and ep.port == "output":
                    dc_output_net_id = net.network_id
                # Display lamps: entity has "lamp" or "_ent" (surrogate)
                ent = merged.entities.get(eid)
                if ent and ent.type == "small-lamp" and not eid.startswith("pb_"):
                    lamp_net_id = net.network_id
                # Progress bar lamps: entity starts with "pb_"
                if eid.startswith("pb_") and ep.port == "input":
                    pb_net_id = net.network_id

        # Connect mod output (modded tick) → DC inputs (tick gating)
        if subtick_net_id and dc_input_net_id:
            _connect_networks(merged, subtick_net_id, dc_input_net_id)

        # Connect DC outputs → display lamps (colour data)
        if dc_output_net_id and lamp_net_id:
            _connect_networks(merged, dc_output_net_id, lamp_net_id)

        # Connect mod output → progress bar (sub-tick)
        if subtick_net_id and pb_net_id:
            _connect_networks(merged, subtick_net_id, pb_net_id)

        # ── 4. Add power ─────────────────────────────────────────
        # TODO: Power supply is not yet implemented.
        # When ready, call add_power_to_logical(merged, pole_type=self._pole_type).
        # See power.py docstring for the redesign plan.
        if self._pole_type is not None:
            import sys
            sys.stderr.write(
                "Warning: --power is not yet implemented; "
                "power poles will not be added.\n"
            )

        return merged


    def _merge_nearby_networks(
        self, merged: LogicalBlueprint,
        merge_distance: int = 64,
    ) -> None:
        """Merge same-color networks whose closest entities are within
        *merge_distance* Chebyshev tiles of each other.

        Networks separated by more than *merge_distance* (e.g. vertical
        chunks, distant sub-blueprints) are kept isolated.

        When merging, the closest pair of endpoints (one from each network)
        is used as the bridge — this minimises wire length and produces
        natural port-to-port connections between sub-blueprints.
        """
        for color in ("red", "green"):
            nets = [n for n in merged.networks
                    if n.color == color and n.endpoints]
            if len(nets) < 2:
                continue

            # Repeatedly merge the closest pair of networks
            # until no pair is within merge_distance.
            changed = True
            rejected: set[tuple[str, str]] = set()  # (net_id_a, net_id_b) pairs to skip
            while changed:
                changed = False
                best_dist = merge_distance + 1
                best_pair: tuple[int, int] | None = None
                best_eps: tuple[Endpoint, Endpoint] | None = None

                # Re-fetch networks each iteration (indices shift after merge)
                nets = [n for n in merged.networks
                        if n.color == color and n.endpoints]

                for i in range(len(nets)):
                    for j in range(i + 1, len(nets)):
                        # Skip pairs that share any endpoint —
                        # those endpoints are already in the same bus.
                        eps_i = {e.entity_id + ":" + e.port for e in nets[i].endpoints}
                        eps_j = {e.entity_id + ":" + e.port for e in nets[j].endpoints}
                        if eps_i & eps_j:
                            continue

                        # Skip previously rejected (self-loop) pairs
                        pair_key = (nets[i].network_id, nets[j].network_id)
                        if pair_key in rejected:
                            continue

                        pair = _find_closest_pair(
                            nets[i].endpoints, nets[j].endpoints, merged,
                        )
                        if pair is None:
                            continue

                        # Skip if merging would create a self-loop
                        # (same entity's input + output on same network)
                        if _would_create_self_loop(
                            merged, pair[0], pair[1], color,
                        ):
                            rejected.add(pair_key)
                            continue

                        d = _chebyshev(
                            _endpoint_position(pair[0], merged),
                            _endpoint_position(pair[1], merged),
                        )
                        if d < best_dist:
                            best_dist = d
                            best_pair = (i, j)
                            best_eps = pair

                if best_pair is not None and best_eps is not None:
                    merged.connect(color, best_eps[0], best_eps[1])
                    changed = True


def compose_all_in_one(
    display_lb: LogicalBlueprint | None = None,
    audio_player_lb: LogicalBlueprint | None = None,
    video_memory_lb: LogicalBlueprint | None = None,
    audio_memory_lb: LogicalBlueprint | None = None,
    timer_lb: LogicalBlueprint | None = None,
    progress_bar_lb: LogicalBlueprint | None = None,
    pole_type: str | None = None,
    output_name: str = "All-in-One Media Player",
    use_cache: bool = True,
    cache_key_parts: Sequence[str] = (),
) -> LogicalBlueprint:
    """Compose an all-in-one blueprint from sub-blueprint components.

    This is the main entry point.  All sub-blueprints are optional;
    defaults are provided for timer and progress bar when a display
    is present.

    Parameters
    ----------
    display_lb : LogicalBlueprint | None
        Lamp grid display.
    audio_player_lb : LogicalBlueprint | None
        Audio decoder + speakers.
    video_memory_lb : LogicalBlueprint | None
        Video DC memory pages.
    audio_memory_lb : LogicalBlueprint | None
        Audio DC memory pages.
    timer_lb : LogicalBlueprint | None
        Clock timer.  Auto-generated if None.
    progress_bar_lb : LogicalBlueprint | None
        Progress bar.  Auto-generated if display_lb is provided.
    pole_type : str | None
        Power pole type: ``"small"``, ``"medium"``, ``"substation"``, or None.
    output_name : str
        Blueprint label.
    use_cache : bool
        If True, check cache before composing.
    cache_key_parts : Sequence[str]
        Additional key parts for cache identification.

    Returns
    -------
    LogicalBlueprint
    """
    if use_cache and cache_key_parts:
        cached = cache_get(output_name, *cache_key_parts)
        if cached is not None:
            return cached

    c = Composer(output_name=output_name)
    if display_lb is not None:
        c.set_display(display_lb)
    if audio_player_lb is not None:
        c.set_audio_player(audio_player_lb)
    if video_memory_lb is not None:
        c.set_video_memory(video_memory_lb)
    if audio_memory_lb is not None:
        c.set_audio_memory(audio_memory_lb)
    if timer_lb is not None:
        c.set_timer(timer_lb)
    if progress_bar_lb is not None:
        c.set_progress_bar(progress_bar_lb)
    if pole_type is not None:
        c.set_power(pole_type)

    result = c.compose()

    if use_cache and cache_key_parts:
        cache_put(result, output_name, *cache_key_parts)

    return result
