"""The project route (spec §3.3.8) and assembly from the -ltx parts (spec §4.2.4)."""
import json
import subprocess
import warnings

import pytest

from h3_48gb import assemble
from h3_48gb import project as p
from h3_48gb import queue as q
from test_web import _call, _serve


def test_default_routes_per_kind():
    assert p.default_route("video") == [
        {"stage": "scenario", "enabled": True}, {"stage": "scenes", "enabled": True},
        {"stage": "upscale", "enabled": True}, {"stage": "assemble", "enabled": True}]
    assert p.default_route("clip") == [
        {"stage": "scenario", "enabled": True}, {"stage": "scenes", "enabled": True},
        {"stage": "upscale", "enabled": True}, {"stage": "track", "enabled": True},
        {"stage": "assemble", "enabled": True}]
    assert p.default_route("song") == [{"stage": "track", "enabled": True}]


def _rewrite(proj, mutate):
    data = json.loads(proj.path.read_text())
    mutate(data)
    proj.path.write_text(json.dumps(data))


def test_an_old_project_without_route_gets_its_kind_default(tmp_path):
    proj = p.create_project(tmp_path, "clip", "Old")
    _rewrite(proj, lambda d: d.pop("route"))
    assert p.load_project(proj.path).route == p.default_route("clip")


@pytest.mark.parametrize("route", [
    [{"stage": "teleport", "enabled": True}],
    [{"stage": "track", "enabled": True}],          # track on a video project
    [{"stage": "upscale"}],                          # no `enabled`
    "upscale"])
def test_a_bad_route_is_a_load_error_not_a_silent_skip(tmp_path, route):
    proj = p.create_project(tmp_path, "video", "Bad")
    _rewrite(proj, lambda d: d.__setitem__("route", route))
    with pytest.raises(p.ProjectNotFound):
        p.load_project(proj.path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert p.list_projects(tmp_path) == []
    assert len(caught) == 1


def test_only_upscale_can_be_switched(tmp_path):
    proj = p.create_project(tmp_path, "video", "T")
    proj.set_route_stage("upscale", False)
    reloaded = p.load_project(proj.path)
    assert (reloaded.route_enabled("upscale"), reloaded.route_enabled("scenes")) == (False, True)
    with pytest.raises(p.ProjectError):
        proj.set_route_stage("scenes", False)


class _Job:
    def __init__(self, job_id):
        self.id = job_id


def _done_project(tmp_path, *, upscale_stage="draft"):
    proj = p.create_project(tmp_path / "out", "video", "Done")
    pdir = proj.path.parent
    scenes = []
    for i in range(2):
        clip = pdir / "scenes" / f"s{i}.mp4"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"raw")
        ltx = pdir / "scenes" / f"s{i}-ltx.mp4"
        ltx.write_bytes(b"ltx")
        scenes.append({"idx": i, "prompt": "x", "duration": 1.0, "status": "done",
                       "job_id": f"j{i}", "clip_path": str(clip), "keyframe_path": None,
                       "head_drop_frames": 0, "ltx_path": str(ltx)})
    proj.scenes = scenes
    proj.stages.update({"scenes": "done", "upscale": upscale_stage})
    proj.save()
    return proj


def _submits():
    calls = []

    def submit(root, args, note, report, estimate, kind):
        calls.append((args, kind))
        return _Job(f"j{len(calls)}")
    return calls, submit


def test_on_sglang_done_scenes_submit_one_upscale_job_then_assembly(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path)
    calls, submit = _submits()
    assert assemble.advance_project(proj, tmp_path / "q", tmp_path / "out", submit=submit) == \
        {"action": "submitted_upscale", "job_id": "j1"}
    assert calls == [(["upscale", "--project", str(proj.path)], q.KIND_UPSCALE)]
    assert p.load_project(proj.path).stages["upscale"] == "running"
    assert assemble.advance_project(proj, tmp_path / "q", tmp_path / "out", submit=submit) == \
        {"action": "nothing_to_do"}
    p.load_project(proj.path).set_stage_status("upscale", "done")
    assert assemble.advance_project(proj, tmp_path / "q", tmp_path / "out",
                                    submit=submit)["action"] == "submitted_assembly"
    assert calls[-1][1] == q.KIND_ASSEMBLE


