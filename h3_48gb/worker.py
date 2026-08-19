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
import random
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


def _project_arg(args: list[str]) -> Path:
    """The value following `--project` in a `kind="song"`/`kind="assemble"` job's `args` (task 3's
    brief: `["song", "--project", <path>]` / `["assemble", "--project", <path>]`). Raises
    `ValueError` if the token is missing -- a job in this shape without it is malformed, not a case
    either dispatcher can run.
    """
    for i, token in enumerate(args):
        if token == "--project" and i + 1 < len(args):
            return Path(args[i + 1])
    raise ValueError(f"job args have no --project value: {args!r}")


@contextlib.contextmanager
def _caffeinate_block(spawn=subprocess.Popen):
    """Hold the machine awake (`caffeinate -dimsu`, no target command) for the duration of the
    block -- the in-process equivalent of what a `KIND_GENERATE` job gets for free by putting
    `caffeinate` in front of the spawned child's own argv (`job_command`). `KIND_SONG`/
    `KIND_ASSEMBLE` jobs run their heavy subprocesses (Music3, ffmpeg, `mlx_whisper`) from
    *inside* this worker process, as `h3_48gb.songrun`'s/`h3_48gb.assemble`'s own blocking
    `subprocess.run` calls, rather than as one spawned child `run_job` can wrap the usual way -- so
    there is no argv to put `caffeinate` in front of. This wraps the call by hand instead: start a
    bare `caffeinate -dimsu` (no target -- it simply asserts its claims until told to stop, see
    `man caffeinate`) before the block and terminate it after, success or failure. Skipping this
    would leave exactly the 2026-08-10 exposure the module docstring describes (a sleeping machine
    took the GPU firmware down mid-run) unguarded for every song/assemble job, which is why "GPU
    lock/lease/pause unchanged; song жрёт GPU так же, как generate" (task 3 brief) is read here as
    including this, not only the lease.

    `spawn` is the same injection point `run_job` already takes for its `KIND_GENERATE` child, so a
    test substitutes one fake for both rather than needing a second seam.

    **`-w <this worker's own pid>`** (fix round 1, M1, 2026-08-18 review): binds the bare
    `caffeinate`'s own lifetime to the worker process's, on top of the `terminate()`/`wait()` this
    function already does in its `finally`. The `finally` alone only fires on an orderly exit or an
    exception -- a worker taken down by `kill -9` (the exact scenario C1 of this same review round
    is about: a second `SIGTERM` reaching a worker mid-song-job used to set `SIG_DFL` and re-deliver
    the signal to *itself*, which is indistinguishable from `kill -9` as far as Python's own
    `finally` blocks are concerned) skips it entirely, and the bare `caffeinate` this function
    started is left running with no target and nothing left to stop it -- forever, per `man
    caffeinate`'s own "-w: waits for the process ... to exit" contract read backwards: without `-w`,
    nothing waits for anything, so nothing exits. `-w <pid>` makes `caffeinate` itself watch the
    worker's pid and exit the moment it is gone, whichever way it goes -- this is not a replacement
    for the `finally` (a `caffeinate` that already believes the "normal" case's `terminate()` reached
    it will simply see its watched process gone one instruction later and exit anyway), it is what
    closes the gap the `finally` cannot.
    """
    proc = spawn(["caffeinate", "-dimsu", "-w", str(os.getpid())])
    try:
        yield
    finally:
        proc.terminate()
        proc.wait()


def _tracked_child_run(spawn=subprocess.Popen):
    """Build a `run` callable for `songrun.run_song`/`align_track` and (task 4) `assemble.run`
    that registers each subprocess it starts as `_current_child` for the exact duration of the
    call -- the seam task 2 left for this (every subprocess call in `songrun.py` goes through one
    injected `run(cmd, capture_output=True, text=True) -> CompletedProcess`, see
    `songrun._run_or_raise`'s own docstring, and `assemble.py` matches it exactly), closing C1
    (fix round 1, 2026-08-18 review), extended to a `KIND_ASSEMBLE` job's own `ffmpeg`/`ffprobe`
    subprocess in task 4 (the same class of gap, per the controller's decision recorded in the
    task 3 report: "assemble.run получит run= ... тот же класс дыры, что C1"):

    Before this, `KIND_SONG`/`KIND_ASSEMBLE` never set `_current_child` at all (only
    `KIND_GENERATE`'s own top-level `Popen` did) -- so a *second* `SIGTERM` arriving while a song job
    was mid-flight (a human running `web-stop.sh` twice, say) hit `_terminate_child_group`'s
    `if proc is None: return` and signalled nothing: `_stop_signals`' handler still set `SIG_DFL` and
    re-delivered the signal to the worker itself, taking the *worker* process down immediately, with
    its currently-running Music3/`ffmpeg`/`mlx_whisper` child (and the bare `caffeinate`
    `_caffeinate_block` started, see that function's own M1 note) left running with no worker, no
    lease and nothing left to reap them -- ~30 GB stuck on the GPU until a human noticed, and
    `reconcile` would meanwhile see the lease gone and hand the same track straight back to
    `pending/`, so the *next* worker starts a second Music3 run right next to the orphaned first one.

    The returned `run` does exactly what `KIND_GENERATE`'s own dispatch already does for its
    top-level child (`run_job`'s `KIND_GENERATE` branch): `start_new_session=True` so
    `_terminate_child_group`'s `os.killpg` reaches the whole tree, `_current_child` set before the
    blocking wait and cleared in a `finally` right after -- the exact mechanism `_terminate_child_group`
    already knows how to use, reused rather than duplicated. `songrun.py` itself needed **no
    changes** for this: it already always calls `run(cmd, capture_output=True, text=True)` and
    expects a `subprocess.CompletedProcess` back, which is exactly what a real `Popen` +
    `communicate()` produces once assembled by hand.

    `spawn` is the same seam `_caffeinate_block` and `run_job`'s `KIND_GENERATE` branch already take
    -- one fake `spawn` in a test covers the bare `caffeinate` companion process and every one of a
    song job's own Music3/`ffmpeg`/`mlx_whisper` calls.
    """
    def run(cmd, *, capture_output=True, text=True):
        global _current_child
        kwargs: dict = {"start_new_session": True}
        if capture_output:
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.PIPE
        if text:
            kwargs["text"] = True
        proc = spawn(cmd, **kwargs)
        _current_child = proc
        try:
            stdout, stderr = proc.communicate()
        finally:
            _current_child = None
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

    return run


