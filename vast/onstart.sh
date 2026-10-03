#!/usr/bin/env bash
# Works as Vast onstart on EVERY container start (not only provisioning).
set -euo pipefail
umask 077
export GPUHARBOR_STORAGE_ROOT="${GPUHARBOR_STORAGE_ROOT:-/workspace/gpuharbor}"
export GPUHARBOR_CACHE_ROOT="${GPUHARBOR_CACHE_ROOT:-/workspace/gpuharbor-cache}"
export GPUHARBOR_TRAINING_PYTHON="${GPUHARBOR_TRAINING_PYTHON:-/venv/main/bin/python}"
export GPUHARBOR_HOST="${GPUHARBOR_HOST:-127.0.0.1}"
export GPUHARBOR_PORT="${GPUHARBOR_PORT:-5000}"
if [[ -x /opt/gpuharbor_venv/bin/python ]]; then
    export GPUHARBOR_WORKER_VENV=/opt/gpuharbor_venv
else
    export GPUHARBOR_WORKER_VENV="${GPUHARBOR_WORKER_VENV:-/workspace/gpuharbor_venv}"
fi
: "${GPUHARBOR_AUTH_TOKEN:?Supply the private token from gpuharbor deploy}"
: "${GPUHARBOR_TUNNEL_TOKEN:?Supply the tunnel token from gpuharbor deploy}"
if [[ -f /opt/GPUHarborWorker/install.sh ]]; then
    export GPUHARBOR_WORKER_REPO=/opt/GPUHarborWorker
    export GPUHARBOR_WORKER_REF=image
else
    : "${GPUHARBOR_WORKER_REF:?Pin a published worker commit SHA}"
    [[ "$GPUHARBOR_WORKER_REF" =~ ^[a-fA-F0-9]{40}$ ]] || { echo 'Worker ref must be a commit SHA' >&2; exit 1; }
    export GPUHARBOR_WORKER_REPO=/workspace/GPUHarborWorker
    if [[ ! -d "$GPUHARBOR_WORKER_REPO/.git" ]]; then
        git clone --no-checkout https://github.com/tsg162/GPUHarborWorker.git "$GPUHARBOR_WORKER_REPO"
    fi
    git -C "$GPUHARBOR_WORKER_REPO" fetch --depth 1 origin "$GPUHARBOR_WORKER_REF"
    git -C "$GPUHARBOR_WORKER_REPO" checkout --detach "$GPUHARBOR_WORKER_REF"
fi
# Reuse the installed worker after a stop/start; reinstall only for a new code/lock fingerprint.
mkdir -p "$GPUHARBOR_STORAGE_ROOT" "$GPUHARBOR_CACHE_ROOT"
FINGERPRINT=$(cat "$GPUHARBOR_WORKER_REPO/requirements.lock" "$GPUHARBOR_WORKER_REPO/pyproject.toml"; find "$GPUHARBOR_WORKER_REPO/gpuharbor" -name '*.py' -type f -print0 | sort -z | xargs -0 sha256sum)
FINGERPRINT=$(printf '%s\n' "$FINGERPRINT" "${GPUHARBOR_AUTH_TOKEN}" "${GPUHARBOR_TUNNEL_TOKEN}" "${GPUHARBOR_SERVER_NAME:-}" "${GPUHARBOR_BACKUP_DEST:-}" "${GPUHARBOR_WORKER_URL:-}" "${GPUHARBOR_TRAINING_PYTHON}" "${GPUHARBOR_PORT}" | sha256sum | cut -d' ' -f1)
if [[ -x "$GPUHARBOR_WORKER_VENV/bin/python" && -f "$GPUHARBOR_STORAGE_ROOT/worker.env" && -f "$GPUHARBOR_STORAGE_ROOT/install.fingerprint" && "$(cat "$GPUHARBOR_STORAGE_ROOT/install.fingerprint")" == "$FINGERPRINT" ]]; then
    "$GPUHARBOR_WORKER_VENV/bin/python" -m gpuharbor.worker.service start --root "$GPUHARBOR_STORAGE_ROOT"
else
    GPUHARBOR_SKIP_DOTENV=1 bash "$GPUHARBOR_WORKER_REPO/install.sh"
    printf '%s' "$FINGERPRINT" > "$GPUHARBOR_STORAGE_ROOT/install.fingerprint"
fi
