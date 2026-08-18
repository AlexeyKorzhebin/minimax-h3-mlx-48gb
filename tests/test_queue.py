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
import re
import shutil
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


#: A job's own output subdirectory, exactly the shape `submit` produces: `YYYYMMDD-HHMM-<slug>`.
#: Used to assert on it without hard-coding `submit`'s internal choice of separators.
_SUBDIR_RE = re.compile(r"^\d{8}-\d{4}-[a-z0-9-]+$")


def _assert_relocated(output_stem: str, base, filename: str, tag: str | None) -> None:
    """Assert that `output_stem` is `<base>/<a fresh job subdirectory ending in tag's slug>/<filename>`
    -- what `submit` (task A6) is supposed to produce, checked independently of
    `queue._relocate_to_job_subdir`'s own implementation rather than by calling it, so a test using
    this cannot pass merely because it agrees with itself.
    """
    stem = Path(output_stem)
    assert stem.name == filename, f"the filename must survive relocation unchanged: {stem.name!r}"
    assert stem.parent.parent == Path(base), (
        f"expected one fresh subdirectory directly under {base}, got {stem.parent.parent}")
    assert _SUBDIR_RE.fullmatch(stem.parent.name), (
        f"the job subdirectory {stem.parent.name!r} does not match YYYYMMDD-HHMM-<slug>")
    assert stem.parent.name.endswith(f"-{q._slug(tag)}"), (
        f"the job subdirectory {stem.parent.name!r} must end with the tag's own slug")


def _predicted_stem(root, output_stem: str, tag: str, created_at: str) -> str:
    """Where `submit` will actually place `output_stem`, given a job whose `--tag` is `tag` and
    whose clock answers `created_at` -- for tests that need to plant a conflicting artifact, or a
    hand-written job file, at the exact path a real submission would collide with.
    """
    _, relocated = q._relocate_to_job_subdir(root, ["generate", "--tag", tag], output_stem,
                                             created_at)
    return relocated


def _try(fn):
    """Call `fn`, returning either its result or the exception it raised. Used by tests that need
    to inspect what a background thread produced without re-raising and killing the test process.
    """
    try:
        return fn()
    except BaseException as exc:  # noqa: BLE001 -- deliberately catches everything, see above
        return exc


def _answer_within(seconds, fn):
    """`fn()`, but failing the test if it has not answered within `seconds`.

    Several things here are contractually *non-blocking*: the lease probe (`lease_is_free` runs
    under the queue lock and must never wait on a lease), and the worker lock (a second worker must
    refuse, not queue up behind the first for hours). Both degrade into a *blocking* call under a
    one-token mutation -- dropping `LOCK_NB` -- and a blocking call does not make a test red, it
    makes the suite hang, which is not a test result at all. Running the call on a daemon thread
    and asserting it finished converts "never answered" into a real failure. Whatever `fn` raised
    is re-raised here, so `pytest.raises` around this still works.
    """
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.update(result=_try(fn)), daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), (
        f"the call did not answer within {seconds}s -- it blocked where it must not")
    result = outcome["result"]
    if isinstance(result, BaseException):
        raise result
    return result


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
def _external_lock(root, mode, name="queue.lock"):
    """Hold one of the queue's lock files from a *separate process*: flock is per-process, so a
    thread in this one would silently re-acquire its own lock and prove nothing.

    `name` is relative to `root`, so the same helper stands in for another worker holding
    `worker.lock` and for another run holding `leases/<id>.lock` -- both are the same `flock` on
    the same kind of file, and both are only honest when the holder is a different process. It
    must be an existing directory's file: nothing here creates `leases/`, so a caller passing a
    lease path has `layout` (or `submit`) behind it.
    """
    script = ("import fcntl, os, sys, time\n"
              "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
              f"fcntl.flock(fd, fcntl.{mode})\n"
              "print('held', flush=True)\n"
              "sys.stdin.readline()\n")
    proc = subprocess.Popen([sys.executable, "-c", script, str(Path(root) / name)],
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


# -- Task A6: every job gets its own output subdirectory ----------------------------------------


def test_submit_puts_outdir_in_args_as_a_stamped_slug_subdirectory(tmp_path):
    """The brief's own example: a job tagged `kot-italy` gets `--outdir <outdir>/<stamp>-kot-italy`
    in its stored `args`, not the flat `<outdir>` the caller (the web form) actually sent.
    """
    root = tmp_path / "queue"
    frozen = lambda: "2026-08-13T14:35:00"
    job = q.submit(root, ["generate", "--tag", "kot-italy", "--outdir", "/out"], "",
                   {"output_stem": "/out/h3-kot-italy-896x576"}, {}, now=frozen)
    assert "--outdir" in job.args
    assert job.args[job.args.index("--outdir") + 1] == "/out/20260813-1435-kot-italy", (
        f"expected the minute-stamped, slugged subdirectory in args, got {job.args}")
    assert job.output_stem == "/out/20260813-1435-kot-italy/h3-kot-italy-896x576"


def test_submit_adds_outdir_when_args_names_none(tmp_path):
    """`args` with no `--outdir` at all (a caller that let the CLI default it) still gets one --
    `submit` reads the directory to nest under from the dry-run report's own `output_stem`, the
    only place that value exists when `args` itself does not carry it.
    """
    root = tmp_path / "queue"
    frozen = lambda: "2026-08-13T14:35:00"
    job = q.submit(root, ["generate", "--tag", "b"], "",
                   {"output_stem": "/out/h3-b-896x576"}, {}, now=frozen)
    assert job.args[-2:] == ["--outdir", "/out/20260813-1435-b"]


def test_submit_rewrites_the_inline_outdir_spelling(tmp_path):
    """`--outdir=value`, argparse's other accepted spelling, must be rewritten in place -- not
    left stale while a second, separate `--outdir value` token is appended after it."""
    root = tmp_path / "queue"
    frozen = lambda: "2026-08-13T14:35:00"
    job = q.submit(root, ["generate", "--tag", "b", "--outdir=/out"], "",
                   {"output_stem": "/out/h3-b-896x576"}, {}, now=frozen)
    assert job.args == ["generate", "--tag", "b", "--outdir=/out/20260813-1435-b"]


def test_submit_rewrites_only_the_last_of_two_outdir_tokens(tmp_path):
    """`check_path_flags` (`web.py`) rewrites every occurrence of a repeated flag without removing
    any, so `submit` can see `--outdir` twice. Only the *last* one is what argparse will actually
    use -- and it is the only one this module may safely edit; an earlier, dead one must survive
    untouched so a test asserting on it (`test_the_job_stores_the_resolved_paths_not_what_the_
    browser_sent` in `test_web.py`) still finds the plain, unrelocated value there.
    """
    root = tmp_path / "queue"
    frozen = lambda: "2026-08-13T14:35:00"
    job = q.submit(root, ["generate", "--tag", "b", "--outdir", "/dead", "--outdir", "/out"], "",
                   {"output_stem": "/out/h3-b-896x576"}, {}, now=frozen)
    assert job.args == ["generate", "--tag", "b", "--outdir", "/dead",
                        "--outdir", "/out/20260813-1435-b"]


def test_duplicating_a_relocated_job_gets_a_sibling_subdirectory_not_a_nested_one(tmp_path):
    """The scenario `_duplicate_job` (`web.py`) actually creates: it resubmits the source job's own
    `args`, whose `--outdir` this module already relocated once. A `submit` that blindly appended a
    fresh subdirectory onto whatever `--outdir` already said would nest the copy one level inside
    the source's subdirectory instead of beside it -- worse with every duplicate of a duplicate.
    """
    root = tmp_path / "queue"
    frozen_a = lambda: "2026-08-13T14:35:00"
    source = q.submit(root, ["generate", "--tag", "a"], "",
                      {"output_stem": "/out/h3-a-896x576"}, {}, now=frozen_a)
    source_dir = Path(source.output_stem).parent
    assert source_dir == Path("/out/20260813-1435-a")

    # `_duplicate_job` reuses `source.args` (already carrying `--outdir <source_dir>`) with only
    # `--tag` rewritten, and an `output_stem` in the same directory with the new tag spliced in --
    # exactly what `_duplicate_tag_candidates` builds.
    frozen_b = lambda: "2026-08-13T14:40:00"
    duplicate_args = ["generate", "--tag", "a-copy", "--outdir", str(source_dir)]
    duplicate = q.submit(root, duplicate_args, "",
                         {"output_stem": str(source_dir / "h3-a-copy-896x576")}, {}, now=frozen_b)
    duplicate_dir = Path(duplicate.output_stem).parent

    assert duplicate_dir != source_dir, "the duplicate must not land in the source's own directory"
    assert duplicate_dir.parent == source_dir.parent, (
        f"the duplicate's subdirectory must be a *sibling* of the source's, not nested inside it "
        f"-- got {duplicate_dir} under {duplicate_dir.parent}, source under {source_dir.parent}")
    assert duplicate_dir == Path("/out/20260813-1440-a-copy")


def test_a_user_outdir_that_merely_looks_like_a_job_subdirectory_is_not_stripped(tmp_path):
    """BACKLOG "UX-мелочи", task 7. `_base_outdir` used to strip *any* `--outdir` whose last
    component matched `queue._JOB_SUBDIR_RE` (`YYYYMMDD-HHMM-<slug>`), on the theory that only
    `_relocate_to_job_subdir` ever produces that shape. That theory breaks the moment a human types
    (or the web form's `defaultOutdir()` pre-fills, from a *previous* job's own already-relocated
    `--outdir`) a directory that merely happens to look that way but was never actually used by any
    job this queue knows about -- a fresh, stand-alone destination the human means literally.

    The fix asks the queue's own records, not the string: nothing has ever been submitted to `root`
    with `output_stem` inside `/out/20260101-0000-myproject`, so it must not be treated as an
    existing job's own subdirectory and stripped up a level -- the new job's subdirectory must nest
    *inside* it, exactly as it would inside any other outdir.
    """
    root = tmp_path / "queue"
    user_dir = Path("/out/20260101-0000-myproject")
    frozen = lambda: "2026-08-13T14:35:00"
    job = q.submit(root, ["generate", "--tag", "a", "--outdir", str(user_dir)], "",
                  {"output_stem": str(user_dir / "h3-a-896x576")}, {}, now=frozen)
    job_dir = Path(job.output_stem).parent
    assert job_dir.parent == user_dir, (
        f"a user's own directory that merely looks like a job subdirectory must not be stripped -- "
        f"the new job's subdirectory should nest inside {user_dir}, got {job_dir}")
    assert job_dir != user_dir, "the job must still get its own fresh subdirectory, not write flat"


def test_relocate_to_job_subdir_without_the_slug_is_wrong(tmp_path):
    """Mutation check: a version of `_relocate_to_job_subdir` that dropped the slug (stamp alone,
    e.g. because someone "simplified" `subdir = _dir_stamp(created_at)`) would still pass every
    test above that submits one job at a time. This is the one that would not: two jobs queued in
    the same minute under different tags must not collide on the *directory*, only ever on the
    exact same `output_stem` if they also share a tag.
    """
    root = tmp_path / "queue"
    stamp_only = "20260813-1435"  # what a slug-less implementation would produce
    args_a, stem_a = q._relocate_to_job_subdir(root, ["generate", "--tag", "a"],
                                                "/out/h3-a-896x576", "2026-08-13T14:35:00")
    args_b, stem_b = q._relocate_to_job_subdir(root, ["generate", "--tag", "b"],
                                                "/out/h3-b-896x576", "2026-08-13T14:35:00")
    assert Path(stem_a).parent != Path(stem_b).parent, (
        "two different tags in the same minute must not share a subdirectory")
    assert Path(stem_a).parent.name != stamp_only and Path(stem_b).parent.name != stamp_only, (
        "the subdirectory must carry the tag's slug, not just the timestamp")


def test_stem_taken_is_unaffected_by_relocation(tmp_path):
    """`_stem_taken` itself does not know about subdirectories -- it never needs to, since
    `submit` (task A6) already resolves `output_stem` to its final, relocated form before calling
    it. This pins that `_stem_taken` still finds a collision when given two paths that really are
    the same, subdirectory included, guarding against a future edit that started passing it the
    pre-relocation stem instead.
    """
    root = tmp_path / "queue"
    q.layout(root)
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "h3-a-896x576.mp4").write_bytes(b"")
    assert q._stem_taken(root, str(tmp_path / "run" / "h3-a-896x576")) is True
    assert q._stem_taken(root, str(tmp_path / "run" / "h3-b-896x576")) is False


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
    _assert_relocated(job.output_stem, "/out", "h3-a-896x576", "a")


