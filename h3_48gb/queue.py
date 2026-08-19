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

#: `Job.kind` -- what the worker actually does with a claimed job (task 3, "Проекты"):
#: `KIND_GENERATE` spawns `h3 generate` as a subprocess, exactly as every job did before this field
#: existed (see `Job.kind`'s own docstring for why that is also the default). `KIND_SONG` and
#: `KIND_ASSEMBLE` are run in-process by `h3_48gb.worker` -- `h3_48gb.songrun.run_song`/
#: `align_track` and `h3_48gb.assemble.run` (task 4) respectively -- never as a subprocess of this
#: package's own CLI, because neither needs `generate`'s dry-run/canvas/output-stem machinery and
#: both already isolate their own heavy lifting (Music3, ffmpeg, `mlx_whisper`) behind their own
#: subprocess calls, in Music3's *separate* virtualenv.
KIND_GENERATE = "generate"
KIND_SONG = "song"
KIND_ASSEMBLE = "assemble"

#: Every `Job.kind` this module and the worker know about. `submit` refuses anything else -- a
#: caller with a typo'd kind should see `QueueError` at submission time, not a job that sits in
#: `pending/` forever because nothing claims to know how to run it.
JOB_KINDS = (KIND_GENERATE, KIND_SONG, KIND_ASSEMBLE)

#: Every suffix a finished (or half-finished) run can leave next to `output_stem`. A stale `.wav`
#: or `.json` from a killed run claims the name just as surely as a `.mp4` does -- the next attempt
#: at that name would overwrite it just the same -- so a stem check that only looked at `.mp4`
#: would let a second job clobber it.
ARTIFACT_SUFFIXES = (".mp4", ".wav", ".npz", ".json")

#: Alphabet for the four-character random suffix appended to every job id. Lowercase and digits
#: only, matching the `[a-z0-9-]` the rest of the id is already restricted to.
_ID_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits

#: How many trailing log lines `read_log_tail` keeps, and therefore how much of a run's output
#: lands in `Job.log_tail`. A log can be tens of megabytes; this is the amount a human reads to
#: find out why a run ended.
LOG_TAIL_LINES = 40

#: What `reconcile` records as `log_tail` for the third row of its table -- a job whose `.mp4`
#: exists but whose result marker does not. In Russian because it is shown to a human on the
#: queue page, unlike this module's exception messages (see the module docstring).
RESULT_RECOVERED_NOTE = "результат найден на диске, отметка потеряна"

#: Name of the marker file directly under the queue root whose mere *existence* means "paused" --
#: see `is_paused`. A bare name, not a path: every caller reaches it through `is_paused`/
#: `set_paused`, so nothing outside this module needs to know it lives at the root rather than,
#: say, under `leases/`.
PAUSED_MARKER_NAME = "paused"


class QueueError(Exception):
    """Base class for every refusal this module raises."""


class JobNotPending(QueueError):
    """Raised by an operation restricted to `pending/` when the job has already left it."""


class OutputStemConflict(QueueError):
    """Raised when `output_stem` is already claimed by a queued job or a file on disk.

    `output_stem` is carried as an attribute, not just folded into the message: `submit` (task A6)
    raises this against the *relocated* stem -- the job's own subdirectory included -- which a
    caller reporting "which name is taken" (`web.py`'s `queue_write_errors`) needs verbatim rather
    than reconstructed, and reconstructing it from `str(exc)` would parse a sentence this module's
    own docstring says is not a contract (see the module docstring's "Exception messages" note).
    """

    def __init__(self, output_stem: str):
        self.output_stem = output_stem
        super().__init__(f"output_stem already claimed: {output_stem}")


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
    #: Task 3 ("Проекты"): which of `JOB_KINDS` this job is, defaulting to `KIND_GENERATE`. The
    #: default is not a policy choice -- it is what makes reading an old job file written before
    #: this field existed work at all: `Job(**data)` (`_job_from_file`/`_build_job`) fills in
    #: `"generate"` for any `pending/`, `running/`, `done/` or `failed/` file on disk that predates
    #: this field, exactly what every one of them in fact was. Every explicit `Job(...)`
    #: construction elsewhere in this module (`submit`, `update`) passes `kind` itself rather than
    #: relying on this default, so it is only ever *read* here, never silently reintroduced by a
    #: caller editing a song/assemble job.
    kind: str = KIND_GENERATE
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
    """A file an operation could not act on, reported instead of silently skipped: unparseable to
    `scan`, unrecoverable to `reconcile` (see `Reconciled.conflicted`). Both need the same two
    facts -- which file, and why -- and a human staring at a queue that is one job short needs
    them for the same reason.
    """

    path: str
    error: str


@dataclass(frozen=True)
class Reconciled:
    """What `reconcile` did, what it deliberately did not touch, and what it could not fix.

    `changed` is every job whose state it moved -- finished by its result marker, finished because
    its `.mp4` is on disk, or returned to `pending/`. `alive` is every job it left in `running/`
    because that job's lease is held right now: someone else's process is still generating, and the
    caller must not start a second one (see `reconcile`'s table).

    `conflicted` is every file it had to step over: a corrupt job file, or a desync where the
    destination of the move already exists (`_rename_durably` refuses to overwrite). These are
    reported rather than raised because `reconcile` runs at the top of *every* worker iteration --
    an exception here does not fail one job, it kills the worker, and it kills the next one too,
    on the same file, until someone repairs the directories by hand.
    """

    changed: list[Job]
    alive: list[Job]
    conflicted: list[Broken]


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


