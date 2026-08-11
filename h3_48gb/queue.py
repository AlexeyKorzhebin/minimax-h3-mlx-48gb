"""On-disk state for the job queue: layout, durable writes, the queue lock, submission, and scan.

A job's *state* is which of `pending/`, `running/`, `done/`, `failed/` its JSON file sits in, not
a field inside that file: `os.rename` between two directories on the same filesystem is atomic, so
`ls pending/` answers "what is queued" without parsing a single byte. Everything this module adds
on top of that fact is the queue lock, described below, and the durable-write protocol every
committed file goes through -- see `write_text_durably`.

**Why a lock is needed even though renames are atomic.** A caller that reads a job's file, edits
it, and writes it back races the worker: if the worker claims the job (renames it out of
`pending/`) between the read and the write-back, the write-back's `os.replace(temp, pending/<id>)`
does not fail -- it *recreates* the file in the directory the job was just taken from. The job then
exists twice, unexpanded in `running/` and stale-but-fresh-looking in `pending/`, and runs twice.
The same gap means a plain sequential walk of the four state directories is not a snapshot: a job
mid-rename can appear in two of them, or in none, depending on timing. `queue_lock` closes both
holes by serializing every state-changing operation and by holding `scan`'s walk under one lock
acquisition for its whole duration.

Deliberately no dependency on `mlx` or on any of `h3_48gb`'s modules that pull it in: a worker and
a web server both need to inspect and mutate the queue on every request, and neither should pay for
loading a 33B-parameter transformer to do it. See `test_queue_module_does_not_import_mlx` in
`tests/test_queue.py`.

Exception messages raised here are in English, matching `CliError`'s existing `ERROR_CODES`
convention: the design doc's "Ошибки" section folds this module's failures into that same single
contract (`output_stem_conflict`, `job_not_pending`, ...) for the CLI, the worker and the server
alike, matched by a caller on `.code`-equivalent identity (the exception class here), never on the
sentence -- the Russian a human reads comes from the web page rendering that code, in a later task,
not from this module's message text.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import string
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

#: The four directories a job's file can live in. Order matches the order `scan` walks them in,
#: which is also the order a human would want to see them listed in.
QUEUE_STATES = ("pending", "running", "done", "failed")

#: Every suffix a finished (or half-finished) run can leave next to `output_stem`. A stale `.wav`
#: or `.json` from a killed run claims the name just as surely as a `.mp4` does -- the next attempt
#: at that name would overwrite it just the same -- so a stem check that only looked at `.mp4`
#: would let a second job clobber it.
ARTIFACT_SUFFIXES = (".mp4", ".wav", ".npz", ".json")

#: Alphabet for the four-character random suffix appended to every job id. Lowercase and digits
#: only, matching the `[a-z0-9-]` the rest of the id is already restricted to.
_ID_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits


class QueueError(Exception):
    """Base class for every refusal this module raises."""


class JobNotPending(QueueError):
    """Raised by an operation restricted to `pending/` when the job has already left it."""


class OutputStemConflict(QueueError):
    """Raised when `output_stem` is already claimed by a queued job or a file on disk."""


@dataclass(frozen=True)
class Job:
    """One job, exactly as it is stored on disk (plus `state`, which the directory carries instead
    of the file -- see the module docstring). `args` is the literal argument list `h3` will run;
    nothing here reinterprets it, because there is nothing left to lose in translation.
    """

    id: str
    state: str
    created_at: str
    args: list[str]
    note: str
    prompt_source: str | None
    prompt_sha256: str | None
    output_stem: str
    estimate: dict
    priority: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    log_tail: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _job_file_payload(job: Job) -> dict:
    """`Job.as_dict()` minus `state`, for whatever is written to `job_path`.

    The directory a job's file lives in already says its state (see the module docstring); storing
    it a second time inside the file risks exactly the drift a reader has to guard against, except
    a *human* running `cat running/<id>.json` gets no such guard -- `write_json_durably`'s own
    docstring promises the file is meant to be read that way. `Job.as_dict()` itself keeps the
    field, since in-memory callers (the CLI, a future HTTP response) do want it.
    """
    payload = job.as_dict()
    del payload["state"]
    return payload


@dataclass(frozen=True)
class Broken:
    """A file `scan` could not parse into a `Job`, reported instead of silently skipped."""

    path: str
    error: str


def _now() -> str:
    """Indirected so `submit` can be driven with a frozen clock in tests."""
    return datetime.now().isoformat(timespec="seconds")


def _suffix() -> str:
    """Four random id characters. A module-level function (not inlined) so tests can force a
    collision by monkeypatching it to return a fixed sequence -- a real random suffix collides too
    rarely for a test to ever exercise the retry loop honestly.
    """
    return "".join(secrets.choice(_ID_SUFFIX_ALPHABET) for _ in range(4))


def _stamp(created_at: str) -> str:
    """`created_at` as `YYYYmmdd-HHMMSS`, the timestamp half of a job id."""
    return datetime.fromisoformat(created_at).strftime("%Y%m%d-%H%M%S")


def _slug(tag: str | None) -> str:
    """`tag` reduced to `[a-z0-9-]`, or `"job"` if there is no tag or nothing survives the filter."""
    if not tag:
        return "job"
    return re.sub(r"[^a-z0-9-]", "", tag.lower()) or "job"


def _tag_from_args(args: list[str]) -> str | None:
    """The value following `--tag` in an `h3` argument list, or `None`."""
    for i, token in enumerate(args):
        if token == "--tag" and i + 1 < len(args):
            return args[i + 1]
    return None


def layout(root) -> dict[str, Path]:
    """Create every directory the queue needs under `root` and return their paths.

    Idempotent: called by `submit` and `scan` on every invocation (`root` may not exist yet the
    first time either runs), and safe to call again by hand, e.g. from a test that wants a queue
    on disk before writing a job file into it directly.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    paths = {"root": root}
    for name in ("pending", "running", "done", "failed", "leases", "results", "prompts", "logs"):
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        paths[name] = path
    return paths


