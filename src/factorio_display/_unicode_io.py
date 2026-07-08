"""Unicode-safe file I/O helpers for Windows.

On Windows, many C libraries (libsndfile, libpng via Pillow, mido's
underlying fopen) cannot open files whose paths contain non-ASCII
characters.  Python's built-in ``open()`` handles Unicode correctly,
so we read/write via Python file objects.

Usage
-----
    from ._unicode_io import mido_open, image_open, mido_save

    # Instead of:  mid = mido.MidiFile(path)
    mid = mido_open(path)

    # Instead of:  img = Image.open(path)
    img = image_open(path)

    # Instead of:  mid.save(path)
    mido_save(mid, path)
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import mido


def _needs_unicode_workaround(path: str | os.PathLike) -> bool:
    """Return True if *path* contains non-ASCII characters on Windows."""
    if sys.platform != "win32":
        return False
    try:
        return not os.fspath(path).isascii()
    except Exception:
        return False


# ── mido helpers ────────────────────────────────────────────────────────

def mido_open(path: str | os.PathLike, **kwargs: object) -> "mido.MidiFile":
    """Open a MIDI file with Unicode-safe path handling.

    Equivalent to ``mido.MidiFile(path)``, but works with non-ASCII
    paths on Windows.
    """
    import mido as _mido  # pylint: disable=import-outside-toplevel

    if _needs_unicode_workaround(path):
        with open(path, "rb") as fh:
            return _mido.MidiFile(file=fh, **kwargs)

    return _mido.MidiFile(filename=os.fspath(path), **kwargs)


def mido_save(mid: "mido.MidiFile", path: str | os.PathLike) -> None:
    """Save a MIDI file with Unicode-safe path handling.

    Equivalent to ``mid.save(path)``, but works with non-ASCII
    paths on Windows.
    """
    if _needs_unicode_workaround(path):
        buf = io.BytesIO()
        mid.save(file=buf)
        with open(path, "wb") as fh:
            fh.write(buf.getvalue())
        return

    mid.save(filename=os.fspath(path))


# ── Pillow helpers ──────────────────────────────────────────────────────

def image_open(path: str | os.PathLike):
    """Open an image with Unicode-safe path handling.

    Equivalent to ``Image.open(path)``, but works with non-ASCII
    paths on Windows.
    """
    from PIL import Image  # pylint: disable=import-outside-toplevel

    if _needs_unicode_workaround(path):
        with open(path, "rb") as fh:
            return Image.open(fh)

    return Image.open(os.fspath(path))
