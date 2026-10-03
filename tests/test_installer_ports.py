from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = PROJECT_ROOT / "install.sh"


class InstallerPortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.storage = self.base / "storage"
        self.storage.mkdir()
        self.bin_dir = self.base / "bin"
        self.bin_dir.mkdir()
        self.ss = self.bin_dir / "ss"
        self._write_ss("")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_ss(self, output: str) -> None:
        self.ss.write_text(f"#!/bin/sh\nprintf '%s' '{output}'\n")
        self.ss.chmod(0o755)

    def _run_installer_mode(
        self,
        mode_variable: str,
        **extra_env: str,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("VAST_TCP_PORT_")
            and key not in {
                "GPUHARBOR_HOST",
                "GPUHARBOR_PORT",
                "GPUHARBOR_EXTERNAL_PORT",
                "GPUHARBOR_PORT_SELECTION_ONLY",
                "GPUHARBOR_RENDER_ENV_ONLY",
                "GPUHARBOR_STORAGE_ROOT",
            }
        }
        env.update(
            {
                "PATH": f"{self.bin_dir}:{env['PATH']}",
                "GPUHARBOR_STORAGE_ROOT": str(self.storage),
                mode_variable: "1",
            }
        )
        env.update(extra_env)
        return subprocess.run(
            ["bash", str(INSTALLER)],
            cwd=self.base,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _run_selection(self, **extra_env: str) -> subprocess.CompletedProcess[str]:
        return self._run_installer_mode(
            "GPUHARBOR_PORT_SELECTION_ONLY",
            **extra_env,
        )

    def _run_render(self, **extra_env: str) -> subprocess.CompletedProcess[str]:
        return self._run_installer_mode(
            "GPUHARBOR_RENDER_ENV_ONLY",
            **extra_env,
        )

    def test_two_installs_keep_the_same_endpoint_while_worker_occupies_it(self) -> None:
        first = self._run_selection()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("GPUHARBOR_PORT=5000", first.stdout)
        self.assertIn("GPUHARBOR_EXTERNAL_PORT=5000", first.stdout)

        (self.storage / "worker.env").write_text(
            "GPUHARBOR_PORT=5000\n"
            "GPUHARBOR_EXTERNAL_PORT=5000\n"
        )
        self._write_ss("LISTEN 0 128 0.0.0.0:5000 0.0.0.0:* ")

        second = self._run_selection()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Reusing established endpoint: 5000", second.stdout)
        self.assertIn("GPUHARBOR_PORT=5000", second.stdout)
        self.assertIn("GPUHARBOR_EXTERNAL_PORT=5000", second.stdout)

    def test_vast_never_falls_back_to_an_unmapped_port(self) -> None:
        result = self._run_selection(VAST_TCP_PORT_22="40022")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "No free, valid Vast.ai TCP mapping is available",
            result.stderr,
        )

    def test_vast_reinstall_reuses_occupied_mapped_endpoint(self) -> None:
        (self.storage / "worker.env").write_text(
            "GPUHARBOR_PORT=5000\n"
            "GPUHARBOR_EXTERNAL_PORT=45000\n"
        )
        self._write_ss("LISTEN 0 128 0.0.0.0:5000 0.0.0.0:* ")

        result = self._run_selection(VAST_TCP_PORT_5000="45000")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "Reusing established Vast.ai endpoint: 5000 -> 45000",
            result.stdout,
        )
        self.assertIn("GPUHARBOR_PORT=5000", result.stdout)
        self.assertIn("GPUHARBOR_EXTERNAL_PORT=45000", result.stdout)

    def test_tunnel_mode_uses_fixed_origin_without_vast_mapping(self) -> None:
        result = self._run_render(VAST_TCP_PORT_22="40022", GPUHARBOR_TUNNEL_TOKEN="dummy")
        self.assertEqual(result.returncode, 0, result.stderr)
        env = (self.storage / "worker.env").read_text()
        self.assertIn("GPUHARBOR_PORT=5000\n", env)
        self.assertIn("GPUHARBOR_HOST=127.0.0.1\n", env)

    def test_tunnel_mode_does_not_follow_an_unrelated_mapped_port(self) -> None:
        result = self._run_selection(VAST_TCP_PORT_8443="38443", GPUHARBOR_TUNNEL_TOKEN="dummy")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GPUHARBOR_PORT=5000\n", result.stdout)

    def test_explicit_loopback_host_survives_runtime_env_rendering(self) -> None:
        (self.base / ".env").write_text("GPUHARBOR_HOST=127.0.0.1\n")

        result = self._run_render()

        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = dict(
            line.split("=", 1)
            for line in (self.storage / "worker.env").read_text().splitlines()
        )
        self.assertEqual(rendered["GPUHARBOR_HOST"], "127.0.0.1")
        self.assertEqual(rendered["GPUHARBOR_PORT"], "5000")

    def test_invalid_listener_host_is_rejected_before_rendering(self) -> None:
        result = self._run_render(GPUHARBOR_HOST="127.0.0.1\nINJECTED=1")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid GPUHARBOR_HOST", result.stderr)
        self.assertFalse((self.storage / "worker.env").exists())
