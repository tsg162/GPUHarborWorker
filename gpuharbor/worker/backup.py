"""Verified job snapshots to a mounted backup directory or an rclone remote."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from gpuharbor.common.storage import LocalStorage, compute_sha256
from gpuharbor.common.states import JobState, is_terminal
from gpuharbor.worker.checkpoints import completed_checkpoints
from gpuharbor.worker.state import JobStore


class BackupManager:
    def __init__(
        self, storage: LocalStorage, store: JobStore, destination: str | None = None
    ):
        self.storage, self.store = storage, store
        self.destination = (
            destination
            if destination is not None
            else os.environ.get("GPUHARBOR_BACKUP_DEST", "")
        )
        self._lock = threading.RLock()

    def snapshot(self, job_id: str) -> dict:
        """Publish a new immutable snapshot; record success only after verification."""
        with self._lock:
            if not self.destination:
                raise ValueError(
                    "Set GPUHARBOR_BACKUP_DEST to a mounted path or rclone remote:prefix"
                )
            job = self.store.get_job(job_id)
            if not job:
                raise KeyError(job_id)
            previous = job.get("backup") or {}
            previous_files = (
                previous.get("files", {}) if previous.get("verified") else {}
            )
            terminal = is_terminal(JobState(job["state"]))
            base = self.storage.job_dir(job_id)
            if not base.is_dir():
                raise ValueError("Job workspace is unavailable")
            if terminal:
                roots = [base]
            else:
                roots = completed_checkpoints(self.storage, job_id)
                if not roots:
                    raise ValueError("No completed checkpoints are available yet")
                if (base / "project").exists():
                    roots.append(base / "project")
            files = {}
            for root in roots:
                for file in root.rglob("*"):
                    if file.is_symlink():
                        raise ValueError(f"Refusing to back up symbolic link: {file}")
                    if not file.is_file() or file.name in {
                        ".exitcode",
                        ".exitcode.tmp",
                        ".execution-result.json",
                    }:
                        continue
                    if not self.storage.is_confined(file, within=base):
                        raise ValueError("Backup path escapes the job")
                    name = file.relative_to(base).as_posix()
                    before = file.stat()
                    signature = (
                        f"{before.st_size}:{before.st_mtime_ns}:{before.st_ctime_ns}"
                    )
                    cached = previous_files.get(name, {})
                    # A stat signature detects overwrites without rereading unchanged
                    # multi-gigabyte model files on every monitor tick.
                    checksum = (
                        cached["sha256"]
                        if cached.get("signature") == signature
                        else compute_sha256(file)
                    )
                    after = file.stat()
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ):
                        raise ValueError(f"File changed during backup: {name}")
                    files[name] = {
                        "size": after.st_size,
                        "sha256": checksum,
                        "signature": f"{after.st_size}:{after.st_mtime_ns}:{after.st_ctime_ns}",
                    }
            matches = (
                previous_files == files
                if terminal
                else all(
                    previous_files.get(name) == metadata
                    for name, metadata in files.items()
                )
            )
            if (
                previous.get("verified")
                and previous.get("destination_root") == self.destination
                and matches
                and previous.get("terminal") == terminal
            ):
                # Retention may have removed older sets since this active snapshot.
                # Its verified superset still covers every retained checkpoint.
                if "last_error" in previous:
                    previous.pop("last_error")
                    self.store.set_backup(job_id, previous)
                return previous
            snapshot_id = uuid.uuid4().hex
            relative = f"{job_id}/{snapshot_id}"
            spec = dict(job["spec"])
            spec["env"] = {}  # do not replicate WANDB/HF credentials into backups
            receipt = {
                "version": 1,
                "job_id": job_id,
                "snapshot_id": snapshot_id,
                "terminal": terminal,
                "destination_root": self.destination,
                "files": files,
                "spec": spec,
            }
            staging_root = self.storage.root / "backup-staging"
            staging_root.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(dir=staging_root) as temporary:
                staging = Path(temporary)
                for name, metadata in files.items():
                    target = staging / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(base / name, target)
                    if compute_sha256(target) != metadata["sha256"]:
                        raise ValueError(f"File changed during backup: {name}")
                (staging / "gpuharbor-backup.json").write_text(
                    json.dumps(receipt, indent=2)
                )
                if ":" in self.destination and not self.destination.startswith("/"):
                    target = self.destination.rstrip("/") + "/" + relative
                    subprocess.run(
                        ["rclone", "copy", str(staging), target],
                        check=True,
                        timeout=86400,
                    )
                    subprocess.run(
                        ["rclone", "check", "--download", str(staging), target],
                        check=True,
                        timeout=86400,
                    )
                else:
                    backup_root = Path(self.destination).expanduser().resolve()
                    if backup_root.is_relative_to(self.storage.root):
                        raise ValueError(
                            "Backup destination must be outside worker storage"
                        )
                    target_path = backup_root / relative
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    partial = target_path.with_name(snapshot_id + ".partial")
                    shutil.copytree(staging, partial)
                    for name, metadata in files.items():
                        if compute_sha256(partial / name) != metadata["sha256"]:
                            raise ValueError(f"Backup verification failed: {name}")
                    os.replace(partial, target_path)
                    target = str(target_path)
            receipt.update({"verified": True, "destination": target})
            self.store.set_backup(job_id, receipt)
            return receipt

    def covers_workspace(self, job_id: str) -> bool:
        """Require a verified terminal snapshot matching every current file."""
        job = self.store.get_job(job_id)
        receipt = (job or {}).get("backup") or {}
        if not receipt.get("verified") or not receipt.get("terminal"):
            return False
        base = self.storage.job_dir(job_id)
        current = {}
        for file in base.rglob("*"):
            if file.is_symlink():
                return False
            if file.is_file() and file.name not in {
                ".exitcode",
                ".exitcode.tmp",
                ".execution-result.json",
            }:
                name = file.relative_to(base).as_posix()
                original = receipt.get("files", {}).get(name)
                stat = file.stat()
                signature = f"{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"
                if not original:
                    return False
                if original.get("signature") == signature:
                    current[name] = original
                elif original.get("sha256") == compute_sha256(file):
                    current[name] = original
                else:
                    return False
        return current == receipt.get("files")
