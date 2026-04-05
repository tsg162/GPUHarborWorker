#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="/workspace/gpuharbor_venv"
WORKER_BIN="${VENV_DIR}/bin/gpuharbor-worker"
STORAGE_ROOT="${GPUHARBOR_STORAGE_ROOT:-/workspace/gpuharbor}"
ENV_FILE="${STORAGE_ROOT}/worker.env"

# Re-install package from local source if available
if [[ -d "${SCRIPT_DIR}/gpuharbor" ]]; then
    echo "Reinstalling worker from local source..."
    "${VENV_DIR}/bin/pip" install -q "${SCRIPT_DIR}"
fi

# Load environment if available
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# Ensure Vast.ai instance ID is in worker.env (CONTAINER_ID is set by Vast.ai)
if [[ -n "${CONTAINER_ID:-}" ]] && ! grep -q "^GPUHARBOR_VAST_INSTANCE_ID=" "$ENV_FILE" 2>/dev/null; then
    echo "GPUHARBOR_VAST_INSTANCE_ID=${CONTAINER_ID}" >> "$ENV_FILE"
    export GPUHARBOR_VAST_INSTANCE_ID="${CONTAINER_ID}"
    echo "Added Vast.ai instance ID (${CONTAINER_ID}) to ${ENV_FILE}"
fi

echo "Stopping existing GPUHarborWorker process..."
if pkill -f "gpuharbor-worker"; then
    echo "Process stopped. Waiting 1 second..."
    sleep 1
else
    echo "No existing process found."
fi

echo "Starting GPUHarborWorker..."
nohup "$WORKER_BIN" > "${STORAGE_ROOT}/worker.log" 2>&1 &
echo "GPUHarborWorker started (PID: $!). Logs: ${STORAGE_ROOT}/worker.log"