def test_submit_without_a_prompt_text_leaves_the_args_alone(tmp_path):
    """"Alone" now excludes `--outdir`: `submit` still appends the job's own subdirectory (task
    A6) when the caller's `args` name none, exactly as it would if `--outdir` had been present and
    needed rewriting. Everything else about `args` -- and everything about the prompt -- is
    untouched.
    """
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "a dog", "--tag", "a"], "", _DRY, {})
    assert job.args[:4] == ["generate", "a dog", "--tag", "a"]
    assert job.args[4:] == ["--outdir", str(Path(job.output_stem).parent)]
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
    """A leftover .wav means the name is taken just as surely as a leftover .mp4.

    The artifact is planted at the *relocated* stem -- `submit` (task A6) checks the path the run
    will actually write, subdirectory included, not the flat one `dry_run_report` names. `now=` is
    pinned so the planted path and the one `submit` computes for real agree.
    """
    root = tmp_path / "queue"
    frozen = lambda: "2026-08-11T13:05:00"
    flat_stem = str(tmp_path / "h3-a-896x576")
    relocated = _predicted_stem(root, flat_stem, "a", frozen())
    Path(relocated).parent.mkdir(parents=True, exist_ok=True)
    Path(f"{relocated}{suffix}").write_bytes(b"")
    with pytest.raises(q.OutputStemConflict):
        q.submit(root, ["generate", "--tag", "a"], "", _stem(_DRY, flat_stem), {}, now=frozen)


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
    frozen = lambda: "2026-08-11T00:00:00"
    running_job = {
        "id": "20260811-000000-a-run1", "created_at": "2026-08-11T00:00:00",
        "args": ["generate", "--tag", "a"], "note": "", "prompt_source": None,
        "prompt_sha256": None,
        "output_stem": _predicted_stem(root, _DRY["output_stem"], "a", frozen()),
        "estimate": {},
        "priority": 0, "started_at": "2026-08-11T00:00:00", "finished_at": None,
        "exit_code": None, "log_tail": None,
    }
    q.write_json_durably(paths["running"] / "20260811-000000-a-run1.json", running_job)
    with pytest.raises(q.OutputStemConflict):
        q.submit(root, ["generate", "--tag", "a"], "", _DRY, {}, now=frozen)


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
    """`update` never relocates `--outdir` itself (task A6's rule is `submit`-only), so the
    conflict it must catch is against `other`'s real, already-relocated `output_stem` -- not the
    flat one `_stem(_DRY, "/out/other")` on its own would name.
    """
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    other = q.submit(root, ["generate", "--tag", "b"], "", _stem(_DRY, "/out/other"), {})
    with pytest.raises(q.OutputStemConflict):
        q.update(root, job.id, ["generate", "--tag", "a"], "",
                 {"output_stem": other.output_stem}, {})
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


