#!/usr/bin/env bash
# GPUHarbor Worker Bootstrap Script
#
# Turns a fresh GPU instance (Vast.ai, Runpod, bare metal) into a ready
# GPUHarbor worker.  Designed to be idempotent and fast on re-run.
#
# Jobs run as direct subprocesses — no Docker required.
# All artifacts stored locally under /workspace/gpuharbor/.
#
# Usage:
#   git clone https://github.com/tsg162/GPUHarborWorker.git
#   cd GPUHarborWorker && ./install.sh
#
# Configuration (env vars or .env file):
#   GPUHARBOR_PORT                 API port (auto-detected on Vast.ai, default: 8443)
#   GPUHARBOR_TLS                  "auto" (self-signed), "none", or cert path prefix
#   GPUHARBOR_MAX_CONCURRENT_JOBS  Max simultaneous jobs (default: 1)
#   GPUHARBOR_STORAGE_ROOT         Storage root (default: /workspace/gpuharbor)

set -euo pipefail

# ── Colors ──────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
fatal()   { error "$@"; exit 1; }

# ── Load .env if present ───────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/.env" ]]; then
    info "Loading configuration from ${SCRIPT_DIR}/.env"
    set -a; source "${SCRIPT_DIR}/.env"; set +a
elif [[ -f ".env" ]]; then
    info "Loading configuration from .env"
    set -a; source ".env"; set +a
fi

# ── Configuration defaults ─────────────────────────────────────────────

GPUHARBOR_TLS="${GPUHARBOR_TLS:-auto}"
GPUHARBOR_MAX_CONCURRENT_JOBS="${GPUHARBOR_MAX_CONCURRENT_JOBS:-1}"
GPUHARBOR_STORAGE_ROOT="${GPUHARBOR_STORAGE_ROOT:-/workspace/gpuharbor}"
GPUHARBOR_LOG_LEVEL="${GPUHARBOR_LOG_LEVEL:-info}"

PYTHON_MIN_VERSION="3.10"

# ── Auto-detect port (Vast.ai awareness) ───────────────────────────────

port_is_free() {
    ! ss -tlnp 2>/dev/null | grep -q ":${1} " && return 0
    return 1
}

auto_detect_port() {
    # On Vast.ai, exposed ports are in VAST_TCP_PORT_* env vars.
    # Try each mapped port, but only if it's actually free.
    local preferred_ports=(5000 8443 8000 1111)

    for port in "${preferred_ports[@]}"; do
        local var="VAST_TCP_PORT_${port}"
        if [[ -n "${!var:-}" ]] && port_is_free "$port"; then
            echo "$port"
            return
        fi
    done

    # Fall back: any Vast.ai mapped port that's free
    for var in $(compgen -v VAST_TCP_PORT_ 2>/dev/null || true); do
        local port="${var#VAST_TCP_PORT_}"
        if [[ "$port" =~ ^[0-9]+$ ]] && port_is_free "$port"; then
            echo "$port"
            return
        fi
    done

    # Last resort: find any free port from preferred list
    for port in 5000 8443 8000 9000 7000; do
        if port_is_free "$port"; then
            echo "$port"
            return
        fi
    done

    echo "5000"
}

GPUHARBOR_PORT="${GPUHARBOR_PORT:-$(auto_detect_port)}"

# ── Pre-flight checks ──────────────────────────────────────────────────

info "Starting GPUHarbor worker installation..."
echo ""

# Check disk space
DISK_FREE_KB=$(df /workspace 2>/dev/null | awk 'NR==2 {print $4}' || df / | awk 'NR==2 {print $4}')
DISK_FREE_GB=$(( DISK_FREE_KB / 1024 / 1024 ))
if [[ "$DISK_FREE_GB" -lt 5 ]]; then
    warn "Only ${DISK_FREE_GB}GB free disk space."
    read -p "Continue anyway? [y/N] " -n 1 -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]] || exit 1
fi

# ── Step 1: Verify GPU ─────────────────────────────────────────────────

info "Step 1/5: Verifying GPU and CUDA..."

if ! command -v nvidia-smi &>/dev/null; then
    fatal "nvidia-smi not found. Install NVIDIA drivers first."
fi

if ! nvidia-smi &>/dev/null; then
    fatal "nvidia-smi failed. GPU drivers may not be properly installed."
fi

GPU_INFO=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null || true)
GPU_COUNT=$(echo "$GPU_INFO" | grep -c '[^[:space:]]' || echo "0")
CUDA_VERSION=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || echo "unknown")

if [[ "$GPU_COUNT" -eq 0 ]]; then
    fatal "No GPUs detected by nvidia-smi"
fi