#: Music3 runs roughly 11-13x slower than the song it produces (design spec, "Трек": "~11-13x
#: реалтайма"); task 3's brief pins the queue-facing wall-clock estimate at the upper bound, 13x,
#: so the number a human sees errs toward "this will take a little less", never the other way.
SONG_WALLCLOCK_FACTOR = 13.0


def song_job_wallclock_estimate_seconds(lyrics: str) -> float:
    """How long a `kind="song"` job is expected to take **to run** (wall clock), as opposed to how
    long the song itself will play: `h3_48gb.songrun.estimate_duration(lyrics)` (the song's own
    length estimate) times `SONG_WALLCLOCK_FACTOR`. For the caller that submits a song job
    (`queue.submit(..., kind=q.KIND_SONG, estimate={...})`) to build `estimate`'s number from --
    this module has no opinion about the rest of that dict's shape, matching `queue.Job.estimate`'s
    own "whatever the caller put there" contract.
    """
    from h3_48gb import songrun
    return songrun.estimate_duration(lyrics) * SONG_WALLCLOCK_FACTOR


def _run_song_job(job, *, spawn=subprocess.Popen) -> tuple[int, str]:
    """The `kind="song"` job body (task 3): load the project `job.args` names, run
    `h3_48gb.songrun.run_song` (or, for an imported track, `align_track` -- the align-only path,
    design spec addendum "импортированный трек") against its `track`, and record the result on the
    project through the **locked** mutators (`update_track`/`set_stage_status`), never `save()` --
    see `project.py`'s own docstring for why a blind `save()` on a live project risks clobbering a
    concurrent write.

    Returns `(exit_code, log_text)`, the same shape `run_job` already gets back from a spawned
    subprocess, so the rest of `run_job`'s contract (write the result marker, `q.finish`) does not
    need to know or care that nothing was actually spawned for this job as a whole.

    **An `undersung` take is still `exit_code == 0`** (task 3, review decision): the mp3s exist,
    the job did what it was asked, and the design spec's gate is a human listening and deciding,
    not this function refusing to hand off a take that might need a redo. The warning travels on
    the *project* (`track.undersung`), not on the job's own exit code -- a `kind="song"` job that
    actually raises (a subprocess failed to start, the project file will not load, `track.source`
    is `"import"` with no `mp3` set) is the only case this returns non-zero for.

    **C1 (fix round 1, 2026-08-18 review): `run=_tracked_child_run(spawn)` is passed into
    `songrun.run_song`/`align_track`.** Both already accept a `run` keyword -- the seam task 2 built
    for testing without a real subprocess -- so no change to `songrun.py` was needed: the worker
    supplies the *same* mechanism `KIND_GENERATE` uses (`_current_child` + `start_new_session=True`)
    through that existing seam instead of a bare `subprocess.run`, and `_terminate_child_group`
    (already written for `KIND_GENERATE`) then reaches a song job's own Music3/`ffmpeg`/
    `mlx_whisper` child for free. See `_tracked_child_run`'s own docstring for the orphan this
    closes.

    **I1 (fix round 1): a song generation's seed is resolved and recorded, not silently defaulted
    to `0`.** `track.get("seed")` -- if `None` (a fresh track, `_empty_track`'s own default) --
    is replaced with a fresh `random.randrange(2**31)` roll, and the value actually used is written
    back onto the project through the same `update_track` call as everything else the job produced,
    so a duplicate ("Копия" on a `done` song job, task 6) can reproduce the exact take rather than
    rolling a different random seed on every retry. Only the generation branch touches `seed` --
    `track.source == "import"` has no seed to resolve or record.

    **I4 (fix round 1): `track.duration` is recorded too** -- `result.duration`, from either path
    (`run_song`'s actual generated length, `align_track`'s `ffprobe`-read one).

    Never lets a bare exception escape: whatever `h3_48gb.project`/`h3_48gb.songrun` raise is
    caught and turned into `(1, <message>)`, because a job whose body raises must still let
    `run_job` write a result marker and move the file out of `running/` -- an uncaught exception
    here would instead take the whole worker process down mid-loop.
    """
    from h3_48gb import project as project_module
    from h3_48gb import songrun

    log_lines: list[str] = []
    try:
        project_path = _project_arg(job.args)
        proj = project_module.load_project(project_path)
        track = proj.track
        # Task 1 ("Сюжет клипа" wave, "авто-лирика"): kept nullable here, *not* coerced to `""`
        # the way the generation branch below needs -- `raw_lyrics is None` is exactly the signal
        # `songrun.align_track` uses to switch into its no-reference transcription mode (its own
        # docstring). `or None` folds an *empty* string into that same signal (Task 1 review I1):
        # PROMPT_SCHEMA types `lyrics` as string-or-null, so a chat model answering `""` for "no
        # lyrics" must not silently land in the reference-matching branch, where the transcript
        # Whisper just computed would be thrown away and `lyrics_auto` never written. Only the
        # import branch ever sees this raw value; the generation branch still falls back to `""`
        # immediately below, unchanged from before this task.
        raw_lyrics = track.get("lyrics") or None
        caption = track.get("caption") or ""
        track_dir = proj.path.parent / "track"
        run = _tracked_child_run(spawn)
        seed_used = None  # only the generation branch below has one to record (I1)

        with _caffeinate_block(spawn=spawn):
            if track.get("source") == "import":
                mp3_path = track.get("mp3")
                if not mp3_path:
                    raise songrun.SongRunError(
                        "track.source is 'import' but track.mp3 is not set")
                log_lines.append(f"align-only (imported track): {mp3_path}")
                result = songrun.align_track(track_dir, mp3_path, raw_lyrics, run=run)
            else:
                lyrics = raw_lyrics or ""
                duration = songrun.estimate_duration(lyrics)
                seed_used = track.get("seed")
                if seed_used is None:
                    seed_used = random.randrange(2**31)
                log_lines.append(
                    f"generating: estimated duration={duration}s seed={seed_used}")
                result = songrun.run_song(track_dir, lyrics, caption, duration, seed=seed_used,
                                           run=run)

        update_fields = dict(
            wav=str(result.wav), mp3=str(result.mp3), mastered_mp3=str(result.mastered_mp3),
            sections=result.sections, status="awaiting_approval", undersung=result.undersung,
            duration=result.duration,
        )
        if seed_used is not None:
            update_fields["seed"] = seed_used
        if result.raw_segments is not None:
            # Task 1: only `align_track(..., lyrics=None)` ever sets this (see `SongResult.
            # raw_segments`'s own docstring) -- `lyrics_auto` is the same call's `transcript`,
            # given a track-facing name so a human (or Task 2's scenario LLM) reads it as "the
            # lyrics we don't actually have, guessed from the audio", not as a debug log field.
            update_fields["lyrics_auto"] = result.transcript
            update_fields["raw_segments"] = result.raw_segments
        proj.update_track(**update_fields)
        proj.set_stage_status("track", "awaiting_approval")
        log_lines.append(
            f"song job done: undersung={result.undersung}, duration={result.duration}s")
        return 0, "\n".join(log_lines) + "\n"
    except Exception as exc:  # noqa: BLE001 -- a song job's own failure must route to failed/,
        # not crash the worker loop; see the docstring.
        log_lines.append(f"song job failed: {type(exc).__name__}: {exc}")
        # I1 (final review): before this, a raising song job left `stages.track` exactly where it
        # was -- "draft" for a project's first-ever attempt (script approval never moves it),
        # "running" for a "Пересчитать трек" retry (`web._retry_project_track` moves it there right
        # after resubmitting) -- with nothing actually running any more. Nothing else in this
        # codebase ever revisits a `track` stage that is not `"draft"`/`"awaiting_approval"`
        # (`_retry_project_track`'s own gate, before this fix), so the project was stuck: the retry
        # route refused every further attempt with `project_stage_not_ready`, forever, and the only
        # way out was hand-editing `project.json`. Mirrors `_mark_assembly_failed` exactly, one
        # stage over -- see that function's own docstring for why this is best-effort and never
        # raises.
        project_path = _try_project_arg(job.args)
        if project_path is not None:
            _mark_track_failed(project_path)
        return 1, "\n".join(log_lines) + "\n"


