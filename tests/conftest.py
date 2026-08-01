"""Shared test fixtures for factorio-display audio tests."""

from __future__ import annotations

import pytest


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "perf: performance regression test (can be skipped with -m 'not perf')"
    )

from factorio_display.integer2signal.pool import get_filtered_pool


@pytest.fixture(scope="session")
def sample_signal_pool() -> list[str]:
    """A small deterministic pool of real Factorio item signals for testing."""
    return [
        "wooden-chest", "iron-chest", "steel-chest", "storage-tank",
        "transport-belt", "fast-transport-belt", "express-transport-belt",
        "underground-belt", "fast-underground-belt", "express-underground-belt",
        "splitter", "fast-splitter", "express-splitter",
        "burner-inserter", "inserter", "long-handed-inserter", "fast-inserter",
        "bulk-inserter", "stack-inserter", "small-electric-pole",
        "medium-electric-pole", "big-electric-pole", "substation",
        "pipe", "pipe-to-ground", "pump",
        "rail", "rail-signal", "rail-chain-signal",
        "locomotive", "cargo-wagon", "fluid-wagon",
        "car", "tank", "logistic-robot", "construction-robot",
        "active-provider-chest", "passive-provider-chest",
        "storage-chest", "buffer-chest",
    ]


@pytest.fixture(scope="session")
def large_signal_pool() -> list[str]:
    """A pool large enough to fill one 720-cell page (144 base signals × 5 qualities).

    Uses real Factorio item names from draftsman's items.raw registry.
    """
    try:
        pool = get_filtered_pool("signal-clock")
        # Ensure we have at least 144 base signals
        if len(pool) < 144:
            # Fallback: pad with dummy names
            pool = list(pool)
            for i in range(144 - len(pool)):
                pool.append(f"dummy-signal-{i}")
        return pool[:144]  # exactly 144 = 720 / 5
    except Exception:  # pylint: disable=broad-exception-caught — test fixture fallback
        return [f"test-signal-{i:04d}" for i in range(144)]


@pytest.fixture(scope="session")
def sample_qualities() -> list[str]:
    """The 5 Space Age quality tiers."""
    return ["normal", "uncommon", "rare", "epic", "legendary"]


@pytest.fixture
def silent_tick() -> list[int]:
    """One tick of all-zero loudness (48 values)."""
    return [0] * 48


# ═══════════════════════════════════════════════════════════════════════
# Logical-blueprint validation helpers
# ═══════════════════════════════════════════════════════════════════════


def validate_blueprint_via_logical(
    bp_str: str,
    *,
    require_positions: bool = True,
    require_wiring: bool = True,
    require_valid_networks: bool = True,
) -> dict:
    """Validate a blueprint string by round-tripping through LogicalBlueprint.

    Parses *bp_str* with draftsman, converts to a :class:`LogicalBlueprint`,
    then checks structural invariants.  Returns a dict of inspection results.

    Parameters
    ----------
    bp_str : str
        The Factorio blueprint string (``0e...``).
    require_positions : bool
        If True, every entity must have a non-None position.
    require_wiring : bool
        If True, at least one network must exist (blueprint is wired).
    require_valid_networks : bool
        If True, check network consistency invariants.

    Returns
    -------
    dict
        Keys: ``entity_count``, ``network_count``, ``errors`` (list[str]).
    """
    from draftsman.blueprintable import Blueprint

    from factorio_display.logical_blueprint import from_draftsman

    bp = Blueprint.from_string(bp_str)
    lb = from_draftsman(bp)

    errors: list[str] = []
    entity_ids = set(lb.entities.keys())

    # ── 1. Entity-level checks ───────────────────────────────────
    for eid, ent in lb.entities.items():
        if ent.type not in (
            "arithmetic-combinator", "decider-combinator",
            "constant-combinator", "programmable-speaker", "small-lamp",
            "small-electric-pole", "medium-electric-pole", "substation",
        ):
            errors.append(f"Entity {eid!r}: unknown type {ent.type!r}")

        if require_positions and ent.position is None:
            errors.append(f"Entity {eid!r}: missing position")

    # ── 2. Network consistency ───────────────────────────────────
    if require_valid_networks:
        seen_endpoints: set[tuple[str, str, str]] = set()  # (entity_id, port, color)
        for net in lb.networks:
            if not net.endpoints:
                errors.append(f"Network {net.network_id!r}: empty (no endpoints)")

            for ep in net.endpoints:
                if ep.entity_id not in entity_ids:
                    errors.append(
                        f"Network {net.network_id!r}: endpoint {ep.to_string()!r} "
                        f"references non-existent entity {ep.entity_id!r}"
                    )

                key = (ep.entity_id, ep.port, net.color)
                if key in seen_endpoints:
                    errors.append(
                        f"Endpoint {ep.to_string()!r} appears in multiple "
                        f"{net.color} networks"
                    )
                seen_endpoints.add(key)

        # Check no cross-color contamination within a network
        for net in lb.networks:
            if net.color not in ("red", "green"):
                errors.append(f"Network {net.network_id!r}: invalid color {net.color!r}")

    if require_wiring and len(lb.networks) == 0:
        errors.append("Blueprint has no circuit networks")

    return {
        "entity_count": len(lb.entities),
        "network_count": len(lb.networks),
        "errors": errors,
    }


