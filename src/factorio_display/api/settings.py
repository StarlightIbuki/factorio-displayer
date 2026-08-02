"""Server settings for the factorio-display web API."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Settings:
    """Runtime configuration for the web server."""

    data_dir: Path = Path("server_data")
    max_workers: int = 2
    max_jobs_per_user: int = 2
    api_token: str | None = None
    compress_artifacts: bool = True
    compress_threshold: int = 262144  # 256 KiB
    compress_min_size: int = 1024
    static_dir: Path | None = None  # None → package api/static
    host: str = "127.0.0.1"
    port: int = 8000
    base_url: str = "http://127.0.0.1:8000"
    pastebin_dev_key: str = ""  # https://pastebin.com/doc_api (env PASTEBIN_DEV_KEY)

    def ensure(self) -> "Settings":
        # Resolve to an absolute path: upload paths are stored on disk and
        # later consumed by the encode subprocess whose cwd differs (job dir),
        # so relative paths would break.  Accept str inputs too.
        self.data_dir = Path(self.data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self
