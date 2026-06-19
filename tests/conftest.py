"""Shared test fixtures for factorio-display audio tests."""

from __future__ import annotations

import pytest

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


@pytest.fixture
def full_volume_tick() -> list[int]:
    """One tick with all speakers at max volume (100)."""
    return [100] * 48


@pytest.fixture
def ramp_tick() -> list[int]:
    """One tick with linearly increasing loudness 0..47."""
    return list(range(48))