def test_update_and_claim_are_serialized_end_to_end(tmp_path, monkeypatch):
    """`update` and `claim` share the queue lock, and the lock is held for `update`'s entire
    critical section -- validate, prompt snapshot, final write -- not released partway through.
    So a `claim` attempted while `update` is mid-edit must not run at all until `update` releases
    the lock: serialization, not "eventually converges to something consistent", is the actual
    property the lock provides.

    Forced deterministically, not hoped for: `write_json_durably` is paused the first time
    `update` calls it (i.e. inside its single locked section, committing the new content). While
    paused, a second thread starts a `claim` and must *not* finish within a short window -- if it
    does, `update`'s lock is not covering its write and the two operations ran concurrently. Only
    after the pause is released may `claim` proceed, and by then `update` has already committed:
    `claim` must take the *edited* job, not the stale one.

    A two-lock-acquisition version of `update` (validate under one lock, write unlocked, verify
    under a second) cannot pass this: `claim` would run to completion during the pause instead of
    waiting for it, immediately failing the "must not finish yet" assertion below.
    """
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

    claim_finished = threading.Event()
    claim_outcome: dict = {}
    claimer = threading.Thread(
        target=lambda: (claim_outcome.update(result=_try(lambda: q.claim(root))),
                        claim_finished.set()))
    claimer.start()
    assert not claim_finished.wait(0.5), (
        "claim ran to completion while update was mid-edit: not serialized")

    resume.set()
    editor.join(10)
    claimer.join(10)

    assert isinstance(outcome["result"], q.Job), f"update must have succeeded: {outcome['result']!r}"
    present = [s for s in q.QUEUE_STATES if q.job_path(root, job.id, s).exists()]
    assert present == ["running"], f"job exists in {present}, expected exactly ['running']"
    on_disk = json.loads(q.job_path(root, job.id, "running").read_text())
    assert on_disk["note"] == "новая", (
        "claim must have taken update's committed (edited) content, not a stale copy")
    assert not isinstance(claim_outcome["result"], BaseException) and claim_outcome["result"] is not None
    assert claim_outcome["result"].note == "новая"


def test_update_takes_the_queue_lock_exactly_once(tmp_path, monkeypatch):
    """The race test above forces one specific interleaving and checks its outcome is safe -- its
    real discriminator is "the final `write_json_durably` runs under the lock", not "there is
    exactly one acquisition". A version that validates under one acquisition, writes the prompt
    snapshot unlocked, and writes the job file under a *second* acquisition passes every
    functional test here, including the race test (nothing in this suite forces a pause between
    the two acquisitions the way the race test forces one around the single write) -- while
    reopening precisely the corruption C1 exists to prevent: a job resurrected in `pending/` after
    `claim` has already moved it to `running/`. Assert the actual invariant directly instead of
    only its downstream symptom: `update` must acquire `queue_lock` exactly once.
    """
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--prompt-file", "p.txt", "--tag", "a"], "", _DRY, {},
                   prompt_source="p.txt", prompt_text="one\n")
    acquisitions: list[bool] = []
    real_queue_lock = q.queue_lock

    @contextlib.contextmanager
    def counting_queue_lock(root_arg, exclusive):
        acquisitions.append(exclusive)
        with real_queue_lock(root_arg, exclusive):
            yield

    monkeypatch.setattr(q, "queue_lock", counting_queue_lock)
    q.update(root, job.id, ["generate", "--prompt-file", "p.txt", "--tag", "a"], "новая", _DRY, {},
             prompt_source="p.txt", prompt_text="two\n")

    assert acquisitions == [True], (
        f"update must take the queue lock exactly once, exclusively; took {acquisitions}")


# -- Review circle 1: state transition is a rename, not write+unlink ----------------------------


def test_claim_moves_the_job_with_a_single_rename_not_write_then_unlink(tmp_path, monkeypatch):
    """The design spec requires the pending->running transition to be one `os.rename` (atomic
    within a filesystem), not a durable write to the new path followed by unlinking the old one:
    two operations leave a real window -- a crash between them, or an `unlink` that never reaches
    disk -- where the job exists in both directories *after a reboot*, not just during a race.
    Spy on both `os.rename` and `Path.unlink` around the one call under test: a regression to
    write-then-unlink would still pass every functional test above while failing this one.
    """
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    renamed, unlinked = [], []
    real_rename = os.rename
    real_unlink = Path.unlink
    monkeypatch.setattr(q.os, "rename", lambda src, dst: (renamed.append((str(src), str(dst))),
                                                           real_rename(src, dst))[1])
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (unlinked.append(str(self)),
                                                                real_unlink(self, *a, **k))[1])

    claimed = q.claim(root)

    assert renamed, "claim must move the job with os.rename"
    assert not unlinked, f"claim must not unlink anything -- it should rename instead: {unlinked}"
    assert claimed is not None


def test_finish_moves_the_job_with_a_single_rename_not_write_then_unlink(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    job = q.claim(root)  # before monkeypatching, so its own rename/unlink calls aren't counted
    renamed, unlinked = [], []
    real_rename = os.rename
    real_unlink = Path.unlink
    monkeypatch.setattr(q.os, "rename", lambda src, dst: (renamed.append((str(src), str(dst))),
                                                           real_rename(src, dst))[1])
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (unlinked.append(str(self)),
                                                                real_unlink(self, *a, **k))[1])

    finished = q.finish(root, job.id, 0, "ok")

    assert renamed, "finish must move the job with os.rename"
    assert not unlinked, f"finish must not unlink anything -- it should rename instead: {unlinked}"
    assert finished.state == "done"


def test_claim_fsyncs_both_directories_the_rename_touches(tmp_path, monkeypatch):
    """The rename's own durability requires fsyncing *both* directories whose listing it changes
    -- `pending/` (an entry disappeared) and `running/` (one appeared) -- *after* the rename, not
    merely at some point during `claim`. `write_json_durably` already fsyncs `pending/` earlier,
    while committing `started_at` into the not-yet-moved file -- a version of `_rename_durably`
    that fsyncs only `dest.parent` would still make `pending/` appear in a naive "was it fsynced
    at all" check, purely because of that earlier, unrelated fsync. Recording the *order* of
    events (rename, then which directories get fsynced) is what actually distinguishes the two:
    only a fsync recorded strictly after the `rename` event proves the rename's own effect on that
    directory's listing was made durable. Recover each fsync'd fd's path via the `os.open` call
    that produced it (macOS has no `/proc/self/fd`), the same way
    `test_durable_write_fsyncs_the_file_and_its_directory` distinguishes file syncs from directory
    ones by kind, not just by count.
    """
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    paths_by_fd: dict[int, str] = {}
    real_open, real_fsync, real_rename = os.open, os.fsync, os.rename
    events: list[tuple] = []

    def spy_open(path, *a, **k):
        fd = real_open(path, *a, **k)
        paths_by_fd[fd] = str(path)
        return fd

    def spy_fsync(fd):
        path = paths_by_fd.get(fd)
        if path is not None and Path(path).is_dir():
            events.append(("fsync", path))
        return real_fsync(fd)

    def spy_rename(src, dst):
        events.append(("rename", str(src), str(dst)))
        return real_rename(src, dst)

    monkeypatch.setattr(q.os, "open", spy_open)
    monkeypatch.setattr(q.os, "fsync", spy_fsync)
    monkeypatch.setattr(q.os, "rename", spy_rename)

    q.claim(root)

    rename_at = next(i for i, e in enumerate(events) if e[0] == "rename")
    fsyncs_after_rename = [e[1] for e in events[rename_at + 1:] if e[0] == "fsync"]
    assert str(root / "pending") in fsyncs_after_rename, (
        "pending/'s directory entry, changed by the rename, must itself be fsynced afterward")
    assert str(root / "running") in fsyncs_after_rename, (
        "running/'s directory entry, changed by the rename, must itself be fsynced afterward")


