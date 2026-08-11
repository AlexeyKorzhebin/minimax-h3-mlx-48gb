"""Layout, durable writes, the queue lock, submission, and `scan` -- with no HTTP and no MLX.

Every lock/durable-write/submit/scan test here is written to fail loudly if the behavior it names
is missing, not just to pass when the behavior is present: a lock test that never actually contends
the lock, or a durable-write test that only checks "no temp file is left behind", passes just as
happily against code that dropped the feature. Each such test's docstring says what a naive,
feature-dropped implementation would still get right, and how this test tells the difference
anyway (see `test_the_queue_lock_actually_blocks_a_second_exclusive_holder` and
`test_the_operation_waits_for_the_queue_lock` in particular, which drive `flock` from a *separate
process* -- a thread in this one would silently re-acquire its own process's lock and prove
nothing).
"""
import contextlib
import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from h3_48gb import queue as q

#: Minimal dry-run report -- the only source `submit` ever takes `output_stem` from (see the
#: "three decisions" in `queue.py`'s `submit` docstring).
_DRY = {"output_stem": "/out/h3-a-896x576", "forwards": 7}


def _stem(report: dict, new_stem: str) -> dict:
    """A copy of a dry-run report with a different `output_stem`, for tests that need two jobs
    with distinct output names but otherwise identical reports.
    """
    return {**report, "output_stem": new_stem}


def _try(fn):
    """Call `fn`, returning either its result or the exception it raised. Used by tests that need
    to inspect what a background thread produced without re-raising and killing the test process.
    """
    try:
        return fn()
    except BaseException as exc:  # noqa: BLE001 -- deliberately catches everything, see above
        return exc


# -- Step 1: layout and durable writes ---------------------------------------------------------


def test_layout_creates_every_subdirectory(tmp_path):
    paths = q.layout(tmp_path / "queue")
    for name in ("pending", "running", "done", "failed", "leases", "results", "prompts", "logs"):
        assert paths[name].is_dir(), name


def test_durable_write_survives_a_crash_after_the_temp_file(tmp_path, monkeypatch):
    """Not "no temp file is left" -- that passes with a plain write_text. The invariant is that
    a crash mid-write leaves the OLD content, never a mixture.
    """
    target = tmp_path / "job.json"
    q.write_json_durably(target, {"v": 1})

    real_replace = os.replace
    def boom(src, dst):
        raise OSError("simulated crash between fsync and rename")
    monkeypatch.setattr(q.os, "replace", boom)
    with pytest.raises(OSError):
        q.write_json_durably(target, {"v": 2})
    monkeypatch.setattr(q.os, "replace", real_replace)

    assert json.loads(target.read_text()) == {"v": 1}, "a failed write corrupted the old content"
    assert list(tmp_path.iterdir()) == [target], "the temporary file was not cleaned up"


def test_durable_write_fsyncs_the_file_and_its_directory(tmp_path, monkeypatch):
    """Without the directory fsync the rename itself can be lost, and a job the user was told
    is queued is simply gone after a power cut.
    """
    synced: list[bool] = []
    real_fsync = os.fsync
    monkeypatch.setattr(q.os, "fsync", lambda fd: (synced.append(os.fstat(fd).st_mode), real_fsync(fd))[1])
    q.write_json_durably(tmp_path / "j.json", {"v": 1})
    assert len(synced) == 2, f"expected fsync of the file and of the directory, got {len(synced)}"
    # A count of two alone does not prove *which* two: fsyncing the temp file twice (e.g. by
    # accident, or by a change that dropped the directory fsync and duplicated the file one
    # instead) leaves this count unchanged. Check the actual kind of each fd fsync was called on.
    assert sorted(stat.S_ISDIR(mode) for mode in synced) == [False, True], (
        "expected exactly one fsync of a regular file and one of a directory")


# -- Step 4: the queue lock actually blocks ----------------------------------------------------


