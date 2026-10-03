"""Run the real CLI against a local HTTP worker with simulated GPU discovery.

Requires a sibling GPUHarbor checkout and its .venv, or GPUHARBOR_CLI_PYTHON.
All job data and service processes are temporary; no provider is contacted.
"""

import json, os, re, socket, subprocess, sys, tempfile, time
from pathlib import Path
import httpx

WORKER = Path(__file__).resolve().parents[1]
CLI = WORKER.parent / "GPUHarbor"
with tempfile.TemporaryDirectory(prefix="ghb-integration-") as tmp:
    root = Path(tmp)
    storage = root / "storage"
    backup = root / "backup"
    project = root / "project"
    project.mkdir()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    env = {
        **os.environ,
        "PYTHONPATH": str(WORKER),
        "GPUHARBOR_AUTH_TOKEN": "integration-token",
        "GPUHARBOR_STORAGE_ROOT": str(storage),
        "GPUHARBOR_DB_PATH": str(root / "jobs.db"),
        "GPUHARBOR_HOST": "127.0.0.1",
        "GPUHARBOR_PORT": str(port),
        "GPUHARBOR_BACKUP_DEST": str(backup),
        "GPUHARBOR_TRAINING_PYTHON": sys.executable,
    }
    script = 'from gpuharbor.worker import gpu; gpu.get_gpu_info=lambda:[gpu.GPUInfo(0,"fake",24,0,0),gpu.GPUInfo(1,"fake",24,0,0)]; from gpuharbor.worker.api import main; main()'
    log = (root / "worker.log").open("w")
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=WORKER,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    cfg = root / "servers.yaml"
    cfg.write_text(
        f"servers:\n  local:\n    url: http://127.0.0.1:{port}\n    token: integration-token\ndefaults:\n  server: local\n"
    )
    cli_env = {
        **os.environ,
        "GPUHARBOR_CONFIG": str(cfg),
        "GPUHARBOR_STATE_DIR": str(root / "client-state"),
    }

    def cli(*args):
        response = subprocess.run(
            [
                os.environ.get("GPUHARBOR_CLI_PYTHON", str(CLI / ".venv/bin/python")),
                "-m",
                "gpuharbor.cli.main",
                *args,
            ],
            cwd=CLI,
            env=cli_env,
            text=True,
            capture_output=True,
            timeout=90,
        )
        if response.returncode:
            raise AssertionError(f"{args}: {response.stdout}\n{response.stderr}")
        return response.stdout

    client = httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        headers={"Authorization": "Bearer integration-token"},
    )

    def wait_job(jid):
        for _ in range(150):
            job = client.get(f"/v1/jobs/{jid}").json()
            if job["state"] in ["completed", "failed"] and (
                job.get("backup") or {}
            ).get("verified"):
                return job
            time.sleep(0.1)
        raise AssertionError(job)

    try:
        for _ in range(50):
            try:
                if client.get("/health").is_success:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        (project / "train.py").write_text("""import os,json,pathlib
from gpuharbor_training import checkpoint_complete
step=0
if os.environ.get('GPUHARBOR_RESUME_DIR'):
 step=int(pathlib.Path(os.environ['GPUHARBOR_RESUME_DIR'],'step.txt').read_text())
step+=1
folder=pathlib.Path(os.environ['GPUHARBOR_CHECKPOINT_DIR'],f'checkpoint-{step}')
folder.mkdir();(folder/'step.txt').write_text(str(step));checkpoint_complete(folder)
pathlib.Path(os.environ['GPUHARBOR_OUTPUT_DIR'],'result.txt').write_text(str(step))
print('completed step',step,flush=True)
""")
        (project / "job.yaml").write_text(
            "name: integration\ncommand: [python3, train.py]\ncheckpointing:\n  enabled: true\n  keep_last_n: 1\n"
        )
        output = cli("submit", str(project / "job.yaml"))
        jid = re.findall(r"job_[a-f0-9]{32}", output)[-1]
        job = wait_job(jid)
        assert job["state"] == "completed", job
        print(
            "PASS CLI project upload -> HTTP worker -> executed training -> verified automatic backup"
        )
        result = cli("logs", jid, "--follow")
        assert "completed step 1" in result, result
        cli("download", jid, "--out", str(root / "downloaded"))
        assert (root / "downloaded/output/result.txt").read_text() == "1"
        print("PASS SSE logs and verified artifact download")
        output = cli("resume", jid, "--server", "local")
        resumed = re.findall(r"job_[a-f0-9]{32}", output)[-1]
        job = wait_job(resumed)
        assert job["state"] == "completed", job
        assert (storage / "jobs" / resumed / "output/result.txt").read_text() == "2"
        print(
            "PASS resume bundle export -> download/upload -> new job restored checkpoint"
        )
        cli("backup", jid)
        cli("pin", jid)
        dry = cli("cleanup", "--job", jid)
        assert "pinned" in dry
        cli("pin", jid, "--unpin")
        cli("cleanup", "--job", jid, "--execute")
        assert not (storage / "jobs" / jid).exists()
        print(
            "PASS explicit verified backup, pinned cleanup protection, and explicit cleanup"
        )
        snapshot = Path(
            client.get(f"/v1/jobs/{resumed}").json()["backup"]["destination"]
        )
        output = cli("resume", "--snapshot", str(snapshot), "--server", "local")
        from_snapshot = re.findall(r"job_[a-f0-9]{32}", output)[-1]
        job = wait_job(from_snapshot)
        assert job["state"] == "completed", job
        assert (
            storage / "jobs" / from_snapshot / "output/result.txt"
        ).read_text() == "3"
        print("PASS offline snapshot integrity verification and resume")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        client.close()
        log.close()
