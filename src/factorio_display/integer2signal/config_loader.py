"""Shared configuration loader — **build-time only**.

This module exists solely for the Hatchling build hook (:file:`hatch_build.py`)
to read :file:`config.toml` and bake its values into :file:`_generated.py`.
Runtime code must import constants from :mod:`factorio_display` (e.g.
``DISPLAY_WIDTH``, ``CLOCK_SIGNAL``) — never call :func:`load_config` at runtime.
"""

import tomllib


def load_config(path: str = "config.toml") -> dict:
    """Load and return the TOML configuration file."""
    with open(path, "rb") as f:
        return tomllib.load(f)
