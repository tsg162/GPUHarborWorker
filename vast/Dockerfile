# Verified Vast PyTorch multi-version base, 2026-09-08. Override deliberately
# when upgrading CUDA/PyTorch, then rerun gpuharbor doctor on the target GPU.
ARG BASE_IMAGE=vastai/pytorch:multi-210-291-271-py312-2026-09-08@sha256:5db899a229d14a525c45a8f2aa106aa206138475af26191b68ac1e98f909e7fa
FROM ${BASE_IMAGE}
USER root
WORKDIR /opt/GPUHarborWorker
COPY requirements.lock pyproject.toml README.md ./
COPY gpuharbor ./gpuharbor
COPY install.sh restart-server.sh ./
COPY vast ./vast
RUN python3 -m venv /opt/gpuharbor_venv \
    && /opt/gpuharbor_venv/bin/pip install --require-hashes -r requirements.lock \
    && /opt/gpuharbor_venv/bin/pip install --no-deps . \
    && bash vast/install-cloudflared.sh
ENV GPUHARBOR_WORKER_VENV=/opt/gpuharbor_venv \
    GPUHARBOR_PREINSTALLED=1 \
    GPUHARBOR_TRAINING_PYTHON=/venv/main/bin/python \
    GPUHARBOR_STORAGE_ROOT=/workspace/gpuharbor \
    GPUHARBOR_CACHE_ROOT=/workspace/gpuharbor-cache \
    GPUHARBOR_PORT=5000 \
    GPUHARBOR_HOST=127.0.0.1
# Preserve Vast's entrypoint. Set template onstart to vast/onstart.sh.
