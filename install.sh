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
#   GPUHARBOR_PORT                 Internal bind port (auto-detected, default: 5000)
#   GPUHARBOR_TLS                  "auto" (self-signed), "none" (default), or cert path prefix
#   GPUHARBOR_STORAGE_ROOT         Storage root (default: /workspace/gpuharbor)
#   GPUHARBOR_TUNNEL_TOKEN         Cloudflare named tunnel token (enables persistent tunnel)

set -euo pipefail

# ── Colors ──────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[ OK ]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
fatal()   { error "$@"; exit 1; }
debug()   { echo -e "${DIM}       $*${NC}"; }

# ── Load .env if present ───────────────────────────────────────────────

# Clear stale port config that may linger from a previous run's worker.env
# so that only values explicitly set in .env (or auto-detected) are used.
unset GPUHARBOR_PORT GPUHARBOR_EXTERNAL_PORT 2>/dev/null || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/.env" ]]; then
    info "Loading configuration from ${SCRIPT_DIR}/.env"
    set -a; source "${SCRIPT_DIR}/.env"; set +a
elif [[ -f ".env" ]]; then
    info "Loading configuration from .env"
    set -a; source ".env"; set +a
fi

# ── Configuration defaults ─────────────────────────────────────────────

GPUHARBOR_TLS="${GPUHARBOR_TLS:-none}"
GPUHARBOR_STORAGE_ROOT="${GPUHARBOR_STORAGE_ROOT:-/workspace/gpuharbor}"
GPUHARBOR_LOG_LEVEL="${GPUHARBOR_LOG_LEVEL:-info}"

PYTHON_MIN_VERSION="3.10"

# ── Auto-detect port (Vast.ai awareness) ───────────────────────────────
#
# On Vast.ai, VAST_TCP_PORT_XXXX=YYYYY means:
#   - XXXX = internal port the process should BIND to
#   - YYYYY = external port clients connect to from outside
# We bind to XXXX internally and show YYYYY for the external URL.

port_is_free() {
    ! ss -tlnp 2>/dev/null | grep -q ":${1} " && return 0
    return 1
}

# Sets GPUHARBOR_PORT (bind) and GPUHARBOR_EXTERNAL_PORT (advertise)
detect_ports() {
    local preferred=(5000 8443 8000 1111)

    # Detect if we're on Vast.ai
    local vastai_detected=false
    local vast_mappings=""
    for var in $(compgen -v VAST_TCP_PORT_ 2>/dev/null || true); do
        vastai_detected=true
        local internal="${var#VAST_TCP_PORT_}"
        if [[ "$internal" =~ ^[0-9]+$ ]]; then
            vast_mappings="${vast_mappings}  ${internal} -> ${!var} (external)\n"
        fi
    done

    if $vastai_detected; then
        info "Vast.ai detected. Port mappings found:"
        for var in $(compgen -v VAST_TCP_PORT_ 2>/dev/null || true); do
            local p="${var#VAST_TCP_PORT_}"
            if [[ "$p" =~ ^[0-9]+$ ]]; then
                debug "${p} -> ${!var} (external)"
            fi
        done
    fi

    # Try preferred internal ports that have a Vast.ai mapping and are free
    for internal in "${preferred[@]}"; do
        local var="VAST_TCP_PORT_${internal}"
        if [[ -n "${!var:-}" ]]; then
            if [[ "$internal" -gt 65535 ]]; then
                debug "Port ${internal} exceeds 65535, skipping"
            elif port_is_free "$internal"; then
                GPUHARBOR_PORT="$internal"
                GPUHARBOR_EXTERNAL_PORT="${!var}"
                info "Selected port ${internal} (internal) -> ${!var} (external)"
                return
            else
                debug "Port ${internal} is in use, skipping"
            fi
        fi
    done

    # Try any Vast.ai mapped port that's free (skip invalid ports > 65535 and port 22)
    for var in $(compgen -v VAST_TCP_PORT_ 2>/dev/null || true); do
        local internal="${var#VAST_TCP_PORT_}"
        if [[ "$internal" =~ ^[0-9]+$ ]] && [[ "$internal" -le 65535 ]] && [[ "$internal" -ne 22 ]] && port_is_free "$internal"; then
            GPUHARBOR_PORT="$internal"
            GPUHARBOR_EXTERNAL_PORT="${!var}"
            info "Selected port ${internal} (internal) -> ${!var} (external)"
            return
        fi
    done

    # No Vast.ai — find a free port, external = internal
    for port in 5000 8443 8000 9000 7000; do
        if port_is_free "$port"; then
            GPUHARBOR_PORT="$port"
            GPUHARBOR_EXTERNAL_PORT="$port"
            info "Selected port ${port} (no Vast.ai port mapping found)"
            return
        fi
    done

    GPUHARBOR_PORT="5000"
    GPUHARBOR_EXTERNAL_PORT="5000"
    warn "All preferred ports in use, defaulting to 5000"
}