# ═══════════════════════════════════════════════════════════════════════
# Logical → draftsman → logical connectivity round-trip validator
# ═══════════════════════════════════════════════════════════════════════

# Factorio circuit wire max Chebyshev distance.  Draftsman warns above
# this threshold (ConnectionDistanceWarning, 9 tiles for combinators).
# We allow a bit more headroom for entity-to-entity variation.
_MAX_CIRCUIT_WIRE_DISTANCE = 18


def validate_logical_connectivity(
    lb: "LogicalBlueprint",
    *,
    max_wire_distance: int = _MAX_CIRCUIT_WIRE_DISTANCE,
) -> dict:
    """Round-trip a :class:`LogicalBlueprint` through draftsman and back,
    then validate that every materialised wire is feasible and faithfully
    implements the logical networks.

    1.  ``to_draftsman(lb)`` → draftsman ``Blueprint``.
    2.  ``from_draftsman(bp)`` → ``LogicalBlueprint`` (lb2).
    3.  Check that each logical network's endpoints appear in the
        materialised wires and form exactly one connected component
        per colour.
    4.  Check that every wire connects endpoints at a Chebyshev
        distance ≤ *max_wire_distance*.
    5.  Check that materialised wires only connect endpoints that
        belong to the same logical network (no cross-network leaks).

    Parameters
    ----------
    lb : LogicalBlueprint
        The logical blueprint to validate.
    max_wire_distance : int
        Maximum allowed Chebyshev distance for a circuit wire.

    Returns
    -------
    dict
        Keys: ``entity_count``, ``wire_count``, ``errors`` (list[str]).
    """
    import warnings

    from draftsman.warning import ConnectionDistanceWarning

    from factorio_display.logical_blueprint import (
        _chebyshev,
        _endpoint_position,
        from_draftsman,
        to_draftsman,
    )

    errors: list[str] = []

    # ── 1. Materialise → parse ────────────────────────────────────
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        bp = to_draftsman(lb)
    lb2 = from_draftsman(bp)

    # ── 2. Collect materialised wires by colour ───────────────────
    # Build endpoint-aware adjacency: (colour, (entity_id, port)) → set of
    # (other_entity_id, other_port).  A materialised wire always joins a
    # specific endpoint (entity_id + port) on one colour.  Using entity ids
    # alone would misattribute wires for entities that participate in
    # multiple logical networks of the same colour via different ports.
    adj: dict[tuple[str, tuple[str, str]], set[tuple[str, str]]] = {}
    wire_count = 0

    for w in getattr(bp, "wires", []):
        assoc1, conn1, assoc2, conn2 = w
        e1 = assoc1() if callable(assoc1) else assoc1
        e2 = assoc2() if callable(assoc2) else assoc2
        eid1 = getattr(e1, "id", None) or f"_e{id(e1)}"
        eid2 = getattr(e2, "id", None) or f"_e{id(e2)}"

        wt1 = conn1.value if hasattr(conn1, "value") else int(conn1)
        wt2 = conn2.value if hasattr(conn2, "value") else int(conn2)
        colour = "red" if wt1 % 2 == 1 else "green"
        side1 = "input" if wt1 in (1, 2) else "output"
        side2 = "input" if wt2 in (1, 2) else "output"

        ep1 = (eid1, side1)
        ep2 = (eid2, side2)
        adj.setdefault((colour, ep1), set()).add(ep2)
        adj.setdefault((colour, ep2), set()).add(ep1)
        wire_count += 1

    # ── 3. Wire distance feasibility ─────────────────────────────
    # Also collect ConnectionDistanceWarnings (they indicate infeasible wires).
    for wm in recorded:
        if isinstance(wm.message, ConnectionDistanceWarning):
            errors.append(f"Wire distance warning: {wm.message}")

    # Explicit distance check using lb entity positions
    for w in getattr(bp, "wires", []):
        assoc1, conn1, assoc2, conn2 = w
        e1 = assoc1() if callable(assoc1) else assoc1
        e2 = assoc2() if callable(assoc2) else assoc2
        eid1 = getattr(e1, "id", None) or f"_e{id(e1)}"
        eid2 = getattr(e2, "id", None) or f"_e{id(e2)}"

        # Look up positions in the *original* lb (before round-trip)
        ent1 = lb.entities.get(eid1)
        ent2 = lb.entities.get(eid2)
        if ent1 and ent2 and ent1.position and ent2.position:
            d = _chebyshev(ent1.position, ent2.position)
            if d > max_wire_distance:
                errors.append(
                    f"Wire {eid1!r} ↔ {eid2!r}: distance {d} exceeds "
                    f"max {max_wire_distance}"
                )

    # ── 4. Network → connected-component correspondence ──────────
    for net in lb.networks:
        if net.color == "copper":
            continue  # power networks use neighbour lists, not circuit wires

        # A logical network is defined by its (entity_id, port) endpoints.
        net_eps: set[tuple[str, str]] = {(ep.entity_id, ep.port) for ep in net.endpoints}
        if not net_eps:
            continue

        visited: set[tuple[str, str]] = set()
        components: list[set[tuple[str, str]]] = []

        for ep in net_eps:
            if ep in visited:
                continue
            # BFS over endpoint nodes
            stack = [ep]
            comp: set[tuple[str, str]] = set()
            while stack:
                cur = stack.pop()
                if cur in visited:
                    continue
                visited.add(cur)
                comp.add(cur)
                for nb in adj.get((net.color, cur), set()):
                    if nb in net_eps and nb not in visited:
                        stack.append(nb)
            components.append(comp)

        if len(components) == 0:
            if net.endpoints:
                errors.append(
                    f"Network {net.network_id!r} ({net.color}): "
                    f"{len(net.endpoints)} endpoint(s) have no materialised wires"
                )
        elif len(components) > 1:
            sizes = [len(c) for c in components]
            errors.append(
                f"Network {net.network_id!r} ({net.color}): "
                f"{len(net.endpoints)} endpoint(s) split into "
                f"{len(components)} connected component(s) "
                f"(sizes: {sizes})"
            )

    # ── 5. No cross-network wire leaks ───────────────────────────
    # Build lookup: (colour, entity_id, port) → logical network id
    ep_colour_to_net: dict[tuple[str, str, str], str] = {}
    for net in lb.networks:
        for ep in net.endpoints:
            ep_colour_to_net[(net.color, ep.entity_id, ep.port)] = net.network_id

    for w in getattr(bp, "wires", []):
        assoc1, conn1, assoc2, conn2 = w
        e1 = assoc1() if callable(assoc1) else assoc1
        e2 = assoc2() if callable(assoc2) else assoc2
        eid1 = getattr(e1, "id", None) or f"_e{id(e1)}"
        eid2 = getattr(e2, "id", None) or f"_e{id(e2)}"
        wt1 = conn1.value if hasattr(conn1, "value") else int(conn1)
        wt2 = conn2.value if hasattr(conn2, "value") else int(conn2)
        colour = "red" if wt1 % 2 == 1 else "green"
        side1 = "input" if wt1 in (1, 2) else "output"
        side2 = "input" if wt2 in (1, 2) else "output"

        net1 = ep_colour_to_net.get((colour, eid1, side1))
        net2 = ep_colour_to_net.get((colour, eid2, side2))
        if net1 is not None and net2 is not None and net1 != net2:
            errors.append(
                f"Wire {eid1!r}:{side1} ↔ {eid2!r}:{side2} ({colour}) leaks between "
                f"networks {net1!r} and {net2!r}"
            )

    return {
        "entity_count": len(lb.entities),
        "wire_count": wire_count,
        "errors": errors,
    }


