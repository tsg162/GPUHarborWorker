"""Internal job health monitoring via process liveness checks."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from gpuharbor.common.states import JobState, is_terminal
from gpuharbor.worker.state import JobStore

if TYPE_CHECKING:
    from gpuharbor.worker.executor import JobExecutor

logger = logging.getLogger(__name__)


class HeartbeatMonitor:
    """Periodically checks that running job processes are still alive.

    If a process has died without the executor noticing (e.g., OOM kill),
    the monitor marks the job as FAILED.
    """

    def __init__(
        self,
        job_store: JobStore,
        executor: JobExecutor | None = None,
        check_interval: int = 15,
    ):
        self._store = job_store
        self._executor = executor
        self._interval = check_interval
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info("Heartbeat monitor started (interval: %ds)", self._interval)

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Heartbeat monitor stopped")

    async def _check_loop(self) -> None:
        while self._running:
            try:
                await self._check_all_jobs()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Error in heartbeat check")

            await asyncio.sleep(self._interval)

    async def _check_all_jobs(self) -> None:
        """Check all running jobs whose PID is recorded in container_id field."""
        running_ids = self._store.get_running_job_ids()
        if not running_ids:
            return

        for job_id in running_ids:
            job = self._store.get_job(job_id)
            if not job:
                continue

            pid_str = job.get("container_id")
            if not pid_str:
                continue

            try:
                pid = int(pid_str)
            except (ValueError, TypeError):
                continue

            await self._check_process(job_id, pid)

    async def _check_process(self, job_id: str, pid: int) -> None:
        """Check if a process is still alive by sending signal 0."""
        # Skip jobs the executor is actively monitoring to avoid races
        if self._executor and self._executor.is_tracking(job_id):
            return

        try:
            os.kill(pid, 0)  # Doesn't actually send a signal, just checks existence
        except ProcessLookupError:
            # Process is gone -- mark job appropriately
            job = self._store.get_job(job_id)
            if job and not is_terminal(JobState(job["state"])):
                state = JobState(job["state"])
                try:
                    if state == JobState.CANCEL_REQUESTED:
                        # Cancel was in progress and process died -- treat as canceled
                        self._store.update_state(job_id, JobState.CANCELED)
                        logger.info(
                            "Job %s: cancel completed (process %d exited)",
                            job_id,
                            pid,
                        )
                    else:
                        error_msg = (
                            f"Process {pid} not found "
                            f"(may have been killed by OOM or external signal)"
                        )
                        logger.warning("Job %s: %s", job_id, error_msg)
                        self._store.update_state(
                            job_id,
                            JobState.FAILED,
                            error_message=error_msg,
                        )
                except (ValueError, KeyError):
                    pass
        except PermissionError:
            # Process exists but we can't signal it (different user) -- it's alive
            pass
