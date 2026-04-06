# GPUHarbor Worker

Worker agent that runs on GPU instances (Vast.ai, Runpod, bare metal) to accept and execute training jobs. Part of the [GPUHarbor](https://github.com/tsg162/GPUHarbor) ecosystem.

## Quick Start

### 1. Create a tunnel (one-time, from your laptop)

```bash
gpuharbor tunnels create gpu1
# Outputs: GPUHARBOR_TUNNEL_TOKEN=eyJ...
```

This creates a permanent URL `gpu1.gpuharbor.xyz` that survives instance restarts. See the [GPUHarbor README](https://github.com/tsg162/GPUHarbor#tunnel-management) for one-time Cloudflare setup.

### 2. Set up the worker (on the GPU instance)

```bash
git clone https://github.com/tsg162/GPUHarborWorker.git
cd GPUHarborWorker

# Add tunnel token to .env
echo "GPUHARBOR_TUNNEL_TOKEN=eyJ...paste-token-here..." > .env

# Install and start everything
./install.sh
```

The script will:
1. Verify GPU drivers and CUDA
2. Install Python >= 3.10, create venv, install `gpuharbor-worker`
3. Generate an auth token (`ghb_tok_...`)
4. Configure TLS (default: none — tunnel provides encryption)
5. Start the worker and health-check it
6. Install `cloudflared` and connect the named tunnel

### 3. Register the server (one-time, from your laptop)

```bash
gpuharbor servers add gpu1 \
  --url https://gpu1.gpuharbor.xyz \
  --token ghb_tok_...from-install-output... \
  --default
```

### Reusing tunnels

When you destroy a Vast.ai instance and create a new one, just use the same `GPUHARBOR_TUNNEL_TOKEN` in `.env`. The new instance connects to the same tunnel automatically — no need to re-create the tunnel or update your CLI config. `gpu1.gpuharbor.xyz` just points to the new machine.

## Configuration

Set these in a `.env` file before running `install.sh`:

| Variable | Default | Description |
|----------|---------|-------------|
| `GPUHARBOR_TUNNEL_TOKEN` | _(none)_ | Cloudflare tunnel token (from `gpuharbor tunnels create`) |
| `GPUHARBOR_PORT` | Auto-detected | Internal bind port (Vast.ai port mapping auto-detected) |
| `GPUHARBOR_TLS` | `none` | `none`, `auto` (self-signed), or cert path prefix |
| `GPUHARBOR_STORAGE_ROOT` | `/workspace/gpuharbor` | Where jobs and artifacts are stored |
| `GPUHARBOR_LOG_LEVEL` | `info` | Logging level |

## Storage

```
/workspace/gpuharbor/
├── jobs.db                    # SQLite job state
├── auth_token                 # Bearer token
├── worker.env                 # Runtime config (including tunnel token)
├── worker.pid / tunnel.pid    # Process PIDs
├── worker.log / tunnel.log    # Process logs
├── restart.sh                 # Restart worker + tunnel
├── uploads/                   # Files uploaded via CLI
└── jobs/
    └── job_abc123/
        ├── input/             # Checkpoints, datasets
        ├── output/            # Final models, logs
        ├── checkpoints/       # Periodic checkpoints
        └── logs/              # Job stdout/stderr
```

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
tail -f /workspace/gpuharbor/worker.log    # live worker logs
tail -f /workspace/gpuharbor/tunnel.log    # live tunnel logs
bash /workspace/gpuharbor/restart.sh       # restart worker + tunnel
curl http://localhost:5000/health           # health check
```

## Re-running install.sh

The script is idempotent. Running it again will:
- Skip already-installed components
- Reuse the existing auth token
- Restart the worker and tunnel