if [[ -z "${GPUHARBOR_PORT:-}" ]]; then
    detect_ports
else
    GPUHARBOR_EXTERNAL_PORT="${GPUHARBOR_EXTERNAL_PORT:-$GPUHARBOR_PORT}"
    info "Using configured port: ${GPUHARBOR_PORT}"
fi

# ── Detect Vast.ai instance ID ───────────────────────────────────────
#
# Vast.ai sets CONTAINER_ID=<instance_id> in the environment.

VAST_INSTANCE_ID="${CONTAINER_ID:-}"
if [[ -n "$VAST_INSTANCE_ID" ]]; then
    info "Vast.ai instance ID detected: ${VAST_INSTANCE_ID}"
fi

# ── Pre-flight checks ──────────────────────────────────────────────────

echo ""
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

if [[ -n "${GPUHARBOR_AUTH_TOKEN:-}" ]]; then
    # Pre-configured token (from gpuharbor deploy)
    AUTH_TOKEN="$GPUHARBOR_AUTH_TOKEN"
    echo "$AUTH_TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    success "Using pre-configured auth token"
elif [[ -f "$TOKEN_FILE" ]]; then
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
    success "TLS disabled (use a tunnel for encryption)"
else
    TLS_CERT_PATH="${GPUHARBOR_TLS}.crt"
    TLS_KEY_PATH="${GPUHARBOR_TLS}.key"
    if [[ ! -f "$TLS_CERT_PATH" || ! -f "$TLS_KEY_PATH" ]]; then
        fatal "TLS cert/key not found at ${TLS_CERT_PATH} / ${TLS_KEY_PATH}"
    fi
    success "Using provided TLS certificate"
fi

# ── Step 5: Start the worker ───────────────────────────────────────────

info "Step 5/5: Starting worker on port ${GPUHARBOR_PORT}..."

PUBLIC_IP=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
HOSTNAME_LABEL=$(hostname -s 2>/dev/null || echo "gpuharbor-worker")

PROTOCOL="http"
if [[ "$GPUHARBOR_TLS" != "none" ]]; then
    PROTOCOL="https"
fi

# Write environment file
ENV_FILE="${GPUHARBOR_STORAGE_ROOT}/worker.env"
cat > "$ENV_FILE" << ENVEOF
GPUHARBOR_SERVER_NAME=${HOSTNAME_LABEL}
GPUHARBOR_AUTH_TOKEN=${AUTH_TOKEN}
GPUHARBOR_PORT=${GPUHARBOR_PORT}
GPUHARBOR_DB_PATH=${GPUHARBOR_STORAGE_ROOT}/jobs.db
GPUHARBOR_STORAGE_ROOT=${GPUHARBOR_STORAGE_ROOT}
GPUHARBOR_LOG_LEVEL=${GPUHARBOR_LOG_LEVEL}
GPUHARBOR_VAST_INSTANCE_ID=${VAST_INSTANCE_ID}
ENVEOF

if [[ -n "$TLS_CERT_PATH" ]]; then
    echo "GPUHARBOR_TLS_CERT=${TLS_CERT_PATH}" >> "$ENV_FILE"
    echo "GPUHARBOR_TLS_KEY=${TLS_KEY_PATH}" >> "$ENV_FILE"
fi

if [[ -n "${GPUHARBOR_TUNNEL_TOKEN:-}" ]]; then
    echo "GPUHARBOR_TUNNEL_TOKEN=${GPUHARBOR_TUNNEL_TOKEN}" >> "$ENV_FILE"
fi

chmod 600 "$ENV_FILE"

WORKER_BIN="${VENV_DIR}/bin/gpuharbor-worker"