def _try_project_arg(args):
    """`_project_arg(args)`, or `None` if `args` cannot even be parsed that far -- lets
    `_run_song_job`'s own except block call `_mark_track_failed` only when there is a project path
    to mark at all, the same guard `_run_assemble_job` gets from resolving `project_path` ahead of
    its own `try` block. A song job's `args` is validated at submission time (`queue.submit`'s own
    `_validate_args_shape_for_kind`), so this only ever matters for a malformed job that reached the
    worker some other way.
    """
    try:
        return _project_arg(args)
    except Exception:  # noqa: BLE001 -- "no project to mark" is the only thing this needs to know.
        return None


def _mark_track_failed(project_path) -> None:
    """Best-effort: record `stages.track = "failed"` on the project a `kind="song"` job named (I1,
    final review) -- called from `_run_song_job`'s own failure path so a crashed (or malformed-
    import) song job does not leave `stages.track` stuck `"draft"`/`"running"` forever with nothing
    actually generating. Mirrors `_mark_assembly_failed` exactly (see its own docstring for why this
    never raises): a second failure here (a moved project, a corrupt `project.json`) must not
    replace the job's own honest failure message with an unrelated traceback.
    """
    try:
        from h3_48gb import project as project_module
        proj = project_module.load_project(project_path)
        proj.set_stage_status("track", "failed")
    except Exception:  # noqa: BLE001 -- see docstring.
        pass


