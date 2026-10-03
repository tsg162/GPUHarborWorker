"""Training-side checkpoint handoff; no third-party dependencies required."""

from __future__ import annotations

import json
import os
from pathlib import Path

READY_MARKER = ".gpuharbor-complete"


def checkpoint_complete(path: str | Path) -> None:
    """Call after ALL ranks finish writing a checkpoint directory (rank zero only).

    Publish checkpoints under unique names. Never modify a published checkpoint.
    GPUHarbor only backs up/prunes directories with this completion marker.
    """
    path = Path(path)
    if not path.is_dir() or path.is_symlink():
        raise ValueError("Checkpoint must be a real directory")
    files = {}
    for file in sorted(path.rglob("*")):
        if file.is_symlink():
            raise ValueError("Checkpoint cannot contain symbolic links")
        if file.is_file() and file.name not in {READY_MARKER, READY_MARKER + ".tmp"}:
            if file.name.endswith((".tmp", ".part")):
                raise ValueError("Checkpoint still contains temporary files")
            files[file.relative_to(path).as_posix()] = file.stat().st_size
    if not files:
        raise ValueError("Checkpoint is empty")
    temporary = path / (READY_MARKER + ".tmp")
    with temporary.open("w") as output:
        json.dump({"version": 1, "files": files}, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path / READY_MARKER)
