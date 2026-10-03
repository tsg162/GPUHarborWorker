"""Durable, bounded, content-addressed upload sessions."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path

from gpuharbor.common.storage import LocalStorage, compute_sha256, validate_filename

CHUNK_SIZE = 8 * 1024 * 1024


class UploadConflict(ValueError):
    pass


class UploadManager:
    def __init__(self, storage: LocalStorage):
        self.storage = storage
        self.root = storage.root / "transfer-sessions"
        self.root.mkdir(exist_ok=True, mode=0o700)

    def _folder(self, upload_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", upload_id):
            raise ValueError("Invalid upload ID")
        return self.root / upload_id

    @contextmanager
    def locked(self, upload_id: str):
        folder = self._folder(upload_id)
        folder.mkdir(exist_ok=True)
        with (folder / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield folder

    def create(self, filename: str, size: int, sha256: str) -> dict:
        validate_filename(filename)
        if size <= 0 or not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise ValueError("A positive size and SHA-256 are required")
        upload_id = hashlib.sha256(f"{filename}:{size}:{sha256}".encode()).hexdigest()
        with self.locked(upload_id) as folder:
            meta = folder / "meta.json"
            if not meta.exists():
                if size > self.storage.disk_free_gb() * 1024**3:
                    raise ValueError("Insufficient free disk for upload")
                # Preserve the extension for source/checkpoint archives. The
                # content hash avoids filename collisions between projects.
                stored_name = f"{sha256}_{filename[-180:]}"
                payload = {
                    "upload_id": upload_id,
                    "filename": stored_name,
                    "size": size,
                    "sha256": sha256,
                }
                meta.write_text(json.dumps(payload))
            return self._status(folder)

    def _status(self, folder: Path) -> dict:
        meta = json.loads((folder / "meta.json").read_text())
        complete = (folder / "complete").exists()
        partial = folder / "data.part"
        return {
            **meta,
            "offset": meta["size"]
            if complete
            else (partial.stat().st_size if partial.exists() else 0),
            "complete": complete,
            "chunk_size": CHUNK_SIZE,
        }

    def status(self, upload_id: str) -> dict:
        with self.locked(upload_id) as folder:
            return self._status(folder)

    def append(self, upload_id: str, offset: int, data: bytes, sha256: str) -> dict:
        if (
            not data
            or len(data) > CHUNK_SIZE
            or hashlib.sha256(data).hexdigest() != sha256
        ):
            raise ValueError("Invalid chunk size or checksum")
        with self.locked(upload_id) as folder:
            status = self._status(folder)
            if status["complete"]:
                return status
            partial = folder / "data.part"
            if offset < status["offset"]:
                with partial.open("rb") as source:
                    source.seek(offset)
                    if offset >= 0 and source.read(len(data)) == data:
                        return status  # retry after an acknowledgement was lost
                raise UploadConflict("Chunk differs from already accepted bytes")
            if offset != status["offset"]:
                raise UploadConflict(f"Expected offset {status['offset']}")
            if offset + len(data) > status["size"]:
                raise ValueError("Chunk exceeds declared upload size")
            with partial.open("ab") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            return self._status(folder)

    def finish(self, upload_id: str) -> dict:
        with self.locked(upload_id) as folder:
            status = self._status(folder)
            if status["complete"]:
                return status
            partial = folder / "data.part"
            dest = self.storage.root / "uploads" / status["filename"]
            # Recover a crash after publication but before writing the receipt.
            candidate = partial if partial.exists() else dest
            if (
                not candidate.is_file()
                or candidate.stat().st_size != status["size"]
                or compute_sha256(candidate) != status["sha256"]
            ):
                raise ValueError("Upload is incomplete or checksum does not match")
            if candidate == partial:
                os.replace(partial, dest)
            with (folder / "complete").open("w") as receipt:
                receipt.write(status["sha256"])
                receipt.flush()
                os.fsync(receipt.fileno())
            return self._status(folder)
