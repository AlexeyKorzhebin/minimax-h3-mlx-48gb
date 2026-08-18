"""The worker: the lease, the loop, the stop signals, and recovery from being killed mid-run.

**Nothing here starts a real generation.** Every test substitutes `spawn`, because the real
command loads 36 GB of MLX weights and this machine has 48 GB -- a second one means swap, and swap
once turned a 568-second step into 818. A test that hangs here is the symptom of a substitution
that did not take.

Two shapes recur, and both exist because the obvious version of the test passes against the bug it
is supposed to catch:

* **The lease is checked while the child is running**, never only at spawn time. A worker that
  takes the lease and releases it the instant the child starts satisfies "the lease was held when
  `spawn` was called" perfectly, and that is precisely the defect the lease exists to prevent.
* **Signals are checked on a real grandchild.** `job_command` puts `caffeinate` in front of the
  Python that holds the weights, so a stop that signals only the direct child leaves a 36 GB
  orphan. A fake `caffeinate` cannot show that; a real process tree can.

Interruptions are real too: the fault-injection tests kill an actual worker process with
`os._exit(1)` at a chosen point rather than hand-crafting the files a crash would have left, which
is the only way the test can be wrong in the same direction the code can.
"""
import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from h3_48gb import assemble
from h3_48gb import project as project_module
from h3_48gb import queue as q
from h3_48gb import songrun as sr
from h3_48gb import worker
# `flock` is only honest when the holder is a separate process; `_external_lock` is the queue
# suite's helper for exactly that, extended there with `name=` so it can hold `worker.lock` and
# `leases/<id>.lock` as well as `queue.lock`.
from _fake_llama import _FakeLlama
from test_queue import _DRY, _answer_within, _external_lock, _stem

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class _FakeProcess:
    """Stands in for `subprocess.Popen`: exits immediately with `returncode`, writing `output` to
    whatever stream `run_job` handed it, so the log tail has something real to read.
    """

    def __init__(self, returncode=0, output="", kwargs=None):
        self.returncode = returncode
        self.output = output
        self.kwargs = dict(kwargs or {})
        self.pid = os.getpid()

    def wait(self, timeout=None):
        stream = self.kwargs.get("stdout")
        if stream is not None and self.output:
            stream.write(self.output.encode("utf-8"))
            stream.flush()
        return self.returncode


def _recording(spawned):
    """A `spawn` that records every command line it is asked to start and exits 0 at once."""

    def spawn(cmd, **kw):
        spawned.append(list(cmd))
        return _FakeProcess(0, "ok\n", kw)

    return spawn


def _stop_after(seconds):
    """An event that sets itself after `seconds`, so a loop under test can never hang the suite."""
    event = threading.Event()
    timer = threading.Timer(seconds, event.set)
    timer.daemon = True
    timer.start()
    return event


def _queued(root, tmp_path, tag="a"):
    """One pending job whose `output_stem` is under `tmp_path`, so an `.mp4` can be planted at it."""
    return q.submit(root, ["generate", "--tag", tag], "",
                    _stem(_DRY, str(tmp_path / f"h3-{tag}-1x1")), {})


# -- Step 3: the command, and that `run_job` actually uses it -----------------------------------


def test_job_command_wraps_the_job_in_caffeinate_and_this_interpreter(tmp_path):
    root = tmp_path / "queue"
    job = _queued(root, tmp_path)
    # `job.args` itself, not a hard-coded list: `submit` (task A6) appends the job's own
    # `--outdir`, whose value is a timestamp this test does not pin. What this test is actually
    # about -- the caffeinate/python/module wrapping -- does not depend on that value.
    assert worker.job_command(job, python="/venv/bin/python") == [
        "caffeinate", "-dimsu", "/venv/bin/python", "-m", "h3_48gb", *job.args]
    assert job.args[:3] == ["generate", "--tag", "a"], "the job's own args must pass through"
    assert worker.job_command(job)[2] == sys.executable, (
        "the child must run in the worker's own virtualenv, not whichever python is on PATH")


def test_run_job_launches_exactly_the_command_job_command_builds(tmp_path):
    """A correct `job_command` proves nothing if `run_job` builds its own line and skips
    caffeinate. On 2026-08-10 an idle sleep took the GPU firmware down mid-run.
    """
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _stem(_DRY, str(tmp_path / "h3-a-1x1")), {})
    job = q.claim(root)
    seen = {}

    def spawn(cmd, **kw):
        seen["cmd"] = list(cmd)
        seen["session"] = kw.get("start_new_session")
        return _FakeProcess(0, "ok\n", kw)

    worker.run_job(root, job, spawn=spawn)
    assert seen["cmd"] == worker.job_command(job)
    assert seen["cmd"][:2] == ["caffeinate", "-dimsu"]
    assert seen["session"] is True


def test_run_job_files_the_exit_code_and_the_log_tail(tmp_path):
    """The subprocess's own output is the only explanation a human gets for a failed run, and the
    exit code is what routes the job to `failed/` instead of `done/`.
    """
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _stem(_DRY, str(tmp_path / "h3-a-1x1")), {})
    job = q.claim(root)

    code = worker.run_job(root, job, spawn=lambda cmd, **kw: _FakeProcess(137, "OOM\n", kw))

    assert code == 137
    assert q.job_path(root, job.id, "failed").exists()
    on_disk = json.loads(q.job_path(root, job.id, "failed").read_text())
    assert on_disk["exit_code"] == 137
    assert "OOM" in on_disk["log_tail"], "the tail of the run's log must reach the job file"
    assert q.log_path(root, job.id).read_text() == "OOM\n", (
        "the subprocess's output must go to queue/logs/<id>.log, not to the worker's own stdout")


def test_run_job_lands_the_result_inside_the_jobs_own_subdirectory(tmp_path):
    """Task A6, end to end through the worker: `submit` rewrote `--outdir` in `job.args` to the
    job's own subdirectory (`<outdir>/<YYYYMMDD-HHMM>-<slug>/`), and the fake `spawn` below plays
    the part of `h3 generate` -- it reads `--outdir` out of the command line it is actually given
    (not a path this test already knows, which would prove nothing about `job.args` reaching the
    child) and creates that directory before writing into it, exactly as `RunSpec.outdir.mkdir(...)`
    does for a real run (see `test_run_generate_creates_a_nonexistent_outdir` in `test_cli.py`).
    The result must land there, and `run_job`/`reconcile` must find it there.
    """
    root = tmp_path / "queue"
    outdir = tmp_path / "out"
    job = q.submit(root, ["generate", "--tag", "kot-italy", "--outdir", str(outdir)], "",
                   {"output_stem": str(outdir / "h3-kot-italy-1x1")}, {})
    assert Path(job.output_stem).parent != outdir, (
        "the fixture must actually exercise a relocated job, or this test proves nothing")
    claimed = q.claim(root)

    def spawn(cmd, **kw):
        job_outdir = Path(cmd[cmd.index("--outdir") + 1])
        job_outdir.mkdir(parents=True, exist_ok=True)
        (job_outdir / "h3-kot-italy-1x1.mp4").write_bytes(b"video")
        return _FakeProcess(0, "готово\n", kw)

    code = worker.run_job(root, claimed, spawn=spawn)

    assert code == 0
    assert q.job_path(root, job.id, "done").exists()
    result_clip = Path(f"{job.output_stem}.mp4")
    assert result_clip.exists(), f"the clip must land at the job's own output_stem: {result_clip}"
    assert result_clip.parent == Path(job.output_stem).parent
    assert result_clip.read_bytes() == b"video"
    # And it is really the job's *own* subdirectory, not the plain outdir the form named.
    assert result_clip.parent.parent == outdir


# -- Step 4: the lease covers the child, and the marker lands before the lease is released -------


def test_the_lease_covers_the_child_and_the_marker_lands_before_release(tmp_path):
    """Checking the lease only at spawn time passes even if it is released immediately after
    the child starts -- which is exactly the bug the lease exists to prevent.
    """
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _stem(_DRY, str(tmp_path / "h3-a-1x1")), {})
    job = q.claim(root)
    observed = {}

    class _Proc:
        returncode = 0

        def wait(self, timeout=None):
            observed["lease_during_wait"] = q.lease_is_free(root, job.id) is False
            observed["marker_during_wait"] = q.result_path(root, job.id).exists()
            return 0

    def spawn(*a, **kw):
        # The spec's wording is "the worker takes the lease **before starting the subprocess**",
        # and nothing else in the suite pins that half down: moving `_acquire_lease` to just after
        # `spawn` leaves every other test green, while opening a window in which the job is in
        # `running/` with a live child and no lease -- a reconciling worker would call it dead.
        observed["lease_at_spawn"] = q.lease_is_free(root, job.id) is False
        return _Proc()

    marker_seen_free = {}
    real_release = worker._release_lease

    def spy_release(*a, **kw):
        marker_seen_free["marker_before_release"] = q.result_path(root, job.id).exists()
        return real_release(*a, **kw)

    worker._release_lease = spy_release
    try:
        worker.run_job(root, job, spawn=spawn)
    finally:
        worker._release_lease = real_release

    assert observed["lease_at_spawn"] is True, "the lease must be held before the child is started"
    assert observed["lease_during_wait"] is True, "the lease was released while the child ran"
    assert observed["marker_during_wait"] is False, "the marker was written before the child exited"
    assert marker_seen_free["marker_before_release"] is True, (
        "the lease was released before the result marker existed -- a crash there is unrecoverable")


def test_the_job_leaves_running_only_after_the_lease_has_been_released(tmp_path, monkeypatch):
    """The last two steps of `run_job`'s documented order. No corruption follows from swapping
    them today -- there is one worker, and it is the one holding the lease -- but the order is
    written down as a contract, and an undefended contract is a comment. It is also the order that
    keeps "the job is in `running/`" and "its lease is held" from ever disagreeing in the wrong
    direction: a job that reaches `done/` while its lease is still held reads, to anything probing
    the lease, as a finished job that is still generating.
    """
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _stem(_DRY, str(tmp_path / "h3-a-1x1")), {})
    job = q.claim(root)
    order: list[str] = []

    real_release = worker._release_lease
    real_finish = q.finish
    monkeypatch.setattr(worker, "_release_lease",
                        lambda fd: (order.append("release"), real_release(fd))[1])
    monkeypatch.setattr(q, "finish",
                        lambda *a, **kw: (order.append("finish"), real_finish(*a, **kw))[1])

    worker.run_job(root, job, spawn=lambda cmd, **kw: _FakeProcess(0, "ok\n", kw))

    assert order == ["release", "finish"], (
        f"the lease must be released before the job leaves running/; got {order}")
    assert q.job_path(root, job.id, "done").exists()