#: The subdirectory `submit` gives every job's own output: `<outdir>/<YYYYMMDD-HHMM>-<slug>/`.
#: Minute precision -- not `_stamp`'s seconds -- because a human reading directory names on disk
#: does not need second-level uniqueness: the slug already separates two jobs queued in the same
#: minute under different tags, and `submit`'s own id-claim retry loop (not this string) is what
#: actually guarantees the *job*'s uniqueness. Matched by `_base_outdir` so that a job resubmitted
#: from another job's own, already-relocated `args` -- `_duplicate_job` in `web.py` does exactly
#: this, reusing the source job's `args` verbatim except for `--tag` -- gets a subdirectory that is
#: a *sibling* of the source's, not nested inside it.
_JOB_SUBDIR_RE = re.compile(r"^\d{8}-\d{4}-[a-z0-9-]+$")


def _dir_stamp(created_at: str) -> str:
    """`created_at` as `YYYYmmdd-HHMM` -- the timestamp half of a job's own output subdirectory.
    Coarser than `_stamp` (seconds, used for the job id itself) on purpose; see `_JOB_SUBDIR_RE`.
    """
    return datetime.fromisoformat(created_at).strftime("%Y%m%d-%H%M")


def _outdir_is_a_known_job_subdir(root, outdir: Path) -> bool:
    """Whether some job this queue actually knows about -- `pending`, `running`, `done` or
    `failed`, the same four states `defaultOutdir()` (`app.js`) reads from `state.queue` -- was
    really relocated into `outdir`, i.e. `Path(job.output_stem).parent == outdir` for some job
    file under `root`.

    This is the evidence `_base_outdir` needs and the bare directory *name* cannot provide: a
    directory shaped like `YYYYMMDD-HHMM-<slug>` proves nothing about who created it, only that it
    is shaped the way `_relocate_to_job_subdir` shapes the subdirectories it creates. Actually
    finding a job recorded under `root` whose own `output_stem` sits inside it is proof, because
    nothing else in this codebase ever writes a job whose `output_stem` claims a directory it does
    not use.

    Broken or unreadable job files are skipped exactly like `_stem_taken` skips them: this is a
    same-shape check, not `scan`, and is not the place to surface a corrupt job file.
    """
    root = Path(root)
    for state in QUEUE_STATES:
        directory = root / state
        if not directory.is_dir():
            continue
        for file in directory.glob("*.json"):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            stem = data.get("output_stem")
            if stem and Path(stem).parent == outdir:
                return True
    return False


def _base_outdir(root, outdir: Path) -> Path:
    """`outdir` with a trailing job subdirectory stripped, or `outdir` itself if either its last
    component does not look like one `submit` created (`_JOB_SUBDIR_RE`), or -- fix round 2,
    BACKLOG "UX-мелочи" -- no job `root` actually knows about was ever relocated into it.

    Pattern alone used to be the whole test, and that is wrong in the direction of stripping too
    eagerly: `defaultOutdir()` (`app.js`) pre-fills the web form's outdir field with whatever the
    *previous* job's own, already-relocated `--outdir` was, so a human who edits that field, or who
    simply types a similarly-shaped path from habit -- `--outdir ~/out/20260101-1200-myproject`,
    never submitted through this queue before -- hands `submit` a string indistinguishable by shape
    alone from one `_relocate_to_job_subdir` actually produced. Stripping it anyway silently moves
    the job's output one level *above* the directory the human named, into its parent, which is not
    what "I typed this directory as my own outdir" means.

    The fix asks the queue itself, not the string, via `_outdir_is_a_known_job_subdir`: only a
    directory that some job recorded under `root` actually used is "ours" to strip. A brand-new
    directory nobody has ever been relocated into -- pattern match or not -- is treated like any
    other outdir a human is free to write straight into, and `_relocate_to_job_subdir` nests a
    fresh job subdirectory inside it exactly as it would for `~/video-out`.

    Without this, duplicating a job would nest the copy's subdirectory inside the source's rather
    than beside it -- `_duplicate_job` (`web.py`) resubmits the source job's own `args`, whose
    `--outdir` this module already relocated once, so the naive "always append a fresh
    subdirectory onto whatever `--outdir` already says" reading of this feature would grow one
    level deeper every time a job already sitting under a job subdirectory is duplicated again.
    That case is exactly what `_outdir_is_a_known_job_subdir` still recognizes: the source job's own
    record is sitting right there under `root`, `output_stem` and all.
    """
    if not _JOB_SUBDIR_RE.fullmatch(outdir.name):
        return outdir
    if not _outdir_is_a_known_job_subdir(root, outdir):
        return outdir
    return outdir.parent


def _last_outdir_token(args: list[str]) -> tuple[int, bool] | None:
    """Where `--outdir`'s value sits in `args`: `(index, inline)`. `inline` means the
    `--outdir=value` spelling, where the value is `args[index]` itself past the `=`; otherwise the
    value is the separate token `args[index]`, one past the flag.

    The *last* occurrence, matching argparse's own "a repeated flag, last spelling wins" rule: an
    earlier, now-dead occurrence -- `check_path_flags` (`web.py`) rewrites every occurrence it
    finds without removing any, so a caller can arrive here with `--outdir` twice -- must not be
    the one this module edits, or the value the CLI will actually resolve is left untouched while
    a value nothing reads gets the new subdirectory.

    `None` if `args` has no `--outdir` at all: `_relocate_to_job_subdir` then falls back to the
    dry-run report's own `output_stem`, which already names the directory the CLI actually
    resolved -- its own default, for a caller that left the flag out.
    """
    found = None
    for i, token in enumerate(args):
        if token == "--outdir" and i + 1 < len(args):
            found = (i + 1, False)
        elif token.startswith("--outdir="):
            found = (i, True)
    return found