def test_on_mlx_an_enabled_upscale_is_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("H3_ENGINE", raising=False)
    proj = _done_project(tmp_path)
    calls, submit = _submits()
    assert assemble.advance_project(proj, tmp_path / "q", tmp_path / "out",
                                    submit=submit)["action"] == "submitted_assembly"
    assert [kind for _, kind in calls] == [q.KIND_ASSEMBLE]


def test_a_disabled_upscale_goes_straight_to_assembly(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path)
    proj.set_route_stage("upscale", False)
    calls, submit = _submits()
    assert assemble.advance_project(proj, tmp_path / "q", tmp_path / "out",
                                    submit=submit)["action"] == "submitted_assembly"


def _assembled_inputs(proj, monkeypatch):
    seen = []

    def run(cmd, capture_output=True, text=True):
        if "concat" in cmd:
            listing = open(cmd[cmd.index("-i") + 1], encoding="utf-8").read()
            seen.append([line[len("file '"):-1] for line in listing.splitlines()])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assemble.run(proj.path, run=run, log=lambda line: None)
    return seen


def test_assembly_after_upscale_uses_the_ltx_parts(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path, upscale_stage="done")
    assert _assembled_inputs(proj, monkeypatch) == [[s["ltx_path"] for s in proj.scenes]]


def test_assembly_with_upscale_off_uses_the_raw_parts(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path, upscale_stage="done")
    proj.set_route_stage("upscale", False)
    assert _assembled_inputs(proj, monkeypatch) == [[s["clip_path"] for s in proj.scenes]]


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    outdir = tmp_path / "out"
    outdir.mkdir()
    server = _serve(q.layout(outdir / "queue")["root"], outdir)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def test_put_route_switches_upscale_and_continues_the_project(live, tmp_path):
    proj = _done_project(tmp_path)
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/route", {"upscale": False})
    assert status == 200, body
    assert body["project"]["route"][2] == {"stage": "upscale", "enabled": False}
    assert [job.kind for job in q.scan(live.queue_root)[0]] == ["assemble"]


def test_put_route_refuses_anything_but_upscale(live, tmp_path):
    proj = _done_project(tmp_path)
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/route", {"scenes": False})
    assert (status, body["error"]["code"]) == (400, "args_invalid")


def test_retry_a_failed_upscale(live, tmp_path):
    proj = _done_project(tmp_path, upscale_stage="failed")
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/upscale/retry", {})
    assert status == 200, body
    assert body["advance"]["action"] == "submitted_upscale"
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/upscale/retry", {})
    assert (status, body["error"]["code"]) == (409, "project_stage_not_ready")


def test_a_reshoot_after_upscale_invalidates_the_ltx_parts(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path, upscale_stage="done")
    proj.invalidate_scene_chain(1)
    reloaded = p.load_project(proj.path)
    assert reloaded.stages["upscale"] == "draft"
    assert ["ltx_path" in s for s in reloaded.scenes] == [True, False]
    clip = proj.path.parent / "scenes" / "s1-new.mp4"
    clip.write_bytes(b"raw2")
    reloaded.set_scene_status(1, "done", job_id=None, clip_path=str(clip))
    calls, submit = _submits()
    assert assemble.advance_project(reloaded, tmp_path / "q", tmp_path / "out",
                                    submit=submit)["action"] == "submitted_upscale"
    assert [kind for _, kind in calls] == [q.KIND_UPSCALE]