def test_the_lease_is_free_again_once_run_job_returns(tmp_path):
    """The other half of the test above: a lease that is never released is just as broken as one
    released too early -- the next reconciliation would call a finished job "alive" for ever and
    the queue would stop dead.
    """
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _stem(_DRY, str(tmp_path / "h3-a-1x1")), {})
    job = q.claim(root)
    worker.run_job(root, job, spawn=lambda cmd, **kw: _FakeProcess(0, "ok\n", kw))
    assert q.lease_is_free(root, job.id) is True


# -- Step 6: the loop takes the worker lock, and a second worker refuses -------------------------


def test_main_loop_refuses_to_start_when_another_worker_holds_the_lock(tmp_path):
    """Testing `hold_worker_lock` in isolation does not prove `main_loop` ever calls it.

    Wrapped in `_answer_within` rather than called directly (the brief's own snippet calls it
    directly): the refusal must be *immediate*, and the one-token way to break that -- dropping
    `LOCK_NB`, so the second worker silently queues up behind the first and starts running jobs
    hours later -- turns a direct call into a hang, which is not a red test but a stuck suite.
    """
    root = tmp_path / "queue"
    q.layout(root)
    with _external_lock(root, "LOCK_EX", name="worker.lock"):
        with pytest.raises(worker.WorkerAlreadyRunning):
            _answer_within(5, lambda: worker.main_loop(root, poll=0.01, stop=_stop_after(0.2)))


def test_a_worker_can_start_once_the_previous_one_has_finished(tmp_path):
    """The refusal above is only correct if it is temporary: a lock that is never released turns
    one crashed worker into a machine that can never run a job again. Two-sided by construction --
    the same call that must refuse above must succeed here.
    """
    root = tmp_path / "queue"
    q.layout(root)
    with worker.hold_worker_lock(root):
        pass
    spawned = []
    assert _answer_within(10, lambda: worker.main_loop(
        root, poll=0.01, stop=_stop_after(0.2), spawn=_recording(spawned))) == 0


def test_a_momentary_holder_does_not_look_like_a_second_worker(tmp_path):
    """`web.worker_state` answers "is a worker running" by taking this very lock non-blockingly
    and dropping it again, so an `h3 worker` starting inside that microsecond window used to die
    with `worker_already_running`, naming a worker that was really a liveness probe. With the page
    polling every twenty seconds beside the terminal the human types into, that window gets hit.

    Driven with a stub rather than a real race: one `BlockingIOError` and then success is exactly
    what a probe leaves behind, and a real race would either be flaky or never reproduce at all.
    """
    root = tmp_path / "queue"
    q.layout(root)
    real = fcntl.flock
    calls = []

    def flock_once_busy(fd, operation):
        calls.append(operation)
        if len(calls) == 1:
            raise BlockingIOError(35, "Resource temporarily unavailable")
        return real(fd, operation)

    with mock.patch.object(worker.fcntl, "flock", flock_once_busy):
        with worker.hold_worker_lock(root):
            pass
    assert len(calls) >= 2, "hold_worker_lock gave up on the first BlockingIOError"


def test_a_worker_that_really_is_running_still_refuses(tmp_path):
    """The other half of the retry: it must distinguish a probe from a worker, not swallow both.
    A real worker holds the lock for days, so it is still there after the retry -- checked with a
    lock held by a separate process, and bounded in time so a retry loop that never gave up would
    fail rather than hang.
    """
    root = tmp_path / "queue"
    q.layout(root)
    with _external_lock(root, "LOCK_EX", name="worker.lock"):
        started = time.monotonic()
        with pytest.raises(worker.WorkerAlreadyRunning):
            _answer_within(5, lambda: worker.hold_worker_lock(root).__enter__())
        assert time.monotonic() - started < 2, "the refusal stopped being immediate"


def test_main_loop_runs_the_queue_in_order_and_counts_what_it_ran(tmp_path):
    """`_answer_within` here guards the other half of `stop`: a loop that never consults it runs
    for ever, and "for ever" is a hung suite rather than a failing test.
    """
    root = tmp_path / "queue"
    first = _queued(root, tmp_path, tag="a")
    second = _queued(root, tmp_path, tag="b")
    q.set_paused(root, False)  # A5: a freshly created queue starts paused; not what this test is about
    spawned: list[list[str]] = []

    ran = _answer_within(10, lambda: worker.main_loop(
        root, poll=0.01, stop=_stop_after(1.0), spawn=_recording(spawned)))

    assert ran == 2, f"the loop must run both queued jobs, ran {ran}"
    # `--tag`'s value, not the command's last token: `submit` (task A6) now appends the job's own
    # `--outdir` after `--tag`, so the last token is a path, not the tag.
    assert [cmd[cmd.index("--tag") + 1] for cmd in spawned[:2]] == ["a", "b"], (
        "jobs must run in queue order")
    assert q.job_path(root, first.id, "done").exists()
    assert q.job_path(root, second.id, "done").exists()


def test_the_queue_pauses_itself_once_it_runs_dry_but_not_between_jobs(tmp_path):
    """Очередь, опустевшая сама собой, обязана встать на паузу.

    Иначе вечер выглядит так: ночная пачка досчиталась, человек утром кидает в форму пару задач,
    чтобы посмотреть на них перед запуском, — и они стартуют сами, потому что работник всё ещё
    крутится и берёт всё, что видит. Маркер после последней задачи возвращает решение «считать»
    человеку.

    Ровно два утверждения, и второе не менее важно первого: *между* задачами паузы быть не должно,
    иначе пачка из десяти роликов встанет после первого же.
    """
    root = tmp_path / "queue"
    _queued(root, tmp_path, tag="a")
    _queued(root, tmp_path, tag="b")
    q.set_paused(root, False)
    spawned: list[list[str]] = []

    ran = _answer_within(10, lambda: worker.main_loop(
        root, poll=0.01, stop=_stop_after(1.0), spawn=_recording(spawned)))

    assert ran == 2, f"пауза не должна вставать между задачами, пока pending непуст, ran {ran}"
    assert [cmd[cmd.index("--tag") + 1] for cmd in spawned] == ["a", "b"], (
        "обе задачи обязаны отсчитаться подряд")
    assert q.is_paused(root) is True, "опустевшая очередь обязана поставить маркер паузы"


def test_a_job_added_after_the_queue_drained_waits_for_a_human(tmp_path):
    """Вторая половина того же правила: маркер, поставленный опустевшей очередью, обязан
    удерживать следующую задачу — иначе он не значит ничего.
    """
    root = tmp_path / "queue"
    _queued(root, tmp_path, tag="a")
    q.set_paused(root, False)
    spawned: list[list[str]] = []
    _answer_within(10, lambda: worker.main_loop(
        root, poll=0.01, stop=_stop_after(0.6), spawn=_recording(spawned)))
    assert q.is_paused(root) is True

    _queued(root, tmp_path, tag="b")            # человек накидал новую поверх стоящего маркера
    after: list[list[str]] = []
    ran = _answer_within(10, lambda: worker.main_loop(
        root, poll=0.01, stop=_stop_after(0.6), spawn=_recording(after)))

    assert (ran, after) == (0, []), "задача поверх стоящего маркера не должна стартовать сама"
    assert q.job_path(root, q.scan(root)[0][0].id, "pending").exists(), (
        "она обязана остаться ждущей, а не пропасть")


# -- Step 7: a live job elsewhere defers the queue, and the queue resumes afterwards -------------


def test_the_worker_waits_for_a_live_job_then_picks_the_queue_back_up(tmp_path):
    """Two MLX processes at 36 GB on a 48 GB machine means swap, and swap once turned a
    568-second step into 818. But waiting for ever is also wrong: once the other job is gone,
    the queue must move.
    """
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _stem(_DRY, str(tmp_path / "h3-a-1x1")), {})
    alive = q.claim(root)
    q.submit(root, ["generate", "--tag", "b"], "", _stem(_DRY, str(tmp_path / "h3-b-1x1")), {})
    q.set_paused(root, False)  # A5: a freshly created queue starts paused; not what this test is about
    spawned: list[list[str]] = []
    holder = _external_lock(root, "LOCK_EX", name=f"leases/{alive.id}.lock")
    holder.__enter__()
    stop = threading.Event()
    thread = threading.Thread(target=lambda: worker.main_loop(
        root, poll=0.05, stop=stop, spawn=_recording(spawned)), daemon=True)
    thread.start()
    time.sleep(0.4)
    assert spawned == [], "the worker started a second job while another was still running"
    holder.__exit__(None, None, None)
    deadline = time.time() + 5
    while not spawned and time.time() < deadline:
        time.sleep(0.05)
    stop.set()
    thread.join(5)
    assert spawned, "the worker never resumed after the other job's lease was released"


# -- Task 6: a resident LLM holding the GPU defers the queue too ---------------------------------


def test_worker_leaves_the_queue_alone_while_the_llm_holds_the_gpu(tmp_path, monkeypatch):
    """A resident local LLM and a 27 GB generation cannot share 48 GB. The page is the one that
    asks the human and calls `POST /api/llm/unload` -- the worker only ever watches the port, so
    this test drives `_llm_holds_gpu` directly rather than standing up a real llama-server.
    """
    root = tmp_path / "queue"
    _queued(root, tmp_path, tag="a")
    q.set_paused(root, False)  # A5: a freshly created queue starts paused; not what this test is about
    holds = {"value": True}
    monkeypatch.setattr(worker, "_llm_holds_gpu", lambda r: holds["value"])
    spawned: list[list[str]] = []
    stop = threading.Event()

    thread = threading.Thread(target=lambda: worker.main_loop(
        root, poll=0.05, stop=stop, spawn=_recording(spawned)), daemon=True)
    thread.start()
    time.sleep(0.3)
    assert spawned == [], "the worker took a job while the LLM still held the GPU"
    holds["value"] = False  # the page confirmed with the human and called /api/llm/unload
    deadline = time.time() + 5
    while not spawned and time.time() < deadline:
        time.sleep(0.05)
    stop.set()
    thread.join(timeout=5)
    assert spawned, "the worker never resumed once the LLM released the GPU"


@pytest.mark.parametrize("pass_outdir", [True, False],
                         ids=["outdir_explicit", "outdir_default_via_roots_parent"])
