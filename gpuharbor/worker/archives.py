"""Safe project/checkpoint archive extraction for Python 3.10+."""

from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

from gpuharbor.common.storage import validate_relative_path


def extract_archive(archive: Path, destination: Path, max_bytes: int) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive, "r:*") as source:
        members = source.getmembers()
        if len(members) > 100_000 or sum(m.size for m in members) > max_bytes:
            raise ValueError("Archive exceeds available disk or file-count limit")
        seen = set()
        for member in members:
            name = member.name.rstrip("/")
            validate_relative_path(name)
            if name in seen or not (member.isdir() or member.isfile()):
                raise ValueError(
                    "Archive contains duplicate names, links, or special files"
                )
            seen.add(name)
            path = (root / name).resolve()
            path.relative_to(root)
            if path.exists() and not (member.isdir() and path.is_dir()):
                raise ValueError(f"Archive would replace an existing file: {name}")
        for member in members:
            path = root / member.name
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as input_file, path.open("xb") as output:
                shutil.copyfileobj(input_file, output, 1024 * 1024)
            path.chmod(0o755 if member.mode & 0o111 else 0o644)
