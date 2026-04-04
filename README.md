# GPUHarbor Worker

Worker agent that runs on GPU instances (Vast.ai, Runpod, bare metal) to accept and execute training jobs via Docker containers. Part of the [GPUHarbor](https://github.com/tsg162/GPUHarbor) ecosystem.

## Quick Start

SSH into your GPU instance and run:

```bash
git clone https://github.com/tsg162/GPUHarborWorker.git
cd GPUHarborWorker
./install.sh
```

The script will:
1. Verify GPU drivers and CUDA are working
2. Install Docker + NVIDIA Container Toolkit
3. Install the worker agent in a virtualenv at `/workspace/gpuharbor_venv`
4. Generate an auth token
5. Set up TLS (self-signed by default)
6. Start the worker service

When done, it prints connection info to paste into your `~/.gpuharbor/servers.yaml`:

```
============================================
  GPUHarbor worker ready!

  URL:    https://45.67.89.10:8443
  Token:  ghb_tok_a1b2c3d4e5f6...
  GPU:    4x NVIDIA RTX 4090
  CUDA:   12.4
  Disk:   450GB free
  Store:  /workspace/gpuharbor

  Add to ~/.gpuharbor/servers.yaml:

    servers:
      my-server:
        url: https://45.67.89.10:8443
        token: ghb_tok_a1b2c3d4e5f6...
        description: "4x RTX 4090"
============================================
```

## Configuration

Set these env vars or put them in a `.env` file before running `install.sh`:

| Variable | Default | Description |
|----------|---------|-------------|
| `GPUHARBOR_PORT` | Auto-detected (Vast.ai) or `8443` | API port |
| `GPUHARBOR_TLS` | `auto` | `auto` (self-signed), `none`, or cert path prefix |
| `GPUHARBOR_MAX_CONCURRENT_JOBS` | `1` | Max simultaneous jobs |
| `GPUHARBOR_STORAGE_ROOT` | `/workspace/gpuharbor` | Where jobs and artifacts are stored |

## Storage

All artifacts are stored locally on disk under `/workspace/gpuharbor/`:

```
/workspace/gpuharbor/
├── jobs.db                    # SQLite job state
├── auth_token                 # Bearer token
├── uploads/                   # Files uploaded via CLI
└── jobs/
    └── job_abc123/
        ├── input/             # Checkpoints, datasets
        ├── output/            # Final models, logs
        ├── checkpoints/       # Periodic checkpoints
        └── logs/              # Container stdout/stderr
```

No S3 or external storage needed. The CLI uploads/downloads files directly to/from the worker over HTTP.

## API

All endpoints require `Authorization: Bearer <token>` (except `/health`).

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check (no auth) |
| `GET` | `/v1/status` | Server state: GPUs, utilization, memory, jobs, disk |
| `POST` | `/v1/upload` | Upload a file (multipart) |
| `GET` | `/v1/files/{path}` | Download a file by path |
| `POST` | `/v1/jobs` | Submit a new job |
| `GET` | `/v1/jobs` | List jobs |
| `GET` | `/v1/jobs/{id}` | Job detail + metrics |
| `POST` | `/v1/jobs/{id}/cancel` | Request cancellation |
| `GET` | `/v1/jobs/{id}/logs` | Fetch logs (`?follow=true` for SSE streaming) |
| `GET` | `/v1/jobs/{id}/artifacts` | List artifacts |
| `POST` | `/v1/metrics` | Report training metrics |

## Managing the Worker

```bash
# If using systemd:
systemctl status gpuharbor-worker
journalctl -u gpuharbor-worker -f

# If running directly (Vast.ai containers without systemd):
tail -f /workspace/gpuharbor/worker.log
bash /workspace/gpuharbor/restart.sh
```

## Re-running install.sh

The script is idempotent. Running it again will:
- Skip already-installed components
- Reuse the existing auth token
- Reuse the existing TLS certificate
- Restart the worker service