@contextlib.contextmanager
def _external_lock(root, mode):
    """Hold the queue lock from a *separate process*: flock is per-process, so a thread in this
    one would silently re-acquire its own lock and prove nothing.
    """
    script = ("import fcntl, os, sys, time\n"
              "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
              f"fcntl.flock(fd, fcntl.{mode})\n"
              "print('held', flush=True)\n"
              "sys.stdin.readline()\n")
    proc = subprocess.Popen([sys.executable, "-c", script, str(Path(root) / "queue.lock")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    try:
        yield
    finally:
        proc.stdin.write("\n"); proc.stdin.flush(); proc.wait(timeout=5)


def test_the_queue_lock_actually_blocks_a_second_exclusive_holder(tmp_path):
    root = tmp_path / "queue"; q.layout(root)
    with _external_lock(root, "LOCK_EX"):
        done = threading.Event()
        threading.Thread(target=lambda: (q.queue_lock(root, exclusive=True).__enter__(),
                                         done.set()), daemon=True).start()
        assert not done.wait(0.5), "queue_lock returned while another process held it exclusively"
    # `done` staying unset above is not, by itself, proof of blocking: a `queue_lock` gutted to
    # raise instead of acquiring the lock also never sets `done`, and would pass the assertion
    # above for the wrong reason. Only a genuine wait-then-succeed tells the two apart -- confirm
    # the background thread actually goes on to enter the lock once the external holder lets go.
    assert done.wait(5), "queue_lock never returned after the external lock was released"


# -- Step 6: submit and scan actually take the lock --------------------------------------------


@pytest.mark.parametrize("operation", ["submit", "scan"])
def test_the_operation_waits_for_the_queue_lock(tmp_path, operation):
    """Holding the lock externally must stall the operation. Without this, `submit` and `scan`
    could simply never call `queue_lock` and the rest of the suite would stay green.
    """
    root = tmp_path / "queue"; q.layout(root)
    calls = {"submit": lambda: q.submit(root, ["generate", "--tag", "a"], "", _DRY, {}),
             "scan": lambda: q.scan(root)}
    finished = threading.Event()
    with _external_lock(root, "LOCK_EX"):
        threading.Thread(target=lambda: (calls[operation](), finished.set()), daemon=True).start()
        assert not finished.wait(0.5), f"{operation} did not take the queue lock"
    assert finished.wait(5), f"{operation} never completed after the lock was released"


def test_submit_takes_an_exclusive_lock_not_a_shared_one(tmp_path):
    """The previous test proves *a* lock is taken, not which mode. Two `LOCK_SH` holders never
    conflict, so if `submit` ever took `LOCK_SH` instead of `LOCK_EX` -- e.g. `queue_lock`'s
    `exclusive` flag failing to reach `flock` -- a second, concurrent `submit` could pass the
    output_stem check before either finished writing, and both land the exact same name: precisely
    the race this lock exists to prevent (see the module docstring).
    """
    root = tmp_path / "queue"; q.layout(root)
    with _external_lock(root, "LOCK_SH"):
        finished = threading.Event()
        threading.Thread(target=lambda: (q.submit(root, ["generate", "--tag", "a"], "", _DRY, {}),
                                         finished.set()), daemon=True).start()
        assert not finished.wait(0.5), (
            "submit proceeded while an external LOCK_SH was held -- it must take LOCK_EX")
    assert finished.wait(5), "submit never completed after the external lock was released"


def test_scan_does_not_conflict_with_a_shared_external_lock(tmp_path):
    """The companion check to the test above: scan's own LOCK_SH must not be needlessly widened
    to LOCK_EX either, or a status read would start blocking on another concurrent status read.
    """
    root = tmp_path / "queue"; q.layout(root)
    with _external_lock(root, "LOCK_SH"):
        finished = threading.Event()
        threading.Thread(target=lambda: (q.scan(root), finished.set()), daemon=True).start()
        assert finished.wait(2), "scan waited on a second shared lock holder, which should never conflict"


# -- Step 7: submission -------------------------------------------------------------------------


def test_submit_snapshots_the_prompt_and_repoints_the_args_at_it(tmp_path):
    """The snapshot is why a prompt edited at midnight does not change a job queued at ten:
    the queued job runs the bytes the user read, not the file's later contents.
    """
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--prompt-file", "prompts/p.txt", "--tag", "a"],
                   "ночной", _DRY, {"seconds": 60},
                   prompt_source="prompts/p.txt", prompt_text="a centaur\n")
    snapshot = q.prompt_path(root, job.id)
    assert snapshot.read_text() == "a centaur\n"
    assert job.args[job.args.index("--prompt-file") + 1] == str(snapshot), (
        "the queued job still points at the shared prompts/ file")
    assert job.prompt_sha256 == hashlib.sha256(b"a centaur\n").hexdigest()
    assert job.output_stem == _DRY["output_stem"]


def test_submit_without_a_prompt_text_leaves_the_args_alone(tmp_path):
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "a dog", "--tag", "a"], "", _DRY, {})
    assert job.args == ["generate", "a dog", "--tag", "a"]
    assert job.prompt_sha256 is None
    assert not q.prompt_path(root, job.id).exists()


