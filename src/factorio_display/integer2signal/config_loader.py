"""Shared configuration loader for the Factorio display project."""

import tomllib


def load_config(path: str = "config.toml") -> dict:
    """Load and return the TOML configuration file."""
    with open(path, "rb") as f:
        return tomllib.load(f)