def test_draft_assembly_always_uses_the_raw_parts(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path, upscale_stage="done")
    seen = []

    def run(cmd, capture_output=True, text=True):
        if "concat" in cmd:
            listing = open(cmd[cmd.index("-i") + 1], encoding="utf-8").read()
            seen.append([line[len("file '"):-1] for line in listing.splitlines()])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    out = assemble.run(proj.path, run=run, log=lambda line: None, draft=True)
    reloaded = p.load_project(proj.path)
    assert out == proj.path.parent / "assembly" / "draft.mp4"
    assert seen == [[s["clip_path"] for s in proj.scenes]]
    assert (reloaded.assembly["draft_path"], reloaded.stages["assembly"]) == (str(out), "draft")


def test_draft_route_queues_a_draft_assembly(live, tmp_path):
    proj = _done_project(tmp_path, upscale_stage="running")
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/assembly/draft", {})
    assert status == 200, body
    (job,) = q.scan(live.queue_root)[0]
    assert (job.kind, job.args, body["job_id"]) == (
        q.KIND_ASSEMBLE, ["assemble", "--project", str(proj.path), "--draft"], job.id)

@pytest.mark.parametrize("stage", ["draft", "running", "failed"])
def test_a_final_assembly_never_takes_ltx_parts_before_the_upscale_is_done(
        tmp_path, monkeypatch, stage):
    """A failed retry leaves -ltx parts from an earlier attempt (maybe another strength) on some
    scenes: only `stages.upscale == "done"` makes them valid."""
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path, upscale_stage=stage)
    with pytest.raises(assemble.AssembleError, match="upscale"):
        _assembled_inputs(proj, monkeypatch)


def test_a_draft_assembly_ignores_the_upscale_stage_and_keeps_it(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path, upscale_stage="failed")
    out = assemble.run(proj.path, run=lambda cmd, capture_output=True, text=True:
                       subprocess.CompletedProcess(cmd, 0, "", ""),
                       log=lambda line: None, draft=True)
    assert out.name == "draft.mp4"
    assert p.load_project(proj.path).stages["upscale"] == "failed"


def test_a_failed_draft_assembly_does_not_fail_the_assembly_stage(tmp_path, monkeypatch):
    from h3_48gb import worker
    proj = _done_project(tmp_path)
    proj.set_stage_status("assembly", "done")

    class _Boom(Exception):
        pass

    def boom(cmd, capture_output=True, text=True):
        raise _Boom("ffmpeg died")

    monkeypatch.setattr(worker, "_tracked_child_run", lambda spawn: boom)
    monkeypatch.setattr(worker, "_caffeinate_block", lambda spawn=None: __import__("contextlib").nullcontext())
    job = type("J", (), {"args": ["assemble", "--project", str(proj.path), "--draft"]})()
    code, log = worker._run_assemble_job(job)
    assert code == 1 and "ffmpeg died" in log
    assert p.load_project(proj.path).stages["assembly"] == "done"
    job.args = ["assemble", "--project", str(proj.path)]
    code, _ = worker._run_assemble_job(job)
    assert code == 1
    assert p.load_project(proj.path).stages["assembly"] == "failed"


def test_draft_route_needs_every_scene_done(live, tmp_path):
    proj = _done_project(tmp_path)
    proj.set_scene_status(1, "pending", job_id=None)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/assembly/draft", {})
    assert (status, body["error"]["code"]) == (409, "project_stage_not_ready")


def test_the_route_is_in_the_project_payload_and_survives_a_save(tmp_path):
    proj = p.create_project(tmp_path, "clip", "R")
    assert proj.as_dict()["route"] == p.default_route("clip")
    proj.set_route_stage("upscale", False)
    proj.set_stage_status("scenes", "done")
    assert p.load_project(proj.path).route_enabled("upscale") is False



@pytest.mark.parametrize("stage", ["running", "done", "failed"])
def test_submit_upscale_fires_only_from_draft(tmp_path, stage):
    proj = _done_project(tmp_path, upscale_stage=stage)
    calls, submit = _submits()
    assert assemble._submit_upscale(proj, tmp_path / "q", submit=submit) == \
        {"action": "nothing_to_do"}
    assert calls == []