def test_two_submissions_in_the_same_second_retry_until_the_id_is_free(tmp_path, monkeypatch):
    """Random suffixes almost never collide, so a random-suffix test proves nothing about the
    retry. Force a collision: the first two draws are identical.
    """
    root = tmp_path / "queue"
    draws = iter(["aaaa", "aaaa", "bbbb"])
    monkeypatch.setattr(q, "_suffix", lambda: next(draws))
    frozen = lambda: "2026-08-11T13:05:00"
    a = q.submit(root, ["generate", "--tag", "c"], "", _DRY, {}, now=frozen)
    b = q.submit(root, ["generate", "--tag", "c"], "", _stem(_DRY, "/out/other"), {}, now=frozen)
    assert a.id.endswith("aaaa") and b.id.endswith("bbbb")
    assert q.job_path(root, a.id, "pending").exists()
    assert q.job_path(root, b.id, "pending").exists()


@pytest.mark.parametrize("suffix", [".mp4", ".wav", ".npz", ".json"])
def test_submit_refuses_when_any_artifact_of_that_stem_exists(tmp_path, suffix):
    """A leftover .wav means the name is taken just as surely as a leftover .mp4."""
    root = tmp_path / "queue"
    stem = tmp_path / "h3-a-896x576"
    (stem.parent / f"{stem.name}{suffix}").write_bytes(b"")
    with pytest.raises(q.OutputStemConflict):
        q.submit(root, ["generate", "--tag", "a"], "", _stem(_DRY, str(stem)), {})


def test_submit_refuses_a_stem_another_pending_job_already_claims(tmp_path):
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    with pytest.raises(q.OutputStemConflict):
        q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})


def test_submit_refuses_a_stem_a_running_job_already_claims(tmp_path):
    """`submit` never moves a job to `running/` on its own (that is `claim`, a later task), so this
    places one there by hand -- the point is that a two-hour-long generation is the one thing an
    artifact-suffix check on disk cannot see yet, so `running/` is the *only* thing standing between
    a second submission and clobbering that job's eventual output. A stem check that only looked at
    `pending/` would miss it entirely.
    """
    root = tmp_path / "queue"
    paths = q.layout(root)
    running_job = {
        "id": "20260811-000000-a-run1", "created_at": "2026-08-11T00:00:00",
        "args": ["generate", "--tag", "a"], "note": "", "prompt_source": None,
        "prompt_sha256": None, "output_stem": _DRY["output_stem"], "estimate": {},
        "priority": 0, "started_at": "2026-08-11T00:00:00", "finished_at": None,
        "exit_code": None, "log_tail": None,
    }
    q.write_json_durably(paths["running"] / "20260811-000000-a-run1.json", running_job)
    with pytest.raises(q.OutputStemConflict):
        q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})


