"""One supervised worker/tunnel lifecycle, shared by installer and Vast boots."""

from __future__ import annotations

import argparse
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.request
import json
from pathlib import Path


def read_environment(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        parts = shlex.split(value)
        result[key] = parts[0] if parts else ""
    return result


def write_configuration(root: Path) -> Path:
    env_file = root / "worker.env"
    env = read_environment(env_file)
    root.mkdir(parents=True, exist_ok=True)
    worker = root / "run-worker.sh"
    worker.write_text(
        f"#!/bin/bash\nset -euo pipefail\nset -a\nsource {shlex.quote(str(env_file))}\nset +a\nexec {shlex.quote(sys.executable)} -m gpuharbor.worker.api\n"
    )
    worker.chmod(0o700)
    tunnel = root / "run-tunnel.sh"
    tunnel.write_text(
        f"#!/bin/bash\nset -euo pipefail\nset -a\nsource {shlex.quote(str(env_file))}\nset +a\nexec cloudflared tunnel run\n"
    )
    # cloudflared reads TUNNEL_TOKEN, keeping the secret out of argv.
    with tunnel.open("r+") as file:
        text = file.read().replace(
            "exec cloudflared",
            'export TUNNEL_TOKEN="$GPUHARBOR_TUNNEL_TOKEN"\nexec cloudflared',
        )
        file.seek(0)
        file.write(text)
        file.truncate()
    tunnel.chmod(0o700)
    config = root / "supervisor.conf"
    text = f"""[unix_http_server]
file={root}/supervisor.sock
chmod=0700
[supervisord]
logfile={root}/supervisor.log
logfile_maxbytes=10MB
logfile_backups=2
pidfile={root}/supervisor.pid
childlogdir={root}
[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface
[supervisorctl]
serverurl=unix://{root}/supervisor.sock
[program:gpuharbor-worker]
command=/bin/bash {shlex.quote(str(worker))}
autostart=true
autorestart=true
startsecs=2
stopwaitsecs=30
stopasgroup=false
killasgroup=false
redirect_stderr=true
stdout_logfile={root}/worker.log
stdout_logfile_maxbytes=20MB
stdout_logfile_backups=3
"""
    if env.get("GPUHARBOR_TUNNEL_TOKEN"):
        text += f"""[program:gpuharbor-tunnel]
command=/bin/bash {shlex.quote(str(tunnel))}
autostart=true
autorestart=true
startsecs=2
redirect_stderr=true
stdout_logfile={root}/tunnel.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=2
"""
    config.write_text(text.replace("%", "%%"))
    config.chmod(0o600)
    return config


def verify(root: Path, public: bool = True, attempts: int = 30) -> None:
    env = read_environment(root / "worker.env")
    scheme = "https" if env.get("GPUHARBOR_TLS_CERT") else "http"
    local = f"{scheme}://127.0.0.1:{env['GPUHARBOR_PORT']}"
    urls = [local]
    if public and env.get("GPUHARBOR_WORKER_URL"):
        urls.append(env["GPUHARBOR_WORKER_URL"])
    for url in urls:
        last_error = None
        for _ in range(attempts):
            try:
                request = urllib.request.Request(
                    url + "/v1/status",
                    headers={"Authorization": f"Bearer {env['GPUHARBOR_AUTH_TOKEN']}"},
                )
                context = None
                if url == local and env.get("GPUHARBOR_TLS_CERT"):
                    import ssl

                    context = ssl.create_default_context(
                        cafile=env["GPUHARBOR_TLS_CERT"]
                    )
                with urllib.request.urlopen(
                    request, timeout=5, context=context
                ) as response:
                    status = json.load(response)
                if (
                    status.get("protocol_version") != 2
                    or status.get("hostname") != env["GPUHARBOR_SERVER_NAME"]
                ):
                    raise ValueError("Endpoint points to an unexpected worker/version")
                expected_instance = env.get("GPUHARBOR_VAST_INSTANCE_ID")
                if (
                    expected_instance
                    and status.get("vast_instance_id") != expected_instance
                ):
                    raise ValueError("Tunnel points to another Vast instance")
                print(f"Verified authenticated worker: {url}")
                break
            except Exception as exc:
                last_error = exc
                time.sleep(2)
        else:
            raise RuntimeError(f"Worker verification failed for {url}: {last_error}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["start", "restart", "stop", "status", "verify"]
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("GPUHARBOR_STORAGE_ROOT", "/workspace/gpuharbor"),
    )
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if args.update:
        env = read_environment(root / "worker.env")
        repo = Path(env["GPUHARBOR_WORKER_REPO"])
        if env.get("GPUHARBOR_WORKER_REF", "main") != "main":
            raise SystemExit(
                "Pinned deployments update by rebuilding the image or choosing a new commit, then rerunning setup"
            )
        subprocess.run(["git", "-C", str(repo), "pull", "--ff-only"], check=True)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--require-hashes",
                "-r",
                str(repo / "requirements.lock"),
            ],
            check=True,
        )
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-deps", str(repo)], check=True
        )
    if args.action == "verify":
        verify(root)
        return
    config = write_configuration(root)
    ctl = [sys.executable, "-m", "supervisor.supervisorctl", "-c", str(config)]
    running = subprocess.run([*ctl, "pid"], capture_output=True).returncode == 0
    if args.action == "status":
        raise SystemExit(subprocess.run([*ctl, "status"]).returncode)
    if args.action == "stop":
        subprocess.run([*ctl, "stop", "all"], check=True)
        return
    # Migrate old nohup workers only after verifying the PID's command.
    if not running and (root / "worker.pid").exists():
        pid = int((root / "worker.pid").read_text())
        proc = Path(f"/proc/{pid}/cmdline")
        if proc.exists() and any(
            marker in proc.read_bytes()
            for marker in [b"gpuharbor-worker", b"gpuharbor.worker.api"]
        ):
            os.kill(pid, signal.SIGTERM)
            for _ in range(30):
                if not proc.exists():
                    break
                time.sleep(1)
            else:
                raise RuntimeError(
                    "Old worker is still running; refusing to start a second worker"
                )
    if not running and (root / "tunnel.pid").exists():
        env = read_environment(root / "worker.env")
        pid = int((root / "tunnel.pid").read_text())
        proc = Path(f"/proc/{pid}/cmdline")
        token = env.get("GPUHARBOR_TUNNEL_TOKEN", "")
        if proc.exists() and token and b"cloudflared" in proc.read_bytes() and token.encode() in proc.read_bytes():
            os.kill(pid, signal.SIGTERM)
            for _ in range(15):
                if not proc.exists():
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Old tunnel has not stopped; refusing duplicate connectors")
    if running:
        subprocess.run([*ctl, "reread"], check=True)
        subprocess.run([*ctl, "update"], check=True)
        subprocess.run(
            [*ctl, "restart" if args.action == "restart" else "start", "all"],
            check=False,
        )
    else:
        subprocess.run(
            [sys.executable, "-m", "supervisor.supervisord", "-c", str(config)],
            check=True,
        )
    verify(root)


if __name__ == "__main__":
    main()
