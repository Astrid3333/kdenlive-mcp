import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kdenlive_mcp import jobs, project
from kdenlive_mcp import server

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
    monkeypatch.setenv("KDENLIVE_MCP_FORCE_RENDER", "1")
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
    monkeypatch.setenv("KDENLIVE_MCP_FORCE_RENDER", "1")
    jobs.JOBS_DIR = Path(tmp_path / "state" / "jobs")
    job = jobs.start_job(["bash", "-c", "sleep 30"], project="p", output="o")
    time.sleep(0.2)
    res = jobs.stop_job(job.job_id)
    assert res.get("ok") is True
    assert res.get("running_after_signal") is False
    time.sleep(0.7)
    status = jobs.job_status(job.job_id)
    assert status["running"] is False


def test_stop_already_finished_job(tmp_path, monkeypatch):
    monkeypatch.setenv("KDENLIVE_MCP_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("KDENLIVE_MCP_FORCE_RENDER", "1")
    jobs.JOBS_DIR = Path(tmp_path / "state" / "jobs")
    job = jobs.start_job(["bash", "-c", "true"], project="p", output="o")
    for _ in range(50):
        if not jobs.job_status(job.job_id)["running"]:
            break
        time.sleep(0.1)
    res = jobs.stop_job(job.job_id)
    assert res.get("ok") is True
    assert res.get("stopped") is False
    assert "not running" in res.get("note", "")


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
    monkeypatch.setenv("KDENLIVE_MCP_FORCE_RENDER", "1")
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
    env = server.kdenlive_env()
    assert isinstance(env["ready"], bool)
    assert env["melt"]["path"] is not None


def test_progress_parses_melt_current_frame(tmp_path):
    from kdenlive_mcp import jobs

    log = tmp_path / "render.log"
    log.write_text("Current Frame:        49, percentage:         98\n")
    prog = jobs._progress(str(log))
    assert prog["percent"] == 98.0
    assert prog["frames"] == "frame 49"


def test_render_refuses_when_avformat_broken(tmp_path, monkeypatch):
    from kdenlive_mcp import server

    monkeypatch.setattr(server, "_avformat_ok", lambda: (False, "libopenjp2.so.7: invalid ELF header"))
    res = server.render_start(str(FIXTURE), str(tmp_path / "x.mp4"))
    assert res["ok"] is False
    assert "libavformat" in res["error"]
    assert "reinstall" in res["hint"]


def test_env_reports_avformat(tmp_path, monkeypatch):
    from kdenlive_mcp import jobs

    monkeypatch.setattr(jobs, "_avformat_ok", lambda: (False, "boom"))
    rep = jobs.environment_report()
    assert rep["avformat"] == {"ok": False, "error": "boom"}
    assert rep["ready"] is False
    assert "libopenjp2" in rep["hint"]


def _stub_melt(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    melt = bindir / "melt"
    melt.write_text("#!/bin/bash\nexit 0\n")
    melt.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("KDENLIVE_MCP_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("KDENLIVE_MCP_FORCE_RENDER", "1")
    jobs.JOBS_DIR = Path(tmp_path / "state" / "jobs")


def _capturing_start_job(monkeypatch):
    captured = {}

    def fake(command, project, output, env=None):
        captured["command"] = list(command)
        captured["env"] = env
        return jobs.Job(
            job_id="cap", project=project, output=output, command=command, created="now"
        )

    monkeypatch.setattr(server, "start_job", fake)
    return captured


def test_render_start_zone(tmp_path, monkeypatch):
    _stub_melt(monkeypatch, tmp_path)
    captured = _capturing_start_job(monkeypatch)
    out = tmp_path / "out.mp4"
    res = server.render_start(str(FIXTURE), str(out), zone=[1.0, 2.0])
    assert res["ok"] is True, res
    cmd = captured["command"]
    assert "-in" in cmd and "-out" in cmd
    i = cmd.index("-in")
    assert cmd[i + 1] == "30"  # 1.0 s * 29.97 fps -> 30
    i = cmd.index("-out")
    assert cmd[i + 1] == "60"  # 2.0 s * 29.97 fps -> 60


def test_render_start_preset(tmp_path, monkeypatch):
    _stub_melt(monkeypatch, tmp_path)
    captured = _capturing_start_job(monkeypatch)
    res = server.render_start(str(FIXTURE), str(tmp_path / "out.mp4"), preset="hd1080p25")
    assert res["ok"] is True, res
    cmd = captured["command"]
    i = cmd.index("-profile")
    assert cmd[i + 1] == "hdv_1080_25i"


def test_render_start_resolution(tmp_path, monkeypatch):
    _stub_melt(monkeypatch, tmp_path)
    captured = _capturing_start_job(monkeypatch)
    res = server.render_start(str(FIXTURE), str(tmp_path / "out.mp4"), resolution="1280x720")
    assert res["ok"] is True, res
    cmd = captured["command"]
    i = cmd.index("-profile")
    assert cmd[i + 1].startswith("kdenlive_custom_1280x720")
    env = captured["env"]
    assert env is not None
    assert Path(env["HOME"], ".mlt", "profiles", "kdenlive_custom_1280x720").is_file()


def test_render_batch(tmp_path, monkeypatch):
    _stub_melt(monkeypatch, tmp_path)
    captured = []

    def fake(command, project, output, env=None):
        captured.append((list(command), output))
        return jobs.Job(
            job_id=f"j{len(captured)}",
            project=project,
            output=output,
            command=command,
            created="now",
        )

    monkeypatch.setattr(server, "start_job", fake)
    res = server.kdenlive_render_batch(
        str(FIXTURE),
        outputs=[
            {"output": str(tmp_path / "a.mp4"), "preset": "720p"},
            {"output": str(tmp_path / "b.mp4"), "zone": [0.0, 1.0]},
        ],
    )
    assert res["ok"] is True, res
    assert len(res["jobs"]) == 2
    assert res["jobs"][0]["output"] == str(tmp_path / "a.mp4")
    assert res["jobs"][1]["output"] == str(tmp_path / "b.mp4")
    assert res["jobs"][0]["job_id"] != res["jobs"][1]["job_id"]
    cmd0 = captured[0][0]
    i = cmd0.index("-profile")
    assert cmd0[i + 1].startswith("kdenlive_custom_1280x720")
    cmd1 = captured[1][0]
    assert "-in" in cmd1


def test_concatenate(tmp_path, monkeypatch):
    _stub_melt(monkeypatch, tmp_path)
    captured = {}

    def fake(command, project, output, env=None):
        captured["command"] = list(command)
        captured["output"] = output
        return jobs.Job(
            job_id="j", project=project, output=output, command=command, created="now"
        )

    monkeypatch.setattr(server, "start_job", fake)
    p1 = tmp_path / "a.kdenlive"
    p2 = tmp_path / "b.kdenlive"
    p1.write_text(FIXTURE.read_text())
    p2.write_text(FIXTURE.read_text())
    out = tmp_path / "final.mp4"
    res = server.kdenlive_concatenate(str(out), projects=[str(p1), str(p2)])
    assert res["ok"] is True, res
    cmd = captured["command"]
    assert cmd[0].endswith("melt")
    assert str(p1) in cmd and str(p2) in cmd
    assert cmd.index(str(p1)) < cmd.index(str(p2))
    i = cmd.index("-consumer")
    assert cmd[i + 1] == f"avformat:{captured['output']}"
    assert captured["output"] == str(out)


def test_extract_frames(tmp_path, monkeypatch):
    _stub_melt(monkeypatch, tmp_path)
    captured = []

    def fake_run(command, timeout=300, env=None):
        captured.append(list(command))
        for a in command:
            if a.startswith("avformat:"):
                Path(a.split(":", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
                Path(a.split(":", 1)[1]).write_bytes(b"fake frame")
        return {"returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(server, "run_command", fake_run)
    monkeypatch.setattr(server, "_avformat_ok", lambda: (True, None))
    outdir = tmp_path / "frames"
    res = server.kdenlive_extract_frames(
        str(FIXTURE), timestamps=[1.0, 2.5], output_dir=str(outdir), format="png"
    )
    assert res["ok"] is True, res
    assert len(res["frames"]) == 2
    assert (outdir / "frame_0000.png").is_file()
    assert (outdir / "frame_0001.png").is_file()
    cmd0 = captured[0]
    assert "-in" in cmd0 and "-out" in cmd0
    i = cmd0.index("-in")
    assert cmd0[i + 1] == "30"  # 1.0 s * 29.97 fps -> 30
    cmd1 = captured[1]
    i = cmd1.index("-in")
    assert cmd1[i + 1] == "75"  # 2.5 s * 29.97 fps -> 75


def test_validate_resources(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 100)
    proj = tmp_path / "p.kdenlive"
    proj.write_text(
        "<mlt mlt_version=\"7.22.0\">"
        "<profile frame_rate_num=\"25\" frame_rate_den=\"1\" width=\"1920\" height=\"1080\"/>"
        f"<producer id=\"producer0\" out=\"24\"><property name=\"resource\">{clip}</property>"
        "<property name=\"mlt_service\">avformat</property></producer>"
        "</mlt>"
    )
    monkeypatch.setattr(
        jobs,
        "ffprobe",
        lambda p: {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "r_frame_rate": "25/1",
                    "duration": "10.0",
                }
            ]
        },
    )
    res = server.kdenlive_validate_resources(str(proj))
    assert res["ok"] is True, res
    r = res["resources"][0]
    assert r["exists"] is True
    assert r["checks"]["resolution_match"] is True
    assert r["checks"]["fps_match"] is True
    assert len(r["streams"]) == 1


def test_validate_resources_missing(tmp_path, monkeypatch):
    proj = tmp_path / "p.kdenlive"
    proj.write_text(
        "<mlt mlt_version=\"7.22.0\">"
        "<profile frame_rate_num=\"25\" frame_rate_den=\"1\" width=\"640\" height=\"360\"/>"
        "<producer id=\"producer0\" out=\"9\">"
        "<property name=\"resource\">/tmp/does-not-exist.mp4</property>"
        "</producer>"
        "</mlt>"
    )
    monkeypatch.setattr(jobs, "ffprobe", lambda p: {"streams": []})
    res = server.kdenlive_validate_resources(str(proj))
    assert res["ok"] is True
    assert res["resources"][0]["exists"] is False
    assert res["resources"][0]["error"] == "missing file"


def test_list_effects(tmp_path, monkeypatch):
    _stub_melt(monkeypatch, tmp_path)

    def fake_run(command, timeout=300, env=None):
        assert command[-1] == "-query"
        return {
            "returncode": 0,
            "stdout": (
                "consumers:\n- null\n- xml\n"
                "filters:\n- oldfilm\n- dust\n"
                "producers:\n- avformat\n- color\n"
            ),
            "stderr": "",
        }

    monkeypatch.setattr(server, "run_command", fake_run)
    res = server.kdenlive_list_effects()
    assert res["ok"] is True
    assert res["consumers"] == ["null", "xml"]
    assert res["filters"] == ["oldfilm", "dust"]
    assert res["producers"] == ["avformat", "color"]


def test_merge_projects(tmp_path):
    def mini(path, n_producers):
        path.write_text(
            "<mlt mlt_version=\"7.22.0\">"
            "<profile frame_rate_num=\"25\" frame_rate_den=\"1\" width=\"640\" height=\"360\"/>"
            + "".join(
                f'<producer id="producer{i}" out="9"/>' for i in range(n_producers)
            )
            + "<playlist id=\"playlist0\"><entry producer=\"producer0\"/></playlist>"
            + "<tractor id=\"tractor0\"><track producer=\"playlist0\"/></tractor>"
            + "</mlt>"
        )

    p1 = tmp_path / "a.kdenlive"
    p2 = tmp_path / "b.kdenlive"
    mini(p1, 2)
    mini(p2, 3)
    out = tmp_path / "merged.kdenlive"
    res = server.kdenlive_merge_projects(str(out), projects=[str(p1), str(p2)])
    assert res["ok"] is True, res
    assert res["projects"] == 2
    assert res["producers"] == 5
    assert res["playlists"] == 2
    assert res["tractors"] == 2
    tree = ET.parse(str(out))
    ids = [e.get("id") for e in tree.getroot() if e.tag.endswith("producer")]
    assert len(ids) == 5
    assert len(set(ids)) == 5
    info = project.inspect_project(str(out))
    assert info.ok
