"""MCP server exposing Kdenlive project inspection and melt rendering."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__, jobs
from .jobs import (
    _avformat_ok,
    environment_report,
    find_binary,
    job_status,
    list_jobs,
    run_command,
    start_job,
    stop_job,
)
from .project import inspect_project, merge_projects, validate_resources

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


# Named presets -> MLT profile names. Custom resolutions ("WxH") get a
# temporary profile written under a per-call HOME/.mlt/profiles so melt picks
# it up without polluting the user's home.
PRESETS: dict[str, str] = {
    "dvd_pal": "dv_pal",
    "dvd_ntsc": "dv_ntsc",
    "hd720p25": "atsc_720p25",
    "hd720p30": "atsc_720p",
    "hd1080p24": "hdv_1080_24p",
    "hd1080p25": "hdv_1080_25i",
    "hd1080p30": "hdv_1080_30i",
}
RESOLUTIONS: dict[str, str] = {
    "720p": "1280x720",
    "1080p": "1920x1080",
    "4k": "3840x2160",
}


def _resolve_profile(
    profile: str | None, preset: str | None, resolution: str | None
) -> tuple[str | None, dict[str, str] | None]:
    """Return (mlt_profile_or_None, extra_env_or_None).

    For custom resolutions a profile XML is written under a temporary
    HOME/.mlt/profiles and HOME is overridden so melt finds it."""
    res: str | None = None
    if profile:
        return profile, None
    if preset:
        if preset in PRESETS:
            return PRESETS[preset], None
        if preset in RESOLUTIONS:
            res = RESOLUTIONS[preset]
        else:
            return None, None
    elif resolution:
        res = resolution
    if not res:
        return None, None
    try:
        w, h = res.lower().split("x")
        int(w)
        int(h)
    except ValueError:
        return None, None
    tmp = tempfile.mkdtemp(prefix="kdenlive-mcp-profiles-")
    prof_dir = Path(tmp) / ".mlt" / "profiles"
    prof_dir.mkdir(parents=True, exist_ok=True)
    prof_name = f"kdenlive_custom_{w}x{h}"
    (prof_dir / prof_name).write_text(
        '<?xml version="1.0"?>\n'
        f'<profile width="{w}" height="{h}" frame_rate_num="25" frame_rate_den="1" '
        'progressive="1" colorspace="709" description="kdenlive-mcp custom"/>\n',
        encoding="utf-8",
    )
    return prof_name, {"HOME": tmp}


def _zone_frames(project: str, zone: list[float]) -> tuple[int, int] | None:
    """Convert [start_sec, end_sec] to melt -in/-out frame numbers using the
    project's fps. Falls back to treating values as raw frames."""
    try:
        start_sec, end_sec = float(zone[0]), float(zone[1])
    except (TypeError, ValueError, IndexError):
        return None
    info = inspect_project(project)
    fps = info.profile.get("fps") if info.ok else None
    if fps:
        return int(round(start_sec * fps)), int(round(end_sec * fps))
    return int(start_sec), int(end_sec)


def _melt_check() -> tuple[str | None, dict[str, Any] | None]:
    melt = find_binary("melt", "melt-7", "melt-6")
    if not melt:
        return None, {
            "ok": False,
            "error": "melt not found on PATH",
            "hint": "install it, e.g. `sudo apt install melt`",
        }
    return melt, None


