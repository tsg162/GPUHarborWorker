"""Local filesystem storage for job artifacts under /workspace/gpuharbor/."""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# 8 MiB read chunks for hashing large files
_HASH_CHUNK_SIZE = 8 * 1024 * 1024

DEFAULT_WORKSPACE = Path("/workspace/gpuharbor")

_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class StoragePathError(ValueError):
    """Raised when an input path cannot be confined to managed storage."""


def validate_filename(filename: str) -> str:
    """Validate a single, portable upload filename."""
    if (
        not isinstance(filename, str)
        or filename in {".", ".."}
        or not _SAFE_FILENAME.fullmatch(filename)
    ):
        raise StoragePathError(
            "Filename must be a single 1-255 character component containing "
            "only letters, numbers, '.', '_', or '-'"
        )
    return filename


def validate_job_id(job_id: str) -> str:
    """Validate a job identifier before it is used in SQL or filesystem paths."""
    if not isinstance(job_id, str) or not _SAFE_JOB_ID.fullmatch(job_id):
        raise StoragePathError(
            "Job ID must be 1-128 characters, start with a letter or number, "
            "and contain only letters, numbers, '_' or '-'"
        )
    return job_id


def validate_relative_path(relative_path: str) -> str:
    """Reject absolute, empty, and traversal-bearing POSIX paths."""
    if not isinstance(relative_path, str) or not relative_path:
        raise StoragePathError("Path must be a non-empty relative path")
    if "\x00" in relative_path or "\\" in relative_path:
        raise StoragePathError("Path contains an invalid character")
    path = Path(relative_path)
    if path.is_absolute():
        raise StoragePathError("Absolute paths are not allowed")
    if any(part in {"", ".", ".."} for part in relative_path.split("/")):
        raise StoragePathError("Path traversal components are not allowed")
    return relative_path


