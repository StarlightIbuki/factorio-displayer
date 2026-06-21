"""Factorio Display — video encoder and audio decoder blueprint builders
for in-game Factorio RGB displays and programmable-speaker audio playback."""

# ═══════════════════════════════════════════════════════════════════════
# Patch: suppress draftsman's debug ``print("add_section")``
# ═══════════════════════════════════════════════════════════════════════
# draftsman's ConstantCombinator.add_section() contains a bare
# ``print("add_section")`` (constant_combinator.py:134) that fires on
# every signal-slot creation.  With 13 CCs × 60 slots per audio decoder
# that's ~780 syscalls per build_audio_decoder() call, wasting ~1.2s
# per build on Windows due to console I/O overhead.
#
# We monkey-patch it at import time so the fix applies to both the
# application and the test suite.


def _patch_draftsman_leaking_debug_log() -> None:
    """Silence the stray ``print("add_section")`` in draftsman."""
    try:
        from draftsman.prototypes.constant_combinator import ConstantCombinator

        _original_add_section = ConstantCombinator.add_section

        def _quiet_add_section(self, group=None, index=None, active=True):
            # Capture the return value without letting print() reach stdout.
            import io
            import sys
            old_stdout = sys.stdout
            try:
                sys.stdout = io.StringIO()
                return _original_add_section(self, group=group, index=index, active=active)
            finally:
                sys.stdout = old_stdout

        ConstantCombinator.add_section = _quiet_add_section
    except Exception:
        pass  # draftsman may not be installed; that's fine


_patch_draftsman_leaking_debug_log()
del _patch_draftsman_leaking_debug_log

try:
    from .build._generated import (
        CLOCK_SIGNAL,
        DISPLAY_HEIGHT,
        DISPLAY_WIDTH,
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
    DISPLAY_HEIGHT = 26
    DISPLAY_WIDTH = 28
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
            QUALITIES,
            _dev_pool,
        )
        SIGNAL_POOL = _dev_mapping.base_signals
    except Exception:  # pylint: disable=broad-exception-caught — fallback for dev installs
        pass

__all__ = [
    "CLOCK_SIGNAL",
    "DISPLAY_HEIGHT",
    "DISPLAY_WIDTH",
    "LOUDNESS_SIGNAL",
    "POOL_HASH",
    "QUALITIES",
    "SIGNAL_POOL",
    "__version__",
]
