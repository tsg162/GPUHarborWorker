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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
if [[ "${GPUHARBOR_SKIP_DOTENV:-0}" != "1" && -f "${SCRIPT_DIR}/.env" ]]; then
    info "Loading configuration from ${SCRIPT_DIR}/.env"
    set -a; source "${SCRIPT_DIR}/.env"; set +a
elif [[ "${GPUHARBOR_SKIP_DOTENV:-0}" != "1" && -f ".env" ]]; then
    info "Loading configuration from .env"
    set -a; source ".env"; set +a
fi

# ── Configuration defaults ─────────────────────────────────────────────

GPUHARBOR_TLS="${GPUHARBOR_TLS:-none}"
GPUHARBOR_STORAGE_ROOT="${GPUHARBOR_STORAGE_ROOT:-/workspace/gpuharbor}"
GPUHARBOR_LOG_LEVEL="${GPUHARBOR_LOG_LEVEL:-info}"
if [[ -n "${GPUHARBOR_TUNNEL_TOKEN:-}" ]]; then
    GPUHARBOR_HOST="${GPUHARBOR_HOST:-127.0.0.1}"
else
    GPUHARBOR_HOST="${GPUHARBOR_HOST:-0.0.0.0}"
fi

PYTHON_MIN_VERSION="3.10"
PREVIOUS_WORKER_ENV="${GPUHARBOR_STORAGE_ROOT}/worker.env"

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

valid_port() {
    [[ "${1:-}" =~ ^[0-9]+$ ]] && [[ "$1" -ge 1 ]] && [[ "$1" -le 65535 ]]
}

valid_listener_host() {
    local host="${1:-}"
    [[ -n "$host" ]] \
        && [[ "${#host}" -le 253 ]] \
        && [[ "$host" =~ ^[A-Za-z0-9_.:%-]+$ ]]
}

vastai_is_detected() {
    compgen -v VAST_TCP_PORT_ 2>/dev/null | grep -q .
}

vast_external_port() {
    local internal="$1"
    local mapping_var="VAST_TCP_PORT_${internal}"
    local external="${!mapping_var:-}"
    if valid_port "$external"; then
        echo "$external"
        return 0
    fi
    return 1
}

read_worker_env_value() {
    local key="$1"
    [[ -r "$PREVIOUS_WORKER_ENV" ]] || return 1
    sed -n "s/^${key}=//p" "$PREVIOUS_WORKER_ENV" | tail -1
}

render_worker_env() {
    local env_file="$1"
    local hostname_label="$2"
    local auth_token="$3"
    local vast_instance_id="$4"
    local tls_cert_path="${5:-}"
    local tls_key_path="${6:-}"

    mkdir -p "$(dirname "$env_file")"
    {
        printf 'GPUHARBOR_SERVER_NAME=%q\n' "$hostname_label"
        printf 'GPUHARBOR_AUTH_TOKEN=%q\n' "$auth_token"
        printf 'GPUHARBOR_HOST=%q\n' "$GPUHARBOR_HOST"
        printf 'GPUHARBOR_PORT=%q\n' "$GPUHARBOR_PORT"
        printf 'GPUHARBOR_EXTERNAL_PORT=%q\n' "$GPUHARBOR_EXTERNAL_PORT"
        printf 'GPUHARBOR_DB_PATH=%s/jobs.db\n' "$GPUHARBOR_STORAGE_ROOT"
        printf 'GPUHARBOR_STORAGE_ROOT=%q\n' "$GPUHARBOR_STORAGE_ROOT"
        printf 'GPUHARBOR_LOG_LEVEL=%q\n' "$GPUHARBOR_LOG_LEVEL"
        printf 'GPUHARBOR_VAST_INSTANCE_ID=%q\n' "$vast_instance_id"
        local key
        for key in GPUHARBOR_CACHE_ROOT GPUHARBOR_BACKUP_DEST GPUHARBOR_TRAINING_PYTHON GPUHARBOR_WORKER_URL GPUHARBOR_WORKER_REF GPUHARBOR_WORKER_REPO GPUHARBOR_WORKER_VENV RCLONE_CONFIG; do
            if [[ -n "${!key:-}" ]]; then printf '%s=%q\n' "$key" "${!key}"; fi
        done
        if [[ -n "$tls_cert_path" ]]; then
            printf 'GPUHARBOR_TLS_CERT=%q\n' "$tls_cert_path"
            printf 'GPUHARBOR_TLS_KEY=%q\n' "$tls_key_path"
        fi
        if [[ -n "${GPUHARBOR_TUNNEL_TOKEN:-}" ]]; then
            printf 'GPUHARBOR_TUNNEL_TOKEN=%q\n' "$GPUHARBOR_TUNNEL_TOKEN"
        fi
    } > "$env_file"
    chmod 600 "$env_file"
}