def test_worker_reads_providers_json_from_outdir_not_the_queue_root(tmp_path, pass_outdir):
    """`cli.run_worker` passes `queue_root(outdir) == outdir/"queue"` as `root`, while
    `providers.json` lives in `outdir` itself (`provider.load_providers`, and `_Live.root` in
    `test_chat_web.py`). Stubbing `_llm_holds_gpu` the way the test above does cannot catch that
    mismatch -- it has to be driven end to end, through a real (fake) llama-server's `/health`,
    with `providers.json` sitting where the web server actually puts it.

    Parametrized over whether `outdir` is passed explicitly or left to default to `root.parent`:
    both must resolve to the same directory, because `cli.run_worker` is only one of the two
    callers -- anything that calls `main_loop` directly with `root == queue_root(outdir)` and no
    `outdir=` must keep working exactly as before this fix.
    """
    outdir = tmp_path
    root = outdir / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _stem(_DRY, str(tmp_path / "h3-a-1x1")), {})
    q.set_paused(root, False)  # A5: a freshly created queue starts paused; not what this test is about
    llama = _FakeLlama()
    (outdir / "providers.json").write_text(json.dumps({
        "active": "qwen-local",
        "providers": {"qwen-local": {"type": "llama-local", "port": llama.port}},
    }), encoding="utf-8")
    spawned: list[list[str]] = []
    stop = threading.Event()
    kwargs = {"poll": 0.05, "stop": stop, "spawn": _recording(spawned)}
    if pass_outdir:
        kwargs["outdir"] = outdir

    thread = threading.Thread(target=lambda: worker.main_loop(root, **kwargs), daemon=True)
    thread.start()
    closed = False
    try:
        time.sleep(0.3)
        assert spawned == [], "the worker took a job while the fake llama-server was still up"
        llama.close()
        closed = True
        deadline = time.time() + 5
        while not spawned and time.time() < deadline:
            time.sleep(0.05)
        assert spawned, "the worker never resumed once providers.json's LLM went down"
    finally:
        stop.set()
        thread.join(timeout=5)
        if not closed:
            llama.close()


def test_llm_holds_gpu_sees_a_resident_local_provider_that_is_not_the_active_one(tmp_path):
    """Finding I1: the chat page's per-turn `provider` field can raise ANY `llama-local` entry in
    the roster, not only `providers.json`'s `active` one -- a human picks it from a dropdown, the
    active field never moves. `_llm_holds_gpu` used to read only the active provider's port, so
    "active external, `gemma-local` resident on its own port" was invisible to this gate: the
    worker would claim a 27 GB job right next to a 30 GB resident model. Today this is additionally
    masked because the real roster's two local providers share one port (8080), which is exactly
    why the roster below uses two distinct providers rather than relying on that coincidence.
    """
    outdir = tmp_path
    llama = _FakeLlama()
    try:
        (outdir / "providers.json").write_text(json.dumps({
            "active": "openrouter",
            "providers": {
                "openrouter": {"type": "openai", "base_url": "https://example.invalid/v1",
                               "model": "m"},
                "gemma-local": {"type": "llama-local", "port": llama.port},
            },
        }), encoding="utf-8")
        assert worker._llm_holds_gpu(outdir) is True
    finally:
        llama.close()


# -- Task A5: pause/start the queue ---------------------------------------------------------------


def test_worker_leaves_a_pending_job_alone_while_the_queue_is_paused(tmp_path):
    """No monkeypatch, unlike the LLM-gate test above: `q.is_paused`/`q.set_paused` are real files
    on real disk, driven exactly the way `POST /api/queue/pause`/`/start` will drive them (task
    A5's web routes), so this test exercises `main_loop`'s own gate rather than a stand-in for it --
    a stub of the gate itself cannot catch a mutation that comments the gate out of `main_loop`.

    A freshly created queue starts paused (`queue.layout`'s own contract), so the marker does not
    even need to be planted by hand: `_queued` -> `q.submit` -> `q.layout` already leaves one.
    """
    root = tmp_path / "queue"
    _queued(root, tmp_path, tag="a")
    assert q.is_paused(root) is True, "a freshly created queue must start paused"
    spawned: list[list[str]] = []
    stop = threading.Event()

    thread = threading.Thread(target=lambda: worker.main_loop(
        root, poll=0.05, stop=stop, spawn=_recording(spawned)), daemon=True)
    thread.start()
    time.sleep(0.3)
    assert spawned == [], "the worker took a pending job while the queue was paused"
    q.set_paused(root, False)  # the page's «начать расчёт» button, or a human by hand
    deadline = time.time() + 5
    while not spawned and time.time() < deadline:
        time.sleep(0.05)
    stop.set()
    thread.join(timeout=5)
    assert spawned, "the worker never resumed once the queue was unpaused"


# -- BACKLOG "UX-мелочи", task 1: the pause gate must survive a broken providers.json ------------


def test_worker_stays_paused_and_alive_with_a_broken_providers_json(tmp_path):
    """Before this fix, `main_loop` checked the LLM gate (`_llm_holds_gpu`, which reads
    `providers.json` through `provider.load_providers`) *before* `q.is_paused`. A `providers.json`
    that fails to parse -- mid-write, a bad hand-edit -- raised straight out of `load_providers`,
    which killed the worker before the pause check ever ran, even though the queue was paused and
    would have refused the job regardless. Pausing has to work no matter what nonsense sits in a
    file the worker does not own.

    The probe runs the loop in a thread so a crash is observable as the thread dying rather than as
    an exception this test would otherwise never see (daemon threads swallow it).
    """
    root = tmp_path / "queue"
    outdir = tmp_path
    _queued(root, tmp_path, tag="a")
    assert q.is_paused(root) is True, "a freshly created queue must start paused"
    (outdir / "providers.json").write_text("{ this is not json", encoding="utf-8")
    spawned: list[list[str]] = []
    crashed: list[BaseException] = []
    stop = threading.Event()

    def run():
        try:
            worker.main_loop(root, poll=0.05, stop=stop, spawn=_recording(spawned), outdir=outdir)
        except BaseException as exc:  # noqa: BLE001 -- the point is that nothing escapes main_loop
            crashed.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.3)
    assert thread.is_alive(), f"the worker died on a broken providers.json: {crashed}"
    assert spawned == [], "a paused queue must not take a job, broken providers.json or not"
    stop.set()
    thread.join(timeout=5)
    assert crashed == [], f"the worker must survive a broken providers.json: {crashed}"


def test_llm_holds_gpu_survives_a_broken_providers_json(tmp_path, capsys):
    """`_llm_holds_gpu` itself must not propagate a broken roster: it answers `False` (no LLM
    visible, the same direction `reconcile`/`claim` already fail toward) and writes exactly one
    line to stderr, rather than dying and rather than staying silent -- a corrupt file that never
    logged anything could sit unnoticed for days with the gate quietly never firing again.
    """
    outdir = tmp_path
    (outdir / "providers.json").write_text("{ this is not json", encoding="utf-8")
    assert worker._llm_holds_gpu(outdir) is False
    err = capsys.readouterr().err
    assert err.count("\n") == 1, f"expected exactly one line on stderr, got: {err!r}"
    assert "providers.json" in err


# -- Step 9: stopping ----------------------------------------------------------------------------


#: A worker driven from a real process, so real signals can be delivered to it. `spawn` is
#: substituted here exactly as it is in-process: `MODE` decides whether the stand-in child is a
#: simple sleeper or a two-level tree (a parent that outlives nothing and a grandchild that holds
#: the memory), which is the shape `caffeinate -dimsu python -m h3_48gb` actually has.
_SIGNAL_WORKER = """
import subprocess, sys
sys.path.insert(0, {project!r})
from h3_48gb import worker

root, mode, argument = sys.argv[1], sys.argv[2], sys.argv[3]

_GRANDCHILD = (
    "import subprocess, sys\\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\\n"
    "child.wait()\\n")


def spawn(cmd, **kw):
    if mode == "sleeper":
        line = [sys.executable, "-c", "import time; time.sleep(%s)" % argument]
    else:
        line = [sys.executable, "-c", _GRANDCHILD, argument]
    proc = subprocess.Popen(line, **kw)
    print("spawned", flush=True)
    return proc


worker.main_loop(root, poll=0.05, spawn=spawn)
print("stopped", flush=True)
"""


@contextlib.contextmanager
def _signal_worker(root, mode, argument):
    """Start a worker in its own process, block until it has a child running, and make sure it is
    gone by the end of the block however the test turns out -- a worker whose loop ignored the
    stop request would otherwise outlive the failing test and keep spawning children for the rest
    of the session.
    """
    script = _SIGNAL_WORKER.format(project=str(PROJECT_ROOT))
    # `start_new_session=True` for the *worker under test*, not only for its child. Without it the
    # worker sits in pytest's own process group, and the moment a mutation drops
    # `start_new_session=True` from `run_job` the job's child joins that same group -- at which
    # point the worker's `killpg` on the second stop signal takes the test runner down with it.
    # That is not a red test, it is a run that vanishes mid-output: verified against exactly that
    # mutation. The harness must contain its own blast radius.
    proc = subprocess.Popen([sys.executable, "-c", script, str(root), mode, str(argument)],
                            stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert proc.stdout.readline().strip() == "spawned", "the worker never started a job"
        yield proc
    finally:
        # The whole group, for the same reason the worker signals the whole group: killing the
        # worker alone would leave its child (and grandchild) running for the rest of the session.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        proc.wait(timeout=10)


def test_the_first_stop_signal_lets_the_running_job_finish_and_takes_no_new_one(tmp_path):
    """Interrupting a run because someone asked the *queue* to stop throws away hours of GPU time.
    The discriminator is the clock: a worker that killed its child would exit immediately and file
    the job under `failed/`, so "the job is in done/" alone is not enough -- the elapsed time has
    to show it actually waited for the child it was told not to interrupt.
    """
    root = tmp_path / "queue"
    first = _queued(root, tmp_path, tag="a")
    second = _queued(root, tmp_path, tag="b")
    q.set_paused(root, False)  # A5: a freshly created queue starts paused; not what this test is about

    with _signal_worker(root, "sleeper", 1.5) as proc:
        started = time.time()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0, "a requested stop is not a failure"
        elapsed = time.time() - started

    assert elapsed >= 1.0, (
        f"the worker stopped after {elapsed:.2f}s -- it killed the running job instead of "
        "letting it finish")
    assert q.job_path(root, first.id, "done").exists(), "the running job must finish normally"
    assert q.job_path(root, second.id, "pending").exists(), (
        "the worker took a new job after being asked to stop")


def test_the_second_stop_signal_kills_the_grandchild_not_just_the_direct_child(tmp_path):
    """A signal to the direct child only would kill `caffeinate` and leave the nested Python --
    the process actually holding 36 GB -- orphaned, with no worker and no lease. This checks a
    real grandchild: a stand-in `caffeinate` that has no children of its own could not tell the
    difference. Two-sided: the grandchild must still be alive after the first signal, because the
    first signal is a request to stop taking work, not to abandon the run.
    """
    root = tmp_path / "queue"
    job = _queued(root, tmp_path, tag="a")
    q.set_paused(root, False)  # A5: a freshly created queue starts paused; not what this test is about
    pidfile = tmp_path / "grandchild.pid"

    with _signal_worker(root, "grandchild", pidfile) as proc:
        deadline = time.time() + 10
        while not pidfile.exists() and time.time() < deadline:
            time.sleep(0.05)
        grandchild = int(pidfile.read_text())
        os.kill(grandchild, 0)  # raises if the grandchild never came up at all

        proc.send_signal(signal.SIGTERM)
        time.sleep(0.5)
        os.kill(grandchild, 0)  # must NOT raise: the first signal leaves the run alone

        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == -signal.SIGTERM, (
            "the second signal must take the worker down with the default handler, not be "
            "swallowed")

    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(grandchild, signal.SIGKILL)  # do not leave it running for the rest of the session
        pytest.fail(f"the grandchild {grandchild} outlived the worker -- the signal reached only "
                    "the direct child, and a 36 GB orphan is exactly what that looks like in "
                    "production")

    assert q.job_path(root, job.id, "running").exists(), (
        "a force-stopped job stays in running/ for the next worker's reconciliation to judge")


# -- Step 10: a real crash at six points, each reconciled into the right row ---------------------


#: A worker that dies for real -- `os._exit(1)`, no unwinding, no `finally` -- at one chosen point
#: of a job's life. Hand-writing the files a crash "would have" left is a test of the test author's
#: imagination; killing a process at a named point is a test of the code.
_CRASH_WORKER = """
import os, sys
sys.path.insert(0, {project!r})
from h3_48gb import queue as q
from h3_48gb import worker

root, point, stem = sys.argv[1], sys.argv[2], sys.argv[3]
make_mp4 = sys.argv[4] == "mp4"
worker._now = lambda: "2020-01-01T00:00:00"

if point == "after_submit":
    q.submit(root, ["generate", "--tag", "z"], "", {{"output_stem": stem}}, {{}})
    os._exit(1)

job = q.claim(root)
if point == "after_claim":
    os._exit(1)


class _Proc:
    returncode = 0

    def wait(self, timeout=None):
        if point == "during_child":
            os._exit(1)
        if make_mp4:
            open(stem + ".mp4", "wb").write(b"video")
        return 0


def spawn(cmd, **kw):
    if point == "before_spawn":
        os._exit(1)
    return _Proc()


if point == "before_marker":
    q.write_result_marker = lambda *a, **k: os._exit(1)
if point == "before_rename":
    q.finish = lambda *a, **k: os._exit(1)

worker.run_job(root, job, spawn=spawn)
os._exit(0)
"""


def _crash_at(root, point, stem, make_mp4=False):
    """Run a worker in its own process and let it die at `point`. Returns nothing: what the crash
    left on disk is the whole result.
    """
    script = _CRASH_WORKER.format(project=str(PROJECT_ROOT))
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), point, str(stem), "mp4" if make_mp4 else "-"],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 1, (
        f"the worker was supposed to die at {point}, exited {result.returncode}: {result.stderr}")