# Gracefully stop existing worker (job processes continue in their own sessions)
if [[ -f "${GPUHARBOR_STORAGE_ROOT}/worker.pid" ]]; then
    OLD_PID=$(cat "${GPUHARBOR_STORAGE_ROOT}/worker.pid")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        info "Stopping existing worker (PID: ${OLD_PID})... running jobs will continue"
        kill "$OLD_PID"
        for i in $(seq 1 10); do
            kill -0 "$OLD_PID" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$OLD_PID" 2>/dev/null; then
            warn "Worker didn't stop gracefully, force killing..."
            kill -9 "$OLD_PID" 2>/dev/null || true
            sleep 1
        fi
    fi
else
    # Fallback for first install or missing PID file
    pkill -f "gpuharbor-worker" 2>/dev/null || true
    sleep 1
fi

# Start worker with nohup (works everywhere — systemd or not)
set -a; source "$ENV_FILE"; set +a
nohup "$WORKER_BIN" > "${GPUHARBOR_STORAGE_ROOT}/worker.log" 2>&1 &
WORKER_PID=$!
echo "$WORKER_PID" > "${GPUHARBOR_STORAGE_ROOT}/worker.pid"

# ── Verify worker is running and healthy ──────────────────────────────

info "Waiting for worker to start..."
HEALTH_URL="${PROTOCOL}://localhost:${GPUHARBOR_PORT}/health"
CURL_OPTS="-s --max-time 2"
if [[ "$PROTOCOL" == "https" ]]; then
    CURL_OPTS="$CURL_OPTS --insecure"
fi

HEALTHY=false
for i in 1 2 3 4 5; do
    sleep 1
    if ! kill -0 "$WORKER_PID" 2>/dev/null; then
        error "Worker process died (PID: ${WORKER_PID})"
        echo ""
        error "=== Worker log ==="
        tail -30 "${GPUHARBOR_STORAGE_ROOT}/worker.log" 2>/dev/null || true
        echo ""
        error "=== Environment ==="
        cat "$ENV_FILE"
        exit 1
    fi

    HEALTH_RESPONSE=$(curl $CURL_OPTS "$HEALTH_URL" 2>/dev/null || true)
    if echo "$HEALTH_RESPONSE" | grep -q '"status"' 2>/dev/null; then
        HEALTHY=true
        break
    fi
    debug "Attempt ${i}/5: waiting for ${HEALTH_URL} ..."
done

if ! $HEALTHY; then
    error "Worker started (PID: ${WORKER_PID}) but health check failed after 5 attempts"
    echo ""
    error "Health URL tested: ${HEALTH_URL}"
    error "=== Worker log ==="
    tail -30 "${GPUHARBOR_STORAGE_ROOT}/worker.log" 2>/dev/null || true
    echo ""
    error "=== Environment ==="
    cat "$ENV_FILE"
    echo ""
    error "=== Listening ports ==="
    ss -tlnp 2>/dev/null | grep "$WORKER_PID" || ss -tlnp 2>/dev/null | head -20
    exit 1
fi

success "Worker started and healthy (PID: ${WORKER_PID})"
debug "Health check response: ${HEALTH_RESPONSE}"

# ── Step 6: Start Cloudflare tunnel (if token provided) ──────────────

