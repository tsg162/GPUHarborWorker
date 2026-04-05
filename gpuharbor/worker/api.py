"""FastAPI worker agent exposing the GPUHarbor API.

All artifacts are stored on local disk under /workspace/gpuharbor/.
Files are transferred between CLI and worker via HTTP multipart upload/download.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from gpuharbor.common.auth import extract_bearer_token, validate_token
from gpuharbor.common.job_spec import JobSpec
from gpuharbor.common.states import JobState, is_terminal
from gpuharbor.common.storage import LocalStorage
from gpuharbor.worker.checkpoint import CheckpointManager
from gpuharbor.worker.executor import JobExecutor
from gpuharbor.worker.gpu import get_full_status
from gpuharbor.worker.heartbeat import HeartbeatMonitor
from gpuharbor.worker.state import JobStore

logger = logging.getLogger(__name__)

# ── Configuration from environment ──────────────────────────────────────

SERVER_NAME = os.environ.get("GPUHARBOR_SERVER_NAME", "gpuharbor-worker")
AUTH_TOKEN = os.environ.get("GPUHARBOR_AUTH_TOKEN", "")
DB_PATH = os.environ.get("GPUHARBOR_DB_PATH", "/workspace/gpuharbor/jobs.db")
STORAGE_ROOT = Path(os.environ.get("GPUHARBOR_STORAGE_ROOT", "/workspace/gpuharbor"))
PORT = int(os.environ.get("GPUHARBOR_PORT", "5000"))
TLS_CERT = os.environ.get("GPUHARBOR_TLS_CERT", "")
TLS_KEY = os.environ.get("GPUHARBOR_TLS_KEY", "")

# ── Globals initialised at startup ─────��────────────────────────────────

_start_time: float = 0
_job_store: JobStore | None = None
_storage: LocalStorage | None = None
_executor: JobExecutor | None = None
_checkpoint_mgr: CheckpointManager | None = None
_heartbeat: HeartbeatMonitor | None = None
_job_tasks: dict[str, asyncio.Task] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    global _start_time, _job_store, _storage, _executor, _checkpoint_mgr, _heartbeat

    _start_time = time.time()

    _storage = LocalStorage(root=STORAGE_ROOT)
    _job_store = JobStore(db_path=DB_PATH)
    _executor = JobExecutor(storage=_storage, job_store=_job_store)
    _checkpoint_mgr = CheckpointManager(storage=_storage, job_store=_job_store)
    _heartbeat = HeartbeatMonitor(job_store=_job_store)
    _heartbeat.start()

    logger.info(
        "GPUHarbor worker started: server=%s, storage=%s, port=%d",
        SERVER_NAME, STORAGE_ROOT, PORT,
    )

    yield

    logger.info("Shutting down GPUHarbor worker...")
    _heartbeat.stop()
    _checkpoint_mgr.stop_all()
    for task in _job_tasks.values():
        task.cancel()
    if _job_tasks:
        await asyncio.gather(*_job_tasks.values(), return_exceptions=True)


app = FastAPI(
    title="GPUHarbor Worker",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Auth dependency ─────────────────────────────────────────────────────

async def verify_auth(request: Request) -> None:
    """Validate the bearer token on every request."""
    if not AUTH_TOKEN:
        return  # No token = auth disabled (dev mode)

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
    status = get_full_status(SERVER_NAME, running_jobs, uptime)
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

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    path, sha = _storage.store_upload(file.filename, data)
    logger.info("Uploaded file: %s (%d bytes, sha256:%s)", file.filename, len(data), sha[:16])

    return {
        "filename": file.filename,
        "size": len(data),
        "sha256": sha,
        "path": f"uploads/{file.filename}",
    }


@app.get("/v1/files/{file_path:path}", dependencies=[Depends(verify_auth)])
async def download_file(file_path: str):
    """Download a file from the worker's storage by its relative path.

    Path is relative to the storage root (e.g., jobs/job_abc123/output/model.pt).
    """
    if not _storage:
        raise HTTPException(status_code=503, detail="Worker not initialized")

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


@app.post("/v1/jobs", dependencies=[Depends(verify_auth)], status_code=201)
async def submit_job(req: SubmitJobRequest):
    """Submit a new job for execution."""
    if not _job_store or not _executor or not _checkpoint_mgr:
        raise HTTPException(status_code=503, detail="Worker not fully initialized")

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

    # Create job record
    spec_json = req.spec.model_dump_json()
    job = _job_store.create_job(
        spec_json=spec_json,
        name=req.spec.name,
        project=req.spec.project,
        server_name=SERVER_NAME,
        job_id=req.job_id,
    )
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
    follow: bool = Query(False, description="Stream logs via SSE"),
    tail: int = Query(0, ge=0, description="Return last N lines (0 = all)"),
):
    """Fetch logs for a job. Supports SSE streaming with ?follow=true."""
    if not _job_store or not _executor:
        raise HTTPException(status_code=503, detail="Worker not initialized")

    job = _job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    if follow and not is_terminal(JobState(job["state"])):
        async def event_stream():
            async for line in _executor.stream_logs(job_id):
                yield {"event": "log", "data": line}
            yield {"event": "done", "data": ""}

        return EventSourceResponse(event_stream())

    # Non-streaming: return log file contents
    log_path = _executor.get_log_file_path(job_id)
    if not log_path:
        return {"job_id": job_id, "logs": [], "message": "No logs available yet"}

    with open(log_path) as f:
        lines = f.readlines()

    if tail > 0:
        lines = lines[-tail:]

    return {"job_id": job_id, "logs": [l.rstrip("\n") for l in lines]}


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

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level=log_level,
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()