def test_submit_writes_the_prompt_snapshot_durably(tmp_path, monkeypatch):
    """No existing test proves the prompt snapshot itself goes through `write_text_durably` rather
    than a plain `Path.write_text` -- `write_json_durably`'s own tests only ever exercise it on an
    arbitrary path, never on this specific call site. Counting `fsync` calls across a submit with a
    prompt catches a downgrade here: 2 for the snapshot, 2 for the job file, 4 total; a plain write
    for the snapshot would drop that to 2.
    """
    root = tmp_path / "queue"
    calls: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(q.os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd))[1])
    q.submit(root, ["generate", "--prompt-file", "p.txt", "--tag", "a"], "", _DRY, {},
             prompt_source="p.txt", prompt_text="hi\n")
    assert len(calls) == 4, (
        f"expected 2 fsyncs for the prompt snapshot and 2 for the job file, got {len(calls)}")


@pytest.mark.parametrize("bad_args", [
    ["generate", "a dog", "--tag", "a"],                     # no --prompt-file at all
    ["generate", "--tag", "a", "--prompt-file"],              # --prompt-file with no value after it
])
def test_submit_refuses_prompt_text_without_a_prompt_file_placeholder(tmp_path, bad_args):
    """A bare ValueError/IndexError used to leak out of submit's boundary here -- indistinguishable
    from a real bug to a caller matching on QueueError -- and left an empty placeholder job file
    (claimed via O_CREAT|O_EXCL before the mismatch was ever noticed) behind for every later scan
    to report as Broken forever. The refusal must be a QueueError, and it must happen before an id
    is ever claimed, so the queue is left exactly as empty as it was.
    """
    root = tmp_path / "queue"
    with pytest.raises(q.QueueError):
        q.submit(root, bad_args, "", _DRY, {}, prompt_text="hi\n")
    jobs, broken = q.scan(root)
    assert jobs == [] and broken == [], "a refused submit must leave no trace in the queue"


def test_submit_rolls_back_the_claimed_id_when_the_job_file_write_fails(tmp_path, monkeypatch):
    """Not every failure between the id claim and the final write is preventable by validating
    `args` up front -- a full disk or a crash inside `write_text_durably` can still land there. The
    id (an empty placeholder from O_CREAT|O_EXCL) and, if a prompt was already snapshotted, its now
    orphaned copy must both be removed rather than left for `scan` to report as Broken forever.
    """
    root = tmp_path / "queue"
    real_replace = os.replace
    call_count = {"n": 0}
    def boom(src, dst):
        call_count["n"] += 1
        if call_count["n"] == 2:  # 1st replace commits the prompt snapshot; 2nd is the job file
            raise OSError("simulated crash while writing the job file")
        return real_replace(src, dst)
    monkeypatch.setattr(q.os, "replace", boom)
    with pytest.raises(OSError):
        q.submit(root, ["generate", "--prompt-file", "p.txt", "--tag", "a"], "", _DRY, {},
                 prompt_source="p.txt", prompt_text="hi\n")
    monkeypatch.setattr(q.os, "replace", real_replace)

    assert list(q.layout(root)["pending"].iterdir()) == [], "the claimed placeholder must be rolled back"
    assert list(q.layout(root)["prompts"].iterdir()) == [], "the orphaned prompt snapshot must be rolled back"


def test_submitted_job_file_does_not_persist_the_redundant_state_field(tmp_path):
    """The directory a job's file lives in already says its state (see the module docstring); a
    duplicate `"state"` key inside the file itself would drift the moment a future rename-based
    transition (task 2) moves the file without rewriting it -- `cat running/<id>.json` would still
    read `"state": "pending"` forever, silently contradicting the very directory it sits in, exactly
    the kind of gap `write_json_durably`'s "meant to be read with cat" promise cannot survive.
    """
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    on_disk = json.loads(q.job_path(root, job.id, "pending").read_text())
    assert "state" not in on_disk, "the on-disk job file must not duplicate what the directory says"
    assert job.state == "pending", "the in-memory Job returned to the caller still carries it"


# -- Step 9: reading ------------------------------------------------------------------------------


