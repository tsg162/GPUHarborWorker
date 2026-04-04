"""Docker-based job execution engine with local filesystem storage."""

from __future__ import annotations

import asyncio
import logging
import shutil
import signal
from pathlib import Path

import docker
from docker.errors import DockerException, ImageNotFound, NotFound
from docker.models.containers import Container

from gpuharbor.common.job_spec import JobSpec
from gpuharbor.common.states import JobState
from gpuharbor.common.storage import LocalStorage, compute_sha256
from gpuharbor.worker.state import JobStore

logger = logging.getLogger(__name__)

CONTAINER_WORKSPACE = "/workspace"
CONTAINER_INPUT_DIR = f"{CONTAINER_WORKSPACE}/input"
CONTAINER_OUTPUT_DIR = f"{CONTAINER_WORKSPACE}/output"
CONTAINER_CHECKPOINT_DIR = f"{CONTAINER_WORKSPACE}/checkpoints"


class JobExecutor:
    """Manages Docker-based job execution lifecycle with local storage."""

    def __init__(
        self,
        storage: LocalStorage,
        job_store: JobStore,
        default_grace_period: int = 30,
    ):
        self._storage = storage
        self._store = job_store
        self._grace_period = default_grace_period

        # Track running containers for cancel support
        self._running: dict[str, Container] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._log_queues: dict[str, asyncio.Queue] = {}

        try:
            self._docker = docker.from_env()
            self._docker.ping()
        except DockerException:
            logger.error("Docker is not available. Job execution will fail.")
            self._docker = None

    async def execute_job(self, job_id: str, spec: JobSpec) -> None:
        """Full job execution pipeline: prepare workspace -> run container -> record outputs.

        Designed to be run as an asyncio task.
        """
        cancel_event = asyncio.Event()
        self._cancel_events[job_id] = cancel_event
        self._log_queues[job_id] = asyncio.Queue(maxsize=10000)

        try:
            self._validate_resources(spec)
            await self._prepare_workspace(job_id, spec)
            await self._run_container(job_id, spec, cancel_event)
            self._record_output_artifacts(job_id)
            self._store.update_state(job_id, JobState.COMPLETED)
            logger.info("Job %s completed successfully", job_id)

        except asyncio.CancelledError:
            logger.info("Job %s was cancelled", job_id)
            try:
                self._store.update_state(job_id, JobState.CANCELED)
            except (ValueError, KeyError):
                pass
        except Exception as e:
            logger.exception("Job %s failed: %s", job_id, e)
            try:
                self._store.update_state(job_id, JobState.FAILED, error_message=str(e))
            except (ValueError, KeyError):
                logger.error("Could not update job %s state to FAILED", job_id)
        finally:
            self._running.pop(job_id, None)
            self._cancel_events.pop(job_id, None)
            # Signal end of logs
            if job_id in self._log_queues:
                await self._log_queues[job_id].put(None)

    def _validate_resources(self, spec: JobSpec) -> None:
        """Check that the server can satisfy the job's resource requirements."""
        if self._docker is None:
            raise RuntimeError("Docker is not available on this server")

        from gpuharbor.worker.gpu import get_gpu_info, get_system_info

        gpus = get_gpu_info()
        if spec.resources.gpu_count > len(gpus):
            raise ValueError(
                f"Job requires {spec.resources.gpu_count} GPU(s) but server has {len(gpus)}"
            )

        if spec.resources.disk_gb_min > 0:
            free_gb = self._storage.disk_free_gb()
            if free_gb < spec.resources.disk_gb_min:
                raise ValueError(
                    f"Job requires {spec.resources.disk_gb_min}GB free disk "
                    f"but only {free_gb}GB available"
                )

    async def _prepare_workspace(self, job_id: str, spec: JobSpec) -> None:
        """Create workspace dirs and copy input artifacts into place."""
        self._store.update_state(job_id, JobState.UPLOADING_INPUTS)
        self._storage.ensure_job_dirs(job_id)

        # If an input checkpoint was specified, copy it from uploads/ into job input/
        if spec.artifacts.input_checkpoint:
            filename = spec.artifacts.input_checkpoint
            # Check uploads/ directory first, then absolute path
            src = self._storage.get_file(f"uploads/{filename}")
            if src is None:
                # Maybe it was already placed in the job's input dir
                src = self._storage.get_file(f"jobs/{job_id}/input/{filename}")
            if src is None:
                raise FileNotFoundError(
                    f"Input checkpoint '{filename}' not found. "
                    f"Upload it first via POST /v1/upload"
                )
            # Copy to job input dir if not already there
            dest = self._storage.job_input_dir(job_id) / filename
            if not dest.exists():
                shutil.copy2(src, dest)
                logger.info("Copied checkpoint %s -> %s", src, dest)

            self._store.add_artifact(
                job_id, "input_checkpoint",
                f"jobs/{job_id}/input/{filename}",
                compute_sha256(dest),
            )

    async def _run_container(
        self,
        job_id: str,
        spec: JobSpec,
        cancel_event: asyncio.Event,
    ) -> None:
        """Pull image, create and start container, stream logs until exit."""
        self._store.update_state(job_id, JobState.RUNNING)
        loop = asyncio.get_event_loop()

        # Pull image
        logger.info("Pulling image %s for job %s", spec.container_image, job_id)
        try:
            await loop.run_in_executor(
                None, self._docker.images.pull, spec.container_image
            )
        except ImageNotFound:
            raise RuntimeError(f"Docker image not found: {spec.container_image}")

        # Build container config
        job_dir = self._storage.job_dir(job_id)
        env = dict(spec.env)
        env["GPUHARBOR_JOB_ID"] = job_id

        volumes = {
            str(job_dir / "input"): {"bind": CONTAINER_INPUT_DIR, "mode": "ro"},
            str(job_dir / "output"): {"bind": CONTAINER_OUTPUT_DIR, "mode": "rw"},
            str(job_dir / "checkpoints"): {"bind": CONTAINER_CHECKPOINT_DIR, "mode": "rw"},
        }

        # GPU device request
        device_requests = []
        if spec.resources.gpu_count > 0:
            device_requests.append(
                docker.types.DeviceRequest(
                    count=spec.resources.gpu_count,
                    capabilities=[["gpu"]],
                )
            )

        # Create and start container
        container = await loop.run_in_executor(
            None,
            lambda: self._docker.containers.run(
                spec.container_image,
                command=spec.command,
                environment=env,
                volumes=volumes,
                device_requests=device_requests if device_requests else None,
                detach=True,
                name=f"gpuharbor-{job_id}",
                labels={"gpuharbor.job_id": job_id},
                network_mode="bridge",
                shm_size="8g",
            ),
        )

        self._running[job_id] = container
        self._store.update_container_id(job_id, container.id)
        logger.info("Started container %s for job %s", container.short_id, job_id)

        await self._monitor_container(job_id, container, cancel_event)

    async def _monitor_container(
        self,
        job_id: str,
        container: Container,
        cancel_event: asyncio.Event,
    ) -> None:
        """Stream container logs and wait for exit, handling cancellation."""
        loop = asyncio.get_event_loop()
        log_task = asyncio.create_task(self._stream_logs(job_id, container))

        try:
            while True:
                if cancel_event.is_set():
                    await self._handle_cancel(job_id, container)
                    return

                try:
                    status = await loop.run_in_executor(
                        None, lambda: container.reload() or container.status
                    )
                except NotFound:
                    raise RuntimeError("Container disappeared unexpectedly")

                if status == "exited":
                    exit_code = container.attrs.get("State", {}).get("ExitCode", -1)
                    if exit_code != 0:
                        tail_logs = container.logs(tail=20).decode("utf-8", errors="replace")
                        raise RuntimeError(
                            f"Container exited with code {exit_code}.\n"
                            f"Last output:\n{tail_logs}"
                        )
                    return  # Success

                await asyncio.sleep(2)
        finally:
            log_task.cancel()
            try:
                await log_task
            except asyncio.CancelledError:
                pass
            try:
                await loop.run_in_executor(None, lambda: container.remove(force=True))
            except Exception:
                pass

    async def _stream_logs(self, job_id: str, container: Container) -> None:
        """Stream container logs into the log queue and local log file."""
        log_queue = self._log_queues.get(job_id)
        log_file = self._storage.job_log_file(job_id)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            log_gen = container.logs(stream=True, follow=True, timestamps=True)
            with open(log_file, "a") as f:
                for chunk in log_gen:
                    line = chunk.decode("utf-8", errors="replace").rstrip("\n")
                    f.write(line + "\n")
                    f.flush()
                    if log_queue:
                        try:
                            log_queue.put_nowait(line)
                        except asyncio.QueueFull:
                            try:
                                log_queue.get_nowait()
                            except asyncio.QueueEmpty:
                                pass
                            log_queue.put_nowait(line)
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Error streaming logs for job %s", job_id)

    async def _handle_cancel(self, job_id: str, container: Container) -> None:
        """Gracefully cancel a running job."""
        loop = asyncio.get_event_loop()
        logger.info("Cancelling job %s (grace period: %ds)", job_id, self._grace_period)

        try:
            self._store.update_state(job_id, JobState.CANCEL_REQUESTED)
        except (ValueError, KeyError):
            pass

        # SIGTERM first
        try:
            await loop.run_in_executor(
                None, lambda: container.kill(signal=signal.SIGTERM)
            )
        except Exception:
            logger.warning("Could not send SIGTERM to container for job %s", job_id)

        # Wait for grace period, then force kill
        try:
            await loop.run_in_executor(
                None, lambda: container.wait(timeout=self._grace_period)
            )
        except Exception:
            logger.warning("Force-killing container for job %s", job_id)
            try:
                await loop.run_in_executor(None, lambda: container.kill())
            except Exception:
                pass

        # Record whatever outputs were produced
        self._record_output_artifacts(job_id)
        self._store.update_state(job_id, JobState.CANCELED)
        logger.info("Job %s canceled", job_id)

    def _record_output_artifacts(self, job_id: str) -> None:
        """Scan output and checkpoint dirs and record artifacts in the store."""
        for subdir, default_type in [("output", "final_model"), ("checkpoints", "checkpoint")]:
            files = self._storage.list_job_files(job_id, subdir)
            for f in files:
                abs_path = self._storage.job_dir(job_id) / f["path"]
                sha = compute_sha256(abs_path) if abs_path.exists() else None
                # Infer type from filename
                artifact_type = default_type
                name_lower = f["name"].lower()
                if "log" in name_lower or name_lower.endswith((".log", ".txt")):
                    artifact_type = "training_log"
                elif "config" in name_lower or name_lower.endswith((".yaml", ".yml", ".json")):
                    artifact_type = "config"

                self._store.add_artifact(
                    job_id, artifact_type, f"jobs/{job_id}/{f['path']}", sha
                )

        # Record container log
        log_file = self._storage.job_log_file(job_id)
        if log_file.exists():
            self._store.add_artifact(
                job_id, "training_log",
                f"jobs/{job_id}/logs/container.log",
                compute_sha256(log_file),
            )

    async def cancel_job(self, job_id: str) -> bool:
        """Request cancellation of a running job.

        Returns True if cancel was initiated, False if job is not running.
        """
        cancel_event = self._cancel_events.get(job_id)
        if cancel_event is None:
            return False
        cancel_event.set()
        return True

    async def stream_logs(self, job_id: str):
        """Yield log lines for a job. Blocks until new lines are available."""
        queue = self._log_queues.get(job_id)
        if queue is None:
            # Job might be finished; read from log file
            log_file = self._storage.job_log_file(job_id)
            if log_file.exists():
                with open(log_file) as f:
                    for line in f:
                        yield line.rstrip("\n")
            return

        while True:
            line = await queue.get()
            if line is None:
                break
            yield line

    def get_log_file_path(self, job_id: str) -> Path | None:
        """Return the path to the local log file if it exists."""
        log_file = self._storage.job_log_file(job_id)
        return log_file if log_file.exists() else None