def compute_sha256(file_path: str | Path) -> str:
    """Compute the SHA-256 hex digest of a local file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class LocalStorage:
    """Manages artifact storage on the local filesystem.

    Directory layout:
        {root}/
            jobs/{job_id}/
                input/          # uploaded checkpoints, datasets
                output/         # final model, logs produced by training
                checkpoints/    # periodic checkpoints
                logs/           # container stdout/stderr
            uploads/            # ad-hoc uploads (checkpoints uploaded outside a job)
    """

    def __init__(self, root: Path = DEFAULT_WORKSPACE):
        requested_root = Path(root).resolve(strict=False)
        requested_root.mkdir(parents=True, exist_ok=True)
        self.root = requested_root.resolve(strict=True)
        uploads = self.root / "uploads"
        uploads.mkdir(exist_ok=True)
        self._require_confined(uploads, within=self.root)

    def _require_confined(
        self,
        path: str | Path,
        *,
        within: str | Path | None = None,
    ) -> Path:
        """Resolve a path and require component-aware containment.

        ``Path.relative_to`` prevents sibling-prefix bypasses and resolving the
        candidate before the check prevents symlink escapes.
        """
        base = Path(within) if within is not None else self.root
        if not base.is_absolute():
            base = self.root / base
        resolved_base = base.resolve(strict=False)
        try:
            resolved_base.relative_to(self.root)
        except ValueError as exc:
            raise StoragePathError(
                f"Storage boundary escapes the configured root: {within}"
            ) from exc

        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = resolved_base / candidate
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(resolved_base)
        except ValueError as exc:
            raise StoragePathError(
                f"Path escapes managed storage: {path}"
            ) from exc
        return resolved

    def is_confined(
        self,
        path: str | Path,
        *,
        within: str | Path | None = None,
    ) -> bool:
        """Return whether a path resolves beneath the requested storage area."""
        try:
            self._require_confined(path, within=within)
            return True
        except (OSError, StoragePathError):
            return False

    # ── Job workspace management ────────────────────────────────────────

    def job_dir(self, job_id: str) -> Path:
        validate_job_id(job_id)
        expected = self.root / "jobs" / job_id
        resolved = self._require_confined(expected)
        # A job directory may never alias another job through a symlink.
        if resolved != expected:
            raise StoragePathError(f"Job directory contains a symlink: {job_id}")
        return resolved

    def ensure_job_dirs(self, job_id: str) -> Path:
        """Create the full directory tree for a job. Returns the job root."""
        base = self.job_dir(job_id)
        for sub in ("input", "output", "checkpoints", "logs"):
            dest = self._require_confined(base / sub, within=base)
            dest.mkdir(parents=True, exist_ok=True)
            self._require_confined(dest, within=base)
        return base

    def job_input_dir(self, job_id: str) -> Path:
        base = self.job_dir(job_id)
        return self._require_confined(base / "input", within=base)

    def job_output_dir(self, job_id: str) -> Path:
        base = self.job_dir(job_id)
        return self._require_confined(base / "output", within=base)

    def job_checkpoint_dir(self, job_id: str) -> Path:
        base = self.job_dir(job_id)
        return self._require_confined(base / "checkpoints", within=base)

    def job_log_dir(self, job_id: str) -> Path:
        base = self.job_dir(job_id)
        return self._require_confined(base / "logs", within=base)

    def job_log_file(self, job_id: str) -> Path:
        log_dir = self.job_log_dir(job_id)
        return self._require_confined(log_dir / "container.log", within=log_dir)

    # ── File operations ─────────────────────────────────────────────────

    def store_bytes(self, dest: Path, data: bytes) -> str:
        """Write raw bytes to a path. Returns SHA-256 of the written data."""
        dest = self._require_confined(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Re-check after directory creation in case an existing component was
        # a symlink. The resolved path is the one that is opened.
        dest = self._require_confined(dest)
        dest.write_bytes(data)
        sha = compute_sha256(dest)
        logger.info("Stored %d bytes -> %s (sha256:%s)", len(data), dest, sha[:16])
        return sha

    def store_upload(self, filename: str, data: bytes) -> tuple[Path, str]:
        """Store an uploaded file in the uploads/ directory.

        Returns (absolute_path, sha256).
        """
        validate_filename(filename)
        upload_dir = self._require_confined(self.root / "uploads")
        dest = self._require_confined(upload_dir / filename, within=upload_dir)
        sha = self.store_bytes(dest, data)
        return dest, sha

    def store_job_input(self, job_id: str, filename: str, data: bytes) -> tuple[Path, str]:
        """Store an uploaded input file for a specific job.

        Returns (absolute_path, sha256).
        """
        validate_filename(filename)
        input_dir = self.job_input_dir(job_id)
        dest = self._require_confined(input_dir / filename, within=input_dir)
        sha = self.store_bytes(dest, data)
        return dest, sha

    def copy_to_job_input(self, job_id: str, src: Path) -> tuple[Path, str]:
        """Copy an existing file into a job's input directory.

        Returns (dest_path, sha256).
        """
        src = self._require_confined(src)
        if not src.is_file():
            raise FileNotFoundError(src)
        validate_filename(src.name)
        input_dir = self.job_input_dir(job_id)
        dest = self._require_confined(input_dir / src.name, within=input_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        dest = self._require_confined(dest, within=input_dir)
        sha = compute_sha256(dest)
        return dest, sha

    def get_file(self, relative_path: str) -> Path | None:
        """Resolve a relative path under the storage root.

        Returns the absolute path if it exists, None otherwise.
        Guards against path traversal.
        """
        try:
            validate_relative_path(relative_path)
            resolved = self._require_confined(self.root / relative_path)
        except (OSError, StoragePathError):
            logger.warning("Unsafe storage path blocked: %s", relative_path)
            return None
        return resolved if resolved.is_file() else None

    def list_job_files(self, job_id: str, subdir: str = "") -> list[dict]:
        """List files under a job directory (optionally under a subdirectory).

        Returns list of {name, path, size, sha256} dicts.
        """
        job_base = self.job_dir(job_id)
        base = job_base
        if subdir:
            validate_relative_path(subdir)
            base = self._require_confined(base / subdir, within=job_base)

        if not base.exists():
            return []

        results = []
        for path in sorted(base.rglob("*")):
            try:
                safe_path = self._require_confined(path, within=job_base)
            except (OSError, StoragePathError):
                logger.warning("Symlink escape skipped while listing: %s", path)
                continue
            if not safe_path.is_file():
                continue
            rel = path.relative_to(job_base)
            results.append({
                "name": path.name,
                "path": str(rel),
                "size": safe_path.stat().st_size,
            })
        return results

    def list_uploads(self) -> list[dict]:
        """List files in the uploads/ directory."""
        upload_dir = self.root / "uploads"
        if not upload_dir.exists():
            return []
        results = []
        for path in sorted(upload_dir.iterdir()):
            try:
                safe_path = self._require_confined(path, within=upload_dir)
            except (OSError, StoragePathError):
                logger.warning("Symlink escape skipped while listing: %s", path)
                continue
            if not safe_path.is_file():
                continue
            results.append({
                "name": path.name,
                "path": f"uploads/{path.name}",
                "size": safe_path.stat().st_size,
            })
        return results

    # ── Cleanup ─────────────────────────────────────────────────────────

    def path_size_bytes(self, path: Path) -> int:
        """Return the total size of a file or directory tree."""
        path = self._require_confined(path)
        if not path.exists():
            return 0
        if path.is_file():
            return path.stat().st_size
        total = 0
        for child in path.rglob("*"):
            try:
                safe_child = self._require_confined(child, within=path)
                if safe_child.is_file():
                    total += safe_child.stat().st_size
            except (OSError, StoragePathError):
                continue
        return total

    def cleanup_job(self, job_id: str) -> int:
        """Remove all files for a job and return approximate bytes freed."""
        job_dir = self.job_dir(job_id)
        if job_dir.exists():
            bytes_freed = self.path_size_bytes(job_dir)
            # Resolve once more immediately before recursive deletion.
            safe_job_dir = self._require_confined(job_dir)
            if safe_job_dir != job_dir:
                raise StoragePathError(
                    f"Refusing to remove symlinked job directory: {job_id}"
                )
            shutil.rmtree(safe_job_dir)
            logger.info(
                "Cleaned up job directory: %s (freed %.2f GB)",
                safe_job_dir,
                bytes_freed / (1024**3),
            )
            return bytes_freed
        return 0

    def prune_checkpoints(self, job_id: str, keep_last_n: int) -> list[Path]:
        """Delete old checkpoints, keeping only the most recent N.

        Returns list of deleted paths.
        """
        ckpt_dir = self.job_checkpoint_dir(job_id)
        if not ckpt_dir.exists():
            return []

        # Only complete checkpoint directories are eligible. Unmarked work in
        # progress and legacy loose files are never pruned automatically.
        from gpuharbor.worker.checkpoints import completed_checkpoints
        units = completed_checkpoints(self, job_id)
        to_delete = units[:-keep_last_n] if keep_last_n > 0 else []
        for folder in to_delete:
            safe = self._require_confined(folder, within=ckpt_dir)
            shutil.rmtree(safe)
        return to_delete

    def disk_free_gb(self) -> float:
        """Return free disk space at the storage root in GB."""
        try:
            usage = shutil.disk_usage(self.root)
            return round(usage.free / (1024**3), 1)
        except OSError:
            return 0.0
