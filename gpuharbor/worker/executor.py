"""Direct process-based job execution engine with local filesystem storage.

Runs training commands as subprocesses directly on the host — no Docker.
This is the right approach for Vast.ai / Runpod where the instance already
has CUDA, PyTorch, etc. installed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
from pathlib import Path
from typing import AsyncIterator

from gpuharbor.common.job_spec import JobSpec
from gpuharbor.common.states import JobState
from gpuharbor.common.storage import LocalStorage, compute_sha256
from gpuharbor.worker.state import JobStore

logger = logging.getLogger(__name__)


class JobExecutor:
    """Manages direct subprocess job execution with local storage."""

    def __init__(
        self,
        storage: LocalStorage,
        job_store: JobStore,
        default_grace_period: int = 30,
    ):
        self._storage = storage
        self._store = job_store
        self._grace_period = default_grace_period

        # Track running processes for cancel support
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._log_queues: dict[str, asyncio.Queue] = {}

    async def execute_job(self, job_id: str, spec: JobSpec) -> None:
        """Full job execution: prepare workspace -> run process -> record outputs.

        Designed to be run as an asyncio task.
        """
        cancel_event = asyncio.Event()
        self._cancel_events[job_id] = cancel_event
        self._log_queues[job_id] = asyncio.Queue(maxsize=10000)

        try:
            self._validate_resources(spec)
            await self._prepare_workspace(job_id, spec)
            await self._run_process(job_id, spec, cancel_event)
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
            self._processes.pop(job_id, None)
            self._cancel_events.pop(job_id, None)
            # Signal end of logs
            if job_id in self._log_queues:
                await self._log_queues[job_id].put(None)

    def _validate_resources(self, spec: JobSpec) -> None:
        """Check that the server can satisfy the job's resource requirements."""
        from gpuharbor.worker.gpu import get_gpu_info

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

        # Copy input checkpoint from uploads/ into job input/
        if spec.artifacts.input_checkpoint:
            filename = spec.artifacts.input_checkpoint
            src = self._storage.get_file(f"uploads/{filename}")
            if src is None:
                src = self._storage.get_file(f"jobs/{job_id}/input/{filename}")
            if src is None:
                raise FileNotFoundError(
                    f"Input checkpoint '{filename}' not found. "
                    f"Upload it first via POST /v1/upload"
                )
            dest = self._storage.job_input_dir(job_id) / filename
            if not dest.exists():
                shutil.copy2(src, dest)
                logger.info("Copied checkpoint %s -> %s", src, dest)

            self._store.add_artifact(
                job_id, "input_checkpoint",
                f"jobs/{job_id}/input/{filename}",
                compute_sha256(dest),
            )

    async def _run_process(
        self,
        job_id: str,
        spec: JobSpec,
        cancel_event: asyncio.Event,
    ) -> None:
        """Spawn the command as a subprocess and stream its output."""
        self._store.update_state(job_id, JobState.RUNNING)

        job_dir = self._storage.job_dir(job_id)

        # Build environment: inherit host env + job spec env + gpuharbor vars
        env = dict(os.environ)
        env.update(spec.env)
        env["GPUHARBOR_JOB_ID"] = job_id
        env["GPUHARBOR_INPUT_DIR"] = str(job_dir / "input")
        env["GPUHARBOR_OUTPUT_DIR"] = str(job_dir / "output")
        env["GPUHARBOR_CHECKPOINT_DIR"] = str(job_dir / "checkpoints")

        # Restrict visible GPUs if the job requests fewer than available
        from gpuharbor.worker.gpu import get_gpu_count
        total_gpus = get_gpu_count()
        if 0 < spec.resources.gpu_count < total_gpus:
            # Give the job the first N GPUs (simple allocation)
            visible = ",".join(str(i) for i in range(spec.resources.gpu_count))
            env["CUDA_VISIBLE_DEVICES"] = visible

        cmd = spec.command
        logger.info("Running job %s: %s", job_id, " ".join(cmd))

        log_file = self._storage.job_log_file(job_id)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        # Start subprocess
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge stderr into stdout
            env=env,
            cwd=str(job_dir),
        )

        self._processes[job_id] = proc
        self._store.update_container_id(job_id, str(proc.pid))
        logger.info("Started process PID %d for job %s", proc.pid, job_id)

        # Stream output
        await self._stream_and_wait(job_id, proc, cancel_event, log_file)

    async def _stream_and_wait(
        self,
        job_id: str,
        proc: asyncio.subprocess.Process,
        cancel_event: asyncio.Event,
        log_file: Path,
    ) -> None:
        """Read stdout, write to log file + queue, handle cancellation."""
        log_queue = self._log_queues.get(job_id)

        async def _read_output():
            with open(log_file, "ab") as f:
                while True:
                    line_bytes = await proc.stdout.readline()
                    if not line_bytes:
                        break
                    # Write to log file
                    f.write(line_bytes)
                    f.flush()
                    # Push to live queue
                    line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
                    if log_queue:
                        try:
                            log_queue.put_nowait(line)
                        except asyncio.QueueFull:
                            try:
                                log_queue.get_nowait()
                            except asyncio.QueueEmpty:
                                pass
                            log_queue.put_nowait(line)

        async def _watch_cancel():
            while not cancel_event.is_set():
                await asyncio.sleep(1)
            # Cancel requested — send SIGTERM
            await self._handle_cancel(job_id, proc)

        read_task = asyncio.create_task(_read_output())
        cancel_task = asyncio.create_task(_watch_cancel())

        try:
            # Wait for process to finish or cancel
            await asyncio.wait(
                [read_task, cancel_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # If cancel fired, read_task may still be running
            if cancel_event.is_set():
                read_task.cancel()
                try:
                    await read_task
                except asyncio.CancelledError:
                    pass
                return  # cancel handler already set state

            # Process finished normally — wait for it
            await proc.wait()
            cancel_task.cancel()

            if proc.returncode != 0:
                # Read last lines from log for error context
                tail = ""
                if log_file.exists():
                    with open(log_file, "rb") as f:
                        data = f.read()
                        lines = data.decode("utf-8", errors="replace").splitlines()
                        tail = "\n".join(lines[-20:])
                raise RuntimeError(
                    f"Process exited with code {proc.returncode}.\n"
                    f"Last output:\n{tail}"
                )

        finally:
            cancel_task.cancel()
            try:
                await cancel_task
            except asyncio.CancelledError:
                pass

    async def _handle_cancel(self, job_id: str, proc: asyncio.subprocess.Process) -> None:
        """Gracefully cancel a running process."""
        logger.info("Cancelling job %s (grace period: %ds)", job_id, self._grace_period)

        try:
            self._store.update_state(job_id, JobState.CANCEL_REQUESTED)
        except (ValueError, KeyError):
            pass

        # SIGTERM first
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass

        # Wait for grace period
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._grace_period)
        except asyncio.TimeoutError:
            # Force kill
            logger.warning("Force-killing process for job %s", job_id)
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass

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
                artifact_type = default_type
                name_lower = f["name"].lower()
                if "log" in name_lower or name_lower.endswith((".log", ".txt")):
                    artifact_type = "training_log"
                elif "config" in name_lower or name_lower.endswith((".yaml", ".yml", ".json")):
                    artifact_type = "config"

                self._store.add_artifact(
                    job_id, artifact_type, f"jobs/{job_id}/{f['path']}", sha
                )

        log_file = self._storage.job_log_file(job_id)
        if log_file.exists():
            self._store.add_artifact(
                job_id, "training_log",
                f"jobs/{job_id}/logs/container.log",
                compute_sha256(log_file),
            )

    async def cancel_job(self, job_id: str) -> bool:
        """Request cancellation of a running job."""
        cancel_event = self._cancel_events.get(job_id)
        if cancel_event is None:
            return False
        cancel_event.set()
        return True

    async def stream_logs(self, job_id: str) -> AsyncIterator[str]:
        """Yield log lines for a job."""
        queue = self._log_queues.get(job_id)
        if queue is None:
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
        log_file = self._storage.job_log_file(job_id)
        return log_file if log_file.exists() else None