# -- Review circle 1: bare exceptions must not escape as ValueError/TypeError/KeyError -----------


@pytest.mark.parametrize("op", [
    lambda root, job_id: q.update(root, job_id, ["generate", "--tag", "a"], "", _DRY, {}),
    lambda root, job_id: q.move_to_front(root, job_id),
    lambda root, job_id: q.cancel(root, job_id),
])
def test_pending_mutators_raise_queue_error_on_a_corrupt_pending_file(tmp_path, op):
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    q.job_path(root, job.id, "pending").write_text("{ not json")
    with pytest.raises(q.QueueError):
        op(root, job.id)


def test_finish_raises_queue_error_on_a_corrupt_running_file(tmp_path):
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    job = q.claim(root)
    q.job_path(root, job.id, "running").write_text("{ not json")
    with pytest.raises(q.QueueError):
        q.finish(root, job.id, 0, "ok")


def test_update_raises_queue_error_on_a_pending_file_with_an_extra_field(tmp_path):
    """A shape mismatch -- not just unparseable JSON -- must also come out as `QueueError`, not a
    bare `TypeError` from `Job(**data)`.
    """
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    data = json.loads(q.job_path(root, job.id, "pending").read_text())
    data["unexpected_field"] = "surprise"
    q.write_json_durably(q.job_path(root, job.id, "pending"), data)
    with pytest.raises(q.QueueError):
        q.update(root, job.id, ["generate", "--tag", "a"], "", _DRY, {})


def test_claim_skips_an_unreadable_file_and_still_claims_a_good_one(tmp_path):
    """One broken file in `pending/` must not jam every valid job behind it -- `scan` is the place
    that surfaces a broken file; `claim` just needs to move past it.
    """
    root = tmp_path / "queue"
    good = q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    (q.layout(root)["pending"] / "20260811-000000-junk-zzzz.json").write_text("{ not json")
    claimed = q.claim(root)
    assert claimed.id == good.id


def test_claim_skips_a_pending_file_with_a_shape_mismatch(tmp_path):
    """Valid JSON that does not match `Job`'s shape (an extra field here) must be skipped the same
    way unparseable JSON is -- not raised, and not silently claimed with a `TypeError` waiting to
    happen at the end of the function.
    """
    root = tmp_path / "queue"
    good = q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    paths = q.layout(root)
    bad = {
        "id": "20260811-000000-bad-zzzz", "created_at": "2026-08-11T00:00:00",
        "args": ["generate"], "note": "", "prompt_source": None, "prompt_sha256": None,
        "output_stem": "/out/bad", "estimate": {}, "priority": 0, "started_at": None,
        "finished_at": None, "exit_code": None, "log_tail": None, "unexpected_field": True,
    }
    q.write_json_durably(paths["pending"] / "20260811-000000-bad-zzzz.json", bad)
    claimed = q.claim(root)
    assert claimed.id == good.id
    assert q.job_path(root, "20260811-000000-bad-zzzz", "pending").exists(), (
        "a shape-mismatched file must be left alone, not claimed or destroyed")


# -- Review circle 1: durability and the on-disk shape must be tested for every mutator, not -----
# -- only for submit -------------------------------------------------------------------------------


def test_update_writes_the_prompt_snapshot_durably(tmp_path, monkeypatch):
    """Mirrors `test_submit_writes_the_prompt_snapshot_durably`: `submit`'s snapshot durability
    was proven by counting fsyncs, but nothing proved `update`'s rewrite of the *same* snapshot
    goes through `write_text_durably` rather than a plain `Path.write_text`.
    """
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--prompt-file", "p.txt", "--tag", "a"], "", _DRY, {},
                   prompt_source="p.txt", prompt_text="one\n")
    calls: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(q.os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd))[1])
    q.update(root, job.id, ["generate", "--prompt-file", "p.txt", "--tag", "a"], "", _DRY, {},
             prompt_source="p.txt", prompt_text="two\n")
    assert len(calls) == 4, (
        f"expected 2 fsyncs for the rewritten prompt snapshot and 2 for the job file, got {len(calls)}")


def test_update_recomputes_prompt_sha256_for_the_new_text(tmp_path):
    """A hash that stays at the old value after an edit lies about what the snapshot now
    contains -- exactly the kind of drift the field exists to prevent.
    """
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--prompt-file", "p.txt", "--tag", "a"], "", _DRY, {},
                   prompt_source="p.txt", prompt_text="one\n")
    edited = q.update(root, job.id, ["generate", "--prompt-file", "p.txt", "--tag", "a"], "",
                      _DRY, {}, prompt_source="p.txt", prompt_text="two\n")
    assert edited.prompt_sha256 == hashlib.sha256(b"two\n").hexdigest()
    assert edited.prompt_sha256 != job.prompt_sha256


def test_updated_job_file_does_not_persist_the_redundant_state_field(tmp_path):
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    q.update(root, job.id, ["generate", "--tag", "a"], "новая", _DRY, {})
    on_disk = json.loads(q.job_path(root, job.id, "pending").read_text())
    assert "state" not in on_disk


def test_finished_job_file_does_not_persist_the_redundant_state_field(tmp_path):
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    job = q.claim(root)
    q.finish(root, job.id, 0, "ok")
    on_disk = json.loads(q.job_path(root, job.id, "done").read_text())
    assert "state" not in on_disk


def test_claimed_job_file_does_not_persist_the_redundant_state_field(tmp_path):
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    job = q.claim(root)
    on_disk = json.loads(q.job_path(root, job.id, "running").read_text())
    assert "state" not in on_disk


# -- Review circle 1: update must carry forward the lease trail, not zero it ---------------------


def test_update_preserves_started_at_left_by_a_reconciled_job(tmp_path):
    """A job task 4's reconciliation returns to `pending/` after an interrupted run keeps its
    original `started_at` (the run resumes from its checkpoint, not from zero). `update` touching
    unrelated fields (`note` here) must not silently erase that trace just because it also
    rewrites the file.
    """
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    data = json.loads(q.job_path(root, job.id, "pending").read_text())
    data["started_at"] = "2026-08-11T10:00:00"
    q.write_json_durably(q.job_path(root, job.id, "pending"), data)

    edited = q.update(root, job.id, ["generate", "--tag", "a"], "новая", _DRY, {})
    assert edited.started_at == "2026-08-11T10:00:00"


# -- Review circle 1: mutators must not recreate a missing queue layout --------------------------


@pytest.mark.parametrize("name", ["claim", "finish", "update", "move_to_front", "cancel"])
def test_mutators_do_not_recreate_missing_queue_subdirectories(tmp_path, name):
    """`layout(root)` unconditionally creates all eight subdirectories. Task 1 removed the call
    from `scan` for the same reason this removes it from every mutator here: a job can only exist
    to be claimed/finished/edited/moved/cancelled because `submit` already ran `layout`, so there
    is nothing legitimate left for these to create -- and calling it anyway risks silently
    rebuilding a typo'd `H3_OUTDIR/queue` path into existence. `leases/` and `results/` are
    untouched by every operation under test, so their disappearance after each call is the signal.
    """
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    if name == "finish":
        q.claim(root)
    shutil.rmtree(root / "leases")
    shutil.rmtree(root / "results")
    ops = {
        "claim": lambda: q.claim(root),
        "finish": lambda: q.finish(root, job.id, 0, "ok"),
        "update": lambda: q.update(root, job.id, ["generate", "--tag", "a"], "", _DRY, {}),
        "move_to_front": lambda: q.move_to_front(root, job.id),
        "cancel": lambda: q.cancel(root, job.id),
    }
    ops[name]()
    assert not (root / "leases").exists(), "leases/ must not be recreated"
    assert not (root / "results").exists(), "results/ must not be recreated"


