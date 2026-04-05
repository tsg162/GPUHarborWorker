"""GPU and system info via nvidia-smi and /proc."""

from __future__ import annotations

import logging
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class GPUInfo:
    """Information about a single GPU."""

    index: int
    model: str
    memory_gb: float
    utilization_pct: int
    memory_used_gb: float


def get_gpu_info() -> list[GPUInfo]:
    """Query nvidia-smi for GPU details.

    Returns an empty list if nvidia-smi is not available or fails.
    """
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        logger.warning("nvidia-smi not found in PATH")
        return []

    try:
        result = subprocess.run(
            [nvidia_smi, "-q", "-x"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.error("nvidia-smi failed: %s", result.stderr)
            return []
        return _parse_nvidia_smi_xml(result.stdout)
    except subprocess.TimeoutExpired:
        logger.error("nvidia-smi timed out")
        return []
    except Exception:
        logger.exception("Failed to query GPU info")
        return []


def _parse_nvidia_smi_xml(xml_str: str) -> list[GPUInfo]:
    """Parse nvidia-smi XML output into GPUInfo objects."""
    gpus: list[GPUInfo] = []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        logger.error("Failed to parse nvidia-smi XML output")
        return []

    for idx, gpu_elem in enumerate(root.findall("gpu")):
        model = _text(gpu_elem, "product_name", f"GPU {idx}")

        # Memory info
        fb_mem = gpu_elem.find("fb_memory_usage")
        mem_total_mb = _parse_mib(fb_mem, "total") if fb_mem is not None else 0
        mem_used_mb = _parse_mib(fb_mem, "used") if fb_mem is not None else 0

        # Utilization
        util_elem = gpu_elem.find("utilization")
        gpu_util = _parse_pct(util_elem, "gpu_util") if util_elem is not None else 0

        gpus.append(
            GPUInfo(
                index=idx,
                model=model,
                memory_gb=round(mem_total_mb / 1024, 1),
                utilization_pct=gpu_util,
                memory_used_gb=round(mem_used_mb / 1024, 1),
            )
        )

    return gpus


def _text(elem: ET.Element, tag: str, default: str = "") -> str:
    child = elem.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return default


def _parse_mib(parent: ET.Element, tag: str) -> float:
    """Parse a value like '24576 MiB' into a float of MiB."""
    text = _text(parent, tag, "0")
    return float(text.replace("MiB", "").replace("MB", "").strip() or "0")


def _parse_pct(parent: ET.Element, tag: str) -> int:
    """Parse a value like '87 %' into an int."""
    text = _text(parent, tag, "0")
    return int(float(text.replace("%", "").strip() or "0"))


def get_gpu_count() -> int:
    """Return the number of GPUs detected."""
    return len(get_gpu_info())


def get_system_info() -> dict:
    """Gather CPU, RAM, and disk info from the system."""
    info: dict = {
        "cpu_count": _get_cpu_count(),
        "ram_gb": 0,
        "ram_used_gb": 0,
        "disk_free_gb": 0,
    }

    # RAM from /proc/meminfo
    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip().split()[0]  # value in kB
                    meminfo[key] = int(val)
            total_kb = meminfo.get("MemTotal", 0)
            available_kb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
            info["ram_gb"] = round(total_kb / (1024 * 1024), 1)
            info["ram_used_gb"] = round((total_kb - available_kb) / (1024 * 1024), 1)
    except (OSError, ValueError):
        logger.warning("Could not read /proc/meminfo")

    # Disk free from shutil
    try:
        usage = shutil.disk_usage("/")
        info["disk_free_gb"] = round(usage.free / (1024**3), 0)
    except OSError:
        logger.warning("Could not determine disk usage")

    return info


def _get_cpu_count() -> int:
    import os

    return os.cpu_count() or 1


def get_full_status(
    server_name: str,
    running_jobs: int,
    uptime_seconds: int,
    vast_instance_id: str = "",
) -> dict:
    """Build the full /v1/status response payload."""
    gpus = get_gpu_info()
    sys_info = get_system_info()

    status = {
        "hostname": server_name,
        "gpus": [asdict(g) for g in gpus],
        "cpu_count": sys_info["cpu_count"],
        "ram_gb": sys_info["ram_gb"],
        "ram_used_gb": sys_info["ram_used_gb"],
        "disk_free_gb": sys_info["disk_free_gb"],
        "running_jobs": running_jobs,
        "worker_version": "0.1.0",
        "uptime_seconds": uptime_seconds,
    }

    if vast_instance_id:
        status["vast_instance_id"] = vast_instance_id

    return status
