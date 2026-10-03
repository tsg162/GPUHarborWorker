# GPUHarbor Vast template

This template boots a supervised GPUHarbor worker and Cloudflare tunnel on every
instance start. Rent instances yourself; no provider provisioning service is needed.
Worker configuration, jobs and caches live under `/workspace`. Completed checkpoint
backups should go to storage outside the instance.

## Option 1: Build the reusable image (recommended for frequent training)

From the **GPUHarborWorker repository root**:

```bash
docker build -f vast/Dockerfile -t YOUR_REGISTRY/gpuharbor-worker:0.2.0 .
docker push YOUR_REGISTRY/gpuharbor-worker:0.2.0
```

The Dockerfile pins a Vast PyTorch base by tag and digest, installs hashed worker
dependencies and pins/checksums cloudflared. It preserves Vast's entrypoint and its
`/venv/main` training environment. Use `--build-arg BASE_IMAGE=...` to deliberately
choose another compatible Vast image. Verify that image/driver combination with
`gpuharbor doctor` on the rented machine.

On your laptop, install the updated CLI, create a tunnel once, then generate the
private template (the file contains tokens and is written with mode 0600):

```bash
python3 -m pip install -e ../GPUHarbor
gpuharbor tunnels create gpu1
gpuharbor deploy gpu1 --default \
  --image YOUR_REGISTRY/gpuharbor-worker:0.2.0 \
  --vast-template "$HOME/gpu1-private-template.json"
```

Save the template privately in Vast's console. Copy `image`, `env`, and `onstart`
from the JSON into the corresponding image, Docker options/environment, and
on-start fields; choose SSH mode. Alternatively the JSON is a request body for
Vast's documented `POST /api/v0/template` API. Generating it does not contact Vast,
publish anything or rent an instance. `template.example.json` is a credential-free
reference; it is not ready to boot until deployment credentials are supplied.

Use only one live instance per tunnel/server name. Retire the old connector before
reusing its name; simultaneous connectors may route requests to different machines.
For a custom tunnel origin port, pass the same `--port` to `tunnels create` and
`deploy`. No inbound mapping for worker port 5000 is necessary. SSH can retain its normal mapping.

After renting with the saved template:

```bash
gpuharbor doctor --server gpu1
gpuharbor submit examples/train.yaml --server gpu1
gpuharbor logs JOB_ID --follow
```

`onstart.sh` reinstalls only when its code/config fingerprint changes. Supervisor
restarts crashed workers/tunnels and rotates their logs. Worker restarts preserve
training subprocesses; stopping/destroying the **container** stops training.

## Option 2: Provision a pinned worker commit on a Vast PyTorch image

Publish your worker changes to GitHub first. Use the complete published commit SHA:

```bash
gpuharbor deploy gpu1 --default \
  --image vastai/pytorch:multi-210-291-271-py312-2026-09-08 \
  --worker-ref FULL_40_CHARACTER_PUBLISHED_COMMIT_SHA \
  --vast-template "$HOME/gpu1-private-template.json"
```

This uses an on-start script fetched from that same commit, then clones/checks out
exactly that commit and installs `requirements.lock`. A private/unpublished commit
cannot be fetched by a public raw GitHub URL. The custom image option can package
local changes without first publishing a Git commit.

## Backup and cache configuration

Add these **private instance environment variables** as needed:

- `GPUHARBOR_BACKUP_DEST=myremote:gpuharbor`: an already configured rclone remote.
- `RCLONE_CONFIG=/workspace/secrets/rclone.conf`: mounted/provisioned rclone credentials.
- Or `GPUHARBOR_BACKUP_DEST=/mnt/backup/gpuharbor`: a separately mounted durable target,
  outside the worker storage root. A second folder on the instance's own disk is
  not protection against instance destruction.
- `GPUHARBOR_CACHE_ROOT=/workspace/gpuharbor-cache`: shared Hugging Face, PyTorch,
  pip and environment caches. Point this at a mounted volume for longer retention.

Configure rclone before relying on backups (`rclone lsd myremote:`). Backup failures
appear on the job record; they do not fail training, and checkpoint pruning is
blocked until backup succeeds. Terminal cleanup protects unverified outputs.
Vast container storage survives stop/start but is deleted with the instance;
Vast local volumes are tied to one host. Use off-instance storage for migration.

## Startup and troubleshooting

```bash
tail -f /workspace/gpuharbor/worker.log
tail -f /workspace/gpuharbor/tunnel.log
bash /workspace/gpuharbor/restart.sh
/opt/gpuharbor_venv/bin/python -m gpuharbor.worker.service status
```

Provisioned installations use `/workspace/gpuharbor_venv/bin/python` instead.
The installer verifies `/v1/status` with the worker token locally and through
`GPUHARBOR_WORKER_URL`; a wrong tunnel port or wrong worker fails readiness.
Pinned images/commits update by choosing a new version and rerunning setup.

References: [Vast template customization](https://docs.vast.ai/guides/templates/advanced-setup),
[template API](https://docs.vast.ai/api-reference/templates/create-template),
[storage behavior](https://docs.vast.ai/guides/instances/storage/types).
