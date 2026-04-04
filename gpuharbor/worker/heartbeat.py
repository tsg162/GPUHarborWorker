"""Internal job health monitoring via container heartbeat checks."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import docker
from docker.errors import DockerException, NotFound

from gpuharbor.common.states import JobState, is_terminal
from gpuharbor.worker.state import JobStore

logger = logging.getLogger(__name__)


class HeartbeatMonitor:
    """Periodically checks that running job containers are still alive.

    If a container has died without the executor noticing (e.g., OOM kill,
    host-level kill), the monitor marks the job as FAILED.
    """

    def __init__(
        self,
        job_store: JobStore,
        check_interval: int = 15,
    ):
        self._store = job_store
        self._interval = check_interval
        self._task: asyncio.Task | None = None
        self._running = False

        try:
            self._docker = docker.from_env()
        except DockerException:
            logger.warning("Docker not available for heartbeat monitoring")
            self._docker = None

    def start(self) -> None:
        """Start the heartbeat monitor background task."""
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info("Heartbeat monitor started (interval: %ds)", self._interval)

    def stop(self) -> None:
        """Stop the heartbeat monitor."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Heartbeat monitor stopped")

    async def _check_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            try:
                await self._check_all_jobs()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Error in heartbeat check")

            await asyncio.sleep(self._interval)

    async def _check_all_jobs(self) -> None:
        """Check all non-terminal jobs that have a container ID."""
        running_ids = self._store.get_running_job_ids()
        if not running_ids:
            return

        for job_id in running_ids:
            job = self._store.get_job(job_id)
            if not job:
                continue

            container_id = job.get("container_id")
            if not container_id:
                continue

            await self._check_container(job_id, container_id)

    async def _check_container(self, job_id: str, container_id: str) -> None:
        """Check if a specific container is still running."""
        if not self._docker:
            return

        loop = asyncio.get_event_loop()

        try:
            container = await loop.run_in_executor(
                None, self._docker.containers.get, container_id
            )
            status = container.status

            if status == "exited":
                exit_code = container.attrs.get("State", {}).get("ExitCode", -1)
                if exit_code != 0:
                    error_msg = (
                        f"Container exited unexpectedly with code {exit_code} "
                        f"(detected by heartbeat monitor)"
                    )
                    logger.warning("Job %s: %s", job_id, error_msg)
                    try:
                        self._store.update_state(
                            job_id, JobState.FAILED, error_message=error_msg
                        )
                    except (ValueError, KeyError):
                        pass
                # If exit code is 0, the executor should handle completion

            elif status in ("dead", "removing"):
                error_msg = f"Container in unexpected state: {status}"
                logger.warning("Job %s: %s", job_id, error_msg)
                try:
                    self._store.update_state(
                        job_id, JobState.FAILED, error_message=error_msg
                    )
                except (ValueError, KeyError):
                    pass

        except NotFound:
            # Container was removed; mark job as failed if still in running state
            job = self._store.get_job(job_id)
            if job and not is_terminal(JobState(job["state"])):
                error_msg = "Container not found (may have been removed externally)"
                logger.warning("Job %s: %s", job_id, error_msg)
                try:
                    self._store.update_state(
                        job_id, JobState.FAILED, error_message=error_msg
                    )
                except (ValueError, KeyError):
                    pass

        except DockerException as e:
            logger.warning("Docker error checking job %s: %s", job_id, e)
