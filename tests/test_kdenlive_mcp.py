import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kdenlive_mcp import jobs, project

FIXTURE = Path(__file__).parent / "fixtures" / "demo.kdenlive"


def test_inspect_fixture():
    info = project.inspect_project(str(FIXTURE))
    assert info.ok, info.error
    assert info.mlt_version == "7.22.0"
    assert info.profile["width"] == 1920
    assert info.duration_frames == 6000
    assert abs(info.duration_seconds - 6000 * 1001 / 30000) < 0.01
    assert info.counts["producers"] == 3
    titles = [t["title"] for t in info.tracks]
    assert "Video 1" in titles and "Audio 1" in titles
    resources = {r["path"] for r in info.resources}
    assert "/home/user/videos/interview.mp4" in resources
    assert "/home/user/videos/broll_missing.mp4" in resources
    # color:#112233 is not a file
    assert len(info.resources) == 2
    assert len(info.missing_resources) == 2


def test_existing_resource_not_flagged(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00")
    proj = tmp_path / "p.kdenlive"
    proj.write_text(
        "<mlt mlt_version=\"7.22.0\">"
        "<profile frame_rate_num=\"25\" frame_rate_den=\"1\" width=\"640\" height=\"360\"/>"
        f"<producer id=\"producer0\" out=\"99\"><property name=\"resource\">{clip}</property></producer>"
        "</mlt>"
    )
    info = project.inspect_project(str(proj))
    assert info.ok
    assert info.resources[0]["exists"] is True
    assert info.missing_resources == []
    assert info.duration_seconds == 4.0


def test_missing_file():
    info = project.inspect_project("/nonexistent/x.kdenlive")
    assert not info.ok and "not found" in info.error


def test_bad_xml(tmp_path):
    bad = tmp_path / "bad.kdenlive"
    bad.write_text("<mlt><oops></mlt>")
    info = project.inspect_project(str(bad))
    assert not info.ok and "parse" in info.error


def test_environment_report():
    report = jobs.environment_report()
    assert "melt" in report and "kdenlive" in report
    assert isinstance(report["ready"], bool)


def test_fake_job_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("KDENLIVE_MCP_HOME", str(tmp_path / "state"))
    jobs.JOBS_DIR = Path(tmp_path / "state" / "jobs")
    out = tmp_path / "out.mp4"
    job = jobs.start_job(
        ["bash", "-c", f"echo '50%'; sleep 0.3; echo '100%'; touch {out}"],
        project="p.kdenlive",
        output=str(out),
    )
    for _ in range(50):
        status = jobs.job_status(job.job_id)
        if not status["running"]:
            break
        time.sleep(0.1)
    assert status["running"] is False
    assert status["returncode"] == 0
    assert status["output_exists"] is True
    assert status["progress"]["percent"] is not None
    assert any(j["job_id"] == job.job_id for j in jobs.list_jobs())


def test_stop_job(tmp_path, monkeypatch):
    monkeypatch.setenv("KDENLIVE_MCP_HOME", str(tmp_path / "state"))
    jobs.JOBS_DIR = Path(tmp_path / "state" / "jobs")
    job = jobs.start_job(["bash", "-c", "sleep 30"], project="p", output="o")
    time.sleep(0.2)
    res = jobs.stop_job(job.job_id)
    assert res.get("ok") is True
    time.sleep(0.7)
    status = jobs.job_status(job.job_id)
    assert status["running"] is False


def test_render_start_with_stub_melt(tmp_path, monkeypatch):
    from kdenlive_mcp import server

    bindir = tmp_path / "bin"
    bindir.mkdir()
    melt = bindir / "melt"
    melt.write_text(
        "#!/bin/bash\n"
        "out=\"\"\n"
        "for a in \"$@\"; do case \"$a\" in avformat:*) out=\"${a#avformat:}\";; esac; done\n"
        "echo \"0%\"; sleep 0.2; echo \"60%\"; sleep 0.2; echo \"100%\"\n"
        "printf 'fake video' > \"$out\"\n"
    )
    melt.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("KDENLIVE_MCP_HOME", str(tmp_path / "state"))
    jobs.JOBS_DIR = Path(tmp_path / "state" / "jobs")

    out = tmp_path / "render.mp4"
    res = server.render_start(str(FIXTURE), str(out))
    assert res["ok"] is True, res
    assert "melt" in res["command"][0]

    for _ in range(50):
        status = server.render_status(res["job_id"])
        if not status["running"]:
            break
        time.sleep(0.1)
    assert status["running"] is False
    assert status["returncode"] == 0
    assert status["output_exists"] is True
    assert status["progress"]["percent"] == 100.0
    assert server.kdenlive_env()["ready"] is True


def test_progress_parses_melt_current_frame(tmp_path):
    from kdenlive_mcp import jobs

    log = tmp_path / "render.log"
    log.write_text("Current Frame:        49, percentage:         98\n")
    prog = jobs._progress(str(log))
    assert prog["percent"] == 98.0
    assert prog["frames"] == "frame 49"