valid_listener_host "$GPUHARBOR_HOST" \
    || fatal "Invalid GPUHARBOR_HOST: ${GPUHARBOR_HOST}"

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
            elif ! valid_port "${!var}"; then
                debug "External port ${!var} is invalid, skipping"
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
        if valid_port "$internal" \
            && valid_port "${!var}" \
            && [[ "$internal" -ne 22 ]] \
            && port_is_free "$internal"; then
            GPUHARBOR_PORT="$internal"
            GPUHARBOR_EXTERNAL_PORT="${!var}"
            info "Selected port ${internal} (internal) -> ${!var} (external)"
            return
        fi
    done

    if $vastai_detected; then
        fatal "No free, valid Vast.ai TCP mapping is available for the worker"
    fi

    # Non-Vast hosts use the same internal and external port.
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

# Cloudflare connects to a fixed local origin; no Vast TCP mapping is needed.
if [[ -n "${GPUHARBOR_TUNNEL_TOKEN:-}" ]]; then
    GPUHARBOR_PORT="${GPUHARBOR_PORT:-5000}"
    GPUHARBOR_EXTERNAL_PORT="$GPUHARBOR_PORT"
    valid_port "$GPUHARBOR_PORT" || fatal "Invalid GPUHARBOR_PORT"
else
# If no explicit port was supplied, reuse the last installed endpoint without
# probing it for availability: the verified incumbent worker is expected to
# still be listening there until the controlled restart later in this script.
if [[ -z "${GPUHARBOR_PORT:-}" && -r "$PREVIOUS_WORKER_ENV" ]]; then
    PRIOR_PORT=$(read_worker_env_value GPUHARBOR_PORT || true)
    PRIOR_EXTERNAL_PORT=$(read_worker_env_value GPUHARBOR_EXTERNAL_PORT || true)
    if valid_port "$PRIOR_PORT"; then
        if vastai_is_detected; then
            if MAPPED_EXTERNAL=$(vast_external_port "$PRIOR_PORT"); then
                GPUHARBOR_PORT="$PRIOR_PORT"
                GPUHARBOR_EXTERNAL_PORT="$MAPPED_EXTERNAL"
                info "Reusing established Vast.ai endpoint: ${GPUHARBOR_PORT} -> ${GPUHARBOR_EXTERNAL_PORT}"
            else
                fatal "Established port ${PRIOR_PORT} no longer has a Vast.ai TCP mapping"
            fi
        elif [[ -z "$PRIOR_EXTERNAL_PORT" ]] || valid_port "$PRIOR_EXTERNAL_PORT"; then
            GPUHARBOR_PORT="$PRIOR_PORT"
            GPUHARBOR_EXTERNAL_PORT="${PRIOR_EXTERNAL_PORT:-$PRIOR_PORT}"
            info "Reusing established endpoint: ${GPUHARBOR_PORT}"
        else
            warn "Ignoring invalid prior external port: ${PRIOR_EXTERNAL_PORT}"
        fi
    else
        warn "Ignoring invalid prior bind port: ${PRIOR_PORT:-missing}"
    fi
fi

if [[ -z "${GPUHARBOR_PORT:-}" ]]; then
    detect_ports
else
    valid_port "$GPUHARBOR_PORT" || fatal "Invalid GPUHARBOR_PORT: ${GPUHARBOR_PORT}"
    if vastai_is_detected; then
        MAPPED_EXTERNAL=$(vast_external_port "$GPUHARBOR_PORT") \
            || fatal "Port ${GPUHARBOR_PORT} has no valid Vast.ai TCP mapping"
        GPUHARBOR_EXTERNAL_PORT="$MAPPED_EXTERNAL"
    else
        GPUHARBOR_EXTERNAL_PORT="${GPUHARBOR_EXTERNAL_PORT:-$GPUHARBOR_PORT}"
        valid_port "$GPUHARBOR_EXTERNAL_PORT" \
            || fatal "Invalid GPUHARBOR_EXTERNAL_PORT: ${GPUHARBOR_EXTERNAL_PORT}"
    fi
    info "Using configured port: ${GPUHARBOR_PORT} (external: ${GPUHARBOR_EXTERNAL_PORT})"
fi

fi

if [[ "${GPUHARBOR_RENDER_ENV_ONLY:-0}" == "1" ]]; then
    RENDERED_ENV="${GPUHARBOR_STORAGE_ROOT}/worker.env"
    render_worker_env \
        "$RENDERED_ENV" \
        "${GPUHARBOR_SERVER_NAME:-gpuharbor-worker}" \
        "${GPUHARBOR_AUTH_TOKEN:-ghb_tok_installer_test}" \
        "${CONTAINER_ID:-}" \
        "" \
        ""
    echo "GPUHARBOR_WORKER_ENV=${RENDERED_ENV}"
    exit 0
elif [[ "${GPUHARBOR_PORT_SELECTION_ONLY:-0}" == "1" ]]; then
    echo "GPUHARBOR_PORT=${GPUHARBOR_PORT}"
    echo "GPUHARBOR_EXTERNAL_PORT=${GPUHARBOR_EXTERNAL_PORT}"
    exit 0
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
    fatal "At least 5 GiB free disk is required for installation"
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
VENV_DIR="${GPUHARBOR_WORKER_VENV:-/workspace/gpuharbor_venv}"
if [[ ! -d "$VENV_DIR" ]]; then
    $PYTHON -m venv "$VENV_DIR"
