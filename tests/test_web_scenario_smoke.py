"""Task 5 ("Сюжет клипа" wave, UI): the brief's own smoke, verbatim -- "сервер на 18765, mock-LLM
недоступен -> generate отдаёт честную ошибку; procedural-путь проходит до сцен в очереди; воркер
не запускать".

**Not a second copy of `tests/test_web_projects.py`'s exhaustive scenario-route coverage.** Every
gate, every error code and every validation rule for `/scenario/generate`, `PUT /scenario` and
`approve/scenario` already has its own test there (task 4's own wave) -- this file adds exactly
one thing those do not: a server bound to a *real, fixed* port, `18765`, the one this repo's own
docs reserve for a manual `curl` smoke against the web layer (`docs/superpowers/plans/
2026-08-18-projects.md`: "боевой сервер 8765 не трогать; ручные проверки — порты 18765/18766") --
proof that the two routes the panel (`h3_48gb/webui/app.js`'s `projectScenarioStageHtml`) actually
calls behave the same way over a real socket that a browser or `curl` would use, not only over the
`port=0` ephemeral socket every other test in this suite binds.

**"Воркер не запускать" (task brief, verbatim) is honoured literally**: neither test below ever
calls `worker.run_job` on a `kind="generate"` scene job -- the procedural path's own proof stops at
"a scene job sits in the queue, pending", read straight off `q.scan`, never executed. The one
`worker.run_job` call either test makes at all is `_clip_project_with_approved_track`'s own
(`tests/test_web_projects.py`) fake `kind="song"` run needed just to get `stages.track` to
`"approved"` in the first place -- a precondition every scenario-gate test in that file already
runs the same way, not the thing this smoke is about.
"""
import threading
from pathlib import Path

import pytest

from h3_48gb import assemble as assemble_module
from h3_48gb import queue as q
from h3_48gb import web
from test_chat_web import _Live
from test_web_projects import _clip_project_with_approved_track

#: docs/superpowers/plans/2026-08-18-projects.md's own reserved pair for manual web checks; this
#: file binds the first of the two for real (not `port=0`) -- see the module docstring.
_PORT = 18765


@pytest.fixture
def _serve18765(tmp_path):
    """The same shape as `test_chat_web._serve()`, minus the factory (this file only ever needs
    one server per test) and minus a `providers.json` (none is written at all) -- an empty roster
    is exactly what the "mock-LLM недоступен" half of the brief asks for: `/api/projects/<id>/
    scenario/generate` must refuse honestly with no provider configured at all, the same shape
    `test_web_projects.test_generate_scenario_provider_unavailable` already checks on an ephemeral
    port, checked here again on the fixed one.

    Bound with `allow_reuse_address = True` (`web._Server`), so binding the same fixed port again
    on a second test in this file/run does not races a lingering `TIME_WAIT` socket from the one
    before it.
    """
    root = tmp_path / "outdir"
    root.mkdir(exist_ok=True)
    queue_root = q.layout(root / "queue")["root"]
    httpd = web.make_server(queue_root, root, port=_PORT)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    live = _Live(httpd=httpd, port=httpd.server_address[1], root=root, queue_root=queue_root)
    assert live.port == _PORT, "bound to a different port than the one the brief names"
    yield live
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def test_scenario_generate_on_the_smoke_port_is_an_honest_error_with_no_provider(_serve18765,
                                                                                  monkeypatch):
    """Task 5 brief: "mock-LLM недоступен -> generate отдаёт честную ошибку" -- no `providers.json`
    at all (`_serve18765` writes none), so `web._active_provider()` has nothing to hand back, and
    the route must refuse before it ever tries `ensure_up` or a chat turn -- `409
    provider_unavailable`, not a 500 and not a silent 200 with an empty scenario."""
    srv = _serve18765
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=12.0)

    status, payload = srv.post_json_raw(f"/api/projects/{pid}/scenario/generate", {})

    assert (status, payload["error"]["code"]) == (409, "provider_unavailable"), payload
    # Content, not just the refusal: the gate must not have moved, and no scenario written.
    stuck = srv.get_json(f"/api/projects/{pid}")["project"]
    assert stuck["stages"]["scenario"] == "draft"
    assert stuck["scenario_scenes"] == []


def test_scenario_procedural_path_on_the_smoke_port_reaches_a_queued_scene(_serve18765,
                                                                            monkeypatch):
    """Task 5 brief: "procedural-путь проходит до сцен в очереди (воркер не запускать)" -- the
    fallback button ("Сюжет без LLM" in the panel) needs no provider at all, so the same
    provider-less server the refusal test above uses must still carry it all the way through
    `approve/scenario` to a `kind="generate"` scene job sitting `pending` in the queue, never run.
    """
    srv = _serve18765
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=12.0)

    generated = srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})
    assert generated["project"]["stages"]["scenario"] == "awaiting_approval"
    assert generated["project"]["scenario_scenes"], "procedural path must write scenes"

    approved = srv.post_json(f"/api/projects/{pid}/approve/scenario", {})
    assert approved["project"]["stages"]["scenario"] == "approved"
    assert approved["advance"]["action"] == "submitted_scene"

    jobs, broken = q.scan(srv.queue_root)
    assert broken == []
    pending = [j for j in jobs if j.state == "pending"]
    assert len(pending) == 1, "the worker must not have been run -- exactly one job, still queued"
    assert pending[0].kind == q.KIND_GENERATE
    assert pending[0].note == assemble_module.scene_note(pid, 0)
