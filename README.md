# GPUHarbor Worker 0.2

A self-contained training worker for rented Vast.ai/Runpod instances and Linux GPU
servers. FastAPI + SQLite manage direct training subprocesses. The companion
[GPUHarbor CLI](../GPUHarbor/README.md) uploads projects, submits jobs, streams logs,
and transfers artifacts. No central coordinator or provider provisioning is needed.

## Start on Vast

Use the [reusable Vast template](vast/README.md): a pinned Dockerfile or a pinned
Git commit installed by the instance's on-start script. Worker and tunnel processes
are supervised, caches are shared across runs, and startup verifies the authenticated
public worker URL. The template does not open the worker port to the Internet.

For an existing GPU instance, generate a deployment command on your laptop:

```bash
gpuharbor tunnels create gpu1       # one time
gpuharbor deploy gpu1 --default     # paste the resulting command on the instance
gpuharbor doctor --server gpu1 --python /venv/main/bin/python
```

Or install this checkout directly on the GPU instance:

```bash
export GPUHARBOR_TUNNEL_TOKEN=YOUR_TUNNEL_TOKEN
export GPUHARBOR_AUTH_TOKEN=YOUR_PRIVATE_WORKER_TOKEN
export GPUHARBOR_SERVER_NAME=gpu1
export GPUHARBOR_WORKER_URL=https://gpu1.gpuharbor.xyz
bash install.sh
```

