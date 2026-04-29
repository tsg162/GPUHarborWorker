"""Direct process-based job execution engine with local filesystem storage.

Runs training commands as subprocesses directly on the host -- no Docker.
This is the right approach for Vast.ai / Runpod where the instance already
has CUDA, PyTorch, etc. installed.

Processes are spawned in their own sessions (start_new_session=True) so they
survive worker restarts.  On startup, the executor re-attaches to any
still-running processes from the previous worker instance.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
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
    """Manages direct subprocess job execution with local storage.

    Processes are spawned in their own sessions so they survive worker
    restarts.  On startup the executor re-attaches to any still-running
    processes from the previous worker instance.
    """

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
        # Jobs we are actively monitoring (for heartbeat coordination)
        self._monitored_jobs: set[str] = set()

    def is_tracking(self, job_id: str) -> bool:
        """Check if this executor is actively monitoring a job."""
        return job_id in self._monitored_jobs

    # ── New job execution ──────────────────────────────────────────────

    async def execute_job(self, job_id: str, spec: JobSpec) -> None:
        """Full job execution: prepare workspace -> run process -> record outputs.

        Designed to be run as an asyncio task.
        """
        self._monitored_jobs.add(job_id)
        cancel_event = asyncio.Event()
        self._cancel_events[job_id] = cancel_event
        self._log_queues[job_id] = asyncio.Queue(maxsize=10000)
        process_started = False

        try:
            self._validate_resources(job_id, spec)
            await self._prepare_workspace(job_id, spec)
            proc = await self._start_process(job_id, spec)
            process_started = True

            log_file = self._storage.job_log_file(job_id)
            cancelled = await self._monitor_process(
                job_id, proc.pid, cancel_event, log_file, proc=proc
            )

            if cancelled:
                return  # cancel handler already set state

            if proc.returncode != 0:
                tail = self._read_log_tail(log_file)
                raise RuntimeError(
                    f"Process exited with code {proc.returncode}.\n"
                    f"Last output:\n{tail}"
                )

            self._record_output_artifacts(job_id)
            self._store.update_state(job_id, JobState.COMPLETED)
            logger.info("Job %s completed successfully", job_id)

        except asyncio.CancelledError:
            if process_started:
                logger.info(
                    "Detaching from job %s "
                    "(worker shutting down, process continues in background)",
                    job_id,
                )
            else:
                logger.info("Job %s interrupted before process started", job_id)
                try:
                    self._store.update_state(
                        job_id,
                        JobState.FAILED,
                        error_message="Worker shutdown before process started",
                    )
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
            self._monitored_jobs.discard(job_id)
            # Signal end of logs
            if job_id in self._log_queues:
                await self._log_queues[job_id].put(None)

    # ── Re-attach to running jobs after restart ────────────────────────

    async def reattach_running_jobs(
        self,
        checkpoint_mgr=None,
    ) -> dict[str, asyncio.Task]:
        """Re-attach to running jobs from a previous worker instance.

        Returns a dict of {job_id: asyncio.Task} for the caller to track.
        """
        running_ids = self._store.get_running_job_ids()
        if not running_ids:
            return {}

        tasks: dict[str, asyncio.Task] = {}

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

            if not self._is_pid_alive(pid):
                logger.info(
                    "Job %s process (PID %d) already dead, skipping reattach",
                    job_id,
                    pid,
                )
                continue

            state = job.get("state")

            # If cancel was in progress when we restarted, resume it
            if state == JobState.CANCEL_REQUESTED.value:
                logger.info(
                    "Resuming cancellation of job %s (PID %d)", job_id, pid
                )
                task = asyncio.create_task(self._resume_cancel(job_id, pid))
                tasks[job_id] = task
                continue

            logger.info("Re-attaching to job %s (PID %d)", job_id, pid)

            # Restart checkpoint monitoring if enabled
            if checkpoint_mgr and job.get("spec"):
                ckpt_cfg = job["spec"].get("checkpointing", {})
                if ckpt_cfg.get("enabled"):
                    checkpoint_mgr.start_monitoring(
                        job_id=job_id,
                        interval_minutes=ckpt_cfg.get("save_every_minutes", 10),
                        keep_last_n=ckpt_cfg.get("keep_last_n", 3),
                    )

            task = asyncio.create_task(self._reattach_job(job_id, pid))
            tasks[job_id] = task

        if tasks:
            logger.info("Re-attached to %d running job(s)", len(tasks))

        return tasks

    async def _reattach_job(self, job_id: str, pid: int) -> None:
        """Re-attach to a single running job process."""
        self._monitored_jobs.add(job_id)
        cancel_event = asyncio.Event()
        self._cancel_events[job_id] = cancel_event
        self._log_queues[job_id] = asyncio.Queue(maxsize=10000)

        log_file = self._storage.job_log_file(job_id)
        exit_code_file = self._storage.job_dir(job_id) / ".exitcode"

        try:
            cancelled = await self._monitor_process(
                job_id, pid, cancel_event, log_file, proc=None
            )

            if cancelled:
                return  # cancel handler set state

            # Process exited -- wait for .exitcode file to be written
            exit_code = None
            for _attempt in range(5):
                exit_code = self._read_exit_code(exit_code_file)
                if exit_code is not None:
                    break
                await asyncio.sleep(0.5)

            self._record_output_artifacts(job_id)

            if exit_code is not None and exit_code == 0:
                self._store.update_state(job_id, JobState.COMPLETED)
                logger.info(
                    "Reattached job %s completed successfully", job_id
                )
            else:
                error = (
                    f"Process exited with code {exit_code}"
                    if exit_code is not None
                    else "Process exited without recording exit code "
                    "(may have been killed)"
                )
                tail = self._read_log_tail(log_file)
                if tail:
                    error += f"\nLast output:\n{tail}"
                self._store.update_state(
                    job_id, JobState.FAILED, error_message=error
                )
                logger.warning(
                    "Reattached job %s failed (exit code: %s)",
                    job_id,
                    exit_code,
                )

        except asyncio.CancelledError:
            logger.info(
                "Detaching from reattached job %s (worker shutting down)",
                job_id,
            )
        except Exception as e:
            logger.exception(
                "Error monitoring reattached job %s: %s", job_id, e
            )
            try:
                self._store.update_state(
                    job_id, JobState.FAILED, error_message=str(e)
                )
            except (ValueError, KeyError):
                pass
        finally:
            self._cancel_events.pop(job_id, None)
            self._monitored_jobs.discard(job_id)
            if job_id in self._log_queues:
                await self._log_queues[job_id].put(None)

    async def _resume_cancel(self, job_id: str, pid: int) -> None:
        """Resume an interrupted cancellation after worker restart."""
        self._monitored_jobs.add(job_id)
        try:
            await self._handle_cancel_by_pid(job_id, pid)
        except Exception as e:
            logger.exception(
                "Error resuming cancel for job %s: %s", job_id, e
            )
            try:
                self._store.update_state(
                    job_id, JobState.FAILED, error_message=str(e)
                )
            except (ValueError, KeyError):
                pass
        finally:
            self._monitored_jobs.discard(job_id)

    # ── Process lifecycle ──────────────────────────────────────────────

    def cleanup_terminal_job_dirs(
        self,
        *,
        exclude_job_id: str | None = None,
        project: str | None = None,
        limit: int = 1000,
    ) -> dict:
        """Delete files for terminal jobs while preserving DB records."""
        cleaned: list[dict] = []
        bytes_freed = 0
        for job in self._store.list_terminal_jobs(project=project, limit=limit):
            job_id = str(job["job_id"])
            if exclude_job_id and job_id == exclude_job_id:
                continue
            freed = self._storage.cleanup_job(job_id)
            if freed:
                bytes_freed += freed
                cleaned.append(
                    {
                        "job_id": job_id,
                        "state": job.get("state"),
                        "project": job.get("project"),
                        "bytes_freed": freed,
                    }
                )
        return {
            "cleaned": cleaned,
            "cleaned_count": len(cleaned),
            "project": project,
            "limit": limit,
            "bytes_freed": bytes_freed,
            "gb_freed": round(bytes_freed / (1024**3), 3),
            "disk_free_gb": self._storage.disk_free_gb(),
        }

    def _validate_resources(self, job_id: str, spec: JobSpec) -> None:
        """Check that the server can satisfy the job's resource requirements."""
        from gpuharbor.worker.gpu import get_gpu_info

        gpus = get_gpu_info()
        if spec.resources.gpu_count > len(gpus):
            raise ValueError(
                f"Job requires {spec.resources.gpu_count} GPU(s) but server has {len(gpus)}"
            )

        if spec.resources.disk_gb_min > 0:
            free_gb = self._storage.disk_free_gb()
            if (
                free_gb < spec.resources.disk_gb_min
                and os.environ.get("GPUHARBOR_CLEANUP_ON_LOW_DISK", "1").lower()
                not in {"0", "false", "no"}
            ):
                cleanup = self.cleanup_terminal_job_dirs(exclude_job_id=job_id)
                logger.info(
                    "Low disk cleanup before job %s: freed %.3f GB; disk_free_gb=%.1f",
                    job_id,
                    cleanup["gb_freed"],
                    cleanup["disk_free_gb"],
                )
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
                job_id,
                "input_checkpoint",
                f"jobs/{job_id}/input/{filename}",
                compute_sha256(dest),
            )

    async def _start_process(
        self, job_id: str, spec: JobSpec
    ) -> asyncio.subprocess.Process:
        """Spawn the command as a subprocess in its own session.

        The process writes stdout/stderr directly to the log file (no pipe)
        so it survives worker restarts without SIGPIPE.  A bash wrapper
        records the exit code to a file for crash recovery.
        """
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
            visible = ",".join(str(i) for i in range(spec.resources.gpu_count))
            env["CUDA_VISIBLE_DEVICES"] = visible

        cmd = spec.command
        logger.info("Running job %s: %s", job_id, " ".join(cmd))

        log_file = self._storage.job_log_file(job_id)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        # Wrap command to record exit code for crash recovery
        exit_code_file = job_dir / ".exitcode"
        wrapped_cmd = (
            f"{shlex.join(cmd)}\n"
            f"_ec=$?\n"
            f"printf '%d' \"$_ec\" > {shlex.quote(str(exit_code_file))}\n"
            f'exit "$_ec"'
        )

        # Subprocess writes directly to log file (survives worker restarts)
        log_fd = open(log_file, "wb")
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                wrapped_cmd,
                stdout=log_fd,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=str(job_dir),
                start_new_session=True,
            )
        finally:
            log_fd.close()

        self._processes[job_id] = proc
        self._store.update_container_id(job_id, str(proc.pid))
        logger.info("Started process PID %d for job %s", proc.pid, job_id)

        return proc

    # ── Unified process monitoring ─────────────────────────────────────

    async def _monitor_process(
        self,
        job_id: str,
        pid: int,
        cancel_event: asyncio.Event,
        log_file: Path,
        proc: asyncio.subprocess.Process | None = None,
    ) -> bool:
        """Monitor a process (new or reattached).

        Tails the log file for live streaming, handles cancellation, waits
        for the process to exit.

        Returns True if cancelled, False if the process exited on its own.
        """
        log_queue = self._log_queues.get(job_id)
        tail_stop = asyncio.Event()

        async def _tail_log() -> None:
            """Tail the log file and push new lines to the live queue."""
            pos = 0
            buffer = b""
            while not tail_stop.is_set():
                try:
                    if log_file.exists():
                        with open(log_file, "rb") as f:
                            f.seek(pos)
                            new_data = f.read()
                        if new_data:
                            pos += len(new_data)
                            buffer += new_data
                            while b"\n" in buffer:
                                line_bytes, buffer = buffer.split(b"\n", 1)
                                line = line_bytes.decode(
                                    "utf-8", errors="replace"
                                )
                                if log_queue:
                                    try:
                                        log_queue.put_nowait(line)
                                    except asyncio.QueueFull:
                                        try:
                                            log_queue.get_nowait()
                                        except asyncio.QueueEmpty:
                                            pass
                                        log_queue.put_nowait(line)
                except (IOError, OSError):
                    pass
                await asyncio.sleep(0.3)

            # Flush remaining partial line
            if buffer and log_queue:
                line = buffer.decode("utf-8", errors="replace").rstrip("\n")
                if line:
                    try:
                        log_queue.put_nowait(line)
                    except asyncio.QueueFull:
                        pass

        async def _wait_for_exit() -> None:
            """Wait for the process to terminate."""
            if proc is not None:
                await proc.wait()
            else:
                # Reattached process: poll PID
                while True:
                    if not self._is_pid_alive(pid):
                        return
                    await asyncio.sleep(2)

        async def _watch_cancel() -> None:
            """Watch for a cancellation request."""
            while not cancel_event.is_set():
                await asyncio.sleep(1)
            # Cancel requested
            if proc is not None:
                await self._handle_cancel(job_id, proc)
            else:
                await self._handle_cancel_by_pid(job_id, pid)

        tail_task = asyncio.create_task(_tail_log())
        exit_task = asyncio.create_task(_wait_for_exit())
        cancel_task = asyncio.create_task(_watch_cancel())

        try:
            await asyncio.wait(
                [exit_task, cancel_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            cancelled = cancel_event.is_set()
            tail_stop.set()

            if not cancelled:
                # Give a moment for final log writes to flush to disk
                await asyncio.sleep(0.5)

            return cancelled
        finally:
            tail_stop.set()
            for t in [tail_task, exit_task, cancel_task]:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    # ── Cancellation ───────────────────────────────────────────────────

    async def _handle_cancel(
        self, job_id: str, proc: asyncio.subprocess.Process
    ) -> None:
        """Gracefully cancel a running process (with Process handle)."""
        logger.info(
            "Cancelling job %s (grace period: %ds)",
            job_id,
            self._grace_period,
        )

        try:
            self._store.update_state(job_id, JobState.CANCEL_REQUESTED)
        except (ValueError, KeyError):
            pass

        # SIGTERM to process group (bash wrapper + child command)
        self._signal_process_group(proc.pid, signal.SIGTERM)

        # Wait for grace period
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._grace_period)
        except asyncio.TimeoutError:
            # Force kill
            logger.warning("Force-killing process group for job %s", job_id)
            self._signal_process_group(proc.pid, signal.SIGKILL)
            try:
                await proc.wait()
            except Exception:
                pass

        self._record_output_artifacts(job_id)
        self._store.update_state(job_id, JobState.CANCELED)
        logger.info("Job %s canceled", job_id)

    async def _handle_cancel_by_pid(self, job_id: str, pid: int) -> None:
        """Cancel a process by PID (for reattached jobs without Process handle)."""
        logger.info(
            "Cancelling reattached job %s PID %d (grace period: %ds)",
            job_id,
            pid,
            self._grace_period,
        )

        try:
            self._store.update_state(job_id, JobState.CANCEL_REQUESTED)
        except (ValueError, KeyError):
            pass

        # SIGTERM to process group
        self._signal_process_group(pid, signal.SIGTERM)

        # Poll until dead or timeout
        for _ in range(self._grace_period * 2):  # check every 0.5s
            if not self._is_pid_alive(pid):
                break
            await asyncio.sleep(0.5)
        else:
            # Still alive -- force kill
            logger.warning(
                "Force-killing process group for reattached job %s", job_id
            )
            self._signal_process_group(pid, signal.SIGKILL)
            await asyncio.sleep(1)

        self._record_output_artifacts(job_id)
        self._store.update_state(job_id, JobState.CANCELED)
        logger.info("Reattached job %s canceled", job_id)

    async def cancel_job(self, job_id: str) -> bool:
        """Request cancellation of a running job."""
        cancel_event = self._cancel_events.get(job_id)
        if cancel_event is None:
            return False
        cancel_event.set()
        return True

    # ── Output artifacts ───────────────────────────────────────────────

    def _record_output_artifacts(self, job_id: str) -> None:
        """Scan output and checkpoint dirs and record artifacts in the store."""
        for subdir, default_type in [
            ("output", "final_model"),
            ("checkpoints", "checkpoint"),
        ]:
            files = self._storage.list_job_files(job_id, subdir)
            for f in files:
                abs_path = self._storage.job_dir(job_id) / f["path"]
                sha = compute_sha256(abs_path) if abs_path.exists() else None
                artifact_type = default_type
                name_lower = f["name"].lower()
                if "log" in name_lower or name_lower.endswith(
                    (".log", ".txt")
                ):
                    artifact_type = "training_log"
                elif "config" in name_lower or name_lower.endswith(
                    (".yaml", ".yml", ".json")
                ):
                    artifact_type = "config"

                self._store.add_artifact(
                    job_id, artifact_type, f"jobs/{job_id}/{f['path']}", sha
                )

        log_file = self._storage.job_log_file(job_id)
        if log_file.exists():
            self._store.add_artifact(
                job_id,
                "training_log",
                f"jobs/{job_id}/logs/container.log",
                compute_sha256(log_file),
            )

    # ── Log streaming ──────────────────────────────────────────────────

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

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """Check if a process is still alive."""
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but different user

    @staticmethod
    def _signal_process_group(pid: int, sig: int) -> None:
        """Send a signal to an entire process group."""
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    @staticmethod
    def _read_exit_code(exit_code_file: Path) -> int | None:
        """Read the exit code from the .exitcode file written by the bash wrapper."""
        if not exit_code_file.exists():
            return None
        try:
            return int(exit_code_file.read_text().strip())
        except (ValueError, OSError):
            return None

    @staticmethod
    def _read_log_tail(log_file: Path, n_lines: int = 20) -> str:
        """Read the last N lines of a log file."""
        if not log_file.exists():
            return ""
        with open(log_file, "rb") as f:
            data = f.read()
            lines = data.decode("utf-8", errors="replace").splitlines()
            return "\n".join(lines[-n_lines:])
