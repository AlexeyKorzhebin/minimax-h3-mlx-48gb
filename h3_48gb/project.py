"""On-disk state for one project: layout, durable writes, the project lock, and the stage/scene/
track mutations the worker, the web API and the UI (tasks 3-7) read and write through.

A "project" (see `docs/superpowers/specs/2026-08-18-projects-design.md`) produces long-form
content -- a scripted multi-scene video, a clip cut to a song, or a bare mp3 -- out of ordinary
generate/song jobs on the *existing* queue (`h3_48gb.queue`). This module owns none of that
execution: a scene only ever carries a `job_id` pointing at a job the queue already knows about,
never a second scheduling mechanism. What this module owns is the *durable record* of where a
project stands -- which stage is approved, which scene is done, where its clip and its automatic
keyframe landed -- because a project can run for hours (a 3-minute video is 18-20 sequential clips)
and both the worker and the web server restart independently of it. "Переживает рестарт" (the
brief's own words) means exactly this: nothing about a project's state may live only in a process's
memory.

**Layout.** `create_project` claims `<outdir>/projects/<YYYYMMDD-HHMM>-<slug>/`, mirroring
`queue.py`'s job subdirectories (`_relocate_to_job_subdir`) in spirit -- minute precision, a slugged
human-readable component -- but claimed by `Path.mkdir()` (atomically refuses if the name is
already taken) rather than `queue.py`'s `O_CREAT|O_EXCL` file trick, because here the directory
*is* the claim: `project.json` lives inside it, one file, not one of four state directories a
rename moves a file between. A colliding name (two projects titled the same thing in the same
minute) gets `-2`, `-3`, ... appended -- see `create_project` -- rather than a random suffix, so the
path stays exactly the shape the design spec documents.

**Durable writes.** `write_json_durably`/`write_text_durably` are imported from `h3_48gb.queue`
rather than reimplemented: both are already fully generic (path + payload, no notion of a queue
root) and already proven crash-safe by `test_queue.py`'s own fault-injection tests -- duplicating
that logic here would only create a second place for the same bug to hide. Neither pulls in `mlx`
(see `queue.py`'s own module docstring), so importing it here does not compromise this module's own
"no mlx" property -- see `test_project_module_does_not_import_mlx`.

**The project lock.** One `flock` per project, at `<project_dir>/project.lock` -- deliberately
per-*project*, not a single lock for every project under `<outdir>/projects/`: two unrelated
projects being edited at once (a human on one project's page, a worker advancing another's scenes)
must not serialize on each other, the way `queue.py`'s single `queue.lock` correctly does serialize
unrelated *jobs* (they share one pending/running/done/failed state machine; two projects share
nothing but a parent directory). Every one of the four documented mutators
(`approve_stage`/`set_scene_status`/`next_pending_scene`/`invalidate_scene_chain`) re-reads
`project.json` from disk *inside* the lock before touching it, exactly like `queue.py`'s `update`
re-reads a pending job under the queue lock: a `Project` object held by one caller (the web server,
say) must not silently clobber a change the worker wrote through a different `Project` object
between the caller's last read and this write. `Project.save()` is the one exception -- see its own
docstring for why a blind overwrite is the right contract there.

Messages a human reads are Russian; comments and docstrings are English -- same split as the rest
of the package (see `queue.py`'s module docstring).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import warnings
from datetime import datetime
from pathlib import Path

from h3_48gb.queue import write_json_durably  # noqa: F401 -- see "Durable writes" above

#: The three products this fundament serves (design spec, "Суть"): a scripted multi-scene video, a
#: video cut to a song's sections, or a bare mp3. Nothing else this module does depends on which
#: one a project is, except the default `assembly.audio_mode` a fresh project starts with -- see
#: `_DEFAULT_AUDIO_MODE`.
PROJECT_KINDS = ("video", "clip", "song")

#: The four stages of the pipeline (design spec, "Этапы и гейты") every project's `stages` dict
#: carries a status for, regardless of `kind`. Not every kind's flow visits every stage -- a `song`
#: project never reaches `assembly`, and only `clip`/`song` ever populate `track` -- but a fixed,
#: uniform key set means a caller (the web page, task 6) never has to special-case which keys exist
#: for which `kind`; an unused stage simply stays `"draft"` forever.
STAGE_NAMES = ("script", "track", "scenes", "assembly")

#: What a stage's status can be. `draft` (nothing produced yet), `awaiting_approval` (produced,
#: waiting on the gate -- "Утвердить сценарий" / "Утвердить трек"), `approved` (gate passed),
#: `running` (the automatic part -- clips, assembly -- is in flight), `done`, `failed`.
STAGE_STATUSES = ("draft", "awaiting_approval", "approved", "running", "done", "failed")

#: What one scene's status can be -- deliberately the same four words as `queue.QUEUE_STATES`
#: (minus `leases`/etc, which are queue-internal), because a scene's status *is* the status of the
#: job it is waiting on, once one exists: not started (`pending`), the queue is running it
#: (`running`), or it finished one way or the other (`done`/`failed`). Scenes are not individually
#: gated the way stages are -- there is no `awaiting_approval` for a single scene -- so this is a
#: narrower set than `STAGE_STATUSES` on purpose.
SCENE_STATUSES = ("pending", "running", "done", "failed")

#: `assembly.audio_mode` (design spec, "Сборка"): the final render's audio track is either the
#: song (`song`), the clips' own diegetic sound with the song mixed in low underneath (`mix`), or
#: just the clips' own sound with no song at all (`clips`).
ASSEMBLY_AUDIO_MODES = ("song", "mix", "clips")

#: `create_project`'s default `assembly.audio_mode` per `kind`. `clip` defaults to `"song"` --
#: the design spec is explicit that a clip-on-a-song's default is "только песня" (song only,
#: diegetic clip sound dropped). `video`'s main case has no song at all, so `"clips"` (its own
#: diegetic sound) is the only default that produces anything. `song` never reaches the assembly
#: stage (a bare mp3 *is* the product -- design spec, "для kind=song проект на этом завершён"), so
#: its default is arbitrary and simply never read; `"clips"` keeps it a valid member of
#: `ASSEMBLY_AUDIO_MODES` rather than carrying a value nothing else in this enum recognizes.
_DEFAULT_AUDIO_MODE = {"video": "clips", "clip": "song", "song": "clips"}

PROJECT_FILENAME = "project.json"
PROJECT_LOCK_NAME = "project.lock"

#: Every top-level key `create_project` writes and `_read_data` requires. A file missing one of
#: these -- truncated mid-write despite the durable-write protocol (e.g. hand-edited, or written by
#: some future, buggier version of this module) -- is treated as corrupt, the same as unparseable
#: JSON: see `_read_data` and `list_projects`.
_REQUIRED_FIELDS = ("id", "kind", "title", "created_at", "stages", "scenes", "track", "assembly")


class ProjectError(Exception):
    """Base class for every refusal this module raises."""


class ProjectNotFound(ProjectError):
    """Raised by `load_project` when `path` does not hold a readable, well-shaped project.json."""


class UnknownStage(ProjectError):
    """Raised by `approve_stage` for a name outside `STAGE_NAMES`."""


class UnknownScene(ProjectError):
    """Raised by `set_scene_status`/`invalidate_scene_chain` for an `idx` no scene carries."""


def _now() -> str:
    """Indirected so `create_project` can be driven with a frozen clock in tests, exactly like
    `queue._now`.
    """
    return datetime.now().isoformat(timespec="seconds")


def _dir_stamp(created_at: str) -> str:
    """`created_at` as `YYYYmmdd-HHMM` -- the timestamp half of a project's directory name.
    Minute precision, matching `queue._dir_stamp`: a human reading directory names does not need
    second-level uniqueness, and `create_project`'s own collision loop (not this string) is what
    actually guarantees the directory is claimed.
    """
    return datetime.fromisoformat(created_at).strftime("%Y%m%d-%H%M")


def _slug(title: str | None) -> str:
    """`title` reduced to `[a-z0-9-]`, with whitespace/underscores folded to a single `-` first so
    a human-typed title like "My First Video" reads as `my-first-video`, not `myfirstvideo`.
    `"project"` if there is no title or nothing survives the filter.
    """
    if not title:
        return "project"
    lowered = re.sub(r"[\s_]+", "-", title.strip().lower())
    lowered = re.sub(r"[^a-z0-9-]", "", lowered)
    lowered = re.sub(r"-+", "-", lowered).strip("-")
    return lowered or "project"


def _empty_track() -> dict:
    return {
        "lyrics": None,
        "caption": None,
        "wav": None,
        "mp3": None,
        "mastered_mp3": None,
        "sections": [],
        "status": "draft",
    }


@contextlib.contextmanager
def _project_lock(project_dir, exclusive: bool):
    """Hold `<project_dir>/project.lock` for the duration of the `with` block: exclusively
    (`LOCK_EX`) for every mutation, shared (`LOCK_SH`) for a read-only pass (`load_project`,
    `list_projects`). Same shape as `queue.queue_lock`, scoped to one project's own directory
    instead of one queue root -- see the module docstring's "The project lock" section for why that
    scope is the right one. Blocks until available rather than failing fast, for the same reason
    `queue_lock` does: every caller here is waiting on milliseconds of file I/O from another such
    caller, never on an hours-long generation run.
    """
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    lock_file = project_dir / PROJECT_LOCK_NAME
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _validate_shape(data, path) -> dict:
    """`data` if it looks like a project (a dict with every field `_REQUIRED_FIELDS` names),
    otherwise `ProjectNotFound` -- the same refusal `_read_data` raises for unparseable JSON, so a
    caller (`list_projects`) can treat "not JSON" and "JSON but not a project" identically.
    """
    if not isinstance(data, dict) or any(field not in data for field in _REQUIRED_FIELDS):
        raise ProjectNotFound(f"{path} does not have the shape of a project.json")
    return data


def _read_data(path: Path) -> dict:
    """Parse `path` into a project's raw dict, translating every way a hand-inspected or
    interrupted file can fail into `ProjectNotFound` -- never a bare `OSError`/`ValueError` -- so
    `list_projects` can catch exactly one exception type and `load_project` raises the same family
    other callers already match `ProjectError` against.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectNotFound(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ProjectNotFound(f"{path} is not valid JSON: {exc}") from exc
    return _validate_shape(data, path)


def _find_scene(scenes: list[dict], idx: int) -> dict:
    for scene in scenes:
        if scene["idx"] == idx:
            return scene
    raise UnknownScene(f"no scene with idx={idx}")


class Project:
    """One project, exactly as it is stored on disk. Every field the design spec and the task
    brief document is a plain, directly-mutable attribute (`stages`, `scenes`, `track`,
    `assembly`) rather than something hidden behind a getter: a caller populating a fresh
    project's scenes and track after the script gate (task 3, out of this module's scope) has
    nothing to call here but plain assignment followed by `save()` -- see `save`'s docstring.

    The four methods the task brief names as this module's contract --
    `approve_stage`/`set_scene_status`/`next_pending_scene`/`invalidate_scene_chain` -- are the
    ones with a race to actually guard against (two processes advancing the same project at once),
    so unlike plain attribute mutation they re-read `project.json` from disk under the project
    lock before acting, and write the result straight back before returning. Each returns `self`,
    updated in place, so a caller can chain or simply ignore the return value.
    """

    def __init__(self, path, data: dict):
        self.path = Path(path)
        self._apply(data)

    def _apply(self, data: dict) -> None:
        self.id = data["id"]
        self.kind = data["kind"]
        self.title = data["title"]
        self.created_at = data["created_at"]
        self.stages = dict(data["stages"])
        self.scenes = [dict(scene) for scene in data["scenes"]]
        self.track = dict(data["track"])
        self.assembly = dict(data["assembly"])

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "created_at": self.created_at,
            "stages": dict(self.stages),
            "scenes": [dict(scene) for scene in self.scenes],
            "track": dict(self.track),
            "assembly": dict(self.assembly),
        }

    def save(self) -> None:
        """Durably overwrite `project.json` with this object's current in-memory state --
        `tmp` write, `fsync`, `os.replace`, `fsync` the directory (`write_json_durably`), under an
        exclusive hold of this project's own lock.

        **Deliberately a blind overwrite, not a read-modify-write.** `save` exists for a caller
        that already knows it is the sole writer of a whole batch of fields at once -- most
        notably the script gate (task 3) populating a freshly created project's `scenes`/`track`
        for the first time, where there is nothing on disk yet to race against. The four
        documented mutators below are the ones built for the read-modify-write shape, because
        *they* are the ones a concurrent second writer (the worker, mid-project) is realistically
        racing.
        """
        with _project_lock(self.path.parent, exclusive=True):
            write_json_durably(self.path, self.as_dict())

    def approve_stage(self, name: str) -> "Project":
        """Set stage `name`'s status to `"approved"` -- the gate button ("Утвердить сценарий",
        "Утвердить трек") in full. No precondition is enforced on the stage's *current* status:
        which transitions are legal (e.g. only from `awaiting_approval`) is a decision for the web
        layer that renders the gate buttons (task 6), not for the storage layer -- enforcing it
        here would freeze that policy into this module before any caller of it exists.
        """
        if name not in STAGE_NAMES:
            raise UnknownStage(f"unknown stage {name!r}, expected one of {STAGE_NAMES}")
        with _project_lock(self.path.parent, exclusive=True):
            data = _read_data(self.path)
            data["stages"][name] = "approved"
            write_json_durably(self.path, data)
            self._apply(data)
        return self

    def set_scene_status(self, idx: int, status: str, *, job_id: str | None = None,
                          clip_path: str | None = None, keyframe_path: str | None = None
                          ) -> "Project":
        """Set scene `idx`'s status, and any of `job_id`/`clip_path`/`keyframe_path` that are
        given. A field left as `None` is left exactly as it already was on disk -- this is a
        partial update, matching how the worker actually learns these facts one at a time (a
        `job_id` the moment it submits, a `clip_path` only once the job is `done`); it is not how
        a scene gets its fields *cleared* -- that is `invalidate_scene_chain`'s job, which sets
        them to `None` explicitly rather than by omission.
        """
        if status not in SCENE_STATUSES:
            raise ProjectError(f"unknown scene status {status!r}, expected one of {SCENE_STATUSES}")
        with _project_lock(self.path.parent, exclusive=True):
            data = _read_data(self.path)
            scene = _find_scene(data["scenes"], idx)
            scene["status"] = status
            if job_id is not None:
                scene["job_id"] = job_id
            if clip_path is not None:
                scene["clip_path"] = clip_path
            if keyframe_path is not None:
                scene["keyframe_path"] = keyframe_path
            write_json_durably(self.path, data)
            self._apply(data)
        return self

    def next_pending_scene(self) -> dict | None:
        """The one scene the worker should submit next, or `None` if nothing is ready.

        Scenes are strictly sequential -- automatic keyframes need the *actual* last frame of the
        previous clip, not a guess (design spec, "Клипы"). Walking scenes in `idx` order: a `done`
        scene lets the walk continue past it; the first `pending` scene found is the answer;
        anything else (`running` or `failed`) means the chain is not caught up yet, and `None` is
        the answer for every scene from there on, including scenes after it that are themselves
        `pending` -- a `pending` scene sitting behind a `failed` one is not next, it is blocked,
        and stays blocked until a retry (or `invalidate_scene_chain`) clears the scene ahead of it.

        Read-only, but still re-reads under a *shared* lock rather than trusting this object's own
        in-memory `scenes` -- the same reasoning `queue.scan` uses its shared lock for: a second
        `Project` object (the worker's own, say) may have advanced a scene since this one was last
        loaded.
        """
        with _project_lock(self.path.parent, exclusive=False):
            data = _read_data(self.path)
        self._apply(data)
        for scene in sorted(data["scenes"], key=lambda s: s["idx"]):
            if scene["status"] == "done":
                continue
            if scene["status"] == "pending":
                return dict(scene)
            return None
        return None

    def invalidate_scene_chain(self, idx: int) -> "Project":
        """Reset scene `idx` and every scene after it to `pending`, clearing each one's `job_id`,
        `clip_path` and `keyframe_path`. This is the "пересчёт отдельной сцены" button (design
        spec, "Клипы"): every scene from `idx` onward carries an automatic keyframe derived,
        transitively, from scene `idx`'s own clip, so reusing any of them after scene `idx`
        changes would silently keep stale downstream clips -- clearing the whole tail is the
        "честное предупреждение" the design spec calls for, not a convenience.
        """
        with _project_lock(self.path.parent, exclusive=True):
            data = _read_data(self.path)
            _find_scene(data["scenes"], idx)  # raises UnknownScene if idx does not exist
            for scene in data["scenes"]:
                if scene["idx"] >= idx:
                    scene["status"] = "pending"
                    scene["job_id"] = None
                    scene["clip_path"] = None
                    scene["keyframe_path"] = None
            write_json_durably(self.path, data)
            self._apply(data)
        return self