Requirements: Linux, NVIDIA drivers (`nvidia-smi`), Python >=3.10, and a compatible
training interpreter (such as the Vast image's `/venv/main/bin/python`). The worker
has its own venv; it does not install PyTorch into the training environment. The
installer installs hashed dependencies and a pinned, checksum-verified cloudflared.

## Submit and resume training

From your laptop, using the worker repository's included example:

```bash
gpuharbor submit examples/train.yaml --server gpu1
gpuharbor logs JOB_ID --follow
gpuharbor download JOB_ID --out results
gpuharbor backup JOB_ID
gpuharbor resume JOB_ID --server gpu2
```

The CLI uploads a project archive, respecting Git exclusions and `.gpuharborignore`.
The worker starts the command in `project/work_dir`, with `environment.python` and
an optional flattened, hashed `environment.requirements_lock`. The dependency cache
key includes the interpreter identity and lock content. Isolated environments are
the default; `system_site_packages: true` explicitly reuses base packages.

Injected variables:

| Variable | Meaning |
|---|---|
| `CUDA_VISIBLE_DEVICES` | Exclusively allocated GPU IDs |
| `GPUHARBOR_JOB_ID` | Job ID |
| `GPUHARBOR_INPUT_DIR` | Uploaded input files |
| `GPUHARBOR_OUTPUT_DIR` | Final model and output files |
| `GPUHARBOR_CHECKPOINT_DIR` | Checkpoint directory root |
| `GPUHARBOR_DATASET_DIR` | Validated `artifacts.dataset` directory, when specified |
| `GPUHARBOR_RESUME_DIR` | Restored checkpoint contents for a resume job |
| `HF_HOME`, `TORCH_HOME`, `PIP_CACHE_DIR` | Shared cache defaults, overridable in job env |
| `PYTHONUNBUFFERED` | Enabled for prompt log output |

Each checkpoint must be a uniquely named directory containing a **complete set**
of model, optimizer, scheduler/RNG, and trainer state as appropriate. After all ranks
finish writing, rank zero publishes it:

```python
from gpuharbor_training import checkpoint_complete
checkpoint_complete(checkpoint_directory)
```

The helper is provided automatically through a small runtime module, with no extra
training dependency. Do not modify a published directory. Only directories bearing
a valid completion manifest are backed up/pruned while training is active. Loose
legacy files and unfinished checkpoints are never automatically pruned.
`save_every_minutes` is a discovery interval; the trainer decides when to save.

A resume creates a new job and supplies checkpoint contents in `GPUHARBOR_RESUME_DIR`.
The training command must explicitly load that directory. `examples/train.py`
demonstrates restoring model, optimizer, step and RNG. No implicit auto-retry is
advertised or accepted. The CLI and worker share a versioned wire contract.

## Configuration

Set environment variables or a `.env` alongside `install.sh`:

| Variable | Default | Purpose |
|---|---|---|
| `GPUHARBOR_AUTH_TOKEN` | Generated/reused by installer | Required bearer authentication |
| `GPUHARBOR_TUNNEL_TOKEN` | None | Named Cloudflare tunnel |
| `GPUHARBOR_WORKER_URL` | None | Public URL to verify during startup |
| `GPUHARBOR_SERVER_NAME` | Hostname | Reported worker identity |
| `GPUHARBOR_HOST` | `127.0.0.1` with tunnel; otherwise `0.0.0.0` | Listener |
| `GPUHARBOR_PORT` | `5000` with tunnel; otherwise auto-detected | Local port |
| `GPUHARBOR_STORAGE_ROOT` | `/workspace/gpuharbor` | Job and service state |
| `GPUHARBOR_CACHE_ROOT` | `/workspace/gpuharbor-cache` in installer | Shared caches |
| `GPUHARBOR_TRAINING_PYTHON` | `python3`; Vast template sets `/venv/main/bin/python` | Default training interpreter |
| `GPUHARBOR_BACKUP_DEST` | None | Mounted path outside storage, or `rclone-remote:prefix` |
| `RCLONE_CONFIG` | rclone default | Config file for remote backup credentials |
| `GPUHARBOR_TLS` | `none` | `auto` or certificate path prefix for direct HTTPS |
| `GPUHARBOR_WORKER_VENV` | `/workspace/gpuharbor_venv` | Worker interpreter; baked image uses `/opt/gpuharbor_venv` |

Manual `gpuharbor-worker` startup requires a nonempty token. For local development
only, `GPUHARBOR_ALLOW_UNAUTHENTICATED=1` allows a loopback listener. Other anonymous
configurations fail closed. HTTP file access confines paths and rejects traversal.

## Backups and cleanup

Configure a mounted durable target or an existing rclone remote. The worker copies
completed checkpoints and source while running, and the full workspace on terminal
completion. Files are checked against SHA-256 manifests; rclone snapshots are verified
with `rclone check --download`. Job specs in backups omit `env` secrets. Source files
can still contain secrets: keep them out of uploaded projects.

Backup failures appear in `job.backup.last_error`, do not fail training, and prevent
checkpoint pruning until a later successful backup. Restore a downloaded snapshot
with `gpuharbor resume --snapshot PATH --job-file train.yaml --server gpu2`.

Low disk never deletes prior jobs. `gpuharbor cleanup` previews eligible workspaces;
`--execute` deletes only unpinned terminal jobs with a matching verified backup.
`--force --execute` explicitly bypasses backup protection. `gpuharbor pin JOB_ID`
protects a job even during forced cleanup. Backup destination capacity should include
staging space; this implementation deliberately favors verified snapshots over
in-place remote mirroring. Remote snapshot retention is managed by your storage policy.

## Managing the worker

```bash
bash /workspace/gpuharbor/restart.sh
bash /workspace/gpuharbor/restart.sh --update   # unpinned main-branch source installs only
/workspace/gpuharbor_venv/bin/python -m gpuharbor.worker.service status
tail -f /workspace/gpuharbor/worker.log
tail -f /workspace/gpuharbor/tunnel.log
```

All restart entry points use the same private Supervisor instance. It restarts crashed
workers/tunnels and rotates their logs. Training processes use independent sessions
and direct file logs, so worker restarts preserve them. Durable completion receipts
reconcile jobs that finish during worker downtime; live jobs restore their GPU leases.
Container stop/reboot does stop training; resume from a checkpoint afterward.

## API

Every endpoint except `/health` requires the worker bearer token.

| Endpoint | Purpose |
|---|---|
| `GET /v1/status` | GPU status, protocol/capabilities, allocations, disk |
| `POST /v1/doctor` | Training interpreter and CUDA smoke check |
| `POST /v1/uploads` | Start/recover content-addressed upload session |
| `GET, PUT /v1/uploads/{id}` | Resume status / one bounded chunk |
| `POST /v1/uploads/{id}/complete` | Verify and atomically publish uploaded file |
| `POST /v1/upload` | Legacy multipart upload, capped at 64 MiB |
| `GET /v1/files/{path}` | Stream/download, including HTTP byte ranges |
| `POST, GET /v1/jobs` | Idempotent submission / paginated inventory |
| `GET /v1/jobs/{id}` | Spec, state, allocation, latest metrics and backup status |
| `POST /v1/jobs/{id}/cancel` | Graceful cancellation |
| `GET /v1/jobs/{id}/logs` | Bounded log fetch or SSE with independent byte cursors |
| `GET /v1/jobs/{id}/artifacts` | Deduplicated artifact inventory |
| `POST, GET /v1/jobs/{id}/backup` | Start/poll a verified backup |
| `POST, GET /v1/jobs/{id}/resume-bundle` | Prepare/poll complete checkpoint + source archives |
| `POST /v1/jobs/{id}/pin` | Protect a workspace |
| `POST /v1/cleanup`, `/v1/jobs/{id}/cleanup` | Protected cleanup, dry-run by default |
| `POST /v1/metrics` | Report latest training metrics |

Uploads use 8 MiB **separate requests**, staying below Cloudflare public HTTP body
limits. Chunks and complete files are checksummed; interrupted clients resume from
durable offsets. The API offloads file work and GPU queries from its event loop.
GPU inventory is cached briefly. Ordinary log fetches cap output at 4 MiB and report
`truncated`; use `--tail`, `--follow`, or download the log artifact for larger logs.

## Optional dashboard

```bash
cd dashboard
npm ci
export GPUHARBOR_DASHBOARD_PASSWORD='choose-a-private-password'
export GPUHARBOR_STORAGE_ROOT=/workspace/gpuharbor
npm start
```

The dashboard binds to loopback by default and requires HTTP Basic authentication
(user `admin`, password above). Its proxy reads the worker token from `worker.env`.
Job environment values and internal execution markers are redacted. Access it over
an SSH forward or an HTTPS tunnel; do not send Basic credentials over public HTTP.
Set `WORKER_URL` when using a worker port other than 5000.

## Development checks

```bash
uv run --no-project --with pytest --with httpx --with fastapi --with uvicorn \
  --with sse-starlette --with python-multipart --with pydantic --with supervisor \
  python -m pytest -q
(cd dashboard && npm ci && npm test)
```

The CLI remains in the GPUHarbor repository. Do not install both distributions into
the same Python environment: they intentionally share the `gpuharbor` namespace.

With the sibling CLI checkout installed in its `.venv`, run the full local workflow:

```bash
uv run --no-project --with-requirements requirements.lock --with httpx \
  python scripts/smoke_workflow.py
```

This uses a real HTTP worker and CLI subprocesses with simulated GPU inventory; it
checks project upload, execution, logs, transfers, backups, cleanup, and both resume
paths. It does not measure CUDA throughput or provision a cloud instance.