# -- Review circle 2: the content write before a rename must itself be durable -------------------


def test_claim_writes_the_updated_content_durably(tmp_path, monkeypatch):
    """`claim` writes `started_at` into the *pending* copy before `_rename_durably` moves it (see
    `claim`'s docstring) -- a step separate from, and before, the rename's own two directory
    fsyncs. Nothing so far proved that write itself goes through `write_json_durably` rather than
    a plain `Path.write_text`: `test_claim_fsyncs_both_directories_the_rename_touches` only counts
    fsyncs recorded *after* the `os.rename` event, so a downgrade to a plain write for the content
    itself passed the whole suite clean before this test existed. After a crash right there,
    `running/<id>.json` could be truncated or simply missing bytes.
    """
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    calls: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(q.os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd))[1])
    q.claim(root)
    # 2 fsyncs for the content write (write_json_durably: the file itself, then pending/) + 2 more
    # for the rename (_rename_durably fsyncs pending/ and running/) = 4 total.
    assert len(calls) == 4, (
        f"expected 2 fsyncs for the content write and 2 for the rename, got {len(calls)}")


def test_finish_writes_the_updated_content_durably(tmp_path, monkeypatch):
    """The same gap, the same fix, the second instance: `finish` writes `finished_at`/`exit_code`/
    `log_tail` into the *running* copy before renaming it into `done/`/`failed/`.
    """
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _DRY, {})
    job = q.claim(root)
    calls: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(q.os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd))[1])
    q.finish(root, job.id, 0, "ok")
    assert len(calls) == 4, (
        f"expected 2 fsyncs for the content write and 2 for the rename, got {len(calls)}")


def test_rename_durably_refuses_to_overwrite_an_existing_destination(tmp_path):
    """`os.rename` silently replaces an existing destination on POSIX. Every caller here only ever
    targets a path a correctly functioning queue does not already have an entry at (`claim` into
    `running/`, `finish` into `done/`/`failed/`); finding one anyway is a desync worth surfacing
    loudly -- a duplicate id, or a previous crash mid-transition -- not silently clobbering.
    """
    root = tmp_path / "queue"
    q.layout(root)
    src = root / "pending" / "a.json"
    dest = root / "running" / "a.json"
    q.write_json_durably(src, {"v": "new"})
    q.write_json_durably(dest, {"v": "stale"})

    with pytest.raises(q.QueueError):
        q._rename_durably(src, dest)

    assert json.loads(dest.read_text()) == {"v": "stale"}, (
        "the pre-existing destination must survive untouched")
    assert src.exists(), "the source must not be consumed by a rename that was refused"


# -- Task 4, step 1: reconciliation, every row of the table --------------------------------------


def _claimed(root, stem, tag="a"):
    """A job sitting in `running/`, put there the way the worker puts it there -- `submit` then
    `claim` -- so it carries a real `started_at` and a real `output_stem` under `tmp_path`.
    """
    q.submit(root, ["generate", "--tag", tag], "", _stem(_DRY, str(stem)), {})
    return q.claim(root)


def test_lease_is_free_answers_free_held_and_unknown(tmp_path):
    """The three answers are not decoration: `reconcile` finishes or re-queues a job on `True`
    and `None`, and leaves it strictly alone on `False`. A probe that could only say "free"
    would let the worker re-queue a job that is running right now and start a second 36 GB
    process on top of it.
    """
    root = tmp_path / "queue"
    q.layout(root)
    assert q.lease_is_free(root, "no-such-job") is True, "an absent lease file is nobody holding it"
    assert not q.lease_path(root, "no-such-job").exists(), (
        "asking whether a lease is held must not create the lease file")

    q.lease_path(root, "probe").touch()
    assert q.lease_is_free(root, "probe") is True, "an existing but unheld lease file is free"
    with _external_lock(root, "LOCK_EX", name="leases/probe.lock"):
        # Through `_answer_within`, not called directly: the probe must be non-blocking, and a
        # blocking one would hang the suite here rather than fail it -- see `_answer_within`.
        assert _answer_within(5, lambda: q.lease_is_free(root, "probe")) is False, (
            "a lease held by another process is held")
    assert q.lease_is_free(root, "probe") is True, "the lease is free again once the holder exits"

    # Not a contrived failure: any OSError that is not "no such file" lands here. A directory
    # where the lock file belongs is the portable way to produce one without being root.
    q.lease_path(root, "weird").mkdir()
    assert q.lease_is_free(root, "weird") is None, (
        "an unanswerable probe must say so, not guess in either direction")


def test_the_result_marker_is_written_durably(tmp_path, monkeypatch):
    """The marker is the only record of a run's exit code between the subprocess exiting and the
    job leaving `running/`, and the crash it is written for is exactly the kind that can lose an
    unsynced write. A plain `write_text` here would leave a truncated or absent marker after a
    power cut and reconciliation would call a finished run interrupted.
    """
    root = tmp_path / "queue"
    q.layout(root)
    calls: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(q.os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd))[1])
    q.write_result_marker(root, "20260811-000000-a-aaaa", 0, "2020-01-01T00:00:00")
    assert len(calls) == 2, (
        f"expected an fsync of the marker and one of results/, got {len(calls)}")


def test_reconcile_leaves_a_live_job_alone_and_reports_it_alive(tmp_path):
    """Row 1. The whole point of the lease: while another process holds it, that job is generating
    right now, and touching its file -- finishing it, re-queueing it -- destroys a run in flight.
    Two-sided, because "reconcile did nothing" is also what a `reconcile` that crashed instantly
    looks like: once the holder lets go, the very same call must act on the job.
    """
    root = tmp_path / "queue"
    job = _claimed(root, tmp_path / "h3-a-1x1")

    with _external_lock(root, "LOCK_EX", name=f"leases/{job.id}.lock"):
        held = q.reconcile(root)
        assert [j.id for j in held.alive] == [job.id]
        assert held.changed == [], "a live job must not be touched"
        assert q.job_path(root, job.id, "running").exists(), "a live job stays in running/"

    freed = q.reconcile(root)
    assert freed.alive == [], "the lease was released, so the job is no longer alive"
    assert [j.id for j in freed.changed] == [job.id], (
        "once the lease is free the same reconcile must act on the job -- otherwise the test above "
        "proves nothing about the lease and everything about reconcile doing nothing at all")


@pytest.mark.parametrize("exit_code,state", [(0, "done"), (137, "failed")])
def test_reconcile_finishes_a_free_job_by_its_result_marker(tmp_path, exit_code, state):
    """Row 2. The marker is written before the lease is released, so it is the only thing that
    still knows how the run ended once the worker is gone -- including `finished_at`, which must
    come from when the run actually exited and not from whenever the machine came back up.
    """
    root = tmp_path / "queue"
    job = _claimed(root, tmp_path / "h3-a-1x1")
    q.log_path(root, job.id).write_text("шаг 7\nготово\n")
    q.write_result_marker(root, job.id, exit_code, "2020-01-01T00:00:00")

    result = q.reconcile(root)

    assert [j.id for j in result.changed] == [job.id] and result.alive == []
    assert q.job_path(root, job.id, state).exists()
    assert not q.job_path(root, job.id, "running").exists()
    landed = result.changed[0]
    assert landed.state == state and landed.exit_code == exit_code
    assert landed.finished_at == "2020-01-01T00:00:00", (
        "finished_at must be restored from the marker, not invented at reconciliation time")
    assert "готово" in landed.log_tail, "the run's log tail must survive into the finished job"