GPU_DESC=""
while IFS=, read -r model mem; do
    model=$(echo "$model" | xargs)
    mem=$(echo "$mem" | xargs)
    if [[ -n "$model" ]]; then
        GPU_DESC="${GPU_DESC:+$GPU_DESC, }${model} (${mem} MiB)"
    fi
done <<< "$GPU_INFO"

success "Found ${GPU_COUNT} GPU(s): ${GPU_DESC}"
success "CUDA driver version: ${CUDA_VERSION}"

# ── Step 2: Install Python and GPUHarbor worker ────────────────────────

info "Step 2/5: Installing GPUHarbor worker..."

PYTHON=""
for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" &>/dev/null; then
        PY_VERSION=$($candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
        PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
        PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
        if [[ "$PY_MAJOR" -ge 3 && "$PY_MINOR" -ge 10 ]]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    info "Python >= ${PYTHON_MIN_VERSION} not found. Installing..."
    if [[ -f /etc/os-release ]]; then source /etc/os-release; fi
    case "${ID:-unknown}" in
        ubuntu|debian)
            apt-get update -qq
            apt-get install -y -qq python3 python3-pip python3-venv
            ;;
        centos|rhel|fedora|amzn)
            yum install -y python3 python3-pip
            ;;
    esac
    PYTHON="python3"
fi

success "Using Python: $($PYTHON --version)"

# Create venv under /workspace so it persists across Vast.ai stops
VENV_DIR="/workspace/gpuharbor_venv"
if [[ ! -d "$VENV_DIR" ]]; then
    $PYTHON -m venv "$VENV_DIR"
fi

VENV_PIP="${VENV_DIR}/bin/pip"

if [[ -d "${SCRIPT_DIR}/gpuharbor" ]]; then
    info "Installing from local source..."
    "$VENV_PIP" install -q "${SCRIPT_DIR}"
else
    info "Installing gpuharbor package..."
    "$VENV_PIP" install -q gpuharbor-worker
fi

success "GPUHarbor worker installed"

# ── Step 3: Generate auth token ────────────────────────────────────────

info "Step 3/5: Setting up authentication..."

mkdir -p "${GPUHARBOR_STORAGE_ROOT}"
TOKEN_FILE="${GPUHARBOR_STORAGE_ROOT}/auth_token"

if [[ -f "$TOKEN_FILE" ]]; then
    AUTH_TOKEN=$(cat "$TOKEN_FILE")
    success "Using existing auth token"
else
    AUTH_TOKEN=$("${VENV_DIR}/bin/python" -c "from gpuharbor.common.auth import generate_token; print(generate_token())")
    echo "$AUTH_TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    success "Generated new auth token"
fi

# ── Step 4: TLS setup ──────────────────────────────────────────────────

info "Step 4/5: Configuring TLS..."

TLS_CERT_PATH=""
TLS_KEY_PATH=""

if [[ "$GPUHARBOR_TLS" == "auto" ]]; then
    CERT_DIR="${GPUHARBOR_STORAGE_ROOT}/tls"
    TLS_CERT_PATH="${CERT_DIR}/cert.pem"
    TLS_KEY_PATH="${CERT_DIR}/key.pem"

    if [[ -f "$TLS_CERT_PATH" && -f "$TLS_KEY_PATH" ]]; then
        success "Using existing self-signed certificate"
    else
        mkdir -p "$CERT_DIR"
        PUBLIC_IP=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')

        openssl req -x509 -newkey rsa:4096 -keyout "$TLS_KEY_PATH" -out "$TLS_CERT_PATH" \
            -days 365 -nodes -subj "/CN=gpuharbor-worker" \
            -addext "subjectAltName=IP:${PUBLIC_IP},IP:127.0.0.1" \
            2>/dev/null

        chmod 600 "$TLS_KEY_PATH"
        success "Generated self-signed TLS certificate for ${PUBLIC_IP}"
    fi
elif [[ "$GPUHARBOR_TLS" == "none" ]]; then
    warn "TLS disabled. Communication will be unencrypted."
else
    TLS_CERT_PATH="${GPUHARBOR_TLS}.crt"
    TLS_KEY_PATH="${GPUHARBOR_TLS}.key"
    if [[ ! -f "$TLS_CERT_PATH" || ! -f "$TLS_KEY_PATH" ]]; then
        fatal "TLS cert/key not found at ${TLS_CERT_PATH} / ${TLS_KEY_PATH}"
    fi
    success "Using provided TLS certificate"
fi

# ── Step 5: Start the worker ───────────────────────────────────────────

info "Step 5/5: Starting worker..."

PUBLIC_IP=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
HOSTNAME_LABEL=$(hostname -s 2>/dev/null || echo "gpuharbor-worker")

