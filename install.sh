#!/usr/bin/env bash
# GPUHarbor Worker Bootstrap Script
#
# Turns a fresh GPU instance (Vast.ai, Runpod, bare metal) into a ready
# GPUHarbor worker.  Designed to be idempotent and fast on re-run.
#
# All artifacts are stored locally on disk under /workspace/gpuharbor/.
# No S3 or external storage required.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/yourorg/gpuharbor/main/install.sh | bash
#   or:
#   ./install.sh
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

auto_detect_port() {
    # On Vast.ai, exposed ports are in VAST_TCP_PORT_* env vars
    # Try common ports in priority order
    local preferred_ports=(8443 5000 8080 8000 6006 1111)

    for port in "${preferred_ports[@]}"; do
        local var="VAST_TCP_PORT_${port}"
        if [[ -n "${!var:-}" ]]; then
            echo "$port"
            return
        fi
    done

    # If no Vast.ai port mapping found, check if any VAST_TCP_PORT_ exists
    for var in $(compgen -v VAST_TCP_PORT_ 2>/dev/null || true); do
        local port="${var#VAST_TCP_PORT_}"
        if [[ "$port" =~ ^[0-9]+$ ]]; then
            echo "$port"
            return
        fi
    done

    # Default
    echo "8443"
}

GPUHARBOR_PORT="${GPUHARBOR_PORT:-$(auto_detect_port)}"

# ── Pre-flight checks ──────────────────────────────────────────────────

info "Starting GPUHarbor worker installation..."
echo ""

# Check disk space
DISK_FREE_KB=$(df /workspace 2>/dev/null | awk 'NR==2 {print $4}' || df / | awk 'NR==2 {print $4}')
DISK_FREE_GB=$(( DISK_FREE_KB / 1024 / 1024 ))
if [[ "$DISK_FREE_GB" -lt 5 ]]; then
    warn "Only ${DISK_FREE_GB}GB free disk space. This may not be enough."
    read -p "Continue anyway? [y/N] " -n 1 -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]] || exit 1
fi

# Must be root or have sudo
if [[ $EUID -ne 0 ]]; then
    if command -v sudo &>/dev/null; then
        SUDO="sudo"
    else
        # On Vast.ai we're usually root; if not, try without sudo
        SUDO=""
    fi
else
    SUDO=""
fi

# ── Step 1: Verify GPU ─────────────────────────────────────────────────

info "Step 1/6: Verifying GPU and CUDA..."

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

# ── Step 2: Install Docker ─────────────────────────────────────────────

info "Step 2/6: Checking Docker..."

if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    DOCKER_VERSION=$(docker --version | head -1)
    success "Docker already installed: ${DOCKER_VERSION}"
else
    info "Installing Docker..."

    if [[ -f /etc/os-release ]]; then
        source /etc/os-release
        DISTRO="${ID}"
    else
        DISTRO="unknown"
    fi

    case "$DISTRO" in
        ubuntu|debian)
            $SUDO apt-get update -qq
            $SUDO apt-get install -y -qq ca-certificates curl gnupg lsb-release
            $SUDO install -m 0755 -d /etc/apt/keyrings
            curl -fsSL "https://download.docker.com/linux/${DISTRO}/gpg" | $SUDO gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
            $SUDO chmod a+r /etc/apt/keyrings/docker.gpg
            echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${DISTRO} $(lsb_release -cs) stable" | $SUDO tee /etc/apt/sources.list.d/docker.list > /dev/null
            $SUDO apt-get update -qq
            $SUDO apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin
            ;;
        centos|rhel|fedora|amzn)
            $SUDO yum install -y yum-utils
            $SUDO yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
            $SUDO yum install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
            ;;
        *)
            fatal "Unsupported distro '${DISTRO}'. Install Docker manually and re-run."
            ;;
    esac

    $SUDO systemctl enable docker
    $SUDO systemctl start docker
    success "Docker installed and started"
fi

# Install NVIDIA Container Toolkit if needed
if ! docker info 2>/dev/null | grep -qi "nvidia"; then
    info "Installing NVIDIA Container Toolkit..."

    if [[ -f /etc/os-release ]]; then source /etc/os-release; DISTRO="${ID}"; fi

    case "$DISTRO" in
        ubuntu|debian)
            curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | $SUDO gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg 2>/dev/null || true
            DIST=$(. /etc/os-release; echo "${ID}${VERSION_ID}")
            curl -s -L "https://nvidia.github.io/libnvidia-container/${DIST}/libnvidia-container.list" | \
                sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
                $SUDO tee /etc/apt/sources.list.d/nvidia-container-toolkit.list > /dev/null
            $SUDO apt-get update -qq
            $SUDO apt-get install -y -qq nvidia-container-toolkit
            ;;
        centos|rhel|fedora|amzn)
            curl -s -L https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo | \
                $SUDO tee /etc/yum.repos.d/nvidia-container-toolkit.repo > /dev/null
            $SUDO yum install -y nvidia-container-toolkit
            ;;
        *)
            warn "Could not auto-install NVIDIA Container Toolkit."
            ;;
    esac

    $SUDO nvidia-ctk runtime configure --runtime=docker 2>/dev/null || true
    $SUDO systemctl restart docker 2>/dev/null || true
    success "NVIDIA Container Toolkit installed"
