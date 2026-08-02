"""factorio-display web API.

The API is a thin wrapper over :mod:`factorio_display.service`.  Long-running
``encode`` work runs as asynchronous jobs (subprocess per job); fast builders
run synchronously in-process.  Every upload, job and artifact is scoped to a
*principal* (the caller), so one user can never see another's data — the
per-user isolation model is in place from day one even before OIDC auth lands.
"""

__all__ = ["create_app", "serve", "Settings"]

from .server import create_app, serve
from .settings import Settings