def write_text_durably(path, text: str) -> None:
    """Write `text` to `path` so that a crash leaves either the old content or the new content,
    never a mixture, and never loses a rename to a power cut.

    Same protocol as `CheckpointStore.write` in `checkpoint.py`, for the same reason: temp file →
    `fsync` the temp file's data → `os.replace` over the target → `fsync` the containing directory.
    The first `fsync` makes the bytes durable before anything points at them; the second makes the
    *rename itself* durable -- without it, a crash right after `os.replace` can lose the rename on
    some filesystems even though the data it points to survives, and a job the caller was told is
    committed turns out not to exist after a reboot. Any failure removes the temp file and leaves
    whatever was previously at `path` untouched.
    """
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    temp = directory / f".{path.name}.tmp-{os.getpid()}"
    try:
        temp.write_text(text, encoding="utf-8")
        fd = os.open(temp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, path)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def write_json_durably(path, payload) -> None:
    """`write_text_durably` for JSON. Human-readable on purpose: every file this writes is small
    and meant to be read with `cat` while debugging a stuck queue.
    """
    write_text_durably(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


@contextlib.contextmanager
def queue_lock(root, exclusive: bool):
    """Hold `queue/queue.lock` for the duration of the `with` block: exclusively (`LOCK_EX`) for
    every operation that changes queue state, shared (`LOCK_SH`) for `scan`'s read-only walk --
    see the module docstring for why a walk without this races a concurrent rename.

    Blocks until the lock is available rather than failing fast: a caller here is a CLI or an HTTP
    handler waiting on a few milliseconds of file I/O from another such caller, never on a
    multi-hour generation run, so there is nothing to time out for. Not reentrant -- a second
    acquisition from the same process while the first is still held would deadlock -- but nothing
    in this module or its callers nests two acquisitions.
    """
    lock_file = Path(root) / "queue.lock"
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def job_path(root, job_id: str, state: str) -> Path:
    if state not in QUEUE_STATES:
        raise QueueError(f"unknown queue state {state!r}, expected one of {QUEUE_STATES}")
    return Path(root) / state / f"{job_id}.json"


def log_path(root, job_id: str) -> Path:
    return Path(root) / "logs" / f"{job_id}.log"


def lease_path(root, job_id: str) -> Path:
    return Path(root) / "leases" / f"{job_id}.lock"


def result_path(root, job_id: str) -> Path:
    return Path(root) / "results" / f"{job_id}.json"


def prompt_path(root, job_id: str) -> Path:
    return Path(root) / "prompts" / f"{job_id}.txt"


def _stem_taken(root: Path, output_stem: str) -> bool:
    """Whether `output_stem` is already claimed -- by a leftover artifact on disk, in any of
    `ARTIFACT_SUFFIXES` (not just `.mp4`: a stale `.wav` or `.json` is just as taken), or by a job
    already sitting in `pending/` or `running/`. A job file that cannot be parsed is skipped here,
    not reported: `scan` is the place that surfaces broken files, and a stem check is not a scan.
    """
    for suffix in ARTIFACT_SUFFIXES:
        if Path(f"{output_stem}{suffix}").exists():
            return True
    for state in ("pending", "running"):
        directory = Path(root) / state
        if not directory.is_dir():
            continue
        for file in directory.glob("*.json"):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("output_stem") == output_stem:
                return True
    return False


def submit(root, args: list[str], note: str, dry_run_report: dict, estimate: dict,
           prompt_source: str | None = None, prompt_text: str | None = None, now=None) -> Job:
    """Queue a new job in `pending/` and return it.

    `output_stem` comes **only** from `dry_run_report["output_stem"]`. The queue does not accept an
    output name from any other source: with `--image`, the canvas size that decides the name is
    computed by code that pulls in `mlx`, so `generate --dry-run` -- the CLI itself -- is the only
    thing that ever actually knows it.

    If `prompt_text` is given, `submit` -- not the caller -- snapshots it to `queue/prompts/<id>.txt`
    and repoints `--prompt-file` in `args` at that copy, so a prompt file edited after queueing runs
    the bytes that were reviewed, not whatever the shared file contains by the time the worker gets
    to it. The snapshot's path needs `id`, and `id` is only decided here, so the order is: claim the
    id (`O_CREAT|O_EXCL` on the pending job file), write the snapshot durably, rewrite `args`, then
    write the job file itself durably. Without a prompt, nothing under `prompts/` is touched and
    `args` is passed through unchanged.

    `args` is validated to actually contain `--prompt-file` (with a value after it) *before* an id
    is claimed, whenever `prompt_text` is given: the alternative -- discovering the mismatch only
    while rewriting `args`, after `O_CREAT|O_EXCL` has already claimed the id -- would raise a bare
    `ValueError`/`IndexError` out of `submit`'s boundary (indistinguishable from a real bug to a
    caller matching on `QueueError`) and leave an empty placeholder job file behind forever. Any
    other failure between the id claim and the final durable write (a full disk, a `write_text_durably`
    crash) is still possible, so everything from the claim onward is wrapped to unclaim the id and
    remove any prompt snapshot already written before the failure propagates.
    """
    root = Path(root)
    layout(root)
    output_stem = str(dry_run_report["output_stem"])

    with queue_lock(root, exclusive=True):
        if _stem_taken(root, output_stem):
            raise OutputStemConflict(f"output_stem already claimed: {output_stem}")

        if prompt_text is not None:
            if "--prompt-file" not in args or args.index("--prompt-file") + 1 >= len(args):
                raise QueueError(
                    "prompt_text was given but args has no --prompt-file placeholder to repoint "
                    "at the snapshot"
                )

        created_at = now() if now is not None else _now()
        stamp = _stamp(created_at)
        slug = _slug(_tag_from_args(args))

        while True:
            job_id = f"{stamp}-{slug}-{_suffix()}"
            path = job_path(root, job_id, "pending")
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                continue
            os.close(fd)
            break

        snapshot = None
        try:
            args = list(args)
            prompt_sha256 = None
            if prompt_text is not None:
                snapshot = prompt_path(root, job_id)
                write_text_durably(snapshot, prompt_text)
                prompt_sha256 = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
                args[args.index("--prompt-file") + 1] = str(snapshot)

            job = Job(
                id=job_id, state="pending", created_at=created_at, args=args, note=note,
                prompt_source=prompt_source, prompt_sha256=prompt_sha256,
                output_stem=output_stem, estimate=dict(estimate), priority=0,
                started_at=None, finished_at=None, exit_code=None, log_tail=None,
            )
            write_json_durably(path, _job_file_payload(job))
        except BaseException:
            path.unlink(missing_ok=True)
            if snapshot is not None:
                snapshot.unlink(missing_ok=True)
            raise

    return job


def _job_from_file(path: Path, state: str) -> Job:
    """Parse one job file, with `state` taken from the directory it was found in -- the directory
    is the authority (see the module docstring), so a stale `state` field inside the file itself,
    if one is ever left behind by a future bug, still gets overridden here rather than trusted.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    data["state"] = state
    return Job(**data)


def scan(root) -> tuple[list[Job], list[Broken]]:
    """Every job under `root`, plus every file that could not be read as one.

    Held under the queue lock, shared (`LOCK_SH`), for the whole walk -- without it, a job mid-move
    between two state directories could be seen twice, once, or not at all, depending on exactly
    when the rename lands relative to this function's directory listings. A broken file is reported
    in the second list, not skipped: a silently shorter queue would leave a human wondering where a
    job went.

    Read-only: unlike `submit`, `scan` never calls `layout` and never creates a missing state
    directory -- a caller repeatedly reading `<H3_OUTDIR>/queue/` should not risk quietly building
    that path into existence out of a typo, and a status endpoint polled every few seconds should
    not touch the filesystem beyond the reads it actually needs. A directory that does not exist
    contributes no jobs, same as one that exists and is empty.
    """
    root = Path(root)
    jobs: list[Job] = []
    broken: list[Broken] = []

    if not root.is_dir():
        return jobs, broken

    with queue_lock(root, exclusive=False):
        for state in QUEUE_STATES:
            directory = root / state
            if not directory.is_dir():
                continue
            for file in sorted(directory.glob("*.json")):
                try:
                    jobs.append(_job_from_file(file, state))
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    broken.append(Broken(path=str(file), error=f"{type(exc).__name__}: {exc}"))

    return jobs, broken