# Write environment file
ENV_FILE="${GPUHARBOR_STORAGE_ROOT}/worker.env"
cat > "$ENV_FILE" << ENVEOF
GPUHARBOR_SERVER_NAME=${HOSTNAME_LABEL}
GPUHARBOR_AUTH_TOKEN=${AUTH_TOKEN}
GPUHARBOR_PORT=${GPUHARBOR_PORT}
GPUHARBOR_MAX_CONCURRENT_JOBS=${GPUHARBOR_MAX_CONCURRENT_JOBS}
GPUHARBOR_DB_PATH=${GPUHARBOR_STORAGE_ROOT}/jobs.db
GPUHARBOR_STORAGE_ROOT=${GPUHARBOR_STORAGE_ROOT}
GPUHARBOR_LOG_LEVEL=${GPUHARBOR_LOG_LEVEL}
ENVEOF

if [[ -n "$TLS_CERT_PATH" ]]; then
    echo "GPUHARBOR_TLS_CERT=${TLS_CERT_PATH}" >> "$ENV_FILE"
    echo "GPUHARBOR_TLS_KEY=${TLS_KEY_PATH}" >> "$ENV_FILE"
fi

chmod 600 "$ENV_FILE"

WORKER_BIN="${VENV_DIR}/bin/gpuharbor-worker"

# Kill any existing worker
pkill -f "gpuharbor-worker" 2>/dev/null || true
sleep 1

# Start worker with nohup (works everywhere — systemd or not)
set -a; source "$ENV_FILE"; set +a
nohup "$WORKER_BIN" > "${GPUHARBOR_STORAGE_ROOT}/worker.log" 2>&1 &
WORKER_PID=$!
echo "$WORKER_PID" > "${GPUHARBOR_STORAGE_ROOT}/worker.pid"

sleep 2
if kill -0 "$WORKER_PID" 2>/dev/null; then
    success "GPUHarbor worker started (PID: ${WORKER_PID})"
else
    error "Worker failed to start. Check log:"
    tail -20 "${GPUHARBOR_STORAGE_ROOT}/worker.log" 2>/dev/null || true
    exit 1
fi

# Write restart helper
cat > "${GPUHARBOR_STORAGE_ROOT}/restart.sh" << 'RESTARTEOF'
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pkill -f "gpuharbor-worker" 2>/dev/null || true
sleep 1
set -a; source "$SCRIPT_DIR/worker.env"; set +a
nohup "/workspace/gpuharbor_venv/bin/gpuharbor-worker" > "$SCRIPT_DIR/worker.log" 2>&1 &
echo $! > "$SCRIPT_DIR/worker.pid"
echo "Worker restarted (PID: $!)"
RESTARTEOF
chmod +x "${GPUHARBOR_STORAGE_ROOT}/restart.sh"

# ── Print connection info ──────────────────────────────────────────────

PROTOCOL="https"
if [[ "$GPUHARBOR_TLS" == "none" ]]; then
    PROTOCOL="http"
fi

CONNECT_URL="${PROTOCOL}://${PUBLIC_IP}:${GPUHARBOR_PORT}"
DISK_FREE=$(df -BG /workspace 2>/dev/null | awk 'NR==2 {print $4}' | tr -d 'G' || df -BG / | awk 'NR==2 {print $4}' | tr -d 'G')
GPU_FIRST_NAME=$(echo "$GPU_INFO" | head -1 | cut -d, -f1 | xargs)

echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "${GREEN}  GPUHarbor worker ready!${NC}"
echo ""
echo -e "  URL:    ${BOLD}${CONNECT_URL}${NC}"
echo -e "  Token:  ${BOLD}${AUTH_TOKEN}${NC}"
echo -e "  GPU:    ${GPU_COUNT}x ${GPU_FIRST_NAME}"
echo -e "  CUDA:   ${CUDA_VERSION}"
echo -e "  Disk:   ${DISK_FREE}GB free"
echo -e "  Store:  ${GPUHARBOR_STORAGE_ROOT}"
echo ""
echo -e "  Add to ${CYAN}~/.gpuharbor/servers.yaml${NC}:"
echo ""
echo -e "    ${YELLOW}servers:${NC}"
echo -e "    ${YELLOW}  ${HOSTNAME_LABEL}:${NC}"
echo -e "    ${YELLOW}    url: ${CONNECT_URL}${NC}"
echo -e "    ${YELLOW}    token: ${AUTH_TOKEN}${NC}"
echo -e "    ${YELLOW}    description: \"${GPU_COUNT}x ${GPU_FIRST_NAME}\"${NC}"
echo -e "${BOLD}============================================${NC}"
echo ""
echo -e "Manage the worker:"
echo -e "  ${CYAN}tail -f ${GPUHARBOR_STORAGE_ROOT}/worker.log${NC}"
echo -e "  ${CYAN}bash ${GPUHARBOR_STORAGE_ROOT}/restart.sh${NC}"
echo ""
