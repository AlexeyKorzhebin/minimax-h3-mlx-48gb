"""Wave 1.5, spec §5.2.1: «Промпт для H3» shows, before approval, the prompt and the conditions
sglang will get for a saved scene -- built by the very path the gate and the submission use."""
import pytest

from h3_48gb import assemble as p_assemble
from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import queue as q
from test_web import _call, _serve

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


def _project(live):
    proj = p.create_project(live.outdir, "video", "Бой")
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/scenes", {
        "scenes": [{"prompt": "@amazon fights", "duration": 8.0, "start_image": "@arena", "seed": 305},
                   {"prompt": "@amazon runs", "duration": 5.0, "refs": ["@arena"], "steps": 30}],
        "references": [{"tag": "@amazon"}, {"tag": "@arena"}]})
    assert status == 200, body
    assert _call(live, "PUT", f"/api/projects/{proj.id}/settings", {"i2v_prefix": "Continue."})[0] == 200
    return proj


def test_scene_0_shows_its_start_frame_and_its_own_seed(live):
    proj = _project(live)
    amazon = str(live.outdir / "library" / "amazon" / "v1" / "01-face.png")
    arena = str(live.outdir / "library" / "arena" / "v1" / "01-opening.png")
    status, body = _call(live, "GET", f"/api/projects/{proj.id}/scenes/0/h3-prompt")
    assert (status, body) == (200, {
        "ok": True, "idx": 0,
        "prompt": "subject_definitions:\n<Subject 1> is an armored amazon, appearance from "
                  "<Picture 1>.\n\n<Subject 1> fights",
        "pictures": [{"label": "<Picture 1>", "path": amazon}], "audios": [],
        "keyframe": {"kind": "start_image", "path": arena},
        "duration": 8.0, "seed": 305, "steps": 50})


def test_a_chained_scene_carries_the_prefix_and_its_refs_come_first(live):
    proj = _project(live)
    amazon = str(live.outdir / "library" / "amazon" / "v1" / "01-face.png")
    arena = str(live.outdir / "library" / "arena" / "v1" / "01-opening.png")
    status, body = _call(live, "GET", f"/api/projects/{proj.id}/scenes/1/h3-prompt")
    assert (status, body) == (200, {
        "ok": True, "idx": 1,
        "prompt": "Continue.\n\nsubject_definitions:\n<Subject 1> is an armored amazon, appearance "
                  "from <Picture 2>.\n\n<Subject 1> runs",
        "pictures": [{"label": "<Picture 1>", "path": arena}, {"label": "<Picture 2>", "path": amazon}],
        "audios": [], "keyframe": {"kind": "previous_scene", "path": None},
        "duration": 5.125, "seed": 42, "steps": 30})


def test_unknown_scene_and_mlx(live, monkeypatch):
    proj = _project(live)
    status, body = _call(live, "GET", f"/api/projects/{proj.id}/scenes/9/h3-prompt")
    assert (status, body["error"]["code"]) == (404, "project_scene_not_found")
    monkeypatch.setenv("H3_ENGINE", "mlx")
    status, body = _call(live, "GET", f"/api/projects/{proj.id}/scenes/0/h3-prompt")
    assert (status, body["error"]["code"], body["error"]["message"]) == (
        400, "args_invalid", "Промпт для H3 есть только на sglang")


def test_an_off_grid_scene_answers_like_the_gate(live, monkeypatch):
    proj = _project(live)

    def off_grid(*_args, **_kwargs):
        raise p_assemble.AssembleError("scene 0: 190 frames is off sglang's 17n+5 grid")
    monkeypatch.setattr(p_assemble, "_scene_generate_args_sglang", off_grid)
    status, body = _call(live, "GET", f"/api/projects/{proj.id}/scenes/0/h3-prompt")
    assert (status, body["error"]["code"], body["error"]["message"]) == (
        400, "duration_off_grid", "scene 0: 190 frames is off sglang's 17n+5 grid")


def test_a_library_error_answers_like_the_gate(live):
    proj = _project(live)
    # scene 0's start frame file vanishes: the gate's start_image_invalid, not a 500
    arena = live.outdir / "library" / "arena" / "v1" / "01-opening.png"
    arena.unlink()
    status, body = _call(live, "GET", f"/api/projects/{proj.id}/scenes/0/h3-prompt")
    assert (status, body["error"]["code"]) == (400, "start_image_invalid")