def _relocate_to_job_subdir(root, args: list[str], output_stem: str,
                            created_at: str) -> tuple[list[str], str]:
    """Rewrite (or add) `--outdir` in `args` to `<base>/<YYYYMMDD-HHMM>-<slug>`, and return the
    matching `output_stem` alongside the rewritten `args` -- `submit`'s "every job gets its own
    output subdirectory" feature, in full.

    `<base>` is *not* whatever `--outdir` already says in `args`: it is that value with any
    existing job subdirectory stripped first (`_base_outdir`), so a duplicated job's own copy of
    `args` -- which already names its source's subdirectory -- lands beside it, not inside it.
    `root` is `_base_outdir`'s: stripping only happens for a directory some job under `root`
    actually used, never on shape alone -- see `_base_outdir` for why.

    The directory is read off `output_stem` (`dry_run_report["output_stem"]`'s own parent) rather
    than by parsing `--outdir` back out of `args` a second time: `prepare_submission` (`web.py`)
    already guarantees the two agree -- `output_stem` is `outdir / f"h3-{tag}-{W}x{H}"`, computed
    by the CLI's own dry run against exactly the `--outdir` in `args` -- and reading it off
    `output_stem` instead also covers the one case where `args` carries no `--outdir` at all: the
    report still names the directory the CLI's own default resolved to, which `args` alone cannot.

    Only the filename half of `output_stem` (`h3-<tag>-<W>x<H>`) survives untouched; everything
    about *where* it lives is replaced by the new subdirectory.
    """
    old_outdir = Path(output_stem).parent
    base = _base_outdir(root, old_outdir)
    subdir = f"{_dir_stamp(created_at)}-{_slug(_tag_from_args(args))}"
    new_outdir = base / subdir
    new_output_stem = str(new_outdir / Path(output_stem).name)

    args = list(args)
    token = _last_outdir_token(args)
    if token is None:
        args.extend(["--outdir", str(new_outdir)])
    else:
        index, inline = token
        args[index] = f"--outdir={new_outdir}" if inline else str(new_outdir)
    return args, new_output_stem


def layout(root) -> dict[str, Path]:
    """Create every directory the queue needs under `root` and return their paths.

    Idempotent: called by `submit` and `scan` on every invocation (`root` may not exist yet the
    first time either runs), and safe to call again by hand, e.g. from a test that wants a queue
    on disk before writing a job file into it directly.

    **A queue root that did not exist yet is created paused** (`set_paused(root, True)`, once,
    only on this first call): a brand-new queue is one nobody has looked at, and a job submitted
    to it before a human has seen the page should not start a multi-hour run unattended. The check
    is "did `root` itself exist before this call", not "is the marker missing" -- `main_loop` calls
    `layout` on every worker startup (see its docstring), and a marker a human explicitly removed
    to resume the queue must not be resurrected by the next restart. An *existing* root is left
    exactly as paused or unpaused as it already was, however incomplete its subdirectories are --
    `root` existing at all is proof `layout` (or `submit`) ran here before, so there is nothing
    "fresh" left to default.
    """
    root = Path(root)
    freshly_created = not root.is_dir()
    root.mkdir(parents=True, exist_ok=True)
    paths = {"root": root}
    for name in ("pending", "running", "done", "failed", "leases", "results", "prompts", "logs"):
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        paths[name] = path
    if freshly_created:
        set_paused(root, True)
    return paths


def is_paused(root) -> bool:
    """Whether the queue at `root` is paused: does `<root>/paused` exist.

    A missing marker means "not paused" -- **deliberately the safe direction**. The marker carries
    no content and nothing durable-writes it (see `set_paused`), so a plain crash cannot corrupt it
    the way a job file can; what can make it disappear is a human clearing the queue directory by
    hand, a backup that drops zero-length files, or any other loss nobody asked for. Losing "paused"
    resumes the queue, which a human watching the page notices within one `poll` interval and can
    re-pause; losing "running" would leave the queue silently idle with a free GPU and no worker
    ever explaining why nothing moves -- there is no poll interval that surfaces an *absence* of
    activity as loudly as `unloadBanner`'s plate flags a *presence* of one. `main_loop` calls this,
    not the reverse, at the top of every iteration -- see its docstring for exactly where.

    **The same "not paused" answer, not an exception, for any `OSError` `Path.exists` can raise**
    (BACKLOG "UX-мелочи", task 2) -- not only a plain missing file. `Path.exists` only swallows the
    handful of errnos that mean "there is nothing here" (`ENOENT`, `ENOTDIR`, ...); a directory this
    process cannot even `stat` -- permissions changed under it, a network mount gone away -- raises
    `PermissionError`/`OSError` straight through, which used to reach `main_loop` unhandled despite
    this docstring already claiming the safe direction. The fix-open reasoning above already covers
    this case: a queue a human cannot even ask "are you paused?" about is no safer standing frozen
    than one that resumes and gets re-paused once the underlying problem -- permissions, a missing
    mount -- is fixed and the check starts answering normally again.
    """
    try:
        return (Path(root) / PAUSED_MARKER_NAME).exists()
    except OSError:
        return False


def set_paused(root, value: bool) -> None:
    """Create (`value=True`) or remove (`value=False`) `<root>/paused`.

    Not routed through `write_text_durably`: the marker's only fact is whether the file exists, and
    an interrupted `touch`/`unlink` leaves it either fully present or fully absent, never a
    half-written mixture the way a truncated job file can be -- there is no partial state for a
    crash to land in. `main_loop` polls `is_paused` again every `poll` seconds regardless, so even
    the "wrong" outcome of an interrupted call self-heals on the next iteration rather than sticking.
    """
    path = Path(root) / PAUSED_MARKER_NAME
    if value:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    else:
        path.unlink(missing_ok=True)