def create_project(outdir, kind: str, title: str, now=None) -> Project:
    """Claim `<outdir>/projects/<YYYYMMDD-HHMM>-<slug>/`, write a fresh `project.json` into it,
    and return the `Project`.

    Every field the model documents starts at its emptiest legal value: every stage `"draft"`, no
    scenes, an empty track, and `assembly.audio_mode` defaulted from `kind` (`_DEFAULT_AUDIO_MODE`)
    with no `final_path` yet. Populating `scenes`/`track` for a real project (after the script gate
    resolves them) is a later task's job (task 3, the chat integration), done by assigning to
    `project.scenes`/`project.track` and calling `save()` -- see `Project.save`'s docstring for why
    that is a plain overwrite rather than one of the four locked mutators.

    A colliding directory name -- two projects titled the same thing created in the same minute --
    gets `-2`, `-3`, ... appended, tried via `Path.mkdir()`'s own atomic "already exists" refusal
    (no `O_CREAT|O_EXCL` file trick needed, the way `queue.submit` needs one for a job id: here the
    directory itself, not a file inside it, is the thing being claimed).
    """
    if kind not in PROJECT_KINDS:
        raise ProjectError(f"unknown project kind {kind!r}, expected one of {PROJECT_KINDS}")

    projects_root = Path(outdir) / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)
    created_at = now() if now is not None else _now()
    stamp = _dir_stamp(created_at)
    slug = _slug(title)

    attempt = 0
    while True:
        name = f"{stamp}-{slug}" if attempt == 0 else f"{stamp}-{slug}-{attempt + 1}"
        project_dir = projects_root / name
        try:
            project_dir.mkdir()
            break
        except FileExistsError:
            attempt += 1
            continue

    data = {
        "id": name,
        "kind": kind,
        "title": title,
        "created_at": created_at,
        "stages": {stage: "draft" for stage in STAGE_NAMES},
        "scenes": [],
        "track": _empty_track(),
        "assembly": {"audio_mode": _DEFAULT_AUDIO_MODE[kind], "final_path": None},
    }
    project = Project(project_dir / PROJECT_FILENAME, data)
    project.save()
    return project