def _mark_assembly_failed(project_path) -> None:
    """Best-effort: record `stages.assembly = "failed"` on the project a `kind="assemble"` job
    named (I1a, fix round 1, 2026-08-18 review) -- called from `_run_assemble_job`'s own failure
    paths so a crashed (or import-broken) assembly job does not leave the stage stuck `"running"`
    forever: nothing else in this codebase ever revisits an `assembly` stage that is not
    `"draft"` (`advance_project._submit_assembly`'s own idempotency gate), so without this a
    failed assembly job looked, permanently, like one that was still in flight. Never raises --
    this runs from inside an already-failing job's own except block, and a second failure here (a
    moved project, a corrupt `project.json`) must not replace the job's own honest failure message
    with an unrelated traceback. A retry needs a human resetting the stage back to `"draft"`
    first -- task 6's "пересчёт" button, out of this task's own scope (see the module's report).
    """
    try:
        from h3_48gb import project as project_module
        proj = project_module.load_project(project_path)
        proj.set_stage_status("assembly", "failed")
    except Exception:  # noqa: BLE001 -- see docstring.
        pass


def _run_assemble_job(job, *, spawn=subprocess.Popen) -> tuple[int, str]:
    """The `kind="assemble"` job body (task 3 dispatches; the implementation is task 4's). A lazy
    `import h3_48gb.assemble` -- not a module-level one -- so this module keeps working, and every
    `kind="generate"`/`kind="song"` job keeps running, on a checkout that has task 3 but not yet
    task 4: the import failing is reported as an honest job failure (`assemble` "появится в Task
    4"), not an `ImportError` that takes the worker process down. A test substitutes a fake
    `h3_48gb.assemble` module (`sys.modules["h3_48gb.assemble"]`) to exercise the dispatch without
    the real module existing.

    **`run=_tracked_child_run(spawn)` is passed into `assemble.run`** (task 4, closing the one gap
    task 3's own C1 fix left open -- see that fix's own note in `run_job`'s docstring and in the
    task 3 report: "`KIND_ASSEMBLE` is the one branch [C1] does not [cover]"). `assemble.run`'s
    signature (task 4, controller's correction over the task 3 report's original
    `assemble.run(project_path)`) is `run(project_path, *, run=subprocess.run) -> Path`, taking the
    exact same seam `songrun.run_song`/`align_track` already accept -- so a second `SIGTERM`
    arriving mid-assembly now reaches ffmpeg/ffprobe through `_terminate_child_group`'s `os.killpg`
    exactly as it already reaches a song job's own subprocess.

    **I1a (fix round 1, 2026-08-18 review): every failure path here also calls `_mark_assembly_
    failed`** -- the import failing, `assemble.run` itself raising, anything -- so `stages.assembly`
    never sits stuck `"running"` after a job that is actually dead. `project_path` is resolved
    once, ahead of both `try` blocks, specifically so it is available to the `ImportError` branch
    too; if `_project_arg` itself cannot parse `job.args` (malformed beyond what `queue.submit`'s
    own `_validate_args_shape_for_kind` already guards against at submission time), there is no
    project to mark and this just fails the job honestly, as before.
    """
    log_lines: list[str] = []
    try:
        project_path = _project_arg(job.args)
    except Exception as exc:  # noqa: BLE001 -- malformed args, nothing to mark; fail the job.
        log_lines.append(f"assemble job failed: {type(exc).__name__}: {exc}")
        return 1, "\n".join(log_lines) + "\n"

    try:
        try:
            from h3_48gb import assemble
        except ImportError as exc:
            log_lines.append(
                f"assemble job failed: h3_48gb.assemble ещё не реализован (появится в Task 4): "
                f"{exc}")
            _mark_assembly_failed(project_path)
            return 1, "\n".join(log_lines) + "\n"

        run = _tracked_child_run(spawn)
        with _caffeinate_block(spawn=spawn):
            assemble.run(project_path, run=run)
        log_lines.append("assemble job done")
        return 0, "\n".join(log_lines) + "\n"
    except Exception as exc:  # noqa: BLE001 -- same reasoning as `_run_song_job`.
        log_lines.append(f"assemble job failed: {type(exc).__name__}: {exc}")
        _mark_assembly_failed(project_path)
        return 1, "\n".join(log_lines) + "\n"


