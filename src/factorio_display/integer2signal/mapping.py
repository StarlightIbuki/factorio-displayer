"""Bidirectional mapping between display pixel coordinates and Factorio signals.

Every pixel on the display is assigned a unique (signal name, quality)
pair.  This module provides forward (coord → signal) and reverse (signal → coords)
lookups, plus helpers to iterate pixels and export the signal manifest.
"""

from __future__ import annotations

import json
import math


def _signal_key(name: str, quality: str) -> str:
    """Create a stable lookup key from a signal name and quality level."""
    return f"{name}|{quality}"


def compute_chunking(
    width: int,
    height: int,
    pool: list[str],
    qualities: list[str],
) -> tuple[int, int]:
    """Compute vertical chunk splitting parameters.

    When a display has more pixels than the signal pool can address,
    it must be split into disconnected vertical chunks (full-width strips).
    Each chunk gets its own :class:`SignalMapping` with a subset of the pool.

    Returns ``(chunk_height, num_chunks)``.  If no chunking is needed
    (display fits within pool), returns ``(height, 1)``.
    """
    available = len(pool) * len(qualities)
    total_pixels = width * height
    if total_pixels > available and available >= width:
        chunk_height = available // width
        num_chunks = math.ceil(height / chunk_height)
    else:
        chunk_height = height
        num_chunks = 1
    return chunk_height, num_chunks


class SignalMapping:  # pylint: disable=too-many-instance-attributes
    """Maps every display pixel to a Factorio signal and back.

    All W×H pixels are mapped — there is no hole or cutout.
    The user supplies power poles in-game as needed.
    """

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        width: int,
        height: int,
        qualities: list[str],
        signal_pool: list[str],
    ) -> None:
        """Build the mapping from display parameters and a pool of available signals.

        Parameters
        ----------
        width : int
            Display grid width in tiles.
        height : int
            Display grid height in tiles.
        qualities : list[str]
            Space Age quality tiers (e.g. ``["normal","uncommon","rare","epic","legendary"]``).
        signal_pool : list[str]
            Pool of available signal names.  Only the first *N* required signals
            are used; the rest are ignored.
        """
        self.width: int = width
        self.height: int = height
        self.qualities: list[str] = qualities

        total_pixels = self.width * self.height
        required = math.ceil(total_pixels / len(self.qualities))

        if required > len(signal_pool):
            raise ValueError(
                f"Display requires {required} base signals "
                f"({total_pixels} px / {len(self.qualities)} qualities), "
                f"but only {len(signal_pool)} are available."
            )

        self.base_signals: list[str] = signal_pool[:required]

        # Forward: (x, y) → {"name": …, "quality": …}
        self._coord_to_signal: dict[tuple[int, int], dict[str, str]] = {}
        # Reverse: "name|quality" → [(x, y), …]
        self._signal_to_coords: dict[str, list[tuple[int, int]]] = {}

        self._build()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build(self) -> None:
        signal_idx, quality_idx = 0, 0
        num_qualities = len(self.qualities)

        for y in range(self.height):
            for x in range(self.width):
                sig: dict[str, str] = {
                    "name": self.base_signals[signal_idx],
                    "quality": self.qualities[quality_idx],
                }
                self._coord_to_signal[(x, y)] = sig

                key = _signal_key(sig["name"], sig["quality"])
                self._signal_to_coords.setdefault(key, []).append((x, y))

                quality_idx += 1
                if quality_idx >= num_qualities:
                    quality_idx = 0
                    signal_idx += 1

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_signal(self, x: int, y: int) -> dict[str, str] | None:
        """Return the ``{"name", "quality"}`` dict for pixel *(x, y)*, or None."""
        return self._coord_to_signal.get((x, y))

    def get_coords(self, name: str, quality: str) -> list[tuple[int, int]]:
        """Return all pixel coordinates that use the given signal + quality."""
        return self._signal_to_coords.get(_signal_key(name, quality), [])

    def iter_pixels(self):
        """Yield ``((x, y), signal_dict)`` for every pixel in row-major order."""
        yield from self._coord_to_signal.items()

    @property
    def pixel_count(self) -> int:
        """Total number of pixels in the mapping."""
        return len(self._coord_to_signal)

    def export_manifest(self, path: str = "signal_manifest.json") -> None:
        """Write the base signal list to a JSON manifest for other tools."""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.base_signals, f)

    # ------------------------------------------------------------------
    # Alternate constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_manifest(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        cls,
        width: int,
        height: int,
        qualities: list[str],
        manifest_path: str = "signal_manifest.json",
    ) -> "SignalMapping":
        """Build a SignalMapping using a previously exported manifest file.

        This is the preferred constructor for tools that consume the mapping
        (e.g. the video encoder) rather than generating it from scratch.
        """
        with open(manifest_path, encoding="utf-8") as f:
            base_signals = json.load(f)
        return cls(width, height, qualities, base_signals)