@pytest.mark.parametrize("extra,expected", [([], False), (["--draft"], True)])
def test_the_worker_passes_draft_to_assemble_run(tmp_path, monkeypatch, extra, expected):
    from h3_48gb import worker
    proj = p.create_project(tmp_path, "video", "W")
    seen = []
    monkeypatch.setattr(assemble, "run", lambda path, **kw: seen.append(kw["draft"]))
    monkeypatch.setattr(worker, "_caffeinate_block",
                        lambda spawn=None: __import__("contextlib").nullcontext())
    job = type("J", (), {"args": ["assemble", "--project", str(proj.path), *extra]})()
    assert worker._run_assemble_job(job)[0] == 0
    assert seen == [expected]


# ---- fix round 1 -------------------------------------------------------------------------------

def _fake_ltx(monkeypatch, on_part=None):
    """`ltx.run_upscale` with the ComfyUI/ffmpeg parts faked: every part becomes its -ltx sibling."""
    from h3_48gb.engines import ltx, motion
    monkeypatch.setattr(motion, "clip_motion", lambda clips, run: 0.0)
    monkeypatch.setattr(motion, "lora_for", lambda value: 0.3)

    def part(clip, **kw):
        if on_part:
            on_part(clip)
        return clip.with_name(f"{clip.stem}-ltx.mp4")

    monkeypatch.setattr(ltx, "upscale_part", part)
    return ltx


def _run_upscale(ltx, proj):
    return ltx.run_upscale(proj.path, client=None, comfy_output="x", run=None, attempt="a")


def test_assembly_refuses_an_ltx_part_that_is_not_the_sibling_of_the_current_clip(
        tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path, upscale_stage="done")
    new = proj.path.parent / "scenes" / "s1-new.mp4"      # scene 1 re-shot, old s1-ltx.mp4 kept
    new.write_bytes(b"raw2")
    proj.set_scene_fields(1, ltx_path=proj.scenes[1]["ltx_path"])
    data = json.loads(proj.path.read_text())
    data["scenes"][1]["clip_path"] = str(new)
    proj.path.write_text(json.dumps(data))
    with pytest.raises(assemble.AssembleError, match="not the -ltx part of the current clip"):
        _assembled_inputs(proj, monkeypatch)


def test_a_reshoot_during_the_upscale_never_gets_the_old_ltx_part(tmp_path, monkeypatch):
    """The repro: the upscale job holds the old clip list while the scene is re-shot."""
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path, upscale_stage="running")
    for i in range(2):
        proj.scenes[i].pop("ltx_path")
    proj.save()

    def reshoot(clip):
        if clip.name == "s1.mp4":                         # while scene 1 is being upscaled
            p.load_project(proj.path).invalidate_scene_chain(1)
            new = proj.path.parent / "scenes" / "s1-new.mp4"
            new.write_bytes(b"raw2")
            p.load_project(proj.path).set_scene_status(1, "done", job_id=None, clip_path=str(new))

    ltx = _fake_ltx(monkeypatch, reshoot)
    code, log = _run_upscale(ltx, proj)
    reloaded = p.load_project(proj.path)
    assert code == 0 and "переснята во время апскейла" in log
    assert reloaded.stages["upscale"] == "draft"
    assert "ltx_path" not in reloaded.scenes[1]
    calls, submit = _submits()
    assert assemble.advance_project(reloaded, tmp_path / "q", tmp_path / "out",
                                    submit=submit)["action"] == "submitted_upscale"


def test_a_reshoot_after_the_last_part_still_keeps_the_stage_out_of_done(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path, upscale_stage="running")
    for i in range(2):
        proj.scenes[i].pop("ltx_path")
    proj.save()

    def reshoot(clip):
        if clip.name == "s1.mp4":                         # the last part: reshoot lands after it
            monkeypatch.setattr(
                p.Project, "set_scene_ltx_if_current",
                lambda self, idx, clip_path, ltx_path: (
                    p.Project.set_scene_fields(self, idx, ltx_path=ltx_path),
                    p.load_project(proj.path).invalidate_scene_chain(1))[0] or True)

    ltx = _fake_ltx(monkeypatch, reshoot)
    _run_upscale(ltx, proj)
    assert p.load_project(proj.path).stages["upscale"] == "draft"


