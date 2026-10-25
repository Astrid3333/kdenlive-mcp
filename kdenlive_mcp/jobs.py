"""Background render jobs (melt / kdenlive) with logs and progress."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_DIR = Path(
    os.environ.get("KDENLIVE_MCP_HOME", os.path.expanduser("~/.local/state/kdenlive-mcp"))
)
JOBS_DIR = STATE_DIR / "jobs"

_PATTERNS = [
    re.compile(
        r"Current Frame:\s*(?P<done>\d+).*?percentage:\s*(?P<pct>\d{1,3}(?:\.\d+)?)"
    ),
    re.compile(r"percentage:\s*(?P<pct>\d{1,3}(?:\.\d+)?)"),
    re.compile(r"(?P<pct>\d{1,3}(?:\.\d+)?)\s*%"),
    re.compile(r"#\s*(?P<done>\d+)\s*/\s*(?P<total>\d+)"),
    re.compile(r"\b(?P<done>\d+)\s*/\s*(?P<total>\d+)\b"),
]


def find_binary(*names: str) -> str | None:
    for name in names:
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            candidate = Path(directory) / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def _version(cmd: list[str]) -> str | None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or proc.stderr or "").strip()
    return text.splitlines()[0] if text else None


def environment_report() -> dict[str, Any]:
    melt = find_binary("melt", "melt-7", "melt-6")
    kdenlive = find_binary("kdenlive")
    report: dict[str, Any] = {
        "kdenlive": {"path": kdenlive, "version": _version([kdenlive, "--version"]) if kdenlive else None},
        "melt": {"path": melt, "version": _version([melt, "-version"]) if melt else None},
        "ffmpeg": {"path": find_binary("ffmpeg")},
        "ffprobe": {"path": find_binary("ffprobe")},
        "jobs_dir": str(JOBS_DIR),
        "ready": bool(melt),
    }
    if not melt:
        report["hint"] = "melt not found: install it (e.g. `sudo apt install melt`) to enable rendering."
    return report


@dataclass
class Job:
    job_id: str
    project: str
    output: str
    command: list[str]
    created: str
    pid: int | None = None
    returncode: int | None = None
    log_path: str = ""
    meta_path: str = ""
    _proc: subprocess.Popen | None = field(default=None, repr=False)

    @property
    def dir(self) -> Path:
        return JOBS_DIR / self.job_id


def _write_meta(job: Job) -> None:
    meta = {
        "job_id": job.job_id,
        "project": job.project,
        "output": job.output,
        "command": job.command,
        "created": job.created,
        "pid": job.pid,
        "returncode": job.returncode,
        "log_path": job.log_path,
    }
    Path(job.meta_path).write_text(json.dumps(meta, indent=2))


def _load_meta(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_meta_from_dict(meta: dict[str, Any]) -> None:
    path = JOBS_DIR / meta.get("job_id", "") / "job.json"
    try:
        path.write_text(json.dumps(meta, indent=2))
    except OSError:
        pass


def _tail(path: str, lines: int = 40) -> list[str]:
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read().splitlines()[-lines:]
    except OSError:
        return []


def _progress(log_path: str) -> dict[str, Any]:
    best: dict[str, Any] = {"percent": None, "frames": None, "line": None}
    for line in reversed(_tail(log_path, 200)):
        for pattern in _PATTERNS:
            m = pattern.search(line)
            if not m:
                continue
            groups = m.groupdict()
            if groups.get("pct") is not None:
                pct = float(groups["pct"])
                if pct <= 100:
                    done = groups.get("done")
                    best = {
                        "percent": pct,
                        "frames": f"frame {done}" if done else None,
                        "line": line.strip(),
                    }
            else:
                done, total = int(groups["done"]), int(groups["total"])
                if total > 0:
                    best = {
                        "percent": round(100.0 * done / total, 2),
                        "frames": f"{done}/{total}",
                        "line": line.strip(),
                    }
            break
        if best["percent"] is not None:
            break
    return best


def start_job(
    command: list[str],
    project: str,
    output: str,
    env: dict[str, str] | None = None,
) -> Job:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc)
    job_id = f"{stamp.strftime('%Y%m%dT%H%M%S')}-{os.urandom(3).hex()}"
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    log_path = str(job_dir / "render.log")
    meta_path = str(job_dir / "job.json")
    job = Job(
        job_id=job_id,
        project=project,
        output=output,
        command=command,
        created=stamp.isoformat(),
        log_path=log_path,
        meta_path=meta_path,
    )
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, **(env or {})},
        )
    job._proc = proc
    job.pid = proc.pid
    _write_meta(job)
    return job


def job_status(job_id: str) -> dict[str, Any]:
    meta_path = JOBS_DIR / job_id / "job.json"
    if not meta_path.is_file():
        return {"ok": False, "error": f"unknown job: {job_id}"}
    meta = _load_meta(meta_path)
    log_path = meta.get("log_path", str(JOBS_DIR / job_id / "render.log"))

    running = False
    pid = meta.get("pid")
    if pid and meta.get("returncode") is None:
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            # Started by a previous server process: only liveness is checkable.
            try:
                os.kill(pid, 0)
                running = True
            except OSError:
                meta["returncode"] = -1
                _write_meta_from_dict(meta)
        else:
            if done == 0:
                running = True
            else:
                if os.WIFEXITED(status):
                    meta["returncode"] = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    meta["returncode"] = -os.WTERMSIG(status)
                else:
                    meta["returncode"] = -1
                _write_meta_from_dict(meta)

    rc = meta.get("returncode")
    tail = _tail(log_path)
    hint = None
    if rc not in (None, 0):
        lines = [ln for ln in tail if ln.strip() and "%" not in ln]
        hint = lines[-1] if lines else "render failed with no log output"
    return {
        "ok": True,
        "job_id": job_id,
        "project": meta.get("project"),
        "output": meta.get("output"),
        "command": meta.get("command"),
        "created": meta.get("created"),
        "pid": pid,
        "running": running,
        "returncode": rc,
        "progress": _progress(log_path),
        "log_tail": tail,
        "error_hint": hint,
        "output_exists": bool(meta.get("output")) and os.path.exists(meta["output"]),
    }


def list_jobs() -> list[dict[str, Any]]:
    if not JOBS_DIR.is_dir():
        return []
    out = []
    for entry in sorted(JOBS_DIR.iterdir(), reverse=True):
        if (entry / "job.json").is_file():
            status = job_status(entry.name)
            status.pop("log_tail", None)
            out.append(status)
    return out


def stop_job(job_id: str) -> dict[str, Any]:
    meta_path = JOBS_DIR / job_id / "job.json"
    if not meta_path.is_file():
        return {"ok": False, "error": f"unknown job: {job_id}"}
    meta = _load_meta(meta_path)
    pid = meta.get("pid")
    if not pid:
        return {"ok": False, "error": "job has no pid"}
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return {"ok": False, "error": "process not running"}
    time.sleep(0.5)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    return {"ok": True, "job_id": job_id, "stopped": True}