def _avformat_check() -> dict[str, Any] | None:
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
    return None


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
    preset: str | None = None,
    resolution: str | None = None,
    zone: list[float] | None = None,
    realtime: bool = False,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Render a .kdenlive project to a video file with melt (background job).

    Args:
        project: path to the .kdenlive file.
        output: output file path; container/codec comes from the extension
            (e.g. .mp4, .mkv, .mov). Parent directories are created.
        profile: optional MLT profile name (e.g. "dv_pal", "hdv_1080_50i").
        preset: named preset (dvd_pal, dvd_ntsc, hd720p25, hd720p30,
            hd1080p24, hd1080p25, hd1080p30, 720p, 1080p, 4k). Custom
            resolutions create a temporary MLT profile.
        resolution: "WxH" custom resolution (only when profile/preset unset).
        zone: [start_sec, end_sec] render only that segment (converted to
            frames using the project fps).
        realtime: if true, use -real_time 0 (faster, skips some filters' quality).
        extra_args: extra melt arguments appended verbatim.
    """
    project = _abs(project)
    output = _abs(output)
    if not Path(project).is_file():
        return {"ok": False, "error": f"project not found: {project}"}
    melt, err = _melt_check()
    if err:
        return err
    av_err = _avformat_check()
    if av_err:
        return av_err
    Path(output).parent.mkdir(parents=True, exist_ok=True)

    profile_name, env_extra = _resolve_profile(profile, preset, resolution)
    command = [melt, project, "-progress"]
    if profile_name:
        command += ["-profile", profile_name]
    if zone:
        frames = _zone_frames(project, zone)
        if frames:
            command += ["-in", str(frames[0]), "-out", str(frames[1])]
    if realtime:
        command += ["-real_time", "0"]
    command += ["-consumer", f"avformat:{output}"]
    command += list(extra_args or [])

    job = start_job(command, project=project, output=output, env=env_extra)
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
def kdenlive_extract_frames(
    project: str,
    timestamps: list[float] | None = None,
    output_dir: str | None = None,
    format: str = "png",
) -> dict[str, Any]:
    """Extract single frames from a .kdenlive project at the given seconds.

    Runs melt synchronously per timestamp (-in/-out same frame -> avformat
    image consumer). Returns per-frame path, ok flag and melt stderr on failure.
    """
    project = _abs(project)
    if not Path(project).is_file():
        return {"ok": False, "error": f"project not found: {project}"}
    if not timestamps:
        return {"ok": False, "error": "timestamps is required (list of seconds)"}
    melt, err = _melt_check()
    if err:
        return err
    av_err = _avformat_check()
    if av_err:
        return av_err

    info = inspect_project(project)
    fps = info.profile.get("fps") if info.ok else None
    out_dir = Path(output_dir or str(Path(project).parent / "frames"))
    out_dir.mkdir(parents=True, exist_ok=True)

    frames: list[dict[str, Any]] = []
    for i, ts in enumerate(timestamps):
        try:
            seconds = float(ts)
        except (TypeError, ValueError):
            frames.append(
                {"timestamp": ts, "ok": False, "error": "invalid timestamp"}
            )
            continue
        frame = int(round(seconds * fps)) if fps else int(seconds)
        out = out_dir / f"frame_{i:04d}.{format}"
        proc = run_command(
            [melt, project, "-in", str(frame), "-out", str(frame),
             "-consumer", f"avformat:{out}"]
        )
        ok = proc.get("returncode") == 0 and out.exists()
        stderr = (proc.get("stderr") or "").strip()
        frames.append(
            {
                "timestamp": seconds,
                "frame": frame,
                "path": str(out),
                "ok": ok,
                "returncode": proc.get("returncode"),
                "exists": out.exists(),
                "error": proc.get("error") or stderr[-200:] or None,
            }
        )
    return {
        "ok": all(f.get("ok") for f in frames),
        "project": project,
        "output_dir": str(out_dir),
        "frames": frames,
    }


@mcp.tool()
def kdenlive_validate_resources(project: str) -> dict[str, Any]:
    """Deep validation of every clip resource: existence, ffprobe streams
    (codec, resolution, fps, duration, audio) and consistency vs. the
    project profile. Read-only."""
    return validate_resources(_abs(project))


@mcp.tool()
def kdenlive_list_effects() -> dict[str, Any]:
    """List melt/MLT consumers, filters and producers (effects/transitions
    available for rendering)."""
    melt, err = _melt_check()
    if err:
        return err
    text = run_command([melt, "-query"]).get("stdout", "")
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.endswith(":") and line[:-1] in {"consumers", "filters", "producers"}:
            current = line[:-1]
            sections[current] = []
            continue
        if current and line.startswith("- "):
            sections[current].append(line[2:].strip())
    return {
        "ok": True,
        "consumers": sections.get("consumers", []),
        "filters": sections.get("filters", []),
        "producers": sections.get("producers", []),
    }


@mcp.tool()
def kdenlive_concatenate(
    output: str,
    projects: list[str] | None = None,
    preset: str | None = None,
    resolution: str | None = None,
    realtime: bool = False,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Render several .kdenlive projects sequentially into one output file.

    melt plays the listed projects in order; useful to join edits without
    merging XML. Returns the background render job (see render_status)."""
    output = _abs(output)
    projects = [_abs(p) for p in (projects or [])]
    if len(projects) < 2:
        return {"ok": False, "error": "concatenate needs at least 2 projects"}
    for p in projects:
        if not Path(p).is_file():
            return {"ok": False, "error": f"project not found: {p}"}
    melt, err = _melt_check()
    if err:
        return err
    av_err = _avformat_check()
    if av_err:
        return av_err
    Path(output).parent.mkdir(parents=True, exist_ok=True)

    profile_name, env_extra = _resolve_profile(None, preset, resolution)
    command = [melt] + projects + ["-progress"]
    if profile_name:
        command += ["-profile", profile_name]
    if realtime:
        command += ["-real_time", "0"]
    command += ["-consumer", f"avformat:{output}"]
    command += list(extra_args or [])

    job = start_job(
        command,
        project=" + ".join(projects),
        output=output,
        env=env_extra,
    )
    return {
        "ok": True,
        "job_id": job.job_id,
        "command": job.command,
        "output": output,
        "log": job.log_path,
    }


@mcp.tool()
def kdenlive_render_batch(
    project: str,
    outputs: list[dict[str, Any]] | None = None,
    preset: str | None = None,
    profile: str | None = None,
    resolution: str | None = None,
    zone: list[float] | None = None,
    realtime: bool = False,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Render one project into several outputs at once (one background job
    per output, each with its own preset/resolution/zone overrides).

    outputs: list of {"output": path, "preset"?: ..., "profile"?:
    ..., "resolution"?: ..., "zone"?: ..., "extra_args"?: ...}"""
    project = _abs(project)
    if not Path(project).is_file():
        return {"ok": False, "error": f"project not found: {project}"}
    results: list[dict[str, Any]] = []
    for spec in (outputs or []):
        out = _abs(spec.get("output", ""))
        res = render_start(
            project,
            out,
            profile=spec.get("profile", profile),
            preset=spec.get("preset", preset),
            resolution=spec.get("resolution", resolution),
            zone=spec.get("zone", zone),
            realtime=realtime,
            extra_args=spec.get("extra_args", extra_args),
        )
        results.append({"output": out, **res})
    return {
        "ok": all(r.get("ok") for r in results),
        "project": project,
        "jobs": results,
    }


@mcp.tool()
def kdenlive_merge_projects(
    output: str, projects: list[str] | None = None
) -> dict[str, Any]:
    """Merge several .kdenlive projects into one .kdenlive file (overlay
    composition: all producers/playlists/tractors pooled, ids renumbered,
    all timelines start at 0). Read-only inspection afterwards; render the
    result with render_start."""
    output = _abs(output)
    projects = [_abs(p) for p in (projects or [])]
    if len(projects) < 2:
        return {"ok": False, "error": "merge needs at least 2 projects"}
    for p in projects:
        if not Path(p).is_file():
            return {"ok": False, "error": f"project not found: {p}"}
    return merge_projects(output, projects)


@mcp.tool()
def jobs_list() -> list[dict[str, Any]]:
    """List all known render jobs (newest first) with status and progress."""
    return list_jobs()


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
