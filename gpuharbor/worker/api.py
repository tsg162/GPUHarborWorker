"""FastAPI worker agent exposing the GPUHarbor API.

All artifacts are stored on local disk under /workspace/gpuharbor/.
Files are transferred between CLI and worker via HTTP multipart upload/download.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import time
import uuid
import json
import hashlib
import tempfile
import tarfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sse_starlette.sse import EventSourceResponse

from gpuharbor.common.auth import extract_bearer_token, validate_token
from gpuharbor.common.job_spec import JobSpec
from gpuharbor.common.states import JobState, is_terminal
from gpuharbor.common.storage import (
    LocalStorage,
    StoragePathError,
    validate_filename,
    validate_job_id,
    validate_relative_path,
)
from gpuharbor.worker.checkpoint import CheckpointManager
from gpuharbor.worker.executor import JobExecutor
from gpuharbor.worker.gpu import get_full_status
from gpuharbor.worker.heartbeat import HeartbeatMonitor
from gpuharbor.worker.state import JobStore
from gpuharbor import __version__
from gpuharbor.worker.transfers import UploadManager, UploadConflict, CHUNK_SIZE
from gpuharbor.worker.checkpoints import completed_checkpoints
from gpuharbor.worker.environment import doctor
from gpuharbor.common.storage import compute_sha256

logger = logging.getLogger(__name__)

# ── Configuration from environment ──────────────────────────────────────

SERVER_NAME = os.environ.get("GPUHARBOR_SERVER_NAME", "gpuharbor-worker")
AUTH_TOKEN = os.environ.get("GPUHARBOR_AUTH_TOKEN", "")
ALLOW_UNAUTHENTICATED = os.environ.get(
    "GPUHARBOR_ALLOW_UNAUTHENTICATED", ""
).lower() in {"1", "true", "yes"}
HOST = os.environ.get(
    "GPUHARBOR_HOST",
    "127.0.0.1" if ALLOW_UNAUTHENTICATED else "0.0.0.0",
)
DB_PATH = os.environ.get("GPUHARBOR_DB_PATH", "/workspace/gpuharbor/jobs.db")
STORAGE_ROOT = Path(os.environ.get("GPUHARBOR_STORAGE_ROOT", "/workspace/gpuharbor"))
PORT = int(os.environ.get("GPUHARBOR_PORT", "5000"))
TLS_CERT = os.environ.get("GPUHARBOR_TLS_CERT", "")
TLS_KEY = os.environ.get("GPUHARBOR_TLS_KEY", "")
VAST_INSTANCE_ID = os.environ.get("GPUHARBOR_VAST_INSTANCE_ID", "")

# ── Globals initialised at startup ─────��────────────────────────────────

_start_time: float = 0
_job_store: JobStore | None = None
_storage: LocalStorage | None = None
_executor: JobExecutor | None = None
_checkpoint_mgr: CheckpointManager | None = None
_heartbeat: HeartbeatMonitor | None = None
_job_tasks: dict[str, asyncio.Task] = {}
_backup_tasks: dict[str, asyncio.Task] = {}
_export_tasks: dict[str, asyncio.Task] = {}
_exports: dict[str, dict] = {}
_uploads: UploadManager | None = None


def _is_loopback_host(host: str) -> bool:
    """Return whether a listener host is restricted to loopback."""
    normalized = host.strip().strip("[]")
    if normalized.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_security_configuration() -> None:
    """Fail closed unless auth is configured or safe dev mode is explicit."""
    if AUTH_TOKEN.strip():
        return
    if not ALLOW_UNAUTHENTICATED:
        raise RuntimeError(
            "GPUHARBOR_AUTH_TOKEN is required. For explicit local development "
            "only, set GPUHARBOR_ALLOW_UNAUTHENTICATED=1."
        )
    if not _is_loopback_host(HOST):
        raise RuntimeError(
            "Unauthenticated development mode may only bind to a loopback host"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    global _start_time, _job_store, _storage, _executor, _checkpoint_mgr, _heartbeat, _uploads

    validate_security_configuration()
    _start_time = time.time()

    _storage = LocalStorage(root=STORAGE_ROOT)
    _job_store = JobStore(db_path=DB_PATH)
    _executor = JobExecutor(storage=_storage, job_store=_job_store)
    _checkpoint_mgr = CheckpointManager(storage=_storage, job_store=_job_store, backup=_executor.backup)
    _uploads = UploadManager(_storage)
    _heartbeat = HeartbeatMonitor(job_store=_job_store, executor=_executor)

    # Reconcile all interrupted jobs before heartbeat observes their state.
    reattached = await _executor.reattach_running_jobs(
        checkpoint_mgr=_checkpoint_mgr
    )
    for job_id, task in reattached.items():
        _job_tasks[job_id] = task
        task.add_done_callback(lambda t, jid=job_id: _job_tasks.pop(jid, None))
    _heartbeat.start()

    logger.info(
        "GPUHarbor worker started: server=%s, storage=%s, port=%d",
        SERVER_NAME, STORAGE_ROOT, PORT,
    )

    yield

    # Graceful shutdown: cancel async tasks but job processes survive
    # (they run in their own sessions via start_new_session=True)
    logger.info("Shutting down GPUHarbor worker...")
    _heartbeat.stop()
    _checkpoint_mgr.stop_all()
    tasks = list(_job_tasks.values()) + list(_backup_tasks.values()) + list(_export_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(
    title="GPUHarbor Worker",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# ── Auth dependency ─────────────────────────────────────────────────────

async def verify_auth(request: Request) -> None:
    """Validate the bearer token on every request."""
    if not AUTH_TOKEN.strip():
        if ALLOW_UNAUTHENTICATED and _is_loopback_host(HOST):
            return
        raise HTTPException(
            status_code=401,
            detail="Authentication is not configured",
        )

    header = request.headers.get("authorization")
    token = extract_bearer_token(header)
    if not token or not validate_token(token, AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing authentication token")


# ── Status endpoint ─────────────────────────��───────────────────────────

@app.get("/v1/status", dependencies=[Depends(verify_auth)])
async def get_status():
    """Return server state: GPUs, utilization, memory, jobs, disk."""
    running_jobs = _job_store.count_active_jobs() if _job_store else 0
    uptime = int(time.time() - _start_time) if _start_time else 0
    status = await asyncio.to_thread(get_full_status, SERVER_NAME, running_jobs, uptime, VAST_INSTANCE_ID)
    status["protocol_version"] = 2
    status["capabilities"] = ["resumable_uploads", "project_archive", "exclusive_gpus", "checkpoint_sets", "backup", "resume"]
    status["gpu_allocations"] = dict(_executor._gpu_allocations) if _executor else {}
    status["backup_configured"] = bool(_executor and _executor.backup.destination)
    # Add disk info from storage
    if _storage:
        status["disk_free_gb"] = _storage.disk_free_gb()
    return status


# ── File upload / download ──────────────────────────────────────────────

@app.post("/v1/upload", dependencies=[Depends(verify_auth)])
async def upload_file(file: UploadFile = File(...)):
    """Upload a file to the worker's storage (uploads/ directory).

    Used to upload checkpoints, datasets, or other inputs before submitting a job.
    """
    if not _storage:
        raise HTTPException(status_code=503, detail="Worker not initialized")

    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")
    try:
        filename = validate_filename(file.filename)
    except StoragePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Legacy multipart endpoint remains bounded. New clients use upload sessions.
    digest = hashlib.sha256()
    size = 0
    with tempfile.NamedTemporaryFile(dir=_storage.root / "uploads", delete=False) as temporary:
        temp_path = Path(temporary.name)
    try:
        with temp_path.open("wb") as output:
            while data := await file.read(CHUNK_SIZE):
                size += len(data)
                if size > 64 * 1024 * 1024:
                    raise HTTPException(413, "Use /v1/uploads resumable sessions for files over 64 MiB")
                digest.update(data)
                await asyncio.to_thread(output.write, data)
        if not size:
            raise HTTPException(400, "Empty file")
        stored_name = f"{digest.hexdigest()}_{filename[-180:]}"
        target = _storage.root / "uploads" / stored_name
        await asyncio.to_thread(os.replace, temp_path, target)
        return {"filename": stored_name, "size": size, "sha256": digest.hexdigest(), "path": f"uploads/{stored_name}"}
    finally:
        temp_path.unlink(missing_ok=True)


class UploadStart(BaseModel):
    filename: str
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


@app.post("/v1/uploads", dependencies=[Depends(verify_auth)])
async def start_upload(req: UploadStart):
    if not _uploads:
        raise HTTPException(503, "Worker not initialized")
    try:
        return await asyncio.to_thread(_uploads.create, req.filename, req.size, req.sha256)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/v1/uploads/{upload_id}", dependencies=[Depends(verify_auth)])
async def upload_status(upload_id: str):
    try:
        return await asyncio.to_thread(_uploads.status, upload_id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc


@app.put("/v1/uploads/{upload_id}", dependencies=[Depends(verify_auth)])
async def upload_chunk(upload_id: str, request: Request, offset: int = Query(ge=0), sha256: str = Query(pattern=r"^[a-f0-9]{64}$")):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > CHUNK_SIZE:
            raise HTTPException(413, "Chunk exceeds 8 MiB")
    try:
        return await asyncio.to_thread(_uploads.append, upload_id, offset, bytes(content), sha256)
    except UploadConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/v1/uploads/{upload_id}/complete", dependencies=[Depends(verify_auth)])
async def finish_upload(upload_id: str):
    try:
        return await asyncio.to_thread(_uploads.finish, upload_id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/v1/files/{file_path:path}", dependencies=[Depends(verify_auth)])
async def download_file(file_path: str):
    """Download a file from the worker's storage by its relative path.

    Path is relative to the storage root (e.g., jobs/job_abc123/output/model.pt).
    """
    if not _storage:
        raise HTTPException(status_code=503, detail="Worker not initialized")

    try:
        validate_relative_path(file_path)
    except StoragePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    resolved = _storage.get_file(file_path)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

    return FileResponse(
        path=resolved,
        filename=resolved.name,
        media_type="application/octet-stream",
    )


# ── Job endpoints ───────────��───────────────────────────────────────────

class SubmitJobRequest(BaseModel):
    spec: JobSpec
    job_id: Optional[str] = None

    @field_validator("job_id")
    @classmethod
    def job_id_is_safe(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        try:
            return validate_job_id(value)
        except StoragePathError as exc:
            raise ValueError(str(exc)) from exc


@app.post("/v1/jobs", dependencies=[Depends(verify_auth)], status_code=201)
async def submit_job(req: SubmitJobRequest):
    """Submit a new job for execution."""
    if not _job_store or not _executor or not _checkpoint_mgr:
        raise HTTPException(status_code=503, detail="Worker not fully initialized")

    async with _executor._admission_lock:
        return await _submit_job(req)


async def _submit_job(req: SubmitJobRequest):
    # Validate input checkpoint exists if specified
    if req.spec.artifacts.input_checkpoint and _storage:
        filename = req.spec.artifacts.input_checkpoint
        found = _storage.get_file(f"uploads/{filename}")
        if found is None:
            raise HTTPException(
                status_code=400,
                detail=f"Input checkpoint '{filename}' not found in uploads. "
                f"Upload it first via POST /v1/upload",
            )

    job_id = req.job_id or f"job_{uuid.uuid4().hex}"
    existing = _job_store.get_job(job_id)
    if existing:
        if existing["spec"] != req.spec.model_dump(mode="json"):
            raise HTTPException(409, "Job ID already belongs to another specification")
        return {"job_id": job_id, "state": existing["state"], "server": SERVER_NAME}
    try:
        await asyncio.to_thread(_executor._validate_resources, job_id, req.spec)
        gpu_ids = await _executor.reserve_gpus(job_id, req.spec.resources.gpu_count)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

    # Create job record
    spec_json = req.spec.model_dump_json()
    try:
        job = _job_store.create_job(spec_json=spec_json, name=req.spec.name,
                                    project=req.spec.project, server_name=SERVER_NAME, job_id=job_id)
        _job_store.set_gpu_ids(job_id, gpu_ids)
    except Exception:
        _executor.release_gpus(job_id)
        raise
    job_id = job["job_id"]

    # Start execution in background
    task = asyncio.create_task(_run_job(job_id, req.spec))
    _job_tasks[job_id] = task
    task.add_done_callback(lambda t: _job_tasks.pop(job_id, None))

    return {"job_id": job_id, "state": "created", "server": SERVER_NAME}


async def _run_job(job_id: str, spec: JobSpec) -> None:
    """Execute a job and manage checkpointing."""
    try:
        if spec.checkpointing.enabled and _checkpoint_mgr:
            _checkpoint_mgr.start_monitoring(
                job_id=job_id,
                interval_minutes=spec.checkpointing.save_every_minutes,
                keep_last_n=spec.checkpointing.keep_last_n,
            )
        await _executor.execute_job(job_id, spec)
    finally:
        if _checkpoint_mgr:
            _checkpoint_mgr.stop_monitoring(job_id)


@app.get("/v1/jobs", dependencies=[Depends(verify_auth)])
async def list_jobs(
    state: Optional[str] = Query(None, description="Filter by job state"),
    project: Optional[str] = Query(None, description="Filter by project"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """List jobs on this server."""
    if not _job_store:
        raise HTTPException(status_code=503, detail="Worker not initialized")

    if state:
        try:
            JobState(state)
        except ValueError:
            valid = ", ".join(s.value for s in JobState)
            raise HTTPException(status_code=400, detail=f"Invalid state '{state}'. Valid: {valid}")

    jobs = _job_store.list_jobs(state=state, project=project, limit=limit, offset=offset)

    results = []
    for j in jobs:
        results.append({
            "job_id": j["job_id"],
            "name": j["name"],
            "project": j["project"],
            "state": j["state"],
            "server_name": j["server_name"],
            "created_at": j["created_at"],
            "started_at": j.get("started_at"),
            "completed_at": j.get("completed_at"),
            "error_message": j.get("error_message"),
        })

    return {"jobs": results, "count": len(results)}


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(verify_auth)])
async def get_job(job_id: str):
    """Get full job detail including spec and metrics."""
    if not _job_store:
        raise HTTPException(status_code=503, detail="Worker not initialized")

    job = _job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    return job


@app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(verify_auth)])
async def cancel_job(job_id: str):
    """Request cancellation of a running job."""
    if not _job_store or not _executor:
        raise HTTPException(status_code=503, detail="Worker not initialized")

    job = _job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    if is_terminal(JobState(job["state"])):
        raise HTTPException(
            status_code=409,
            detail=f"Job is already in terminal state: {job['state']}",
        )

    initiated = await _executor.cancel_job(job_id)
    if not initiated:
        raise HTTPException(
            status_code=409,
            detail="Job is not currently running (may be in a non-cancellable state)",
        )

    return {"job_id": job_id, "state": "cancel_requested"}


@app.get("/v1/jobs/{job_id}/logs", dependencies=[Depends(verify_auth)])
async def get_job_logs(
    job_id: str,
    request: Request,
    follow: bool = Query(False, description="Stream logs via SSE"),
    tail: int = Query(0, ge=0, description="Return last N lines (0 = all)"),
):
    """Fetch logs for a job. Supports SSE streaming with ?follow=true."""
    if not _job_store or not _executor:
        raise HTTPException(status_code=503, detail="Worker not initialized")

    job = _job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    if follow:
        async def event_stream():
            try:
                offset = max(0, int(request.headers.get("last-event-id", "0")))
            except ValueError:
                offset = 0
            async for line, cursor in _executor.stream_logs(job_id, offset=offset, tail=tail, with_offsets=True):
                yield {"event": "log", "id": str(cursor), "data": line}
            yield {"event": "done", "data": ""}

        return EventSourceResponse(event_stream())

    # Non-streaming: return log file contents
    log_path = _executor.get_log_file_path(job_id)
    if not log_path:
        return {"job_id": job_id, "logs": [], "message": "No logs available yet"}

    def read_logs():
        with log_path.open("rb") as source:
            if tail:
                source.seek(_executor._tail_offset(log_path, tail))
            data = source.read(4 * 1024 * 1024)
            truncated = bool(source.read(1))
        return {"job_id": job_id, "logs": data.decode("utf-8", errors="replace").splitlines(), "truncated": truncated}
    return await asyncio.to_thread(read_logs)


@app.get("/v1/jobs/{job_id}/artifacts", dependencies=[Depends(verify_auth)])
async def list_job_artifacts(job_id: str):
    """List artifacts for a job (from DB records and filesystem)."""
    if not _job_store:
        raise HTTPException(status_code=503, detail="Worker not initialized")

    job = _job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    artifacts = _job_store.get_artifacts(job_id)
    return {"job_id": job_id, "artifacts": artifacts}


@app.post("/v1/cleanup", dependencies=[Depends(verify_auth)])
async def cleanup_terminal_jobs(
    project: Optional[str] = Query(
        None,
        description="Only cleanup terminal jobs in this project",
    ),
    limit: int = Query(1000, ge=1, le=5000),
    dry_run: bool = True,
    force: bool = False,
):
    """Delete terminal job workspaces while retaining job/artifact metadata."""
    if not _executor:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    return await asyncio.to_thread(_executor.cleanup_terminal_job_dirs, project=project, limit=limit, dry_run=dry_run, force=force)


@app.post("/v1/jobs/{job_id}/cleanup", dependencies=[Depends(verify_auth)])
async def cleanup_job_files(job_id: str, dry_run: bool = True, force: bool = False):
    """Delete one terminal job workspace while retaining job/artifact metadata."""
    if not _job_store or not _storage:
        raise HTTPException(status_code=503, detail="Worker not initialized")

    job = _job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    if not is_terminal(JobState(job["state"])):
        raise HTTPException(
            status_code=409,
            detail=f"Job is not terminal: {job['state']}",
        )

    return await asyncio.to_thread(_executor.cleanup_terminal_job_dirs, only_job_id=job_id, dry_run=dry_run, force=force)


@app.post("/v1/jobs/{job_id}/pin", dependencies=[Depends(verify_auth)])
async def pin_job(job_id: str, pinned: bool = True):
    if not _job_store.get_job(job_id):
        raise HTTPException(404, "Job not found")
    _job_store.set_pinned(job_id, pinned)
    return {"job_id": job_id, "pinned": pinned}


@app.post("/v1/jobs/{job_id}/backup", dependencies=[Depends(verify_auth)], status_code=202)
async def backup_job(job_id: str):
    if not _job_store.get_job(job_id):
        raise HTTPException(404, "Job not found")
    if not _executor.backup.destination:
        raise HTTPException(400, "GPUHARBOR_BACKUP_DEST is not configured")
    if job_id not in _backup_tasks:
        async def run():
            try:
                await asyncio.to_thread(_executor.backup.snapshot, job_id)
            except Exception as exc:
                previous = _job_store.get_job(job_id).get("backup") or {}
                _job_store.set_backup(job_id, {**previous, "last_error": str(exc)})
            finally:
                _backup_tasks.pop(job_id, None)
        _backup_tasks[job_id] = asyncio.create_task(run())
    return {"job_id": job_id, "status": "running"}


@app.get("/v1/jobs/{job_id}/backup", dependencies=[Depends(verify_auth)])
async def backup_status(job_id: str):
    job = _job_store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {"status": "running" if job_id in _backup_tasks else "idle", "backup": job.get("backup")}


@app.post("/v1/doctor", dependencies=[Depends(verify_auth)])
async def run_doctor(python: str = "python3", cuda: bool = True):
    try:
        result = await asyncio.to_thread(doctor, python, cuda)
        result["disk_free_gb"] = _storage.disk_free_gb()
        result["protocol_version"] = 2
        return result
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _resume_bundle(job_id: str, checkpoint: str | None) -> dict:
    job = _job_store.get_job(job_id)
    if not job:
        raise ValueError("Job not found")
    units = completed_checkpoints(_storage, job_id)
    chosen = next((u for u in units if u.name == checkpoint), None) if checkpoint else (units[-1] if units else None)
    if chosen is None:
        raise ValueError("No complete checkpoint found")
    def pack(folder, label):
        filename = f"resume_{job_id}_{uuid.uuid4().hex}_{label}.tar"
        path = _storage.root / "uploads" / filename
        with tarfile.open(path, "w") as archive:
            for file in sorted(folder.rglob("*")):
                if file.is_symlink():
                    raise ValueError("Cannot export symbolic links")
                if file.is_file():
                    archive.add(file, arcname=file.relative_to(folder).as_posix(), recursive=False)
        return {"filename": filename, "path": f"uploads/{filename}", "size": path.stat().st_size, "sha256": compute_sha256(path)}
    project = _storage.job_dir(job_id) / "project"
    return {"spec": job["spec"], "checkpoint_name": chosen.name, "checkpoint": pack(chosen, "checkpoint"), "source": pack(project, "source") if project.exists() else None}


@app.post("/v1/jobs/{job_id}/resume-bundle", dependencies=[Depends(verify_auth)], status_code=202)
async def resume_bundle(job_id: str, checkpoint: str | None = None):
    if not _job_store.get_job(job_id):
        raise HTTPException(404, "Job not found")
    if job_id not in _export_tasks:
        _exports[job_id] = {"status": "running"}
        async def run():
            try:
                result = await asyncio.to_thread(_resume_bundle, job_id, checkpoint)
                _exports[job_id] = {"status": "complete", "bundle": result}
            except Exception as exc:
                _exports[job_id] = {"status": "failed", "error": str(exc)}
            finally:
                _export_tasks.pop(job_id, None)
        _export_tasks[job_id] = asyncio.create_task(run())
    return {"status": "running"}


@app.get("/v1/jobs/{job_id}/resume-bundle", dependencies=[Depends(verify_auth)])
async def resume_bundle_status(job_id: str):
    if job_id not in _exports:
        raise HTTPException(404, "Export not found; start it again")
    return _exports[job_id]


# ── Artifact download URL (returns file path for direct download) ──────

@app.get("/v1/artifacts/{artifact_id}/download-url", dependencies=[Depends(verify_auth)])
async def get_artifact_download_url(artifact_id: str):
    """Return the download path for an artifact.

    The client should use GET /v1/files/{path} to download the file.
    """
    if not _job_store:
        raise HTTPException(status_code=503, detail="Worker not initialized")

    artifact = _job_store.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}")

    return {
        "artifact_id": artifact_id,
        "download_path": f"/v1/files/{artifact['uri']}",
        "filename": Path(artifact["uri"]).name,
    }


# ── Metrics reporting endpoint ──────────────────────────────────────────

class MetricsReport(BaseModel):
    job_id: str
    step: Optional[int] = None
    epoch: Optional[float] = None
    loss: Optional[float] = None
    samples_per_sec: Optional[float] = None
    gpu_util: Optional[int] = None
    gpu_mem_gb: Optional[float] = None


@app.post("/v1/metrics", dependencies=[Depends(verify_auth)])
async def report_metrics(metrics: MetricsReport):
    """Receive structured training metrics from a running job."""
    if not _job_store:
        raise HTTPException(status_code=503, detail="Worker not initialized")

    job = _job_store.get_job(metrics.job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {metrics.job_id}")

    _job_store.update_metrics(metrics.job_id, metrics.model_dump(exclude_none=True))
    return {"status": "ok"}


# ── Health check (no auth) ─────���────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "server": SERVER_NAME}


# ── Entry point ─────────────────────────────────────────────────────────

def main():
    """Run the worker agent."""
    import uvicorn

    log_level = os.environ.get("GPUHARBOR_LOG_LEVEL", "info").lower()
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    ssl_kwargs = {}
    if TLS_CERT and TLS_KEY:
        ssl_kwargs["ssl_certfile"] = TLS_CERT
        ssl_kwargs["ssl_keyfile"] = TLS_KEY

    validate_security_configuration()

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level=log_level,
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()