def test_reconcile_treats_an_mp4_without_a_marker_as_success(tmp_path):
    """Row 3, the window between the subprocess exiting and the marker landing. A finished `.mp4`
    exists only after a fully successful run, so it is a sounder signal than any timeout -- and
    re-running the job would overwrite exactly the result being rescued.
    """
    root = tmp_path / "queue"
    job = _claimed(root, tmp_path / "h3-a-1x1")
    Path(job.output_stem).parent.mkdir(parents=True, exist_ok=True)
    Path(f"{job.output_stem}.mp4").write_bytes(b"video")

    result = q.reconcile(root)

    assert q.job_path(root, job.id, "done").exists(), "a finished .mp4 means the run succeeded"
    assert result.changed[0].exit_code == 0
    assert result.changed[0].log_tail == q.RESULT_RECOVERED_NOTE, (
        "the job must say why it was called done without a marker")


def test_reconcile_believes_the_marker_over_the_artifact_when_both_exist(tmp_path):
    """The order of rows 2 and 3 is load-bearing, and no single-row test can catch a swap: with
    only a marker, or only an `.mp4`, either order produces the same outcome. A failed run whose
    stem already carries an `.mp4` -- an earlier attempt, or a partial rewrite -- is where they
    diverge: checking the artifact first files a failure under `done/` with exit code 0, and the
    non-zero exit code the marker knows about is lost for good.
    """
    root = tmp_path / "queue"
    job = _claimed(root, tmp_path / "h3-a-1x1")
    Path(job.output_stem).parent.mkdir(parents=True, exist_ok=True)
    Path(f"{job.output_stem}.mp4").write_bytes(b"video from an earlier attempt")
    q.write_result_marker(root, job.id, 137, "2020-01-01T00:00:00")

    q.reconcile(root)

    assert q.job_path(root, job.id, "failed").exists(), (
        "the marker knows the exit code was 137; the .mp4 only knows a file exists")
    assert not q.job_path(root, job.id, "done").exists()


def test_reconcile_returns_an_interrupted_job_to_pending_keeping_started_at(tmp_path):
    """Row 4. Nothing survived the crash, so the job goes back to the queue -- and keeps the
    `started_at` `claim` stamped, because the rerun resumes from its checkpoint rather than
    starting from zero, and that first start is still the truth about the job.
    """
    root = tmp_path / "queue"
    job = _claimed(root, tmp_path / "h3-a-1x1")

    result = q.reconcile(root)

    assert q.job_path(root, job.id, "pending").exists()
    assert not q.job_path(root, job.id, "running").exists()
    returned = result.changed[0]
    assert returned.state == "pending"
    assert returned.started_at == job.started_at, "the interrupted run's start time must survive"
    assert returned.exit_code is None and returned.finished_at is None, (
        "a re-queued job was never finished; stamping it would make the page lie")
    assert q.claim(root).id == job.id, "a job returned to pending/ must be claimable again"


def test_reconcile_treats_an_unanswerable_lease_probe_as_free(tmp_path):
    """Row 5. The worker holds the single worker lock, so nothing in `running/` can be one of
    ours; leaving a job there because its lock file could not be opened would jam the queue on
    that job for ever, and the recovery cost of the other choice is one resumed run.
    """
    root = tmp_path / "queue"
    job = _claimed(root, tmp_path / "h3-a-1x1")
    q.lease_path(root, job.id).mkdir()
    assert q.lease_is_free(root, job.id) is None

    result = q.reconcile(root)

    assert result.alive == [], "an unknown lease must not be reported as a live job"
    assert q.job_path(root, job.id, "pending").exists()


def test_reconcile_steps_over_a_corrupt_running_file_and_names_it(tmp_path):
    """One unreadable file in `running/` must not stop every other job from being recovered, and
    it must not disappear either: a queue that is silently one job shorter than the human
    remembers is the exact failure `scan`'s `Broken` list exists to prevent, and reconciliation
    owes the same answer.
    """
    root = tmp_path / "queue"
    job = _claimed(root, tmp_path / "h3-a-1x1")
    (q.layout(root)["running"] / "20260811-000000-junk-zzzz.json").write_text("{ not json")

    result = q.reconcile(root)

    assert [j.id for j in result.changed] == [job.id]
    assert [Path(b.path).stem for b in result.conflicted] == ["20260811-000000-junk-zzzz"]
    assert "json" in result.conflicted[0].error.lower()
    assert q.job_path(root, "20260811-000000-junk-zzzz", "running").exists(), (
        "the corrupt file must be left where it is, not moved or destroyed")


def test_a_desync_is_reported_and_does_not_stop_the_rest_of_the_recovery(tmp_path):
    """A job that exists in both `running/` and `pending/` -- a duplicate id, a crash caught
    mid-transition -- makes `_rename_durably` refuse, correctly. What must not follow is the
    refusal escaping `reconcile`: it runs at the top of *every* worker iteration, so an exception
    here does not fail one job, it kills the worker, and then kills the next `h3 worker` on the
    same file, and the queue stays dead until someone shuffles directories by hand.

    Two jobs, one of them wedged: the healthy one must still be recovered, and the wedged one must
    come back in `conflicted` rather than vanish -- a silently shorter queue is the failure mode
    `scan`'s `Broken` list already exists to prevent.
    """
    root = tmp_path / "queue"
    wedged = _claimed(root, tmp_path / "h3-a-1x1", tag="a")
    healthy = _claimed(root, tmp_path / "h3-b-1x1", tag="b")
    # The desync itself: `running/<id>.json` is live, and a stale copy already sits at the path
    # reconciliation would move it back to.
    shutil.copyfile(q.job_path(root, wedged.id, "running"),
                    q.job_path(root, wedged.id, "pending"))

    result = q.reconcile(root)

    assert [j.id for j in result.changed] == [healthy.id], (
        "one wedged job must not stop every other job from being recovered")
    assert [Path(b.path).stem for b in result.conflicted] == [wedged.id]
    assert "refusing to overwrite" in result.conflicted[0].error
    assert q.job_path(root, wedged.id, "running").exists(), (
        "the wedged job must be left exactly where it was, for a human to look at")

    # And the worker survives it -- not once, but on every pass, which is the actual claim.
    again = q.reconcile(root)
    assert [Path(b.path).stem for b in again.conflicted] == [wedged.id]


def test_reconcile_on_a_queue_that_does_not_exist_is_empty_not_an_error(tmp_path):
    """`scan` already answers "no directory" with "no jobs" rather than an exception, and the HTTP
    layer that will call both should not have to tell a missing queue apart from an empty one.
    Without this, `queue_lock`'s `os.open` raises a bare `FileNotFoundError` -- not even a
    `QueueError` -- from inside a request handler.
    """
    result = q.reconcile(tmp_path / "no-such-queue")
    assert result.changed == [] and result.alive == [] and result.conflicted == []
    assert not (tmp_path / "no-such-queue").exists(), (
        "reconciling a queue that does not exist must not create one")


class _CountingFile:
    """A file wrapper that records how many bytes were handed out, whichever way they were read --
    `read`, `readline` or iteration. Counting only `read` would miss a `deque(stream)` walk, which
    is exactly the implementation this is here to rule out.
    """

    def __init__(self, handle, counted):
        self._handle = handle
        self._counted = counted

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *exc):
        return self._handle.__exit__(*exc)

    def __iter__(self):
        return self

    def __next__(self):
        chunk = next(self._handle)
        self._counted.append(len(chunk))
        return chunk

    def read(self, *a):
        chunk = self._handle.read(*a)
        self._counted.append(len(chunk))
        return chunk

    def readline(self, *a):
        chunk = self._handle.readline(*a)
        self._counted.append(len(chunk))
        return chunk

    def seek(self, *a):
        return self._handle.seek(*a)

    def tell(self):
        return self._handle.tell()


