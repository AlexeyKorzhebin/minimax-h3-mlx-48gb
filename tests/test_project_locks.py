"""Wave 1.5, spec §5.5: while a project has a job in the queue its references and settings are
refused (the chained scenes would silently get two halves); the upscale tick only while the
upscale or the assembly runs."""
import pytest

from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import queue as q
from test_web import _call, _pending, _serve

PNG = b"\x89PNG\r\n\x1a\n"
REFS = ("Проект считается — референсы меняются после конца прогона. Чтобы поменять для части "
        "сцен: дождитесь конца и пересчитайте с нужной сцены.")
SETTINGS = ("Проект считается — начало сцепленной сцены и сид меняются после конца прогона. "
            "Чтобы поменять для части сцен: дождитесь конца и пересчитайте с нужной сцены.")
ROUTE = "Идёт апскейл или сборка — галочку апскейла можно поменять после них."


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


def _ready(live):
    proj = p.create_project(live.outdir, "video", "Бой")
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/scenes", {
        "scenes": [{"prompt": "@amazon walks", "duration": 8.0},
                   {"prompt": "@amazon runs", "duration": 8.0}],
        "references": [{"tag": "@amazon"}]})
    assert status == 200, body
    return proj


def _error(body):
    error = body.get("error") or {}
    return error.get("code"), error.get("message")


def test_references_and_settings_are_refused_while_a_scene_is_queued(live):
    proj = _ready(live)
    assert _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})[0] == 200
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/references",
                         {"references": [{"tag": "@amazon"}]})
    assert (status, _error(body), body["error"]["detail"]) == (
        409, ("project_running", REFS), {"id": proj.id, "active": "scene"})
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/settings", {"i2v_prefix": "x"})
    assert (status, _error(body)) == (409, ("project_running", SETTINGS))
    assert p.load_project(proj.path).i2v_prefix == p.DEFAULT_I2V_PREFIX


def test_the_same_edits_pass_when_nothing_is_queued(live):
    proj = _ready(live)
    assert _call(live, "PUT", f"/api/projects/{proj.id}/references",
                 {"references": [{"tag": "@amazon", "version": 1}]})[0] == 200
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/settings", {"i2v_prefix": "x"})
    assert (status, body["project"]["i2v_prefix"]) == (200, "x")


def test_route_is_open_during_scenes(live):
    proj = _ready(live)
    assert _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})[0] == 200
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/route", {"upscale": False})
    assert status == 200, body


@pytest.mark.parametrize("kind, args0, note, stem, active", [
    (q.KIND_UPSCALE, "upscale", "upscale project {id}", "upscale/job-upscale", "upscale"),
    (q.KIND_ASSEMBLE, "assemble", "assemble project {id}", "assembly/job-final", "assembly"),
])
def test_route_is_closed_while_the_upscale_or_the_assembly_runs(live, kind, args0, note, stem,
                                                                 active):
    other = p.create_project(live.outdir, "video", "Другой")
    q.submit(live.queue_root, [args0, "--project", str(other.path)], note.format(id=other.id),
             {"output_stem": str(other.path.parent / stem)}, {}, kind=kind)
    status, body = _call(live, "PUT", f"/api/projects/{other.id}/route", {"upscale": False})
    assert (status, body["error"]) == (409, {
        "code": "project_running", "message": ROUTE, "detail": {"id": other.id, "active": active}})
    assert [e for e in p.load_project(other.path).route if e["stage"] == "upscale"] == [
        {"stage": "upscale", "enabled": True}]
