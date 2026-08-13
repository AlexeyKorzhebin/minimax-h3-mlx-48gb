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

from h3_48gb import queue as q
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