def test_a_scene_reshoot_cancels_the_pending_upscale_job(live, tmp_path, monkeypatch):
    proj = _done_project(tmp_path)
    from h3_48gb import assemble as asm
    asm.advance_project(proj, live.queue_root, tmp_path / "out")
    # the fake clips cannot yield a keyframe for the resubmitted scene; this test is the cancel
    monkeypatch.setattr(asm, "advance_project", lambda *a, **k: {"action": "nothing_to_do"})
    assert [j.kind for j in q.scan(live.queue_root)[0]] == ["upscale"]
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/scenes/1/retry", {})
    assert status == 200, body
    assert [j.kind for j in q.scan(live.queue_root)[0] if j.state == "pending"] == []


def test_a_scene_reshoot_asks_the_running_upscale_job_to_stop(live, tmp_path, monkeypatch):
    """Triage of tasks 10/11: the running part is interrupted, not finished for nothing."""
    proj = _done_project(tmp_path)
    from h3_48gb import assemble as asm
    asm.advance_project(proj, live.queue_root, tmp_path / "out")
    monkeypatch.setattr(asm, "advance_project", lambda *a, **k: {"action": "nothing_to_do"})
    job = q.claim(live.queue_root)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/scenes/1/retry", {})
    assert status == 200, body
    assert q.cancel_reason(live.queue_root, job.id) == "сцена переснята"


def _upscale_job(live, proj):
    from h3_48gb import assemble as asm
    asm.advance_project(proj, live.queue_root, live.outdir)
    (job,) = [j for j in q.scan(live.queue_root)[0] if j.kind == "upscale"]
    return job


def test_cancelling_a_pending_upscale_job_fails_the_stage(live, tmp_path):
    proj = _done_project(tmp_path)
    job = _upscale_job(live, proj)
    assert p.load_project(proj.path).stages["upscale"] == "running"
    status, body = _call(live, "DELETE", f"/api/jobs/{job.id}")
    assert status == 200, body
    assert p.load_project(proj.path).stages["upscale"] == "failed"


def test_early_exits_of_an_upscale_job_fail_the_stage(tmp_path, monkeypatch):
    from h3_48gb import worker
    proj = _done_project(tmp_path, upscale_stage="running")
    job = type("J", (), {"id": "j", "args": ["upscale", "--project", str(proj.path)]})()
    monkeypatch.setattr(worker, "make_gpu_gate", lambda *a, **k: (lambda j: "cancelled_by_user"))
    code, log = worker._run_upscale_job(tmp_path / "q", tmp_path / "out", job)
    assert code == 1 and "cancelled_by_user" in log
    assert p.load_project(proj.path).stages["upscale"] == "failed"
    proj.set_stage_status("upscale", "running")

    def engine_down(j):
        raise worker.GpuEngineFailed("no engine", "/log")

    monkeypatch.setattr(worker, "make_gpu_gate", lambda *a, **k: engine_down)
    assert worker._run_upscale_job(tmp_path / "q", tmp_path / "out", job)[0] == 1
    assert p.load_project(proj.path).stages["upscale"] == "failed"


def _final_done(proj):
    proj.set_stage_status("assembly", "done")
    proj.update_assembly(final_path=str(proj.path.parent / "assembly" / "final.mp4"))


def test_switching_upscale_on_after_a_final_rebuilds_it_from_ltx(live, tmp_path):
    proj = _done_project(tmp_path)
    proj.set_route_stage("upscale", False)
    _final_done(proj)
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/route", {"upscale": True})
    assert status == 200, body
    reloaded = p.load_project(proj.path)
    assert (reloaded.stages["assembly"], reloaded.assembly["final_path"]) == ("draft", None)
    assert [j.kind for j in q.scan(live.queue_root)[0]] == ["upscale"]
    reloaded.set_stage_status("upscale", "done")
    from h3_48gb import assemble as asm
    assert asm.advance_project(reloaded, live.queue_root, live.outdir)["action"] == \
        "submitted_assembly"
    assert _assembled_inputs(reloaded, None) == [[s["ltx_path"] for s in reloaded.scenes]]