def _handle_project_scene_result(root, outdir, job, exit_code: int, *, run) -> None:
    """After a `KIND_GENERATE` job is filed done/failed, if its `note` marks it as a project
    scene's own job (`h3_48gb.assemble.scene_note`'s format -- design spec: "Клипы -- обычные
    generate-задачи с пометкой project/scene в note"), record the scene's outcome on its project
    and continue the scene chain (`h3_48gb.assemble.advance_project`) -- task 4's brief, verbatim:
    "advance_project ... вызывается воркером после каждого done/failed задания проекта".

    An ordinary generate job (no project scene note, which is every job before task 4 and most
    jobs after it -- the web form's own submissions) is a no-op here: `parse_scene_note` returns
    `None` for anything that does not look exactly like this module's own `scene_note` format, and
    this function returns immediately without touching `h3_48gb.project`/`h3_48gb.assemble` at all.

    `clip_path` is read off the job's own `output_stem` (`f"{job.output_stem}.mp4"`) -- the same
    convention `queue._stem_taken`'s `ARTIFACT_SUFFIXES` check already assumes for every
    `KIND_GENERATE` job, and the only place this function can learn where the run actually wrote
    without re-deriving `RunSpec.output_stem()`'s own canvas-dependent logic (which needs `mlx` for
    an `--image` run -- exactly what this module must never import, see its own docstring). A
    `done` job whose `.mp4` does not actually exist at that path (an unexpected shape a future bug
    could produce) is treated the same as `failed` -- a scene without a real clip on disk is not
    something the next scene's automatic keyframe or the final assembly can use either way.

    A lazy `import h3_48gb.assemble`/`import h3_48gb.project` -- not module-level ones -- for the
    same reason `_run_assemble_job`'s is lazy: an ordinary generate job (the overwhelming majority
    of `KIND_GENERATE` jobs) must keep working on a checkout where `assemble.py` does not exist,
    and importing it unconditionally at module scope would break that for no reason, since this
    function returns before touching either module whenever `parse_scene_note` finds nothing.

    **Never lets an exception escape.** A missing/corrupt `project.json`, a project directory that
    moved, or a scene-chain step failing (a keyframe extraction, a submission) must not take the
    whole worker process down over bookkeeping for a job that has *already* been filed done/failed
    -- the queue's own record of this job is already correct and final by the time this runs; only
    what happens *next* for its project is at stake. One line to stderr, matching `_llm_holds_gpu`'s
    own "never let this kill the loop" discipline.

    **I3 (fix round 1, 2026-08-18 review): the `h3_48gb.assemble`/`h3_48gb.project` imports
    themselves are inside the `try`, not above it.** They used to sit ahead of this function's own
    `try` block, so an `ImportError` here (the exact checkout-predates-task-4 shape `_run_assemble_
    job`'s own lazy import is deliberately protected against) would propagate straight out of
    `run_job`, contradicting this module's own docstring ("must not take the whole worker process
    down") for every `KIND_GENERATE` job, not only project-scene ones. A broken import is now just
    another reason to return without touching either module, exactly like `parse_scene_note`
    finding nothing.

    **C2 (fix round 1, 2026-08-18 review): a scene's `job_id` on disk must match `job.id` before
    this writes `done`/`failed`.** Without this, a *stale* job -- one whose scene was reset by an
    `invalidate_scene_chain` (a human's "пересчитать сцену") after this job was already queued
    against the old attempt, or one that simply lost a race to a newer submission for the same
    index -- would overwrite the *current* attempt's outcome with its own: a scene correctly
    `running` under a fresh job silently flipped to `done` with a clip from a keyframe that no
    longer exists in the chain, or to `failed` for a job nobody is waiting on any more. The check
    reads the project fresh (`project_module.load_project`, the same call this function already
    makes) and compares `scenes[idx]["job_id"]` to `job.id` *before* writing anything -- a mismatch
    means this job's own outcome no longer speaks for scene `idx`, and the right move is to leave
    the project exactly as the newer attempt already has it and say nothing further (one line to
    stderr, not a raise -- this is an expected, not exceptional, shape once retries exist).
    """
    try:
        from h3_48gb import assemble
        from h3_48gb import project as project_module
    except ImportError:
        return

    parsed = assemble.parse_scene_note(job.note)
    if parsed is None:
        return
    project_id, idx = parsed
    try:
        proj = project_module.load_project(Path(outdir) / "projects" / project_id)
        scene = next((s for s in proj.scenes if s.get("idx") == idx), None)
        if scene is None or scene.get("job_id") != job.id:
            print(f"h3 worker: stale project scene job {job.id} for project {project_id!r} "
                  f"scene {idx} (scene's own job_id is {scene.get('job_id') if scene else None!r})"
                  f" -- ignoring, a newer attempt already owns this scene", file=sys.stderr)
            return
        clip_path = f"{job.output_stem}.mp4"
        if exit_code == 0 and Path(clip_path).is_file():
            proj.set_scene_status(idx, "done", clip_path=clip_path)
        else:
            proj.set_scene_status(idx, "failed")
        assemble.advance_project(proj, root, outdir, run=run)
    except Exception as exc:  # noqa: BLE001 -- see docstring: bookkeeping for an already-filed job
        # must never take the worker down.
        print(f"h3 worker: project scene bookkeeping failed for job {job.id} "
              f"(project {project_id!r} scene {idx}): {type(exc).__name__}: {exc}",
              file=sys.stderr)