# ═══════════════════════════════════════════════════════════════════════
# Warning-check helpers
# ═══════════════════════════════════════════════════════════════════════

# Warning types that are *expected* in normal operation and should NOT
# cause test failures.  These are known non-fatal warnings from draftsman.
KNOWN_NON_FATAL_WARNINGS = frozenset({
    "UnknownNoteWarning",
    "UnknownSignalWarning",
    "ConnectionDistanceWarning",
    "ConnectionSideWarning",
    "OverlappingObjectsWarning",
})


def assert_no_unexpected_warnings(recorded_warnings: list) -> None:
    """Assert that all recorded warnings are in the known-safe set.

    Call this after a test action that may produce draftsman warnings.
    Any warning whose type name is NOT in ``KNOWN_NON_FATAL_WARNINGS``
    will cause an assertion failure.

    Usage::

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = some_blueprint_builder()
        assert_no_unexpected_warnings(w)
    """
    import warnings

    unexpected: list[warnings.WarningMessage] = []
    for wm in recorded_warnings:
        wtype = type(wm.message).__name__
        if wtype not in KNOWN_NON_FATAL_WARNINGS:
            unexpected.append(wm)

    if unexpected:
        msgs = "\n".join(
            f"  {type(wm.message).__name__}: {wm.message}"
            for wm in unexpected
        )
        raise AssertionError(
            f"Unexpected warnings were raised:\n{msgs}"
        )


