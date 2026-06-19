"""Hatchling build hook — generates :file:`_generated.py` with pre-computed
display-unit blueprint and signal-pool hash so they are baked into the wheel."""

from __future__ import annotations

import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface  # pylint: disable=import-error


class CustomBuildHook(BuildHookInterface):  # pylint: disable=too-few-public-methods
    """Generate ``_generated.py`` before the wheel is assembled."""

    PLUGIN_NAME = "factorio_display_build"

    def initialize(self, _version: str, _build_data: dict) -> None:  # pylint: disable=missing-function-docstring
        root = Path(self.root)
        src_dir = root / "src"

        # Ensure the package source is importable during the build
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))

        from factorio_display.build._generate import (  # pylint: disable=import-outside-toplevel
            generate_resources,
            write_generated_module,
        )

        config_path = root / "config.toml"
        resources = generate_resources(str(config_path))

        generated_path = src_dir / "factorio_display" / "build" / "_generated.py"
        write_generated_module(generated_path, resources, _version)
