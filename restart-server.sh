#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="/workspace/gpuharbor_venv"
WORKER_BIN="${VENV_DIR}/bin/gpuharbor-worker"
STORAGE_ROOT="${GPUHARBOR_STORAGE_ROOT:-/workspace/gpuharbor}"
ENV_FILE="${STORAGE_ROOT}/worker.env"
PID_FILE="${STORAGE_ROOT}/worker.pid"

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

# Gracefully stop the worker process only (job processes continue in their own sessions)
echo "Stopping worker process... (running jobs will continue)"
if [[ -f "$PID_FILE" ]]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        kill "$OLD_PID"
        for i in $(seq 1 10); do
            kill -0 "$OLD_PID" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "Worker didn't stop gracefully, force killing..."
            kill -9 "$OLD_PID" 2>/dev/null || true
            sleep 1
        fi
        echo "Worker stopped."
    else
        echo "PID $OLD_PID not running."
    fi
else
    echo "No PID file found, checking for stray processes..."
    pkill -f "gpuharbor-worker" 2>/dev/null || true
    sleep 1
fi

echo "Starting GPUHarborWorker..."
nohup "$WORKER_BIN" > "${STORAGE_ROOT}/worker.log" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
echo "GPUHarborWorker started (PID: ${NEW_PID}). Running jobs preserved."
echo "Logs: ${STORAGE_ROOT}/worker.log"
