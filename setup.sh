#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# setup.sh — One-command GPU worker bootstrap
#
# Called by `gpuharbor deploy` (recommended) or standalone.
#
# Deploy mode (both tokens — nothing to copy back):
#   setup.sh <tunnel-token> <auth-token>
#
# Manual mode (tunnel token only — prints servers add command):
#   setup.sh <tunnel-token> [server-name]
#
# One-liner on a fresh GPU instance:
#   bash <(curl -sL https://raw.githubusercontent.com/tsg162/GPUHarborWorker/main/setup.sh) <args...>
# ─────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Parse arguments ───────────────────────────────────────────────────

if [[ $# -lt 1 ]]; then
    cat <<'USAGE'
Usage: setup.sh <tunnel-token> <auth-token>       (deploy mode)
       setup.sh <tunnel-token> [server-name]       (manual mode)

Deploy mode — called by `gpuharbor deploy`:
  Both tokens provided. Server is already pre-registered on the control
  node. Nothing to copy back — just paste and wait.

Manual mode — tunnel token only:
  Generates an auth token on the worker and prints the `gpuharbor servers add`
  command to run on the control node.

Examples:
  gpuharbor deploy gpu1                            # prints the one-liner
  bash <(curl -sL .../setup.sh) eyJ... ghb_tok_... # paste the one-liner
  ./setup.sh eyJ... gpu1                           # manual: tunnel token + name
USAGE
    exit 1
fi

TUNNEL_TOKEN="$1"

# Detect mode: if second arg starts with ghb_tok_, it's an auth token (deploy mode)
AUTH_TOKEN_PRESET=""
SERVER_NAME=""
if [[ "${2:-}" == ghb_tok_* ]]; then
    AUTH_TOKEN_PRESET="$2"
    SERVER_NAME="${3:-$(hostname -s 2>/dev/null || echo worker)}"
else
    SERVER_NAME="${2:-$(hostname -s 2>/dev/null || echo worker)}"
fi

REPO_URL="https://github.com/tsg162/GPUHarborWorker.git"
STORAGE_ROOT="${GPUHARBOR_STORAGE_ROOT:-/workspace/gpuharbor}"

# ── Colors ────────────────────────────────────────────────────────────

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

header() { echo -e "\n${BOLD}${CYAN}▸ $*${NC}"; }

# ── Locate or clone repo ─────────────────────────────────────────────

REPO_DIR=""

if [[ -f "./install.sh" && -d "./gpuharbor" ]]; then
    REPO_DIR="$(pwd)"
    header "Using repo in current directory"
elif [[ -f "/workspace/GPUHarborWorker/install.sh" ]]; then
    REPO_DIR="/workspace/GPUHarborWorker"
    header "Updating existing repo..."
    git -C "$REPO_DIR" pull --ff-only 2>/dev/null || true
else
    if [[ -d "/workspace" ]]; then
        REPO_DIR="/workspace/GPUHarborWorker"
    else
        REPO_DIR="${HOME}/GPUHarborWorker"
    fi
    header "Cloning GPUHarborWorker → ${REPO_DIR}"
    git clone --depth 1 "$REPO_URL" "$REPO_DIR"
fi

cd "$REPO_DIR"

# ── Write .env ────────────────────────────────────────────────────────

{
    echo "GPUHARBOR_TUNNEL_TOKEN=${TUNNEL_TOKEN}"
    if [[ -n "$AUTH_TOKEN_PRESET" ]]; then
        echo "GPUHARBOR_AUTH_TOKEN=${AUTH_TOKEN_PRESET}"
    fi
} > .env

echo -e "${DIM}       .env written${NC}"

# ── Run install ───────────────────────────────────────────────────────

header "Running install..."
echo ""
chmod +x install.sh
GPUHARBOR_QUIET_HINTS=1 ./install.sh

# ── Output ────────────────────────────────────────────────────────────

if [[ -n "$AUTH_TOKEN_PRESET" ]]; then
    # Deploy mode — control node already has everything
    echo ""
    echo -e "${BOLD}${GREEN}Done!${NC} Worker is running."
    echo -e "${DIM}The control node already has this server registered.${NC}"
    echo -e "${DIM}Verify with:  gpuharbor servers${NC}"
    echo ""
else
    # Manual mode — user needs to register on control node
    AUTH_TOKEN=""
    if [[ -f "${STORAGE_ROOT}/auth_token" ]]; then
        AUTH_TOKEN=$(cat "${STORAGE_ROOT}/auth_token")
    fi

    if [[ -z "$AUTH_TOKEN" ]]; then
        echo -e "\n${YELLOW}Could not read auth token from ${STORAGE_ROOT}/auth_token${NC}"
        exit 1
    fi

    # Try to detect tunnel URL from cloudflared logs
    TUNNEL_URL=""
    if [[ -f "${STORAGE_ROOT}/tunnel.log" ]]; then
        for _ in 1 2 3 4 5; do
            DETECTED=$(grep -o '"hostname":"[^"]*"' "${STORAGE_ROOT}/tunnel.log" 2>/dev/null \
                | head -1 | sed 's/"hostname":"//;s/"//g' || true)
            if [[ -n "$DETECTED" ]]; then
                TUNNEL_URL="https://${DETECTED}"
                break
            fi
            sleep 2
        done
    fi

    # Fall back to naming convention
    if [[ -z "$TUNNEL_URL" ]]; then
        TUNNEL_URL="https://${SERVER_NAME}.gpuharbor.xyz"
        URL_GUESSED=true
    else
        URL_GUESSED=false
    fi

    echo ""
    echo -e "${BOLD}┌─────────────────────────────────────────────────────────────┐${NC}"
    echo -e "${BOLD}│${NC}  ${GREEN}Run this on your control node:${NC}                              ${BOLD}│${NC}"
    echo -e "${BOLD}├─────────────────────────────────────────────────────────────┤${NC}"
    echo -e "${BOLD}│${NC}                                                             ${BOLD}│${NC}"
    echo -e "${BOLD}│${NC}  ${CYAN}gpuharbor servers add ${SERVER_NAME} \\${NC}"
    echo -e "${BOLD}│${NC}  ${CYAN}  --url ${TUNNEL_URL} \\${NC}"
    echo -e "${BOLD}│${NC}  ${CYAN}  --token ${AUTH_TOKEN}${NC}"
    echo -e "${BOLD}│${NC}                                                             ${BOLD}│${NC}"
    echo -e "${BOLD}└─────────────────────────────────────────────────────────────┘${NC}"

    if $URL_GUESSED; then
        echo -e "  ${DIM}URL inferred from server name — adjust if your tunnel uses a different hostname.${NC}"
    fi
    echo ""
fi
