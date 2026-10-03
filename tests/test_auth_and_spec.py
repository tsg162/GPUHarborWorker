from __future__ import annotations

import json
import unittest
from unittest import mock

from fastapi import HTTPException
from fastapi.routing import APIRoute
from pydantic import ValidationError
from starlette.requests import Request

from gpuharbor.common.job_spec import JobSpec
from gpuharbor.worker import api


def _request_without_headers(path: str = "/v1/status") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
        }
    )


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_configuration_rejects_anonymous_requests(self) -> None:
        with (
            mock.patch.object(api, "AUTH_TOKEN", ""),
            mock.patch.object(api, "ALLOW_UNAUTHENTICATED", False),
            mock.patch.object(api, "HOST", "0.0.0.0"),
        ):
            with self.assertRaises(HTTPException) as raised:
                await api.verify_auth(_request_without_headers())
            self.assertEqual(raised.exception.status_code, 401)
            with self.assertRaises(RuntimeError):
                api.validate_security_configuration()

    async def test_unauthenticated_mode_requires_loopback_and_opt_in(self) -> None:
        with (
            mock.patch.object(api, "AUTH_TOKEN", ""),
            mock.patch.object(api, "ALLOW_UNAUTHENTICATED", True),
            mock.patch.object(api, "HOST", "127.0.0.1"),
        ):
            api.validate_security_configuration()
            await api.verify_auth(_request_without_headers())

        with (
            mock.patch.object(api, "AUTH_TOKEN", ""),
            mock.patch.object(api, "ALLOW_UNAUTHENTICATED", True),
            mock.patch.object(api, "HOST", "0.0.0.0"),
        ):
            with self.assertRaises(RuntimeError):
                api.validate_security_configuration()
            with self.assertRaises(HTTPException):
                await api.verify_auth(_request_without_headers())

    def test_only_health_route_is_public(self) -> None:
        routes = [route for route in api.app.routes if isinstance(route, APIRoute)]
        self.assertEqual({route.path for route in routes if route.path == "/health"}, {"/health"})
        for route in routes:
            dependency_calls = {
                dependency.call for dependency in route.dependant.dependencies
            }
            if route.path == "/health":
                self.assertNotIn(api.verify_auth, dependency_calls)
            else:
                self.assertTrue(route.path.startswith("/v1/"), route.path)
                self.assertIn(api.verify_auth, dependency_calls, route.path)


class JobContractTests(unittest.TestCase):
    def test_auto_retry_is_rejected_until_supported(self) -> None:
        with self.assertRaises(ValidationError):
            JobSpec(
                name="retry",
                command=["false"],
                on_failure="auto_retry",
                max_retries=3,
            )

        spec = JobSpec(name="manual", command=["true"])
        self.assertEqual(spec.on_failure, "manual")
        self.assertEqual(spec.max_retries, 3)

    def test_canonical_manual_payload_remains_wire_compatible(self) -> None:
        # Snapshot of JobSpec(...).model_dump_json() from the canonical
        # GPUHarbor control-plane model, whose CLI submits defaults verbatim.
        canonical_json = """
        {
          "name": "canonical-default",
          "project": "default",
          "command": ["true"],
          "env": {},
          "resources": {"gpu_count": 1, "disk_gb_min": 0},
          "artifacts": {"input_checkpoint": null, "dataset": null},
          "checkpointing": {
            "enabled": false,
            "save_every_minutes": 10,
            "keep_last_n": 3
          },
          "on_failure": "manual",
          "max_retries": 3,
          "source_git_repo": null,
          "source_git_commit": null
        }
        """
        request = api.SubmitJobRequest.model_validate(
            {"spec": json.loads(canonical_json)}
        )

        self.assertEqual(request.spec.on_failure, "manual")
        self.assertEqual(request.spec.max_retries, 3)

    def test_client_job_id_rejects_path_syntax(self) -> None:
        for job_id in ("/tmp/job", "../job", "jobs/other", ".", ".."):
            with self.subTest(job_id=job_id), self.assertRaises(ValidationError):
                api.SubmitJobRequest(
                    spec=JobSpec(name="unsafe", command=["true"]),
                    job_id=job_id,
                )
