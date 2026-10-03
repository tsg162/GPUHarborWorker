"""Explicit training interpreters and reusable environments from hashed locks."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from gpuharbor.common.job_spec import TrainingEnvironment


def interpreter(value: str) -> str:
    value = (
        os.environ.get("GPUHARBOR_TRAINING_PYTHON", value)
        if value == "python3"
        else value
    )
    resolved = shutil.which(value)
    if not resolved:
        raise ValueError(f"Training Python not found: {value}")
    # Do not resolve the symlink: its venv location controls site-packages.
    return os.path.abspath(resolved)


def prepare_environment(config: TrainingEnvironment, project: Path, cache: Path) -> str:
    python = interpreter(config.python)
    if not config.requirements_lock:
        return python
    lock = (project / config.requirements_lock).resolve()
    lock.relative_to(project.resolve())
    if not lock.is_file():
        raise ValueError(f"Requirements lock not found: {config.requirements_lock}")
    # pip --require-hashes enforces pinned, hashed dependencies. Reject nested
    # requirements so the cache key describes the complete dependency input.
    lock_text = lock.read_text()
    if any(
        line.strip().startswith(
            ("-r", "--requirement", "-c", "--constraint", "-e", "--editable")
        )
        for line in lock_text.splitlines()
    ):
        raise ValueError("Use one flattened, hashed requirements lock")
    identity = subprocess.check_output(
        [
            python,
            "-c",
            "import sys,json; print(json.dumps([sys.version,sys.prefix,sys.base_prefix]))",
        ],
        text=True,
        timeout=30,
    )
    key = hashlib.sha256(
        (python + identity + str(config.system_site_packages) + lock_text).encode()
    ).hexdigest()
    root = cache / "environments"
    root.mkdir(parents=True, exist_ok=True)
    target = root / key
    with (root / f"{key}.lock").open("a") as mutex:
        fcntl.flock(mutex, fcntl.LOCK_EX)
        if not (target / ".ready").is_file():
            if target.exists():
                shutil.rmtree(target)
            args = [python, "-m", "venv"]
            if config.system_site_packages:
                args.append("--system-site-packages")
            subprocess.run([*args, str(target)], check=True, timeout=120)
            subprocess.run(
                [
                    str(target / "bin/python"),
                    "-m",
                    "pip",
                    "install",
                    "--require-hashes",
                    "-r",
                    str(lock),
                ],
                check=True,
                timeout=3600,
                env={**os.environ, "PIP_CACHE_DIR": str(cache / "pip")},
            )
            (target / ".ready").write_text(key)
    return str(target / "bin/python")


def doctor(python: str, cuda: bool = True) -> dict:
    executable = interpreter(python)
    script = """import json, sys
result = {"python": sys.executable, "version": sys.version.split()[0]}
try:
    import torch
    result.update(torch=torch.__version__, cuda_build=torch.version.cuda, cuda_available=torch.cuda.is_available())
    if CUDA:
        x = torch.ones((32, 32), device="cuda")
        result["cuda_operation"] = float((x @ x).sum().item())
    result["ok"] = True
except Exception as e:
    result.update(ok=False, error=str(e))
print(json.dumps(result))
""".replace("CUDA", repr(cuda))
    result = subprocess.run(
        [executable, "-c", script], capture_output=True, text=True, timeout=90
    )
    if result.returncode:
        return {"ok": False, "python": executable, "error": result.stderr[-2000:]}
    return json.loads(result.stdout.splitlines()[-1])