def test_scan_returns_jobs_from_every_state_directory(tmp_path):
    """Placed by hand into all four state directories, not via `submit` (which only ever produces
    `pending/` jobs in this task) -- this is the test that proves `scan` actually looks at
    `running/`, `done/` and `failed/`, not just the one directory `submit` writes to.
    """
    root = tmp_path / "queue"
    paths = q.layout(root)
    for state in q.QUEUE_STATES:
        job_id = f"20260811-000000-a-{state[:4]}"
        # No "state" key here: a real on-disk job file does not carry one (see
        # test_submitted_job_file_does_not_persist_the_redundant_state_field) -- the directory is
        # the only thing that says it. _job_from_file must add it back from `state` alone.
        data = {
            "id": job_id, "created_at": "2026-08-11T00:00:00",
            "args": ["generate", "--tag", "a"], "note": "", "prompt_source": None,
            "prompt_sha256": None, "output_stem": f"/out/{job_id}", "estimate": {},
            "priority": 0, "started_at": None, "finished_at": None, "exit_code": None,
            "log_tail": None,
        }
        q.write_json_durably(paths[state] / f"{job_id}.json", data)

    jobs, broken = q.scan(root)
    assert broken == []
    assert len(jobs) == 4
    assert {job.state for job in jobs} == set(q.QUEUE_STATES), (
        "scan must read all four state directories, not just the one submit writes to")


def test_scan_reports_an_unreadable_job_instead_of_hiding_it(tmp_path):
    """A skipped bad file makes the queue silently shorter than the user remembers."""
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    (q.layout(root)["pending"] / "20260811-000000-junk-zzzz.json").write_text("{ not json")
    jobs, broken = q.scan(root)
    assert len(jobs) == 1 and len(broken) == 1
    assert "json" in broken[0].error.lower()


def test_scan_does_not_write_to_disk(tmp_path):
    """A read must have no side effects. `scan` used to call `layout(root)` internally, so a status
    check against a queue path that does not exist yet -- a typo'd H3_OUTDIR being the obvious
    real-world cause -- would silently create all eight subdirectories and forever after report an
    "empty queue" instead of anything a human could notice was wrong. A missing directory is an
    empty list, not a reason to create one, and a polled `/api/state` (task 5) must not touch the
    filesystem beyond the reads it actually needs.
    """
    root = tmp_path / "queue"
    jobs, broken = q.scan(root)
    assert jobs == [] and broken == []
    assert not root.exists(), "scan must not create the queue directory it was asked to read"


# -- Step 1: every mutator takes the queue lock --------------------------------------------------


@pytest.mark.parametrize("name", ["claim", "finish", "update", "move_to_front", "cancel"])
def test_every_mutator_waits_for_the_queue_lock(tmp_path, name):
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    if name == "finish":
        q.claim(root)
    ops = {
        "claim": lambda: q.claim(root),
        "finish": lambda: q.finish(root, job.id, 0, "ok"),
        "update": lambda: q.update(root, job.id, ["generate", "--tag", "a"], "", _DRY, {}),
        "move_to_front": lambda: q.move_to_front(root, job.id),
        "cancel": lambda: q.cancel(root, job.id),
    }
    finished = threading.Event()
    with _external_lock(root, "LOCK_EX"):
        threading.Thread(target=lambda: (ops[name](), finished.set()), daemon=True).start()
        assert not finished.wait(0.5), f"{name} did not take the queue lock"
    assert finished.wait(5), f"{name} never completed after the lock was released"


# -- Step 3: claim order and finish ----------------------------------------------------------------


def test_claim_takes_the_oldest_first_and_records_started_at(tmp_path):
    root = tmp_path / "queue"
    first = q.submit(root, ["generate", "--tag", "a"], "", _DRY, {},
                     now=lambda: "2026-08-11T13:00:00")
    q.submit(root, ["generate", "--tag", "b"], "", _stem(_DRY, "/out/b"), {},
             now=lambda: "2026-08-11T13:05:00")
    taken = q.claim(root, now=lambda: "2026-08-11T14:00:00")
    assert taken.id == first.id
    assert taken.started_at == "2026-08-11T14:00:00"
    assert taken.state == "running"
    assert not q.job_path(root, first.id, "pending").exists()
    assert q.job_path(root, first.id, "running").exists()


