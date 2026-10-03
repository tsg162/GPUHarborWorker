from __future__ import annotations
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from gpuharbor.worker import api
from gpuharbor.worker.gpu import GPUInfo


class ApiWorkflowTests(unittest.TestCase):
    def test_upload_submission_idempotence_gpu_admission_and_range_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.multiple(
                    api,
                    AUTH_TOKEN="test-token",
                    DB_PATH=str(root / "db.sqlite"),
                    STORAGE_ROOT=root / "storage",
                    ALLOW_UNAUTHENTICATED=False,
                ),
                patch(
                    "gpuharbor.worker.gpu.get_gpu_info",
                    return_value=[GPUInfo(0, "test", 24, 0, 0)],
                ),
                TestClient(
                    api.app, headers={"Authorization": "Bearer test-token"}
                ) as client,
            ):
                data = b"checkpoint fixture"
                sha = hashlib.sha256(data).hexdigest()
                response = client.post(
                    "/v1/uploads",
                    json={"filename": "model.pt", "size": len(data), "sha256": sha},
                )
                self.assertEqual(response.status_code, 200, response.text)
                uid = response.json()["upload_id"]
                self.assertEqual(
                    client.put(
                        f"/v1/uploads/{uid}",
                        params={"offset": 0, "sha256": sha},
                        content=data,
                    ).status_code,
                    200,
                )
                uploaded = client.post(f"/v1/uploads/{uid}/complete").json()
                response = client.get(
                    "/v1/files/uploads/" + uploaded["filename"],
                    headers={"Range": "bytes=3-"},
                )
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.content, data[3:])
                request = {
                    "job_id": "job_idempotent",
                    "spec": {"name": "sleep", "command": ["sleep", "1"]},
                }
                first = client.post("/v1/jobs", json=request)
                second = client.post("/v1/jobs", json=request)
                self.assertEqual(first.status_code, 201, first.text)
                self.assertEqual(first.json()["job_id"], second.json()["job_id"])
                busy = client.post(
                    "/v1/jobs", json={"spec": {"name": "other", "command": ["true"]}}
                )
                self.assertEqual(busy.status_code, 409)
                for _ in range(60):
                    job = client.get("/v1/jobs/job_idempotent").json()
                    if job["state"] in {"completed", "failed"}:
                        break
                    time.sleep(0.05)
                self.assertEqual(job["state"], "completed", job.get("error_message"))
                self.assertEqual(client.get("/v1/status").json()["protocol_version"], 2)
                self.assertEqual(
                    client.get("/v1/jobs/job_idempotent/logs?follow=true").status_code,
                    200,
                )
                cleaned = client.post("/v1/jobs/job_idempotent/cleanup").json()
                self.assertTrue(cleaned["dry_run"])
                self.assertTrue(cleaned["skipped"])