@pytest.mark.parametrize("point", ["after_claim", "before_spawn", "during_child", "before_marker"])
def test_a_crash_before_any_result_returns_the_job_to_pending(tmp_path, point):
    """Rows 4 of the table, reached four different ways. Nothing survived the crash -- no marker,
    no `.mp4` -- so the job goes back to the queue and resumes from its checkpoint. The lease is
    gone in every case without anything cleaning it up, because `flock` dies with its process.
    """
    root = tmp_path / "queue"
    stem = tmp_path / "h3-a-1x1"
    job = q.submit(root, ["generate", "--tag", "a"], "", _stem(_DRY, str(stem)), {})

    _crash_at(root, point, stem)

    assert q.job_path(root, job.id, "running").exists(), (
        f"a crash at {point} must leave the job in running/ for reconciliation to find")
    assert q.lease_is_free(root, job.id) is True, "the lease must not outlive the process holding it"

    result = q.reconcile(root)

    assert [j.state for j in result.changed] == ["pending"]
    assert q.job_path(root, job.id, "pending").exists()
    assert json.loads(q.job_path(root, job.id, "pending").read_text())["started_at"] is not None, (
        "the interrupted run keeps the moment it first started")


def test_a_crash_after_submit_leaves_a_pending_job_reconciliation_does_not_touch(tmp_path):
    """The first point: the queue file is committed before anything else happens, so a crash right
    after it has nothing to recover -- and reconciliation must not invent work for a job that
    never started.
    """
    root = tmp_path / "queue"
    stem = tmp_path / "h3-z-1x1"
    q.layout(root)

    _crash_at(root, "after_submit", stem)

    jobs, broken = q.scan(root)
    assert broken == [] and [j.state for j in jobs] == ["pending"], (
        "an interrupted submit must leave one clean pending job and no debris")
    result = q.reconcile(root)
    assert result.changed == [] and result.alive == []
    assert q.scan(root)[0][0].state == "pending"


def test_a_crash_between_the_child_exiting_and_the_marker_keeps_a_finished_mp4(tmp_path):
    """Row 3, and the reason it exists. The child succeeded and wrote its `.mp4`; the worker died
    before recording that. Re-running the job would overwrite the very file being rescued.
    """
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--tag", "a"], "", _stem(_DRY, str(tmp_path / "h3-a-1x1")), {})
    # `job.output_stem`, not the flat path handed to `submit` -- `submit` (task A6) relocates it
    # into the job's own subdirectory, and that is where the crashed run's `.mp4` really lands.
    # `h3 generate` creates this directory itself (`RunSpec.outdir.mkdir(...)` in `cli.py`, see
    # `test_run_generate_creates_a_nonexistent_outdir` in `test_cli.py`); the fake child here
    # stands in for the real one, so it has to do the same.
    Path(job.output_stem).parent.mkdir(parents=True, exist_ok=True)

    _crash_at(root, "before_marker", job.output_stem, make_mp4=True)

    assert not q.result_path(root, job.id).exists(), "the crash must land before the marker"
    result = q.reconcile(root)

    assert q.job_path(root, job.id, "done").exists()
    assert result.changed[0].exit_code == 0
    assert result.changed[0].log_tail == q.RESULT_RECOVERED_NOTE
    assert Path(f"{job.output_stem}.mp4").read_bytes() == b"video", (
        "the rescued result must be untouched")


def test_a_crash_between_the_marker_and_the_rename_finishes_from_the_marker(tmp_path):
    """Row 2, and the reason the marker is written before the lease is released: this is the one
    window where the job is still in `running/` but its outcome is already known for certain.
    """
    root = tmp_path / "queue"
    stem = tmp_path / "h3-a-1x1"
    job = q.submit(root, ["generate", "--tag", "a"], "", _stem(_DRY, str(stem)), {})

    _crash_at(root, "before_rename", stem)

    assert q.job_path(root, job.id, "running").exists()
    assert json.loads(q.result_path(root, job.id).read_text()) == {
        "exit_code": 0, "finished_at": "2020-01-01T00:00:00"}

    result = q.reconcile(root)

    assert q.job_path(root, job.id, "done").exists()
    assert result.changed[0].finished_at == "2020-01-01T00:00:00", (
        "the finish time must come from the marker the dead worker wrote, not from now")


# -- Step: dispatch by `job.kind` (task 3, "Проекты") --------------------------------------------


class _FakeCaffeinate:
    """Stands in for `subprocess.Popen(["caffeinate", "-dimsu"])`: never actually runs anything,
    just records that it was asked to start and stop.
    """

    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def _caffeinate_spy(calls):
    """A `spawn` that records every command it is asked to start (song/assemble dispatch's own
    `caffeinate -dimsu` companion process, see `worker._caffeinate_block`) and hands back a fake
    that never touches a real process.
    """

    def spawn(cmd, **kw):
        calls.append(list(cmd))
        return _FakeCaffeinate()

    return spawn


def _song_project(tmp_path, *, lyrics="[verse]\nстрока раз\nстрока два\n", caption="upbeat",
                   source="generate", mp3=None, title="Моя песня"):
    """A `kind="song"` project on disk with a track already populated -- the shape a completed
    script/track-parameters gate (task 6, out of this task's scope) would have left behind before
    a song job is ever queued against it.
    """
    proj = project_module.create_project(tmp_path / "out", "song", title)
    proj.track = {**proj.track, "lyrics": lyrics, "caption": caption, "source": source}
    if mp3 is not None:
        proj.track["mp3"] = str(mp3)
    proj.save()
    return proj


def _song_job(root, proj, tmp_path):
    """One `kind="song"` job queued against `proj`, claimed and ready for `worker.run_job`."""
    q.submit(root, ["song", "--project", str(proj.path)], "",
            {"output_stem": str(proj.path.parent / "track" / "song")}, {}, kind=q.KIND_SONG)
    return q.claim(root)


_FAKE_SECTIONS = [{"name": "verse", "start": 0.0, "end": 30.0}]


def _fake_song_result(track_dir: Path, *, undersung=False) -> sr.SongResult:
    return sr.SongResult(
        wav=track_dir / "song.wav", mastered_wav=track_dir / "song.mastered.wav",
        mp3=track_dir / "song.mp3", mastered_mp3=track_dir / "song.mastered.mp3",
        duration=30.0, transcript="ла ла ла", sections=_FAKE_SECTIONS, undersung=undersung,
    )


def test_run_job_dispatches_a_song_kind_to_songrun_run_song_and_gates_the_track(tmp_path,
                                                                                 monkeypatch):
    """`kind="song"` (design spec, "Трек"): the worker calls `songrun.run_song` in-process --
    never spawns `h3 generate` -- and, once it succeeds, moves `track.status` to
    `"awaiting_approval"` (the gate) through the *locked* mutators, not a blind `save()`.
    """
    root = tmp_path / "queue"
    proj = _song_project(tmp_path)
    job = _song_job(root, proj, tmp_path)
    track_dir = proj.path.parent / "track"
    fake_result = _fake_song_result(track_dir)

    seen = {}

    def fake_run_song(track_dir_arg, lyrics, caption, duration, *, seed, **kw):
        seen["track_dir"] = Path(track_dir_arg)
        seen["lyrics"] = lyrics
        seen["caption"] = caption
        seen["duration"] = duration
        return fake_result

    monkeypatch.setattr(sr, "run_song", fake_run_song)
    caffeinate_calls = []

    code = worker.run_job(root, job, spawn=_caffeinate_spy(caffeinate_calls))

    assert code == 0
    assert q.job_path(root, job.id, "done").exists()
    assert seen["track_dir"] == track_dir
    assert seen["lyrics"] == proj.track["lyrics"]
    assert seen["caption"] == proj.track["caption"]
    assert seen["duration"] == sr.estimate_duration(proj.track["lyrics"])
    assert caffeinate_calls == [["caffeinate", "-dimsu", "-w", str(os.getpid())]], (
        "a song job must be caffeinated the same way a generate job is (task 3 brief); M1 (fix "
        "round 1) additionally binds it to the worker's own pid so it cannot outlive a killed "
        "worker")
    assert caffeinate_calls  # sanity, redundant with the assert above

    reloaded = project_module.load_project(proj.path)
    assert reloaded.track["status"] == "awaiting_approval"
    assert reloaded.track["mp3"] == str(fake_result.mp3)
    assert reloaded.track["mastered_mp3"] == str(fake_result.mastered_mp3)
    assert reloaded.track["wav"] == str(fake_result.wav)
    assert reloaded.track["sections"] == _FAKE_SECTIONS
    assert reloaded.track["undersung"] is False
    assert reloaded.stages["track"] == "awaiting_approval"