def test_claim_returns_none_when_pending_is_empty(tmp_path):
    root = tmp_path / "queue"
    q.layout(root)
    assert q.claim(root) is None


def test_a_job_moved_to_the_front_is_claimed_before_older_ones(tmp_path):
    root = tmp_path / "queue"
    first = q.submit(root, ["generate", "--tag", "a"], "", _DRY, {},
                     now=lambda: "2026-08-11T13:00:00")
    second = q.submit(root, ["generate", "--tag", "b"], "", _stem(_DRY, "/out/b"), {},
                      now=lambda: "2026-08-11T13:05:00")
    moved = q.move_to_front(root, second.id)
    assert moved.priority == 1
    taken = q.claim(root)
    assert taken.id == second.id, "the job moved to the front must be claimed before the older one"
    assert taken.priority == 1


def test_finish_routes_by_exit_code_and_keeps_a_supplied_finished_at(tmp_path):
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    job = q.claim(root)
    failed = q.finish(root, job.id, 137, "Killed", finished_at="2026-08-11T15:00:00")
    assert failed.state == "failed" and failed.exit_code == 137
    assert failed.finished_at == "2026-08-11T15:00:00"
    assert failed.log_tail == "Killed"
    assert not q.job_path(root, job.id, "running").exists()
    assert q.job_path(root, job.id, "failed").exists()


def test_finish_with_a_zero_exit_code_goes_to_done(tmp_path):
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    job = q.claim(root)
    ok = q.finish(root, job.id, 0, "all good")
    assert ok.state == "done" and ok.exit_code == 0
    assert q.job_path(root, job.id, "done").exists()
    assert not q.job_path(root, job.id, "running").exists()


def test_finish_raises_a_queue_error_when_the_job_is_not_running(tmp_path):
    """Not `JobNotPending` -- that exception is specifically for the three `pending/`-only
    mutators. `finish` operates on `running/`, and this is a caller bug, not a normal refusal --
    but it still must be a `QueueError`, never a bare `FileNotFoundError`/`KeyError`.
    """
    root = tmp_path / "queue"
    q.layout(root)
    with pytest.raises(q.QueueError):
        q.finish(root, "no-such-job", 0, "")


# -- Step 4: update and its boundaries -------------------------------------------------------------


def test_update_replaces_content_and_keeps_identity(tmp_path):
    """id, created_at and priority are references other things hang off: the log, the lease,
    the snapshot, the queue order the user just set."""
    root = tmp_path / "queue"
    # NOTE: the brief's snippet for this test omits `--prompt-file` from `args`, which `submit`
    # (task 1) requires whenever `prompt_text` is given -- see
    # test_submit_refuses_prompt_text_without_a_prompt_file_placeholder. Added here; every other
    # assertion is unchanged from the brief. `now=` is also pinned to a date nowhere near the real
    # clock: without it, a mutation that has `update` overwrite `created_at` with a fresh `_now()`
    # call can coincidentally match `job.created_at` (both land in the same real second, since
    # `_now()` only has second resolution) and the identity assertion below would not catch it.
    job = q.submit(root, ["generate", "--prompt-file", "p.txt", "--tag", "a"], "старая", _DRY,
                   {"seconds": 60}, prompt_source="prompts/p.txt", prompt_text="one",
                   now=lambda: "2020-01-01T00:00:00")
    q.move_to_front(root, job.id)
    edited = q.update(root, job.id, ["generate", "--prompt-file", "p.txt", "--tag", "a",
                                      "--seed", "2"], "новая",
                      _DRY, {"seconds": 90}, prompt_source="prompts/p.txt", prompt_text="two")
    assert (edited.id, edited.created_at) == (job.id, job.created_at)
    assert edited.priority == 1
    assert edited.note == "новая" and edited.estimate == {"seconds": 90}
    assert q.prompt_path(root, job.id).read_text() == "two"
    assert edited.args[edited.args.index("--prompt-file") + 1] == str(q.prompt_path(root, job.id))


