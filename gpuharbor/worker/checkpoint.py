"""Monitor complete checkpoint directories, back up, and retain whole sets."""

from __future__ import annotations
import asyncio
import logging
from gpuharbor.common.storage import LocalStorage
from gpuharbor.worker.backup import BackupManager
from gpuharbor.worker.checkpoints import (
    completed_checkpoints,
    record_file,
    reconcile_artifacts,
)
from gpuharbor.worker.state import JobStore

logger = logging.getLogger(__name__)


class CheckpointManager:
    def __init__(
        self,
        storage: LocalStorage,
        job_store: JobStore,
        backup: BackupManager | None = None,
    ):
        self._storage, self._store = storage, job_store
        self.backup = backup or BackupManager(storage, job_store)
        self._monitors: dict[str, asyncio.Task] = {}

    def start_monitoring(
        self, job_id: str, interval_minutes: int = 10, keep_last_n: int = 3
    ) -> None:
        if job_id not in self._monitors:
            self._monitors[job_id] = asyncio.create_task(
                self._monitor_loop(job_id, interval_minutes, keep_last_n)
            )

    def stop_monitoring(self, job_id: str) -> None:
        task = self._monitors.pop(job_id, None)
        if task:
            task.cancel()

    def stop_all(self) -> None:
        for job_id in list(self._monitors):
            self.stop_monitoring(job_id)

    def scan(self, job_id: str, keep_last_n: int) -> None:
        with self.backup._lock:
            job = self._store.get_job(job_id)
            if not job:
                return
            units = completed_checkpoints(self._storage, job_id)
            for unit in units:
                for path in unit.rglob("*"):
                    if path.is_file():
                        record_file(
                            self._storage, self._store, job_id, path, "checkpoint"
                        )
            # Failed backup prevents pruning. A later scan retries.
            if units and self.backup.destination and job["spec"].get("backup", True):
                self.backup.snapshot(job_id)
            self._storage.prune_checkpoints(job_id, keep_last_n)
            reconcile_artifacts(self._storage, self._store, job_id)

    async def _monitor_loop(
        self, job_id: str, interval_minutes: int, keep_last_n: int
    ) -> None:
        while True:
            try:
                await asyncio.sleep(interval_minutes * 60)
                job = self._store.get_job(job_id)
                if not job or job["state"] not in {"running", "checkpointing"}:
                    return
                await asyncio.to_thread(self.scan, job_id, keep_last_n)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.exception("Checkpoint scan/backup failed for %s", job_id)
                receipt = (self._store.get_job(job_id) or {}).get("backup") or {}
                self._store.set_backup(job_id, {**receipt, "last_error": str(exc)})
