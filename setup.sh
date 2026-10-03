#!/usr/bin/env bash
# Called by gpuharbor deploy. Explicit arguments override saved configuration.
set -euo pipefail
umask 077
[[ $# -ge 2 ]] || { echo 'Usage: setup.sh TUNNEL_TOKEN AUTH_TOKEN [NAME] [URL] [COMMIT]' >&2; exit 1; }
SETUP_TUNNEL="$1"
SETUP_AUTH="$2"
SETUP_NAME="${3:-$(hostname -s)}"
SETUP_URL="${4:-}"
SETUP_REF="${5:-main}"
REPO_DIR="${GPUHARBOR_WORKER_REPO:-/workspace/GPUHarborWorker}"
if [[ ! -d "$REPO_DIR/.git" ]]; then
    git clone https://github.com/tsg162/GPUHarborWorker.git "$REPO_DIR"
fi
if [[ -n "$(git -C "$REPO_DIR" status --porcelain)" ]]; then
    echo "Worker checkout has local changes; install directly from it or use a clean checkout" >&2
    exit 1
fi
git -C "$REPO_DIR" fetch origin "$SETUP_REF" --depth 1
if [[ "$SETUP_REF" == "main" ]]; then
    git -C "$REPO_DIR" checkout main
    git -C "$REPO_DIR" merge --ff-only FETCH_HEAD
else
    git -C "$REPO_DIR" checkout --detach FETCH_HEAD
fi
if [[ -f "$REPO_DIR/.env" ]]; then set -a; source "$REPO_DIR/.env"; set +a; fi
export GPUHARBOR_TUNNEL_TOKEN="$SETUP_TUNNEL"
export GPUHARBOR_AUTH_TOKEN="$SETUP_AUTH"
export GPUHARBOR_SERVER_NAME="$SETUP_NAME"
export GPUHARBOR_WORKER_URL="$SETUP_URL"
export GPUHARBOR_WORKER_REF="$SETUP_REF"
export GPUHARBOR_WORKER_REPO="$REPO_DIR"
export GPUHARBOR_SKIP_DOTENV=1
bash "$REPO_DIR/install.sh"
