"""The process that runs queued jobs, one at a time, and survives being killed in the middle.

Three things live here and nothing else does: the **worker lock** that makes "one worker per
machine" a fact rather than a convention, the **lease** that makes "this job is running right now"
observable from another process, and the **loop** that ties them to `h3_48gb.queue`.

**No `mlx` import, ever.** MLX is loaded by the subprocess this module starts, never by this
module: the worker is idle for days at a time between runs, and a resident 33B-parameter stack in
a process that is only waiting on `flock` and `poll` costs gigabytes for nothing. It also means
the worker can be restarted while a run is in flight without the restart itself needing memory.
See `test_worker_module_does_not_import_mlx`.

**`caffeinate -dimsu` wraps the job, not the worker.** Sleep must be impossible exactly while a
run is in flight and perfectly possible the rest of the time -- a worker that idles for a day
should not keep a laptop awake for a day. This is not a style preference: on 2026-08-10 the
machine sleeping mid-run took the GPU firmware down with a kernel panic and cost a night's work.
Putting `caffeinate` in `job_command` rather than around the worker gives exactly that shape,
because the wrapper's lifetime is the subprocess's lifetime.

**The lease is held across the child's whole life, and the result marker is written before it is
released.** Both halves matter and both are easy to get subtly wrong. A lease released as soon as
the child starts is the exact bug the lease exists to prevent -- a reconciling worker would see
"free" and re-queue a run that is generating right now. A marker written after the lease is
released leaves a window in which the job looks free, has no marker and (until the very last
moment of a run) has no `.mp4` either, which reads as "interrupted" and silently discards a
finished run's exit code.

Messages a human reads are Russian; comments and docstrings are English, matching the rest of the
package (see `queue.py`'s module docstring for the same split).
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from h3_48gb import provider
from h3_48gb import queue as q

#: The file whose `flock` means "a worker is running on this machine". Probed by the web server
#: (`worker.state` in the design spec) and taken exclusively, non-blocking, by `hold_worker_lock`.
WORKER_LOCK_NAME = "worker.lock"

#: The one subprocess this worker is currently waiting on, or `None`. Written by `run_job` and read
#: only by the signal handler, which needs to reach the child from a context that is given no
#: arguments at all. A worker runs exactly one job at a time -- that is its entire purpose -- so
#: one slot is not a simplification, it is the invariant.
_current_child: subprocess.Popen | None = None


class WorkerAlreadyRunning(Exception):
    """Raised when another process already holds `queue/worker.lock`.

    Deliberately loud rather than a silent exit: two workers on one 48 GB machine means two 36 GB
    MLX processes, which means swap, and swap once turned a 568-second step into 818. Someone who
    typed `h3 worker` twice needs to be told, not quietly ignored.
    """


def _now() -> str:
    """Indirected the same way `queue._now` is, so a test can pin the clock the marker records."""
    return datetime.now().isoformat(timespec="seconds")


def job_command(job, python: str = sys.executable) -> list[str]:
    """The exact argv for a job: `caffeinate -dimsu <python> -m h3_48gb <job.args...>`.

    `job.args` is passed through untranslated -- it is already the literal argument list `h3`
    accepts, which is the whole reason the queue stores it in that form (see `queue.py`'s `Job`).

    `python` defaults to the interpreter running the worker so the child lands in the same
    virtualenv, with the same MLX build, without depending on `PATH` or on the `h3` console script
    being installed.
    """
    return ["caffeinate", "-dimsu", python, "-m", "h3_48gb", *job.args]


#: How long `hold_worker_lock` waits before its one retry. Long enough to outlast the web server's
#: liveness probe, which holds `worker.lock` for the few microseconds between its `LOCK_EX` and its
#: `LOCK_UN`; short enough that a real second worker still fails while the human who typed
#: `h3 worker` is still looking at the terminal.
WORKER_LOCK_RETRY_SECONDS = 0.05


@contextlib.contextmanager
def hold_worker_lock(root):
    """Hold `queue/worker.lock` exclusively for the duration of the block, or refuse immediately.

    `LOCK_NB`, unlike `queue_lock`'s blocking acquisition: a second worker must fail and say why,
    not silently queue up behind the first and start running jobs hours later when the first one
    exits. `flock` dies with the process that holds it, so a worker killed by `kill -9`, a panic or
    a power cut leaves nothing to clean up -- the next `h3 worker` starts normally.

    **One retry, and it is not a nicety.** `web.worker_state` answers "is a worker running" by
    taking this same lock non-blockingly and dropping it again, so there is a microsecond window in
    which a perfectly legitimate `h3 worker` collides with a *probe* and dies with
    `worker_already_running` -- naming a worker that does not exist. With the page polling every
    twenty seconds and a human starting the worker from the terminal beside it, that window gets
    sampled. Retrying once distinguishes the two: a probe is gone in microseconds, and a real
    worker holds the lock for days, so it is still there 50 ms later.

    Blocking outright instead would be wrong in the other direction -- a second worker would wait
    for days rather than say so.
    """
    path = Path(root) / WORKER_LOCK_NAME
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        for remaining in (1, 0):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if not remaining:
                    raise WorkerAlreadyRunning(f"another worker already holds {path}") from exc
                time.sleep(WORKER_LOCK_RETRY_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _acquire_lease(root, job_id: str) -> int:
    """Take `queue/leases/<id>.lock` exclusively and return the open fd holding it.

    Blocking, unlike the worker lock: by the time this runs the caller already owns the worker
    lock, so nobody else can legitimately hold this lease and there is nothing to fail fast about.
    """
    fd = os.open(q.lease_path(root, job_id), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _release_lease(fd: int) -> None:
    """Drop the lease held on `fd`. A module-level function, not an inline `flock` call, so a test
    can wrap it and observe *when* the release happens relative to the result marker being written
    -- the ordering that decides whether a crash right there is recoverable.
    """
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def run_job(root, job, spawn=subprocess.Popen) -> int:
    """Run one claimed job to completion and file its result. Returns the subprocess's exit code.

    The order is the contract, and every step of it is load-bearing:

    1. take the lease -- before the child exists, so there is no window in which the job is in
       `running/` with nothing holding its lease;
    2. start `job_command(job)` in its **own session** (`start_new_session=True`), stdout and
       stderr appended to `queue/logs/<id>.log`. The new session is what makes the second stop
       signal able to reach the whole tree -- `caffeinate` is only the parent of the Python that
       holds the 36 GB, and signalling the parent alone orphans the child;
    3. wait for it;
    4. write the result marker **durably**;
    5. only then release the lease;
    6. and only then move the job out of `running/`.

    Steps 4 and 5 in the other order leave a job that looks free, has no marker and has no `.mp4`,
    i.e. indistinguishable from one that never got anywhere -- reconciliation would re-queue a run
    that had already finished, and the rerun would overwrite its output.

    `spawn` is injectable because every test of this function must not start a real generation: a
    second MLX process on this machine is 36 GB against a 48 GB budget.
    """
    global _current_child
    root = Path(root)
    lease = _acquire_lease(root, job.id)
    try:
        # Append rather than truncate: a job returned to `pending/` by reconciliation resumes from
        # its checkpoint, and the log of the attempt that was interrupted is the only record of why
        # it was interrupted.
        with open(q.log_path(root, job.id), "ab") as stream:
            proc = spawn(job_command(job), stdout=stream, stderr=subprocess.STDOUT,
                         start_new_session=True)
            _current_child = proc
            try:
                exit_code = proc.wait()
            finally:
                _current_child = None
        finished_at = _now()
        log_tail = q.read_log_tail(root, job.id)
        q.write_result_marker(root, job.id, exit_code, finished_at)
    finally:
        _release_lease(lease)

    q.finish(root, job.id, exit_code, log_tail, finished_at=finished_at)
    return exit_code


def _terminate_child_group(signum: int) -> None:
    """Signal the current child's **process group**, not just the child.

    `job_command` puts `caffeinate` in front of the Python that actually holds the model, and
    `run_job` starts the pair in its own session. Signalling only the direct child would kill
    `caffeinate` and leave a 36 GB Python orphaned with no worker, no lease and nothing left to
    reap it -- the machine would stay full until someone noticed and killed it by hand.

    `signum` is always `SIGTERM`, deliberately, even when the worker itself was stopped with a
    second `SIGINT`: the design spec names `SIGTERM` for this, `caffeinate` has no `SIGINT`
    semantics of its own to respect, and a `KeyboardInterrupt` traceback out of a half-written
    generation is noise on top of a run that is being abandoned anyway. The worker still dies of
    whichever signal it was actually sent -- only the group gets `SIGTERM`.
    """
    proc = _current_child
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signum)
    except OSError:
        # The child (or its whole group) is already gone -- nothing to signal, and racing with its
        # exit is normal, not an error worth crashing the shutdown path over.
        pass


@contextlib.contextmanager
def _stop_signals(stop: threading.Event):
    """Install the two-stage stop on `SIGTERM`/`SIGINT` for the duration of the block.

    First signal: set `stop`, which makes the loop take no further jobs. The job already running
    is left alone -- it is hours of GPU time, and interrupting it because someone asked the *queue*
    to stop is a bad trade.

    Second signal: take the child's whole process group down and then die of the signal ourselves,
    with the default handler restored, so the worker's exit status is the honest `-SIGTERM` a
    caller expects. Nothing is written for the interrupted job on purpose: it stays in `running/`
    with its lease dropped by the kernel, and the next worker's `reconcile` decides what it was --
    finished (marker or `.mp4`) or interrupted (back to `pending/`, resuming from its checkpoint).

    Handlers can only be installed from the main thread. The loop is also driven from a worker
    thread by tests that have no signals to deliver, so that case yields unarmed rather than
    raising -- but it is checked explicitly rather than by catching `ValueError`, which would also
    swallow a real failure to install.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum, frame):
        if stop.is_set():
            signal.signal(signum, signal.SIG_DFL)
            _terminate_child_group(signal.SIGTERM)
            os.kill(os.getpid(), signum)
            return
        stop.set()

    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def _llm_holds_gpu(root) -> bool:
    """A resident local LLM and a 27 GB generation cannot share 48 GB. The chat page owns the
    *decision* to free the GPU -- it asks the human and, on confirmation, calls
    `POST /api/llm/unload` -- the worker only ever observes whether the port still answers.

    Reads `providers.json` from `root`, the exact value `main_loop` was called with: it is the
    caller's job to pass the same root the web server treats as its `outdir` (where
    `provider.load_providers` looks), not a queue subdirectory underneath it -- a `root` one level
    too deep would find no roster and this check would silently never fire.
    """
    roster = provider.load_providers(root)
    cfg = roster["providers"].get(roster["active"] or "", {})
    return cfg.get("type") == "llama-local" and provider.port_alive(cfg.get("port", 0))