def test_read_log_tail_seeks_instead_of_walking_the_whole_log(tmp_path, monkeypatch):
    """`reconcile` calls this inside its exclusive critical section, and the design spec promises
    the queue lock is held for milliseconds. A bounded `deque` over the whole file keeps memory
    flat but still pays the full read -- tens of megabytes for a run that prints a line per
    forward -- so every page load would wait behind that walk. Counting the bytes actually
    delivered is what tells "constant memory" and "constant work" apart; asserting on the returned
    text alone cannot, since both implementations return the same forty lines.
    """
    root = tmp_path / "queue"
    q.layout(root)
    log = q.log_path(root, "j")
    log.write_bytes(b"".join(b"line %d\n" % i for i in range(200_000)))
    size = log.stat().st_size
    assert size > 1_000_000, "the log has to be big enough for the difference to mean something"

    counted: list[int] = []
    real_open = open
    monkeypatch.setattr(q, "open",
                        lambda *a, **kw: _CountingFile(real_open(*a, **kw), counted),
                        raising=False)

    tail = q.read_log_tail(root, "j")

    assert tail.splitlines() == [f"line {i}" for i in range(199_960, 200_000)], (
        "the tail must be the last forty lines, in order")
    assert sum(counted) < size / 10, (
        f"read {sum(counted)} of {size} bytes to fetch forty lines -- the whole log was walked")


def test_read_log_tail_handles_a_log_shorter_than_one_chunk(tmp_path):
    """The backwards walk must stop at the start of the file rather than seeking past it, and a
    log with fewer than `lines` lines is the common case for a run that died early.
    """
    root = tmp_path / "queue"
    q.layout(root)
    q.log_path(root, "j").write_text("шаг 1\nшаг 2\n")
    assert q.read_log_tail(root, "j") == "шаг 1\nшаг 2\n"
    assert q.read_log_tail(root, "j", lines=1) == "шаг 2\n"
    assert q.read_log_tail(root, "missing") == "", "no log at all is an empty tail, not an error"