def pause_if_drained(root) -> bool:
    """Pause the queue if `pending/` holds nothing `claim` would take. Returns whether it paused.

    The worker calls this after every finished job, so that a queue which empties itself stops
    instead of standing ready to grab whatever lands in it next. The reason is the morning after a
    night's batch: the run finished hours ago, the worker is still looping, and the two jobs a
    human drops into the form to *look at* before starting would begin computing on their own.

    **The emptiness check and the marker go under one exclusive `queue_lock` acquisition**, which is
    what makes this a function here rather than two lines in `main_loop`. Unlocked, a `submit`
    landing between the look and the touch is seen by neither: the walk misses the file that is
    still being written, and the job that does land is then held by a marker placed after it. Under
    the lock the two orderings are the only two possible ones, and both are correct -- a submit that
    wins the lock first is seen (no pause), and one that loses it lands on a paused queue and waits
    for a human, which is exactly what the marker is for.

    "Nothing to take" is deliberately `claim`'s notion of it and not `any(glob("*.json"))`: `claim`
    skips a file that does not parse, so a corrupt leftover in `pending/` would otherwise cancel the
    auto-pause for ever -- silently, and precisely in a queue where something has already gone wrong.
    """
    root = Path(root)
    with queue_lock(root, exclusive=True):
        directory = root / "pending"
        if directory.is_dir():
            for file in directory.glob("*.json"):
                try:
                    _build_job(json.loads(file.read_text(encoding="utf-8")), "pending")
                except (OSError, ValueError, QueueError):
                    continue
                return False
        set_paused(root, True)
        return True


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


def _stem_taken(root: Path, output_stem: str, exclude_id: str | None = None) -> bool:
    """Whether `output_stem` is already claimed -- by a leftover artifact on disk, in any of
    `ARTIFACT_SUFFIXES` (not just `.mp4`: a stale `.wav` or `.json` is just as taken), or by a job
    already sitting in `pending/` or `running/`. A job file that cannot be parsed is skipped here,
    not reported: `scan` is the place that surfaces broken files, and a stem check is not a scan.

    `exclude_id`, used only by `update`, skips the job being edited itself: otherwise a job could
    never be re-submitted with the exact same `output_stem` it already holds, which is the common
    case (editing a prompt or a seed without renaming the output).
    """
    for suffix in ARTIFACT_SUFFIXES:
        if Path(f"{output_stem}{suffix}").exists():
            return True
    for state in ("pending", "running"):
        directory = Path(root) / state
        if not directory.is_dir():
            continue
        for file in directory.glob("*.json"):
            if exclude_id is not None and file.stem == exclude_id:
                continue
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("output_stem") == output_stem:
                return True
    return False


def _validate_args_shape_for_kind(kind: str, args: list[str]) -> None:
    """M4 (fix round 1, 2026-08-18 review): refuse a `kind`/`args` mismatch honestly, at `submit`
    time, rather than letting it sit in `pending/` and fail only once the worker actually claims it
    -- `_project_arg` (`worker.py`) raising `ValueError` for a `kind="song"` job with no `--project`
    token, or a `kind="generate"` job whose `args` happen to start with `"song"`/`"assemble"` being
    handed to `h3 generate` as nonsense positional arguments, are both mistakes a human made at
    submission time and should be told about right then, not an hour later when the queue finally
    reaches the job.

    `kind="song"`/`"assemble"`: `args[0]` must equal `kind` and `"--project"` must appear somewhere
    in `args` -- the exact shape task 3's brief requires (`["song", "--project", <path>]`/
    `["assemble", "--project", <path>]`) and the only shape `_project_arg` can parse. `kind=
    "generate"` (the default -- every caller before this validation existed): `args[0]` must *not*
    be `"song"`/`"assemble"`, catching the mirror mistake -- song/assemble-shaped args submitted
    without also setting `kind`, which would otherwise be handed to `h3 generate` as its own
    subprocess argv and fail inside the CLI instead of at submission.
    """
    if kind in (KIND_SONG, KIND_ASSEMBLE):
        if not args or args[0] != kind or "--project" not in args:
            raise QueueError(
                f"kind={kind!r} job args must look like [{kind!r}, '--project', <path>, ...], "
                f"got {args!r}")
    elif kind == KIND_GENERATE:
        if args and args[0] in (KIND_SONG, KIND_ASSEMBLE):
            raise QueueError(
                f"kind=\"generate\" job args must not start with {args[0]!r} -- pass "
                f"kind={args[0]!r} to submit() instead, got {args!r}")


