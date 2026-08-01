"""Versioned on-disk cache path helpers.

All cache artifacts are stored under one root folder to keep the project
workspace tidy and avoid cross-version contamination.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import __version__

_CACHE_ROOT = Path(".factorio_display_cache")


def _version_token() -> str:
    raw = __version__ or "0"
    token = re.sub(r"[^0-9A-Za-z._-]+", "_", raw).strip("_")
    return token or "0"


def version_prefix() -> str:
    """Return a stable cache prefix containing the project version."""
    return f"v{_version_token()}"


def cache_root() -> Path:
    """Return the cache root directory and ensure it exists."""
    _CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    return _CACHE_ROOT


def cache_namespace_dir(namespace: str) -> Path:
    """Return a namespace subdirectory under the cache root."""
    ns = cache_root() / namespace
    ns.mkdir(parents=True, exist_ok=True)
    return ns


def cache_file(namespace: str, stem: str, suffix: str) -> Path:
    """Return a version-prefixed cache file path under *namespace*."""
    return cache_namespace_dir(namespace) / f"{version_prefix()}_{stem}{suffix}"


def cache_dir(namespace: str, stem: str) -> Path:
    """Return a version-prefixed cache directory path under *namespace*."""
    return cache_namespace_dir(namespace) / f"{version_prefix()}_{stem}"


def cache_key(*parts: str) -> str:
    """Deterministic short hash key from string parts."""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]


def cache_json_put(namespace: str, stem: str, data: object) -> Path:
    """Write *data* as JSON to a version-prefixed cache file.

    *stem* should already be a stable cache key (e.g. from :func:`cache_key`).
    Returns the written path.
    """
    path = cache_file(namespace, stem, ".json")
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def cache_json_get(namespace: str, stem: str) -> object | None:
    """Read a JSON cache file, or return None on miss / corruption."""
    path = cache_file(namespace, stem, ".json")
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
