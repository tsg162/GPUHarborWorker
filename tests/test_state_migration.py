from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from gpuharbor.worker.state import JobStore


class JobStoreMigrationTests(unittest.TestCase):
    def test_existing_database_gains_process_identity_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "legacy.db"
            connection = sqlite3.connect(db_path)
            connection.execute(
                """CREATE TABLE jobs (
                    job_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    project TEXT NOT NULL DEFAULT 'default',
                    spec_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'created',
                    server_name TEXT NOT NULL DEFAULT '',
                    container_id TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    error_message TEXT,
                    metrics_json TEXT
                )"""
            )
            connection.commit()
            connection.close()

            store = JobStore(db_path)
            columns = {
                row["name"]
                for row in store._get_conn()
                .execute("PRAGMA table_info(jobs)")
                .fetchall()
            }

            self.assertTrue(
                {
                    "process_start_time",
                    "process_pgid",
                    "process_marker",
                }.issubset(columns)
            )
