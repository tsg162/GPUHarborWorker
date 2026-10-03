"""Whole-directory checkpoint discovery, integrity, and retention."""

from __future__ import annotations

import json
from pathlib import Path

from gpuharbor.common.storage import (
    LocalStorage,
    compute_sha256,
    validate_relative_path,
)
from gpuharbor.common.training import READY_MARKER
from gpuharbor.worker.state import JobStore


def completed_checkpoints(storage: LocalStorage, job_id: str) -> list[Path]:
    root = storage.job_checkpoint_dir(job_id)
    complete = []
    if not root.exists():
        return complete
    for folder in root.iterdir():
        if (
            folder.is_symlink()
            or not folder.is_dir()
            or not storage.is_confined(folder, within=root)
        ):
            continue
        marker = folder / READY_MARKER
        if not marker.is_file() or marker.is_symlink():
            continue
        try:
            manifest = json.loads(marker.read_text())
            files = manifest["files"]
            if not files:
                continue
            actual = {}
            for file in folder.rglob("*"):
                if file.is_symlink():
                    raise ValueError("Symlink in checkpoint")
                if file.is_file() and file.name != READY_MARKER:
                    actual[file.relative_to(folder).as_posix()] = file.stat().st_size
            for name in files:
                validate_relative_path(name)
            if actual == files:
                complete.append(folder)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(
        complete, key=lambda p: (p.joinpath(READY_MARKER).stat().st_mtime_ns, p.name)
    )


def record_file(
    storage: LocalStorage, store: JobStore, job_id: str, path: Path, kind: str
) -> dict:
    if (
        not storage.is_confined(path, within=storage.job_dir(job_id))
        or path.is_symlink()
    ):
        raise ValueError("Unsafe artifact path")
    uri = path.relative_to(storage.root).as_posix()
    stat = path.stat()
    signature = f"{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"
    existing = next((a for a in store.get_artifacts(job_id) if a["uri"] == uri), None)
    if existing and existing.get("signature") == signature and existing.get("sha256"):
        return existing
    sha = compute_sha256(path)
    after = path.stat()
    if (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError(f"Artifact changed while hashing: {uri}")
    return store.add_artifact(job_id, kind, uri, sha, stat.st_size, signature)


def reconcile_artifacts(storage: LocalStorage, store: JobStore, job_id: str) -> None:
    missing = [
        a["uri"]
        for a in store.get_artifacts(job_id)
        if storage.get_file(a["uri"]) is None
    ]
    store.remove_artifacts(job_id, missing)