TUNNEL_URL=""
if [[ -n "${GPUHARBOR_TUNNEL_TOKEN:-}" ]]; then
    info "Step 6/6: Starting Cloudflare tunnel..."

    # Install cloudflared if not present
    if ! command -v cloudflared &>/dev/null; then
        info "Installing cloudflared..."
        ARCH=$(uname -m)
        case "$ARCH" in
            x86_64|amd64) CF_ARCH="amd64" ;;
            aarch64|arm64) CF_ARCH="arm64" ;;
            *) fatal "Unsupported architecture: $ARCH" ;;
        esac
        curl -sL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" -o /usr/local/bin/cloudflared
        chmod +x /usr/local/bin/cloudflared
        success "cloudflared installed"
    else
        success "cloudflared already installed"
    fi

    # Stop existing tunnel process
    if [[ -f "${GPUHARBOR_STORAGE_ROOT}/tunnel.pid" ]]; then
        OLD_TUNNEL_PID=$(cat "${GPUHARBOR_STORAGE_ROOT}/tunnel.pid")
        if kill -0 "$OLD_TUNNEL_PID" 2>/dev/null; then
            info "Stopping existing tunnel (PID: ${OLD_TUNNEL_PID})..."
            kill "$OLD_TUNNEL_PID" 2>/dev/null || true
            sleep 2
            kill -9 "$OLD_TUNNEL_PID" 2>/dev/null || true
        fi
    fi

    # Start tunnel
    nohup cloudflared tunnel run --token "$GPUHARBOR_TUNNEL_TOKEN" \
        > "${GPUHARBOR_STORAGE_ROOT}/tunnel.log" 2>&1 &
    TUNNEL_PID=$!
    echo "$TUNNEL_PID" > "${GPUHARBOR_STORAGE_ROOT}/tunnel.pid"

    # Wait briefly and verify cloudflared is still running
    sleep 3
    if kill -0 "$TUNNEL_PID" 2>/dev/null; then
        success "Cloudflare tunnel started (PID: ${TUNNEL_PID})"
        # Extract the hostname from the tunnel connector log
        TUNNEL_URL=$(grep -oP 'https://[a-zA-Z0-9._-]+\.[a-zA-Z]+' "${GPUHARBOR_STORAGE_ROOT}/tunnel.log" 2>/dev/null | head -1 || true)
        if [[ -z "$TUNNEL_URL" ]]; then
            # Tunnel is running but hostname not yet in logs — that's fine
            info "Tunnel is running. Check 'tunnel.log' or Cloudflare dashboard for the hostname."
        fi
    else
        error "Cloudflare tunnel failed to start. Check ${GPUHARBOR_STORAGE_ROOT}/tunnel.log"
        tail -10 "${GPUHARBOR_STORAGE_ROOT}/tunnel.log" 2>/dev/null || true
    fi
else
    info "No GPUHARBOR_TUNNEL_TOKEN set — skipping Cloudflare tunnel."
    info "Set GPUHARBOR_TUNNEL_TOKEN in .env for a persistent tunnel, or start a quick tunnel manually."
fi

# Write restart helper
cat > "${GPUHARBOR_STORAGE_ROOT}/restart.sh" << 'RESTARTEOF'
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/worker.pid"
TUNNEL_PID_FILE="$SCRIPT_DIR/tunnel.pid"

# Gracefully stop worker (job processes continue in their own sessions)
if [[ -f "$PID_FILE" ]]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping worker (PID: $OLD_PID)... running jobs will continue"
        kill "$OLD_PID"
        for i in $(seq 1 10); do
            kill -0 "$OLD_PID" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "Force killing worker..."
            kill -9 "$OLD_PID" 2>/dev/null || true
            sleep 1
        fi
    fi
else
    pkill -f "gpuharbor-worker" 2>/dev/null || true
    sleep 1
fi

set -a; source "$SCRIPT_DIR/worker.env"; set +a

nohup "/workspace/gpuharbor_venv/bin/gpuharbor-worker" > "$SCRIPT_DIR/worker.log" 2>&1 &
echo $! > "$PID_FILE"
echo "Worker restarted (PID: $!). Running jobs preserved."

# Restart Cloudflare tunnel if token is configured
if [[ -n "${GPUHARBOR_TUNNEL_TOKEN:-}" ]]; then
    if [[ -f "$TUNNEL_PID_FILE" ]]; then
        OLD_TUNNEL=$(cat "$TUNNEL_PID_FILE")
        kill "$OLD_TUNNEL" 2>/dev/null || true
        sleep 2
    fi
    nohup cloudflared tunnel run --token "$GPUHARBOR_TUNNEL_TOKEN" > "$SCRIPT_DIR/tunnel.log" 2>&1 &
    echo $! > "$TUNNEL_PID_FILE"
    echo "Tunnel restarted (PID: $!)."
fi
RESTARTEOF
chmod +x "${GPUHARBOR_STORAGE_ROOT}/restart.sh"

# ── Print connection info ──────────────────────────────────────────────

DIRECT_URL="${PROTOCOL}://${PUBLIC_IP}:${GPUHARBOR_EXTERNAL_PORT}"
LOCAL_URL="${PROTOCOL}://localhost:${GPUHARBOR_PORT}"
DISK_FREE=$(df -BG /workspace 2>/dev/null | awk 'NR==2 {print $4}' | tr -d 'G' || df -BG / | awk 'NR==2 {print $4}' | tr -d 'G')
GPU_FIRST_NAME=$(echo "$GPU_INFO" | head -1 | cut -d, -f1 | xargs)

echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "${GREEN}  GPUHarbor worker ready!${NC}"
echo -e "${BOLD}============================================${NC}"
echo ""
echo -e "  ${BOLD}Worker${NC}"
echo -e "  Listening:  ${BOLD}${LOCAL_URL}${NC}  (bind port ${GPUHARBOR_PORT})"
echo -e "  Direct URL: ${BOLD}${DIRECT_URL}${NC}  (external port ${GPUHARBOR_EXTERNAL_PORT})"
echo -e "  Token:      ${BOLD}${AUTH_TOKEN}${NC}"
echo ""
echo -e "  ${BOLD}Hardware${NC}"
echo -e "  GPU:   ${GPU_COUNT}x ${GPU_FIRST_NAME}"
echo -e "  CUDA:  ${CUDA_VERSION}"
echo -e "  Disk:  ${DISK_FREE}GB free"
echo -e "  Store: ${GPUHARBOR_STORAGE_ROOT}"
echo ""
if [[ -n "${GPUHARBOR_TUNNEL_TOKEN:-}" ]]; then
    echo -e "${BOLD}──── Tunnel ────${NC}"
    echo ""
    if [[ -n "$TUNNEL_URL" ]]; then
        echo -e "  Tunnel URL: ${BOLD}${TUNNEL_URL}${NC}"
    else
        echo -e "  Tunnel:     ${BOLD}running${NC} (check Cloudflare dashboard for hostname)"
    fi
    echo -e "  Tunnel log: ${CYAN}tail -f ${GPUHARBOR_STORAGE_ROOT}/tunnel.log${NC}"
    echo ""
fi

# When called from setup.sh (GPUHARBOR_QUIET_HINTS=1), skip Next Steps —
# setup.sh prints its own with the complete gpuharbor servers add command.
if [[ -z "${GPUHARBOR_QUIET_HINTS:-}" ]]; then
    if [[ -n "${GPUHARBOR_TUNNEL_TOKEN:-}" ]]; then
        echo -e "${BOLD}──── Next Steps ────${NC}"
        echo ""
        echo -e "  Add the server to ${CYAN}~/.gpuharbor/servers.yaml${NC} on your laptop (if not already):"
        echo ""
        echo -e "     ${CYAN}gpuharbor servers add ${HOSTNAME_LABEL} \\${NC}"
        echo -e "     ${CYAN}    --url https://<your-tunnel-hostname> \\${NC}"
        echo -e "     ${CYAN}    --token ${AUTH_TOKEN}${NC}"
        echo ""
        echo -e "  Test from your laptop:"
        echo ""
        echo -e "     ${CYAN}gpuharbor servers${NC}"
    else
        echo -e "${BOLD}──── Next Steps ────${NC}"
        echo ""
        echo -e "  ${BOLD}Option A: Named tunnel (recommended — permanent URL)${NC}"
        echo ""
        echo -e "  On your laptop, create a tunnel with:"
        echo -e "     ${CYAN}./setup-tunnel.sh ${HOSTNAME_LABEL} gpuharbor.xyz${NC}"
        echo ""
        echo -e "  Then set GPUHARBOR_TUNNEL_TOKEN in .env and re-run install.sh."
        echo ""
        echo -e "  ${BOLD}Option B: Quick tunnel (temporary — expires ~24h)${NC}"
        echo ""
        echo -e "     ${CYAN}cloudflared tunnel --url ${LOCAL_URL}${NC}"
        echo ""
        echo -e "  Then add to ${CYAN}~/.gpuharbor/servers.yaml${NC} on your laptop:"
        echo ""
        echo -e "     ${CYAN}gpuharbor servers add ${HOSTNAME_LABEL} \\${NC}"
        echo -e "     ${CYAN}    --url https://<tunnel-url>.trycloudflare.com \\${NC}"
        echo -e "     ${CYAN}    --token ${AUTH_TOKEN}${NC}"
    fi
fi
echo ""
echo -e "${BOLD}──── Management ────${NC}"
echo ""
echo -e "  Logs:    ${CYAN}tail -f ${GPUHARBOR_STORAGE_ROOT}/worker.log${NC}"
echo -e "  Restart: ${CYAN}bash ${GPUHARBOR_STORAGE_ROOT}/restart.sh${NC}"
echo -e "  Health:  ${CYAN}curl ${LOCAL_URL}/health${NC}"
echo ""
