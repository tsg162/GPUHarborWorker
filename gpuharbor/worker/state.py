"""SQLite-backed job state storage for the worker agent."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from gpuharbor.common.states import JobState, TERMINAL_STATES, validate_transition
from gpuharbor.common.storage import validate_job_id

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
    process_start_time TEXT,
    process_pgid  INTEGER,
    process_marker TEXT,
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

_JOB_COLUMN_MIGRATIONS = {
    "process_start_time": "TEXT",
    "process_pgid": "INTEGER",
    "process_marker": "TEXT",
    "gpu_ids_json": "TEXT",
    "backup_json": "TEXT",
    "pinned": "INTEGER NOT NULL DEFAULT 0",
}


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
        existing_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for column, column_type in _JOB_COLUMN_MIGRATIONS.items():
            if column not in existing_columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {column_type}")
        artifact_columns = {row["name"] for row in conn.execute("PRAGMA table_info(artifacts)")}
        for column, kind in {"size": "INTEGER NOT NULL DEFAULT 0", "signature": "TEXT"}.items():
            if column not in artifact_columns:
                conn.execute(f"ALTER TABLE artifacts ADD COLUMN {column} {kind}")
        conn.execute("UPDATE artifacts SET uri = 'jobs/' || job_id || '/' || uri WHERE uri LIKE 'checkpoints/%' OR uri LIKE 'output/%' OR uri LIKE 'logs/%'")
        conn.execute("DELETE FROM artifacts WHERE rowid NOT IN (SELECT MAX(rowid) FROM artifacts GROUP BY job_id, uri)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_uri ON artifacts(job_id, uri)")
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
        validate_job_id(job_id)
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
        """Set the legacy process/container identifier for a running job."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE jobs SET container_id = ? WHERE job_id = ?",
            (container_id, job_id),
        )
        conn.commit()

    def update_execution_identity(
        self,
        job_id: str,
        *,
        pid: int,
        start_time: str,
        pgid: int,
        marker: str,
    ) -> None:
        """Atomically persist the Linux process identity used for recovery."""
        conn = self._get_conn()
        cursor = conn.execute(
            """UPDATE jobs
               SET container_id = ?,
                   process_start_time = ?,
                   process_pgid = ?,
                   process_marker = ?
               WHERE job_id = ?""",
            (str(pid), start_time, pgid, marker, job_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"Job not found: {job_id}")
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
        size: int = 0,
        signature: str | None = None,
    ) -> dict:
        """Record an artifact for a job."""
        artifact_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()

        conn = self._get_conn()
        conn.execute(
            """INSERT INTO artifacts (artifact_id, job_id, type, uri, sha256, created_at, size, signature)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_id, uri) DO UPDATE SET type=excluded.type,
               sha256=excluded.sha256, size=excluded.size, signature=excluded.signature""",
            (artifact_id, job_id, artifact_type, uri, sha256, now, size, signature),
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
            "size": size,
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

    def set_gpu_ids(self, job_id: str, gpu_ids: list[int]) -> None:
        conn = self._get_conn()
        conn.execute("UPDATE jobs SET gpu_ids_json=? WHERE job_id=?", (json.dumps(gpu_ids), job_id))
        conn.commit()

    def set_backup(self, job_id: str, receipt: dict) -> None:
        conn = self._get_conn()
        conn.execute("UPDATE jobs SET backup_json=? WHERE job_id=?", (json.dumps(receipt), job_id))
        conn.commit()

    def set_pinned(self, job_id: str, pinned: bool) -> None:
        conn = self._get_conn()
        conn.execute("UPDATE jobs SET pinned=? WHERE job_id=?", (int(pinned), job_id))
        conn.commit()

    def remove_artifacts(self, job_id: str, uris: list[str]) -> None:
        conn = self._get_conn()
        conn.executemany("DELETE FROM artifacts WHERE job_id=? AND uri=?", ((job_id, uri) for uri in uris))
        conn.commit()

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

    def get_nonterminal_jobs(self) -> list[dict]:
        """Return every job requiring deterministic startup reconciliation."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE state NOT IN (?, ?, ?)
               ORDER BY created_at""",
            (
                JobState.COMPLETED.value,
                JobState.FAILED.value,
                JobState.CANCELED.value,
            ),
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def count_active_jobs(self) -> int:
        """Count jobs that are not in a terminal state."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM jobs WHERE state NOT IN (?, ?, ?)",
            (JobState.COMPLETED.value, JobState.FAILED.value, JobState.CANCELED.value),
        ).fetchone()
        return row["cnt"] if row else 0

    def list_terminal_jobs(
        self,
        *,
        project: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """Return terminal jobs, oldest completion first."""
        conn = self._get_conn()
        states = tuple(state.value for state in TERMINAL_STATES)
        placeholders = ", ".join("?" for _ in states)
        where = [f"state IN ({placeholders})"]
        params: list = [*states]
        if project:
            where.append("project = ?")
            params.append(project)
        rows = conn.execute(
            f"""SELECT * FROM jobs
                WHERE {" AND ".join(where)}
                ORDER BY COALESCE(completed_at, created_at), created_at
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

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
        d["gpu_ids"] = json.loads(d.get("gpu_ids_json") or "null")
        d["backup"] = json.loads(d.get("backup_json") or "null")
        d["pinned"] = bool(d.get("pinned"))
        return d
