"""Logical Blueprint — intermediate representation between raw data
and the final draftsman :class:`~draftsman.blueprintable.Blueprint`.

The logical blueprint models *what* combinators and speakers exist and
*which circuit networks they join*, without committing to physical
positions or explicit pairwise wiring.

Key concepts
------------
**Network**: A named virtual circuit network (red, green, or power).
Entities join a network by their *endpoints* (e.g. ``"ap0:output"``).
When two endpoints are connected, their networks are merged (union-find
semantics).  Red, green, and power networks are always kept separate.

**Entity**: A combinator or speaker with an id, type, and type-specific
properties.  Position and direction are optional — they can be filled in
later by a layout pass.

**Serialization**: LLM-friendly TOML.  Arrays of tables for conditions,
outputs and CC signals.  Endpoints are ``"entity_id:port"`` strings.

Draftsman bridge
----------------
:meth:`LogicalBlueprint.from_draftsman` parses a draftsman ``Blueprint``
into this format.  :meth:`LogicalBlueprint.to_draftsman` does the reverse,
materialising networks into explicit ``add_circuit_connection`` calls.

Usage in the encoding pipeline
------------------------------
1. Generate a ``LogicalBlueprint`` (entities + networks, no positions).
2. Pass it through a layout engine that assigns tile positions and
   expands each ``[[network]]`` into short pairwise wires.
3. Convert to a draftsman ``Blueprint`` for final export.
"""

from __future__ import annotations

import itertools
import tomllib
from dataclasses import dataclass, field, replace
from typing import Any

# ═══════════════════════════════════════════════════════════════════════
# Data model
# ═══════════════════════════════════════════════════════════════════════

_VALID_COLORS = {"red", "green", "copper"}
_VALID_PORTS = {"input", "output"}
_VALID_ENTITY_TYPES = {
    "arithmetic-combinator",
    "decider-combinator",
    "constant-combinator",
    "programmable-speaker",
    "small-lamp",
    "small-electric-pole",
    "medium-electric-pole",
    "substation",
}


@dataclass
class Endpoint:
    """A specific connection point on a logical entity.

    Attributes
    ----------
    entity_id : str
        The entity this endpoint belongs to.
    port : str
        ``"input"`` or ``"output"``.  For combinators the *input* side is
        where operands / conditions are read; the *output* side is where
        results are emitted.
    """

    entity_id: str
    port: str

    def __post_init__(self) -> None:
        if self.port not in _VALID_PORTS:
            raise ValueError(f"Invalid port {self.port!r}, must be one of {_VALID_PORTS}")

    def __hash__(self) -> int:
        return hash((self.entity_id, self.port))

    def to_string(self) -> str:
        """``"entity_id:port"`` compact form used in TOML."""
        return f"{self.entity_id}:{self.port}"

    @classmethod
    def from_string(cls, s: str) -> Endpoint:
        """Parse ``"entity_id:port"`` back to an Endpoint."""
        parts = s.rsplit(":", 1)
        if len(parts) != 2 or parts[1] not in _VALID_PORTS:
            raise ValueError(f"Invalid endpoint string {s!r}, expected 'entity_id:port'")
        return cls(entity_id=parts[0], port=parts[1])


@dataclass
class Network:
    """A virtual circuit network of a single colour.

    Attributes
    ----------
    network_id : str
        Unique identifier for this network within the logical blueprint.
    color : str
        ``"red"`` or ``"green"``.
    endpoints : set[Endpoint]
        All endpoints that participate in this network.
        Uses a set to automatically deduplicate by (entity_id, port).
    prewired_pairs : list[tuple[Endpoint, Endpoint]] | None
        Pre-computed pairwise wire connections.  When set, ``to_draftsman``
        uses these directly instead of running the materialisation algorithm.
        Used for grid-structured networks (e.g. lamp displays) where the
        wiring pattern is known at build time.
    """

    network_id: str
    color: str
    endpoints: set[Endpoint] = field(default_factory=set)
    prewired_pairs: list[tuple[Endpoint, Endpoint]] | None = None

    def __post_init__(self) -> None:
        if self.color not in _VALID_COLORS:
            raise ValueError(f"Invalid color {self.color!r}, must be one of {_VALID_COLORS}")
        # Auto-convert list endpoints to set (backward-compat with callers
        # that pass lists and with TOML deserialization via _parse_network).
        if isinstance(self.endpoints, list):
            object.__setattr__(self, "endpoints", set(self.endpoints))


@dataclass
class LogicalEntity:
    """A single logical entity (combinator or speaker).

    Attributes
    ----------
    entity_id : str
        Unique id within the logical blueprint.
    type : str
        One of ``"arithmetic-combinator"``, ``"decider-combinator"``,
        ``"constant-combinator"``, ``"programmable-speaker"``.
    properties : dict
        Type-specific configuration (see TOML schema below).
    position : (int, int) | None
        Optional tile position.  Filled in by the layout pass.
    direction : int | None
        Optional direction (0=North, 2=South, etc.).  Filled in by layout.
    """

    entity_id: str
    type: str
    properties: dict[str, Any] = field(default_factory=dict)
    position: tuple[int, int] | None = None
    direction: int | None = None

    def __post_init__(self) -> None:
        if self.type not in _VALID_ENTITY_TYPES:
            raise ValueError(
                f"Invalid entity type {self.type!r}, must be one of {_VALID_ENTITY_TYPES}"
            )


