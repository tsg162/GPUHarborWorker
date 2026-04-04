"""Periodic checkpoint management — monitors checkpoint dirs and prunes old ones."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from gpuharbor.common.states import JobState
from gpuharbor.common.storage import LocalStorage, compute_sha256
from gpuharbor.worker.state import JobStore

logger = logging.getLogger(__name__)


class CheckpointManager:
    """Monitors checkpoint directories for active jobs and prunes old checkpoints.

    One instance per worker agent, manages checkpointing for all active jobs.
    """

    def __init__(self, storage: LocalStorage, job_store: JobStore):
        self._storage = storage
        self._store = job_store
        self._monitors: dict[str, asyncio.Task] = {}
        self._known_files: dict[str, set[str]] = {}

    def start_monitoring(
        self,
        job_id: str,
        interval_minutes: int = 10,
        keep_last_n: int = 3,
    ) -> None:
        """Start monitoring a job's checkpoint directory."""
        if job_id in self._monitors:
            logger.warning("Already monitoring checkpoints for job %s", job_id)
            return

        self._known_files[job_id] = set()
        task = asyncio.create_task(
            self._monitor_loop(job_id, interval_minutes, keep_last_n)
        )
        self._monitors[job_id] = task
        logger.info(
            "Started checkpoint monitoring for job %s (every %d min, keep last %d)",
            job_id, interval_minutes, keep_last_n,
        )

    def stop_monitoring(self, job_id: str) -> None:
        """Stop monitoring checkpoints for a job."""
        task = self._monitors.pop(job_id, None)
        if task and not task.done():
            task.cancel()
        self._known_files.pop(job_id, None)
        logger.info("Stopped checkpoint monitoring for job %s", job_id)

    def stop_all(self) -> None:
        for job_id in list(self._monitors):
            self.stop_monitoring(job_id)

    async def _monitor_loop(
        self,
        job_id: str,
        interval_minutes: int,
        keep_last_n: int,
    ) -> None:
        """Periodically scan for new checkpoints and prune old ones."""
        interval_seconds = interval_minutes * 60

        while True:
            try:
                await asyncio.sleep(interval_seconds)

                # Check if job is still active
                job = self._store.get_job(job_id)
                if not job or job["state"] not in (
                    JobState.RUNNING.value,
                    JobState.CHECKPOINTING.value,
                ):
                    logger.info("Job %s no longer running; stopping checkpoint monitor", job_id)
                    break

                ckpt_dir = self._storage.job_checkpoint_dir(job_id)
                if not ckpt_dir.exists():
                    continue

                # Find new checkpoint files
                new_files = self._find_new_checkpoints(job_id, ckpt_dir)
                if not new_files:
                    continue

                # Transition to checkpointing state
                try:
                    self._store.update_state(job_id, JobState.CHECKPOINTING)
                except ValueError:
                    continue

                # Record new checkpoint artifacts
                for ckpt_path in new_files:
                    rel = ckpt_path.relative_to(self._storage.job_dir(job_id))
                    sha = compute_sha256(ckpt_path)
                    self._store.add_artifact(
                        job_id, "checkpoint", str(rel), sha
                    )
                    known = self._known_files.get(job_id)
                    if known is not None:
                        known.add(str(ckpt_path))
                    logger.info("New checkpoint recorded: %s (sha256:%s)", ckpt_path.name, sha[:16])

                # Prune old checkpoints
                self._storage.prune_checkpoints(job_id, keep_last_n)

                # Back to running
                try:
                    self._store.update_state(job_id, JobState.RUNNING)
                except ValueError:
                    pass

            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Error in checkpoint monitor for job %s", job_id)

    def _find_new_checkpoints(self, job_id: str, ckpt_dir: Path) -> list[Path]:
        """Find checkpoint files not yet recorded."""
        known = self._known_files.get(job_id, set())
        new_files = []

        for path in ckpt_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.name.startswith(".") or path.name.endswith(".tmp"):
                continue
            if str(path) not in known:
                new_files.append(path)

        new_files.sort(key=lambda p: p.stat().st_mtime)
        return new_files