def test_an_undersung_song_job_still_gates_the_track_as_a_success_with_a_warning(tmp_path,
                                                                                  monkeypatch):
    """Task 3's review decision: `undersung=True` is a *warning on a success*, not a failure -- the
    mp3s exist, the job is `done`, and the warning travels on `track.undersung` for the gate page
    to show, not on the job's exit code.
    """
    root = tmp_path / "queue"
    proj = _song_project(tmp_path)
    job = _song_job(root, proj, tmp_path)
    track_dir = proj.path.parent / "track"
    fake_result = _fake_song_result(track_dir, undersung=True)
    monkeypatch.setattr(sr, "run_song", lambda *a, **kw: fake_result)

    code = worker.run_job(root, job, spawn=_caffeinate_spy([]))

    assert code == 0, "undersung is a warning, not a job failure"
    assert q.job_path(root, job.id, "done").exists()
    reloaded = project_module.load_project(proj.path)
    assert reloaded.track["status"] == "awaiting_approval"
    assert reloaded.track["undersung"] is True


def test_a_song_job_with_an_imported_track_runs_the_align_only_path(tmp_path, monkeypatch):
    """`track.source == "import"` (design spec addendum, "импортированный трек"): generation and
    mastering are skipped entirely -- `songrun.run_song` must not even be called -- only
    `songrun.align_track` runs, against the already-uploaded mp3.
    """
    root = tmp_path / "queue"
    imported_mp3 = tmp_path / "uploaded" / "suno-song.mp3"
    imported_mp3.parent.mkdir(parents=True)
    imported_mp3.write_bytes(b"fake mp3 bytes")
    proj = _song_project(tmp_path, source="import", mp3=imported_mp3)
    job = _song_job(root, proj, tmp_path)
    track_dir = proj.path.parent / "track"
    fake_result = sr.SongResult(
        wav=imported_mp3, mastered_wav=imported_mp3, mp3=imported_mp3, mastered_mp3=imported_mp3,
        duration=42.0, transcript="ла ла ла", sections=_FAKE_SECTIONS, undersung=False,
    )

    def fail_run_song(*a, **kw):
        raise AssertionError("run_song must not be called for an imported track")

    seen = {}

    def fake_align_track(track_dir_arg, mp3_path, lyrics, **kw):
        seen["track_dir"] = Path(track_dir_arg)
        seen["mp3_path"] = Path(mp3_path)
        seen["lyrics"] = lyrics
        return fake_result

    monkeypatch.setattr(sr, "run_song", fail_run_song)
    monkeypatch.setattr(sr, "align_track", fake_align_track)

    code = worker.run_job(root, job, spawn=_caffeinate_spy([]))

    assert code == 0
    assert seen["track_dir"] == track_dir
    assert seen["mp3_path"] == imported_mp3
    reloaded = project_module.load_project(proj.path)
    assert reloaded.track["status"] == "awaiting_approval"
    assert reloaded.track["mp3"] == str(imported_mp3)


def test_a_song_job_with_an_imported_track_but_no_mp3_fails_honestly(tmp_path):
    """`track.source == "import"` with no `track.mp3` set is a malformed project, not something to
    crash the worker over -- the job fails (`failed/`), the project is left untouched.
    """
    root = tmp_path / "queue"
    proj = _song_project(tmp_path, source="import", mp3=None)
    job = _song_job(root, proj, tmp_path)

    code = worker.run_job(root, job, spawn=_caffeinate_spy([]))

    assert code == 1
    assert q.job_path(root, job.id, "failed").exists()
    reloaded = project_module.load_project(proj.path)
    assert reloaded.track["status"] == "draft", "an untouched project must not look gated"


def test_run_job_treats_a_kind_less_job_as_generate(tmp_path):
    """Backward compatibility end to end (task 3 brief, mandatory), not just at the dataclass level
    (`test_queue.py` covers that half): a job claimed from a `pending/<id>.json` written before
    `kind` existed must still dispatch through the subprocess branch.
    """
    root = tmp_path / "queue"
    q.layout(root)
    old_style = {
        "id": "20260101-000000-old-abcd", "created_at": "2026-01-01T00:00:00",
        "args": ["generate", "--tag", "old"], "note": "", "prompt_source": None,
        "prompt_sha256": None, "output_stem": str(tmp_path / "h3-old-1x1"),
        "estimate": {}, "priority": 0, "started_at": None, "finished_at": None,
        "exit_code": None, "log_tail": None,
    }
    q.job_path(root, old_style["id"], "pending").write_text(json.dumps(old_style),
                                                            encoding="utf-8")
    job = q.claim(root)
    assert job.kind == q.KIND_GENERATE

    seen = {}

    def spawn(cmd, **kw):
        seen["cmd"] = list(cmd)
        return _FakeProcess(0, "ok\n", kw)

    code = worker.run_job(root, job, spawn=spawn)

    assert code == 0
    assert seen["cmd"][:2] == ["caffeinate", "-dimsu"], "must still go through job_command"
    assert q.job_path(root, job.id, "done").exists()


def test_run_job_dispatches_an_assemble_kind_with_an_honest_failure_before_task_4(tmp_path,
                                                                                   monkeypatch):
    """`kind="assemble"` when `h3_48gb.assemble` cannot be imported at all: the worker's lazy
    import must fail the *job*, not the worker process -- the whole point of dispatching through a
    lazy import instead of a module-level one. `h3_48gb.assemble` is a real module in this checkout
    (task 4), so the only honest way left to exercise "the import itself fails" is to make the
    import genuinely raise, not to hide an already-imported module from `sys.modules` -- once a
    module has been imported for real anywhere in the process, Python binds it onto its parent
    package's own namespace (`h3_48gb.assemble`), and `from h3_48gb import assemble` finds it there
    directly, bypassing `sys.modules` entirely; deleting the `sys.modules` entry alone (task 3's
    original test, written when this module did not exist yet) stopped being a reliable way to
    simulate "not importable" the moment task 4 added the real file.
    """
    # `hasattr(h3_48gb, "assemble")` is what `from h3_48gb import assemble` actually consults first
    # (CPython's `_handle_fromlist`) -- true the moment *any* test in this process has imported the
    # real module (this file's own top-level `from h3_48gb import assemble` guarantees it), and
    # true regardless of `sys.modules`. Clearing the package attribute *and* poisoning
    # `sys.modules` with the standard "this import failed, do not retry" sentinel (`None`) is what
    # actually forces a fresh `ImportError` again -- either alone is not enough.
    import h3_48gb as h3_48gb_pkg
    monkeypatch.delattr(h3_48gb_pkg, "assemble", raising=False)
    monkeypatch.setitem(sys.modules, "h3_48gb.assemble", None)

    root = tmp_path / "queue"
    proj = project_module.create_project(tmp_path / "out", "video", "Мой ролик")
    job = q.submit(root, ["assemble", "--project", str(proj.path)], "",
                   {"output_stem": str(proj.path.parent / "final")}, {}, kind=q.KIND_ASSEMBLE)
    claimed = q.claim(root)

    code = worker.run_job(root, claimed, spawn=_caffeinate_spy([]))

    assert code == 1
    assert q.job_path(root, job.id, "failed").exists()
    log = q.log_path(root, job.id).read_text(encoding="utf-8")
    assert "Task 4" in log


def test_run_job_dispatches_an_assemble_kind_to_h3_48gb_assemble_run(tmp_path, monkeypatch):
    """The worker's lazy import finds the real `h3_48gb.assemble` and calls `.run(project_path,
    run=...)` -- proven by monkeypatching `.run` directly on the real, already-imported module
    (mirroring how `test_i1_song_job_...` monkeypatches `songrun.run_song` on the real `songrun`
    module), which survives the package-attribute caching a `sys.modules` substitution does not
    (see the previous test's own docstring).
    """
    calls = []

    def fake_run(project_path, *, run):
        calls.append((Path(project_path), run))
        return Path(project_path).parent / "final.mp4"

    monkeypatch.setattr(assemble, "run", fake_run)

    root = tmp_path / "queue"
    proj = project_module.create_project(tmp_path / "out", "video", "Мой ролик")
    job = q.submit(root, ["assemble", "--project", str(proj.path)], "",
                   {"output_stem": str(proj.path.parent / "final")}, {}, kind=q.KIND_ASSEMBLE)
    claimed = q.claim(root)
    caffeinate_calls = []

    code = worker.run_job(root, claimed, spawn=_caffeinate_spy(caffeinate_calls))

    assert code == 0
    assert q.job_path(root, job.id, "done").exists()
    assert len(calls) == 1
    called_path, called_run = calls[0]
    assert called_path == proj.path
    assert callable(called_run), "assemble.run must receive an injectable run callable"
    assert caffeinate_calls == [["caffeinate", "-dimsu", "-w", str(os.getpid())]]


# -- I1a (fix round 1, 2026-08-18 review): a failed assemble job marks stages.assembly failed -----


def test_i1a_a_failed_assemble_job_marks_stages_assembly_failed(tmp_path, monkeypatch):
    """Before this, a crashing `assemble.run` left `stages.assembly` stuck wherever `_submit_
    assembly` last set it (`"running"`) forever -- nothing else in this codebase ever revisits an
    assembly stage that is not `"draft"`, so the project looked permanently in-flight with no job
    actually working on it.
    """
    def boom(project_path, *, run):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(assemble, "run", boom)

    root = tmp_path / "queue"
    proj = project_module.create_project(tmp_path / "out", "video", "Мой ролик")
    proj.set_stage_status("assembly", "running")
    job = q.submit(root, ["assemble", "--project", str(proj.path)], "",
                   {"output_stem": str(proj.path.parent / "final")}, {}, kind=q.KIND_ASSEMBLE)
    claimed = q.claim(root)

    code = worker.run_job(root, claimed, spawn=_caffeinate_spy([]))

    assert code == 1
    assert q.job_path(root, job.id, "failed").exists()
    reloaded = project_module.load_project(proj.path)
    assert reloaded.stages["assembly"] == "failed"