def test_update_without_new_prompt_text_leaves_the_existing_snapshot_untouched(tmp_path):
    """`update` mirrors `submit`: nothing under `prompts/` is touched unless `prompt_text` is
    actually given -- an edit that only changes, say, `--seed` must not silently blank out the
    prompt snapshot or its recorded hash.
    """
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--prompt-file", "p.txt", "--tag", "a"], "", _DRY, {},
                   prompt_source="prompts/p.txt", prompt_text="unchanged\n")
    edited = q.update(root, job.id, ["generate", "--prompt-file", "p.txt", "--tag", "a",
                                      "--seed", "9"], "", _DRY, {})
    assert edited.prompt_source == job.prompt_source
    assert edited.prompt_sha256 == job.prompt_sha256
    assert q.prompt_path(root, job.id).read_text() == "unchanged\n"


def test_update_refuses_a_stem_conflict_with_another_pending_job(tmp_path):
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    other = q.submit(root, ["generate", "--tag", "b"], "", _stem(_DRY, "/out/other"), {})
    with pytest.raises(q.OutputStemConflict):
        q.update(root, job.id, ["generate", "--tag", "a"], "", _stem(_DRY, "/out/other"), {})
    # the collision check must not have touched the other job or corrupted this one
    assert q.job_path(root, other.id, "pending").exists()
    assert json.loads(q.job_path(root, job.id, "pending").read_text())["note"] == ""


def test_editing_a_claimed_job_raises_and_leaves_running_untouched(tmp_path):
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    q.claim(root)
    for call in (lambda: q.update(root, job.id, ["generate", "--tag", "a"], "", _DRY, {}),
                 lambda: q.move_to_front(root, job.id),
                 lambda: q.cancel(root, job.id)):
        with pytest.raises(q.JobNotPending):
            call()
    assert q.job_path(root, job.id, "running").exists()
    assert not q.job_path(root, job.id, "pending").exists()


def test_cancel_removes_the_job_and_its_prompt_snapshot(tmp_path):
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--prompt-file", "p.txt", "--tag", "a"], "", _DRY, {},
                   prompt_source="p.txt", prompt_text="hi\n")
    cancelled = q.cancel(root, job.id)
    assert cancelled.id == job.id
    assert not q.job_path(root, job.id, "pending").exists()
    assert not q.prompt_path(root, job.id).exists()
    jobs, broken = q.scan(root)
    assert jobs == [] and broken == [], "a cancelled job must leave no trace anywhere in the queue"


def test_move_to_front_on_the_only_pending_job_still_raises_its_priority(tmp_path):
    """`_max_pending_priority` must include the job being moved itself in the max it computes --
    excluding it would let repeated `move_to_front` calls on a lone job leave its priority
    unchanged (max of "everyone else" is 0 forever) instead of monotonically increasing.
    """
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    first = q.move_to_front(root, job.id)
    second = q.move_to_front(root, job.id)
    assert first.priority == 1
    assert second.priority == 2, "moving the same lone job to the front twice must keep raising it"


# -- Step 6: update racing claim, forced deterministically -----------------------------------------


