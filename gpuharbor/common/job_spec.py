"""Job specification schema and artifact models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, field_validator


def _generate_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:8]}"


class ResourceRequirements(BaseModel):
    """Hardware requirements the worker validates before accepting a job."""

    gpu_count: int = Field(default=1, ge=1, description="Number of GPUs required")
    disk_gb_min: int = Field(default=0, ge=0, description="Minimum free disk space in GB")


class CheckpointingConfig(BaseModel):
    """Controls automatic checkpoint saving during training."""

    enabled: bool = False
    save_every_minutes: int = Field(default=10, ge=1)
    keep_last_n: int = Field(default=3, ge=1)


class ArtifactPaths(BaseModel):
    """References to input artifacts for a job.

    These are filenames relative to the worker's storage.  The CLI uploads
    files to the worker first, then references them here by name.
    """

    input_checkpoint: Optional[str] = Field(
        default=None, description="Filename of uploaded checkpoint (in worker uploads/ or job input/)"
    )
    dataset: Optional[str] = Field(
        default=None, description="Path to dataset directory on worker"
    )


class JobSpec(BaseModel):
    """Immutable job specification submitted by the CLI."""

    name: str = Field(..., min_length=1, max_length=256)
    project: str = Field(default="default", min_length=1, max_length=256)

    command: list[str] = Field(..., min_length=1)
    env: dict[str, str] = Field(default_factory=dict)

    resources: ResourceRequirements = Field(default_factory=ResourceRequirements)
    artifacts: ArtifactPaths = Field(default_factory=ArtifactPaths)
    checkpointing: CheckpointingConfig = Field(default_factory=CheckpointingConfig)

    on_failure: str = Field(default="manual", pattern=r"^(manual|auto_retry)$")
    max_retries: int = Field(default=3, ge=0)

    # Source reference for reproducibility
    source_git_repo: Optional[str] = None
    source_git_commit: Optional[str] = None

    @field_validator("command")
    @classmethod
    def command_not_empty_strings(cls, v: list[str]) -> list[str]:
        if not v or not v[0].strip():
            raise ValueError("command must contain at least one non-empty string")
        return v


class ArtifactRecord(BaseModel):
    """A single artifact produced or consumed by a job."""

    artifact_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    job_id: str
    type: str = Field(
        ...,
        description="Artifact type: input_checkpoint, checkpoint, final_model, "
        "tokenizer, training_log, metrics, manifest",
    )
    path: str = Field(..., description="Path relative to job directory")
    sha256: Optional[str] = None
    size: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ArtifactManifest(BaseModel):
    """Complete artifact manifest for a job."""

    job_id: str
    server: str
    artifacts: list[ArtifactRecord] = Field(default_factory=list)


class JobRecord(BaseModel):
    """Full job record stored by the worker, combining spec + runtime state."""

    job_id: str = Field(default_factory=_generate_job_id)
    spec: JobSpec
    state: str = "created"
    server_name: str = ""
    container_id: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    metrics: Optional[dict] = None


class JobMetrics(BaseModel):
    """Structured training metrics reported by the training script."""

    job_id: str
    step: Optional[int] = None
    epoch: Optional[float] = None
    loss: Optional[float] = None
    samples_per_sec: Optional[float] = None
    gpu_util: Optional[int] = None
    gpu_mem_gb: Optional[float] = None
    checkpoint_at: Optional[datetime] = None
