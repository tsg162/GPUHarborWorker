"""Local filesystem storage for job artifacts under /workspace/gpuharbor/."""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# 8 MiB read chunks for hashing large files
_HASH_CHUNK_SIZE = 8 * 1024 * 1024

DEFAULT_WORKSPACE = Path("/workspace/gpuharbor")


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
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "uploads").mkdir(exist_ok=True)

    # ── Job workspace management ────────────────────────────────────────

    def job_dir(self, job_id: str) -> Path:
        return self.root / "jobs" / job_id

    def ensure_job_dirs(self, job_id: str) -> Path:
        """Create the full directory tree for a job. Returns the job root."""
        base = self.job_dir(job_id)
        for sub in ("input", "output", "checkpoints", "logs"):
            (base / sub).mkdir(parents=True, exist_ok=True)
        return base

    def job_input_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "input"

    def job_output_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "output"

    def job_checkpoint_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "checkpoints"

    def job_log_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "logs"

    def job_log_file(self, job_id: str) -> Path:
        return self.job_log_dir(job_id) / "container.log"

    # ── File operations ─────────────────────────────────────────────────

    def store_bytes(self, dest: Path, data: bytes) -> str:
        """Write raw bytes to a path. Returns SHA-256 of the written data."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        sha = compute_sha256(dest)
        logger.info("Stored %d bytes -> %s (sha256:%s)", len(data), dest, sha[:16])
        return sha

    def store_upload(self, filename: str, data: bytes) -> tuple[Path, str]:
        """Store an uploaded file in the uploads/ directory.

        Returns (absolute_path, sha256).
        """
        dest = self.root / "uploads" / filename
        sha = self.store_bytes(dest, data)
        return dest, sha

    def store_job_input(self, job_id: str, filename: str, data: bytes) -> tuple[Path, str]:
        """Store an uploaded input file for a specific job.

        Returns (absolute_path, sha256).
        """
        dest = self.job_input_dir(job_id) / filename
        sha = self.store_bytes(dest, data)
        return dest, sha

    def copy_to_job_input(self, job_id: str, src: Path) -> tuple[Path, str]:
        """Copy an existing file into a job's input directory.

        Returns (dest_path, sha256).
        """
        dest = self.job_input_dir(job_id) / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        sha = compute_sha256(dest)
        return dest, sha

    def get_file(self, relative_path: str) -> Path | None:
        """Resolve a relative path under the storage root.

        Returns the absolute path if it exists, None otherwise.
        Guards against path traversal.
        """
        resolved = (self.root / relative_path).resolve()
        # Prevent path traversal outside storage root
        if not str(resolved).startswith(str(self.root.resolve())):
            logger.warning("Path traversal attempt blocked: %s", relative_path)
            return None
        return resolved if resolved.is_file() else None

    def list_job_files(self, job_id: str, subdir: str = "") -> list[dict]:
        """List files under a job directory (optionally under a subdirectory).

        Returns list of {name, path, size, sha256} dicts.
        """
        base = self.job_dir(job_id)
        if subdir:
            base = base / subdir

        if not base.exists():
            return []

        results = []
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.job_dir(job_id))
            results.append({
                "name": path.name,
                "path": str(rel),
                "size": path.stat().st_size,
            })
        return results

    def list_uploads(self) -> list[dict]:
        """List files in the uploads/ directory."""
        upload_dir = self.root / "uploads"
        if not upload_dir.exists():
            return []
        return [
            {
                "name": p.name,
                "path": f"uploads/{p.name}",
                "size": p.stat().st_size,
            }
            for p in sorted(upload_dir.iterdir())
            if p.is_file()
        ]

    # ── Cleanup ─────────────────────────────────────────────────────────

    def cleanup_job(self, job_id: str) -> None:
        """Remove all files for a job."""
        job_dir = self.job_dir(job_id)
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
            logger.info("Cleaned up job directory: %s", job_dir)

    def prune_checkpoints(self, job_id: str, keep_last_n: int) -> list[Path]:
        """Delete old checkpoints, keeping only the most recent N.

        Returns list of deleted paths.
        """
        ckpt_dir = self.job_checkpoint_dir(job_id)
        if not ckpt_dir.exists():
            return []

        # Get all checkpoint files sorted by mtime (oldest first)
        files = sorted(
            (p for p in ckpt_dir.rglob("*") if p.is_file() and not p.name.startswith(".")),
            key=lambda p: p.stat().st_mtime,
        )

        if len(files) <= keep_last_n:
            return []

        to_delete = files[: len(files) - keep_last_n]
        for path in to_delete:
            path.unlink(missing_ok=True)
            logger.info("Pruned old checkpoint: %s", path.name)

        return to_delete

    def disk_free_gb(self) -> float:
        """Return free disk space at the storage root in GB."""
        try:
            usage = shutil.disk_usage(self.root)
            return round(usage.free / (1024**3), 1)
        except OSError:
            return 0.0
