from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gpuharbor.common.storage import LocalStorage, StoragePathError


class StorageConfinementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.root = self.base / "gpuharbor"
        self.storage = LocalStorage(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_upload_names_reject_absolute_and_traversal_paths(self) -> None:
        for filename in (
            "/tmp/escape",
            "../escape",
            "nested/escape",
            ".",
            "..",
        ):
            with self.subTest(filename=filename), self.assertRaises(StoragePathError):
                self.storage.store_upload(filename, b"blocked")

    def test_job_ids_reject_absolute_and_traversal_paths(self) -> None:
        for job_id in (
            "/tmp/job",
            "../job",
            "nested/job",
            ".",
            "..",
        ):
            with self.subTest(job_id=job_id), self.assertRaises(StoragePathError):
                self.storage.ensure_job_dirs(job_id)

    def test_component_containment_blocks_sibling_prefix(self) -> None:
        sibling = self.base / "gpuharbor-secret"
        sibling.mkdir()
        secret = sibling / "secret.txt"
        secret.write_text("secret")

        self.assertIsNone(
            self.storage.get_file("../gpuharbor-secret/secret.txt")
        )
        with self.assertRaises(StoragePathError):
            self.storage._require_confined(secret)

    def test_reads_and_writes_block_symlink_escape(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("secret")

        link = self.root / "uploads" / "linked.txt"
        link.symlink_to(secret)
        self.assertIsNone(self.storage.get_file("uploads/linked.txt"))
        with self.assertRaises(StoragePathError):
            self.storage.store_bytes(link, b"overwrite")
        self.assertEqual(secret.read_text(), "secret")

    def test_cleanup_refuses_symlinked_job_directory(self) -> None:
        outside = self.base / "outside-job"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("keep")
        jobs_dir = self.root / "jobs"
        jobs_dir.mkdir()
        (jobs_dir / "job_escape").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(StoragePathError):
            self.storage.cleanup_job("job_escape")
        self.assertEqual(marker.read_text(), "keep")