@dataclass
class LogicalBlueprint:
    """Top-level logical blueprint.

    Attributes
    ----------
    label : str
        Human-readable label (maps to ``blueprint.label``).
    entities : dict[str, LogicalEntity]
        All entities keyed by id.  Order is preserved (Python 3.7+).
    networks : list[Network]
        All circuit networks.
    input_ports : dict[str, str]
        Named input ports, each mapping a port name to a ``network_id``.
    output_ports : dict[str, str]
        Named output ports, each mapping a port name to a ``network_id``.
    """

    label: str = ""
    entities: dict[str, LogicalEntity] = field(default_factory=dict)
    networks: list[Network] = field(default_factory=list)
    input_ports: dict[str, str] = field(default_factory=dict)
    output_ports: dict[str, str] = field(default_factory=dict)
    # ── cached derived data ──────────────────────────────────────
    _network_bounds_cache: dict[str, tuple[int, int, int, int]] = field(
        default_factory=dict, repr=False, compare=False,
    )

    # ── entity helpers ──────────────────────────────────────────────

    def add_entity(self, entity: LogicalEntity) -> None:
        """Add an entity.  Raises if *entity_id* already exists."""
        if entity.entity_id in self.entities:
            raise ValueError(f"Duplicate entity_id {entity.entity_id!r}")
        self.entities[entity.entity_id] = entity

    def get_entity(self, entity_id: str) -> LogicalEntity:
        """Look up an entity by id."""
        return self.entities[entity_id]

    # ── network helpers ─────────────────────────────────────────────

    def add_network(self, network: Network) -> None:
        """Add a network.  Raises if *network_id* already exists."""
        if any(n.network_id == network.network_id for n in self.networks):
            raise ValueError(f"Duplicate network_id {network.network_id!r}")
        self.networks.append(network)

    def _find_network(self, color: str, endpoint: Endpoint) -> int | None:
        """Return the index of the network that contains *endpoint* on *color*, or None."""
        for i, net in enumerate(self.networks):
            if net.color == color and endpoint in net.endpoints:
                return i
        return None

    def connect(self, color: str, ep_a: Endpoint, ep_b: Endpoint) -> Network:
        """Connect two endpoints on the given colour, merging networks if needed.

        Returns the (possibly new) Network that now contains both endpoints.
        """
        if color not in _VALID_COLORS:
            raise ValueError(f"Invalid color {color!r}")

        idx_a = self._find_network(color, ep_a)
        idx_b = self._find_network(color, ep_b)
        result: Network

        if idx_a is not None and idx_b is not None:
            if idx_a == idx_b:
                # Already in the same network
                result = self.networks[idx_a]
            else:
                # Merge network B into network A
                net_a = self.networks[idx_a]
                net_b = self.networks.pop(idx_b)
                # Snapshot original endpoint sets before union so internal
                # rewiring is computed per-subnetwork, not on the combined set.
                endpoints_a = set(net_a.endpoints)
                endpoints_b = set(net_b.endpoints)

                def _valid_pairs(
                    endpoints: set[Endpoint],
                    pairs: list[tuple[Endpoint, Endpoint]] | None,
                ) -> bool:
                    if pairs is None:
                        return False
                    degree: dict[Endpoint, int] = {}
                    for pa, pb in pairs:
                        if pa == pb:
                            return False
                        if pa not in endpoints or pb not in endpoints:
                            return False
                        degree[pa] = degree.get(pa, 0) + 1
                        degree[pb] = degree.get(pb, 0) + 1
                        if degree[pa] > 2 or degree[pb] > 2:
                            return False
                    return True

                def _build_internal_pairs(
                    endpoints: set[Endpoint],
                    prewired_pairs: list[tuple[Endpoint, Endpoint]] | None,
                ) -> list[tuple[Endpoint, Endpoint]]:
                    # Keep existing prewiring when valid. If missing or invalid,
                    # rebuild deterministically from endpoint positions.
                    if _valid_pairs(endpoints, prewired_pairs):
                        return list(prewired_pairs or [])
                    return _wire_horizontal_first(list(endpoints), self)

                pairs_a = _build_internal_pairs(endpoints_a, net_a.prewired_pairs)
                pairs_b = _build_internal_pairs(endpoints_b, net_b.prewired_pairs)

                degree_a: dict[Endpoint, int] = {}
                degree_b: dict[Endpoint, int] = {}
                for pa, pb in pairs_a:
                    degree_a[pa] = degree_a.get(pa, 0) + 1
                    degree_a[pb] = degree_a.get(pb, 0) + 1
                for pa, pb in pairs_b:
                    degree_b[pa] = degree_b.get(pa, 0) + 1
                    degree_b[pb] = degree_b.get(pb, 0) + 1

                def _pick_bridge_endpoint(
                    endpoints: set[Endpoint],
                    degree: dict[Endpoint, int],
                    preferred: Endpoint,
                ) -> Endpoint | None:
                    spare = [ep for ep in endpoints if degree.get(ep, 0) < 2]
                    if not spare:
                        return None
                    if preferred in spare:
                        return preferred
                    pref_pos = _endpoint_position(preferred, self)
                    return min(
                        spare,
                        key=lambda ep: _chebyshev(_endpoint_position(ep, self), pref_pos),
                    )

                bridge_a = _pick_bridge_endpoint(net_a.endpoints, degree_a, ep_a)
                bridge_b = _pick_bridge_endpoint(net_b.endpoints, degree_b, ep_b)
                if bridge_a is not None and bridge_b is not None:
                    merged_pairs = pairs_a + pairs_b
                    bridge_pair = (bridge_a, bridge_b)
                    bridge_pair_rev = (bridge_b, bridge_a)
                    if bridge_pair not in merged_pairs and bridge_pair_rev not in merged_pairs:
                        merged_pairs.append(bridge_pair)
                    net_a.prewired_pairs = merged_pairs
                else:
                    # No degree headroom to add a safe bridge; fall back to
                    # generic materialisation for the merged network.
                    net_a.prewired_pairs = None

                # Since endpoints is a set, just update (auto-dedup)
                net_a.endpoints.update(endpoints_b)
                result = net_a

                # Keep named ports consistent with the surviving network.
                for port_map in (self.input_ports, self.output_ports):
                    for port_name, net_id in list(port_map.items()):
                        if net_id == net_b.network_id:
                            port_map[port_name] = net_a.network_id
        elif idx_a is not None:
            net = self.networks[idx_a]
            if ep_b not in net.endpoints:
                net.endpoints.add(ep_b)
                # A new endpoint joined a pre-wired network —
                # the pre-wired pairs are no longer complete.
                net.prewired_pairs = None
            result = net
        elif idx_b is not None:
            net = self.networks[idx_b]
            if ep_a not in net.endpoints:
                net.endpoints.add(ep_a)
                net.prewired_pairs = None
            result = net
        else:
            # Neither endpoint is in a network yet — create a new one
            net_id = self._next_network_id(color)
            net = Network(network_id=net_id, color=color, endpoints={ep_a, ep_b})
            self.networks.append(net)
            result = net

        self._network_bounds_cache.clear()
        self._debug_assert_invariants()
        return result

    def _next_network_id(self, color: str) -> str:
        """Generate a unique network id like ``"red_0"``, ``"green_3"``."""
        existing = {n.network_id for n in self.networks}
        for i in itertools.count():
            candidate = f"{color}_{i}"
            if candidate not in existing:
                return candidate
        return f"{color}_0"  # unreachable

    def get_network_bounds(self, network_id: str) -> tuple[int, int, int, int]:
        """Return ``(min_x, min_y, max_x, max_y)`` for all positioned
        endpoints in *network_id*.

        Result is cached until the next mutation (``connect``, ``merge``).
        Returns ``(0, 0, 0, 0)`` if the network is not found or has no
        positioned entities.
        """
        if network_id in self._network_bounds_cache:
            return self._network_bounds_cache[network_id]

        net = next((n for n in self.networks if n.network_id == network_id), None)
        if net is None:
            return (0, 0, 0, 0)

        xs: list[int] = []
        ys: list[int] = []
        for ep in net.endpoints:
            ent = self.entities.get(ep.entity_id)
            if ent is not None and ent.position is not None:
                xs.append(ent.position[0])
                ys.append(ent.position[1])

        if not xs:
            result: tuple[int, int, int, int] = (0, 0, 0, 0)
        else:
            result = (min(xs), min(ys), max(xs), max(ys))

        self._network_bounds_cache[network_id] = result
        return result

    # ── introspection ───────────────────────────────────────────────

    def sort_entities_by_position(self) -> None:
        """Reorder ``entities`` dict so iteration follows spatial position:
        top-to-bottom (y), then left-to-right (x).

        Entities without a position sort to the end.  This makes the
        fast path in ``_to_draftsman_impl`` (sorted-position chain)
        produce correct results after layout.
        """
        items = sorted(
            self.entities.items(),
            key=lambda kv: (
                1 if kv[1].position is None else 0,
                kv[1].position[1] if kv[1].position is not None else 0,
                kv[1].position[0] if kv[1].position is not None else 0,
            )
        )
        self.entities = dict(items)

    def shift_positions(self, dx: int, dy: int) -> None:
        """Shift all positioned entities by ``(dx, dy)`` in-place.

        Unpositioned entities (``position is None``) are left untouched.
        """
        if dx == 0 and dy == 0:
            return
        for ent in self.entities.values():
            if ent.position is None:
                continue
            x, y = ent.position
            ent.position = (x + dx, y + dy)
        self._network_bounds_cache.clear()

    def place_relative(
        self,
        origin_x: int = 0,
        origin_y: int = 0,
        *,
        assign_unpositioned: bool = False,
        unpositioned_step_y: int = 2,
    ) -> None:
        """Place this blueprint relative to an origin while preserving offsets.

        This treats existing positions as *local/relative* coordinates.
        Positioned entities are translated so the current bounding-box origin
        (``min_x``, ``min_y``) lands on ``(origin_x, origin_y)``.

        When ``assign_unpositioned`` is True, any entities with
        ``position is None`` are assigned a fallback vertical column starting
        below the current placed area.
        """
        positioned = [ent.position for ent in self.entities.values() if ent.position is not None]
        if positioned:
            min_x = min(p[0] for p in positioned)
            min_y = min(p[1] for p in positioned)
            self.shift_positions(origin_x - min_x, origin_y - min_y)

        if not assign_unpositioned:
            return

        current_max_y = max(
            (ent.position[1] for ent in self.entities.values() if ent.position is not None),
            default=origin_y - unpositioned_step_y,
        )
        next_y = current_max_y + unpositioned_step_y
        for ent in self.entities.values():
            if ent.position is not None:
                continue
            ent.position = (origin_x, next_y)
            next_y += unpositioned_step_y
        self._network_bounds_cache.clear()

    def endpoints_of(self, entity_id: str, port: str) -> set[str]:
        """Return the set of network ids the given endpoint belongs to."""
        result: set[str] = set()
        for net in self.networks:
            for ep in net.endpoints:
                if ep.entity_id == entity_id and ep.port == port:
                    result.add(net.network_id)
        return result

    # ── topology invariants (debug only) ────────────────────────────

    def check_topology(self) -> list[str]:
        """Check structural invariants on the logical blueprint.

        Returns a list of error strings.  An empty list means all checks pass.
        These checks are cheap enough to run after every mutation during
        development and testing.

        Invariants checked
        ------------------
        * Every endpoint references a known entity.
        * No duplicate endpoint (entity_id, port) within a single network.
        * No endpoint belongs to multiple networks of the same colour.
        * Every network has a valid colour and at least one endpoint.
        * Network IDs are unique.
        * Port networks reference existing networks.
        """
        errors: list[str] = []
        entity_ids = set(self.entities.keys())

        # ── Network-level checks ─────────────────────────────────
        seen_net_ids: set[str] = set()
        for net in self.networks:
            if net.network_id in seen_net_ids:
                errors.append(
                    f"Duplicate network_id {net.network_id!r}"
                )
            seen_net_ids.add(net.network_id)

            if net.color not in _VALID_COLORS:
                errors.append(
                    f"Network {net.network_id!r}: invalid color {net.color!r}"
                )

            if not net.endpoints:
                errors.append(
                    f"Network {net.network_id!r} ({net.color}): no endpoints"
                )

            seen_in_net: set[tuple[str, str]] = set()
            for ep in net.endpoints:
                # Must reference a known entity
                if ep.entity_id not in entity_ids:
                    errors.append(
                        f"Network {net.network_id!r}: endpoint "
                        f"{ep.to_string()!r} references unknown entity "
                        f"{ep.entity_id!r}"
                    )
                # No duplicate within same network
                key = (ep.entity_id, ep.port)
                if key in seen_in_net:
                    errors.append(
                        f"Network {net.network_id!r}: duplicate endpoint "
                        f"{ep.to_string()!r}"
                    )
                seen_in_net.add(key)

        # ── Cross-network: endpoint can be in multiple networks ────
        # of the same colour — this models multiple wires to the same
        # entity port.  The real Factorio limit (≤2 connections per
        # port per colour) is enforced by check_wire_topology() on the
        # materialised blueprint.  We only warn if an endpoint appears
        # in more than 2 networks of the same colour.
        colour_ep_count: dict[tuple[str, str, str], int] = {}
        for net in self.networks:
            for ep in net.endpoints:
                key = (ep.entity_id, ep.port, net.color)
                colour_ep_count[key] = colour_ep_count.get(key, 0) + 1
        for (eid, port, colour), count in colour_ep_count.items():
            if count > 2:
                errors.append(
                    f"Endpoint {eid!r}:{port} appears in {count} "
                    f"{colour} networks (max 2 in Factorio)"
                )

        # Note: port→network reference checks are intentionally NOT
        # done here.  Ports can reference networks from sub-blueprints
        # that haven't been merged yet (valid intermediate state).
        # Validation happens at connect_ports() time.

        return errors

    def _debug_assert_invariants(self) -> None:
        """Assert all topology invariants hold (no-op when ``__debug__`` is False).

        Call this after any mutation to the logical blueprint
        (``connect``, ``merge``, etc.).
        """
        if not __debug__:
            return
        errs = self.check_topology()
        if errs:
            raise AssertionError(
                f"LogicalBlueprint topology violation(s) in "
                f"{self.label!r}:\n" + "\n".join(f"  - {e}" for e in errs)
            )

    # ── port helpers ───────────────────────────────────────────────

    def set_input_port(self, name: str, network_id: str) -> None:
        """Declare *network_id* as a named input port."""
        if not any(n.network_id == network_id for n in self.networks):
            raise ValueError(f"Network {network_id!r} does not exist")
        self.input_ports[name] = network_id

    def set_output_port(self, name: str, network_id: str) -> None:
        """Declare *network_id* as a named output port."""
        if not any(n.network_id == network_id for n in self.networks):
            raise ValueError(f"Network {network_id!r} does not exist")
        self.output_ports[name] = network_id

    def get_port_network(self, port_name: str) -> Network:
        """Return the Network for a named input or output port.

        Checks input ports first, then output ports.  Raises KeyError
        if *port_name* is not found.
        """
        if port_name in self.input_ports:
            net_id = self.input_ports[port_name]
        elif port_name in self.output_ports:
            net_id = self.output_ports[port_name]
        else:
            raise KeyError(f"Port {port_name!r} not found in input_ports or output_ports")
        for net in self.networks:
            if net.network_id == net_id:
                return net
        raise KeyError(f"Network {net_id!r} for port {port_name!r} not found")

    # ── composition ────────────────────────────────────────────────

    def merge(
        self,
        other: LogicalBlueprint,
        entity_prefix: str = "",
        network_prefix: str = "",
        port_prefix: str = "",
        position_offset: tuple[int, int] | None = None,
    ) -> None:
        """Merge *other* into this logical blueprint in-place.

        Entities, networks, and ports from *other* are added.  Entity ids,
        network ids, and port names are prefixed with *entity_prefix* /
        *network_prefix* / *port_prefix* respectively (empty string = no
        prefix).  Duplicate ids after prefixing raise ``ValueError``.

        If *position_offset* is provided, all positioned entities from
        *other* are shifted by ``(dx, dy)`` during merge.  This makes it easy
        to compose blueprints that only define relative coordinates.

        Ports from *other* are preserved with their (now-prefixed) network
        ids and port names, and added to this blueprint's ports.  If a
        port name already exists, it is overwritten.
        """
        # ── Entities ────────────────────────────────────────────
        for eid, ent in other.entities.items():
            new_id = entity_prefix + eid
            if new_id in self.entities:
                raise ValueError(
                    f"Entity id {new_id!r} already exists after prefix "
                    f"{entity_prefix!r}; use a different prefix"
                )
            new_pos = ent.position
            if new_pos is not None and position_offset is not None:
                new_pos = (new_pos[0] + position_offset[0], new_pos[1] + position_offset[1])
            new_ent = replace(ent, entity_id=new_id, position=new_pos)
            self.add_entity(new_ent)

        # ── Networks ────────────────────────────────────────────
        for net in other.networks:
            new_net_id = network_prefix + net.network_id
            if any(n.network_id == new_net_id for n in self.networks):
                raise ValueError(
                    f"Network id {new_net_id!r} already exists after prefix "
                    f"{network_prefix!r}"
                )
            new_endpoints = [
                Endpoint(entity_id=entity_prefix + ep.entity_id, port=ep.port)
                for ep in net.endpoints
            ]
            # Preserve prewired_pairs with prefixed entity IDs
            new_prewired = None
            if net.prewired_pairs is not None:
                new_prewired = [
                    (Endpoint(entity_id=entity_prefix + a.entity_id, port=a.port),
                     Endpoint(entity_id=entity_prefix + b.entity_id, port=b.port))
                    for a, b in net.prewired_pairs
                ]
            self.add_network(Network(
                network_id=new_net_id,
                color=net.color,
                endpoints=new_endpoints,
                prewired_pairs=new_prewired,
            ))

        # ── Ports ───────────────────────────────────────────────
        for port_name, net_id in other.input_ports.items():
            self.input_ports[port_prefix + port_name] = network_prefix + net_id
        for port_name, net_id in other.output_ports.items():
            self.output_ports[port_prefix + port_name] = network_prefix + net_id

        self._debug_assert_invariants()

    def connect_ports(
        self,
        src_lb: LogicalBlueprint,
        src_port: str,
        dst_lb: LogicalBlueprint,
        dst_port: str,
    ) -> Network:
        """Connect an output port of *src_lb* to an input port of *dst_lb*.

        Both *src_lb* and *dst_lb* must have already been merged into this
        blueprint (so their networks are registered here).

        Returns the merged Network.
        """
        src_net = src_lb.get_port_network(src_port)
        dst_net = dst_lb.get_port_network(dst_port)

        # Find the first endpoint in each network to use as bridge
        if not src_net.endpoints or not dst_net.endpoints:
            raise ValueError(
                f"Cannot connect ports with empty networks: "
                f"{src_port!r} ({len(src_net.endpoints)} eps), "
                f"{dst_port!r} ({len(dst_net.endpoints)} eps)"
            )

        ep_src = src_net.endpoints[0]
        ep_dst = dst_net.endpoints[0]
        return self.connect(src_net.color, ep_src, ep_dst)