fi

VENV_PIP="${VENV_DIR}/bin/pip"

if [[ "${GPUHARBOR_PREINSTALLED:-0}" == "1" ]]; then
    "${VENV_DIR}/bin/python" -c 'import gpuharbor, supervisor; assert gpuharbor.__version__ == "0.2.0"'
    info "Using worker baked into the image"
elif [[ -d "${SCRIPT_DIR}/gpuharbor" ]]; then
    info "Installing from local source..."
    "$VENV_PIP" install -q --require-hashes -r "${SCRIPT_DIR}/requirements.lock"
    "$VENV_PIP" install -q --no-deps "${SCRIPT_DIR}"
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
    if [[ -n "${AUTH_TOKEN//[[:space:]]/}" ]]; then
        success "Using existing auth token"
    else
        AUTH_TOKEN=$("${VENV_DIR}/bin/python" -c "from gpuharbor.common.auth import generate_token; print(generate_token())")
        echo "$AUTH_TOKEN" > "$TOKEN_FILE"
        chmod 600 "$TOKEN_FILE"
        success "Replaced empty auth token"
    fi
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
HOSTNAME_LABEL="${GPUHARBOR_SERVER_NAME:-$(hostname -s 2>/dev/null || echo gpuharbor-worker)}"
GPUHARBOR_WORKER_REPO="$SCRIPT_DIR"
GPUHARBOR_WORKER_VENV="$VENV_DIR"
GPUHARBOR_CACHE_ROOT="${GPUHARBOR_CACHE_ROOT:-/workspace/gpuharbor-cache}"

PROTOCOL="http"
if [[ "$GPUHARBOR_TLS" != "none" ]]; then
    PROTOCOL="https"
fi

# Write environment file
ENV_FILE="${GPUHARBOR_STORAGE_ROOT}/worker.env"
render_worker_env \
    "$ENV_FILE" \
    "$HOSTNAME_LABEL" \
    "$AUTH_TOKEN" \
    "$VAST_INSTANCE_ID" \
    "$TLS_CERT_PATH" \
    "$TLS_KEY_PATH"

WORKER_BIN="${VENV_DIR}/bin/gpuharbor-worker"

# Install a pinned, verified cloudflared binary for tunnel deployments.
if [[ -n "${GPUHARBOR_TUNNEL_TOKEN:-}" ]]; then
    CF_VERSION="2026.9.3"
    case "$(uname -m)" in
        x86_64|amd64) CF_ARCH=amd64; CF_SHA=77e26d8d900e0b8469f416239d14b5f296525fdf79fee6f511ef55609e3fbac2 ;;
        aarch64|arm64) CF_ARCH=arm64; CF_SHA=aaeb2d7d0da3614634c7e03ab13487a1522c2e79165ed2929cfe23d5e95b326d ;;
        *) fatal "Unsupported architecture" ;;
    esac
    if ! command -v cloudflared >/dev/null || ! cloudflared --version | grep -q "$CF_VERSION"; then
        CF_TEMP=$(mktemp)
        trap 'rm -f "$CF_TEMP"' EXIT
        curl -fsSL --retry 3 "https://github.com/cloudflare/cloudflared/releases/download/${CF_VERSION}/cloudflared-linux-${CF_ARCH}" -o "$CF_TEMP"
        printf '%s  %s\n' "$CF_SHA" "$CF_TEMP" | sha256sum -c -
        install -m 755 "$CF_TEMP" /usr/local/bin/cloudflared
        rm -f "$CF_TEMP"
        trap - EXIT
    fi
fi

# All entry points share the same Supervisor lifecycle. Worker restarts do
# not signal training process groups. Container reboot runs vast/onstart.sh.
RESTART="${GPUHARBOR_STORAGE_ROOT}/restart.sh"
{
    printf '#!/usr/bin/env bash\nset -euo pipefail\n'
    printf 'exec %q -m gpuharbor.worker.service restart --root %q "$@"\n' "${VENV_DIR}/bin/python" "$GPUHARBOR_STORAGE_ROOT"
} > "$RESTART"
chmod 700 "$RESTART"

if [[ "${GPUHARBOR_INSTALL_ONLY:-0}" != "1" ]]; then
    "${VENV_DIR}/bin/python" -m gpuharbor.worker.service restart --root "$GPUHARBOR_STORAGE_ROOT"
    success "Worker and tunnel supervised; authenticated endpoints verified"
else
    success "Worker installed; startup deferred"
fi
info "Restart: ${RESTART}"
info "Worker log: ${GPUHARBOR_STORAGE_ROOT}/worker.log"
info "Preflight from your laptop: gpuharbor doctor --server ${HOSTNAME_LABEL}"