def run_job(root, job, spawn=subprocess.Popen, outdir=None) -> int:
    """Run one claimed job to completion and file its result. Returns the job's exit code.

    The order is the contract, and every step of it is load-bearing:

    1. take the lease -- before the job's work starts, so there is no window in which the job is in
       `running/` with nothing holding its lease;
    2. run the job, dispatched by `job.kind` (task 3, "Проекты" -- see the three branches below);
    3. write the result marker **durably**;
    4. only then release the lease;
    5. and only then move the job out of `running/`.

    Steps 3 and 4 in the other order leave a job that looks free, has no marker and has no `.mp4`,
    i.e. indistinguishable from one that never got anywhere -- reconciliation would re-queue a run
    that had already finished, and the rerun would overwrite its output.

    **Dispatch by `job.kind`, all three under the one lease held for step 1-4 above** -- the lease
    is what makes "this job is running right now" observable from another process, and that fact
    does not depend on *how* the job runs:

    * `KIND_GENERATE` -- unchanged from before this field existed: `job_command(job)` in its own
      session (`start_new_session=True`), stdout and stderr appended to `queue/logs/<id>.log`. The
      new session is what makes the second stop signal able to reach the whole tree -- `caffeinate`
      is only the parent of the Python that holds the 36 GB, and signalling the parent alone
      orphans the child.
    * `KIND_SONG` -- `_run_song_job`, in this process: `h3_48gb.songrun.run_song`/`align_track`
      against the project `job.args` names, then the track's status onto the project through its
      own locked mutators. See `_run_song_job`'s docstring for why an undersung take is still
      success.
    * `KIND_ASSEMBLE` -- `_run_assemble_job`, in this process: a lazy `h3_48gb.assemble.run` (task
      4; on a checkout that predates task 4, the lazy import itself fails the job honestly instead
      of taking the worker down -- see that function's own docstring).

    **After a `KIND_GENERATE` job is filed, `_handle_project_scene_result` checks whether it was a
    project scene's own job** (task 4's `h3_48gb.assemble.scene_note` tagging its `note` --
    design spec, "Клипы: обычные generate-задачи с пометкой project/scene в note") and, if so,
    records the scene's outcome on its project and continues the scene chain
    (`h3_48gb.assemble.advance_project`) -- task 4's brief: "advance_project ... вызывается
    воркером после каждого done/failed задания проекта". See that function's own docstring for why
    this never lets a broken project take the worker down, and `outdir`'s docstring below for where
    it looks for the project.

    **I1b (fix round 1, 2026-08-18 review): after a `KIND_SONG`/`KIND_ASSEMBLE` job is filed,
    `_advance_project_after_job` calls `advance_project` too** -- the task brief's own "после
    каждого done/failed задания проекта", taken literally, is not scoped to scene jobs alone. See
    that function's own docstring for why this is a safe no-op in every shape this codebase
    currently reaches.

    **`_current_child` is set by `KIND_GENERATE` directly, and by `KIND_SONG`/`KIND_ASSEMBLE`
    through `_run_song_job`/`_run_assemble_job`'s own `run=_tracked_child_run(spawn)`** (fix round
    1, C1, 2026-08-18 review, extended to `KIND_ASSEMBLE` in task 4 -- see `_tracked_child_run`'s
    own docstring for the orphaned-GPU-process gap this closes): all three ultimately go through
    the same `_current_child` + `start_new_session=True` mechanism, so `_terminate_child_group`'s
    "kill the child's process group" reaches a song job's own Music3/`ffmpeg`/`mlx_whisper`
    subprocess, and an assemble job's own `ffmpeg`/`ffprobe` subprocess, exactly as it already
    reached `KIND_GENERATE`'s. No known gap remains here for any of the three kinds.

    `spawn` is injectable because every test of this function must not start a real generation: a
    second MLX process on this machine is 36 GB against a 48 GB budget. The same `spawn` is reused
    by `_caffeinate_block` for the song/assemble branches' `caffeinate` companion process, and by
    `_tracked_child_run` for a song job's own subprocess calls, so one fake covers all three.

    `outdir` is where a project scene's own job would be found under `<outdir>/projects/<id>/` --
    the same value `main_loop` already resolves for `_llm_holds_gpu` (`root`'s parent whenever
    `root` is `cli.queue_root(outdir)`, the only shape `root` actually takes), defaulted here the
    identical way so a direct caller of `run_job` (every existing test) does not have to start
    passing it. It is read only by `_handle_project_scene_result`, and only for a `KIND_GENERATE`
    job whose `note` actually parses as a project scene's -- an ordinary generate job never reads
    it at all.
    """
    global _current_child
    root = Path(root)
    outdir = Path(outdir) if outdir is not None else root.parent
    lease = _acquire_lease(root, job.id)
    try:
        if job.kind == q.KIND_GENERATE:
            # Append rather than truncate: a job returned to `pending/` by reconciliation resumes
            # from its checkpoint, and the log of the attempt that was interrupted is the only
            # record of why it was interrupted.
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
        else:
            if job.kind == q.KIND_SONG:
                exit_code, log_text = _run_song_job(job, spawn=spawn)
            elif job.kind == q.KIND_ASSEMBLE:
                exit_code, log_text = _run_assemble_job(job, spawn=spawn)
            else:
                exit_code, log_text = 1, f"unknown job kind {job.kind!r}\n"
            with open(q.log_path(root, job.id), "ab") as stream:
                stream.write(log_text.encode("utf-8"))
            finished_at = _now()
            log_tail = q.read_log_tail(root, job.id)
        q.write_result_marker(root, job.id, exit_code, finished_at)
    finally:
        _release_lease(lease)

    q.finish(root, job.id, exit_code, log_tail, finished_at=finished_at)

    if job.kind == q.KIND_GENERATE:
        _handle_project_scene_result(root, outdir, job, exit_code, run=_tracked_child_run(spawn))
    elif job.kind in (q.KIND_SONG, q.KIND_ASSEMBLE):
        _advance_project_after_job(root, outdir, job, run=_tracked_child_run(spawn))

    return exit_code


