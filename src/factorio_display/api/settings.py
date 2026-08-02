"""Server settings for the factorio-display web API."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Principal bucket used for unauthenticated callers (no auth configured) and
# for callers that fall back to the default anonymous token.
ANONYMOUS = "anonymous"


@dataclass
class Settings:
    """Runtime configuration for the web server."""

    data_dir: Path = Path("server_data")
    max_workers: int = 2
    max_jobs_per_user: int = 2
    # Rate limits for the shared anonymous bucket ("anonymous").  This is the
    # default token granted to callers without their own token, so all
    # anonymous users share one processing slot and a small queue.
    anonymous_max_processing: int = 1  # at most this many jobs running at once
    anonymous_max_queued: int = 5      # at most this many jobs queued at once
    anonymous_max_per_hour: int = 20   # at most this many jobs per rolling hour
    max_upload_bytes: int = 256 * 1024 * 1024  # reject uploads above 256 MiB (--max-upload-mb)
    api_token: str | None = None
    token_key: str | None = None  # HMAC key for signed access tokens (--token-key)
    # GitHub OAuth (login with GitHub).  client_secret stays server-side only.
    github_oauth_client_id: str = ""
    github_oauth_client_secret: str = ""
    github_oauth_redirect_uri: str = ""  # e.g. https://factorio.qvq.moe:60012/auth/github/callback
    github_oauth_scope: str = "read:user"
    frontend_url: str = ""  # where to redirect back after OAuth (e.g. the GH Pages URL)
    compress_artifacts: bool = True
    compress_threshold: int = 262144  # 256 KiB
    compress_min_size: int = 1024
    static_dir: Path | None = None  # None → package api/static
    host: str = "127.0.0.1"
    port: int = 8000
    base_url: str = "http://127.0.0.1:8000"
    share_ttl_hours: float = 24.0  # lifetime of a temporary public share link

    # CORS: which browser origins may call the API cross-origin.  The default
    # allows the project's GitHub Pages site (and localhost dev servers).
    # Override on the command line with --cors-origins / --cors-origin-regex,
    # or via the CORS_ALLOW_ORIGINS / CORS_ALLOW_ORIGIN_REGEX env vars.
    cors_allow_origins: tuple[str, ...] = (
        "https://StarlightIbuki.github.io",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    )
    cors_allow_origin_regex: str = r"https://[a-zA-Z0-9-]+\.github\.io"

    def ensure(self) -> "Settings":
        # Resolve to an absolute path: upload paths are stored on disk and
        # later consumed by the encode subprocess whose cwd differs (job dir),
        # so relative paths would break.  Accept str inputs too.
        self.data_dir = Path(self.data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self