def test_i1a_an_import_broken_assemble_job_also_marks_stages_assembly_failed(tmp_path, monkeypatch):
    """The other failure path `_run_assemble_job` has -- the lazy import itself failing -- must
    mark the stage the same way a crash inside `assemble.run` does.
    """
    import h3_48gb as h3_48gb_pkg
    monkeypatch.delattr(h3_48gb_pkg, "assemble", raising=False)
    monkeypatch.setitem(sys.modules, "h3_48gb.assemble", None)

    root = tmp_path / "queue"
    proj = project_module.create_project(tmp_path / "out", "video", "Мой ролик")
    proj.set_stage_status("assembly", "running")
    job = q.submit(root, ["assemble", "--project", str(proj.path)], "",
                   {"output_stem": str(proj.path.parent / "final")}, {}, kind=q.KIND_ASSEMBLE)
    claimed = q.claim(root)

    code = worker.run_job(root, claimed, spawn=_caffeinate_spy([]))

    assert code == 1
    reloaded = project_module.load_project(proj.path)
    assert reloaded.stages["assembly"] == "failed"


# -- I1b (fix round 1, 2026-08-18 review): advance_project also runs after song/assemble jobs -----


def test_i1b_a_finished_song_job_also_calls_advance_project(tmp_path, monkeypatch):
    calls = []

    def fake_advance(project, queue_root, outdir, **kwargs):
        calls.append(project.id)
        return {"action": "nothing_to_do"}

    monkeypatch.setattr(assemble, "advance_project", fake_advance)
    root = tmp_path / "queue"
    proj = _song_project(tmp_path)
    job = _song_job(root, proj, tmp_path)
    fake_result = _fake_song_result(proj.path.parent / "track")
    monkeypatch.setattr(sr, "run_song", lambda *a, **kw: fake_result)

    code = worker.run_job(root, job, spawn=_caffeinate_spy([]), outdir=tmp_path / "out")

    assert code == 0
    assert calls == [proj.id]


def test_i1b_a_finished_song_job_with_no_scenes_yet_advances_as_a_safe_no_op(tmp_path, monkeypatch):
    """The no-op case, exercised for real (no `advance_project` mock): a `kind="song"` project has
    no `scenes` at all yet (task 6 populates them, later, from an approved script) --
    `advance_project` must return `"nothing_to_do"` quietly, not raise or submit anything.
    """
    root = tmp_path / "queue"
    proj = _song_project(tmp_path)
    job = _song_job(root, proj, tmp_path)
    fake_result = _fake_song_result(proj.path.parent / "track")
    monkeypatch.setattr(sr, "run_song", lambda *a, **kw: fake_result)

    code = worker.run_job(root, job, spawn=_caffeinate_spy([]), outdir=tmp_path / "out")

    assert code == 0
    pending_jobs = [j for j in q.scan(root)[0] if j.state == "pending"]
    assert pending_jobs == [], "a song-only project has no scenes to advance -- nothing submitted"


def test_i1b_a_finished_assemble_job_also_calls_advance_project(tmp_path, monkeypatch):
    advance_calls = []

    def fake_advance(project, queue_root, outdir, **kwargs):
        advance_calls.append(project.id)
        return {"action": "nothing_to_do"}

    def fake_run(project_path, *, run):
        return Path(project_path).parent / "final.mp4"

    monkeypatch.setattr(assemble, "run", fake_run)
    monkeypatch.setattr(assemble, "advance_project", fake_advance)

    root = tmp_path / "queue"
    proj = project_module.create_project(tmp_path / "out", "video", "Мой ролик")
    job = q.submit(root, ["assemble", "--project", str(proj.path)], "",
                   {"output_stem": str(proj.path.parent / "final")}, {}, kind=q.KIND_ASSEMBLE)
    claimed = q.claim(root)

    code = worker.run_job(root, claimed, spawn=_caffeinate_spy([]), outdir=tmp_path / "out")

    assert code == 0
    assert advance_calls == [proj.id]


def test_song_job_wallclock_estimate_seconds_is_estimate_duration_times_13():
    """Task 3's brief, verbatim: "Оценка song-задачи = estimate_duration × 13" -- the queue-facing
    wall-clock estimate (Music3's own realtime factor), not the song's own duration estimate.
    """
    lyrics = "[verse]\nа б в\n[chorus]\nг д е\n"
    assert worker.song_job_wallclock_estimate_seconds(lyrics) == pytest.approx(
        sr.estimate_duration(lyrics) * 13.0)


# -- C1 (fix round 1, 2026-08-18 review): a song job's own subprocess is a tracked child ---------


class _FakeTrackedChild:
    """Stands in for the `Popen` `worker._tracked_child_run`'s own `spawn` starts: no `.wait()`,
    just `.communicate()` (the way that seam actually uses it) and a `.returncode`.
    """

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.pid = 4242

    def communicate(self):
        return self._stdout, self._stderr


def test_c1_song_job_registers_current_child_while_its_own_subprocess_runs_and_clears_it_after(
        tmp_path, monkeypatch):
    """C1, the round's main fix: before it, only `KIND_GENERATE` ever set `_current_child`, so a
    second `SIGTERM` arriving while a song job was mid-flight found `_current_child is None` and
    took the *worker* down (`_stop_signals`' handler falls through to `SIG_DFL` + re-signalling
    itself) instead of the song job's own Music3/ffmpeg/mlx_whisper subprocess -- orphaning it on
    the GPU with no worker, no lease and nothing left to reap it. This proves the fix at the exact
    seam `songrun.run_song`/`align_track` already call (`run(cmd, capture_output=True, text=True)`,
    task 2's own contract) -- a mocked `run_song` stands in and calls that `run` argument itself,
    exactly as the real one does for each of its four subprocess steps.
    """
    assert worker._current_child is None, "sanity: no state left over from an earlier test"
    root = tmp_path / "queue"
    proj = _song_project(tmp_path)
    job = _song_job(root, proj, tmp_path)
    track_dir = proj.path.parent / "track"
    fake_result = _fake_song_result(track_dir)
    seen = {}

    def fake_spawn(cmd, **kw):
        if cmd[0] == "caffeinate":
            return _FakeCaffeinate()
        seen["child_spawn_kwargs"] = kw
        return _FakeTrackedChild(returncode=0, stdout="ok", stderr="")

    def fake_run_song(track_dir_arg, lyrics, caption, duration, *, seed, run, **kw):
        result = run(["fake-music3-generate.py"], capture_output=True, text=True)
        seen["result_returncode"] = result.returncode
        seen["result_stdout"] = result.stdout
        return fake_result

    # Observe `_current_child` from *inside* `communicate()`, the only moment it is guaranteed to
    # be set -- checking it from `fake_run_song` after `run(...)` returns would only prove it was
    # cleared, not that it was ever set while the call was actually in flight.
    real_communicate = _FakeTrackedChild.communicate

    def spying_communicate(self):
        seen["current_child_during_communicate"] = worker._current_child
        return real_communicate(self)

    monkeypatch.setattr(_FakeTrackedChild, "communicate", spying_communicate)
    monkeypatch.setattr(sr, "run_song", fake_run_song)

    code = worker.run_job(root, job, spawn=fake_spawn)

    assert code == 0
    assert seen["current_child_during_communicate"] is not None, (
        "the song job's own subprocess must be visible to _terminate_child_group (the second "
        "SIGTERM's kill target) while it is actually running")
    assert seen["child_spawn_kwargs"]["start_new_session"] is True, (
        "the song job's own subprocess must run in its own process group -- otherwise "
        "_terminate_child_group's os.killpg has nothing of its own to reach")
    assert seen["result_returncode"] == 0
    assert seen["result_stdout"] == "ok", "run() must hand songrun a real CompletedProcess back"
    assert worker._current_child is None, (
        "must be cleared once the song job's subprocess call returns, exactly like KIND_GENERATE's")


def test_c1_tracked_child_run_clears_current_child_even_if_the_subprocess_call_raises(tmp_path):
    """`_current_child` must not leak past a failing subprocess call either -- a `finally`, not a
    bare assignment after `communicate()` returns, is what the implementation must use.
    """
    class _BoomChild:
        pid = 1

        def communicate(self):
            raise OSError("boom")

    def spawn(cmd, **kw):
        return _BoomChild()

    run = worker._tracked_child_run(spawn=spawn)
    with pytest.raises(OSError, match="boom"):
        run(["anything"], capture_output=True, text=True)
    assert worker._current_child is None


# -- I1/I4 (fix round 1, 2026-08-18 review): seed and duration round-trip through the project -----