def _advance_project_after_job(root, outdir, job, *, run) -> None:
    """I1b (fix round 1, 2026-08-18 review): continue `job`'s own project's scene chain after a
    `KIND_SONG`/`KIND_ASSEMBLE` job is filed done/failed -- the task brief, read literally:
    "advance_project ... вызывается воркером после каждого done/failed задания проекта", not only
    a scene's own `KIND_GENERATE` job (`_handle_project_scene_result`'s own long-standing job).

    **A no-op in every shape this codebase currently reaches, and that is the point, not a bug.**
    A `KIND_SONG` job finishes before its project has any `scenes` populated at all (task 6, not
    built yet, is what turns an approved script into a `scenes` list) -- `advance_project` sees an
    empty `proj.scenes` and returns `"nothing_to_do"` by construction (its own docstring). A
    `KIND_ASSEMBLE` job's own completion (`assemble.run` itself already sets `stages.assembly` to
    `"done"`, and `_run_assemble_job`'s own I1a fix sets it to `"failed"` on any crash) is the
    terminal state for its project either way -- calling `advance_project` again afterward re-reads
    `stages.assembly != "draft"` and, again, does nothing. This hook exists anyway, symmetrically
    with `_handle_project_scene_result`, so a *future* gate change that lets a track and a scene
    chain coexist mid-flight (or a retried assembly) does not silently need this wiring added back.

    `job.args` is `["song"/"assemble", "--project", <path>]` either way (`queue._validate_args_
    shape_for_kind`'s own guarantee at submission time), so `_project_arg` -- the same helper
    `_run_song_job`/`_run_assemble_job` already use to find their project -- is what this reads.

    **Never lets an exception escape**, the same discipline as `_handle_project_scene_result`'s own
    (a missing project, a corrupt `project.json`): this runs after the job's own result is already
    filed and final, so a failure here must only be logged, never propagated into `run_job`.
    """
    try:
        from h3_48gb import assemble
        from h3_48gb import project as project_module
    except ImportError:
        return
    try:
        project_path = _project_arg(job.args)
        proj = project_module.load_project(project_path)
        assemble.advance_project(proj, root, outdir, run=run)
    except Exception as exc:  # noqa: BLE001 -- see docstring: bookkeeping for an already-filed job
        # must never take the worker down.
        print(f"h3 worker: project advance failed after job {job.id} (kind={job.kind!r}): "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)


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


def _llm_holds_gpu(outdir) -> bool:
    """A resident local LLM and a 27 GB generation cannot share 48 GB. The chat page owns the
    *decision* to free the GPU -- it asks the human and, on confirmation, calls
    `POST /api/llm/unload` -- the worker only ever observes whether the port still answers.

    `outdir` is where `provider.load_providers` looks for `providers.json` -- the web server's
    `--outdir`, not the queue directory underneath it (`cli.queue_root(outdir) ==
    outdir / "queue"`). `main_loop` is the only caller and resolves that distinction once; a
    second caller passing the queue root here would find no roster and this check would silently
    never fire, exactly the bug fixed in this round.

    Checks **every** `llama-local` provider in the roster (`provider.local_ports`), not only the
    one `providers.json` names `active` (finding I1). The chat page's per-turn provider dropdown
    can raise a `llama-local` provider that is not `active` without ever touching that field, so
    "active provider is external, some other local one is resident" used to be invisible here --
    the worker would claim a 27 GB job right next to a 30 GB resident model it never looked at.

    **Never lets a broken `providers.json` reach `main_loop`** (BACKLOG "UX-мелочи", task 1). This
    gate only ever *observes* -- it holds no lock, changes nothing -- so a file that is mid-write, a
    human's bad hand-edit, or any other reason `json.loads` chokes is not a reason to take the whole
    worker down with it. Answering `False` (no LLM visible) is the same direction `reconcile`/`claim`
    already fail toward when something is wrong, and the one line to stderr is deliberate: silently
    swallowing it would let a corrupt file sit unnoticed for days, with the queue quietly never
    respecting a resident model it can no longer see.
    """
    try:
        roster = provider.load_providers(outdir)
        return any(provider.port_alive(port) for port in provider.local_ports(roster))
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring: nothing here may
        # be allowed to kill the loop over a file this gate only ever reads.
        print(f"h3 worker: providers.json unreadable, treating the LLM gate as clear: {exc}",
              file=sys.stderr)
        return False


