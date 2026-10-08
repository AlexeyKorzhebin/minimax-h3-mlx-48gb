"""Wave 1.5, spec §5.7: the project payload says what the LTX upscale did -- strength, motion,
parts, error -- from the report the upscale job writes."""
import json

import pytest

from h3_48gb import project as p
from h3_48gb import queue as q
from test_web import _call, _serve


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    server = _serve(q.layout(outdir / "queue")["root"], outdir)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def _report(proj, body):
    path = proj.path.parent / "upscale" / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")


def test_a_done_report_is_summarised(live):
    proj = p.create_project(live.outdir, "video", "Бой")
    _report(proj, {"attempt": "a1", "started_at": 1.0, "motion": 0.12, "strength": 0.6,
                   "parts": [{"idx": 0, "clip": "x", "comfy_s": 3}, {"idx": 1, "clip": "y"}],
                   "status": "done", "finished_at": 9.0})
    status, body = _call(live, "GET", f"/api/projects/{proj.id}")
    assert (status, body["project"]["upscale_report"]) == (200, {
        "status": "done", "strength": 0.6, "motion": 0.12, "attempted": [0, 1], "error": None})


def test_a_failed_report_carries_its_error(live):
    proj = p.create_project(live.outdir, "video", "Бой")
    _report(proj, {"attempt": "a1", "parts": [], "status": "failed", "error": "ComfyUI 500"})
    assert _call(live, "GET", f"/api/projects/{proj.id}")[1]["project"]["upscale_report"] == {
        "status": "failed", "strength": None, "motion": None, "attempted": [], "error": "ComfyUI 500"}


@pytest.mark.parametrize("body", [None, "{broken", "[1, 2]"])
def test_no_or_unreadable_report_is_null(live, body):
    proj = p.create_project(live.outdir, "video", "Бой")
    if body is not None:
        _report(proj, body)
    assert _call(live, "GET", f"/api/projects/{proj.id}")[1]["project"]["upscale_report"] is None
