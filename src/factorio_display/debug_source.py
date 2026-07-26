"""Debug utilities for tracking the source origin of logical/draftsman entities.

Provides :func:`entity_source` to capture ``path:line`` of the caller,
and helpers to attach that information to a :class:`LogicalEntity` or
draftsman entity so assertion failures can report where an entity was
created.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DEBUG_SRC_KEY = "_debug_src"
_DEBUG_TRACEBACK_KEY = "_debug_traceback"

_TRACE_ENABLED_ENV_VAR = "FACTORIO_DISPLAY_DEBUG_TRACE"


def _load_trace_enabled_from_env() -> bool:
    """Return whether tracing is enabled from the environment variable.

    * ``FACTORIO_DISPLAY_DEBUG_TRACE=1`` enables trace capture.
    * ``FACTORIO_DISPLAY_DEBUG_TRACE=0`` disables trace capture.
    * An unset/empty variable defaults to enabled so existing behaviour is
      preserved.
    """
    value = os.environ.get(_TRACE_ENABLED_ENV_VAR, "1").strip()
    return value != "" and value.lower() not in {"0", "false", "no", "off"}


#: Global switch controlling whether debug source/trace metadata is captured.
#: Disable this at runtime (or via ``FACTORIO_DISPLAY_DEBUG_TRACE=0``) to
#: reduce blueprint size and memory overhead when traceability is not needed.
TRACE_ENABLED: bool = _load_trace_enabled_from_env()


def is_trace_enabled() -> bool:
    """Return the current global trace-enabled state."""
    return TRACE_ENABLED


def set_trace_enabled(enabled: bool) -> bool:
    """Set the global trace-enabled state and return the previous value."""
    global TRACE_ENABLED
    previous = TRACE_ENABLED
    TRACE_ENABLED = enabled
    return previous


@dataclass(frozen=True)
class EntityOrigin:
    """Source origin of an entity creation.

    Attributes
    ----------
    source : str
        ``path:line`` of the immediate caller that created the entity.
    traceback : tuple[str, ...]
        A short stack of caller locations, newest first.  Each entry is
        formatted as ``path:line:function`` relative to the project root.
    """

    source: str
    traceback: tuple[str, ...]


def _project_root() -> Path:
    """Return the project root directory (repo root, not the package)."""
    try:
        from factorio_display import __file__ as pkg_file  # type: ignore
        return Path(pkg_file).resolve().parent.parent.parent
    except Exception:
        return Path(os.getcwd())


_PROJECT_ROOT = _project_root()


def _format_frame(frame: Any) -> str:
    """Format a stack frame as ``path:line:function``."""
    path = Path(frame.f_code.co_filename)
    line = frame.f_lineno
    func = frame.f_code.co_name
    try:
        path = path.resolve().relative_to(_PROJECT_ROOT.resolve())
    except ValueError:
        path = path.name
    return f"{path}:{line}:{func}"


def entity_source(frame_idx: int = 2) -> str:
    """Return ``path:line`` for the caller's source location.

    *frame_idx* is the stack frame offset relative to this function.
    The default (2) returns the caller of the function that called
    ``entity_source``.
    """
    frame = sys._getframe(frame_idx)
    path = Path(frame.f_code.co_filename)
    line = frame.f_lineno
    try:
        path = path.resolve().relative_to(_PROJECT_ROOT.resolve())
    except ValueError:
        path = path.name
    return f"{path}:{line}"


def entity_traceback(frame_idx: int = 3, max_frames: int = 5) -> tuple[str, ...]:
    """Return a short caller traceback as ``path:line:function`` strings.

    *frame_idx* is the first frame to record (relative to this function).
    The default (3) starts at the caller of the function that called
    ``entity_traceback``.

    *max_frames* caps the number of frames recorded so the metadata stays
    small and serialisable.
    """
    frames: list[str] = []
    for i in range(max_frames):
        try:
            frame = sys._getframe(frame_idx + i)
        except ValueError:
            break
        frames.append(_format_frame(frame))
    return tuple(frames)


def entity_origin(frame_idx: int = 3, max_frames: int = 5) -> EntityOrigin:
    """Capture both the immediate source and a short caller traceback."""
    return EntityOrigin(
        source=entity_source(frame_idx=frame_idx - 1),
        traceback=entity_traceback(frame_idx=frame_idx, max_frames=max_frames),
    )


def set_entity_origin(entity: Any, origin: EntityOrigin | None = None) -> None:
    """Attach both source and traceback to a logical or draftsman entity.

    Logical entities store the data under ``properties["_debug_src"]`` and
    ``properties["_debug_traceback"]``.  Draftsman entities store it in
    ``tags["src"]`` and ``tags["traceback"]``.

    This is a no-op when :data:`TRACE_ENABLED` is ``False``.
    """
    if not TRACE_ENABLED:
        return

    if origin is None:
        origin = entity_origin()

    if isinstance(entity, dict):
        entity[_DEBUG_SRC_KEY] = origin.source
        entity[_DEBUG_TRACEBACK_KEY] = list(origin.traceback)
        return

    if hasattr(entity, "properties") and isinstance(entity.properties, dict):
        entity.properties[_DEBUG_SRC_KEY] = origin.source
        entity.properties[_DEBUG_TRACEBACK_KEY] = list(origin.traceback)
        return

    if hasattr(entity, "tags") and isinstance(entity.tags, dict):
        entity.tags["src"] = origin.source
        entity.tags["traceback"] = list(origin.traceback)
        return

    if hasattr(entity, "player_description"):
        entity.player_description = origin.source

    # Last resort: private attributes so downstream code can still retrieve
    # the metadata without serialising into the blueprint export.
    object.__setattr__(entity, _DEBUG_SRC_KEY, origin.source)
    object.__setattr__(entity, _DEBUG_TRACEBACK_KEY, list(origin.traceback))


def get_entity_origin(entity: Any) -> EntityOrigin | None:
    """Return the origin attached to *entity*, or None."""
    src = None
    tb = None

    if isinstance(entity, dict):
        src = entity.get(_DEBUG_SRC_KEY)
        tb = entity.get(_DEBUG_TRACEBACK_KEY)
    elif hasattr(entity, "properties") and isinstance(entity.properties, dict):
        src = entity.properties.get(_DEBUG_SRC_KEY)
        tb = entity.properties.get(_DEBUG_TRACEBACK_KEY)
    elif hasattr(entity, "tags") and isinstance(entity.tags, dict):
        tags = entity.tags
        src = tags.get("src")
        tb = tags.get("traceback")
    elif hasattr(entity, "player_description"):
        val = getattr(entity, "player_description", None)
        if val:
            src = str(val)

    if src is None:
        src = getattr(entity, _DEBUG_SRC_KEY, None)
    if tb is None:
        tb = getattr(entity, _DEBUG_TRACEBACK_KEY, None)

    if src is None:
        return None
    if not isinstance(tb, (list, tuple)):
        tb = ()
    return EntityOrigin(source=src, traceback=tuple(str(t) for t in tb))


def set_entity_source(entity: Any, src: str | None = None) -> None:
    """Attach a ``path:line`` source tag to a logical or draftsman entity.

    Logical entities store the tag under ``properties["_debug_src"]``.
    Draftsman entities store it in ``tags["src"]`` (or ``player_description``
    for combinators that support it, as a fallback).

    This is a convenience wrapper around :func:`set_entity_origin` that
    captures a fresh origin (including traceback) when *src* is omitted.
    """
    if src is None:
        set_entity_origin(entity)
        return

    origin = get_entity_origin(entity)
    if origin is None:
        origin = EntityOrigin(source=src, traceback=())
    else:
        origin = EntityOrigin(source=src, traceback=origin.traceback)
    set_entity_origin(entity, origin)


def get_entity_source(entity: Any) -> str | None:
    """Return the source tag attached to *entity*, or None."""
    origin = get_entity_origin(entity)
    return origin.source if origin is not None else None


def format_entity_source(entity: Any, default: str = "unknown") -> str:
    """Return a human-readable source string for *entity*."""
    origin = get_entity_origin(entity)
    if origin is not None:
        parts = [f"src={origin.source}"]
        if origin.traceback:
            parts.append(f"trace={origin.traceback}")
        return " ".join(parts)
    eid = getattr(entity, "entity_id", None) or getattr(entity, "id", None)
    if eid is not None:
        return f"id={eid} (no src)"
    return default


def format_traceback(origin: EntityOrigin, indent: str = "    ") -> str:
    """Return a multi-line traceback string for *origin*."""
    if not origin.traceback:
        return f"{indent}(no traceback)"
    return "\n".join(f"{indent}{frame}" for frame in origin.traceback)
