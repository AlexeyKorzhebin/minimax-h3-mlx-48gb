"""Final review 2026-10-07, I1/I6: a ready-made video scenario is loaded through
`PUT /api/projects/<id>/scenes` without the LLM -- prompts, durations, the references the
@tags name, and scene 0's start image -- and goes through the same approve gate as a chat one."""
import pytest

from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import queue as q
from h3_48gb.engines import sglang_args as sa
from test_web import _call, _pending, _serve

PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    outdir = tmp_path / "outdir"
    (outdir / "uploads").mkdir(parents=True)
    for name in ("face.png", "opening.png"):
        (outdir / "uploads" / name).write_bytes(PNG)
    lib.create_card(outdir, tag="@amazon", kind="person", description="an armored amazon",
                    assets=[outdir / "uploads" / "face.png"])
    lib.create_card(outdir, tag="@arena", kind="environment", description="a sand arena",
                    assets=[outdir / "uploads" / "opening.png"])
    server = _serve(q.layout(outdir / "queue")["root"], outdir)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def _error(answer: dict):
    error = answer.get("error") or {}
    return error.get("code"), error.get("message")


def _scenario(start_image="@arena"):
    scenes = [{"prompt": f"@amazon fights on @arena, beat {i}", "duration": 8.0}
              for i in range(5)]
    scenes[0]["start_image"] = start_image
    return {"scenes": scenes, "references": [{"tag": "@amazon"}, {"tag": "@arena"}]}


def test_a_ready_scenario_loads_and_scene_0_starts_from_its_start_image(live):
    proj = p.create_project(live.outdir, "video", "Бой")
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/scenes", _scenario())
    assert status == 200, body
    loaded = p.load_project(proj.path)
    assert (loaded.stages["script"], loaded.references) == (
        "awaiting_approval", [{"tag": "@amazon", "version": 1}, {"tag": "@arena", "version": 1}])
    assert loaded.scenes[0] == {"idx": 0, "prompt": "@amazon fights on @arena, beat 0",
                                "duration": 8.0, "status": "pending", "job_id": None,
                                "clip_path": None, "keyframe_path": None, "start_image": "@arena"}
    assert [s["idx"] for s in loaded.scenes] == [0, 1, 2, 3, 4]
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert status == 200, body
    (job,) = _pending(live)
    spec = sa.parse(job.args, check_files=False)
    arena = str(live.outdir / "library" / "arena" / "v1" / "01-opening.png")
    amazon = str(live.outdir / "library" / "amazon" / "v1" / "01-face.png")
    assert (spec.image, spec.refs, spec.frames) == (arena, (amazon, arena), 192)
    assert p.load_project(proj.path).scenes[0]["keyframe_path"] == arena


def test_a_start_image_path_must_be_a_file_in_the_outdir(live, tmp_path):
    proj = p.create_project(live.outdir, "video", "Бой")
    inside = str(live.outdir / "uploads" / "opening.png")
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/scenes", _scenario(inside))
    assert status == 200, body
    assert p.load_project(proj.path).scenes[0]["start_image"] == inside
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(PNG)
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/scenes", _scenario(str(outside)))
    assert (status, _error(body)[0]) == (400, "path_outside_root")


def test_a_start_image_tag_the_project_does_not_pin_is_refused(live):
    proj = p.create_project(live.outdir, "video", "Бой")
    body = _scenario("@beach")
    status, answer = _call(live, "PUT", f"/api/projects/{proj.id}/scenes", body)
    assert (status, *_error(answer)) == (
        400, "unknown_tag", "start_image @beach: тег не подключён к проекту")
    assert p.load_project(proj.path).scenes == []


def test_a_start_image_on_another_scene_and_a_bad_duration_are_refused(live):
    proj = p.create_project(live.outdir, "video", "Бой")
    body = _scenario()
    body["scenes"][2]["start_image"] = "@arena"
    status, answer = _call(live, "PUT", f"/api/projects/{proj.id}/scenes", body)
    assert (status, _error(answer)[1]) == (
        400, "`start_image` is a non-empty string, and only scene 0 has one")
    body = _scenario()
    body["scenes"][1]["duration"] = 16
    status, answer = _call(live, "PUT", f"/api/projects/{proj.id}/scenes", body)
    assert (status, _error(answer)[1]) == (
        400, "`scenes[1].duration` must be a number between 3 and 15 seconds")


def test_scenes_cannot_be_replaced_once_the_chain_started(live):
    proj = p.create_project(live.outdir, "video", "Бой")
    assert _call(live, "PUT", f"/api/projects/{proj.id}/scenes", _scenario())[0] == 200
    assert _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})[0] == 200
    status, answer = _call(live, "PUT", f"/api/projects/{proj.id}/scenes", _scenario())
    assert (status, _error(answer)[0]) == (409, "project_stage_not_ready")
    # the script still waits, but a scene is already queued (a worker-side resubmission)
    waiting = p.create_project(live.outdir, "video", "Ждёт")
    waiting.stages.update(script="awaiting_approval", scenes="running")
    waiting.save()
    status, answer = _call(live, "PUT", f"/api/projects/{waiting.id}/scenes", _scenario())
    assert (status, _error(answer)[0]) == (409, "project_stage_not_ready")
    clip = p.create_project(live.outdir, "clip", "Клип")
    status, answer = _call(live, "PUT", f"/api/projects/{clip.id}/scenes", _scenario())
    assert (status, _error(answer)[0]) == (400, "args_invalid")