def test_i1_song_job_respects_an_explicit_seed_from_project_json(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    proj = _song_project(tmp_path)
    proj.track = {**proj.track, "seed": 999999}
    proj.save()
    job = _song_job(root, proj, tmp_path)
    track_dir = proj.path.parent / "track"
    fake_result = _fake_song_result(track_dir)
    seen = {}

    def fake_run_song(track_dir_arg, lyrics, caption, duration, *, seed, **kw):
        seen["seed"] = seed
        return fake_result

    monkeypatch.setattr(sr, "run_song", fake_run_song)

    code = worker.run_job(root, job, spawn=_caffeinate_spy([]))

    assert code == 0
    assert seen["seed"] == 999999, "an explicit project.json seed must be passed through unchanged"
    reloaded = project_module.load_project(proj.path)
    assert reloaded.track["seed"] == 999999, "an already-set seed must still be recorded (no-op)"


def test_i1_song_job_rolls_a_random_seed_when_none_is_set_and_records_it(tmp_path, monkeypatch):
    """`_empty_track()` defaults `seed` to `None` -- the common case, a fresh track. The worker must
    roll one itself and, critically, write the *actual* value it rolled back onto the project, so a
    duplicate of this job (task 6) can reproduce the exact take instead of rolling a different
    random seed on every retry.
    """
    root = tmp_path / "queue"
    proj = _song_project(tmp_path)
    assert proj.track["seed"] is None, "sanity: the fixture must start with the real default"
    job = _song_job(root, proj, tmp_path)
    track_dir = proj.path.parent / "track"
    fake_result = _fake_song_result(track_dir)
    seen = {}

    def fake_run_song(track_dir_arg, lyrics, caption, duration, *, seed, **kw):
        seen["seed"] = seed
        return fake_result

    monkeypatch.setattr(sr, "run_song", fake_run_song)
    monkeypatch.setattr(worker.random, "randrange", lambda n: 424242)

    code = worker.run_job(root, job, spawn=_caffeinate_spy([]))

    assert code == 0
    assert seen["seed"] == 424242, "a rolled seed must actually be used for generation"
    reloaded = project_module.load_project(proj.path)
    assert reloaded.track["seed"] == 424242, (
        "the seed actually used must be written back, not left None -- otherwise a duplicate "
        "cannot reproduce this take")


def test_i1_song_job_never_writes_a_seed_for_an_imported_track(tmp_path, monkeypatch):
    """`track.source == "import"` never generates anything, so there is no seed to roll or record
    -- `update_track` must not be called with `seed=...` at all for this path (as opposed to, say,
    silently writing `seed=None` over a value a human may have typed in by hand for their own
    reference).
    """
    root = tmp_path / "queue"
    imported_mp3 = tmp_path / "uploaded" / "suno-song.mp3"
    imported_mp3.parent.mkdir(parents=True)
    imported_mp3.write_bytes(b"fake mp3 bytes")
    proj = _song_project(tmp_path, source="import", mp3=imported_mp3)
    job = _song_job(root, proj, tmp_path)
    track_dir = proj.path.parent / "track"
    fake_result = sr.SongResult(
        wav=imported_mp3, mastered_wav=imported_mp3, mp3=imported_mp3, mastered_mp3=imported_mp3,
        duration=42.0, transcript="ла ла ла", sections=_FAKE_SECTIONS, undersung=False,
    )
    monkeypatch.setattr(sr, "align_track", lambda *a, **kw: fake_result)

    code = worker.run_job(root, job, spawn=_caffeinate_spy([]))

    assert code == 0
    reloaded = project_module.load_project(proj.path)
    assert reloaded.track["seed"] is None, "an import must never have a seed written for it"


def test_i4_song_job_records_the_tracks_duration_from_run_song(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    proj = _song_project(tmp_path)
    job = _song_job(root, proj, tmp_path)
    track_dir = proj.path.parent / "track"
    fake_result = _fake_song_result(track_dir)
    assert fake_result.duration == pytest.approx(30.0)
    monkeypatch.setattr(sr, "run_song", lambda *a, **kw: fake_result)

    code = worker.run_job(root, job, spawn=_caffeinate_spy([]))

    assert code == 0
    reloaded = project_module.load_project(proj.path)
    assert reloaded.track["duration"] == pytest.approx(30.0)


def test_i4_song_job_records_the_tracks_duration_from_align_track(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    imported_mp3 = tmp_path / "uploaded" / "suno-song.mp3"
    imported_mp3.parent.mkdir(parents=True)
    imported_mp3.write_bytes(b"fake mp3 bytes")
    proj = _song_project(tmp_path, source="import", mp3=imported_mp3)
    job = _song_job(root, proj, tmp_path)
    fake_result = sr.SongResult(
        wav=imported_mp3, mastered_wav=imported_mp3, mp3=imported_mp3, mastered_mp3=imported_mp3,
        duration=123.4, transcript="ла ла ла", sections=_FAKE_SECTIONS, undersung=False,
    )
    monkeypatch.setattr(sr, "align_track", lambda *a, **kw: fake_result)

    code = worker.run_job(root, job, spawn=_caffeinate_spy([]))

    assert code == 0
    reloaded = project_module.load_project(proj.path)
    assert reloaded.track["duration"] == pytest.approx(123.4)


# -- Task 4: assemble's own subprocess is a tracked child too (the C1 gap the task 3 report left
# -- open for KIND_ASSEMBLE, closed here the same way it was closed for KIND_SONG) ---------------


def test_task4_assemble_jobs_own_subprocess_is_a_tracked_child_while_it_runs(tmp_path, monkeypatch):
    """The task 3 report's own known gap: `KIND_ASSEMBLE` never set `_current_child`, so a second
    `SIGTERM` mid-assembly could not reach ffmpeg/ffprobe. Task 4 closes it by passing
    `run=_tracked_child_run(spawn)` into `assemble.run`, exactly mirroring
    `test_c1_song_job_registers_current_child_...` above -- a fake `assemble.run` calls the `run`
    it was handed, and this test observes `_current_child` from *inside* the fake child's own
    `communicate()`, the only moment it is guaranteed to be set.
    """
    assert worker._current_child is None, "sanity: no state left over from an earlier test"
    root = tmp_path / "queue"
    proj = project_module.create_project(tmp_path / "out", "video", "Мой ролик")
    job = q.submit(root, ["assemble", "--project", str(proj.path)], "",
                   {"output_stem": str(proj.path.parent / "assembly" / "job-final")}, {},
                   kind=q.KIND_ASSEMBLE)
    claimed = q.claim(root)
    seen = {}

    def fake_spawn(cmd, **kw):
        if cmd[0] == "caffeinate":
            return _FakeCaffeinate()
        seen["child_spawn_kwargs"] = kw
        return _FakeTrackedChild(returncode=0, stdout="10.0\n", stderr="")

    real_communicate = _FakeTrackedChild.communicate

    def spying_communicate(self):
        seen["current_child_during_communicate"] = worker._current_child
        return real_communicate(self)

    monkeypatch.setattr(_FakeTrackedChild, "communicate", spying_communicate)

    def fake_assemble_run(project_path, *, run):
        result = run(["ffprobe", "fake"], capture_output=True, text=True)
        seen["result_returncode"] = result.returncode
        seen["result_stdout"] = result.stdout
        return Path(project_path).parent / "assembly" / "final.mp4"

    monkeypatch.setattr(assemble, "run", fake_assemble_run)

    code = worker.run_job(root, claimed, spawn=fake_spawn)

    assert code == 0
    assert seen["current_child_during_communicate"] is not None, (
        "an assemble job's own subprocess must be visible to _terminate_child_group while it is "
        "actually running, exactly like a song job's")
    assert seen["child_spawn_kwargs"]["start_new_session"] is True
    assert seen["result_returncode"] == 0
    assert seen["result_stdout"] == "10.0\n"
    assert worker._current_child is None


# -- Task 4: the post-job hook for a project scene's own kind="generate" job ---------------------


class _FakeGenerateProcess:
    """Stands in for the `Popen` `job_command`'s own `KIND_GENERATE` dispatch starts: no
    `.communicate()`, just `.wait()` (the way `run_job`'s `KIND_GENERATE` branch actually uses it).
    """

    def __init__(self, returncode=0):
        self.returncode = returncode
        self.pid = os.getpid()

    def wait(self, timeout=None):
        return self.returncode


def _scene_hook_spawn(*, generate_returncode=0, ffprobe_duration=5.0):
    """A `spawn` covering every shape this hook's own machinery needs: `job_command`'s top-level
    generate subprocess (`.wait()`-based), and `_tracked_child_run`'s ffprobe/ffmpeg calls inside
    `advance_project`'s own keyframe extraction (`.communicate()`-based) -- dispatched by `cmd[0]`.
    """

    def spawn(cmd, **kw):
        if cmd[0] in ("ffprobe", "ffmpeg"):
            return _FakeTrackedChild(returncode=0, stdout=f"{ffprobe_duration}\n", stderr="")
        return _FakeGenerateProcess(returncode=generate_returncode)

    return spawn


def _video_project_with_scenes(tmp_path, *, n=2, title="Мой ролик"):
    proj = project_module.create_project(tmp_path / "out", "video", title)
    proj.scenes = [
        {"idx": i, "prompt": f"scene {i}", "duration": 5.0, "status": "pending",
         "job_id": None, "clip_path": None, "keyframe_path": None}
        for i in range(n)
    ]
    proj.save()
    return proj


def _scene_zero_job(root, proj):
    """Queue and claim scene 0's own `kind="generate"` job, tagged exactly as `advance_project`
    would have tagged it (`h3_48gb.assemble.scene_note`) -- standing in for task 6, which is not
    built yet, submitting the real first scene. Also claims scene 0 on the project itself
    (`Project.claim_next_scene`), exactly as `advance_project`'s own `_submit_next_scene` would --
    C2 (fix round 1, 2026-08-18 review) requires `scenes[0]["job_id"]` to already match the job
    that is about to finish, so this stand-in must leave the project in the same shape a real
    `advance_project(project, ...)`-driven scene 0 submission would, not the bare `q.submit` this
    helper used to do on its own before that check existed.
    """
    scenes_dir = proj.path.parent / "scenes"
    args = ["generate", "scene 0", "--width", "896", "--height", "512", "--duration", "5.0",
            "--tag", "scene-0", "--outdir", str(scenes_dir)]
    submitted = q.submit(root, args, assemble.scene_note(proj, 0),
                          {"output_stem": str(scenes_dir / "h3-scene-0-896x512")}, {},
                          kind=q.KIND_GENERATE)
    proj.claim_next_scene(submitted.id)
    return q.claim(root)


def test_run_job_marks_a_project_scene_done_and_advances_to_the_next_scene(tmp_path):
    root = tmp_path / "queue"
    proj = _video_project_with_scenes(tmp_path, n=2)
    job = _scene_zero_job(root, proj)
    Path(f"{job.output_stem}.mp4").parent.mkdir(parents=True, exist_ok=True)
    Path(f"{job.output_stem}.mp4").write_bytes(b"fake mp4 bytes")

    code = worker.run_job(root, job, spawn=_scene_hook_spawn(), outdir=tmp_path / "out")

    assert code == 0
    reloaded = project_module.load_project(proj.path)
    assert reloaded.scenes[0]["status"] == "done"
    assert reloaded.scenes[0]["clip_path"] == f"{job.output_stem}.mp4"
    assert reloaded.scenes[1]["status"] == "running", (
        "advance_project must have claimed the next scene once the first was recorded done")
    assert reloaded.scenes[1]["job_id"] is not None
    assert reloaded.scenes[1]["keyframe_path"] is not None, (
        "scene 1 follows a done scene with a clip -- it must get an automatic keyframe")
    # A real pending job for scene 1 must now exist in the queue.
    pending_notes = [j.note for j in q.scan(root)[0] if j.state == "pending"]
    assert assemble.scene_note(proj, 1) in pending_notes


def test_run_job_marks_a_project_scene_failed_and_stops_the_chain(tmp_path):
    root = tmp_path / "queue"
    proj = _video_project_with_scenes(tmp_path, n=2)
    job = _scene_zero_job(root, proj)
    # No .mp4 written -- the fake subprocess "failed" the run.

    code = worker.run_job(root, job, spawn=_scene_hook_spawn(generate_returncode=1),
                           outdir=tmp_path / "out")

    assert code == 1
    reloaded = project_module.load_project(proj.path)
    assert reloaded.scenes[0]["status"] == "failed"
    assert reloaded.scenes[1]["status"] == "pending", "the chain must not advance past a failure"
    assert reloaded.stages["scenes"] == "failed"
    pending_jobs = [j for j in q.scan(root)[0] if j.state == "pending"]
    assert pending_jobs == [], "nothing further may be submitted once a scene has failed"


def test_run_job_a_done_generate_job_with_no_mp4_on_disk_is_treated_as_a_failed_scene(tmp_path):
    """`exit_code == 0` alone is not enough -- the hook also checks the clip actually exists at the
    job's own `output_stem`, so a shape this codebase does not expect (a "successful" run that
    somehow produced no file) still marks the scene failed rather than recording a `clip_path` that
    points at nothing.
    """
    root = tmp_path / "queue"
    proj = _video_project_with_scenes(tmp_path, n=1)
    job = _scene_zero_job(root, proj)
    # generate_returncode=0 (the default) but no .mp4 file is ever written.

    code = worker.run_job(root, job, spawn=_scene_hook_spawn(), outdir=tmp_path / "out")

    assert code == 0, "the job's own exit code is untouched by this bookkeeping"
    reloaded = project_module.load_project(proj.path)
    assert reloaded.scenes[0]["status"] == "failed"


def test_run_job_ignores_an_ordinary_generate_job_with_no_project_note(tmp_path):
    root = tmp_path / "queue"
    seen = {}

    def spawn(cmd, **kw):
        seen["cmd"] = list(cmd)
        return _FakeGenerateProcess(0)

    job = q.submit(root, ["generate", "a prompt", "--tag", "plain"], "just a note",
                   {"output_stem": str(tmp_path / "h3-plain-1x1")}, {}, kind=q.KIND_GENERATE)
    claimed = q.claim(root)

    code = worker.run_job(root, claimed, spawn=spawn, outdir=tmp_path / "out")

    assert code == 0
    assert q.job_path(root, job.id, "done").exists()


def test_project_scene_bookkeeping_never_crashes_the_worker_on_a_missing_project(tmp_path,
                                                                                  capsys):
    """The note names a project id that does not exist under `<outdir>/projects/` (a deleted
    project, a stale note) -- `_handle_project_scene_result` must swallow the failure and let
    `run_job` return the job's own honest exit code, not propagate a `ProjectNotFound`.
    """
    root = tmp_path / "queue"
    scenes_dir = tmp_path / "out" / "projects" / "does-not-exist" / "scenes"
    args = ["generate", "scene 0", "--width", "896", "--height", "512", "--duration", "5.0",
            "--tag", "scene-0", "--outdir", str(scenes_dir)]
    job = q.submit(root, args, "project scene does-not-exist #0",
                   {"output_stem": str(scenes_dir / "h3-scene-0-896x512")}, {}, kind=q.KIND_GENERATE)
    claimed = q.claim(root)
    Path(f"{claimed.output_stem}.mp4").parent.mkdir(parents=True, exist_ok=True)
    Path(f"{claimed.output_stem}.mp4").write_bytes(b"fake mp4 bytes")

    code = worker.run_job(root, claimed, spawn=_scene_hook_spawn(), outdir=tmp_path / "out")

    assert code == 0
    assert q.job_path(root, job.id, "done").exists()
    assert "project scene bookkeeping failed" in capsys.readouterr().err


# -- C2 (fix round 1, 2026-08-18 review): a stale scene job must not overwrite a fresher attempt --


def test_c2_a_stale_scene_job_does_not_overwrite_a_fresher_attempts_status(tmp_path, capsys):
    """A job whose own note names scene 1, but whose id no longer matches `scenes[1]["job_id"]` on
    the project (a delayed retry of an old attempt racing a re-submission the human already
    triggered, e.g. via "пересчитать сцену") must not be allowed to record its own outcome over the
    fresher attempt's -- before this fix, an old job's `done`/`failed` blindly overwrote whatever
    the current attempt already had, including clobbering a `done` scene with a stale clip.
    """
    root = tmp_path / "queue"
    proj = _video_project_with_scenes(tmp_path, n=2)
    proj.set_scene_status(0, "done", clip_path=str(tmp_path / "scene0.mp4"), job_id="job-scene-0")
    # Scene 1 is currently owned by a *fresh* job -- not the one about to finish below.
    proj.set_scene_status(1, "running", job_id="fresh-job")

    scenes_dir = proj.path.parent / "scenes"
    args = ["generate", "scene 1", "--width", "896", "--height", "512", "--duration", "5.0",
            "--tag", "scene-1-stale", "--outdir", str(scenes_dir)]
    q.submit(root, args, assemble.scene_note(proj, 1),
             {"output_stem": str(scenes_dir / "h3-scene-1-stale-896x512")}, {},
             kind=q.KIND_GENERATE)
    stale_job = q.claim(root)
    Path(f"{stale_job.output_stem}.mp4").parent.mkdir(parents=True, exist_ok=True)
    Path(f"{stale_job.output_stem}.mp4").write_bytes(b"stale clip bytes")
    assert stale_job.id != "fresh-job"

    code = worker.run_job(root, stale_job, spawn=_scene_hook_spawn(), outdir=tmp_path / "out")

    assert code == 0, "the job's own exit code is untouched by this bookkeeping"
    reloaded = project_module.load_project(proj.path)
    assert reloaded.scenes[1]["status"] == "running", (
        "the stale job must not overwrite the fresher attempt's status")
    assert reloaded.scenes[1]["job_id"] == "fresh-job"
    assert reloaded.scenes[1]["clip_path"] is None
    assert "stale project scene job" in capsys.readouterr().err


# -- I3 (fix round 1, 2026-08-18 review): a broken assemble import must not crash a scene job -----


def test_i3_a_broken_assemble_import_does_not_crash_run_job_for_a_generate_scene_job(tmp_path,
                                                                                      monkeypatch):
    """Before this fix, `_handle_project_scene_result`'s own `from h3_48gb import assemble`/
    `project` imports sat ahead of its `try` block -- an `ImportError` here propagated straight out
    of `run_job`, contradicting this module's own docstring ("must not take the whole worker
    process down") for every `KIND_GENERATE` job whose note happens to parse as a project scene's,
    not just the ones this function otherwise protects.
    """
    import h3_48gb as h3_48gb_pkg
    monkeypatch.delattr(h3_48gb_pkg, "assemble", raising=False)
    monkeypatch.setitem(sys.modules, "h3_48gb.assemble", None)

    root = tmp_path / "queue"
    scenes_dir = tmp_path / "out" / "projects" / "some-project" / "scenes"
    args = ["generate", "scene 0", "--width", "896", "--height", "512", "--duration", "5.0",
            "--tag", "scene-0", "--outdir", str(scenes_dir)]
    job = q.submit(root, args, "project scene some-project #0",
                   {"output_stem": str(scenes_dir / "h3-scene-0-896x512")}, {},
                   kind=q.KIND_GENERATE)
    claimed = q.claim(root)
    Path(f"{claimed.output_stem}.mp4").parent.mkdir(parents=True, exist_ok=True)
    Path(f"{claimed.output_stem}.mp4").write_bytes(b"fake mp4 bytes")

    code = worker.run_job(root, claimed, spawn=_scene_hook_spawn(), outdir=tmp_path / "out")

    assert code == 0, "the job's own exit code must not be affected by the broken import"
    assert q.job_path(root, job.id, "done").exists()


# -- M1 (fix round 1, 2026-08-18 review): the bare caffeinate is bound to the worker's own pid ----


def test_m1_caffeinate_block_binds_to_the_workers_own_pid(tmp_path):
    """A `finally: proc.terminate()` never runs if the worker itself is taken down by `kill -9`
    (or the second-SIGTERM path, which sets `SIG_DFL` and re-signals itself -- indistinguishable
    from `kill -9` as far as Python's own cleanup is concerned): the bare `caffeinate` `
    _caffeinate_block` starts would then run forever, with no target and nothing left to stop it.
    `-w <this process's own pid>` makes `caffeinate` itself watch the worker and exit the moment it
    is gone, whichever way it goes.
    """
    calls = []
    with worker._caffeinate_block(spawn=_caffeinate_spy(calls)):
        pass
    assert calls == [["caffeinate", "-dimsu", "-w", str(os.getpid())]]


# -- I5 (fix round 1, 2026-08-18 review): a worker killed mid-song-job recovers cleanly -----------


_CRASH_SONG_WORKER = """
import os, sys
sys.path.insert(0, {project!r})
from h3_48gb import queue as q
from h3_48gb import songrun as sr
from h3_48gb import worker

root = sys.argv[1]
worker._now = lambda: "2020-01-01T00:00:00"


def dying_run_song(*a, **kw):
    os._exit(1)


sr.run_song = dying_run_song

job = q.claim(root)


class _FakeCaffeinate:
    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


def spawn(cmd, **kw):
    return _FakeCaffeinate()


worker.run_job(root, job, spawn=spawn)
os._exit(0)
"""


def test_i5_a_crash_during_a_song_job_returns_it_to_pending_and_leaves_the_project_untouched(
        tmp_path, monkeypatch):
    """By the same shape as `test_a_crash_before_any_result_returns_the_job_to_pending`
    (`point="during_child"`), but for `KIND_SONG`: a worker killed while `songrun.run_song` is
    running leaves no result marker and no gated project -- `reconcile` must return the job to
    `pending/` and `project.json` must show no intermediate status at all, only the `"draft"` it
    started with. A subsequent, successful run must then finish normally (`run_song`'s own I4
    cleans `song.*`/`whisper.json` before generating, so a retry against the *same* `track_dir`
    is already safe -- this only has to prove the queue side of that story).
    """
    root = tmp_path / "queue"
    proj = _song_project(tmp_path)
    job = q.submit(root, ["song", "--project", str(proj.path)], "",
                   {"output_stem": str(proj.path.parent / "track" / "song")}, {}, kind=q.KIND_SONG)

    script = _CRASH_SONG_WORKER.format(project=str(PROJECT_ROOT))
    result = subprocess.run([sys.executable, "-c", script, str(root)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 1, f"the crash script did not die as expected: {result.stderr}"

    assert q.job_path(root, job.id, "running").exists(), (
        "nothing survived the crash -- the job must still look like it is running")
    assert q.lease_is_free(root, job.id) is True, "the lease must not outlive its dead holder"

    reloaded = project_module.load_project(proj.path)
    assert reloaded.track["status"] == "draft", (
        "a mid-job crash must leave no intermediate status on the project -- update_track is only "
        "ever called after the song job has actually finished")
    assert reloaded.stages["track"] == "draft"

    recon = q.reconcile(root)
    assert [j.state for j in recon.changed] == ["pending"]
    assert q.job_path(root, job.id, "pending").exists()

    # And the retry -- a fresh worker claiming the same job -- finishes normally.
    track_dir = proj.path.parent / "track"
    fake_result = _fake_song_result(track_dir)
    monkeypatch.setattr(sr, "run_song", lambda *a, **kw: fake_result)
    retried = q.claim(root)
    code = worker.run_job(root, retried, spawn=_caffeinate_spy([]))

    assert code == 0
    assert q.job_path(root, job.id, "done").exists()
    reloaded = project_module.load_project(proj.path)
    assert reloaded.track["status"] == "awaiting_approval"


# -- Global constraint: the worker must stay importable without MLX ------------------------------


def test_worker_module_does_not_import_mlx():
    """The worker idles for days between runs; a resident 33B-parameter stack in a process that is
    only waiting on `flock` costs gigabytes for nothing. MLX belongs in the subprocess. Run in a
    subprocess so this session's own earlier imports cannot hide a leak.
    """
    code = "import sys; import h3_48gb.worker; print('mlx' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            cwd=str(PROJECT_ROOT))
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    assert result.stdout.strip() == "False", "importing h3_48gb.worker must not import mlx"