def main_loop(root, poll: float = 5.0, stop=None, spawn=subprocess.Popen) -> int:
    """Run queued jobs until asked to stop. Returns how many jobs were run to completion.

    Each pass reconciles first (`queue.reconcile`), because the previous worker may have been
    killed mid-run and the wreckage has to be understood before anything new is started. If
    reconciliation reports a job whose lease is held right now, this worker takes **nothing**: that
    is another live generation, and a second one means two 36 GB processes on a 48 GB machine.
    It sleeps `poll` and reconciles again instead of waiting for ever -- once the other run is
    gone, the queue has to move.

    The same "take nothing, sleep, look again" shape applies when a resident local LLM holds the
    GPU instead of another lease (`_llm_holds_gpu`): a 27 GB generation cannot start next to a
    30 GB model. Unlike a live lease, nothing here ever unloads the LLM -- only a human, confirming
    on the chat page, does that via `POST /api/llm/unload` -- so the check sits right after
    `reconcile` and before `claim`, purely observing the port until the human's confirmation makes
    it go quiet.

    `stop` is anything with `is_set()`/`wait()`; `_stop_signals` sets the one created here when the
    first `SIGTERM`/`SIGINT` arrives. It stops the *selection* of new jobs only -- a job already
    running is never interrupted by it, which is why the check sits at the top of the loop and
    never inside `run_job`.

    `spawn` is declared here, not added later, because every recovery test in `tests/test_worker.py`
    has to substitute it: the real one launches a 36 GB generation.
    """
    root = Path(root)
    # The worker owns this directory, unlike `scan`, which deliberately refuses to create it: the
    # lock file it is about to take lives inside it, and `h3 worker` on a machine whose queue has
    # never been used must work.
    #
    # NOTE for any future caller: this creates the layout unconditionally, so the guarantee that a
    # typo'd path cannot silently become a second, permanently empty queue does NOT live here --
    # it lives in `cli.run_worker`, which refuses an `--outdir` that does not exist before ever
    # calling this. Anything that invokes `main_loop` directly (the web server in a later task)
    # inherits the responsibility for that check along with it.
    q.layout(root)
    stop = stop if stop is not None else threading.Event()
    ran = 0

    with hold_worker_lock(root), _stop_signals(stop):
        while not stop.is_set():
            state = q.reconcile(root)
            if state.alive:
                if stop.wait(poll):
                    break
                continue
            if _llm_holds_gpu(root):
                if stop.wait(poll):
                    break
                continue
            job = q.claim(root)
            if job is None:
                if stop.wait(poll):
                    break
                continue
            run_job(root, job, spawn=spawn)
            ran += 1

    return ran
