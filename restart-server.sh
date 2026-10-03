#!/usr/bin/env bash
set -euo pipefail
exec "${GPUHARBOR_WORKER_VENV:-/workspace/gpuharbor_venv}/bin/python" -m gpuharbor.worker.service restart --root "${GPUHARBOR_STORAGE_ROOT:-/workspace/gpuharbor}" "$@"
