"""Asynchronous job runner for the factorio-display API.

Jobs are persisted as ``<data_dir>/<principal>/jobs/<job_id>/job.json`` so
state survives restarts.  Encode jobs run as a subprocess (``python -m
factorio_display encode --json …``) which gives crash isolation and avoids
capturing the process-global stdout/stderr in the threaded server; fast
builder jobs run in-process.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .settings import Settings
from .store import Store

_VALID_BUILDER_TYPES = {"display", "audio-decoder", "logical"}


class JobRunner:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self._pool = ThreadPoolExecutor(max_workers=max(1, settings.max_workers))
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen] = {}
        self._recover()

    # ── lifecycle ────────────────────────────────────────────────────
    def _recover(self) -> None:
        """Mark queued/running jobs from a previous server process as failed."""
        data_dir = self.settings.data_dir
        if not data_dir.exists():
            return
        for owner_dir in data_dir.iterdir():
            jobs_dir = owner_dir / "jobs"
            if not jobs_dir.is_dir():
                continue
            owner = owner_dir.name
            for rec in self.store.list_jobs(owner):
                if rec.get("status") in ("queued", "running"):
                    rec["status"] = "failed"
                    rec["finished_at"] = time.time()
                    rec["error"] = "interrupted by server restart"
                    rec["progress"] = {"phase": "failed"}
                    self.store.save_job(owner, rec["job_id"], rec)

    def _active_count(self, owner: str) -> int:
        return sum(
            1 for r in self.store.list_jobs(owner) if r.get("status") in ("queued", "running")
        )

    def can_submit(self, owner: str) -> bool:
        return self._active_count(owner) < self.settings.max_jobs_per_user

    def submit(self, owner: str, spec: dict) -> str:
        """Create a job from *spec* and enqueue it. Returns the job id."""
        if not self.can_submit(owner):
            raise RuntimeError(
                f"Too many active jobs for this caller (limit {self.settings.max_jobs_per_user}). "
                "Wait for one to finish or cancel it."
            )
        job_id = "j_" + uuid.uuid4().hex[:12]
        record = {
            "job_id": job_id,
            "owner": owner,
            "type": spec["type"],
            "name": str(spec.get("name") or spec.get("options", {}).get("name", "")),
            "status": "queued",
            "progress": {"phase": "queued"},
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "result": None,
            "inputs": list(spec.get("inputs", [])),
            "callback_url": spec.get("callback_url"),
            "config": spec.get("config", {}),
        }
        self.store.job_dir(owner, job_id)
        self.store.save_job(owner, job_id, record)
        self._pool.submit(self._run, job_id, owner, record)
        return job_id

    # ── worker ───────────────────────────────────────────────────────
    def _run(self, job_id: str, owner: str, record: dict) -> None:
        rec = dict(record)
        rec["status"] = "running"
        rec["started_at"] = time.time()
        rec["progress"] = {"phase": "running"}
        self.store.save_job(owner, job_id, rec)
        try:
            self._execute(owner, job_id, rec)
            current = self.store.load_job(owner, job_id) or rec
            if current.get("status") == "cancelled":
                return
            current["status"] = "succeeded"
            current["finished_at"] = time.time()
            current["progress"] = {"phase": "done"}
            self.store.save_job(owner, job_id, current)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            current = self.store.load_job(owner, job_id) or rec
            if current.get("status") == "cancelled":
                return
            current["status"] = "failed"
            current["finished_at"] = time.time()
            current["error"] = str(exc)
            current["progress"] = {"phase": "failed"}
            self.store.save_job(owner, job_id, current)
        finally:
            self._fire_webhook(self.store.load_job(owner, job_id) or rec)

    def _execute(self, owner: str, job_id: str, rec: dict) -> None:
        jtype = rec["type"]
        if jtype == "encode":
            self._run_encode(owner, job_id, rec)
        elif jtype in _VALID_BUILDER_TYPES:
            self._run_builder(owner, job_id, rec)
        else:
            raise ValueError(f"unsupported job type: {jtype}")

    # ── encode (subprocess) ──────────────────────────────────────────
    def _run_encode(self, owner: str, job_id: str, rec: dict) -> None:
        from ..service import MediaConfig, parse_media_json  # pylint: disable=import-outside-toplevel

        inputs: list[str] = []
        for upload_id in rec.get("inputs", []):
            up = self.store.load_upload(owner, upload_id)
            if up is None:
                raise FileNotFoundError(f"upload not found: {upload_id}")
            # Defensive: the encode subprocess runs with cwd=job dir, so the
            # input must be absolute even if it was persisted as relative.
            inputs.append(str(Path(up.path).expanduser().resolve()))
        if not inputs:
            raise ValueError("encode job requires at least one input upload")

        cfg = MediaConfig(inputs=inputs, **rec.get("config", {}))
        argv = [sys.executable, "-m", "factorio_display", *cfg.to_argv()]
        job_dir = self.store.job_dir(owner, job_id)
        stderr_path = job_dir / "stderr.log"

        proc = subprocess.Popen(
            argv,
            cwd=str(job_dir),  # per-job cache namespace via .factorio_display_cache
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        with self._lock:
            self._procs[job_id] = proc
        try:
            out, err = proc.communicate()
        finally:
            with self._lock:
                self._procs.pop(job_id, None)
        stderr_path.write_text(err, encoding="utf-8")

        if proc.returncode != 0:
            last = [ln for ln in err.strip().splitlines() if ln.strip()]
            raise RuntimeError(last[-1] if last else f"encode exited with code {proc.returncode}")

        result = parse_media_json(out)
        rec["result"] = result.to_dict()

        if result.blueprint:
            self.store.write_artifact_text(owner, job_id, "result.txt", result.blueprint)
            try:
                from ..logical_blueprint import blueprint_string_to_yaml  # pylint: disable=import-outside-toplevel
                self.store.write_artifact_text(
                    owner, job_id, "result.yaml", blueprint_string_to_yaml(result.blueprint)
                )
            except Exception:  # pylint: disable=broad-exception-caught
                pass  # yaml conversion is best-effort
        rec["result"]["entity_count"] = result.entity_count
        self.store.write_artifact_text(
            owner, job_id, "result.json",
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
        )

    # ── fast builders (in-process) ───────────────────────────────────
    def _run_builder(self, owner: str, job_id: str, rec: dict) -> None:
        from .. import service  # pylint: disable=import-outside-toplevel

        jtype = rec["type"]
        config = rec.get("config", {})
        if jtype == "display":
            res = service.export_display(service.DisplayConfig(**config))
        elif jtype == "audio-decoder":
            res = service.export_audio_decoder(service.AudioDecoderConfig(**config))
        else:  # logical
            res = service.export_logical(service.LogicalConfig(**config))

        self.store.write_artifact_text(owner, job_id, f"result.{res.format}", res.text)
        rec["result"] = {
            "blueprint": res.blueprint,
            "text": res.text,
            "format": res.format,
            "name": res.name,
            "entity_count": res.entity_count,
            "instruments": res.instruments,
        }

    # ── queries / control ────────────────────────────────────────────
    def get(self, owner: str, job_id: str) -> dict | None:
        rec = self.store.load_job(owner, job_id)
        if rec is None:
            return None
        rec = dict(rec)
        rec["progress"] = dict(rec.get("progress") or {})
        tail = self.store.stderr_log(owner, job_id)
        if tail:
            lines = [ln for ln in tail.splitlines() if ln.strip()]
            if lines:
                rec["progress"]["log_tail"] = lines[-20:]
        return rec

    def list(self, owner: str, status: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        jobs = self.store.list_jobs(owner)
        if status:
            jobs = [j for j in jobs if j.get("status") == status]
        jobs.sort(key=lambda j: j.get("created_at", 0.0), reverse=True)
        return jobs[offset : offset + limit]

    def cancel(self, owner: str, job_id: str) -> str | None:
        rec = self.store.load_job(owner, job_id)
        if rec is None:
            return None
        status = rec.get("status")
        if status == "queued":
            rec["status"] = "cancelled"
            rec["finished_at"] = time.time()
            rec["error"] = "cancelled before start"
            self.store.save_job(owner, job_id, rec)
            return "cancelled"
        if status == "running":
            with self._lock:
                proc = self._procs.get(job_id)
            if proc is not None:
                proc.terminate()
            rec["status"] = "cancelled"
            rec["finished_at"] = time.time()
            rec["error"] = "cancelled by user"
            self.store.save_job(owner, job_id, rec)
            return "cancelled"
        return status

    def delete(self, owner: str, job_id: str) -> bool:
        return self.store.delete_job(owner, job_id)

    def shutdown(self, wait: bool = False) -> None:
        """Stop accepting work and release worker threads (idempotent)."""
        self._pool.shutdown(wait=wait, cancel_futures=True)

    def _fire_webhook(self, rec: dict) -> None:
        url = rec.get("callback_url")
        if not url:
            return

        def _post() -> None:
            try:
                import httpx  # pylint: disable=import-outside-toplevel
                httpx.post(
                    url,
                    json={
                        "job_id": rec.get("job_id"),
                        "status": rec.get("status"),
                        "error": rec.get("error"),
                        "result_url": f"/api/v1/jobs/{rec.get('job_id')}/result",
                    },
                    timeout=10.0,
                )
            except Exception:  # pylint: disable=broad-exception-caught
                pass

        threading.Thread(target=_post, daemon=True).start()