# ═══════════════════════════════════════════════════════════════════════
# TOML serialization
# ═══════════════════════════════════════════════════════════════════════


def _toml_escape(value: str) -> str:
    """Minimal TOML string escaping."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _toml_value(v: Any) -> str:
    """Format a Python value as a TOML literal."""
    if isinstance(v, bool):
        return "true" if v else "false"
    elif isinstance(v, int):
        return str(v)
    elif isinstance(v, float):
        return repr(v)
    elif isinstance(v, str):
        return f'"{_toml_escape(v)}"'
    elif isinstance(v, list):
        items = ", ".join(_toml_value(i) for i in v)
        return f"[{items}]"
    elif v is None:
        return '""'
    else:
        return f'"{_toml_escape(str(v))}"'


def _emit_key_val(key: str, value: Any, indent: str = "") -> str:
    """Emit a ``key = value`` line."""
    return f"{indent}{key} = {_toml_value(value)}"


def _entity_to_toml(entity: LogicalEntity) -> str:
    """Serialize one ``[[entity]]`` block."""
    lines: list[str] = []
    lines.append("[[entity]]")
    lines.append(_emit_key_val("id", entity.entity_id))
    lines.append(_emit_key_val("type", entity.type))

    if entity.position is not None:
        lines.append(_emit_key_val("position", list(entity.position)))
    if entity.direction is not None:
        lines.append(_emit_key_val("direction", entity.direction))

    props = entity.properties

    if entity.type == "arithmetic-combinator":
        lines.append(_emit_key_val("first_operand", props.get("first_operand", "")))
        lines.append(_emit_key_val("operation", props.get("operation", "*")))
        lines.append(_emit_key_val("second_operand", props.get("second_operand", 0)))
        lines.append(_emit_key_val("output_signal", props.get("output_signal", "")))
        # Optional wire-source hints
        fow = props.get("first_operand_wires")
        if fow:
            lines.append(_emit_key_val("first_operand_wires", sorted(fow)))
        sow = props.get("second_operand_wires")
        if sow:
            lines.append(_emit_key_val("second_operand_wires", sorted(sow)))

    elif entity.type == "decider-combinator":
        conditions: list[dict] = props.get("conditions", [])
        for cond in conditions:
            lines.append("[[entity.condition]]")
            lines.append(_emit_key_val("first", cond.get("first", ""), "  "))
            lines.append(_emit_key_val("op", cond.get("op", "="), "  "))
            if "second_signal" in cond:
                lines.append(_emit_key_val("second_signal", cond["second_signal"], "  "))
            elif "constant" in cond:
                lines.append(_emit_key_val("constant", cond["constant"], "  "))
            if cond.get("compare_type") and cond["compare_type"] != "and":
                lines.append(_emit_key_val("compare_type", cond["compare_type"], "  "))

        outputs: list[dict] = props.get("outputs", [])
        for out in outputs:
            lines.append("[[entity.output]]")
            lines.append(_emit_key_val("signal", out.get("signal", ""), "  "))
            if "quality" in out:
                lines.append(_emit_key_val("quality", out["quality"], "  "))
            lines.append(_emit_key_val("copy_count", out.get("copy_count", False), "  "))
            lines.append(_emit_key_val("constant", out.get("constant", 0), "  "))

    elif entity.type == "constant-combinator":
        signals: list[dict] = props.get("signals", [])
        for sig in signals:
            lines.append("[[entity.signal]]")
            lines.append(_emit_key_val("name", sig.get("name", ""), "  "))
            if "quality" in sig:
                lines.append(_emit_key_val("quality", sig["quality"], "  "))
            lines.append(_emit_key_val("value", sig.get("value", 0), "  "))

    elif entity.type == "programmable-speaker":
        lines.append(_emit_key_val("instrument", props.get("instrument", "piano")))
        lines.append(_emit_key_val("note", props.get("note", "")))
        lines.append(_emit_key_val("vol_signal", props.get("vol_signal", "")))
        lines.append(_emit_key_val("vol_quality", props.get("vol_quality", "normal")))
        lines.append(_emit_key_val("polyphony", props.get("polyphony", True)))
        lines.append(_emit_key_val("circuit_enabled", props.get("circuit_enabled", True)))

    elif entity.type == "small-lamp":
        lines.append(_emit_key_val("use_colors", props.get("use_colors", False)))
        lines.append(_emit_key_val("always_on", props.get("always_on", False)))
        ce = props.get("circuit_enabled", False)
        lines.append(_emit_key_val("circuit_enabled", ce))
        if ce:
            cond = props.get("condition")
            if cond:
                lines.append("[[entity.condition]]")
                lines.append(_emit_key_val("first", cond.get("first", ""), "  "))
                lines.append(_emit_key_val("op", cond.get("op", "="), "  "))
                if "second_signal" in cond:
                    lines.append(_emit_key_val("second_signal", cond["second_signal"], "  "))
                elif "constant" in cond:
                    lines.append(_emit_key_val("constant", cond["constant"], "  "))
        color_sig = props.get("color_signal")
        if color_sig:
            lines.append(_emit_key_val("color_signal", color_sig))

    elif entity.type in ("small-electric-pole", "medium-electric-pole", "substation"):
        quality = props.get("quality")
        if quality and quality != "normal":
            lines.append(_emit_key_val("quality", quality))

    return "\n".join(lines)


def _network_to_toml(net: Network) -> str:
    """Serialize one ``[[network]]`` block."""
    lines: list[str] = []
    lines.append("[[network]]")
    lines.append(_emit_key_val("id", net.network_id))
    lines.append(_emit_key_val("color", net.color))
    ep_strings = [ep.to_string() for ep in net.endpoints]
    lines.append(_emit_key_val("endpoints", ep_strings))
    if net.prewired_pairs is not None:
        pw_strings = [f"{a.to_string()}->{b.to_string()}" for a, b in net.prewired_pairs]
        lines.append(_emit_key_val("prewired_pairs", pw_strings))
    return "\n".join(lines)


def _port_to_toml(port_name: str, network_id: str, port_type: str) -> str:
    """Serialize one ``[[input_port]]`` or ``[[output_port]]`` block."""
    lines: list[str] = []
    lines.append(f"[[{port_type}_port]]")
    lines.append(_emit_key_val("name", port_name))
    lines.append(_emit_key_val("network", network_id))
    return "\n".join(lines)


def to_toml(lb: LogicalBlueprint) -> str:
    """Serialize a :class:`LogicalBlueprint` to an LLM-friendly TOML string."""
    parts: list[str] = []

    if lb.label:
        parts.append(_emit_key_val("label", lb.label))
        parts.append("")

    for entity in lb.entities.values():
        parts.append(_entity_to_toml(entity))
        parts.append("")

    for net in lb.networks:
        parts.append(_network_to_toml(net))
        parts.append("")

    for port_name, net_id in lb.input_ports.items():
        parts.append(_port_to_toml(port_name, net_id, "input"))
        parts.append("")

    for port_name, net_id in lb.output_ports.items():
        parts.append(_port_to_toml(port_name, net_id, "output"))
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


# ═══════════════════════════════════════════════════════════════════════
# TOML deserialization
# ═══════════════════════════════════════════════════════════════════════


def from_toml(toml_str: str) -> LogicalBlueprint:
    """Parse a TOML string into a :class:`LogicalBlueprint`.

    Uses the stdlib ``tomllib`` (Python ≥ 3.11).
    """
    data = tomllib.loads(toml_str)
    return _from_parsed(data)


def from_blueprint_string(blueprint_str: str) -> LogicalBlueprint:
    """Parse a Factorio blueprint string into a :class:`LogicalBlueprint`."""
    from draftsman.blueprintable import Blueprint

    return from_draftsman(Blueprint.from_string(blueprint_str))


def _logical_to_data(lb: LogicalBlueprint) -> dict[str, Any]:
    """Convert a :class:`LogicalBlueprint` into a plain nested mapping."""
    data: dict[str, Any] = {}

    if lb.label:
        data["label"] = lb.label

    entities: list[dict[str, Any]] = []
    for ent in lb.entities.values():
        item: dict[str, Any] = {
            "id": ent.entity_id,
            "type": ent.type,
        }
        if ent.position is not None:
            item["position"] = [ent.position[0], ent.position[1]]
        if ent.direction is not None:
            item["direction"] = ent.direction
        for k, v in ent.properties.items():
            item[k] = v
        entities.append(item)
    data["entity"] = entities

    networks: list[dict[str, Any]] = []
    for net in lb.networks:
        item = {
            "id": net.network_id,
            "color": net.color,
            "endpoints": sorted(ep.to_string() for ep in net.endpoints),
        }
        if net.prewired_pairs is not None:
            item["prewired_pairs"] = [
                f"{a.to_string()}->{b.to_string()}" for a, b in net.prewired_pairs
            ]
        networks.append(item)
    data["network"] = networks

    data["input_port"] = [
        {"name": name, "network": net_id}
        for name, net_id in lb.input_ports.items()
    ]
    data["output_port"] = [
        {"name": name, "network": net_id}
        for name, net_id in lb.output_ports.items()
    ]
    return data


def _yaml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if v is None:
        return "null"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _yaml_dump(v: Any, indent: int = 0) -> str:
    prefix = " " * indent
    if isinstance(v, dict):
        lines: list[str] = []
        for key, value in v.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.append(_yaml_dump(value, indent + 2))
            else:
                lines.append(f"{prefix}{key}: {_yaml_scalar(value)}")
        return "\n".join(lines)

    if isinstance(v, list):
        lines = []
        if not v:
            return f"{prefix}[]"
        for item in v:
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}-")
                lines.append(_yaml_dump(item, indent + 2))
            else:
                lines.append(f"{prefix}- {_yaml_scalar(item)}")
        return "\n".join(lines)

    return f"{prefix}{_yaml_scalar(v)}"


def to_yaml(lb: LogicalBlueprint) -> str:
    """Serialize a :class:`LogicalBlueprint` to a YAML string."""
    return _yaml_dump(_logical_to_data(lb)) + "\n"


def blueprint_string_to_yaml(blueprint_str: str) -> str:
    """Convert a Factorio blueprint string directly to Logical YAML."""
    return to_yaml(from_blueprint_string(blueprint_str))


def _from_parsed(data: dict[str, Any]) -> LogicalBlueprint:
    """Build a LogicalBlueprint from a parsed TOML dict."""
    lb = LogicalBlueprint(label=data.get("label", ""))

    # Entities
    raw_entities: list[dict] = data.get("entity", [])
    for raw in raw_entities:
        entity = _parse_entity(raw)
        lb.add_entity(entity)

    # Networks
    raw_networks: list[dict] = data.get("network", [])
    for raw in raw_networks:
        net = _parse_network(raw)
        lb.add_network(net)

    # Input ports
    for raw in data.get("input_port", []):
        lb.input_ports[raw["name"]] = raw["network"]

    # Output ports
    for raw in data.get("output_port", []):
        lb.output_ports[raw["name"]] = raw["network"]

    lb._debug_assert_invariants()
    return lb


def _parse_entity(raw: dict[str, Any]) -> LogicalEntity:
    """Parse a single ``[[entity]]`` block."""
    entity_id: str = raw["id"]
    etype: str = raw["type"]
    props: dict[str, Any] = {}
    position = None
    direction = None

    if "position" in raw:
        pos = raw["position"]
        if isinstance(pos, list) and len(pos) == 2:
            position = (int(pos[0]), int(pos[1]))
    if "direction" in raw:
        direction = int(raw["direction"])

    if etype == "arithmetic-combinator":
        props["first_operand"] = raw.get("first_operand", "")
        props["operation"] = raw.get("operation", "*")
        props["second_operand"] = raw.get("second_operand", 0)
        props["output_signal"] = raw.get("output_signal", "")
        if "first_operand_wires" in raw:
            props["first_operand_wires"] = raw["first_operand_wires"]
        if "second_operand_wires" in raw:
            props["second_operand_wires"] = raw["second_operand_wires"]

    elif etype == "decider-combinator":
        conditions: list[dict] = []
        for cond in raw.get("condition", []):
            c: dict[str, Any] = {"first": cond["first"], "op": cond["op"]}
            if "second_signal" in cond:
                c["second_signal"] = cond["second_signal"]
            if "constant" in cond:
                c["constant"] = cond["constant"]
            if "compare_type" in cond:
                c["compare_type"] = cond["compare_type"]
            conditions.append(c)
        props["conditions"] = conditions

        outputs: list[dict] = []
        for out in raw.get("output", []):
            o: dict[str, Any] = {
                "signal": out["signal"],
                "copy_count": out.get("copy_count", False),
                "constant": out.get("constant", 0),
            }
            if "quality" in out:
                o["quality"] = out["quality"]
            outputs.append(o)
        props["outputs"] = outputs

    elif etype == "constant-combinator":
        signals: list[dict] = []
        for sig in raw.get("signal", []):
            s: dict[str, Any] = {
                "name": sig["name"],
                "value": sig.get("value", 0),
            }
            if "quality" in sig:
                s["quality"] = sig["quality"]
            signals.append(s)
        props["signals"] = signals

    elif etype == "programmable-speaker":
        props["instrument"] = raw.get("instrument", "piano")
        props["note"] = raw.get("note", "")
        props["vol_signal"] = raw.get("vol_signal", "")
        props["vol_quality"] = raw.get("vol_quality", "normal")
        props["polyphony"] = raw.get("polyphony", True)
        props["circuit_enabled"] = raw.get("circuit_enabled", True)

    elif etype == "small-lamp":
        props["use_colors"] = raw.get("use_colors", False)
        props["always_on"] = raw.get("always_on", False)
        props["circuit_enabled"] = raw.get("circuit_enabled", False)
        if raw.get("condition"):
            # TOML stores [[entity.condition]] as a list of tables
            cond_list = raw["condition"]
            if isinstance(cond_list, list) and cond_list:
                c_raw = cond_list[0]
                cond: dict[str, Any] = {"first": c_raw["first"], "op": c_raw["op"]}
                if "second_signal" in c_raw:
                    cond["second_signal"] = c_raw["second_signal"]
                if "constant" in c_raw:
                    cond["constant"] = c_raw["constant"]
                props["condition"] = cond
        color_sig = raw.get("color_signal")
        if color_sig:
            props["color_signal"] = color_sig

    elif etype in ("small-electric-pole", "medium-electric-pole", "substation"):
        quality = raw.get("quality")
        if quality and quality != "normal":
            props["quality"] = quality

    return LogicalEntity(
        entity_id=entity_id,
        type=etype,
        properties=props,
        position=position,
        direction=direction,
    )


def _parse_network(raw: dict[str, Any]) -> Network:
    """Parse a single ``[[network]]`` block."""
    net_id: str = raw["id"]
    color: str = raw["color"]
    endpoints: list[Endpoint] = []
    for ep_str in raw.get("endpoints", []):
        endpoints.append(Endpoint.from_string(ep_str))
    prewired_pairs: list[tuple[Endpoint, Endpoint]] | None = None
    if "prewired_pairs" in raw:
        prewired_pairs = []
        for pw_str in raw["prewired_pairs"]:
            a_str, b_str = pw_str.split("->", 1)
            prewired_pairs.append((Endpoint.from_string(a_str), Endpoint.from_string(b_str)))
    return Network(network_id=net_id, color=color, endpoints=endpoints, prewired_pairs=prewired_pairs)


# ═══════════════════════════════════════════════════════════════════════
# Draftsman ↔ LogicalBlueprint bridge
# ═══════════════════════════════════════════════════════════════════════


def from_draftsman(bp: Any) -> LogicalBlueprint:
    """Convert a draftsman :class:`~draftsman.blueprintable.Blueprint` into a
    :class:`LogicalBlueprint`.

    Entity ids, types, positions, directions, and combinator settings are
    preserved.  Circuit connections are translated into networks.
    """
    import warnings

    from draftsman.warning import (
        ConnectionDistanceWarning,
        ConnectionSideWarning,
        OverlappingObjectsWarning,
        UnknownNoteWarning,
        UnknownSignalWarning,
    )

    with warnings.catch_warnings():
        for cat in (
            ConnectionDistanceWarning,
            ConnectionSideWarning,
            UnknownNoteWarning,
            UnknownSignalWarning,
        ):
            warnings.filterwarnings("ignore", category=cat)
        return _from_draftsman_impl(bp)


def _from_draftsman_impl(bp: Any) -> LogicalBlueprint:
    """Internal implementation — see :func:`from_draftsman`."""
    from draftsman.entity import (
        ArithmeticCombinator,
        ConstantCombinator,
        DeciderCombinator,
        ElectricPole,
        Lamp,
        ProgrammableSpeaker,
    )

    lb = LogicalBlueprint(label=getattr(bp, "label", ""))

    # ── 1. Convert entities ───────────────────────────────────────
    # Build entity→surrogate_id map for entities with None id (parsed blueprints).
    # Use Python id() to map entity objects consistently between entity creation
    # and wire resolution (both reference the same object).
    _ent_map: dict[int, str] = {}  # id(entity) → surrogate_id

    def _resolve_eid(ent: Any, idx: int) -> str:
        raw_id = getattr(ent, "id", None)
        if raw_id is not None:
            return raw_id
        obj_id = id(ent)
        if obj_id not in _ent_map:
            _ent_map[obj_id] = f"_ent{idx}"
        return _ent_map[obj_id]

    for idx, ent in enumerate(bp.entities):
        eid: str = _resolve_eid(ent, idx)
        etype: str = ent.name
        pos: tuple[int, int] | None = (
            (ent.tile_position.x, ent.tile_position.y)
            if hasattr(ent, "tile_position") and hasattr(ent.tile_position, "x")
            else None
        )
        direction: int | None = getattr(ent, "direction", None)

        props: dict[str, Any] = {}

        if isinstance(ent, ArithmeticCombinator):
            props["first_operand"] = _signal_to_str(ent.first_operand)
            props["operation"] = ent.operation
            props["second_operand"] = _signal_or_constant(ent.second_operand)
            props["output_signal"] = _signal_to_str(ent.output_signal)
            fow = getattr(ent, "first_operand_wires", None)
            if fow is not None:
                wires = []
                if getattr(fow, "red", False):
                    wires.append("red")
                if getattr(fow, "green", False):
                    wires.append("green")
                if wires:
                    props["first_operand_wires"] = wires
            sow = getattr(ent, "second_operand_wires", None)
            if sow is not None:
                wires = []
                if getattr(sow, "red", False):
                    wires.append("red")
                if getattr(sow, "green", False):
                    wires.append("green")
                if wires:
                    props["second_operand_wires"] = wires

        elif isinstance(ent, DeciderCombinator):
            conditions: list[dict] = []
            for cond in getattr(ent, "conditions", []):
                c: dict[str, Any] = {
                    "first": _signal_to_str(cond.first_signal),
                    "op": cond.comparator,
                }
                second_sig = getattr(cond, "second_signal", None)
                if second_sig is not None:
                    c["second_signal"] = _signal_to_str(second_sig)
                else:
                    const_val = getattr(cond, "constant", None)
                    if const_val is not None:
                        c["constant"] = const_val
                ct = getattr(cond, "compare_type", None)
                if ct:
                    c["compare_type"] = ct
                conditions.append(c)
            props["conditions"] = conditions

            outputs: list[dict] = []
            for out in getattr(ent, "outputs", []):
                sig = out.signal
                o: dict[str, Any] = {
                    "signal": _signal_to_str(sig),
                    "copy_count": out.copy_count_from_input,
                    "constant": out.constant,
                }
                outputs.append(o)
            props["outputs"] = outputs

        elif isinstance(ent, ConstantCombinator):
            signals: list[dict] = []
            max_count: int = getattr(ent, "max_signal_count", 0)
            for i in range(max_count):
                sig = ent.get_signal(i)
                if sig is None:
                    continue
                s: dict[str, Any] = {
                    "name": getattr(sig, "name", ""),
                    "value": getattr(sig, "count", 0),
                }
                quality = getattr(sig, "quality", None)
                if quality:
                    s["quality"] = quality
                signals.append(s)
            props["signals"] = signals

        elif isinstance(ent, ProgrammableSpeaker):
            vs = getattr(ent, "volume_signal", None)
            props["instrument"] = getattr(ent, "instrument_name", "piano")
            props["note"] = getattr(ent, "note_name", "")
            props["vol_signal"] = getattr(vs, "name", "") if vs else ""
            props["vol_quality"] = getattr(vs, "quality", "normal") if vs else "normal"
            props["polyphony"] = getattr(ent, "allow_polyphony", True)
            props["circuit_enabled"] = getattr(ent, "circuit_enabled", True)

        elif isinstance(ent, Lamp):
            props["use_colors"] = getattr(ent, "use_colors", False)
            props["always_on"] = getattr(ent, "always_on", False)
            props["circuit_enabled"] = getattr(ent, "circuit_enable_disable", False)
            cond = getattr(ent, "circuit_condition", None)
            if cond is not None:
                c: dict[str, Any] = {
                    "first": _signal_to_str(cond.first_signal),
                    "op": cond.comparator,
                }
                second = getattr(cond, "second_signal", None)
                if second is not None:
                    c["second_signal"] = _signal_to_str(second)
                else:
                    const = getattr(cond, "constant", None)
                    if const is not None:
                        c["constant"] = const
                props["condition"] = c
            color_sig = getattr(ent, "rgb_signal", None)
            if color_sig:
                props["color_signal"] = _signal_to_str(color_sig)

        elif isinstance(ent, ElectricPole):
            quality = getattr(ent, "quality", None)
            if quality and quality != "normal" and str(quality) != "normal":
                props["quality"] = str(quality)

        lb.add_entity(
            LogicalEntity(
                entity_id=eid,
                type=etype,
                properties=props,
                position=pos,
                direction=direction,
            )
        )

    # ── 2. Convert circuit connections → networks ─────────────────
    for conn in _iter_connections(bp, _ent_map):
        color = conn["color"]
        port_a = _normalise_side(conn["side_1"])
        port_b = _normalise_side(conn["side_2"])

        ep_a = Endpoint(entity_id=conn["id1"], port=port_a)
        ep_b = Endpoint(entity_id=conn["id2"], port=port_b)
        lb.connect(color, ep_a, ep_b)

    lb._debug_assert_invariants()
    return lb


def _normalise_side(side: Any) -> str:
    """Normalise a draftsman side value to ``"input"`` or ``"output"``."""
    if isinstance(side, str):
        s = side.lower()
        if s in ("input", "output"):
            return s
    if isinstance(side, int):
        return "input" if side == 0 else "output"
    return "input"


def _iter_connections(bp: Any, ent_map: dict[int, str] | None = None) -> list[dict[str, Any]]:
    """Extract circuit connections from a draftsman Blueprint.

    Draftsman stores circuit connections as ``bp.wires``, a list of
    ``[Association, WireConnectorID, Association, WireConnectorID]`` tuples.

    *ent_map* is an optional ``{id(entity): surrogate_id}`` dict used to
    resolve entity identities consistently with :func:`from_draftsman`.
    """
    result: list[dict[str, Any]] = []

    wires = getattr(bp, "wires", None)
    if wires is None:
        return result

    for w in wires:
        assoc1, conn1, assoc2, conn2 = w
        ent1 = assoc1()  # resolve Association → entity
        ent2 = assoc2()

        def _resolve(ent: Any) -> str:
            raw_id = getattr(ent, "id", None)
            if raw_id is not None:
                return raw_id
            if ent_map is not None:
                obj_id = id(ent)
                return ent_map.get(obj_id, f"ent_{obj_id}")
            return f"ent_{id(ent)}"

        eid1 = _resolve(ent1)
        eid2 = _resolve(ent2)

        # WireConnectorID from live draftsman has .value; parsed blueprints use plain int
        wire_type_1 = conn1.value if hasattr(conn1, "value") else int(conn1)
        wire_type_2 = conn2.value if hasattr(conn2, "value") else int(conn2)

        # Decode color and side from WireConnectorID value:
        #   1=red+input, 2=green+input, 3=red+output, 4=green+output
        color = "red" if wire_type_1 % 2 == 1 else "green"
        side_1 = "input" if wire_type_1 <= 2 else "output"
        side_2 = "input" if wire_type_2 <= 2 else "output"

        result.append(
            {
                "color": color,
                "id1": eid1,
                "id2": eid2,
                "side_1": side_1,
                "side_2": side_2,
            }
        )

    return result


def _signal_to_str(sig: Any) -> str:
    """Convert a draftsman signal reference to a string.

    Handles :class:`~draftsman.signatures.SignalID`, plain dicts, and strings.
    """
    if sig is None:
        return ""
    # SignalID / SignalFilter from draftsman.signatures
    if hasattr(sig, "name") and hasattr(sig, "type"):
        name = getattr(sig, "name", "")
        quality = getattr(sig, "quality", "normal")
        if quality and quality != "normal":
            return f"{name}@{quality}"
        return str(name)
    if isinstance(sig, dict):
        name = sig.get("name", "")
        quality = sig.get("quality")
        if quality:
            return f"{name}@{quality}"
        return str(name)
    return str(sig)


def _signal_or_constant(val: Any) -> int | str:
    """Return the int constant or signal string for an AC second operand."""
    if isinstance(val, int):
        return val
    return _signal_to_str(val)


def _endpoint_position(ep: Endpoint, lb: LogicalBlueprint) -> tuple[int, int]:
    """Return the (x, y) tile position of the entity referenced by *ep*.

    Returns (0, 0) if the entity or its position is not found.
    """
    ent = lb.entities.get(ep.entity_id)
    if ent is not None and ent.position is not None:
        return ent.position
    return (0, 0)


def _chebyshev(p1: tuple[int, int], p2: tuple[int, int]) -> int:
    """Chebyshev distance between two tile positions."""
    return max(abs(p1[0] - p2[0]), abs(p1[1] - p2[1]))


def _sort_endpoints_by_position(
    eps: list[Endpoint], lb: LogicalBlueprint,
) -> list[Endpoint]:
    """Sort endpoints by entity position: primary y (top-to-bottom),
    secondary x (left-to-right).

    Endpoints without a known position sort to the origin (0, 0).
    """
    return sorted(eps, key=lambda ep: (
        _endpoint_position(ep, lb)[1],  # y first (top-to-bottom)
        _endpoint_position(ep, lb)[0],  # x second (left-to-right)
    ))


def _wire_horizontal_first(
    eps: list[Endpoint], lb: LogicalBlueprint,
    max_distance: int = 64,
) -> list[tuple[Endpoint, Endpoint]]:
    """Materialize a network into pairwise connections using horizontal-first
    chaining with rightmost bridge merging.

    1.  Sort endpoints by position: **x** (left-to-right), then **y**
        (top-to-bottom).
    2.  Connect **horizontally** wherever possible — endpoints on the same
        row (*y*) with ``|Δx| == 1`` are wired together.  Union-find tracks
        the resulting connected subsets (one per row for a lamp grid).
    3.  Repeatedly find the **rightmost** feasible pair of endpoints
        that belong to *different* subsets, wire them, and merge.
        "Rightmost" means largest ``min(x_a, x_b)`` (both ends at the
        right edge); tie-break on shortest distance (keeps rows
        adjacent), then bottom‑most *y*.  Pairs exceeding
        *max_distance* are never chosen.  This reproduces lamp‑matrix
        wiring: rows chained left‑to‑right, rightmost column bridging
        adjacent rows in a chain.
    4.  Stop when all endpoints are in one subset, or when no pair of
        subsets can be bridged within *max_distance* Chebyshev tiles.

    Returns a list of ``(ep_a, ep_b)`` pairs to wire.
    """
    if len(eps) <= 1:
        return []

    # ── Position lookup ──────────────────────────────────────────
    pos: dict[int, tuple[int, int]] = {}  # id(Endpoint) → (x, y)
    for ep in eps:
        pos[id(ep)] = _endpoint_position(ep, lb)

    # Sort: x first (left→right), then y (top→bottom)
    sorted_eps = sorted(eps, key=lambda ep: (pos[id(ep)][0], pos[id(ep)][1]))
    n = len(sorted_eps)

    # ── Union‑find ───────────────────────────────────────────────
    parent: list[int] = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    connections: list[tuple[Endpoint, Endpoint]] = []

    # ── Step 1: Horizontal connections (same y, |Δx| == 1) ───────
    by_y: dict[int, list[int]] = {}  # y → list of indices
    for i, ep in enumerate(sorted_eps):
        y = pos[id(ep)][1]
        by_y.setdefault(y, []).append(i)

    for y, indices in by_y.items():
        # Sort by x within each row
        indices.sort(key=lambda i: pos[id(sorted_eps[i])][0])
        for k in range(len(indices) - 1):
            a_idx = indices[k]
            b_idx = indices[k + 1]
            ax = pos[id(sorted_eps[a_idx])][0]
            bx = pos[id(sorted_eps[b_idx])][0]
            if abs(ax - bx) == 1:
                connections.append((sorted_eps[a_idx], sorted_eps[b_idx]))
                union(a_idx, b_idx)

    # ── Step 2: Rightmost bridge merging ─────────────────────────
    # Track degree per endpoint (Factorio limit: ≤ 2 per port per colour).
    degree: list[int] = [0] * n
    # Build O(1) lookup for endpoint → index
    ep_to_idx: dict[Endpoint, int] = {ep: i for i, ep in enumerate(sorted_eps)}
    # Apply degree from horizontal connections already made
    for a, b in connections:
        a_idx = ep_to_idx[a]
        b_idx = ep_to_idx[b]
        degree[a_idx] += 1
        degree[b_idx] += 1

    while True:
        # Collect distinct subset roots
        roots = {find(i) for i in range(n)}
        if len(roots) <= 1:
            break

        best_i: int = -1
        best_j: int = -1
        best_dist = max_distance + 1
        best_x: int = -(1 << 30)
        best_y: int = -1

        for i in range(n):
            if degree[i] >= 2:
                continue  # Factorio: max 2 connections per port
            ri = find(i)
            pi = pos[id(sorted_eps[i])]
            for j in range(i + 1, n):
                if degree[j] >= 2:
                    continue
                rj = find(j)
                if ri == rj:
                    continue
                pj = pos[id(sorted_eps[j])]
                d = _chebyshev(pi, pj)
                if d > max_distance:
                    continue

                # Rightmost preference: the *lesser* x of the two
                # endpoints must be as large as possible — this
                # ensures both ends sit at the right edge (e.g.
                # (W-1, y) ↔ (W-1, y+1) beats (W-2, y) ↔ (W-1, y+1)).
                # Distance is only a constraint (≤max_distance), not
                # an optimisation target.
                mx = min(pi[0], pj[0])
                my = max(pi[1], pj[1])  # bottom-most tie-break

                better = False
                if mx > best_x:
                    better = True
                elif mx == best_x:
                    if d < best_dist:
                        better = True
                    elif d == best_dist and my > best_y:
                        better = True

                if better:
                    best_dist = d
                    best_x = mx
                    best_y = my
                    best_i = i
                    best_j = j

        if best_i < 0:
            break  # no bridge possible within max_distance

        connections.append((sorted_eps[best_i], sorted_eps[best_j]))
        degree[best_i] += 1
        degree[best_j] += 1
        union(best_i, best_j)

    return connections


def _find_closest_pair(
    eps_a: list[Endpoint],
    eps_b: list[Endpoint],
    lb: LogicalBlueprint,
) -> tuple[Endpoint, Endpoint] | None:
    """Return the pair ``(ep_a, ep_b)`` — one from *eps_a*, one from *eps_b* —
    with the smallest Chebyshev distance between their entity positions.

    Tie-breaking: prefer smaller x, then smaller y (on the *eps_a* side).

    Returns None if either list is empty.
    """
    if not eps_a or not eps_b:
        return None
    best_dist = 1 << 30
    best_pair: tuple[Endpoint, Endpoint] | None = None
    for a in eps_a:
        pa = _endpoint_position(a, lb)
        for b in eps_b:
            pb = _endpoint_position(b, lb)
            d = _chebyshev(pa, pb)
            if d < best_dist:
                best_dist = d
                best_pair = (a, b)
            elif d == best_dist and best_pair is not None:
                # Tie-break: prefer smaller x on a-side, then smaller y
                old_pa = _endpoint_position(best_pair[0], lb)
                if pa[0] < old_pa[0] or (pa[0] == old_pa[0] and pa[1] < old_pa[1]):
                    best_pair = (a, b)
    return best_pair


def to_draftsman(lb: LogicalBlueprint, bp: Any | None = None, *, _validate: bool = True) -> Any:
    """Convert a :class:`LogicalBlueprint` into a draftsman
    :class:`~draftsman.blueprintable.Blueprint`.

    Parameters
    ----------
    lb : LogicalBlueprint
        The logical blueprint to materialise.
    bp : Blueprint | None
        If given, entities and connections are appended to this existing
        blueprint.  Otherwise a new one is created.
    _validate : bool
        When True (default), Draftsman's attrs validators run on every
        entity property assignment.  When False, ``object.__setattr__``
        is used to bypass validators for a significant speedup.  Tests
        should always use ``_validate=True`` (the default).

    Returns
    -------
    Blueprint
        The draftsman blueprint with all entities placed and wired.
    """
    import warnings

    from draftsman.warning import (
        ConnectionDistanceWarning,
        ConnectionSideWarning,
        OverlappingObjectsWarning,
        UnknownNoteWarning,
        UnknownSignalWarning,
    )

    # Suppress known non-fatal draftsman warnings during materialisation.
    with warnings.catch_warnings():
        for cat in (
            ConnectionDistanceWarning,
            ConnectionSideWarning,
            UnknownNoteWarning,
            UnknownSignalWarning,
        ):
            warnings.filterwarnings("ignore", category=cat)
        return _to_draftsman_impl(lb, bp, _validate=_validate)


def _to_draftsman_impl(lb: LogicalBlueprint, bp: Any | None = None, *, _validate: bool = True) -> Any:
    """Internal implementation — see :func:`to_draftsman`.

    When *_validate* is False, uses ``object.__setattr__`` to bypass
    Draftsman's attrs validators/converters, significantly reducing
    per-entity construction overhead for large blueprints.
    """
    from draftsman.blueprintable import Blueprint
    from draftsman.constants import Direction
    from draftsman.entity import new_entity

    if bp is None:
        bp = Blueprint()
        bp.label = lb.label

    # Pre-build the common circuit condition for speakers when skipping
    # validation (every speaker uses signal-no-entry = 0).
    _cached_speaker_condition: Any = None
    if not _validate:
        from draftsman.signatures import Condition, SignalID
        from draftsman.constants import LampColorMode

        # Helper to convert _str_to_signal_ref output → proper SignalID
        def _to_signal_id(value: Any) -> Any:
            if isinstance(value, (str, dict)):
                return SignalID.converter(value)
            return value

        _cached_speaker_condition = Condition.__new__(Condition)
        object.__setattr__(_cached_speaker_condition, "first_signal",
                           _to_signal_id("signal-no-entry"))
        object.__setattr__(_cached_speaker_condition, "comparator", "=")
        object.__setattr__(_cached_speaker_condition, "constant", 0)

    # ── 1. Create entities ─────────────────────────────────────────
    # Stack unpositioned entities vertically so Draftsman doesn't warn
    # about overlap.  This is NOT layout — the composer does real layout.
    _fallback_y = 0
    for entity in lb.entities.values():
        if entity.position is None:
            entity.position = (0, _fallback_y)
            _fallback_y += 2  # combinators are 2 tiles tall
        pos = entity.position or (0, 0)
        direction = entity.direction if entity.direction is not None else Direction.NORTH
        quality_val = entity.properties.get("quality")
        quality = quality_val if quality_val and quality_val != "normal" else None

        # ProgrammableSpeaker, small-lamp and power poles do not accept 'direction' kwarg
        if entity.type in ("programmable-speaker", "small-lamp",
                           "small-electric-pole", "medium-electric-pole", "substation"):
            kwargs_ent: dict[str, Any] = {
                "id": entity.entity_id,
                "tile_position": pos,
            }
            if quality:
                kwargs_ent["quality"] = quality
            de = new_entity(entity.type, **kwargs_ent)
        else:
            de = new_entity(
                entity.type,
                id=entity.entity_id,
                tile_position=pos,
                direction=direction,
            )

        props = entity.properties

        if entity.type == "arithmetic-combinator":
            fow = props.get("first_operand_wires")
            sow = props.get("second_operand_wires")
            if _validate:
                kwargs: dict[str, Any] = {
                    "first_operand": _str_to_signal_ref(props.get("first_operand", "")),
                    "operation": props.get("operation", "*"),
                    "second_operand": _str_to_signal_ref(props.get("second_operand", 0)),
                    "output_signal": _str_to_signal_ref(props.get("output_signal", "")),
                }
                if fow:
                    kwargs["first_operand_wires"] = set(fow)
                if sow:
                    kwargs["second_operand_wires"] = set(sow)
                de.set_arithmetic_condition(**kwargs)
            else:
                # Fast path: bypass attrs validators
                object.__setattr__(
                    de, "first_operand",
                    _to_signal_id(_str_to_signal_ref(props.get("first_operand", ""))),
                )
                object.__setattr__(
                    de, "arithmetic_operation", props.get("operation", "*"),
                )
                object.__setattr__(
                    de, "second_operand",
                    _to_signal_id(_str_to_signal_ref(props.get("second_operand", 0))),
                )
                object.__setattr__(
                    de, "output_signal",
                    _to_signal_id(_str_to_signal_ref(props.get("output_signal", ""))),
                )
                if fow:
                    object.__setattr__(de, "first_operand_wires", set(fow))
                if sow:
                    object.__setattr__(de, "second_operand_wires", set(sow))

        elif entity.type == "decider-combinator":
            if _validate:
                conditions: list[Any] = []
                for c in props.get("conditions", []):
                    cond_kwargs: dict[str, Any] = {
                        "first_signal": _str_to_signal_ref(c["first"]),
                        "comparator": c["op"],
                    }
                    if "second_signal" in c:
                        cond_kwargs["second_signal"] = _str_to_signal_ref(c["second_signal"])
                    elif "constant" in c:
                        cond_kwargs["constant"] = c["constant"]
                    if "compare_type" in c:
                        cond_kwargs["compare_type"] = c["compare_type"]
                    conditions.append(de.Condition(**cond_kwargs))
                de.conditions = conditions

                outputs: list[Any] = []
                for o in props.get("outputs", []):
                    sig_str = o.get("signal", "")
                    if "@" in sig_str:
                        name, quality = sig_str.split("@", 1)
                        sig_ref = {"name": name, "quality": quality}
                    else:
                        sig_ref = sig_str
                    outputs.append(
                        de.Output(
                            signal=sig_ref,
                            copy_count_from_input=o.get("copy_count", False),
                            constant=o.get("constant", 0),
                        )
                    )
                de.outputs = outputs
            else:
                # Fast path: build conditions/outputs with object.__setattr__
                conds: list[Any] = []
                for c in props.get("conditions", []):
                    cond = Condition.__new__(Condition)
                    object.__setattr__(
                        cond, "first_signal",
                        _to_signal_id(_str_to_signal_ref(c["first"])),
                    )
                    object.__setattr__(cond, "comparator", c["op"])
                    if "second_signal" in c:
                        object.__setattr__(
                            cond, "second_signal",
                            _to_signal_id(_str_to_signal_ref(c["second_signal"])),
                        )
                    elif "constant" in c:
                        object.__setattr__(cond, "constant", c["constant"])
                    if "compare_type" in c:
                        object.__setattr__(cond, "compare_type", c["compare_type"])
                    conds.append(cond)
                object.__setattr__(de, "conditions", conds)

                outs: list[Any] = []
                for o in props.get("outputs", []):
                    sig_str = o.get("signal", "")
                    if "@" in sig_str:
                        name, quality = sig_str.split("@", 1)
                        sig_ref = {"name": name, "quality": quality}
                    else:
                        sig_ref = sig_str
                    sig_ref = _to_signal_id(sig_ref)
                    out_obj = de.Output.__new__(de.Output)
                    object.__setattr__(out_obj, "signal", sig_ref)
                    object.__setattr__(
                        out_obj, "copy_count_from_input",
                        o.get("copy_count", False),
                    )
                    object.__setattr__(out_obj, "constant", o.get("constant", 0))
                    outs.append(out_obj)
                object.__setattr__(de, "outputs", outs)

        elif entity.type == "constant-combinator":
            for slot, sig in enumerate(props.get("signals", [])):
                quality = sig.get("quality")
                if _validate:
                    de.set_signal(
                        slot,
                        sig["name"],
                        sig.get("value", 0),
                        quality if quality else None,
                    )
                else:
                    # Fast path: manipulate the signals dict directly
                    signal_dict = {"name": sig["name"], "value": sig.get("value", 0)}
                    if quality:
                        signal_dict["quality"] = quality
                    cbn = getattr(de, "control_behavior", None)
                    if cbn is not None:
                        sigs = getattr(cbn, "signals", None)
                        if sigs is not None and slot < len(sigs):
                            # Replace in place
                            for k, v in signal_dict.items():
                                sigs[slot][k] = v
                        else:
                            de.set_signal(
                                slot,
                                sig["name"],
                                sig.get("value", 0),
                                quality if quality else None,
                            )
                    else:
                        de.set_signal(
                            slot,
                            sig["name"],
                            sig.get("value", 0),
                            quality if quality else None,
                        )

        elif entity.type == "programmable-speaker":
            if _validate:
                de.instrument_name = props.get("instrument", "piano")
                de.note_name = props.get("note", "")
                de.volume_signal = {
                    "name": props.get("vol_signal", ""),
                    "quality": props.get("vol_quality", "normal"),
                }
                de.volume_controlled_by_signal = True
                de.allow_polyphony = props.get("polyphony", True)
                de.circuit_enabled = props.get("circuit_enabled", True)
                de.set_circuit_condition(
                    first_operand="signal-no-entry", comparator="=", second_operand=0,
                )
            else:
                # Fast path: object.__setattr__ + cached condition.
                # Simple attributes bypass attrs validators; volume_signal
                # uses the normal setter (needs converter for SignalID).
                object.__setattr__(
                    de, "instrument_name", props.get("instrument", "piano"),
                )
                object.__setattr__(de, "note_name", props.get("note", ""))
                de.volume_signal = {
                    "name": props.get("vol_signal", ""),
                    "quality": props.get("vol_quality", "normal"),
                }
                object.__setattr__(de, "volume_controlled_by_signal", True)
                object.__setattr__(
                    de, "allow_polyphony", props.get("polyphony", True),
                )
                object.__setattr__(
                    de, "circuit_enabled", props.get("circuit_enabled", True),
                )
                object.__setattr__(
                    de, "circuit_condition", _cached_speaker_condition,
                )

        elif entity.type == "small-lamp":
            if _validate:
                de.always_on = props.get("always_on", False)
                de.use_colors = props.get("use_colors", False)
                if de.use_colors:
                    de.color_mode = 2
                    color_sig = props.get("color_signal")
                    if color_sig:
                        # Handle dict or string
                        if isinstance(color_sig, dict):
                            de.rgb_signal = color_sig
                        else:
                            de.rgb_signal = _str_to_signal_ref(str(color_sig))

                ce = props.get("circuit_enabled", False)
                de.circuit_enabled = ce
                if ce:
                    cond = props.get("condition")
                    if cond:
                        kwargs_set: dict[str, Any] = {
                            "first_operand": _str_to_signal_ref(cond["first"]),
                            "comparator": cond["op"],
                        }
                        if "second_signal" in cond:
                            kwargs_set["second_operand"] = _str_to_signal_ref(
                                cond["second_signal"],
                            )
                        elif "constant" in cond:
                            kwargs_set["second_operand"] = cond["constant"]
                        de.set_circuit_condition(**kwargs_set)
            else:
                # Fast path: object.__setattr__ for simple attributes,
                # skip circuit_condition when circuit_enabled is False.
                # rgb_signal uses the normal setter (needs SignalID converter).
                object.__setattr__(
                    de, "always_on", props.get("always_on", False),
                )
                use_colors = props.get("use_colors", False)
                object.__setattr__(de, "use_colors", use_colors)
                if use_colors:
                    object.__setattr__(de, "color_mode", LampColorMode.PACKED_RGB)
                    color_sig = props.get("color_signal")
                    if color_sig:
                        if isinstance(color_sig, dict):
                            de.rgb_signal = color_sig
                        else:
                            de.rgb_signal = _str_to_signal_ref(str(color_sig))

                ce = props.get("circuit_enabled", False)
                object.__setattr__(de, "circuit_enabled", ce)
                if ce:
                    cond = props.get("condition")
                    if cond:
                        # Build a minimal Condition with object.__setattr__
                        lamp_cond = Condition.__new__(Condition)
                        object.__setattr__(
                            lamp_cond, "first_signal",
                            _to_signal_id(_str_to_signal_ref(cond["first"])),
                        )
                        object.__setattr__(
                            lamp_cond, "comparator", cond["op"],
                        )
                        if "second_signal" in cond:
                            object.__setattr__(
                                lamp_cond, "second_signal",
                                _to_signal_id(_str_to_signal_ref(cond["second_signal"])),
                            )
                        elif "constant" in cond:
                            object.__setattr__(
                                lamp_cond, "constant", cond["constant"],
                            )
                        object.__setattr__(
                            de, "circuit_condition", lamp_cond,
                        )

        elif entity.type in ("small-electric-pole", "medium-electric-pole", "substation"):
            # Power poles have no combinator settings; quality is set at construction
            pass

        bp.entities.append(de)

    # ── 2. Materialise networks → pairwise connections ─────────────
    for net in lb.networks:
        eps = list(net.endpoints)
        if net.color == "copper":
            # Copper networks connect power poles via neighbours
            # Build id→entity map from the draftsman blueprint
            pole_map: dict[str, Any] = {}
            for e in bp.entities:
                if getattr(e, "id", None) and e.name in (
                    "small-electric-pole", "medium-electric-pole", "substation",
                ):
                    pole_map[e.id] = e
            pole_ids_in_net = [
                ep.entity_id for ep in eps
                if ep.entity_id in pole_map
            ]
            # Chain neighbours
            for i in range(len(pole_ids_in_net) - 1):
                e1 = pole_map[pole_ids_in_net[i]]
                e2 = pole_map[pole_ids_in_net[i + 1]]
                from draftsman.classes.association import Association
                if not any(a() is e2 for a in e1.neighbours):
                    e1.neighbours.append(Association(e2))
                if not any(a() is e1 for a in e2.neighbours):
                    e2.neighbours.append(Association(e1))
            continue
        # Materialise the network into pairwise connections.
        #
        # Three paths, in priority order:
        #   1. Pre-wired (net.prewired_pairs set) — use directly.
        #   2. Large network with positioned endpoints — chain in
        #      position-sorted order (fast path, O(n log n)).
        #   3. Generic — run _wire_horizontal_first (O(n³) worst-case
        #      but preserves wire-length and non-crossing properties).
        if net.prewired_pairs is not None:
            pairs = net.prewired_pairs
        elif len(eps) > 500 and all(
            lb.entities.get(ep.entity_id) is not None
            and _endpoint_position(ep, lb) != (0, 0)
            for ep in eps
        ):
            # Fast path: sort by position and chain in order.
            # This is correct when entities form a contiguous grid
            # and are already position-sorted (guaranteed by the
            # composer's topological sort + position ordering).
            sorted_eps = _sort_endpoints_by_position(eps, lb)
            pairs = [
                (sorted_eps[i], sorted_eps[i + 1])
                for i in range(len(sorted_eps) - 1)
            ]
        else:
            pairs = _wire_horizontal_first(eps, lb)
        for a, b in pairs:
            bp.add_circuit_connection(
                net.color, a.entity_id, b.entity_id,
                side_1=a.port, side_2=b.port,
            )

    return bp


def _str_to_signal_ref(s: str | int) -> dict[str, str] | str | int:
    """Parse ``"name@quality"`` back to a draftsman signal dict, or plain string/int."""
    if isinstance(s, int):
        return s
    if "@" in s:
        name, quality = s.split("@", 1)
        return {"name": name, "quality": quality}
    return s


# ═══════════════════════════════════════════════════════════════════════
# Wire-topology debug assertions (draftsman Blueprint level)
# ═══════════════════════════════════════════════════════════════════════


def check_wire_topology(bp: Any, *, label: str = "") -> list[str]:
    """Check low-level wire invariants on a draftsman Blueprint.

    Verifies that the explicit ``bp.wires`` list is self-consistent and
    respects Factorio's circuit connection limits.

    Returns a list of error strings.  An empty list means all checks pass.

    Invariants checked
    ------------------
    * No self-loops (wire from an entity to itself).
    * No duplicate wires (same pair of entities, colour, and sides).
    * Degree bound: each (entity, colour, side) port has ≤ 2 connections
      (Factorio combinators have exactly two connection points per side
      per colour).
    * Wire count sanity: if the blueprint has *N* entities on a colour
      network with no singleton islands, expect ≥ N−1 edges on that colour.
    * Connectivity: for each colour, entities that share wire(s) form
      exactly one connected component per connected subgraph (no orphan
      islands within a colour unless unavoidable).
    """
    errors: list[str] = []

    wires = getattr(bp, "wires", None)
    if not wires:
        return errors

    # ── Build adjacency per colour ────────────────────────────────
    # adj[colour][eid] = set of (other_eid, side_at_other)
    adj: dict[str, dict[str, set[tuple[str, str]]]] = {}
    # Degree counter: deg[colour][(eid, side)] = int
    deg: dict[str, dict[tuple[str, str], int]] = {}
    seen_wires: set[tuple[str, str, str, str, str]] = set()
    wire_count_by_colour: dict[str, int] = {}

    for w in wires:
        assoc1, conn1, assoc2, conn2 = w
        e1 = assoc1() if callable(assoc1) else assoc1
        e2 = assoc2() if callable(assoc2) else assoc2
        eid1: str = getattr(e1, "id", None) or f"_e{id(e1)}"
        eid2: str = getattr(e2, "id", None) or f"_e{id(e2)}"

        wt1 = conn1.value if hasattr(conn1, "value") else int(conn1)
        wt2 = conn2.value if hasattr(conn2, "value") else int(conn2)
        colour = "red" if wt1 % 2 == 1 else "green"
        side1 = "input" if wt1 <= 2 else "output"
        side2 = "input" if wt2 <= 2 else "output"

        # Self-loop on the *same port* is meaningless (input→input, output→output
        # on the same entity).  Same entity but *different ports* (output→input)
        # is a valid Factorio pattern (e.g. clock loopback) — process normally.
        if eid1 == eid2 and side1 == side2:
            errors.append(
                f"Self-loop on same port: {eid1!r}:{side1} ↔ {eid2!r}:{side2} ({colour})"
            )
            continue

        # Duplicate check
        wire_key = (eid1, eid2, colour, side1, side2)
        wire_key_rev = (eid2, eid1, colour, side2, side1)
        if wire_key in seen_wires or wire_key_rev in seen_wires:
            errors.append(
                f"Duplicate wire: {eid1!r} ↔ {eid2!r} ({colour}, "
                f"{side1}↔{side2})"
            )
        seen_wires.add(wire_key)

        # Adjacency
        adj.setdefault(colour, {}).setdefault(eid1, set()).add((eid2, side2))
        adj.setdefault(colour, {}).setdefault(eid2, set()).add((eid1, side1))

        # Degree tracking
        deg.setdefault(colour, {}).setdefault((eid1, side1), 0)
        deg.setdefault(colour, {}).setdefault((eid2, side2), 0)
        deg[colour][(eid1, side1)] += 1
        deg[colour][(eid2, side2)] += 1

        wire_count_by_colour[colour] = wire_count_by_colour.get(colour, 0) + 1

    # ── Degree bound check (Factorio: ≤ 2 per port per colour) ────
    for colour, colour_deg in deg.items():
        for (eid, side), d in colour_deg.items():
            if d > 2:
                errors.append(
                    f"Degree overflow: {eid!r} {side} on {colour} has "
                    f"{d} connections (max 2)"
                )

    # ── Connectivity check per colour ─────────────────────────────
    for colour, colour_adj in adj.items():
        if not colour_adj:
            continue
        eids = set(colour_adj.keys())
        visited: set[str] = set()
        components: list[set[str]] = []

        for eid in sorted(eids):
            if eid in visited:
                continue
            stack = [eid]
            comp: set[str] = set()
            while stack:
                cur = stack.pop()
                if cur in visited:
                    continue
                visited.add(cur)
                comp.add(cur)
                for neighbour, _ in colour_adj.get(cur, set()):
                    if neighbour not in visited:
                        stack.append(neighbour)
            components.append(comp)

        if len(components) > 1:
            sizes = [len(c) for c in components]
            errors.append(
                f"{colour} wires: {len(eids)} entities split into "
                f"{len(components)} component(s) (sizes: {sizes})"
            )

    # ── Wire count sanity ─────────────────────────────────────────
    for colour, eid_set_adj in adj.items():
        n_entities = len(eid_set_adj)
        n_wires = wire_count_by_colour.get(colour, 0)
        # A connected graph needs at least n-1 edges.  More edges
        # than 2(n-1) suggests redundant wiring (cycles).
        if n_entities >= 2 and n_wires < n_entities - 1:
            errors.append(
                f"{colour}: {n_wires} wire(s) for {n_entities} entities "
                f"(need ≥ {n_entities - 1} for connectivity)"
            )
        if n_wires > 2 * (n_entities - 1) and n_entities >= 3:
            errors.append(
                f"{colour}: {n_wires} wire(s) for {n_entities} entities "
                f"(excessive; ≤ {2 * (n_entities - 1)} expected for simple "
                f"chains/trees)"
            )

    return errors


def assert_wire_topology(bp: Any, *, label: str = "") -> None:
    """Assert that *bp* has valid wire topology (no-op when ``__debug__`` is False)."""
    if not __debug__:
        return
    errs = check_wire_topology(bp, label=label)
    if errs:
        prefix = f" [{label}]" if label else ""
        raise AssertionError(
            f"Wire topology violation(s){prefix}:\n" +
            "\n".join(f"  - {e}" for e in errs)
        )
