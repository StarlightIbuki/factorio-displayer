"""Factorio Display — video encoder and audio decoder blueprint builders
for in-game Factorio RGB displays and programmable-speaker audio playback."""

try:
    from .build._generated import DISPLAY_BLUEPRINT, POOL_HASH, VERSION
except ImportError:
    # build/_generated.py is created by the build hook at wheel-build time.
    # It won't exist during a fresh editable install or before the first build.
    DISPLAY_BLUEPRINT = ""
    POOL_HASH = ""
    VERSION = "0.0.0"

__version__ = VERSION

__all__ = ["DISPLAY_BLUEPRINT", "POOL_HASH", "__version__"]
