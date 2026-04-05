"""SQLite-backed job state storage for the worker agent."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from gpuharbor.common.states import JobState, validate_transition

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    project       TEXT NOT NULL DEFAULT 'default',
    spec_json     TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'created',
    server_name   TEXT NOT NULL DEFAULT '',
    container_id  TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    completed_at  TEXT,
    error_message TEXT,
    metrics_json  TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL,
    type        TEXT NOT NULL,
    uri         TEXT NOT NULL,
    sha256      TEXT,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project);
CREATE INDEX IF NOT EXISTS idx_artifacts_job_id ON artifacts(job_id);
"""


class JobStore:
    """Thread-safe SQLite store for job records and artifacts."""

    def __init__(self, db_path: str | Path = "/var/lib/gpuharbor/jobs.db"):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a thread-local connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    def create_job(
        self,
        spec_json: str,
        name: str,
        project: str = "default",
        server_name: str = "",
        job_id: str | None = None,
    ) -> dict:
        """Insert a new job record. Returns the full job dict."""
        if job_id is None:
            job_id = f"job_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).isoformat()

        conn = self._get_conn()
        conn.execute(
            """INSERT INTO jobs (job_id, name, project, spec_json, state, server_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (job_id, name, project, spec_json, JobState.CREATED.value, server_name, now),
        )
        conn.commit()
        logger.info("Created job %s (%s)", job_id, name)
        return self.get_job(job_id)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> dict | None:
        """Fetch a single job by ID. Returns None if not found."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def list_jobs(
        self,
        state: str | None = None,
        project: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """List jobs, optionally filtered by state and/or project."""
        conn = self._get_conn()
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list = []

        if state:
            query += " AND state = ?"
            params.append(state)
        if project:
            query += " AND project = ?"
            params.append(project)

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def update_state(
        self,
        job_id: str,
        new_state: JobState,
        error_message: str | None = None,
    ) -> dict:
        """Transition a job to a new state. Validates the transition.

        Returns the updated job dict. Raises ValueError for invalid transitions,
        KeyError if job not found.
        """
        conn = self._get_conn()
        row = conn.execute("SELECT state FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Job not found: {job_id}")

        current_state = JobState(row["state"])
        validate_transition(current_state, new_state)

        now = datetime.now(timezone.utc).isoformat()
        updates = ["state = ?", "error_message = COALESCE(?, error_message)"]
        params: list = [new_state.value, error_message]

        if new_state == JobState.RUNNING and current_state != JobState.CHECKPOINTING:
            updates.append("started_at = ?")
            params.append(now)

        if new_state in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELED):
            updates.append("completed_at = ?")
            params.append(now)

        params.append(job_id)
        conn.execute(
            f"UPDATE jobs SET {', '.join(updates)} WHERE job_id = ?",
            params,
        )
        conn.commit()
        logger.info("Job %s: %s -> %s", job_id, current_state.value, new_state.value)
        return self.get_job(job_id)  # type: ignore[return-value]

    def update_container_id(self, job_id: str, container_id: str) -> None:
        """Set the Docker container ID for a running job."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE jobs SET container_id = ? WHERE job_id = ?",
            (container_id, job_id),
        )
        conn.commit()

    def update_metrics(self, job_id: str, metrics: dict) -> None:
        """Update the latest training metrics for a job."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE jobs SET metrics_json = ? WHERE job_id = ?",
            (json.dumps(metrics), job_id),
        )
        conn.commit()

    def add_artifact(
        self,
        job_id: str,
        artifact_type: str,
        uri: str,
        sha256: str | None = None,
    ) -> dict:
        """Record an artifact for a job."""
        artifact_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()

        conn = self._get_conn()
        conn.execute(
            """INSERT INTO artifacts (artifact_id, job_id, type, uri, sha256, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (artifact_id, job_id, artifact_type, uri, sha256, now),
        )
        conn.commit()
        logger.info("Artifact %s for job %s: %s", artifact_id, job_id, uri)
        return {
            "artifact_id": artifact_id,
            "job_id": job_id,
            "type": artifact_type,
            "uri": uri,
            "sha256": sha256,
            "created_at": now,
        }

    def get_artifacts(self, job_id: str) -> list[dict]:
        """List all artifacts for a job."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM artifacts WHERE job_id = ? ORDER BY created_at",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_artifact(self, artifact_id: str) -> dict | None:
        """Fetch a single artifact by ID."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_running_job_ids(self) -> list[str]:
        """Return IDs of all jobs in RUNNING, CHECKPOINTING, or CANCEL_REQUESTED state.

        Includes CANCEL_REQUESTED so that interrupted cancellations can be
        resumed after a worker restart.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT job_id FROM jobs WHERE state IN (?, ?, ?)",
            (
                JobState.RUNNING.value,
                JobState.CHECKPOINTING.value,
                JobState.CANCEL_REQUESTED.value,
            ),
        ).fetchall()
        return [r["job_id"] for r in rows]

    def count_active_jobs(self) -> int:
        """Count jobs that are not in a terminal state."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM jobs WHERE state NOT IN (?, ?, ?)",
            (JobState.COMPLETED.value, JobState.FAILED.value, JobState.CANCELED.value),
        ).fetchone()
        return row["cnt"] if row else 0

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        # Parse JSON fields
        if d.get("spec_json"):
            try:
                d["spec"] = json.loads(d["spec_json"])
            except (json.JSONDecodeError, TypeError):
                d["spec"] = None
        if d.get("metrics_json"):
            try:
                d["metrics"] = json.loads(d["metrics_json"])
            except (json.JSONDecodeError, TypeError):
                d["metrics"] = None
        else:
            d["metrics"] = None
        return d