def submit(root, args: list[str], note: str, dry_run_report: dict, estimate: dict,
           prompt_source: str | None = None, prompt_text: str | None = None, now=None,
           kind: str = KIND_GENERATE) -> Job:
    """Queue a new job in `pending/` and return it.

    `output_stem` comes **only** from `dry_run_report["output_stem"]`. The queue does not accept an
    output name from any other source: with `--image`, the canvas size that decides the name is
    computed by code that pulls in `mlx`, so `generate --dry-run` -- the CLI itself -- is the only
    thing that ever actually knows it.

    `kind` (task 3, `JOB_KINDS`) defaults to `KIND_GENERATE`, matching every job queued before this
    parameter existed. **Only a `KIND_GENERATE` job is relocated into its own output subdirectory**
    (`_relocate_to_job_subdir`, below): that rewrite edits `--outdir` in `args`, a flag `generate`
    alone understands -- a `KIND_SONG`/`KIND_ASSEMBLE` job's `args` is `["song", "--project",
    <path>]`/`["assemble", "--project", <path>]` (task 3's brief, verbatim), no `--outdir` token to
    rewrite and no canvas-shaped output file for one to name; relocating it anyway would silently
    graft an `--outdir` flag onto an argument list the worker's in-process song/assemble dispatch
    (`h3_48gb.worker`) never expects and does not parse. For those kinds `dry_run_report` still
    supplies `output_stem` -- used only for `_stem_taken`'s conflict check, e.g. a caller naming a
    project's own track directory so two song jobs for the same project cannot both be pending at
    once -- but the caller's own `args` and `output_stem` otherwise pass straight through unchanged.

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

    **`submit` also gives the job its own output subdirectory.** Before anything else in `args` is
    trusted, `--outdir` is rewritten (or added, if absent) to `<base>/<YYYYMMDD-HHMM>-<slug>` --
    see `_relocate_to_job_subdir` -- and `output_stem` is rewritten to match. This is `submit`'s
    own doing, not `h3 generate`'s: a direct CLI invocation, or a job already sitting in the queue
    from before this feature existed, is never touched. The relocation happens ahead of the
    `output_stem` conflict check, so what is actually checked -- and actually stored -- is the path
    the run will actually write, subdirectory included.
    """
    if kind not in JOB_KINDS:
        raise QueueError(f"unknown job kind {kind!r}, expected one of {JOB_KINDS}")
    _validate_args_shape_for_kind(kind, args)

    root = Path(root)
    layout(root)
    output_stem = str(dry_run_report["output_stem"])

    with queue_lock(root, exclusive=True):
        # Checked on the args exactly as the caller passed them, before `_relocate_to_job_subdir`
        # ever touches `--outdir`: that call appends a token when `--outdir` is absent, and doing
        # it first would make `--prompt-file` (itself the *last* token, with no value) look like it
        # had one -- the appended `--outdir` -- and this refusal would never fire.
        if prompt_text is not None:
            if "--prompt-file" not in args or args.index("--prompt-file") + 1 >= len(args):
                raise QueueError(
                    "prompt_text was given but args has no --prompt-file placeholder to repoint "
                    "at the snapshot"
                )

        created_at = now() if now is not None else _now()
        if kind == KIND_GENERATE:
            args, output_stem = _relocate_to_job_subdir(root, args, output_stem, created_at)

        if _stem_taken(root, output_stem):
            raise OutputStemConflict(output_stem)

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
                output_stem=output_stem, estimate=dict(estimate), kind=kind, priority=0,
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


def _max_pending_priority(root: Path) -> int:
    """The highest `priority` among jobs currently in `pending/`, or 0 if none parse or none
    exist. `move_to_front` adds one to this so the job it touches outranks everything already
    waiting. Broken files are skipped the same way `_stem_taken` skips them -- surfacing them is
    `scan`'s job, not this one's.
    """
    best = 0
    directory = Path(root) / "pending"
    if not directory.is_dir():
        return best
    for file in directory.glob("*.json"):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        priority = data.get("priority", 0)
        if isinstance(priority, int) and priority > best:
            best = priority
    return best


