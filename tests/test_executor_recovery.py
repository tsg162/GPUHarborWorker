from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gpuharbor.common.job_spec import JobSpec
from gpuharbor.common.states import JobState
from gpuharbor.common.storage import LocalStorage
from gpuharbor.worker.executor import JobExecutor, ProcessIdentity
from gpuharbor.worker.state import JobStore


class ExecutorRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name)
        self.storage = LocalStorage(base / "storage")
        self.store = JobStore(base / "jobs.db")
        self.executor = JobExecutor(self.storage, self.store, default_grace_period=1)
        self.spec = JobSpec(name="test", command=["true"])

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_job(self, job_id: str, state: JobState) -> None:
        self.store.create_job(
            self.spec.model_dump_json(),
            self.spec.name,
            job_id=job_id,
        )
        if state in {
            JobState.UPLOADING_INPUTS,
            JobState.RUNNING,
            JobState.CHECKPOINTING,
            JobState.CANCEL_REQUESTED,
        }:
            self.store.update_state(job_id, JobState.UPLOADING_INPUTS)
        if state in {
            JobState.RUNNING,
            JobState.CHECKPOINTING,
            JobState.CANCEL_REQUESTED,
        }:
            self.store.update_state(job_id, JobState.RUNNING)
        if state == JobState.CHECKPOINTING:
            self.store.update_state(job_id, JobState.CHECKPOINTING)
        elif state == JobState.CANCEL_REQUESTED:
            self.store.update_state(job_id, JobState.CANCEL_REQUESTED)

    async def _spawn_marked_process(
        self,
        marker: str,
    ) -> tuple[asyncio.subprocess.Process, ProcessIdentity]:
        env = dict(os.environ)
        env["GPUHARBOR_EXECUTION_ID"] = marker
        proc = await asyncio.create_subprocess_exec(
            "sleep",
            "30",
            env=env,
            start_new_session=True,
        )
        identity = self.executor._capture_process_identity(proc.pid, marker)
        self.assertIsNotNone(identity)
        return proc, identity  # type: ignore[return-value]

    async def _kill_process(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            await proc.wait()

    async def test_startup_fails_every_nonterminal_job_without_identity(self) -> None:
        self._create_job("job_created", JobState.CREATED)
        self._create_job("job_uploading", JobState.UPLOADING_INPUTS)
        self._create_job("job_running", JobState.RUNNING)

        tasks = await self.executor.reattach_running_jobs()

        self.assertEqual(tasks, {})
        for job_id in ("job_created", "job_uploading", "job_running"):
            job = self.store.get_job(job_id)
            self.assertEqual(job["state"], JobState.FAILED.value)
            self.assertTrue(job["error_message"])

    async def test_restart_reattaches_only_the_original_process(self) -> None:
        proc, identity = await self._spawn_marked_process("original-marker")
        try:
            self._create_job("job_original", JobState.RUNNING)
            self.store.update_execution_identity(
                "job_original",
                pid=identity.pid,
                start_time=identity.start_time,
                pgid=identity.pgid,
                marker=identity.marker,
            )

            tasks = await self.executor.reattach_running_jobs()
            self.assertEqual(set(tasks), {"job_original"})
            await asyncio.sleep(0)
            self.assertTrue(self.executor.is_tracking("job_original"))

            tasks["job_original"].cancel()
            await asyncio.gather(tasks["job_original"], return_exceptions=True)
            self.assertIsNone(proc.returncode)
            self.assertEqual(
                self.store.get_job("job_original")["state"],
                JobState.RUNNING.value,
            )
        finally:
            await self._kill_process(proc)

    async def test_mismatched_reused_pid_is_failed_without_signal(self) -> None:
        proc, identity = await self._spawn_marked_process("live-marker")
        try:
            self._create_job("job_reused", JobState.RUNNING)
            self.store.update_execution_identity(
                "job_reused",
                pid=identity.pid,
                start_time=str(int(identity.start_time) + 1),
                pgid=identity.pgid,
                marker=identity.marker,
            )

            with mock.patch("gpuharbor.worker.executor.os.killpg") as killpg:
                tasks = await self.executor.reattach_running_jobs()

            self.assertEqual(tasks, {})
            killpg.assert_not_called()
            self.assertIsNone(proc.returncode)
            job = self.store.get_job("job_reused")
            self.assertEqual(job["state"], JobState.FAILED.value)
            self.assertIn("PID was reused", job["error_message"])
        finally:
            await self._kill_process(proc)

    async def test_noisy_job_finishes_without_any_log_follower(self) -> None:
        spec = JobSpec(
            name="noisy",
            command=[
                sys.executable,
                "-u",
                "-c",
                "import time\nfor i in range(20000): print(i)\ntime.sleep(1)",
            ],
        )
        self.store.create_job(
            spec.model_dump_json(),
            spec.name,
            job_id="job_noisy",
        )
        self.executor._validate_resources = lambda _job_id, _spec: None  # type: ignore[method-assign]

        with mock.patch(
            "gpuharbor.worker.gpu.get_gpu_count",
            return_value=1,
        ):
            await asyncio.wait_for(
                self.executor.execute_job("job_noisy", spec),
                timeout=10,
            )

        self.assertNotIn("job_noisy", self.executor._cancel_events)
        self.assertNotIn("job_noisy", self.executor._processes)
        self.assertEqual(
            self.store.get_job("job_noisy")["state"],
            JobState.COMPLETED.value,
        )
        self.assertGreaterEqual(
            len(self.storage.job_log_file("job_noisy").read_text().splitlines()),
            20000,
        )

    async def test_shutdown_detaches_noisy_job_without_waiting_on_log_followers(
        self,
    ) -> None:
        spec = JobSpec(
            name="noisy-shutdown",
            command=[
                sys.executable,
                "-u",
                "-c",
                "import time\nfor i in range(20000): print(i)\ntime.sleep(30)",
            ],
        )
        self.store.create_job(
            spec.model_dump_json(),
            spec.name,
            job_id="job_shutdown",
        )
        self.executor._validate_resources = lambda _job_id, _spec: None  # type: ignore[method-assign]

        proc: asyncio.subprocess.Process | None = None
        try:
            with mock.patch(
                "gpuharbor.worker.gpu.get_gpu_count",
                return_value=1,
            ):
                task = asyncio.create_task(
                    self.executor.execute_job("job_shutdown", spec)
                )
                for _ in range(50):
                    proc = self.executor._processes.get("job_shutdown")
                    log = self.storage.job_log_file("job_shutdown")
                    if log.exists() and log.stat().st_size > 100_000:
                        break
                    await asyncio.sleep(0.1)
                else:
                    self.fail("Noisy job did not write its log")

                self.assertIsNotNone(proc)
                task.cancel()
                await asyncio.wait_for(task, timeout=2)

            self.assertNotIn("job_shutdown", self.executor._cancel_events)
            self.assertNotIn("job_shutdown", self.executor._processes)
            self.assertIsNone(proc.returncode)
            self.assertEqual(
                self.store.get_job("job_shutdown")["state"],
                JobState.RUNNING.value,
            )
        finally:
            if proc is not None:
                await self._kill_process(proc)
