"""Factorio Display — video encoder and audio decoder blueprint builders
for in-game Factorio RGB displays and programmable-speaker audio playback."""

try:
    from .build._generated import (
        CLOCK_SIGNAL,
        DISPLAY_BLUEPRINT,
        DISPLAY_HEIGHT,
        DISPLAY_WIDTH,
        HOLE_BOTTOM_RIGHT,
        HOLE_TOP_LEFT,
        LOUDNESS_SIGNAL,
        POOL_HASH,
        QUALITIES,
        SIGNAL_POOL,
        VERSION,
    )
except ImportError:
    # build/_generated.py is created by the build hook at wheel-build time.
    # It won't exist during a fresh editable install or before the first build.
    CLOCK_SIGNAL = "signal-clock"
    DISPLAY_BLUEPRINT = ""
    DISPLAY_HEIGHT = 28
    DISPLAY_WIDTH = 28
    HOLE_BOTTOM_RIGHT = (14, 14)
    HOLE_TOP_LEFT = (13, 13)
    LOUDNESS_SIGNAL = "signal-info"
    POOL_HASH = ""
    QUALITIES = ["normal", "uncommon", "rare", "epic", "legendary"]
    SIGNAL_POOL: list[str] = []
    VERSION = "0.0.0"

__version__ = VERSION

# ---------------------------------------------------------------------------
# In an editable install (before the first build), _generated.py may not
# exist.  Compute SIGNAL_POOL on-the-fly from the Factorio item registry
# so that the encoder still works during development.
# ---------------------------------------------------------------------------
if not SIGNAL_POOL:
    try:
        from .integer2signal.pool import get_filtered_pool
        from .integer2signal.mapping import SignalMapping

        _dev_pool = get_filtered_pool(CLOCK_SIGNAL)
        _dev_mapping = SignalMapping(
            DISPLAY_WIDTH,
            DISPLAY_HEIGHT,
            HOLE_TOP_LEFT,
            HOLE_BOTTOM_RIGHT,
            QUALITIES,
            _dev_pool,
        )
        SIGNAL_POOL = _dev_mapping.base_signals
    except Exception:
        pass

__all__ = [
    "CLOCK_SIGNAL",
    "DISPLAY_BLUEPRINT",
    "DISPLAY_HEIGHT",
    "DISPLAY_WIDTH",
    "HOLE_BOTTOM_RIGHT",
    "HOLE_TOP_LEFT",
    "LOUDNESS_SIGNAL",
    "POOL_HASH",
    "QUALITIES",
    "SIGNAL_POOL",
    "__version__",
]
