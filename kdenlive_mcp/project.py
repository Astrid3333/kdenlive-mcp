"""Read-only inspection of Kdenlive (.kdenlive) project files."""

from __future__ import annotations

import copy
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import jobs

# Resource values that are not real files on disk.
NON_FILE_RESOURCES = {
    "",
    "blank",
    "color",
    "colour",
    "qtext",
    "avformat-novalidate",
    "frei0r.c0ler",
    "qtext:#00000000",
}


@dataclass
class ProjectInfo:
    path: str
    ok: bool = True
    error: str | None = None
    mlt_version: str | None = None
    profile: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    tracks: list[dict[str, Any]] = field(default_factory=list)
    resources: list[dict[str, Any]] = field(default_factory=list)
    missing_resources: list[str] = field(default_factory=list)
    duration_frames: int | None = None
    duration_seconds: float | None = None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _coerce(value: str) -> Any:
    if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
        return int(value)
    try:
        return float(value)
    except ValueError:
        return value


def _properties(element: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for child in element:
        if _local(child.tag) == "property":
            name = child.get("name")
            if name:
                out[name] = (child.text or "").strip()
    return out


def _fps(profile: dict[str, Any]) -> float:
    try:
        num = float(profile.get("frame_rate_num", 0))
        den = float(profile.get("frame_rate_den", 1)) or 1.0
    except (TypeError, ValueError):
        return 0.0
    return num / den if num else 0.0


def _extract_path(resource: str) -> str | None:
    """Pull a filesystem path out of an MLT resource string, if there is one."""
    if resource.startswith("/"):
        return resource
    if ":" not in resource:
        return None
    tail = resource.split(":", 1)[1]
    if tail.startswith("/"):
        return tail
    for part in tail.split(":"):
        if part.startswith("/"):
            return part
    return None


def inspect_project(path: str) -> ProjectInfo:
    info = ProjectInfo(path=path)
    if not os.path.isfile(path):
        info.ok = False
        info.error = f"file not found: {path}"
        return info
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        info.ok = False
        info.error = f"XML parse error: {exc}"
        return info

    root = tree.getroot()
    if _local(root.tag) != "mlt":
        info.ok = False
        info.error = f"unexpected root element <{_local(root.tag)}>, expected <mlt>"
        return info

    info.mlt_version = root.get("mlt_version")

    for child in root:
        tag = _local(child.tag)
        if tag == "profile":
            info.profile = {k: _coerce(v) for k, v in child.attrib.items()}
            if info.profile.get("frame_rate_den"):
                info.profile["fps"] = _fps(info.profile)
        elif tag == "producer":
            info.counts["producers"] = info.counts.get("producers", 0) + 1
        elif tag == "playlist":
            info.counts["playlists"] = info.counts.get("playlists", 0) + 1
        elif tag == "tractor":
            info.counts["tractors"] = info.counts.get("tractors", 0) + 1
        elif tag == "chain":
            info.counts["chains"] = info.counts.get("chains", 0) + 1

    for child in root:
        tag = _local(child.tag)
        if tag not in {"playlist", "tractor"}:
            continue
        props = _properties(child)
        title = props.get("title") or child.get("title") or child.get("id", "")
        hidden = props.get("hide") == "1"
        info.tracks.append(
            {
                "id": child.get("id", ""),
                "type": tag,
                "title": title,
                "hidden": hidden,
            }
        )

    max_out = 0
    for child in root:
        tag = _local(child.tag)
        if tag not in {"producer", "chain"}:
            continue
        props = _properties(child)
        resource = props.get("resource", "")
        try:
            out = int(child.get("out") or 0)
        except ValueError:
            out = 0
        max_out = max(max_out, out)
        path = _extract_path(resource) if resource.lower() not in NON_FILE_RESOURCES else None
        if path:
            info.resources.append(
                {
                    "resource": resource,
                    "producer_id": child.get("id", ""),
                    "path": path,
                    "exists": os.path.exists(path),
                }
            )

    seen: set[str] = set()
    for res in info.resources:
        p = res.get("path", "")
        if p.startswith("/") and not res.get("exists", True) and p not in seen:
            seen.add(p)
            info.missing_resources.append(p)

    info.duration_frames = (max_out + 1) if max_out else None
    rate = _fps(info.profile)
    if max_out and rate:
        info.duration_seconds = round((max_out + 1) / rate, 3)
    return info


def _parse_fps(rate: str | None) -> float | None:
    if not rate:
        return None
    try:
        if "/" in rate:
            num, den = rate.split("/", 1)
            return float(num) / float(den)
        return float(rate)
    except (ValueError, ZeroDivisionError):
        return None


def validate_resources(path: str) -> dict[str, Any]:
    """Deep validation of every clip resource: existence, ffprobe streams,
    and consistency vs. the project profile (resolution / frame rate)."""
    info = inspect_project(path)
    report: dict[str, Any] = {
        "project": path,
        "ok": info.ok,
        "error": info.error,
        "profile": dict(info.profile),
        "resources": [],
    }
    if not info.ok:
        return report

    width = info.profile.get("width")
    height = info.profile.get("height")
    fps = info.profile.get("fps")
    for res in info.resources:
        entry: dict[str, Any] = {
            "path": res.get("path"),
            "producer_id": res.get("producer_id"),
            "resource": res.get("resource"),
            "exists": bool(res.get("exists")),
        }
        if not res.get("exists"):
            entry["error"] = "missing file"
            report["resources"].append(entry)
            continue

        probe = jobs.ffprobe(res["path"])
        if probe is None:
            entry["error"] = "ffprobe unavailable or failed"
            report["resources"].append(entry)
            continue

        streams = []
        for stream in probe.get("streams", []):
            streams.append(
                {
                    "codec_type": stream.get("codec_type"),
                    "codec_name": stream.get("codec_name"),
                    "width": stream.get("width"),
                    "height": stream.get("height"),
                    "r_frame_rate": stream.get("r_frame_rate"),
                    "duration": stream.get("duration"),
                    "sample_rate": stream.get("sample_rate"),
                    "channels": stream.get("channels"),
                }
            )
        entry["streams"] = streams

        checks: dict[str, Any] = {}
        warnings: list[str] = []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video and width is not None and height is not None:
            vw, vh = video.get("width"), video.get("height")
            if vw is None or vh is None or int(vw) != int(width) or int(vh) != int(height):
                checks["resolution_match"] = False
                warnings.append(
                    f"video {vw}x{vh} != project {width}x{height}"
                )
            else:
                checks["resolution_match"] = True
            rate = _parse_fps(video.get("r_frame_rate"))
            if fps and rate and abs(rate - float(fps)) > 0.5:
                checks["fps_match"] = False
                warnings.append(f"fps {rate} != project {fps}")
            else:
                checks["fps_match"] = True
        entry["checks"] = checks
        entry["warnings"] = warnings
        report["resources"].append(entry)
    return report


def merge_projects(output: str, projects: list[str]) -> dict[str, Any]:
    """Merge several .kdenlive projects into one (overlay composition).

    All producers, playlists and tractors are pooled, ids are renumbered to
    avoid collisions and cross-references (entry.producer, track.producer)
    are re-pointed. Every timeline starts playing at 0, so the result is a
    superposition of the inputs, not a sequential concat. Use
    kdenlive_concatenate for sequential render."""
    if len(projects) < 2:
        return {"ok": False, "error": "merge needs at least 2 projects"}

    parsed = []
    for p in projects:
        info = inspect_project(p)
        if not info.ok:
            return {"ok": False, "error": f"{p}: {info.error}"}
        parsed.append(ET.parse(p))

    output = str(Path(output).expanduser().resolve())
    Path(output).parent.mkdir(parents=True, exist_ok=True)

    first_root = parsed[0].getroot()
    new_root = ET.Element("mlt")
    for attr in ("version", "mlt_version", "title", "root"):
        value = first_root.get(attr)
        if value:
            new_root.set(attr, value)
    new_root.set("title", "kdenlive-mcp-merge")

    for child in first_root:
        if _local(child.tag) == "profile":
            new_root.append(copy.deepcopy(child))
            break

    producer_map: dict[str, str] = {}
    playlist_map: dict[str, str] = {}
    counter = {"p": 0, "pl": 0, "t": 0}

    def new_id(kind: str) -> str:
        counter[kind] += 1
        return f"{kind}{counter[kind]}"

    for tree in parsed:
        root = tree.getroot()
        for child in root:
            tag = _local(child.tag)
            if tag == "producer":
                old = child.get("id", "")
                nid = new_id("p")
                producer_map[old] = nid
                child.set("id", nid)
                new_root.append(child)
            elif tag == "playlist":
                old = child.get("id", "")
                nid = new_id("pl")
                playlist_map[old] = nid
                child.set("id", nid)
                for entry in child:
                    if _local(entry.tag) == "entry":
                        oldp = entry.get("producer", "")
                        if oldp in producer_map:
                            entry.set("producer", producer_map[oldp])
                new_root.append(child)
            elif tag == "tractor":
                child.set("id", new_id("t"))
                for track in child:
                    if _local(track.tag) == "track":
                        oldpl = track.get("producer", "")
                        if oldpl in playlist_map:
                            track.set("producer", playlist_map[oldpl])
                new_root.append(child)

    ET.ElementTree(new_root).write(output, xml_declaration=True, encoding="utf-8")
    merged = inspect_project(output)
    return {
        "ok": merged.ok,
        "error": merged.error,
        "output": output,
        "projects": len(projects),
        "producers": merged.counts.get("producers", 0),
        "playlists": merged.counts.get("playlists", 0),
        "tractors": merged.counts.get("tractors", 0),
        "profile": dict(merged.profile),
    }
