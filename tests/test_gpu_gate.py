"""The worker's side of the GPU (spec §4.1.1-2, §3.4 /release, §3.3.14)."""
import pytest

from h3_48gb import queue as q
from h3_48gb import worker
from h3_48gb.engines import dispatcher_client as dc
from h3_48gb.engines import sglang as sg
from _fake_dispatcher import FakeDispatcher
from _fake_sglang import FakeSglang
from test_worker import _stop_after


@pytest.fixture
def running(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    root = q.layout(tmp_path / "queue")["root"]
    (tmp_path / "scenes").mkdir()
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n")
    args = ["generate", "p", "--width", "896", "--height", "512", "--duration", str(175 / 24),
            "--tag", "t", "--outdir", str(tmp_path / "scenes"), "--ref", str(ref)]
    q.submit(root, args, "", {"output_stem": str(tmp_path / "scenes" / "h3-t-896x512")}, {})
    return root, q.claim(root), tmp_path


def _recording_sleep(root, job_id, seen):
    def sleep(seconds):
        reason = [j for j in q.scan(root)[0] if j.id == job_id][0].wait_reason
        if not seen or seen[-1] != reason:
            seen.append(reason)
    return sleep


def test_gate_waits_for_foreign_then_qwen_then_returns_ready(running):
    root, job, _ = running
    fake = FakeDispatcher(acquire=(
        {"ok": True, "state": "wait", "engine": "h3", "reason": "GPU занята: comfy (pid 7, 3000 МБ)"},
        {"ok": True, "state": "wait_qwen", "engine": "h3", "reason": "Qwen держит карту"},
        {"ok": True, "state": "starting", "engine": "h3", "log": "/l.log"},
        {"ok": True, "state": "ready", "engine": "h3"}))
    seen = []
    try:
        gate = worker.make_gpu_gate(root, "h3", client=dc.DispatcherClient(fake.url),
                                    sleep=_recording_sleep(root, job.id, seen))
        assert gate(job) is None
    finally:
        fake.close()
    assert seen == ["ждём GPU: GPU занята: comfy (pid 7, 3000 МБ)",
                    "ждём GPU: Qwen держит карту — выгрузите Qwen в панели",
                    "ждём GPU: поднимается h3"]
    assert [j for j in q.scan(root)[0] if j.id == job.id][0].wait_reason is None
    assert [c[2] for c in fake.calls if c[1] == "/acquire"] == [{"engine": "h3"}] * 4


def test_retry_intervals_are_thirty_and_five_seconds(running, monkeypatch):
    root, job, _ = running
    fake = FakeDispatcher(acquire=(
        {"ok": True, "state": "wait", "engine": "h3", "reason": "x"},
        {"ok": True, "state": "starting", "engine": "h3", "log": "/l"},
        {"ok": True, "state": "ready", "engine": "h3"}))
    waits = []

    def record(root_, job_id, seconds, sleep):
        waits.append(([j for j in q.scan(root_)[0] if j.id == job_id][0].wait_reason, seconds))

    monkeypatch.setattr(sg, "_sleep_unless_cancelled", record)
    try:
        gate = worker.make_gpu_gate(root, "h3", client=dc.DispatcherClient(fake.url))
        assert gate(job) is None
    finally:
        fake.close()
    assert waits == [("ждём GPU: x", 30.0), ("ждём GPU: поднимается h3", 5.0)]


def test_the_threshold_itself_is_hot(running):
    root, job, _ = running
    fake = FakeDispatcher(temps=(80, 72))
    seen = []
    try:
        gate = worker.make_gpu_gate(root, "h3", client=dc.DispatcherClient(fake.url),
                                    sleep=_recording_sleep(root, job.id, seen))
        assert gate(job) is None
    finally:
        fake.close()
    assert seen == ["остываем, 80 °C"]


def test_a_hot_card_is_waited_down_to_72(running):
    root, job, _ = running
    fake = FakeDispatcher(temps=(81, 75, 72))
    seen = []
    try:
        gate = worker.make_gpu_gate(root, "h3", client=dc.DispatcherClient(fake.url),
                                    sleep=_recording_sleep(root, job.id, seen))
        assert gate(job) is None
    finally:
        fake.close()
    assert seen == ["остываем, 81 °C", "остываем, 75 °C"]


def test_cancel_while_waiting_returns_the_reason(running):
    root, job, _ = running
    fake = FakeDispatcher(acquire=({"ok": True, "state": "wait", "engine": "h3", "reason": "x"},))
    try:
        gate = worker.make_gpu_gate(root, "h3", client=dc.DispatcherClient(fake.url),
                                    sleep=lambda s: q.request_cancel(root, job.id, "cancelled_by_user"))
        assert gate(job) == "cancelled_by_user"
    finally:
        fake.close()


def test_an_engine_that_failed_to_start_fails_the_job_without_posting(running, monkeypatch):
    root, job, tmp_path = running
    disp = FakeDispatcher(acquire=({"ok": True, "state": "failed", "engine": "h3",
                                    "log": "/home/alex/Projects/h3-bench/logs/serve-panel-1.log",
                                    "reason": "движок не поднялся, смотрите лог"},))
    h3 = FakeSglang()
    monkeypatch.setenv("H3_DISPATCHER_URL", disp.url)
    monkeypatch.setenv("H3_SGLANG_URL", h3.url)
    try:
        code, log = worker._run_sglang_generate_job(root, tmp_path, job)
    finally:
        disp.close()
        h3.close()
    assert (code, log) == (1, "движок не поднялся: движок не поднялся, смотрите лог; "
                              "лог: /home/alex/Projects/h3-bench/logs/serve-panel-1.log\n")
    assert h3.posts == []


def test_released_by_user_releases_the_card_after_the_job_stops(running, monkeypatch):
    root, job, tmp_path = running
    disp = FakeDispatcher()
    h3 = FakeSglang(statuses=("queued",))
    monkeypatch.setenv("H3_DISPATCHER_URL", disp.url)
    monkeypatch.setenv("H3_SGLANG_URL", h3.url)

    def cancel_now(root_, job_id, seconds, sleep):
        q.request_cancel(root_, job_id, "released_by_user")
        return "released_by_user"

    monkeypatch.setattr(sg, "_sleep_unless_cancelled", cancel_now)
    try:
        code, _ = worker._run_sglang_generate_job(root, tmp_path, job)
    finally:
        disp.close()
        h3.close()
    assert code == 1
    assert [c[1] for c in disp.calls if c[0] == "POST"] == ["/acquire", "/release"]
    assert h3.deletes == ["vid-1"]


class _FakeClient:
    def __init__(self):
        self.released = 0

    def release(self):
        self.released += 1
        return {"ok": True, "stopped": ["h3"]}


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_idle_release_fires_once_after_15_minutes_of_an_empty_queue(tmp_path):
    root = q.layout(tmp_path / "queue")["root"]
    client, clock = _FakeClient(), _Clock()
    idle = worker._IdleRelease(15, client, clock=clock)
    idle.tick(root)
    clock.t = 899.0
    idle.tick(root)
    assert client.released == 0
    clock.t = 900.0
    idle.tick(root)
    clock.t = 5000.0
    idle.tick(root)
    assert client.released == 1


def test_idle_countdown_restarts_while_anything_is_queued(tmp_path):
    root = q.layout(tmp_path / "queue")["root"]
    client, clock = _FakeClient(), _Clock()
    idle = worker._IdleRelease(15, client, clock=clock)
    idle.tick(root)
    q.submit(root, ["generate", "--tag", "a"], "", {"output_stem": str(tmp_path / "h3-a")}, {})
    clock.t = 1000.0
    idle.tick(root)                                   # pending job: the countdown is cleared
    assert client.released == 0
    for job in q.scan(root)[0]:
        q.cancel(root, job.id)
    clock.t = 1001.0
    idle.tick(root)
    clock.t = 1001.0 + 899.0
    idle.tick(root)
    assert client.released == 0
    clock.t = 1001.0 + 900.0
    idle.tick(root)
    assert client.released == 1


def test_main_loop_releases_an_idle_card_on_sglang(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    monkeypatch.setenv("H3_IDLE_RELEASE_MIN", "0")
    disp = FakeDispatcher()
    monkeypatch.setenv("H3_DISPATCHER_URL", disp.url)
    root = q.layout(tmp_path / "queue")["root"]
    try:
        worker.main_loop(root, poll=0.01, stop=_stop_after(0.5), outdir=tmp_path)
    finally:
        disp.close()
    assert [c[1] for c in disp.calls] == ["/release"]


def test_main_loop_never_talks_to_a_dispatcher_on_mlx(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_IDLE_RELEASE_MIN", "0")
    disp = FakeDispatcher()
    monkeypatch.setenv("H3_DISPATCHER_URL", disp.url)
    root = q.layout(tmp_path / "queue")["root"]
    try:
        worker.main_loop(root, poll=0.01, stop=_stop_after(0.3), outdir=tmp_path)
    finally:
        disp.close()
    assert disp.calls == []


class _Status:
    def __init__(self, answers):
        self.answers = list(answers)

    def status(self):
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def _assembly(tmp_path):
    root = q.layout(tmp_path / "queue")["root"]
    q.submit(root, ["assemble", "--project", str(tmp_path / "p.json")], "assemble P",
             {"output_stem": str(tmp_path / "job-final")}, {}, kind=q.KIND_ASSEMBLE)
    return root, q.claim(root)


def test_assembly_waits_while_our_engine_is_still_starting_and_says_why(tmp_path):
    root, job = _assembly(tmp_path)
    seen = []
    client = _Status([{"own": {"h3": {"ready": False, "started_at": 1000.0}}},
                      {"own": {"h3": {"ready": False, "started_at": 1000.0}}},
                      {"own": {"h3": {"ready": True, "started_at": 1000.0}}}])
    assert worker._wait_for_engine_start_to_settle(
        root, job, client, sleep=_recording_sleep(root, job.id, seen), clock=lambda: 1001.0) is None
    assert seen == ["ждём: поднимается h3, сборка после"]
    assert [j for j in q.scan(root)[0] if j.id == job.id][0].wait_reason is None


def test_assembly_wait_is_cancellable(tmp_path):
    root, job = _assembly(tmp_path)
    client = _Status([{"own": {"h3": {"ready": False, "started_at": 1000.0}}}])
    got = worker._wait_for_engine_start_to_settle(
        root, job, client, clock=lambda: 1001.0,
        sleep=lambda s: q.request_cancel(root, job.id, "cancelled_by_user"))
    assert got == "cancelled_by_user"


def test_assembly_cancelled_before_the_wait_does_not_even_ask_the_dispatcher(tmp_path):
    root, job = _assembly(tmp_path)
    q.request_cancel(root, job.id, "cancelled_by_user")

    class _Boom:
        def status(self):
            raise AssertionError("must not ask")

    assert worker._wait_for_engine_start_to_settle(root, job, _Boom()) == "cancelled_by_user"


def test_assembly_wait_gives_up_fifteen_minutes_after_the_engine_started(tmp_path):
    root, job = _assembly(tmp_path)
    client = _Status([{"own": {"h3": {"ready": False, "started_at": 1000.0}}}])
    sleeps = []
    now = [1000.0 + 900.0]            # exactly at the limit: still waiting
    clock = lambda: now[0]            # noqa: E731

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += 1.0                 # every slice moves the clock on

    assert worker._wait_for_engine_start_to_settle(root, job, client, sleep=sleep, clock=clock) is None
    assert sleeps == [1.0] * 30      # one 30 s retry period, then past the limit


def test_unknown_temperature_does_not_block_the_gate(running):
    root, job, _ = running
    fake = FakeDispatcher(gpu_null=True)
    try:
        gate = worker.make_gpu_gate(root, "h3", client=dc.DispatcherClient(fake.url),
                                    sleep=lambda s: pytest.fail("must not wait"))
        assert gate(job) is None
    finally:
        fake.close()


def test_temperature_lost_while_cooling_lets_the_job_go(running):
    root, job, _ = running

    class _Client:
        def __init__(self):
            self.statuses = [{"gpu": {"temperature_c": 85}}, {"gpu": None, "gpu_error": "x"}]

        def acquire(self, engine):
            return {"state": "ready"}

        def status(self):
            return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    seen = []
    gate = worker.make_gpu_gate(root, "h3", client=_Client(),
                                sleep=_recording_sleep(root, job.id, seen))
    assert gate(job) is None
    assert seen == ["остываем, 85 °C"]


def test_nvidia_smi_down_wait_reason_is_shown_as_is(running):
    root, job, _ = running
    fake = FakeDispatcher(acquire=(
        {"ok": True, "state": "wait", "engine": "h3", "reason": "nvidia-smi недоступен"},
        {"ok": True, "state": "ready", "engine": "h3"}))
    seen = []
    try:
        gate = worker.make_gpu_gate(root, "h3", client=dc.DispatcherClient(fake.url),
                                    sleep=_recording_sleep(root, job.id, seen))
        assert gate(job) is None
    finally:
        fake.close()
    assert seen == ["ждём GPU: nvidia-smi недоступен"]


def test_client_timeouts_cover_the_dispatcher_stopping_its_engine(monkeypatch):
    """The dispatcher may hold /acquire and /release for up to 120 s while it stops its own
    engine; /status is a quick read. And no Origin header: the dispatcher answers 403 to one."""
    seen = []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout=None):
        seen.append((request.full_url.rsplit("/", 1)[1], timeout,
                     {k.lower() for k in request.headers}))
        return _Resp()

    monkeypatch.setattr(dc.urllib.request, "urlopen", fake_urlopen)
    client = dc.DispatcherClient("http://x:1")
    client.status()
    client.acquire("h3")
    client.release()
    client.qwen_unload()
    client.qwen_restore()
    assert [(name, timeout) for name, timeout, _ in seen] == [
        ("status", 10.0), ("acquire", 130.0), ("release", 130.0),
        ("unload", 130.0), ("restore", 130.0)]
    assert all("origin" not in headers for _, _, headers in seen)
