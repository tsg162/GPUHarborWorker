"""Job specification schema and artifact models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator
from pathlib import PurePosixPath
import re


def validate_filename(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", value):
        raise ValueError("Expected a portable upload filename")
    return value


def relative_path(value: str) -> str:
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or PurePosixPath(value).is_absolute()
        or ".." in value.split("/")
    ):
        raise ValueError("Expected a relative path within the project")
    return value


def _generate_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:8]}"


class ResourceRequirements(BaseModel):
    """Hardware requirements the worker validates before accepting a job."""

    gpu_count: int = Field(default=1, ge=1, description="Number of GPUs required")
    disk_gb_min: int = Field(
        default=0, ge=0, description="Minimum free disk space in GB"
    )


class CheckpointingConfig(BaseModel):
    """Controls discovery and retention of trainer-written checkpoint sets."""

    enabled: bool = False
    save_every_minutes: int = Field(default=10, ge=1)
    keep_last_n: int = Field(default=3, ge=1)


class ArtifactPaths(BaseModel):
    """References to input artifacts for a job.

    These are filenames relative to the worker's storage.  The CLI uploads
    files to the worker first, then references them here by name.
    """

    input_checkpoint: Optional[str] = Field(
        default=None,
        description="Filename of uploaded checkpoint (in worker uploads/ or job input/)",
    )
    dataset: Optional[str] = Field(
        default=None, description="Path to dataset directory on worker"
    )

    @field_validator("input_checkpoint")
    @classmethod
    def checkpoint_is_safe_filename(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        try:
            return validate_filename(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class TrainingEnvironment(BaseModel):
    python: str = Field(default="python3", min_length=1)
    requirements_lock: Optional[str] = None
    system_site_packages: bool = False

    @field_validator("requirements_lock")
    @classmethod
    def safe_lock(cls, value: Optional[str]) -> Optional[str]:
        return relative_path(value) if value is not None else None


class JobSpec(BaseModel):
    """Immutable job specification submitted by the CLI."""

    name: str = Field(..., min_length=1, max_length=256)
    project: str = Field(default="default", min_length=1, max_length=256)

    command: list[str] = Field(..., min_length=1)
    env: dict[str, str] = Field(default_factory=dict)
    source_archive: Optional[str] = None
    work_dir: str = "."
    environment: TrainingEnvironment = Field(default_factory=TrainingEnvironment)
    resume_archive: Optional[str] = None
    backup: bool = True

    @field_validator("source_archive", "resume_archive")
    @classmethod
    def archive_filename(cls, value: Optional[str]) -> Optional[str]:
        return validate_filename(value) if value is not None else None

    @field_validator("work_dir")
    @classmethod
    def safe_work_dir(cls, value: str) -> str:
        return relative_path(value)

    resources: ResourceRequirements = Field(default_factory=ResourceRequirements)
    artifacts: ArtifactPaths = Field(default_factory=ArtifactPaths)
    checkpointing: CheckpointingConfig = Field(default_factory=CheckpointingConfig)

    # Durable automatic retry is not implemented by this worker, so only
    # manual handling is accepted. ``max_retries`` remains on the wire for
    # compatibility with canonical clients that serialize its legacy default.
    on_failure: Literal["manual"] = "manual"
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
    process_start_time: Optional[str] = None
    process_pgid: Optional[int] = None
    process_marker: Optional[str] = None
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
