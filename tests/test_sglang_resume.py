"""Restart in the middle of a render (spec §5, §6): exactly one POST, polling resumes on the
same id; an id sglang no longer knows fails honestly; cancelling a running job from the page."""
import pytest

from h3_48gb import queue as q
from h3_48gb import worker
from h3_48gb.engines import sglang as sg
from _fake_dispatcher import FakeDispatcher
from _fake_sglang import FakeSglang
from test_web import _call, _serve
from test_worker import _stop_after


@pytest.fixture(autouse=True)
def _no_real_frame_decode(monkeypatch):
    """The fake serves placeholder bytes, not a decodable mp4; the real zero-fill check is
    exercised in test_sglang_framecheck.py."""
    monkeypatch.setattr(sg, "_flat_frames", lambda mp4, expected: [])


class _Crash(BaseException):
    """A worker dying mid-poll (a signal, an OOM kill). BaseException on purpose: the worker's
    own `except Exception` safety net around the adapter must not swallow it."""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    # the worker now asks the host dispatcher for the card before every scene (task 8)
    dispatcher = FakeDispatcher()
    monkeypatch.setenv("H3_DISPATCHER_URL", dispatcher.url)
    monkeypatch.setattr(sg, "POLL_SECONDS", 0.0)
    monkeypatch.setattr(sg, "LOST_RETRY_SECONDS", 0.0)
    root = q.layout(tmp_path / "queue")["root"]
    (tmp_path / "scenes").mkdir()
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n")
    args = ["generate", "p", "--width", "896", "--height", "512", "--duration", str(175 / 24),
            "--tag", "t", "--outdir", str(tmp_path / "scenes"), "--ref", str(ref)]
    q.submit(root, args, "", {"output_stem": str(tmp_path / "scenes" / "h3-t-896x512")}, {})
    yield root, tmp_path
    dispatcher.close()


def _crash_after_post(monkeypatch):
    real = sg._sleep_unless_cancelled
    monkeypatch.setattr(sg, "_sleep_unless_cancelled",
                        lambda *a, **k: (_ for _ in ()).throw(_Crash()))
    return real


def test_restart_between_post_and_completed_resumes_without_a_second_post(env, monkeypatch):
    root, tmp_path = env
    fake = FakeSglang(statuses=("queued", "completed"))
    monkeypatch.setenv("H3_SGLANG_URL", fake.url)
    try:
        real = _crash_after_post(monkeypatch)
        job = q.claim(root)
        with pytest.raises(_Crash):
            worker.run_job(root, job, outdir=tmp_path)
        state = q.reconcile(root)
        assert [j.id for j in state.resumable] == [job.id]
        assert state.resumable[0].engine_ref == "vid-1"
        assert q.scan(root)[0][0].state == "running"   # not back in pending
        monkeypatch.setattr(sg, "_sleep_unless_cancelled", real)
        worker.main_loop(root, poll=0.01, stop=_stop_after(1.0), outdir=tmp_path)
    finally:
        fake.close()
    assert len(fake.posts) == 1
    assert set(fake.gets) == {"vid-1"}
    (done,) = [j for j in q.scan(root)[0] if j.id == job.id]
    assert (done.state, done.exit_code) == ("done", 0)


def test_an_id_sglang_forgot_fails_as_lost(env, monkeypatch):
    root, tmp_path = env
    fake = FakeSglang(statuses=("queued",))
    monkeypatch.setenv("H3_SGLANG_URL", fake.url)
    try:
        real = _crash_after_post(monkeypatch)
        job = q.claim(root)
        with pytest.raises(_Crash):
            worker.run_job(root, job, outdir=tmp_path)
        fake.forget_all = True          # H3 restarted: its in-memory store is empty
        monkeypatch.setattr(sg, "_sleep_unless_cancelled", real)
        (resumable,) = q.reconcile(root).resumable
        code = worker.run_job(root, resumable, outdir=tmp_path)
    finally:
        fake.close()
    assert code == 1
    (failed,) = [j for j in q.scan(root)[0] if j.id == job.id]
    assert failed.state == "failed"
    assert "задача потеряна при рестарте H3 (id=vid-1 неизвестен серверу)" in failed.log_tail
    assert len(fake.posts) == 1


def test_a_running_job_without_engine_ref_still_returns_to_pending(env):
    root, tmp_path = env
    job = q.claim(root)                 # claimed, never posted (e.g. died while waiting for GPU)
    state = q.reconcile(root)
    assert (state.resumable, [j.id for j in state.changed]) == ([], [job.id])
    assert q.scan(root)[0][0].state == "pending"


def test_the_page_cancels_a_running_sglang_job(env, monkeypatch):
    root, tmp_path = env
    job = q.claim(root)
    outdir = tmp_path
    live = _serve(root, outdir)
    try:
        status, body = _call(live, "DELETE", f"/api/jobs/{job.id}")
    finally:
        live.httpd.shutdown()
        live.httpd.server_close()
    assert status == 200, body
    assert (body["ok"], body["cancelling"], body["message"], body["job"]["cancel_reason"]) == \
        (True, True, "H3 досчитает сцену впустую, следующая задача начнётся после",
         "cancelled_by_user")
    assert q.cancel_reason(root, job.id) == "cancelled_by_user"


def test_on_mlx_a_running_job_still_cannot_be_cancelled(env, monkeypatch):
    root, tmp_path = env
    monkeypatch.setenv("H3_ENGINE", "mlx")
    job = q.claim(root)
    live = _serve(root, tmp_path)
    try:
        status, body = _call(live, "DELETE", f"/api/jobs/{job.id}")
    finally:
        live.httpd.shutdown()
        live.httpd.server_close()
    assert body["error"]["code"] == "job_not_pending"


def test_a_cancelled_job_that_never_posted_fails_on_restart_instead_of_pending(env):
    root, tmp_path = env
    job = q.claim(root)
    q.request_cancel(root, job.id, "cancelled_by_user")
    state = q.reconcile(root)
    assert state.resumable == []
    (failed,) = [j for j in q.scan(root)[0] if j.id == job.id]
    assert (failed.state, failed.exit_code, failed.log_tail) == \
        ("failed", 1, "отменена до начала")


def test_a_job_finishing_under_the_cancel_click_answers_job_not_pending(env, monkeypatch):
    root, tmp_path = env
    job = q.claim(root)

    def finished_meanwhile(*args, **kwargs):
        raise q.JobNotRunning("gone")

    monkeypatch.setattr(q, "request_cancel", finished_meanwhile)
    live = _serve(root, tmp_path)
    try:
        status, body = _call(live, "DELETE", f"/api/jobs/{job.id}")
    finally:
        live.httpd.shutdown()
        live.httpd.server_close()
    assert status == 409, body
    assert (body["error"]["code"], body["error"]["detail"]) == ("job_not_pending", {"id": job.id})