def test_read_log_tail_does_not_return_a_half_line_from_the_chunk_boundary(tmp_path):
    """Reading backwards means the buffer usually starts in the middle of a line, and the loop has
    to fetch one newline *more* than it needs so that partial line can be thrown away. Stopping at
    exactly `lines` newlines -- the natural off-by-one -- returns that half line as the first line
    of the tail, which is a log entry that never existed.

    Invisible at ordinary sizes: a chunk of a progress log holds hundreds of newlines, so both
    versions stop after one read and slice the same forty lines out of it. It only shows when the
    number of lines asked for is exactly what one backwards chunk provides, so that is computed
    here from the chunk size rather than guessed, and the lines are fixed-width so the arithmetic
    is exact.
    """
    root = tmp_path / "queue"
    q.layout(root)
    width = 100  # bytes per line, newline included
    chunk = q._TAIL_CHUNK_BYTES
    assert chunk % width, (
        "this test needs the chunk size not to be a multiple of the line width, or the boundary "
        f"lands between lines and proves nothing -- pick another width for chunk={chunk}")
    wanted = -(-chunk // width)  # newlines contained in one backwards chunk
    count = wanted * 3
    q.log_path(root, "j").write_bytes(
        b"".join(f"{i:0{width - 1}d}\n".encode() for i in range(count)))

    tail = q.read_log_tail(root, "j", lines=wanted)

    assert tail.splitlines() == [f"{i:0{width - 1}d}" for i in range(count - wanted, count)], (
        "the first line of the tail was cut in half at the chunk boundary")


def test_read_log_tail_keeps_the_last_line_when_it_has_no_newline(tmp_path):
    """A run killed by `SIGKILL` mid-print leaves the last line unterminated, and that line is
    usually the interesting one.
    """
    root = tmp_path / "queue"
    q.layout(root)
    q.log_path(root, "j").write_text("шаг 1\nоборвано")
    assert q.read_log_tail(root, "j", lines=1) == "оборвано"


def test_reconcile_waits_for_the_queue_lock(tmp_path):
    """`reconcile` rewrites queue state -- it finishes jobs and moves them back to `pending/` --
    so it takes the same exclusive lock every other mutator takes. Two-sided: a `reconcile` that
    simply raised would also leave the event unset.
    """
    root = tmp_path / "queue"
    _claimed(root, tmp_path / "h3-a-1x1")
    finished = threading.Event()
    with _external_lock(root, "LOCK_EX"):
        threading.Thread(target=lambda: (q.reconcile(root), finished.set()), daemon=True).start()
        assert not finished.wait(0.5), "reconcile did not take the queue lock"
    assert finished.wait(5), "reconcile never completed after the lock was released"


def test_reconcile_takes_the_queue_lock_exactly_once_for_the_whole_table(tmp_path, monkeypatch):
    """The critical section must cover the whole table, not one job or one decision at a time.
    Task 2 paid for this twice: in the gap between two acquisitions a cancelled job came back to
    life and the file of a job that was running got overwritten. A per-job or read-then-write
    version passes every functional test above -- three jobs still end up in the right
    directories -- while reopening exactly that gap, so assert the invariant itself.
    """
    root = tmp_path / "queue"
    for tag in ("a", "b", "c"):
        job = _claimed(root, tmp_path / f"h3-{tag}-1x1", tag=tag)
        q.write_result_marker(root, job.id, 0, "2020-01-01T00:00:00")

    acquisitions: list[bool] = []
    real_queue_lock = q.queue_lock

    @contextlib.contextmanager
    def counting_queue_lock(root_arg, exclusive):
        acquisitions.append(exclusive)
        with real_queue_lock(root_arg, exclusive):
            yield

    monkeypatch.setattr(q, "queue_lock", counting_queue_lock)
    result = q.reconcile(root)

    assert len(result.changed) == 3
    assert acquisitions == [True], (
        f"reconcile must take the queue lock exactly once, exclusively; took {acquisitions}")


# -- Task A5: pause/start the queue ---------------------------------------------------------------


def test_a_missing_paused_marker_reads_as_not_paused(tmp_path):
    """The safe direction, per `is_paused`'s docstring: losing the marker resumes the queue rather
    than freezing it silently. Checked against a `root` that does not even exist yet, the strongest
    version of "missing" -- `is_paused` must not need `layout` to have run first.
    """
    root = tmp_path / "queue"
    assert q.is_paused(root) is False


def test_is_paused_reads_as_not_paused_when_the_marker_cannot_even_be_stat_ed(tmp_path,
                                                                              monkeypatch):
    """BACKLOG "UX-мелочи", task 2. `Path.exists` only swallows the errnos that mean "there is
    nothing here" (`ENOENT` and friends); a directory this process cannot `stat` at all --
    permissions changed under it, a network mount gone away -- raises `PermissionError` straight
    through instead of returning `False`. `is_paused`'s own docstring already promises the safe
    direction ("a missing marker means not paused") for exactly this class of failure, so the
    exception must not reach `main_loop` -- it must read the same as a missing marker.
    """
    def broken_exists(self):
        raise PermissionError("no permission to stat this")

    monkeypatch.setattr(Path, "exists", broken_exists)
    assert q.is_paused(tmp_path / "queue") is False


def test_set_paused_and_is_paused_round_trip(tmp_path):
    """The pair on a queue that already exists, independent of whatever `layout` set the marker to
    when it created the directory (see the next two tests for that half).
    """
    root = tmp_path / "queue"
    q.layout(root)

    q.set_paused(root, True)
    assert q.is_paused(root) is True
    assert (root / q.PAUSED_MARKER_NAME).exists()

    q.set_paused(root, False)
    assert q.is_paused(root) is False
    assert not (root / q.PAUSED_MARKER_NAME).exists()

    # Idempotent in both directions -- the web routes call this on every click, and a double click
    # (a slow response, an impatient human) must not raise.
    q.set_paused(root, False)
    assert q.is_paused(root) is False
    q.set_paused(root, True)
    q.set_paused(root, True)
    assert q.is_paused(root) is True


def test_layout_pauses_a_freshly_created_queue(tmp_path):
    """A queue nobody has looked at yet must not start running jobs unattended -- see `layout`'s
    docstring for why the marker is created there rather than left for the worker or the page to
    add on their own first touch.
    """
    root = tmp_path / "queue"
    assert not root.exists()

    q.layout(root)

    assert q.is_paused(root) is True


def test_layout_does_not_repause_an_existing_queue(tmp_path):
    """The other half, and the one a one-line mutation (always touching the marker in `layout`,
    not only on first creation) breaks: `main_loop` calls `layout(root)` on every worker startup
    (see its own docstring), and a worker restarted after a human resumed the queue must not
    silently pause it again out from under them.
    """
    root = tmp_path / "queue"
    q.layout(root)
    assert q.is_paused(root) is True, "sanity: the first layout call must have paused it"
    q.set_paused(root, False)  # a human, or the page's «начать расчёт» button, resumes it

    q.layout(root)  # e.g. `main_loop`'s own unconditional call on every startup

    assert q.is_paused(root) is False, (
        "layout on an already-existing queue root must leave the pause marker exactly as it found "
        "it -- recreating it would pause the queue on every worker restart")


def test_pause_if_drained_pauses_only_when_nothing_is_left_to_claim(tmp_path):
    """«Опустела» здесь значит ровно то же, что значит `claim`, вернувший `None`, — и проверять это
    надо тем же взглядом на каталог, а не отдельной прикидкой рядом.

    Отсюда и сломанный файл в третьей части: `claim` его пропускает и отдаёт `None`, значит и
    «опустела» обязана считать очередь пустой. Иначе нечитаемый огрызок в `pending/` навсегда
    отменяет автопаузу — молча, и ровно в той очереди, где что-то уже пошло не так.
    """
    root = tmp_path / "queue"
    q.submit(root, ["generate", "--tag", "a"], "", _stem(_DRY, str(tmp_path / "h3-a-1x1")), {})
    q.set_paused(root, False)

    assert q.pause_if_drained(root) is False, "с ждущей задачей пауза не ставится"
    assert q.is_paused(root) is False

    job = q.claim(root)
    q.finish(root, job.id, 0, "")
    assert q.pause_if_drained(root) is True, "без ждущих задач маркер обязан встать"
    assert q.is_paused(root) is True

    # Сломанный файл — не задача: `claim` его пропускает, значит очередь всё ещё пуста.
    q.set_paused(root, False)
    (root / "pending" / "broken.json").write_text("{не json", encoding="utf-8")
    assert q.pause_if_drained(root) is True, (
        "нечитаемый огрызок в pending/ не должен отменять автопаузу — `claim` его не возьмёт")


def test_pause_if_drained_does_not_deadlock_against_the_queue_lock(tmp_path):
    """`queue_lock` не реентрантный: два захвата в одном процессе — это висящий насмерть работник,
    а не упавший тест. Проверяется тем, что вызов вообще возвращается в отведённое время.
    """
    root = tmp_path / "queue"
    q.layout(root)
    assert _answer_within(5, lambda: q.pause_if_drained(root)) is True


# -- Step: `Job.kind` (task 3, "Проекты") ---------------------------------------------------------


def test_submit_defaults_kind_to_generate(tmp_path):
    """A caller that never heard of `kind` (every caller before task 3) gets exactly the behavior
    it always got: a `KIND_GENERATE` job.
    """
    root = tmp_path / "queue"
    job = q.submit(root, ["generate", "--tag", "a"], "", _stem(_DRY, str(tmp_path / "h3-a-1x1")),
                   {})
    assert job.kind == q.KIND_GENERATE


def test_submit_accepts_a_song_kind_and_leaves_its_args_untouched(tmp_path):
    """A `kind="song"` job's `args` is `["song", "--project", <path>]` (task 3 brief, verbatim) --
    `submit` must not graft an `--outdir` token onto it the way it does for `KIND_GENERATE`
    (`_relocate_to_job_subdir`), because the worker's song dispatch parses exactly this shape and
    nothing else.
    """
    root = tmp_path / "queue"
    project_path = tmp_path / "projects" / "20260818-1200-my-song" / "project.json"
    args = ["song", "--project", str(project_path)]
    job = q.submit(root, args, "", {"output_stem": str(project_path.parent / "track" / "song")},
                   {}, kind=q.KIND_SONG)

    assert job.kind == q.KIND_SONG
    assert job.args == args, "song args must pass through unrelocated"
    assert job.output_stem == str(project_path.parent / "track" / "song")


def test_submit_accepts_an_assemble_kind_and_leaves_its_args_untouched(tmp_path):
    root = tmp_path / "queue"
    project_path = tmp_path / "projects" / "20260818-1200-my-clip" / "project.json"
    args = ["assemble", "--project", str(project_path)]
    job = q.submit(root, args, "", {"output_stem": str(project_path.parent / "final")}, {},
                   kind=q.KIND_ASSEMBLE)

    assert job.kind == q.KIND_ASSEMBLE
    assert job.args == args


def test_submit_refuses_an_unknown_kind(tmp_path):
    root = tmp_path / "queue"
    with pytest.raises(q.QueueError):
        q.submit(root, ["frobnicate", "--project", "/x"], "", _DRY, {}, kind="frobnicate")


def test_submitted_song_job_file_persists_kind_on_disk(tmp_path):
    """`kind` is a real field of the job file, not just an in-memory attribute -- a human reading
    `cat pending/<id>.json` (the module docstring's own promise for every field it writes) must see
    it, and `claim`/`scan` must read it back.
    """
    root = tmp_path / "queue"
    project_path = tmp_path / "projects" / "20260818-1200-my-song" / "project.json"
    args = ["song", "--project", str(project_path)]
    job = q.submit(root, args, "", {"output_stem": str(project_path.parent / "track" / "song")},
                   {}, kind=q.KIND_SONG)

    on_disk = json.loads(q.job_path(root, job.id, "pending").read_text())
    assert on_disk["kind"] == "song"

    claimed = q.claim(root)
    assert claimed.kind == q.KIND_SONG


def test_update_preserves_the_jobs_original_kind(tmp_path):
    """`update` has no `kind` parameter -- editing a job's args/note/estimate must never silently
    turn a song job back into a generate job (the dataclass default `update` would otherwise fall
    through to if it forgot to carry `current.kind` forward).
    """
    root = tmp_path / "queue"
    project_path = tmp_path / "projects" / "20260818-1200-my-song" / "project.json"
    args = ["song", "--project", str(project_path)]
    job = q.submit(root, args, "", {"output_stem": str(project_path.parent / "track" / "song")},
                   {}, kind=q.KIND_SONG)

    updated = q.update(root, job.id, args, "edited note",
                       {"output_stem": str(project_path.parent / "track" / "song")}, {})

    assert updated.kind == q.KIND_SONG


def test_an_old_job_file_with_no_kind_field_reads_as_generate(tmp_path):
    """Backward compatibility (task 3 brief, mandatory): a `pending/<id>.json` written before this
    field existed has no `"kind"` key at all -- `Job(**data)` must fill in `KIND_GENERATE`, not
    raise a `TypeError` for a missing required argument.
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
    path = q.job_path(root, old_style["id"], "pending")
    path.write_text(json.dumps(old_style), encoding="utf-8")

    jobs, broken = q.scan(root)
    assert broken == []
    assert len(jobs) == 1
    assert jobs[0].kind == q.KIND_GENERATE

    claimed = q.claim(root)
    assert claimed.kind == q.KIND_GENERATE


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
