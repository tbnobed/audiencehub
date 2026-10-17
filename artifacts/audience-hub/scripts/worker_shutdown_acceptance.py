"""Release-image docker-stop acceptance, exclusively on disposable containers.

Requires Docker Compose and a locally built image. No production configuration
is loaded. All credentials below are ephemeral, generated for this test alone.
"""
import argparse
import base64
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="audience-hub:latest")
    args = parser.parse_args()
    project = "kinship-shutdown-" + uuid.uuid4().hex[:12]
    password = secrets.token_hex(24)
    env = {
        "APP_ENV": "development", "AUTH_MODE": "dev",
        "DATABASE_URL": f"postgresql+psycopg://probe:{password}@db/probe",
        "SECRET_KEY": secrets.token_hex(32),
        "FERNET_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "PII_HASH_PEPPER": secrets.token_hex(32),
        "WORKER_CONCURRENCY": "1", "WORKER_STOP_GRACE_SECONDS": "20",
    }
    # The real multiprocess supervisor and child run unchanged. Only the
    # bounded noop handler is substituted with long SQL, in this test image
    # invocation. sitecustomize is loaded in spawned children too.
    hook = """
import sys
sys.path.insert(0, '/app/backend')
from app.jobs import handlers
original = handlers.run
def run(kind, payload, job_id=None):
    if kind != 'noop':
        return original(kind, payload, job_id=job_id)
    from app.db import engine
    from sqlalchemy import text
    with engine.connect() as c:
        c.execute(text("SELECT pg_sleep(300)"))
handlers.run = run
"""
    with tempfile.TemporaryDirectory(prefix=project) as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        hook_path = root / "sitecustomize.py"
        hook_path.write_text(hook)
        hook_path.chmod(0o644)
        service = {"image": args.image, "environment": env,
                   "depends_on": {"db": {"condition": "service_healthy"}}}
        config = {"services": {
            "db": {"image": "postgres:16",
                   "environment": {"POSTGRES_USER": "probe", "POSTGRES_PASSWORD": password,
                                   "POSTGRES_DB": "probe"},
                   "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U probe -d probe"],
                                   "interval": "1s", "timeout": "5s", "retries": 60}},
            "admin": {**service, "command": ["true"]},
            "worker": {**service, "command": ["/app/scripts/start-worker.sh"],
                       "stop_grace_period": "30s",
                       "environment": {**env, "PYTHONPATH": "/probe:/app/backend"},
                       "volumes": [f"{hook_path}:/probe/sitecustomize.py:ro"]},
        }}
        config_path = root / "compose.json"
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o600)
        base = ["docker", "compose", "-p", project, "-f", str(config_path)]

        def redact(diagnostic):
            for secret in (env["DATABASE_URL"], password, env["SECRET_KEY"],
                           env["FERNET_KEY"], env["PII_HASH_PEPPER"]):
                diagnostic = diagnostic.replace(secret, "[REDACTED]")
            return diagnostic

        def compose(*command):
            # Do not echo Compose output: it can contain environment values.
            result = subprocess.run([*base, *command], capture_output=True, text=True, timeout=180)
            if result.returncode:
                diagnostic = redact(result.stderr)
                raise RuntimeError(f"Isolated Docker command failed ({command[0]}); "
                                   f"exit {result.returncode}: {diagnostic[-2000:]}")
            return result.stdout.strip()

        def sql(statement):
            return compose("exec", "-T", "db", "psql", "-U", "probe", "-d", "probe",
                           "-At", "-v", "ON_ERROR_STOP=1", "-c", statement)

        try:
            compose("up", "-d", "--wait", "db")
            compose("run", "--rm", "admin", "python", "-m", "alembic", "upgrade", "head")
            job_id = sql("INSERT INTO jobs(type,status,payload) VALUES('noop','queued','{}') RETURNING id").splitlines()[0]
            assert job_id.isdigit()
            compose("up", "-d", "worker")
            deadline = time.monotonic() + 30
            while sql("SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE "
                      "'kinship-worker-%' AND query='SELECT pg_sleep(300)' AND state='active'") != "1":
                if time.monotonic() > deadline:
                    raise AssertionError("Worker did not start the long SQL statement")
                time.sleep(0.2)
            started = time.monotonic()
            # Docker supplies SIGTERM, waits the configured grace, then SIGKILL.
            compose("stop", "worker")
            elapsed = time.monotonic() - started
            assert elapsed < 30, f"Shutdown exceeded grace: {elapsed:.2f}s"
            assert sql(f"SELECT status||':'||attempts FROM jobs WHERE id={job_id}") == "queued:0"
            assert sql("SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE 'kinship-worker-%'") == "0"
            print(f"PASS: docker stop removed worker backends and retried job in {elapsed:.2f}s")
        except Exception as exc:
            diagnostic = redact(compose("logs", "--no-color", "--tail", "30", "db", "worker"))
            cid = compose("ps", "-q", "db")
            if cid:
                health = subprocess.run(["docker", "inspect", "--format", "{{json .State.Health}}", cid],
                                        capture_output=True, text=True, timeout=10)
                diagnostic += "\nDatabase health: " + redact(health.stdout)
            raise RuntimeError(f"{exc}\nIsolated test container diagnostics:\n{diagnostic[-4000:]}") from exc
        finally:
            compose("down", "-v", "--remove-orphans")


if __name__ == "__main__":
    main()
