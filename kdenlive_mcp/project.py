"""Read-only inspection of Kdenlive (.kdenlive) project files."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

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
