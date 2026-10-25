"""MCP server exposing Kdenlive project inspection and melt rendering."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__
from .jobs import (
    _avformat_ok,
    environment_report,
    find_binary,
    job_status,
    list_jobs,
    start_job,
    stop_job,
)
from .project import inspect_project

mcp = MCPServer(
    name="kdenlive",
    version=__version__,
    instructions=(
        "Control Kdenlive video projects: inspect .kdenlive files (tracks, clips, "
        "missing resources, duration) and render them to video with melt (MLT). "
        "Rendering runs as a background job; poll render_status until running=false."
    ),
)


def _abs(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


@mcp.tool()
def kdenlive_env() -> dict[str, Any]:
    """Report available binaries (kdenlive, melt, ffmpeg), versions and job state dir."""
    return environment_report()


@mcp.tool()
def project_info(path: str) -> dict[str, Any]:
    """Inspect a .kdenlive project: profile (fps/resolution), tracks, clip resources,
    duration and any missing resource files. Read-only."""
    return vars(inspect_project(_abs(path)))


@mcp.tool()
def render_start(
    project: str,
    output: str,
    profile: str | None = None,
    realtime: bool = False,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Render a .kdenlive project to a video file with melt (background job).

    Args:
        project: path to the .kdenlive file.
        output: output file path; container/codec comes from the extension
            (e.g. .mp4, .mkv, .mov). Parent directories are created.
        profile: optional MLT profile name (e.g. "dv_pal", "hdv_1080_50i").
        realtime: if true, use -real_time 0 (faster, skips some filters' quality).
        extra_args: extra melt arguments appended verbatim.
    """
    project = _abs(project)
    output = _abs(output)
    if not Path(project).is_file():
        return {"ok": False, "error": f"project not found: {project}"}
    melt = find_binary("melt", "melt-7", "melt-6")
    if not melt:
        return {
            "ok": False,
            "error": "melt not found on PATH",
            "hint": "install it, e.g. `sudo apt install melt`",
        }
    av_ok, av_err = _avformat_ok()
    if not av_ok and os.environ.get("KDENLIVE_MCP_FORCE_RENDER") != "1":
        return {
            "ok": False,
            "error": f"libavformat cannot be loaded: {av_err}",
            "hint": (
                "melt would hang without it; repair the corrupted system library with "
                "`sudo apt-get install --reinstall libopenjp2-7 ffmpeg` "
                "(set KDENLIVE_MCP_FORCE_RENDER=1 to try anyway)"
            ),
        }
    Path(output).parent.mkdir(parents=True, exist_ok=True)

    command = [melt, project, "-progress"]
    if profile:
        command += ["-profile", profile]
    if realtime:
        command += ["-real_time", "0"]
    command += ["-consumer", f"avformat:{output}"]
    command += list(extra_args or [])

    job = start_job(command, project=project, output=output)
    return {
        "ok": True,
        "job_id": job.job_id,
        "command": job.command,
        "output": output,
        "log": job.log_path,
    }


@mcp.tool()
def render_status(job_id: str) -> dict[str, Any]:
    """Get status of a render job: running flag, exit code, percent progress,
    last log lines and whether the output file exists yet."""
    return job_status(job_id)


@mcp.tool()
def render_stop(job_id: str) -> dict[str, Any]:
    """Terminate a running render job (SIGTERM then SIGKILL on its process group)."""
    return stop_job(job_id)


@mcp.tool()
def jobs_list() -> list[dict[str, Any]]:
    """List all known render jobs (newest first) with status and progress."""
    return list_jobs()


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
