#!/usr/bin/env bash
set -euo pipefail

WORKER_BIN="/workspace/gpuharbor_venv/bin/gpuharbor-worker"
STORAGE_ROOT="${GPUHARBOR_STORAGE_ROOT:-/workspace/gpuharbor}"
ENV_FILE="${STORAGE_ROOT}/worker.env"

# Load environment if available
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
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