def _rename_durably(src: Path, dest: Path) -> None:
    """Atomically move `src` to `dest` (same filesystem, `os.rename`) and make the rename durable
    by fsyncing both directories whose listing it changes.

    The design spec is explicit that a queue state transition is a single rename, not a durable
    write to the new path followed by unlinking the old one: two separate operations leave a real
    window -- a crash between them, or an `unlink` whose effect never reaches disk -- where the
    job exists in both the old and the new directory *after a reboot*, not just during a race.
    `os.rename` has no such window: at every instant, including mid-crash, the filesystem has
    either the old name or the new one, never both -- this is exactly why `submit`'s directory
    layout works as a source of truth in the first place (see the module docstring). Without
    fsyncing both directories afterward, though, the rename itself is not guaranteed to survive a
    power cut even though it is atomic while the machine stays up.

    Refuses if `dest` already exists: `os.rename` would otherwise replace it silently on POSIX,
    and every caller here (`claim` into `running/`, `finish` into `done/`/`failed/`) only ever
    targets a path that a correctly functioning queue never already has an entry at. Finding one
    anyway is a symptom of a desync -- a duplicate id, a previous crash mid-transition -- worth
    surfacing loudly rather than quietly clobbering.
    """
    src, dest = Path(src), Path(dest)
    if dest.exists():
        raise QueueError(f"refusing to overwrite an existing {dest} during a state transition")
    os.rename(src, dest)
    for directory in {src.parent, dest.parent}:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _read_job_dict(path: Path) -> dict:
    """Parse a job file (known to exist) into its raw dict, still missing `state`. Translates a
    corrupt file into `QueueError` instead of letting `json.JSONDecodeError` (a `ValueError`
    subclass) escape: `scan` can report the same failure as `Broken` and move on to the next file,
    but a mutator asked to act on one specific id has nothing else to fall back to.
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QueueError(f"{path} is corrupt: {type(exc).__name__}: {exc}") from exc


def _build_job(data: dict, state: str) -> Job:
    """`Job(**data, state=state)`, translating a shape mismatch -- an extra key from a stray
    field, a missing one from a truncated write -- into `QueueError` instead of letting a bare
    `TypeError`/`KeyError` escape the module's own exception family.
    """
    try:
        return Job(**{**data, "state": state})
    except (TypeError, KeyError) as exc:
        raise QueueError(
            f"job data does not match the expected shape: {type(exc).__name__}: {exc}"
        ) from exc


def _load_job_or_raise(path: Path, state: str) -> Job:
    """Read and parse a job file known to exist, as `_job_from_file` does, but through
    `_read_job_dict`/`_build_job` so any corruption -- unparseable JSON or a field-shape mismatch
    -- comes out as `QueueError` rather than a bare `ValueError`/`TypeError`/`KeyError`.
    """
    return _build_job(_read_job_dict(path), state)


def claim(root, now=None) -> Job | None:
    """Take the job `pending/` would list first -- lowest `(-priority, id)`, i.e. highest
    priority, oldest id among ties -- move it into `running/`, and stamp `started_at`. Returns
    `None` if nothing in `pending/` parses into a candidate.

    Deciding which job to take and moving it are one exclusive lock acquisition: nothing else can
    observe `pending/` in between, so there is no window here for anything to race.

    The move itself is `_rename_durably`: `started_at` is written into the *pending* copy first
    (durably, in place), then that file is renamed into `running/` -- never a durable write to the
    new path followed by unlinking the old one (see `_rename_durably`'s docstring for why that
    weaker pattern is a real, reboot-surviving bug, not just a race).

    A job file that fails to parse, or parses but does not match `Job`'s shape, is skipped, not
    raised: one broken file must not jam every valid job queued behind it. `scan` is what reports
    broken files; this is not `scan`.
    """
    root = Path(root)
    with queue_lock(root, exclusive=True):
        directory = root / "pending"
        if not directory.is_dir():
            return None
        candidates: list[tuple[Path, dict]] = []
        for file in directory.glob("*.json"):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                _build_job(data, "pending")  # validate shape; skip anything malformed
            except (OSError, ValueError, QueueError):
                continue
            candidates.append((file, data))
        if not candidates:
            return None

        def sort_key(item: tuple[Path, dict]) -> tuple[int, str]:
            _, data = item
            priority = data.get("priority", 0)
            priority = priority if isinstance(priority, int) else 0
            return (-priority, str(data.get("id", "")))

        candidates.sort(key=sort_key)
        file, data = candidates[0]
        job_id = data["id"]
        data["started_at"] = now() if now is not None else _now()
        write_json_durably(file, data)
        _rename_durably(file, job_path(root, job_id, "running"))
        job = Job(**{**data, "state": "running"})

    return job


def _finish_locked(root: Path, job_id: str, exit_code: int, log_tail: str, finished_at=None) -> Job:
    """`finish`'s body, without acquiring the queue lock -- see `finish` for the contract.

    Split out for `reconcile`, which finishes jobs from inside its own single exclusive
    acquisition. `queue_lock` is not reentrant (its docstring says so), so a `reconcile` that
    called the public `finish` would deadlock against itself; and a `reconcile` that dropped the
    lock around each `finish` would reopen exactly the window review circle 1 of task 2 closed --
    between two acquisitions a cancelled job came back to life and a running job's file was
    overwritten. One lock, one critical section, no exceptions.
    """
    running = job_path(root, job_id, "running")
    if not running.exists():
        raise QueueError(f"job {job_id} is not running")
    data = _read_job_dict(running)
    _build_job(data, "running")  # validate shape before touching disk any further
    data["finished_at"] = finished_at if finished_at is not None else _now()
    data["exit_code"] = exit_code
    data["log_tail"] = log_tail
    new_state = "done" if exit_code == 0 else "failed"
    write_json_durably(running, data)
    _rename_durably(running, job_path(root, job_id, new_state))
    return Job(**{**data, "state": new_state})


def finish(root, job_id: str, exit_code: int, log_tail: str, finished_at=None) -> Job:
    """Move a job out of `running/` into `done/` (exit code 0) or `failed/` (anything else),
    stamping `finished_at`, `exit_code` and `log_tail`.

    `finished_at` is a parameter, not always `_now()`: startup reconciliation (`reconcile`)
    restores it from the result marker `queue/results/<id>.json`, written when the run actually
    exited, not from whenever reconciliation happens to run afterward.

    The move is `_rename_durably`, same as `claim`: the updated content is written into the
    *running* copy first, then that file is renamed into `done/`/`failed/`.

    Raises the module's base `QueueError` if the job is not in `running/` -- this is not the
    `pending/`-only `JobNotPending` restriction `update`/`move_to_front`/`cancel` share, because
    `finish` is only ever called by the worker immediately after the process it started exits, on
    the job id it itself just claimed; a caller here has a real bug, and a bare `KeyError`/
    `FileNotFoundError` leaking out would be indistinguishable from one to code matching on
    `QueueError`. The same translation covers a corrupt or shape-mismatched `running/<id>.json`.
    """
    root = Path(root)
    with queue_lock(root, exclusive=True):
        return _finish_locked(root, job_id, exit_code, log_tail, finished_at)


def _return_to_pending_locked(root: Path, job_id: str) -> Job:
    """Move a job back from `running/` into `pending/` without acquiring the queue lock, for
    `reconcile`'s fourth row: the run was interrupted before it produced anything.

    The file's content is deliberately not rewritten -- in particular `started_at` stays as
    `claim` stamped it. The run resumes from its own checkpoint rather than starting over, so the
    moment it first started is still the truth about it, and `update` is already written to carry
    that field forward untouched (see `test_update_preserves_started_at_left_by_a_reconciled_job`).
    """
    running = job_path(root, job_id, "running")
    data = _read_job_dict(running)
    _build_job(data, "running")  # validate shape before touching disk any further
    _rename_durably(running, job_path(root, job_id, "pending"))
    return Job(**{**data, "state": "pending"})


def write_result_marker(root, job_id: str, exit_code: int, finished_at: str) -> None:
    """Record how a run ended, durably, at `queue/results/<id>.json`.

    Written by the worker immediately after the subprocess exits and **before** the job's lease is
    released, which is the entire point of it: between those two moments the worker can be killed,
    and this file is then the only thing that still knows the run's exit code. `reconcile` reads it
    back -- see its second table row -- and restores `finished_at` from here rather than inventing
    a fresh one, because the run ended when it ended, not when the machine came back up.
    """
    write_json_durably(result_path(root, job_id),
                       {"exit_code": int(exit_code), "finished_at": finished_at})


def _read_result_marker(root: Path, job_id: str) -> dict | None:
    """The result marker for `job_id`, or `None` if there is none that can be believed.

    A marker that is missing, unreadable, not an object, or missing an integer `exit_code` all come
    back as `None` -- the same answer, deliberately. A half-written marker tells us nothing about
    how the run ended, and the rows below it in `reconcile`'s table (is there an `.mp4`?) are a
    better answer than a guessed exit code: at worst the run is re-queued and resumes from its
    checkpoint, where believing a corrupt marker could file a failed run under `done/`.
    """
    try:
        data = json.loads(result_path(root, job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("exit_code"), int):
        return None
    return data


#: How much `read_log_tail` pulls per backwards step. Comfortably more than 40 lines of a progress
#: log, so the common case is a single read.
_TAIL_CHUNK_BYTES = 8192


def read_log_tail(root, job_id: str, lines: int = LOG_TAIL_LINES) -> str:
    """The last `lines` lines of `queue/logs/<id>.log`, or `""` if there is no readable log.

    Read by seeking backwards from the end, not by streaming the file front to back. A bounded
    `deque` over the whole file keeps *memory* constant but still pays the full read, and this is
    called from inside `reconcile`'s exclusive critical section -- where the design spec promises
    the queue lock is held for milliseconds. A run that prints a line per forward leaves tens of
    megabytes; every page load waiting on that walk is the difference between a promise and a lie.

    The first chunk read backwards can start mid-character; `errors="replace"` absorbs that, and
    whatever it produces sits in the partial first line, which is discarded by the final slice.
    """
    try:
        with open(log_path(root, job_id), "rb") as stream:
            stream.seek(0, os.SEEK_END)
            position = stream.tell()
            data = b""
            # One newline more than `lines` guarantees `lines` *complete* lines above the partial
            # one this may have started in the middle of.
            while position > 0 and data.count(b"\n") <= lines:
                step = min(_TAIL_CHUNK_BYTES, position)
                position -= step
                stream.seek(position)
                data = stream.read(step) + data
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return "".join(text.splitlines(keepends=True)[-lines:])


def lease_is_free(root, job_id: str) -> bool | None:
    """Whether nobody currently holds `queue/leases/<id>.lock`: `True` free, `False` held,
    `None` if the question could not be answered at all.

    The lease is what says a job is *actually running*, and it is `flock`, so it disappears the
    instant its holder dies -- no timeout, no heartbeat, no stale-lock heuristic. The checkpoint
    lock cannot serve here: it is released as soon as diffusion ends, before the `.mp4`, `.wav` and
    report are written, so a probe landing in that window would call an almost-finished run dead.

    The probe is non-blocking on purpose. `reconcile` calls it while holding the queue lock, and a
    worker holding a lease takes the queue lock at the end of its run (`finish`); a blocking probe
    there would be a deadlock between the two locks rather than an answer.

    A missing lock file means nobody holds it -- `flock` cannot outlive the file's creator -- and
    is reported as free without creating the file: this is a question, not a claim. Any other
    `OSError` (the path is a directory, permissions are wrong) is `None` rather than a guess in
    either direction; `reconcile` documents what it does with that.
    """
    try:
        fd = os.open(lease_path(root, job_id), os.O_RDWR)
    except FileNotFoundError:
        return True
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        except OSError:
            return None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def reconcile(root) -> Reconciled:
    """Decide what every job still sitting in `running/` really is, and act on it.

    Called by the worker at the top of every loop iteration. The worker holds the single worker
    lock, so nothing in `running/` was started by *this* worker: each entry is either someone
    else's live run (its lease is held) or the wreckage of a run whose worker was killed.

    | lease  | marker | `<output_stem>.mp4` | action                                            |
    |--------|--------|---------------------|---------------------------------------------------|
    | held   | --     | --                  | leave in `running/`, report in `alive`            |
    | free   | yes    | --                  | `finish` by the marker's code and `finished_at`   |
    | free   | no     | yes                 | `finish(0, RESULT_RECOVERED_NOTE)`                |
    | free   | no     | no                  | back to `pending/`, to resume from its checkpoint |
    | unknown| as above (an unanswerable lease probe is treated exactly like a free one)         |

    The marker outranks the `.mp4` because it is the only row that knows a *non-zero* exit code: a
    run can fail after producing an `.mp4` from an earlier attempt at the same stem, and checking
    the artifact first would file that failure under `done/` with exit code 0.

    The third row exists for the window between the subprocess exiting and the marker being
    written. A finished `.mp4` only exists after a fully successful run, so its presence is a
    sounder signal than any timeout -- and re-running the job would overwrite precisely the result
    we are trying not to lose.

    An unanswerable lease probe is treated as free because the worker lock already proves no other
    worker of ours is alive; the alternative -- leaving the job in `running/` forever -- jams the
    queue permanently on a filesystem oddity.

    Everything happens under **one** exclusive `queue_lock` acquisition covering the whole table,
    which is why this calls `_finish_locked`/`_return_to_pending_locked` rather than the public
    `finish` (`queue_lock` is not reentrant). Splitting the section -- lock per job, or "read now,
    write later" -- reopens the window that let a cancelled job come back to life and a running
    job's file be overwritten in task 2.

    **No single file may stop the whole recovery.** Every `QueueError` a file can produce -- it
    does not parse, its shape is wrong, or the move refuses because the destination already exists
    (a duplicate id, a crash caught mid-transition; see `_rename_durably`) -- is collected into
    `conflicted` and the walk continues. Raising instead would not fail one job: `reconcile` runs
    at the top of every worker iteration, so the exception kills the worker, and the next
    `h3 worker` dies on the same file, and the queue stays dead until someone moves files around
    by hand. Stepping over a corrupt file while raising on a rename conflict -- which is what this
    used to do -- was the same policy applied in two opposite directions.

    A `root` that does not exist yields an empty result rather than an error, exactly as `scan`
    does: the queue directory not being there is "no jobs", and the HTTP layer that will call this
    should not have to tell a missing directory apart from an empty one.
    """
    root = Path(root)
    changed: list[Job] = []
    alive: list[Job] = []
    conflicted: list[Broken] = []
    empty = Reconciled(changed=changed, alive=alive, conflicted=conflicted)

    if not root.is_dir():
        return empty

    with queue_lock(root, exclusive=True):
        directory = root / "running"
        if not directory.is_dir():
            return empty
        for file in sorted(directory.glob("*.json")):
            try:
                job = _load_job_or_raise(file, "running")
                if lease_is_free(root, job.id) is False:
                    alive.append(job)
                    continue
                marker = _read_result_marker(root, job.id)
                if marker is not None:
                    changed.append(_finish_locked(
                        root, job.id, int(marker["exit_code"]),
                        read_log_tail(root, job.id), finished_at=marker.get("finished_at")))
                    continue
                if Path(f"{job.output_stem}.mp4").exists():
                    changed.append(_finish_locked(root, job.id, 0, RESULT_RECOVERED_NOTE))
                    continue
                changed.append(_return_to_pending_locked(root, job.id))
            except QueueError as exc:
                conflicted.append(Broken(path=str(file), error=f"{type(exc).__name__}: {exc}"))

    return Reconciled(changed=changed, alive=alive, conflicted=conflicted)


def update(root, job_id: str, args: list[str], note: str, dry_run_report: dict, estimate: dict,
           prompt_source: str | None = None, prompt_text: str | None = None) -> Job:
    """Replace a pending job's `args`, `note`, `estimate` and, if `prompt_text` is given, its
    prompt snapshot -- in place, at the same `pending/<id>.json` path. `id`, `created_at` and
    `priority` survive untouched, and so do `started_at`/`finished_at`/`exit_code`/`log_tail`:
    today a pending job never carries them, but startup reconciliation (task 4) can return an
    interrupted job to `pending/` *with* `started_at` still set (the run resumes from its
    checkpoint rather than restarting), and an edit must not silently erase that trace just
    because it happens to also touch the file. `output_stem` collision is checked the same way
    `submit` checks it, excluding this job's own current entry (see `_stem_taken`'s `exclude_id`)
    -- otherwise a job could never be re-submitted unchanged. Without `prompt_text`, the existing
    snapshot and `prompt_source`/`prompt_sha256` are left exactly as they were, mirroring `submit`.

    One exclusive lock acquisition covers everything: the pending/stem-conflict checks, the prompt
    snapshot rewrite (if any) and the final `write_json_durably`, exactly like `submit`. Nothing
    here is nested or nests anything else, so there is no window in which `claim` (or any other
    mutator) can observe this job mid-edit -- `queue_lock` simply makes it wait, the same way it
    makes two concurrent `submit`s wait on each other. Splitting this into "validate under one
    acquisition, write unlocked, verify under a second" was tried and rejected: it reopens exactly
    the race the module docstring describes (a write recreating a file in the directory the job
    was just claimed out of) for the sake of keeping the lock's *hold time* short, but the actual
    hold time here is two durable writes -- milliseconds, the same cost `submit` already pays under
    one acquisition. See `test_update_and_claim_are_serialized_end_to_end` for the forced-race test
    that a two-acquisition version cannot pass without either racing or timing out.
    """
    root = Path(root)
    output_stem = str(dry_run_report["output_stem"])
    pending = job_path(root, job_id, "pending")

    with queue_lock(root, exclusive=True):
        if not pending.exists():
            raise JobNotPending(f"job {job_id} is not pending")
        current = _load_job_or_raise(pending, "pending")
        if _stem_taken(root, output_stem, exclude_id=job_id):
            raise OutputStemConflict(output_stem)

        if prompt_text is not None:
            if "--prompt-file" not in args or args.index("--prompt-file") + 1 >= len(args):
                raise QueueError(
                    "prompt_text was given but args has no --prompt-file placeholder to repoint "
                    "at the snapshot"
                )

        args = list(args)
        new_prompt_source = current.prompt_source
        new_prompt_sha256 = current.prompt_sha256
        if prompt_text is not None:
            snapshot = prompt_path(root, job_id)
            write_text_durably(snapshot, prompt_text)
            new_prompt_source = prompt_source
            new_prompt_sha256 = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
            args[args.index("--prompt-file") + 1] = str(snapshot)

        job = Job(
            id=current.id, state="pending", created_at=current.created_at, args=args, note=note,
            prompt_source=new_prompt_source, prompt_sha256=new_prompt_sha256,
            output_stem=output_stem, estimate=dict(estimate), kind=current.kind,
            priority=current.priority,
            started_at=current.started_at, finished_at=current.finished_at,
            exit_code=current.exit_code, log_tail=current.log_tail,
        )
        write_json_durably(pending, _job_file_payload(job))

    return job


def move_to_front(root, job_id: str) -> Job:
    """Set a pending job's `priority` to one more than the current maximum among pending jobs, so
    it is claimed before everything already waiting -- the queue's own claim order is
    `(-priority, id)` (see `claim`).
    """
    root = Path(root)
    with queue_lock(root, exclusive=True):
        pending = job_path(root, job_id, "pending")
        if not pending.exists():
            raise JobNotPending(f"job {job_id} is not pending")
        current = _load_job_or_raise(pending, "pending")
        new_priority = _max_pending_priority(root) + 1
        job = Job(**{**current.as_dict(), "priority": new_priority})
        write_json_durably(pending, _job_file_payload(job))
        return job


def cancel(root, job_id: str) -> Job:
    """Remove a pending job and its prompt snapshot, if it has one. Returns the removed `Job`
    (still carrying `state="pending"`, what it was up to the moment it stopped existing) so a
    caller can report what was cancelled.
    """
    root = Path(root)
    with queue_lock(root, exclusive=True):
        pending = job_path(root, job_id, "pending")
        if not pending.exists():
            raise JobNotPending(f"job {job_id} is not pending")
        current = _load_job_or_raise(pending, "pending")
        pending.unlink()
        prompt_path(root, job_id).unlink(missing_ok=True)
        return current
