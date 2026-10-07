"""The panel's GPU routes (spec §3.3.13-14, §3.4): status for the banner, «Освободить карту»
with confirmation and pause, Qwen buttons -- all thin proxies to the dispatcher."""
import pytest

from h3_48gb import queue as q
from _fake_dispatcher import FakeDispatcher
from test_web import _call, _serve


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    disp = FakeDispatcher(own={"h3": {"pid": 4242, "variant": "ref2va", "started_at": 1.0,
                                      "log": "/l", "ready": True}})
    monkeypatch.setenv("H3_DISPATCHER_URL", disp.url)
    root = q.layout(tmp_path / "queue")["root"]
    live = _serve(root, tmp_path)
    yield live, disp, root, tmp_path
    live.httpd.shutdown()
    live.httpd.server_close()
    disp.close()


def _queue_running(root, tmp_path, note="project scene P #3"):
    q.submit(root, ["generate", "--tag", "a"], note, {"output_stem": str(tmp_path / "h3-a")}, {})
    job = q.claim(root)
    q.set_running_fields(root, job.id, wait_reason="ждём GPU: generation.lock занят")
    return job


def test_gpu_state_combines_dispatcher_and_queue(setup):
    live, disp, root, tmp_path = setup
    job = _queue_running(root, tmp_path)
    status, body = _call(live, "GET", "/api/gpu")
    assert status == 200
    assert body == {"ok": True, "dispatcher": disp.status_body(), "dispatcher_error": None,
                    "idle_release_at": None, "worker_alive": False,
                    "queue": {"pending": 0, "paused": True,
                              "running": {"id": job.id, "kind": "generate",
                                          "note": "project scene P #3",
                                          "started_at": job.started_at,
                                          "wait_reason": "ждём GPU: generation.lock занят"}}}


def test_gpu_state_reports_an_unreachable_dispatcher(setup, monkeypatch):
    live, disp, root, tmp_path = setup
    monkeypatch.setenv("H3_DISPATCHER_URL", "http://127.0.0.1:9")
    status, body = _call(live, "GET", "/api/gpu")
    assert (status, body["dispatcher"]) == (200, None)
    assert body["dispatcher_error"].startswith("GET /status: ")


def test_release_with_an_empty_queue_releases_now_and_pauses(setup):
    live, disp, root, tmp_path = setup
    q.set_paused(root, False)
    status, body = _call(live, "POST", "/api/gpu/release", {})
    assert (status, body) == (200, {"ok": True, "paused": True, "releasing": False,
                                    "released": ["h3"]})
    assert q.is_paused(root) is True
    assert [c[1:] for c in disp.calls] == [("/release", {"client": "panel-web", "all": True})]


def test_release_while_rendering_needs_confirmation(setup):
    live, disp, root, tmp_path = setup
    job = _queue_running(root, tmp_path)
    status, body = _call(live, "POST", "/api/gpu/release", {})
    assert (status, body["error"]["code"], body["error"]["message"]) == (
        409, "release_needs_confirm",
        "H3 считает project scene P #3 — освободить карту? Сцена будет потеряна")
    assert q.cancel_reason(root, job.id) is None
    assert disp.calls == []


def test_confirmed_release_while_rendering_cancels_pauses_and_leaves_release_to_the_worker(setup):
    live, disp, root, tmp_path = setup
    job = _queue_running(root, tmp_path)
    q.set_paused(root, False)
    status, body = _call(live, "POST", "/api/gpu/release", {"confirm": True})
    assert (status, body) == (200, {"ok": True, "paused": True, "releasing": True, "job": job.id})
    assert q.cancel_reason(root, job.id) == "released_by_user"
    assert q.is_paused(root) is True
    assert disp.calls == []


def test_release_during_an_assembly_frees_the_card_now_and_leaves_the_assembly(setup):
    live, disp, root, tmp_path = setup
    q.submit(root, ["assemble", "--project", str(tmp_path / "p" / "project.json")], "assemble P",
             {"output_stem": str(tmp_path / "p" / "job-final")}, {}, kind=q.KIND_ASSEMBLE)
    job = q.claim(root)
    status, body = _call(live, "POST", "/api/gpu/release", {})
    assert (status, body) == (200, {"ok": True, "paused": True, "releasing": False,
                                    "released": ["h3"]})
    assert q.cancel_reason(root, job.id) is None
    assert [c[1:] for c in disp.calls] == [("/release", {"client": "panel-web", "all": True})]


def test_qwen_restore_is_refused_while_the_queue_is_busy(setup):
    live, disp, root, tmp_path = setup
    _queue_running(root, tmp_path)
    status, body = _call(live, "POST", "/api/qwen/restore", {})
    assert (status, body["error"]["code"], body["error"]["message"]) == \
        (409, "queue_busy", "вернуть Qwen можно после очереди: в ней ещё есть задачи")
    assert disp.calls == []


def test_qwen_buttons_proxy_and_map_the_refusal(setup):
    live, disp, root, tmp_path = setup
    assert _call(live, "POST", "/api/qwen/unload", {}) == \
        (200, {"ok": True, "was_running": True, "exit_code": 0})
    disp.restore_status = 409
    status, body = _call(live, "POST", "/api/qwen/restore", {})
    assert (status, body["error"]["code"], body["error"]["message"]) == \
        (409, "qwen_was_not_running", "Qwen не был запущен до выгрузки")


def test_gpu_routes_refuse_on_mlx(setup, monkeypatch):
    live, disp, root, tmp_path = setup
    monkeypatch.setenv("H3_ENGINE", "mlx")
    status, body = _call(live, "GET", "/api/gpu")
    assert (status, body["error"]["code"]) == (409, "engine_not_sglang")


def test_gpu_state_survives_a_dispatcher_without_nvidia_smi(setup):
    live, disp, root, tmp_path = setup
    disp.gpu_null = True
    status, body = _call(live, "GET", "/api/gpu")
    assert status == 200
    assert (body["dispatcher"]["gpu"], body["dispatcher"]["gpu_error"]) == (
        None, "nvidia-smi недоступен")


def test_qwen_restore_passes_already_running_through(setup):
    live, disp, root, tmp_path = setup
    disp.restore_status = "already_running"
    assert _call(live, "POST", "/api/qwen/restore", {}) == \
        (200, {"ok": True, "state": "already_running"})


def test_an_unknown_dispatcher_refusal_is_a_502_not_a_made_up_code(setup):
    live, disp, root, tmp_path = setup
    disp.restore_status = 500
    status, body = _call(live, "POST", "/api/qwen/restore", {})
    assert (status, body["error"]["code"], body["error"]["message"]) == \
        (502, "dispatcher_unavailable", "что-то своё")


def test_release_pauses_the_queue_before_asking_the_dispatcher(setup):
    live, disp, root, tmp_path = setup
    q.set_paused(root, False)
    seen = []
    disp.on_release = lambda: seen.append(q.is_paused(root))
    status, _ = _call(live, "POST", "/api/gpu/release", {})
    assert (status, seen) == (200, [True])