def load_project(path) -> Project:
    """Read one project from `path`, which may be either its `project.json` file or its directory.

    Raises `ProjectNotFound` (a `ProjectError`) if there is nothing readable and well-shaped there
    -- never a bare `FileNotFoundError`/`json.JSONDecodeError`, matching this module's own
    exception family the way `queue.py`'s mutators match `QueueError`.
    """
    path = Path(path)
    if path.is_dir():
        path = path / PROJECT_FILENAME
    with _project_lock(path.parent, exclusive=False):
        data = _read_data(path)
    return Project(path, data)


def list_projects(outdir) -> list[Project]:
    """Every project under `<outdir>/projects/`, sorted by directory name (which sorts by
    creation time first, since every name starts with `_dir_stamp`'s `YYYYmmdd-HHMM`).

    A subdirectory that is not readable as a project -- corrupt JSON, or JSON missing a required
    field -- is **skipped with a `warnings.warn`, not raised**: one broken project must not take
    down a page meant to list every *other* one. A subdirectory with no `project.json` at all is
    skipped silently, not warned about -- that is `create_project`'s own claimed-but-not-yet-
    written directory, a normal (if narrow) window, not corruption.

    `<outdir>/projects` not existing yet yields `[]`, matching `queue.scan`'s "a missing root is
    zero jobs, not an error" contract.
    """
    projects_root = Path(outdir) / "projects"
    if not projects_root.is_dir():
        return []

    projects: list[Project] = []
    for entry in sorted(projects_root.iterdir()):
        if not entry.is_dir():
            continue
        path = entry / PROJECT_FILENAME
        if not path.is_file():
            continue
        try:
            with _project_lock(entry, exclusive=False):
                data = _read_data(path)
        except ProjectNotFound as exc:
            warnings.warn(f"skipping corrupt project at {path}: {exc}", stacklevel=2)
            continue
        projects.append(Project(path, data))
    return projects
