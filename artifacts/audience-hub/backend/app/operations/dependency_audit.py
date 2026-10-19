"""Operator-invoked audits. Never called at startup or by the scheduler."""
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def run_audits(output_dir="/data/benchmarks"):
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results = {}
    lock = next((p for p in (Path("/app/audit/package-lock.json"),
                Path(__file__).resolve().parents[3] / "package-lock.json") if p.is_file()), None)
    commands = {
        "pip": [sys.executable, "-m", "pip_audit", "--format=json", "--progress-spinner", "off"],
        "npm": ["npm", "audit", "--package-lock-only", "--ignore-scripts", "--json", "--audit-level=high"],
    }
    for name, command in commands.items():
        path = directory / f"dependency-{name}-{stamp}.json"
        if name == "npm" and (not shutil.which("npm") or lock is None):
            results[name] = {"status": "error", "message": "npm or packaged package-lock.json unavailable"}
            path.write_text(json.dumps(results[name]))
            continue
        try:
            with path.open("w") as output:
                process = subprocess.run(command, cwd=lock.parent if name == "npm" else None,
                                         stdout=output, stderr=subprocess.PIPE, text=True, timeout=600)
            data = json.loads(path.read_text())
            if name == "npm":
                counts = data.get("metadata", {}).get("vulnerabilities", {})
                if "error" in data or not counts:
                    raise ValueError("npm audit failed to return vulnerability metadata")
                results[name] = {"status": "fail" if counts.get("high", 0)+counts.get("critical", 0) else "pass",
                                 "counts": counts, "report": str(path)}
            else:
                if process.returncode not in (0, 1) or "dependencies" not in data:
                    raise ValueError("pip-audit did not complete")
                findings = sum(len(dep.get("vulns", [])) for dep in data["dependencies"])
                results[name] = {"status": "fail" if findings else "pass", "findings": findings,
                                 "report": str(path), "policy": "fail on any Python advisory, including unknown severity"}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            results[name] = {"status": "error", "report": str(path), "message": "Audit unavailable or invalid response; inspect tool installation/network on the server"}
    summary = directory / f"dependency-summary-{stamp}.json"
    summary.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    print(f"Reports: {summary}")
    return 0 if all(result["status"] == "pass" for result in results.values()) else 1