fi

# Quick GPU passthrough test
if docker run --rm --gpus all nvidia/cuda:12.0.0-base-ubuntu22.04 nvidia-smi &>/dev/null 2>&1; then
    success "Docker GPU passthrough verified"
else
    warn "Docker GPU passthrough test failed. Jobs may not have GPU access."
fi

# ── Step 3: Install Python and GPUHarbor worker ────────────────────────

info "Step 3/6: Installing GPUHarbor worker..."

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
    if [[ -f /etc/os-release ]]; then source /etc/os-release; DISTRO="${ID}"; fi
    case "$DISTRO" in
        ubuntu|debian)
            $SUDO apt-get install -y -qq python3 python3-pip python3-venv
            ;;
        centos|rhel|fedora|amzn)
            $SUDO yum install -y python3 python3-pip
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
    "$VENV_PIP" install -q gpuharbor
fi

success "GPUHarbor worker installed"

# ── Step 4: Generate auth token ────────────────────────────────────────

info "Step 4/6: Setting up authentication..."

$SUDO mkdir -p "${GPUHARBOR_STORAGE_ROOT}"
TOKEN_FILE="${GPUHARBOR_STORAGE_ROOT}/auth_token"

if [[ -f "$TOKEN_FILE" ]]; then
    AUTH_TOKEN=$(cat "$TOKEN_FILE")
    success "Using existing auth token"
else
    AUTH_TOKEN=$("${VENV_DIR}/bin/python" -c "from gpuharbor.common.auth import generate_token; print(generate_token())")
    echo "$AUTH_TOKEN" | tee "$TOKEN_FILE" > /dev/null
    chmod 600 "$TOKEN_FILE"
    success "Generated new auth token"
fi

# ── Step 5: TLS setup ──────────────────────────────────────────────────

info "Step 5/6: Configuring TLS..."

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

# ── Step 6: Create and start systemd service ───────────────────────────

info "Step 6/6: Setting up systemd service..."

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

# Check if systemd is available (not always on Vast.ai containers)
if command -v systemctl &>/dev/null && systemctl --version &>/dev/null 2>&1; then
    $SUDO tee /etc/systemd/system/gpuharbor-worker.service > /dev/null << UNITEOF
[Unit]
Description=GPUHarbor Worker Agent
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
EnvironmentFile=${ENV_FILE}
ExecStart=${WORKER_BIN}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=gpuharbor-worker

[Install]
WantedBy=multi-user.target
UNITEOF

    $SUDO systemctl daemon-reload
    $SUDO systemctl enable gpuharbor-worker
    $SUDO systemctl restart gpuharbor-worker

    sleep 2
    if $SUDO systemctl is-active --quiet gpuharbor-worker; then
        success "GPUHarbor worker service started (systemd)"
    else
        warn "Service may have failed to start. Check: journalctl -u gpuharbor-worker -n 50"
    fi
else
    # No systemd (common in containers) — start directly with nohup
    info "No systemd detected. Starting worker directly..."

    # Kill any existing worker
    pkill -f "gpuharbor-worker" 2>/dev/null || true
    sleep 1

    # Source env and start
    set -a; source "$ENV_FILE"; set +a
    nohup "$WORKER_BIN" > "${GPUHARBOR_STORAGE_ROOT}/worker.log" 2>&1 &
    WORKER_PID=$!
    echo "$WORKER_PID" > "${GPUHARBOR_STORAGE_ROOT}/worker.pid"

    sleep 2
    if kill -0 "$WORKER_PID" 2>/dev/null; then
        success "GPUHarbor worker started (PID: ${WORKER_PID})"
    else
        warn "Worker may have failed to start. Check: ${GPUHARBOR_STORAGE_ROOT}/worker.log"
    fi

    # Write a restart helper script
    cat > "${GPUHARBOR_STORAGE_ROOT}/restart.sh" << 'RESTARTEOF'
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pkill -f "gpuharbor-worker" 2>/dev/null || true
sleep 1
set -a; source "$SCRIPT_DIR/worker.env"; set +a
nohup "$SCRIPT_DIR/../gpuharbor_venv/bin/gpuharbor-worker" > "$SCRIPT_DIR/worker.log" 2>&1 &
echo $! > "$SCRIPT_DIR/worker.pid"
echo "Worker restarted (PID: $!)"
RESTARTEOF
    chmod +x "${GPUHARBOR_STORAGE_ROOT}/restart.sh"
fi

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
if command -v systemctl &>/dev/null && systemctl --version &>/dev/null 2>&1; then
    echo -e "  ${CYAN}systemctl status gpuharbor-worker${NC}"
    echo -e "  ${CYAN}journalctl -u gpuharbor-worker -f${NC}"
else
    echo -e "  ${CYAN}tail -f ${GPUHARBOR_STORAGE_ROOT}/worker.log${NC}"
    echo -e "  ${CYAN}bash ${GPUHARBOR_STORAGE_ROOT}/restart.sh${NC}"
fi
echo ""
