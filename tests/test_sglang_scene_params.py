"""UI-gap API 1: a scene may carry its own `seed` and `steps`, the project a default `seed`
(`PUT /scenes`, `PUT /settings`); they reach the sglang argv and the payload, else the old defaults."""
import pytest

from h3_48gb import assemble
from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import queue as q
from h3_48gb.engines import sglang as sglang_engine
from h3_48gb.engines import sglang_args as sa
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


def _put(live, proj, scenes):
    return _call(live, "PUT", f"/api/projects/{proj.id}/scenes",
                 {"scenes": scenes, "references": [{"tag": "@amazon"}]})


def _error(answer):
    error = answer.get("error") or {}
    return error.get("code"), error.get("message")


def test_scene_seed_and_steps_are_stored_and_reach_the_argv_and_payload(live):
    proj = p.create_project(live.outdir, "video", "Params")
    status, body = _put(live, proj, [{"prompt": "@amazon walks", "duration": 8, "seed": 7,
                                      "steps": 30}])
    assert status == 200, body
    scene = p.load_project(proj.path).scenes[0]
    assert (scene["seed"], scene["steps"]) == (7, 30)
    assert _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})[0] == 200
    (job,) = _pending(live)
    spec = sa.parse(job.args, check_files=False)
    assert (spec.seed, spec.steps) == (7, 30)
    payload = sglang_engine.build_payload(spec)
    assert (payload["seed"], payload["num_inference_steps"]) == (7, 30)


def test_a_scene_without_them_gets_project_seed_then_42_and_default_steps(live):
    proj = p.create_project(live.outdir, "video", "Params")
    assert _put(live, proj, [{"prompt": "@amazon walks", "duration": 8}])[0] == 200
    assert _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})[0] == 200
    (job,) = _pending(live)
    spec = sa.parse(job.args, check_files=False)
    assert (spec.seed, spec.steps) == (42, sa.DEFAULT_STEPS)

    other = p.create_project(live.outdir, "video", "Seeded")
    status, body = _call(live, "PUT", f"/api/projects/{other.id}/settings", {"seed": 5})
    assert (status, body["project"]["seed"], body["project"]["i2v_prefix"]) == (
        200, 5, p.DEFAULT_I2V_PREFIX)
    assert _put(live, other, [{"prompt": "@amazon walks", "duration": 8}])[0] == 200
    assert _call(live, "POST", f"/api/projects/{other.id}/approve/script", {})[0] == 200
    job = [j for j in _pending(live) if j.id != job.id][0]
    assert sa.parse(job.args, check_files=False).seed == 5


def test_a_scene_seed_beats_the_project_seed():
    scene = {"idx": 0, "prompt": "x", "duration": 175 / 24, "seed": 9}
    ref = lib.Ref2VAScene("x", ("/r.png",), (), ())
    args, _ = assemble._scene_generate_args_sglang(
        scene, keyframe=None, chained=False, ref2va=ref, track_piece=None,
        scenes_dir=__import__("pathlib").Path("/s"), default_seed=5)
    assert sa.parse(args, check_files=False).seed == 9


@pytest.mark.parametrize("field,value,code,message", [
    ("seed", -1, "args_invalid", "`scenes[0].seed` must be an integer >= 0"),
    ("seed", 1.5, "args_invalid", "`scenes[0].seed` must be an integer >= 0"),
    ("seed", True, "args_invalid", "`scenes[0].seed` must be an integer >= 0"),
    ("steps", 1, "args_invalid", "`scenes[0].steps` must be an integer between 2 and 100"),
    ("steps", 101, "args_invalid", "`scenes[0].steps` must be an integer between 2 and 100"),
    ("steps", "30", "args_invalid", "`scenes[0].steps` must be an integer between 2 and 100"),
])
def test_bad_scene_seed_and_steps_are_refused_with_a_message(live, field, value, code, message):
    proj = p.create_project(live.outdir, "video", "Params")
    status, answer = _put(live, proj, [{"prompt": "@amazon walks", "duration": 8, field: value}])
    assert (status, *_error(answer)) == (400, code, message)
    assert p.load_project(proj.path).scenes == []


def test_the_parser_refuses_steps_the_server_would_reject():
    args = ["generate", "x", "--width", "896", "--height", "512", "--duration", "8", "--steps", "1",
            "--tag", "t", "--outdir", "/o", "--ref", "/r.png"]
    with pytest.raises(sa.SglangArgsError) as excinfo:
        sa.parse(args, check_files=False)
    assert (excinfo.value.code, excinfo.value.message) == (
        "sglang_args_invalid", "--steps 2..100 и --seed ≥ 0")


def test_settings_validate_seed_and_keep_i2v_prefix_changes(live):
    proj = p.create_project(live.outdir, "video", "Params")
    path = f"/api/projects/{proj.id}/settings"
    status, answer = _call(live, "PUT", path, {"seed": -3})
    assert (status, *_error(answer)) == (400, "args_invalid", "`seed` must be an integer >= 0")
    status, answer = _call(live, "PUT", path, {})
    assert (status, *_error(answer)) == (
        400, "args_invalid", "settings: pass `i2v_prefix` and/or `seed`")
    status, body = _call(live, "PUT", path, {"i2v_prefix": "  Go on.  ", "seed": 11})
    assert (status, body["project"]["i2v_prefix"], body["project"]["seed"]) == (200, "Go on.", 11)
    status, body = _call(live, "PUT", path, {"seed": None})
    assert (status, body["project"]["seed"], body["project"]["i2v_prefix"]) == (200, None, "Go on.")
