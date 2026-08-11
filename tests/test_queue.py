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
        data = {
            "id": job_id, "state": state, "created_at": "2026-08-11T00:00:00",
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