def test_update_racing_claim_leaves_one_state_and_consistent_content(tmp_path, monkeypatch):
    """Force the dangerous interleaving instead of hoping for it: `update` is paused after it
    has read the job and before it writes it back, and the worker claims during the pause."""
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--tag", "a"], "старая", _DRY, {})

    paused = threading.Event()
    resume = threading.Event()
    real_write = q.write_json_durably

    def slow_write(path, payload):
        if paused.is_set():
            return real_write(path, payload)
        paused.set()
        assert resume.wait(5)
        return real_write(path, payload)

    monkeypatch.setattr(q, "write_json_durably", slow_write)
    outcome: dict = {}
    editor = threading.Thread(
        target=lambda: outcome.update(
            result=_try(lambda: q.update(root, job.id, ["generate", "--tag", "a"], "новая",
                                         _DRY, {}))))
    editor.start()
    assert paused.wait(5)
    claimed = _try(lambda: q.claim(root))
    resume.set()
    editor.join(10)

    present = [s for s in q.QUEUE_STATES if q.job_path(root, job.id, s).exists()]
    assert len(present) == 1, f"job exists in {present} at once"
    if present == ["pending"]:
        assert json.loads(q.job_path(root, job.id, "pending").read_text())["note"] == "новая", (
            "update won the race but the file holds the old content")
    else:
        assert isinstance(outcome["result"], q.JobNotPending)
    # Whichever side won, `claim` itself must not have raised or silently returned nothing when a
    # job genuinely was there to take.
    assert claimed is not None and not isinstance(claimed, BaseException)


def test_update_first_lock_acquisition_blocks_before_any_write_is_attempted(tmp_path, monkeypatch):
    """`update` takes the queue lock twice (see its docstring): once to validate, once -- after an
    unlocked write -- to verify. The combined test above and the parametrized one in step 1 only
    prove that *some* acquisition inside `update` blocks; held against a single, long external
    lock, the second acquisition alone would make either test pass even if the first never took
    the lock at all. This isolates the first: an external holder must block `update` before it
    ever reaches its write, not just block it somewhere eventually because the second acquisition
    happens to pick up the slack.
    """
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    reached_write = threading.Event()
    real_write = q.write_json_durably

    def mark_and_write(path, payload):
        reached_write.set()
        return real_write(path, payload)

    monkeypatch.setattr(q, "write_json_durably", mark_and_write)
    with _external_lock(root, "LOCK_EX"):
        threading.Thread(target=lambda: q.update(
            root, job.id, ["generate", "--tag", "a"], "новая", _DRY, {}), daemon=True).start()
        assert not reached_write.wait(0.5), (
            "update reached its write before the external lock was released -- its first "
            "(validating) lock acquisition did not wait")
    assert reached_write.wait(5), "update never reached its write after the lock was released"


def test_update_second_lock_acquisition_also_waits_for_the_queue_lock(tmp_path, monkeypatch):
    """The other half of the isolation above: pause `update` right where the race test pauses it
    (after its unlocked write is handed to `write_json_durably`), grab the queue lock externally
    during that pause -- exactly where the post-write verification would need it -- and confirm
    `update` does not finish until that external lock is released. Without this, the verification
    that deletes a stale duplicate and raises `JobNotPending` (see `update`'s docstring) could run
    concurrently with another mutator instead of after it.
    """
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})

    paused = threading.Event()
    resume = threading.Event()
    real_write = q.write_json_durably

    def slow_write(path, payload):
        if paused.is_set():
            return real_write(path, payload)
        paused.set()
        assert resume.wait(5)
        return real_write(path, payload)

    monkeypatch.setattr(q, "write_json_durably", slow_write)
    finished = threading.Event()
    editor = threading.Thread(
        target=lambda: (q.update(root, job.id, ["generate", "--tag", "a"], "новая", _DRY, {}),
                        finished.set()))
    editor.start()
    assert paused.wait(5)
    with _external_lock(root, "LOCK_EX"):
        resume.set()
        assert not finished.wait(0.5), (
            "update's post-write verification did not wait for the queue lock")
    assert finished.wait(5), "update never completed after the external lock was released"
    editor.join(5)


# -- Global constraint: queue.py must stay importable without MLX -------------------------------


def test_queue_module_does_not_import_mlx():
    """`h3_48gb.queue` is imported by both the worker and the web server on every request; loading
    it must never pull in the 33B-parameter transformer stack. Run in a subprocess so this test's
    own process (which may have already imported mlx via another test module) cannot hide a leak.
    """
    code = "import sys; import h3_48gb.queue; print('mlx' in sys.modules)"
    project_root = Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            cwd=str(project_root))
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    assert result.stdout.strip() == "False", "importing h3_48gb.queue must not import mlx"
