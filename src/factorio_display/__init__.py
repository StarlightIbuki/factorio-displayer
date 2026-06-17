"""Factorio Display — blueprint builder and video encoder for in-game RGB displays."""

from ._generated import DISPLAY_BLUEPRINT, POOL_HASH, VERSION

__version__ = VERSION

__all__ = ["DISPLAY_BLUEPRINT", "POOL_HASH", "__version__"]
