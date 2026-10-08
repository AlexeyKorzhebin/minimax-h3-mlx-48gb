"""Wave 1.5, spec §5.6: «Пересчитать сцену» may carry a new prompt, seed and steps; they are
checked like `PUT /scenes`, stored with the invalidation in one write, and a refusal changes
nothing -- neither the project nor the queue."""
import pytest

from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import queue as q
from test_web import _call, _pending, _serve

PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    outdir = tmp_path / "outdir"
    (outdir / "uploads").mkdir(parents=True)
    (outdir / "uploads" / "face.png").write_bytes(PNG)
    lib.create_card(outdir, tag="@amazon", kind="person", description="an armored amazon",
                    assets=[outdir / "uploads" / "face.png"])
    server = _serve(q.layout(outdir / "queue")["root"], outdir)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def _shot_scene_0(live):
    """Two scenes approved; scene 0 marked done with a clip, its queued job left in place, and a
    pending upscale job of the same project (the retry cancels it -- a refusal must not)."""
    proj = p.create_project(live.outdir, "video", "Бой")
    assert _call(live, "PUT", f"/api/projects/{proj.id}/scenes", {
        "scenes": [{"prompt": "@amazon walks", "duration": 8.0},
                   {"prompt": "@amazon runs", "duration": 8.0}],
        "references": [{"tag": "@amazon"}]})[0] == 200
    assert _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})[0] == 200
    clip = proj.path.parent / "scenes" / "s0.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"mp4")
    p.load_project(proj.path).set_scene_status(0, "done", job_id=None, clip_path=str(clip))
    q.submit(live.queue_root, ["upscale", "--project", str(proj.path)], f"upscale project {proj.id}",
             {"output_stem": str(proj.path.parent / "upscale" / "job-upscale")}, {},
             kind=q.KIND_UPSCALE)
    return proj


def _arg(args, flag):
    return args[args.index(flag) + 1]


def test_retry_with_a_new_seed_and_prompt_rewrites_the_scene_and_resubmits(live):
    proj = _shot_scene_0(live)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/scenes/0/retry",
                         {"prompt": "@amazon jumps", "seed": 7, "steps": 30})
    assert status == 200, body
    scene = p.load_project(proj.path).scenes[0]
    assert (scene["prompt"], scene["seed"], scene["steps"], scene["status"]) == (
        "@amazon jumps", 7, 30, "running")
    (job,) = [j for j in _pending(live) if j.args[0] == "generate"]
    assert (_arg(job.args, "--seed"), _arg(job.args, "--steps"), job.args[1]) == (
        "7", "30",
        "subject_definitions:\n<Subject 1> is an armored amazon, appearance from <Picture 1>."
        "\n\n<Subject 1> jumps")


def test_retry_without_a_body_keeps_the_old_behaviour(live):
    proj = _shot_scene_0(live)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/scenes/0/retry", {})
    assert status == 200, body
    scene = p.load_project(proj.path).scenes[0]
    assert (scene["prompt"], "seed" in scene, scene["status"]) == ("@amazon walks", False, "running")


@pytest.mark.parametrize("payload, code, message", [
    ({"seed": -1}, "args_invalid", "`scenes[0].seed` must be an integer >= 0"),
    ({"steps": 1}, "args_invalid", "`scenes[0].steps` must be an integer between 2 and 100"),
    ({"prompt": "  "}, "args_invalid", "`scenes[0].prompt` must be a non-empty string"),
    ({"prompt": "@ghost jumps"}, "unknown_tag", "теги не подключены к проекту: @ghost"),
])
def test_a_refused_retry_touches_nothing(live, payload, code, message):
    proj = _shot_scene_0(live)
    before_jobs = sorted(job.id for job in _pending(live))
    before = p.load_project(proj.path).scenes
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/scenes/0/retry", payload)
    assert (status // 100, body["error"]["code"], body["error"]["message"]) == (4, code, message)
    assert p.load_project(proj.path).scenes == before
    assert sorted(job.id for job in _pending(live)) == before_jobs


def test_seed_and_steps_on_mlx_are_refused_before_their_value(tmp_path, monkeypatch):
    """Task 0's fix round: seed/steps are sglang-only, and the engine is checked first -- an
    otherwise valid seed on MLX is refused for the engine, not accepted."""
    monkeypatch.setenv("H3_ENGINE", "mlx")   # explicit: a shell with H3_ENGINE=sglang must not flip it
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    live = _serve(q.layout(outdir / "queue")["root"], outdir)
    try:
        proj = p.create_project(outdir, "video", "Мак")
        proj.scenes = [{"idx": 0, "prompt": "a cat", "duration": 8.0, "status": "done",
                        "job_id": None, "clip_path": "/c.mp4", "keyframe_path": None}]
        proj.save()
        for seed in (7, -1):   # -1: the engine is refused first, not the value
            status, body = _call(live, "POST", f"/api/projects/{proj.id}/scenes/0/retry",
                                 {"seed": seed})
            assert (status, body["error"]["code"], body["error"]["message"]) == (
                400, "args_invalid", "`scenes[0].seed` is only for the sglang engine")
    finally:
        live.httpd.shutdown()
        live.httpd.server_close()


def test_a_never_shot_scene_is_not_retried(live):
    proj = _shot_scene_0(live)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/scenes/1/retry", {})
    assert (status, body["error"]["code"], body["error"]["message"]) == (
        409, "project_stage_not_ready", "сцена #1 ещё не снималась — пересчитывать нечего")
