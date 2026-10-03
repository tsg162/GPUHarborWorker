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
import json
import shutil
import uuid
import os
import secrets
import shlex
import signal
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from gpuharbor.common.job_spec import JobSpec
from gpuharbor.common.states import JobState
from gpuharbor.common.storage import LocalStorage, compute_sha256
from gpuharbor.worker.state import JobStore
from gpuharbor.worker.backup import BackupManager
from gpuharbor.worker.archives import extract_archive
from gpuharbor.worker.environment import prepare_environment
from gpuharbor.worker.checkpoints import record_file, reconcile_artifacts

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessIdentity:
    """Durable identity for a Linux process across worker restarts."""

    pid: int
    start_time: str
    pgid: int
    marker: str


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
        backup: BackupManager | None = None,
    ):
        self._storage = storage
        self._store = job_store
        self._grace_period = default_grace_period
        self.backup = backup or BackupManager(storage, job_store)
        self._gpu_lock = asyncio.Lock()
        self._admission_lock = asyncio.Lock()
        self._gpu_allocations: dict[str, list[int]] = {}
        self._training_python: dict[str, str] = {}

        # Track running processes for cancel support
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._process_identities: dict[str, ProcessIdentity] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
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
        process_started = False

        try:
            await asyncio.to_thread(self._validate_resources, job_id, spec)
            if job_id not in self._gpu_allocations:
                await self.reserve_gpus(job_id, spec.resources.gpu_count)
                self._store.set_gpu_ids(job_id, self._gpu_allocations[job_id])
            await self._prepare_workspace(job_id, spec)
            proc = await self._start_process(job_id, spec)
            process_started = True

            log_file = self._storage.job_log_file(job_id)
            cancelled = await self._monitor_process(
                job_id,
                proc.pid,
                cancel_event,
                log_file,
                proc=proc,
                identity=self._process_identities.get(job_id),
            )

            if cancelled:
                return  # cancel handler already set state

            if proc.returncode != 0:
                tail = self._read_log_tail(log_file)
                raise RuntimeError(
                    f"Process exited with code {proc.returncode}.\n"
                    f"Last output:\n{tail}"
                )

            await asyncio.to_thread(self._record_output_artifacts, job_id)
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
            self._process_identities.pop(job_id, None)
            self._cancel_events.pop(job_id, None)
            self._monitored_jobs.discard(job_id)
            await self._finalize_terminal(job_id)

    # ── Re-attach to running jobs after restart ────────────────────────

    async def reattach_running_jobs(
        self,
        checkpoint_mgr=None,
    ) -> dict[str, asyncio.Task]:
        """Re-attach to running jobs from a previous worker instance.

        Returns a dict of {job_id: asyncio.Task} for the caller to track.
        """
        interrupted_jobs = self._store.get_nonterminal_jobs()
        if not interrupted_jobs:
            return {}

        tasks: dict[str, asyncio.Task] = {}

        for job in interrupted_jobs:
            job_id = str(job["job_id"])
            state = JobState(job["state"])

            if state in {JobState.CREATED, JobState.UPLOADING_INPUTS}:
                self._fail_interrupted_job(
                    job_id,
                    f"Worker restarted while job was in {state.value}; "
                    "no process was durably started",
                )
                continue

            identity = self.process_identity_from_job(job)
            exit_code = self._read_completed_execution(job_id, job)
            if exit_code is not None and (identity is None or not self.execution_identity_is_valid(identity)):
                if state == JobState.CHECKPOINTING:
                    self._store.update_state(job_id, JobState.RUNNING)
                await asyncio.to_thread(self._record_output_artifacts, job_id)
                terminal = JobState.CANCELED if state == JobState.CANCEL_REQUESTED else (JobState.COMPLETED if exit_code == 0 else JobState.FAILED)
                self._store.update_state(job_id, terminal, error_message=None if exit_code == 0 else f"Process exited with code {exit_code}")
                await self._finalize_terminal(job_id)
                continue
            if identity is None:
                self._fail_interrupted_job(
                    job_id,
                    "Worker restarted without a complete durable process identity",
                )
                continue
            if not self.execution_identity_is_valid(identity):
                self._fail_interrupted_job(
                    job_id,
                    "Stored process identity no longer matches the live process "
                    "(process exited or PID was reused)",
                )
                continue

            # Restore leases BEFORE accepting new jobs. Legacy jobs without
            # assignments conservatively reserve every device.
            from gpuharbor.worker.gpu import get_gpu_count
            ids = job.get("gpu_ids")
            if ids is None:
                ids = list(range(await asyncio.to_thread(get_gpu_count)))
            self._gpu_allocations[job_id] = ids
            self._store.set_gpu_ids(job_id, ids)

            # If cancel was in progress when we restarted, resume it
            if state == JobState.CANCEL_REQUESTED:
                logger.info(
                    "Resuming cancellation of job %s (PID %d)",
                    job_id,
                    identity.pid,
                )
                task = asyncio.create_task(self._resume_cancel(job_id, identity))
                tasks[job_id] = task
                continue

            if state == JobState.CHECKPOINTING:
                self._store.update_state(job_id, JobState.RUNNING)

            logger.info(
                "Re-attaching to job %s (PID %d)", job_id, identity.pid
            )

            # Restart checkpoint monitoring if enabled
            if checkpoint_mgr and job.get("spec"):
                ckpt_cfg = job["spec"].get("checkpointing", {})
                if ckpt_cfg.get("enabled"):
                    checkpoint_mgr.start_monitoring(
                        job_id=job_id,
                        interval_minutes=ckpt_cfg.get("save_every_minutes", 10),
                        keep_last_n=ckpt_cfg.get("keep_last_n", 3),
                    )

            task = asyncio.create_task(self._reattach_job(job_id, identity))
            tasks[job_id] = task

        if tasks:
            logger.info("Re-attached to %d running job(s)", len(tasks))

        return tasks

    async def _reattach_job(
        self,
        job_id: str,
        identity: ProcessIdentity,
    ) -> None:
        """Re-attach to a single running job process."""
        self._monitored_jobs.add(job_id)
        self._process_identities[job_id] = identity
        cancel_event = asyncio.Event()
        self._cancel_events[job_id] = cancel_event

        log_file = self._storage.job_log_file(job_id)
        exit_code_file = self._storage.job_dir(job_id) / ".exitcode"

        try:
            cancelled = await self._monitor_process(
                job_id,
                identity.pid,
                cancel_event,
                log_file,
                proc=None,
                identity=identity,
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

            await asyncio.to_thread(self._record_output_artifacts, job_id)

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
            self._process_identities.pop(job_id, None)
            self._cancel_events.pop(job_id, None)
            self._monitored_jobs.discard(job_id)
            await self._finalize_terminal(job_id)

    async def _resume_cancel(
        self,
        job_id: str,
        identity: ProcessIdentity,
    ) -> None:
        """Resume an interrupted cancellation after worker restart."""
        self._monitored_jobs.add(job_id)
        self._process_identities[job_id] = identity
        try:
            await self._handle_cancel_by_identity(job_id, identity)
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
            self._process_identities.pop(job_id, None)
            self._monitored_jobs.discard(job_id)
            await self._finalize_terminal(job_id)

    # ── Process lifecycle ──────────────────────────────────────────────

    async def reserve_gpus(self, job_id: str, count: int) -> list[int]:
        from gpuharbor.worker.gpu import get_gpu_count
        total = await asyncio.to_thread(get_gpu_count)
        async with self._gpu_lock:
            if job_id in self._gpu_allocations:
                return self._gpu_allocations[job_id]
            occupied = {gpu for ids in self._gpu_allocations.values() for gpu in ids}
            free = [i for i in range(total) if i not in occupied]
            if len(free) < count:
                raise ValueError(f"GPU busy: requested {count}, free {len(free)} of {total}. Retry when a job finishes.")
            self._gpu_allocations[job_id] = free[:count]
            return free[:count]

    def release_gpus(self, job_id: str) -> None:
        self._gpu_allocations.pop(job_id, None)

    async def _finalize_terminal(self, job_id: str) -> None:
        job = self._store.get_job(job_id)
        if not job or job["state"] not in {"completed", "failed", "canceled"}:
            return
        self.release_gpus(job_id)
        self._training_python.pop(job_id, None)
        try:
            await asyncio.to_thread(self._record_output_artifacts, job_id)
            if self.backup.destination and job["spec"].get("backup", True):
                await asyncio.to_thread(self.backup.snapshot, job_id)
        except Exception as exc:
            logger.exception("Artifact backup/finalization failed for %s", job_id)
            receipt = (self._store.get_job(job_id) or {}).get("backup") or {}
            self._store.set_backup(job_id, {**receipt, "last_error": str(exc)})

    def cleanup_terminal_job_dirs(
        self, *, exclude_job_id: str | None = None, project: str | None = None,
        limit: int = 1000, dry_run: bool = True, force: bool = False,
        only_job_id: str | None = None,
    ) -> dict:
        """Explicit cleanup. Pinned jobs and unverified outputs stay protected."""
        cleaned, skipped = [], []
        bytes_freed = 0
        with self.backup._lock:
            jobs = ([self._store.get_job(only_job_id)] if only_job_id else self._store.list_terminal_jobs(project=project, limit=limit))
            for job in jobs:
                if not job or job["job_id"] == exclude_job_id:
                    continue
                job_id = str(job["job_id"])
                reason = None
                if job["state"] not in {"completed", "failed", "canceled"}:
                    reason = "job is active"
                elif job.get("pinned"):
                    reason = "job is pinned"
                elif not force and not self.backup.covers_workspace(job_id):
                    reason = "no matching verified terminal backup; back up first or explicitly force cleanup"
                if reason:
                    skipped.append({"job_id": job_id, "reason": reason})
                    continue
                freed = self._storage.path_size_bytes(self._storage.job_dir(job_id))
                if not dry_run:
                    freed = self._storage.cleanup_job(job_id)
                    self._store.remove_artifacts(job_id, [a["uri"] for a in self._store.get_artifacts(job_id)])
                bytes_freed += freed
                cleaned.append({"job_id": job_id, "state": job["state"], "project": job["project"], "bytes_freed": freed})
        return {"dry_run": dry_run, "cleaned": cleaned, "skipped": skipped, "cleaned_count": len(cleaned), "project": project, "limit": limit, "bytes_freed": bytes_freed, "gb_freed": round(bytes_freed / 1024**3, 3), "disk_free_gb": self._storage.disk_free_gb()}

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
            if free_gb < spec.resources.disk_gb_min:
                raise ValueError(
                    f"Job requires {spec.resources.disk_gb_min}GB free disk "
                    f"but only {free_gb}GB available"
                )

    async def _prepare_workspace(self, job_id: str, spec: JobSpec) -> None:
        """Create workspace dirs and copy input artifacts into place."""
        self._store.update_state(job_id, JobState.UPLOADING_INPUTS)
        await asyncio.to_thread(self._prepare_workspace_sync, job_id, spec)

    def _prepare_workspace_sync(self, job_id: str, spec: JobSpec) -> None:
        job_dir = self._storage.ensure_job_dirs(job_id)
        project = job_dir / "project"
        project.mkdir(exist_ok=True)
        # A tiny stdlib-only helper is importable from any training interpreter.
        from gpuharbor.common import training
        runtime = job_dir / "runtime"
        runtime.mkdir(exist_ok=True)
        shutil.copyfile(training.__file__, runtime / "gpuharbor_training.py")
        for filename, destination in [(spec.source_archive, project), (spec.resume_archive, job_dir / "input" / "resume")]:
            if filename:
                source = self._storage.get_file(f"uploads/{filename}")
                if source is None:
                    raise ValueError(f"Upload not found: {filename}")
                extract_archive(source, destination, int(self._storage.disk_free_gb() * 1024**3))
        cwd = (project / spec.work_dir).resolve()
        cwd.relative_to(project.resolve())
        if not cwd.is_dir():
            raise ValueError(f"Working directory does not exist: {spec.work_dir}")
        if spec.artifacts.dataset and not Path(spec.artifacts.dataset).is_dir():
            raise ValueError(f"Dataset directory does not exist: {spec.artifacts.dataset}")
        cache = Path(os.environ.get("GPUHARBOR_CACHE_ROOT", str(self._storage.root / "cache")))
        cache.mkdir(parents=True, exist_ok=True)
        self._training_python[job_id] = prepare_environment(spec.environment, project, cache)

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
            dest_existed = dest.exists()
            if not dest_existed:
                dest, _sha = self._storage.copy_to_job_input(job_id, src)
                logger.info("Copied checkpoint %s -> %s", src, dest)

            # copy_to_job_input already computed this checksum.
            sha = _sha if not dest_existed else compute_sha256(dest)
            stat = dest.stat()
            self._store.add_artifact(job_id, "input_checkpoint", f"jobs/{job_id}/input/{filename}", sha, stat.st_size,
                                     f"{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}")

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
        execution_marker = secrets.token_hex(32)
        env["GPUHARBOR_EXECUTION_ID"] = execution_marker

        env["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in self._gpu_allocations[job_id])
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = str(job_dir / "runtime") + os.pathsep + env.get("PYTHONPATH", "")
        cache = Path(os.environ.get("GPUHARBOR_CACHE_ROOT", str(self._storage.root / "cache")))
        for key, folder in [("HF_HOME", "huggingface"), ("TORCH_HOME", "torch"), ("PIP_CACHE_DIR", "pip")]:
            env.setdefault(key, str(cache / folder))
        if spec.artifacts.dataset:
            env["GPUHARBOR_DATASET_DIR"] = str(Path(spec.artifacts.dataset).resolve())
        if spec.resume_archive:
            env["GPUHARBOR_RESUME_DIR"] = str(job_dir / "input" / "resume")
        python = self._training_python.get(job_id, spec.environment.python)
        env["PATH"] = str(Path(python).parent) + os.pathsep + env.get("PATH", "")
        cmd = list(spec.command)
        if cmd[0] in {"python", "python3"}:
            cmd[0] = python
        logger.info("Running job %s: %s", job_id, " ".join(cmd))

        log_file = self._storage.job_log_file(job_id)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        # Wrap command to record exit code for crash recovery
        exit_code_file = job_dir / ".exitcode"
        result_file = job_dir / ".execution-result.json"
        result_tmp = job_dir / ".execution-result.json.tmp"
        wrapped_cmd = (
            "trap ':' TERM INT\n"
            f"{shlex.join(cmd)}\n"
            f"_ec=$?\n"
            f"printf '%d' \"$_ec\" > {shlex.quote(str(exit_code_file))}\n"
            f"printf '{{\"marker\":\"{execution_marker}\",\"exit_code\":%d}}' \"$_ec\" > {shlex.quote(str(result_tmp))}\n"
            f"mv {shlex.quote(str(result_tmp))} {shlex.quote(str(result_file))}\n"
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
                cwd=str(job_dir / "project" / spec.work_dir),
                start_new_session=True,
            )
        finally:
            log_fd.close()

        self._processes[job_id] = proc
        identity = self._capture_process_identity(proc.pid, execution_marker)
        if identity is not None:
            self._process_identities[job_id] = identity
            self._store.update_execution_identity(
                job_id,
                pid=identity.pid,
                start_time=identity.start_time,
                pgid=identity.pgid,
                marker=identity.marker,
            )
            logger.info(
                "Started process PID %d (start=%s, pgid=%d) for job %s",
                identity.pid,
                identity.start_time,
                identity.pgid,
                job_id,
            )
        else:
            logger.warning(
                "Could not capture durable identity for job %s PID %d; "
                "the current worker can monitor it, but restart recovery will "
                "fail the job safely",
                job_id,
                proc.pid,
            )

        return proc

    # ── Unified process monitoring ─────────────────────────────────────

    async def _monitor_process(
        self,
        job_id: str,
        pid: int,
        cancel_event: asyncio.Event,
        log_file: Path,
        proc: asyncio.subprocess.Process | None = None,
        identity: ProcessIdentity | None = None,
    ) -> bool:
        """Monitor a process (new or reattached).

        Tails the log file for live streaming, handles cancellation, waits
        for the process to exit.

        Returns True if cancelled, False if the process exited on its own.
        """
        async def _wait_for_exit() -> None:
            """Wait for the process to terminate."""
            if proc is not None:
                await proc.wait()
            else:
                # Reattached process: poll the full identity, not only its PID.
                if identity is None:
                    raise RuntimeError(
                        f"Cannot monitor reattached job {job_id} without "
                        "a durable process identity"
                    )
                while True:
                    if not self.execution_identity_is_valid(identity):
                        return
                    await asyncio.sleep(2)

        async def _watch_cancel() -> None:
            """Watch for a cancellation request."""
            await cancel_event.wait()
            # Cancel requested
            if proc is not None:
                await self._handle_cancel(job_id, proc, identity)
            else:
                if identity is None:
                    raise RuntimeError(
                        f"Cannot cancel reattached job {job_id} without "
                        "a durable process identity"
                    )
                await self._handle_cancel_by_identity(job_id, identity)

        exit_task = asyncio.create_task(_wait_for_exit())
        cancel_task = asyncio.create_task(_watch_cancel())

        try:
            done, _pending = await asyncio.wait(
                [exit_task, cancel_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for completed_task in done:
                completed_task.result()

            # The event may be set just after the process exits but before the
            # cancel watcher runs. Only report cancellation when its handler
            # actually completed and set a terminal state.
            if cancel_event.is_set() and cancel_task not in done:
                await cancel_task
            cancelled = cancel_event.is_set()

            if not cancelled:
                # Give a moment for final log writes to flush to disk
                await asyncio.sleep(0.5)

            return cancelled
        finally:
            for t in [exit_task, cancel_task]:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    # ── Cancellation ───────────────────────────────────────────────────

    async def _handle_cancel(
        self,
        job_id: str,
        proc: asyncio.subprocess.Process,
        identity: ProcessIdentity | None,
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

        if proc.returncode is not None:
            await asyncio.to_thread(self._record_output_artifacts, job_id)
            self._store.update_state(job_id, JobState.CANCELED)
            return
        if identity is None or not self._signal_process_group(
            identity, signal.SIGTERM
        ):
            raise RuntimeError(
                f"Refusing to signal job {job_id}: durable process identity "
                "is missing or no longer matches"
            )

        # Wait for grace period
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._grace_period)
        except asyncio.TimeoutError:
            # Force kill
            logger.warning("Force-killing process group for job %s", job_id)
            if not self._signal_process_group(identity, signal.SIGKILL):
                raise RuntimeError(
                    f"Refusing to force-kill job {job_id}: durable process "
                    "identity no longer matches"
                )
            try:
                await proc.wait()
            except Exception:
                pass

        await asyncio.to_thread(self._record_output_artifacts, job_id)
        self._store.update_state(job_id, JobState.CANCELED)
        logger.info("Job %s canceled", job_id)

    async def _handle_cancel_by_identity(
        self,
        job_id: str,
        identity: ProcessIdentity,
    ) -> None:
        """Cancel a reattached process only while its full identity matches."""
        logger.info(
            "Cancelling reattached job %s PID %d (grace period: %ds)",
            job_id,
            identity.pid,
            self._grace_period,
        )

        try:
            self._store.update_state(job_id, JobState.CANCEL_REQUESTED)
        except (ValueError, KeyError):
            pass

        if not self._signal_process_group(identity, signal.SIGTERM):
            raise RuntimeError(
                f"Refusing to signal reattached job {job_id}: stored process "
                "identity no longer matches"
            )

        # Poll until dead or timeout
        for _ in range(self._grace_period * 2):  # check every 0.5s
            if not self.execution_identity_is_valid(identity):
                break
            await asyncio.sleep(0.5)
        else:
            # Still alive -- force kill
            logger.warning(
                "Force-killing process group for reattached job %s", job_id
            )
            if not self._signal_process_group(identity, signal.SIGKILL):
                raise RuntimeError(
                    f"Refusing to force-kill reattached job {job_id}: stored "
                    "process identity no longer matches"
                )
            await asyncio.sleep(1)

        await asyncio.to_thread(self._record_output_artifacts, job_id)
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
        with self.backup._lock:
            for subdir, kind in [("output", "final_model"), ("checkpoints", "checkpoint"), ("logs", "training_log")]:
                for entry in self._storage.list_job_files(job_id, subdir):
                    path = self._storage.get_file(f"jobs/{job_id}/{entry['path']}")
                    if path is not None:
                        record_file(self._storage, self._store, job_id, path, kind)
            reconcile_artifacts(self._storage, self._store, job_id)

    async def stream_logs(self, job_id: str, offset: int = 0, tail: int = 0, with_offsets: bool = False) -> AsyncIterator[str]:
        """Independent bounded file cursor for every follower; no competing consumers."""
        log_file = self._storage.job_log_file(job_id)
        position = offset
        pending = b""
        if tail and offset == 0 and log_file.exists():
            position = await asyncio.to_thread(self._tail_offset, log_file, tail)
        while True:
            def read_chunk():
                if not log_file.exists():
                    return b""
                with log_file.open("rb") as source:
                    source.seek(position)
                    return source.read(64 * 1024)
            chunk = await asyncio.to_thread(read_chunk)
            position += len(chunk)
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                yield (line.decode("utf-8", errors="replace"), position - len(pending)) if with_offsets else line.decode("utf-8", errors="replace")
            # Bound memory for very long lines (e.g. progress bars).
            if len(pending) >= 64 * 1024:
                yield (pending.decode("utf-8", errors="replace"), position) if with_offsets else pending.decode("utf-8", errors="replace")
                pending = b""
            job = self._store.get_job(job_id)
            if not chunk and (not job or job["state"] in {"completed", "failed", "canceled"}):
                if pending:
                    yield (pending.decode("utf-8", errors="replace"), position) if with_offsets else pending.decode("utf-8", errors="replace")
                return
            if not chunk:
                await asyncio.sleep(0.3)

    @staticmethod
    def _tail_offset(path: Path, count: int) -> int:
        # Scan backwards in fixed blocks, including files with a single huge
        # line. Never accumulate an entire log just to locate its tail.
        with path.open("rb") as source:
            source.seek(0, 2)
            position = source.tell()
            if not position or count <= 0:
                return 0
            source.seek(position - 1)
            remaining = count + (1 if source.read(1) == b"\n" else 0)
            while position:
                amount = min(position, 64 * 1024)
                position -= amount
                source.seek(position)
                data = source.read(amount)
                for index in range(len(data) - 1, -1, -1):
                    if data[index] == 10:
                        remaining -= 1
                        if remaining == 0:
                            return position + index + 1
            return 0

    def _read_completed_execution(self, job_id: str, job: dict) -> int | None:
        result = self._storage.job_dir(job_id) / ".execution-result.json"
        if result.exists():
            try:
                payload = json.loads(result.read_text())
                if payload["marker"] == job.get("process_marker") and type(payload["exit_code"]) is int:
                    return payload["exit_code"]
            except (ValueError, KeyError, OSError):
                pass
            return None
        # Older workers wrote only .exitcode, inside a unique job directory.
        if job.get("process_marker"):
            return self._read_exit_code(self._storage.job_dir(job_id) / ".exitcode")
        return None

    def get_log_file_path(self, job_id: str) -> Path | None:
        log_file = self._storage.job_log_file(job_id)
        return log_file if log_file.exists() else None

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _read_process_start_time(pid: int) -> str | None:
        """Read Linux /proc start time (clock ticks since boot) for a PID."""
        try:
            stat_text = Path(f"/proc/{pid}/stat").read_text()
            fields_after_comm = stat_text.rsplit(")", 1)[1].split()
            return fields_after_comm[19]
        except (FileNotFoundError, IndexError, OSError):
            return None

    @classmethod
    def _capture_process_identity(
        cls,
        pid: int,
        expected_marker: str,
    ) -> ProcessIdentity | None:
        """Capture a process identity and verify the worker-owned marker."""
        try:
            start_time = cls._read_process_start_time(pid)
            if start_time is None:
                return None
            pgid = os.getpgid(pid)
            environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            expected = f"GPUHARBOR_EXECUTION_ID={expected_marker}".encode()
            if expected not in environ:
                return None
            return ProcessIdentity(
                pid=pid,
                start_time=start_time,
                pgid=pgid,
                marker=expected_marker,
            )
        except (ProcessLookupError, PermissionError, OSError):
            return None

    @staticmethod
    def process_identity_from_job(job: dict) -> ProcessIdentity | None:
        """Parse a complete persisted identity from a job record."""
        values = (
            job.get("container_id"),
            job.get("process_start_time"),
            job.get("process_pgid"),
            job.get("process_marker"),
        )
        if any(value is None for value in values):
            return None
        try:
            pid = int(values[0])
            start_time = str(values[1])
            pgid = int(values[2])
            marker = str(values[3])
        except (TypeError, ValueError):
            return None
        if pid <= 0 or pgid <= 0 or not start_time or not marker:
            return None
        return ProcessIdentity(pid, start_time, pgid, marker)

    @classmethod
    def execution_identity_is_valid(cls, identity: ProcessIdentity) -> bool:
        """Validate PID, start time, PGID, and marker against /proc."""
        current = cls._capture_process_identity(identity.pid, identity.marker)
        return current == identity

    @classmethod
    def _signal_process_group(
        cls,
        identity: ProcessIdentity,
        sig: int,
    ) -> bool:
        """Signal the expected group only after revalidating process identity."""
        if not cls.execution_identity_is_valid(identity):
            return False
        try:
            os.killpg(identity.pgid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def _fail_interrupted_job(self, job_id: str, reason: str) -> None:
        """Move an unrecoverable nonterminal record to FAILED."""
        logger.warning("Failing interrupted job %s: %s", job_id, reason)
        try:
            self._store.update_state(
                job_id,
                JobState.FAILED,
                error_message=reason,
            )
        except (KeyError, ValueError):
            logger.exception("Could not reconcile interrupted job %s", job_id)

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
            f.seek(JobExecutor._tail_offset(log_file, n_lines))
            data = f.read(128 * 1024)
            lines = data.decode("utf-8", errors="replace").splitlines()
            return "\n".join(lines[-n_lines:])
