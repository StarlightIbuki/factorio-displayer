"""Per-principal on-disk storage for uploads, jobs and artifacts.

All data lives under ``<data_dir>/<principal>/`` so users are isolated from
day one.  Large text artifacts (TOML/YAML/draftsman JSON) are gzip-compressed
on disk above a threshold and decompressed transparently on read.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .settings import Settings


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically write *text* to *path*.

    Windows frequently refuses ``os.replace`` for a moment after a file is
    created (antivirus / search indexer briefly holds a handle), so we use a
    unique temp name per write and retry the rename with a short backoff.
    A unique temp name also means two threads writing the same target never
    clobber each other's temp file.
    """
    data = text.encode("utf-8")
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_bytes(data)
    last_err: OSError | None = None
    for _ in range(25):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:  # transient Windows lock
            last_err = exc
            time.sleep(0.02)
    # The lock never cleared — last resort: a direct (non-atomic) write.
    try:
        tmp.unlink(missing_ok=True)
        path.write_bytes(data)
    except OSError as exc:
        raise OSError(f"failed to write {path}: {last_err or exc}") from (last_err or exc)


def _classify_media(name: str) -> str:
    from ..cli import _classify_input  # pylint: disable=import-outside-toplevel

    return _classify_input(name)


@dataclass
class UploadRecord:
    upload_id: str
    name: str
    size_bytes: int
    media_type: str
    path: str
    created_at: float
    probe: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class Store:
    """Filesystem store scoped by principal."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure()

    # ── paths ────────────────────────────────────────────────────────
    def principal_root(self, principal: str) -> Path:
        return self.settings.data_dir / _safe(principal)

    def uploads_dir(self, principal: str) -> Path:
        p = self.principal_root(principal) / "uploads"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def jobs_dir(self, principal: str) -> Path:
        p = self.principal_root(principal) / "jobs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def job_dir(self, principal: str, job_id: str) -> Path:
        d = self.jobs_dir(principal) / _safe(job_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def artifact_dir(self, principal: str, job_id: str) -> Path:
        d = self.job_dir(principal, job_id) / "output"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── uploads ──────────────────────────────────────────────────────
    def save_upload(self, principal: str, filename: str, data: bytes) -> UploadRecord:
        upload_id = "u_" + uuid.uuid4().hex[:12]
        d = self.uploads_dir(principal) / upload_id
        d.mkdir(parents=True, exist_ok=True)
        safe_name = Path(filename or "upload.bin").name or "upload.bin"
        (d / safe_name).write_bytes(data)
        rec = UploadRecord(
            upload_id=upload_id,
            name=safe_name,
            size_bytes=len(data),
            media_type=_classify_media(safe_name),
            path=str(d / safe_name),
            created_at=time.time(),
        )
        _atomic_write_text(d / "meta.json", json.dumps(rec.to_dict()))
        return rec

    def load_upload(self, principal: str, upload_id: str) -> UploadRecord | None:
        meta = self.uploads_dir(principal) / _safe(upload_id) / "meta.json"
        if not meta.exists():
            return None
        try:
            return UploadRecord(**json.loads(meta.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def list_uploads(self, principal: str) -> list[UploadRecord]:
        out: list[UploadRecord] = []
        d = self.uploads_dir(principal)
        for sub in sorted(d.iterdir()) if d.exists() else []:
            if sub.is_dir():
                rec = self.load_upload(principal, sub.name)
                if rec is not None:
                    out.append(rec)
        return out

    def delete_upload(self, principal: str, upload_id: str) -> bool:
        d = self.uploads_dir(principal) / _safe(upload_id)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            return True
        return False

    # ── jobs ─────────────────────────────────────────────────────────
    def save_job(self, principal: str, job_id: str, record: dict) -> None:
        d = self.job_dir(principal, job_id)
        _atomic_write_text(
            d / "job.json",
            json.dumps(record, ensure_ascii=False, indent=2),
        )

    def load_job(self, principal: str, job_id: str) -> dict | None:
        p = self.job_dir(principal, job_id) / "job.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None

    def list_jobs(self, principal: str) -> list[dict]:
        out: list[dict] = []
        d = self.jobs_dir(principal)
        for sub in sorted(d.iterdir()) if d.exists() else []:
            if sub.is_dir():
                rec = self.load_job(principal, sub.name)
                if rec is not None:
                    out.append(rec)
        return out

    def delete_job(self, principal: str, job_id: str) -> bool:
        d = self.jobs_dir(principal) / _safe(job_id)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            return True
        return False

    def stderr_log(self, principal: str, job_id: str) -> str:
        p = self.job_dir(principal, job_id) / "stderr.log"
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
        return ""

    # ── artifacts (with storage compression) ─────────────────────────
    def write_artifact_text(self, principal: str, job_id: str, name: str, text: str) -> Path:
        d = self.artifact_dir(principal, job_id)
        raw = text.encode("utf-8")
        compress = self.settings.compress_artifacts and len(raw) >= self.settings.compress_threshold
        if compress:
            path = d / (name + ".gz")
            with gzip.open(path, "wb", compresslevel=6) as f:
                f.write(raw)
        else:
            path = d / name
            _atomic_write_text(path, text)
        return path

    def read_artifact_text(self, principal: str, job_id: str, name: str) -> str | None:
        d = self.artifact_dir(principal, job_id)
        for cand in (d / name, d / (name + ".gz")):
            if cand.exists():
                if cand.suffix == ".gz":
                    with gzip.open(cand, "rt", encoding="utf-8") as f:
                        return f.read()
                return cand.read_text(encoding="utf-8")
        return None

    def artifact_paths(self, principal: str, job_id: str) -> list[Path]:
        d = self.artifact_dir(principal, job_id)
        return sorted(p for p in d.iterdir()) if d.exists() else []