def main_loop(root, poll: float = 5.0, stop=None, spawn=subprocess.Popen, outdir=None) -> int:
    """Run queued jobs until asked to stop. Returns how many jobs were run to completion.

    Each pass reconciles first (`queue.reconcile`), because the previous worker may have been
    killed mid-run and the wreckage has to be understood before anything new is started. If
    reconciliation reports a job whose lease is held right now, this worker takes **nothing**: that
    is another live generation, and a second one means two 36 GB processes on a 48 GB machine.
    It sleeps `poll` and reconciles again instead of waiting for ever -- once the other run is
    gone, the queue has to move.

    The next gate of the same "take nothing, sleep, look again" shape is `q.is_paused(root)`. A
    human pauses the queue from the page (`POST /api/queue/pause`) for reasons this loop cannot see
    -- about to close the laptop, wants the machine quiet for something else -- and the job already
    in `running/` (there can be at most one, this worker's own) is never interrupted by a pause,
    exactly as it is never interrupted by `stop`. Ordered right after `reconcile`, so a paused queue
    still recovers wreckage from a killed worker -- pausing the *selection* of new work must not
    also pause the bookkeeping that keeps `running/` honest.

    **Ordered *before* the LLM gate, not after it** (BACKLOG "UX-мелочи", task 1; it used to sit
    after). `_llm_holds_gpu` reads `providers.json` through `provider.load_providers`, and that file
    lives entirely outside this loop's control -- a human can leave it mid-edit or simply broken.
    `_llm_holds_gpu` itself no longer lets that kill the worker (see its own docstring), but even a
    clean, correctly-swallowed failure there must not stand between a human pausing the queue and
    the queue actually pausing: pausing is the one control this loop promises always works, laptop
    about to close or not, so it cannot depend on a file it does not own being well-formed.

    A third gate of the same shape sits right after the pause check, still before `claim`: a
    resident local LLM holding the GPU (`_llm_holds_gpu`) -- a 27 GB generation cannot start next to
    a 30 GB model. Unlike a live lease, nothing here ever unloads the LLM -- only a human, confirming
    on the chat page, does that via `POST /api/llm/unload` -- so this purely observes the port until
    the human's confirmation makes it go quiet.

    The pause gate is also raised from inside this loop and not only by a human: once a finished
    job leaves nothing in `pending/`, `q.pause_if_drained` puts the marker up. A queue that empties
    itself stops rather than standing ready to grab whatever lands next -- the morning after a
    night's batch, the two jobs a human drops into the form to *look at* before starting would
    otherwise begin computing on their own. The check happens after each `run_job` and not on the
    `claim`-returned-`None` branch on purpose: pausing on an idle pass would also pause a queue a
    human had just resumed and not yet filled. See `pause_if_drained` for why the emptiness test and
    the marker share one lock acquisition, and note that it cannot fire between two queued jobs --
    with the second still in `pending/` the queue is not drained.

    `stop` is anything with `is_set()`/`wait()`; `_stop_signals` sets the one created here when the
    first `SIGTERM`/`SIGINT` arrives. It stops the *selection* of new jobs only -- a job already
    running is never interrupted by it, which is why the check sits at the top of the loop and
    never inside `run_job`.

    `spawn` is declared here, not added later, because every recovery test in `tests/test_worker.py`
    has to substitute it: the real one launches a 36 GB generation.

    `outdir` is where `_llm_holds_gpu` reads `providers.json` from -- the web server's `--outdir`,
    which is `root`'s *parent* whenever `root` is `cli.queue_root(outdir)`, the only shape `root`
    actually takes in this codebase. Defaulting to `Path(root).parent` keeps every existing direct
    caller of `main_loop` working unchanged; `cli.run_worker` passes it explicitly so the intent
    does not depend on that default staying correct if the queue ever moves.
    """
    root = Path(root)
    outdir = Path(outdir) if outdir is not None else root.parent
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
            if q.is_paused(root):
                if stop.wait(poll):
                    break
                continue
            if _llm_holds_gpu(outdir):
                if stop.wait(poll):
                    break
                continue
            job = q.claim(root)
            if job is None:
                if stop.wait(poll):
                    break
                continue
            run_job(root, job, spawn=spawn, outdir=outdir)
            ran += 1
            q.pause_if_drained(root)

    return ran
