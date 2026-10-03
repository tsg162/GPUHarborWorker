from __future__ import annotations
import asyncio
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gpuharbor.common.job_spec import JobSpec
from gpuharbor.common.states import JobState
from gpuharbor.common.storage import LocalStorage
from gpuharbor.common.training import checkpoint_complete
from gpuharbor.worker.archives import extract_archive
from gpuharbor.worker.backup import BackupManager
from gpuharbor.worker.checkpoint import CheckpointManager
from gpuharbor.worker.executor import JobExecutor
from gpuharbor.worker.state import JobStore
from gpuharbor.worker.transfers import UploadManager, UploadConflict


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.storage = LocalStorage(self.root / "storage")
        self.store = JobStore(self.root / "state.db")
        self.backup = BackupManager(
            self.storage, self.store, str(self.root / "backups")
        )
        self.executor = JobExecutor(self.storage, self.store, backup=self.backup)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, jid="job_test", state=JobState.RUNNING, spec=None):
        spec = spec or JobSpec(name=jid, command=["true"])
        self.store.create_job(spec.model_dump_json(), spec.name, job_id=jid)
        self.storage.ensure_job_dirs(jid)
        if state != JobState.CREATED:
            self.store.update_state(jid, JobState.UPLOADING_INPUTS)
            self.store.update_state(jid, JobState.RUNNING)
        if state == JobState.COMPLETED:
            self.store.update_state(jid, state)
        return spec

    def checkpoint(self, jid, step):
        folder = self.storage.job_checkpoint_dir(jid) / f"checkpoint-{step}"
        folder.mkdir()
        for name in ["model.bin", "optimizer.bin", "scheduler.json", "state.json"]:
            (folder / name).write_text(str(step))
        checkpoint_complete(folder)
        os.utime(folder / ".gpuharbor-complete", (step, step))
        return folder

    async def test_low_disk_never_removes_previous_output(self):
        self.create("job_old", JobState.COMPLETED)
        path = self.storage.job_output_dir("job_old") / "model.pt"
        path.write_text("valuable")
        with (
            patch("gpuharbor.worker.gpu.get_gpu_info", return_value=[object()]),
            patch.object(self.storage, "disk_free_gb", return_value=1),
            patch.dict(os.environ, {"GPUHARBOR_CLEANUP_ON_LOW_DISK": "1"}),
        ):
            with self.assertRaises(ValueError):
                self.executor._validate_resources(
                    "job_new",
                    JobSpec(
                        name="new", command=["true"], resources={"disk_gb_min": 50}
                    ),
                )
        self.assertEqual(path.read_text(), "valuable")

    async def test_retention_preserves_sets_and_skips_incomplete(self):
        self.create()
        first = self.checkpoint("job_test", 1)
        second = self.checkpoint("job_test", 2)
        incomplete = self.storage.job_checkpoint_dir("job_test") / "incomplete"
        incomplete.mkdir()
        (incomplete / "model.tmp").write_text("partial")
        manager = CheckpointManager(self.storage, self.store, self.backup)
        manager.scan("job_test", 1)
        self.assertFalse(first.exists())
        self.assertEqual(len(list(second.iterdir())), 5)
        self.assertTrue(incomplete.exists())
        artifacts = self.store.get_artifacts("job_test")
        self.assertEqual(len(artifacts), 5)
        self.assertTrue(all(self.storage.get_file(a["uri"]) for a in artifacts))
        manager.scan("job_test", 1)
        self.assertEqual(len(self.store.get_artifacts("job_test")), 5)
        self.assertTrue(self.store.get_job("job_test")["backup"]["verified"])

    async def test_failed_backup_prevents_checkpoint_pruning(self):
        self.create()
        first = self.checkpoint("job_test", 1)
        self.checkpoint("job_test", 2)
        manager = CheckpointManager(self.storage, self.store, self.backup)
        with patch.object(self.backup, "snapshot", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                manager.scan("job_test", 1)
        self.assertTrue(first.exists())

    async def test_backup_reuses_unchanged_snapshot_after_retention_and_clears_error(
        self,
    ):
        self.create()
        self.checkpoint("job_test", 1)
        self.checkpoint("job_test", 2)
        manager = CheckpointManager(self.storage, self.store, self.backup)
        manager.scan("job_test", 1)
        receipt = self.store.get_job("job_test")["backup"]
        self.store.set_backup("job_test", {**receipt, "last_error": "offline"})
        with patch(
            "gpuharbor.worker.backup.compute_sha256",
            side_effect=AssertionError("Unchanged file was rehashed"),
        ):
            manager.scan("job_test", 1)
        retried = self.store.get_job("job_test")["backup"]
        self.assertEqual(retried["snapshot_id"], receipt["snapshot_id"])
        self.assertNotIn("last_error", retried)

    async def test_backup_destination_change_publishes_new_snapshot(self):
        self.create()
        self.checkpoint("job_test", 1)
        receipt = self.backup.snapshot("job_test")
        self.backup.destination = str(self.root / "replacement-backups")
        replacement = self.backup.snapshot("job_test")
        self.assertNotEqual(receipt["snapshot_id"], replacement["snapshot_id"])
        self.assertTrue(
            Path(replacement["destination"]).is_relative_to(self.backup.destination)
        )
        self.assertTrue(Path(replacement["destination"]).is_dir())

    async def test_terminal_snapshot_tracks_removed_files(self):
        self.create(state=JobState.COMPLETED)
        model = self.storage.job_output_dir("job_test") / "model.pt"
        model.write_text("model")
        receipt = self.backup.snapshot("job_test")
        model.unlink()
        replacement = self.backup.snapshot("job_test")
        self.assertNotEqual(receipt["snapshot_id"], replacement["snapshot_id"])
        self.assertTrue(self.backup.covers_workspace("job_test"))

    async def test_cleanup_requires_current_backup_and_honors_pin_and_dry_run(self):
        self.create(state=JobState.COMPLETED)
        model = self.storage.job_output_dir("job_test") / "model.pt"
        model.write_text("v1")
        self.assertTrue(
            self.executor.cleanup_terminal_job_dirs(dry_run=False)["skipped"]
        )
        receipt = self.backup.snapshot("job_test")
        self.assertTrue(Path(receipt["destination"]).is_dir())
        self.assertEqual(self.executor.cleanup_terminal_job_dirs()["cleaned_count"], 1)
        self.assertTrue(model.exists())
        model.write_text("v2")
        self.assertTrue(
            self.executor.cleanup_terminal_job_dirs(dry_run=False)["skipped"]
        )
        self.backup.snapshot("job_test")
        self.store.set_pinned("job_test", True)
        self.assertTrue(
            self.executor.cleanup_terminal_job_dirs(dry_run=False, force=True)[
                "skipped"
            ]
        )
        self.store.set_pinned("job_test", False)
        self.assertEqual(
            self.executor.cleanup_terminal_job_dirs(dry_run=False)["cleaned_count"], 1
        )
        self.assertFalse(model.exists())

    async def test_gpu_leases_are_exclusive_and_reusable(self):
        with patch("gpuharbor.worker.gpu.get_gpu_count", return_value=2):
            ids = await asyncio.gather(
                self.executor.reserve_gpus("job_a", 1),
                self.executor.reserve_gpus("job_b", 1),
            )
            self.assertEqual(sorted(ids), [[0], [1]])
            with self.assertRaisesRegex(ValueError, "GPU busy"):
                await self.executor.reserve_gpus("job_c", 1)
            self.executor.release_gpus("job_a")
            self.assertEqual(await self.executor.reserve_gpus("job_c", 1), [0])

    async def test_completion_during_downtime_is_recovered(self):
        self.create()
        self.store.update_execution_identity(
            "job_test", pid=99999999, start_time="1", pgid=99999999, marker="expected"
        )
        (self.storage.job_dir("job_test") / ".execution-result.json").write_text(
            json.dumps({"marker": "expected", "exit_code": 0})
        )
        (self.storage.job_output_dir("job_test") / "model.pt").write_text("done")
        await self.executor.reattach_running_jobs()
        self.assertEqual(self.store.get_job("job_test")["state"], "completed")
        self.assertTrue(self.store.get_artifacts("job_test"))

    async def test_mismatched_completion_marker_does_not_recover(self):
        self.create()
        self.store.update_execution_identity(
            "job_test", pid=99999999, start_time="1", pgid=99999999, marker="expected"
        )
        (self.storage.job_dir("job_test") / ".execution-result.json").write_text(
            json.dumps({"marker": "wrong", "exit_code": 0})
        )
        await self.executor.reattach_running_jobs()
        self.assertEqual(self.store.get_job("job_test")["state"], "failed")

    async def test_followers_get_identical_logs_and_independent_resume_offsets(self):
        self.create(state=JobState.COMPLETED)
        self.storage.job_log_file("job_test").write_text("first\nsecond\nthird\n")

        async def collect(**kwargs):
            return [
                line async for line in self.executor.stream_logs("job_test", **kwargs)
            ]

        a, b = await asyncio.gather(collect(), collect())
        self.assertEqual(a, b)
        self.assertEqual(a, ["first", "second", "third"])
        self.assertEqual(await collect(offset=6), ["second", "third"])
        self.assertEqual(await collect(tail=1), ["third"])
        self.assertEqual(
            await collect(with_offsets=True),
            [("first", 6), ("second", 13), ("third", 19)],
        )

    async def test_cancellation_finishes_without_racing_process_exit(self):
        spec = JobSpec(
            name="cancel",
            command=[sys.executable, "-c", "import time; time.sleep(30)"],
            backup=False,
        )
        self.create(state=JobState.CREATED, spec=spec)
        with (
            patch("gpuharbor.worker.gpu.get_gpu_info", return_value=[object()]),
            patch("gpuharbor.worker.gpu.get_gpu_count", return_value=1),
        ):
            task = asyncio.create_task(self.executor.execute_job("job_test", spec))
            for _ in range(100):
                if "job_test" in self.executor._processes:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(await self.executor.cancel_job("job_test"))
            await asyncio.wait_for(task, timeout=5)
        self.assertEqual(self.store.get_job("job_test")["state"], "canceled")
        self.assertNotIn("job_test", self.executor._gpu_allocations)

    async def test_uploaded_project_executes_and_exposes_dataset_and_gpu(self):
        archive = self.storage.root / "uploads" / "source.tar"
        script = b'import os,pathlib; pathlib.Path(os.environ["GPUHARBOR_OUTPUT_DIR"],"result.txt").write_text(os.environ["CUDA_VISIBLE_DEVICES"]+":"+os.environ["GPUHARBOR_DATASET_DIR"])'
        with tarfile.open(archive, "w") as tar:
            info = tarfile.TarInfo("train.py")
            info.size = len(script)
            tar.addfile(info, io.BytesIO(script))
        dataset = self.root / "dataset"
        dataset.mkdir()
        spec = JobSpec(
            name="project",
            command=["python3", "train.py"],
            source_archive="source.tar",
            environment={"python": sys.executable},
            artifacts={"dataset": str(dataset)},
            backup=False,
        )
        self.create(state=JobState.CREATED, spec=spec)
        with (
            patch("gpuharbor.worker.gpu.get_gpu_info", return_value=[object()]),
            patch("gpuharbor.worker.gpu.get_gpu_count", return_value=1),
        ):
            await self.executor.execute_job("job_test", spec)
        self.assertEqual(
            self.store.get_job("job_test")["state"],
            "completed",
            self.store.get_job("job_test")["error_message"],
        )
        self.assertEqual(
            (self.storage.job_output_dir("job_test") / "result.txt").read_text(),
            "0:" + str(dataset),
        )


class TransferTests(unittest.TestCase):
    def test_upload_resumes_after_restart_and_checks_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = LocalStorage(Path(tmp))
            manager = UploadManager(storage)
            data = b"abcdefghij"
            sha = hashlib.sha256(data).hexdigest()
            state = manager.create("weights.bin", len(data), sha)
            uid = state["upload_id"]
            chunk = data[:4]
            chunk_sha = hashlib.sha256(chunk).hexdigest()
            manager.append(uid, 0, chunk, chunk_sha)
            manager = UploadManager(storage)
            self.assertEqual(manager.create("weights.bin", len(data), sha)["offset"], 4)
            self.assertEqual(manager.append(uid, 0, chunk, chunk_sha)["offset"], 4)
            with self.assertRaises(UploadConflict):
                manager.append(uid, 0, b"xxxx", hashlib.sha256(b"xxxx").hexdigest())
            with self.assertRaises(ValueError):
                manager.finish(uid)
            manager.append(uid, 4, data[4:], hashlib.sha256(data[4:]).hexdigest())
            result = manager.finish(uid)
            self.assertEqual(
                (storage.root / "uploads" / result["filename"]).read_bytes(), data
            )
            self.assertTrue(manager.finish(uid)["complete"])

    def test_archives_reject_traversal_and_links(self):
        for name, symlink in [
            ("../escape", False),
            ("/absolute", False),
            ("link", True),
        ]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = root / "bad.tar"
                with tarfile.open(archive, "w") as tar:
                    info = tarfile.TarInfo(name)
                    if symlink:
                        info.type = tarfile.SYMTYPE
                        info.linkname = "/tmp"
                    tar.addfile(info)
                with self.assertRaises(ValueError):
                    extract_archive(archive, root / "output", 1024)