def test_switching_upscale_off_after_a_final_rebuilds_it_from_the_raw_parts(live, tmp_path):
    proj = _done_project(tmp_path, upscale_stage="done")
    _final_done(proj)
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/route", {"upscale": False})
    assert status == 200, body
    assert [j.kind for j in q.scan(live.queue_root)[0]] == ["assemble"]
    reloaded = p.load_project(proj.path)
    assert reloaded.assembly["final_path"] is None
    assert _assembled_inputs(reloaded, None) == [[s["clip_path"] for s in reloaded.scenes]]


@pytest.mark.parametrize("route", [
    [],
    [{"stage": "scenario", "enabled": True}, {"stage": "scenes", "enabled": True},
     {"stage": "assemble", "enabled": True}],                       # no upscale
    [{"stage": "scenario", "enabled": True}, {"stage": "scenes", "enabled": True},
     {"stage": "upscale", "enabled": True}, {"stage": "upscale", "enabled": True},
     {"stage": "assemble", "enabled": True}],                       # duplicate
    [{"stage": "scenes", "enabled": True}, {"stage": "scenario", "enabled": True},
     {"stage": "upscale", "enabled": True}, {"stage": "assemble", "enabled": True}]])  # order
def test_a_route_must_be_exactly_its_kinds_stages_in_order(tmp_path, route):
    proj = p.create_project(tmp_path, "video", "Bad")
    _rewrite(proj, lambda d: d.__setitem__("route", route))
    with pytest.raises(p.ProjectNotFound):
        p.load_project(proj.path)


# ---- fix round 2 -------------------------------------------------------------------------------

def test_a_job_claimed_before_a_reshoot_does_nothing_and_advance_requeues(tmp_path, monkeypatch):
    """gate_race: the retry lands while the claimed upscale job waits at the GPU gate."""
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path, upscale_stage="running")
    for i in range(2):
        proj.scenes[i].pop("ltx_path")
    proj.save()
    p.load_project(proj.path).invalidate_scene_chain(1)
    ltx = _fake_ltx(monkeypatch)
    code, log = _run_upscale(ltx, proj)
    assert code == 0 and "сброшен после постановки" in log
    assert p.load_project(proj.path).stages["upscale"] == "draft"
    new = proj.path.parent / "scenes" / "s1-new.mp4"
    new.write_bytes(b"raw2")
    p.load_project(proj.path).set_scene_status(1, "done", job_id=None, clip_path=str(new))
    calls, submit = _submits()
    assert assemble.advance_project(p.load_project(proj.path), tmp_path / "q", tmp_path / "out",
                                    submit=submit)["action"] == "submitted_upscale"


def test_a_failure_on_stale_clips_leaves_draft_not_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path, upscale_stage="running")
    for i in range(2):
        proj.scenes[i].pop("ltx_path")
    proj.save()

    def reshoot_then_die(clip):
        p.load_project(proj.path).invalidate_scene_chain(1)
        raise ltx_module.UpscaleError("ComfyUI: OOM")

    from h3_48gb.engines import ltx as ltx_module
    ltx = _fake_ltx(monkeypatch, reshoot_then_die)
    code, log = _run_upscale(ltx, proj)
    assert code == 1 and "OOM" in log
    assert p.load_project(proj.path).stages["upscale"] == "draft"


def test_a_failure_on_current_clips_still_fails_the_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path, upscale_stage="running")
    from h3_48gb.engines import ltx as ltx_module

    def die(clip):
        raise ltx_module.UpscaleError("ComfyUI: OOM")

    ltx = _fake_ltx(monkeypatch, die)
    assert _run_upscale(ltx, proj)[0] == 1
    assert p.load_project(proj.path).stages["upscale"] == "failed"
