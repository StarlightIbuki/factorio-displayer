"""Factorio Display — video encoder and audio decoder blueprint builders
for in-game Factorio RGB displays and programmable-speaker audio playback."""

from ._generated import DISPLAY_BLUEPRINT, POOL_HASH, VERSION

__version__ = VERSION

__all__ = ["DISPLAY_BLUEPRINT", "POOL_HASH", "__version__"]