@pytest.fixture
def full_volume_tick() -> list[int]:
    """One tick with all speakers at max volume (100)."""
    return [100] * 48


@pytest.fixture
def ramp_tick() -> list[int]:
    """One tick with linearly increasing loudness 0..47."""
    return list(range(48))


# ═══════════════════════════════════════════════════════════════════════
# Session-scoped blueprint fixtures — built once, reused across tests
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def audio_decoder_bp_str() -> str:
    """Audio decoder blueprint string — built once per session."""
    from factorio_display.audio.player_blueprint import build_audio_decoder
    return build_audio_decoder(name="TestSession")


@pytest.fixture(scope="session")
def audio_decoder_bp(audio_decoder_bp_str: str):
    """Parsed audio decoder Blueprint — built once per session."""
    from draftsman.blueprintable import Blueprint
    return Blueprint.from_string(audio_decoder_bp_str)


@pytest.fixture(scope="session")
def audio_decoder_debug_bp_str() -> str:
    """Audio decoder with debug lamps — built once per session."""
    from factorio_display.audio.player_blueprint import build_audio_decoder
    return build_audio_decoder(name="TestDebug", debug_lamps=True)


@pytest.fixture(scope="session")
def audio_decoder_debug_bp(audio_decoder_debug_bp_str: str):
    """Parsed audio decoder with debug lamps — built once per session."""
    from draftsman.blueprintable import Blueprint
    return Blueprint.from_string(audio_decoder_debug_bp_str)
