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
nothing but a parent directory). Every locked mutator this module documents
(`approve_stage`/`set_stage_status`/`set_scene_status`/`claim_next_scene`/`invalidate_scene_chain`/
`update_track`/`update_assembly`) re-reads `project.json` from disk *inside* the lock before
touching it, exactly like `queue.py`'s `update` re-reads a pending job under the queue lock: a
`Project` object held by one caller (the web server, say) must not silently clobber a change the
worker wrote through a different `Project` object between the caller's last read and this write.
`next_pending_scene` is a deliberate exception -- a read-only preview, not a claim, so it neither
takes the exclusive lock nor re-applies its re-read back onto `self`; see its own docstring, and
`claim_next_scene`'s, for the atomic claim `next_pending_scene` itself cannot safely be used for.
`Project.save()` is the other exception -- see its own docstring for why a blind overwrite is the
right contract there.

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

#: The fields `update_assembly(**fields)` accepts -- exactly `assembly`'s own two keys, see
#: `create_project`. Kept as an explicit tuple, not derived from a live `assembly` dict, since
#: there is no `_empty_assembly()` the way there is an `_empty_track()`.
_ASSEMBLY_FIELDS = ("audio_mode", "final_path")

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

#: `set_scene_status`'s own "omitted" sentinel for `job_id` -- distinct from `None`, which that
#: parameter now treats as an explicit clear (C1, fix round 1, 2026-08-18 review; see the method's
#: own docstring). A private module-level singleton rather than an inline `object()` default so
#: the identity check inside the method reads the same sentinel every call.
_UNSET_JOB_ID = object()

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
    a human-typed title like "My First Video" reads as `my-first-video`, not `myfirstvideo`, and
    Cyrillic transliterated (`_transliterate`) before the filter so "Мой ролик" reads as
    `moy-rolik`, not the empty string every one of its characters would otherwise strip down to.
    `"project"` if there is no title or nothing survives the filter (e.g. a title in a script
    `_CYRILLIC_TO_LATIN` does not cover either).
    """
    if not title:
        return "project"
    lowered = re.sub(r"[\s_]+", "-", title.strip().lower())
    lowered = _transliterate(lowered)
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
        # `status` deliberately reuses no fixed enum today (unlike `stages`/`scenes`) -- task 3's
        # Whisper gate ("трек короче лирики, увеличь длительность") may need a value outside
        # `STAGE_STATUSES`'s six words. If that turns out false, fold this into a real
        # `TRACK_STATUSES` tuple and validate it the same way `set_stage_status` validates
        # `STAGE_STATUSES` -- do not invent the enum speculatively here before task 3 knows what
        # it needs.
        "status": "draft",
        # Task 3 (design spec addendum, "импортированный трек"): whether this track's audio comes
        # from Music3 generation or was uploaded ready-made. `"generate"` is the default because
        # every track before this addendum was one -- a fresh project never has to think about the
        # field unless it is actually an import. `"import"` means the queue's song job skips
        # generation and mastering entirely and only runs the Whisper alignment pass (see
        # `h3_48gb.songrun.align_track`, `h3_48gb.worker`'s song-job dispatch).
        "source": "generate",
        # Task 3: `h3_48gb.songrun.SongResult.undersung` copied straight onto the track once a song
        # job finishes, so the track gate (task 6) can show the "трек короче лирики" warning next
        # to a track that is otherwise a complete success (mp3s exist, the job is `done`) -- an
        # `undersung` take is a warning on an approved-looking result, not a failure.
        "undersung": False,
        # Fix round 1, I1 (2026-08-18 review): the seed a `kind="song"` generation used. `None`
        # until a song job actually runs one -- the worker (`h3_48gb.worker._run_song_job`) reads
        # this before generating, and if it is still `None` picks a random one itself
        # (`random.randrange(2**31)`) and writes the value it actually used straight back here,
        # in the same `update_track` call as the rest of the job's results. This is what makes a
        # duplicate ("Копия" on a `done` song job) reproducible: without recording the seed that
        # was actually rolled, a retry of an unset seed would roll a *different* random one and
        # never be able to recreate a take a human liked. Never touched for `track.source ==
        # "import"` -- there is no generation, so no seed to record.
        "seed": None,
        # Fix round 1, I4 (2026-08-18 review): how long the track actually plays, in seconds --
        # `songrun.SongResult.duration` copied straight onto the track once a song job finishes,
        # for `run_song` (Music3's own actual generated length) exactly as for `align_track` (an
        # imported mp3's real length, via `ffprobe`, not a Whisper segment's own end timestamp --
        # see `align_track`'s docstring for why the earlier version of that function undercounted
        # a track with a wordless tail). `None` until a song job has actually produced one.
        "duration": None,
        # Task 1 ("Сюжет клипа" wave, "авто-лирика"): a `track.source == "import"` project may
        # import an mp3 with no reference lyrics at all -- the worker's song-job dispatch then
        # calls `songrun.align_track(..., lyrics=None)`, which cannot build `sections` (nothing to
        # fuzzy-match against) but still transcribes the audio. `lyrics_auto` is that raw
        # transcript (`songrun.SongResult.transcript`, the same full-text field a lyrics-matched
        # run also produces but never had anywhere to land before this) -- a human-readable stand-
        # in for lyrics a clip's future scenario step (Task 2) reads by *meaning*, not for karaoke
        # accuracy (design spec: "ослышки Whisper допустимы"). `None` until an auto-transcribed
        # song job has actually produced one; stays `None` forever for a track whose lyrics were
        # already known (matched sections exist instead) or that was never imported at all.
        "lyrics_auto": None,
        # Task 1: `songrun.SongResult.raw_segments` copied straight onto the track in the same
        # `lyrics=None` case as `lyrics_auto` above -- `[{"start", "end", "text"}, ...]`, one entry
        # per Whisper segment, timestamps and all. This is what a future scenario LLM call (Task 2)
        # groups into sections itself, since `sections` (built from *known* lyrics) does not exist
        # for this track. Can run to hundreds of entries on a long song; `project.json` already
        # tolerates large scene lists the same way (module docstring), so no special handling here.
        # `None` for every track that is not this specific auto-transcribed-import case.
        "raw_segments": None,
    }


#: The fields `update_track(**fields)` accepts -- exactly `_empty_track()`'s own keys, derived
#: from it rather than duplicated so the two can never silently drift apart.
_TRACK_FIELDS = tuple(_empty_track())


#: `_slug`'s Russian-to-Latin table, applied before the `[a-z0-9-]` filter so a Cyrillic title
#: (a human is not required to type an English one) does not collapse to the `"project"` fallback
#: -- every Cyrillic codepoint the filter would otherwise strip is spelled out in Latin letters
#: first. Deliberately a hand-written dict, not a dependency (`unidecode`/`transliterate`/etc):
#: this is the one, fixed alphabet the filter needs covered, and pulling in a library for a
#: 33-entry table would be the tail wagging the dog -- see the module docstring's "no mlx"
#: discipline extended to "no needless dependencies at all" for this small a need. Common
#: practical-transcription scheme (е/э→e, й→y, ц→ts, ч→ch, ш→sh, щ→shch, ъ/ь→dropped, ю→yu, я→ya)
#: -- the same shape most Russian URL slugs on the web already use, so a generated slug reads as
#: familiar rather than invented.
_CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _transliterate(text: str) -> str:
    """`text` (already lowercased) with every Cyrillic character in `_CYRILLIC_TO_LATIN` replaced
    by its Latin spelling; anything not in the table (already-Latin letters, digits, the `-`
    `_slug` folded whitespace into) passes through untouched, for the `[^a-z0-9-]` filter after
    this to strip.
    """
    return "".join(_CYRILLIC_TO_LATIN.get(ch, ch) for ch in text)


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
    """`data` if it looks like a project -- a dict with every field `_REQUIRED_FIELDS` names, AND
    the four container fields holding the type the rest of this module assumes without checking
    again -- otherwise `ProjectNotFound`, the same refusal `_read_data` raises for unparseable
    JSON, so a caller (`list_projects`) can treat "not JSON", "JSON but not a project" and "JSON,
    shaped like a project, but with a field of the wrong type" identically.

    The type checks exist because presence alone is not shape: `{"scenes": "notalist", ...}` has
    every required key, but `Project._apply`'s `[dict(scene) for scene in data["scenes"]]` would
    iterate the *string* `"notalist"` character by character and blow up on `dict("n")` with a
    bare, uncaught `TypeError` -- a corrupt file crashing `list_projects` for every other, healthy
    project in the same `outdir`, not the "skip with a warning" contract promises. Checking
    `stages`/`track`/`assembly` are objects and `scenes` is an array of objects (not, e.g., a list
    of bare strings or numbers) here turns that crash into one `ProjectNotFound`, caught by
    `list_projects` like any other corrupt file. Deeper still -- a scene missing its own `idx` key,
    say -- is not re-validated field by field here; `_find_scene`/`_next_ready_scene` catch that
    layer with their own `try/except (KeyError, TypeError)`, converting it to a `ProjectError`
    rather than letting a bare `KeyError` escape a mutator call, see there.
    """
    if not isinstance(data, dict) or any(field not in data for field in _REQUIRED_FIELDS):
        raise ProjectNotFound(f"{path} does not have the shape of a project.json")
    if not isinstance(data["stages"], dict):
        raise ProjectNotFound(
            f"{path}: 'stages' must be an object, got {type(data['stages']).__name__}")
    if not isinstance(data["scenes"], list):
        raise ProjectNotFound(
            f"{path}: 'scenes' must be an array, got {type(data['scenes']).__name__}")
    if not all(isinstance(scene, dict) for scene in data["scenes"]):
        raise ProjectNotFound(f"{path}: every element of 'scenes' must be an object")
    if not isinstance(data["track"], dict):
        raise ProjectNotFound(
            f"{path}: 'track' must be an object, got {type(data['track']).__name__}")
    if not isinstance(data["assembly"], dict):
        raise ProjectNotFound(
            f"{path}: 'assembly' must be an object, got {type(data['assembly']).__name__}")
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
    """The scene dict with `idx == idx` inside `scenes`, or `UnknownScene`. A scene missing its own
    `idx` key entirely (on-disk corruption `_validate_shape` does not check this deep, see its own
    docstring) raises `ProjectError`, not a bare `KeyError` -- a caller catching this module's own
    exception family (the web layer, task 6) must not also have to catch built-in exceptions to be
    safe against a hand-edited or half-written file.
    """
    for scene in scenes:
        try:
            scene_idx = scene["idx"]
        except KeyError as exc:
            raise ProjectError(f"malformed scene entry (missing 'idx'): {scene!r}") from exc
        if scene_idx == idx:
            return scene
    raise UnknownScene(f"no scene with idx={idx}")


def _next_ready_scene(scenes: list[dict]) -> dict | None:
    """The scene `next_pending_scene`/`claim_next_scene` agree is next, or `None` -- the shared
    sequential-dependency walk both need (see `next_pending_scene`'s own docstring for the policy:
    a `done` scene lets the walk continue, the first `pending` scene found is the answer, anything
    else -- `running`/`failed` -- means the chain is not caught up and every scene from there on is
    `None`, including a `pending` one sitting behind a `failed` one).

    Returns the actual dict living inside `scenes` (not a copy), so a caller that wants to mutate
    it in place under its own lock hold (`claim_next_scene`) can, while a caller that only wants a
    snapshot (`next_pending_scene`) copies it itself before returning.

    Raises `ProjectError`, not a bare `KeyError`/`TypeError`, for a scene missing `idx`/`status` or
    an `idx` that cannot be compared -- the same discipline `_find_scene` follows, for the same
    reason: this is on-disk corruption `_validate_shape` does not check this deep, see its own
    docstring.
    """
    try:
        ordered = sorted(scenes, key=lambda scene: scene["idx"])
    except (KeyError, TypeError) as exc:
        raise ProjectError(f"malformed scene entry: {exc}") from exc
    for scene in ordered:
        try:
            status = scene["status"]
        except KeyError as exc:
            raise ProjectError(f"malformed scene entry (missing 'status'): {scene!r}") from exc
        if status == "done":
            continue
        if status == "pending":
            return scene
        return None
    return None


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
        # Every top-level key `data` carries beyond `_REQUIRED_FIELDS` (a field a later task, or a
        # hand-edit, added that this module does not itself know about) is kept verbatim in
        # `_extra` rather than dropped, so `as_dict` -- and therefore `save`/every locked mutator's
        # write-back -- round-trips it instead of silently deleting it. See `as_dict`.
        self._extra = {key: value for key, value in data.items() if key not in _REQUIRED_FIELDS}
        self.id = data["id"]
        self.kind = data["kind"]
        self.title = data["title"]
        self.created_at = data["created_at"]
        self.stages = dict(data["stages"])
        self.scenes = [dict(scene) for scene in data["scenes"]]
        self.track = dict(data["track"])
        self.assembly = dict(data["assembly"])

    def as_dict(self) -> dict:
        """This project as a plain dict, ready for `json.dumps` -- every field this module
        documents, plus, verbatim, any top-level field it does not (`self._extra`, populated by
        `_apply` from whatever `data` last carried beyond `_REQUIRED_FIELDS`).

        Starts from a **copy of `self._extra`**, with the documented fields written on top, rather
        than the other way around: a documented field always wins over a same-named stray one (it
        cannot happen today since `_REQUIRED_FIELDS` and `_extra`'s keys are disjoint by
        construction, but "known fields are authoritative" is the right rule to state regardless).
        This is what lets an unrecognized top-level key -- one a future task's version of this
        module wrote, or a human hand-added while inspecting a file -- survive a `Project` object
        loading it, mutating something else entirely, and writing the whole file back out, instead
        of that field being quietly dropped on the next `save()`/locked mutator write.
        """
        result = dict(self._extra)
        result.update({
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "created_at": self.created_at,
            "stages": dict(self.stages),
            "scenes": [dict(scene) for scene in self.scenes],
            "track": dict(self.track),
            "assembly": dict(self.assembly),
        })
        return result

    def save(self) -> None:
        """Durably overwrite `project.json` with this object's current in-memory state --
        `tmp` write, `fsync`, `os.replace`, `fsync` the directory (`write_json_durably`), under an
        exclusive hold of this project's own lock.

        **Deliberately a blind overwrite, not a read-modify-write -- scope this narrowly: use it
        only for the primary population of a freshly created project.** `save` exists for a caller
        that already knows it is the sole writer of a whole batch of fields at once -- most
        notably the script gate (task 3) populating a freshly created project's `scenes`/`track`
        for the first time, where there is nothing on disk yet to race against, and
        `create_project` populating the initial skeleton. It is *not* the right way to update a
        project that may already be in use: once other writers exist, updating `stages`, `track`
        or `assembly` piecemeal belongs to `set_stage_status`/`update_track`/`update_assembly`
        (and a single scene to `set_scene_status`/`claim_next_scene`/`invalidate_scene_chain`) --
        those re-read `project.json` from disk under the lock before writing, so they cannot
        clobber a concurrent writer's change the way a blind `save()` can.
        """
        with _project_lock(self.path.parent, exclusive=True):
            write_json_durably(self.path, self.as_dict())

    def approve_stage(self, name: str) -> "Project":
        """Set stage `name`'s status to `"approved"` -- the gate button ("Утвердить сценарий",
        "Утвердить трек") in full. No precondition is enforced on the stage's *current* status:
        which transitions are legal (e.g. only from `awaiting_approval`) is a decision for the web
        layer that renders the gate buttons (task 6), not for the storage layer -- enforcing it
        here would freeze that policy into this module before any caller of it exists.

        A thin, fixed-destination convenience over `set_stage_status(name, "approved")` -- see
        that method for a stage transition to anything else (`awaiting_approval`/`running`/`done`/
        `failed`), which the worker (task 3) needs and this narrower method deliberately does not
        offer.
        """
        if name not in STAGE_NAMES:
            raise UnknownStage(f"unknown stage {name!r}, expected one of {STAGE_NAMES}")
        with _project_lock(self.path.parent, exclusive=True):
            data = _read_data(self.path)
            data["stages"][name] = "approved"
            write_json_durably(self.path, data)
            self._apply(data)
        return self

    def set_stage_status(self, name: str, status: str) -> "Project":
        """Set stage `name`'s status to any `STAGE_STATUSES` value -- the general form of
        `approve_stage`, which only ever writes `"approved"`. Exists for the worker (task 3): as it
        actually produces the script/track/scenes/assembly it needs to move a stage through
        `awaiting_approval`/`running`/`done`/`failed`, not only the one transition the human gate
        button (`approve_stage`) covers. Same lock -> re-read -> write -> `_apply` shape as every
        other locked mutator here.
        """
        if name not in STAGE_NAMES:
            raise UnknownStage(f"unknown stage {name!r}, expected one of {STAGE_NAMES}")
        if status not in STAGE_STATUSES:
            raise ProjectError(f"unknown stage status {status!r}, expected one of {STAGE_STATUSES}")
        with _project_lock(self.path.parent, exclusive=True):
            data = _read_data(self.path)
            data["stages"][name] = status
            write_json_durably(self.path, data)
            self._apply(data)
        return self

    def set_scene_status(self, idx: int, status: str, *, job_id=_UNSET_JOB_ID,
                          clip_path: str | None = None, keyframe_path: str | None = None
                          ) -> "Project":
        """Set scene `idx`'s status, and any of `job_id`/`clip_path`/`keyframe_path` that are
        given. `clip_path`/`keyframe_path` left as `None` are left exactly as they already were on
        disk -- this is a partial update, matching how the worker actually learns these facts one
        at a time (a `clip_path` only once the job is `done`); neither is how a scene gets these
        two fields *cleared* -- that is `invalidate_scene_chain`'s job, which sets them to `None`
        explicitly rather than by omission.

        **`job_id` follows a different convention (C1, fix round 1, 2026-08-18 review): omitting
        it leaves it unchanged, but passing `job_id=None` explicitly *clears* it.** Every call
        before this fix either omitted `job_id` or passed a real job id string -- never `None` --
        so this is not a behaviour change for any existing caller; it exists for `assemble.
        _submit_next_scene`'s own rollback, which must be able to actually clear a scene's `job_id`
        back to `None` after `claim_next_scene` already wrote a placeholder and the following
        `submit()` then raised, not merely leave the placeholder sitting there under a `"pending"`
        status. `clip_path`/`keyframe_path` keep the plain "`None` means unchanged" convention --
        no caller has ever needed to clear either of them outside `invalidate_scene_chain`'s own
        bulk reset.
        """
        if status not in SCENE_STATUSES:
            raise ProjectError(f"unknown scene status {status!r}, expected one of {SCENE_STATUSES}")
        with _project_lock(self.path.parent, exclusive=True):
            data = _read_data(self.path)
            scene = _find_scene(data["scenes"], idx)
            scene["status"] = status
            if job_id is not _UNSET_JOB_ID:
                scene["job_id"] = job_id
            if clip_path is not None:
                scene["clip_path"] = clip_path
            if keyframe_path is not None:
                scene["keyframe_path"] = keyframe_path
            write_json_durably(self.path, data)
            self._apply(data)
        return self

    def next_pending_scene(self) -> dict | None:
        """The one scene the worker should submit next, or `None` if nothing is ready -- a
        read-only *preview*, not a claim.

        Scenes are strictly sequential -- automatic keyframes need the *actual* last frame of the
        previous clip, not a guess (design spec, "Клипы"). `_next_ready_scene` does the actual walk
        (see its own docstring for the exact policy: a `done` scene lets the walk continue, the
        first `pending` scene found is the answer, `running`/`failed` means `None` for every scene
        from there on).

        Re-reads under a *shared* lock rather than trusting this object's own in-memory `scenes`
        -- the same reasoning `queue.scan` uses its shared lock for: a second `Project` object (the
        worker's own, say) may have advanced a scene since this one was last loaded. Deliberately
        does **not** call `self._apply` after that re-read the way the locked mutators do -- calling
        this must not have the side effect of silently refreshing `self.scenes`/`self.stages`/etc.
        out from under a caller mid-edit, and more importantly: two callers racing to submit a
        scene must not both be handed the same one, which this method alone cannot prevent (there
        is a window between reading here and a caller's own later `set_scene_status` call). A
        caller that intends to actually claim a scene for submission -- the worker, task 3 -- must
        use `claim_next_scene` instead, which closes that window under one lock hold.
        """
        with _project_lock(self.path.parent, exclusive=False):
            data = _read_data(self.path)
        scene = _next_ready_scene(data["scenes"])
        return dict(scene) if scene is not None else None

    def claim_next_scene(self, job_id: str, *, expected_idx: int | None = None) -> dict | None:
        """Atomically find *and claim* the next ready scene (`_next_ready_scene`'s walk, the same
        one `next_pending_scene` previews) for `job_id`, in a single exclusive lock hold: read,
        pick, set `status="running"` and `job_id`, write, all before the lock is released.

        This is what the worker (task 3) must call to submit a scene, not
        `next_pending_scene()` followed by a separate `set_scene_status()` call -- those are two
        lock acquisitions with a window in between where a second, concurrent caller's own
        `next_pending_scene()` can read the same still-`pending` scene and both callers end up
        submitting the same one. Folding "find" and "claim" into one lock hold is what closes that
        window: whichever caller's `claim_next_scene` acquires the lock first sees the scene as
        `pending` and flips it to `running` before releasing the lock, so the second caller's
        re-read (after it finally gets the lock) sees `running`, not `pending`, and moves on to
        whatever scene (if any) is ready after that -- or gets `None`.

        **`expected_idx` (C1, fix round 1, 2026-08-18 review).** A caller that already built a
        job (prompt, keyframe, `--tag`) for a *specific* scene index must be able to say so, and
        have the claim refuse -- inside this same lock hold, before writing anything -- if the
        scene that is actually next-ready by the time the lock is acquired is a *different* one.
        Without this, a caller racing an `invalidate_scene_chain` that landed between its own
        preview (`next_pending_scene()`) and this call would have the *wrong* scene silently
        claimed under a job id that was never built for it -- e.g. scene 0, freshly reset to
        `pending` by the invalidation, claimed under a job whose `args`/`note` actually describe
        scene 1; nothing then ever revisits scene 0 again, since the worker's own post-job hook
        only ever looks at the scene index a job's `note` names (`h3_48gb.assemble.parse_scene_
        note`), so it is stuck `running` forever. Checking and refusing *before* the write is what
        `assemble._submit_next_scene` needs to claim first, then submit -- see its own docstring.
        `expected_idx=None` (every call before this parameter existed) keeps the old, unchecked
        behaviour exactly.

        Returns a copy of the claimed scene, or `None` if nothing is ready *or* `expected_idx` was
        given and does not match. The lock is still taken and released even when nothing is
        claimed (a `None` result is not free of I/O), matching every other locked mutator here.
        """
        with _project_lock(self.path.parent, exclusive=True):
            data = _read_data(self.path)
            scene = _next_ready_scene(data["scenes"])
            if scene is None or (expected_idx is not None and scene["idx"] != expected_idx):
                self._apply(data)
                return None
            scene["status"] = "running"
            scene["job_id"] = job_id
            write_json_durably(self.path, data)
            self._apply(data)
            return dict(scene)

    def invalidate_scene_chain(self, idx: int) -> "Project":
        """Reset scene `idx` and every scene after it to `pending`, clearing each one's `job_id`,
        `clip_path` and `keyframe_path`. This is the "пересчёт отдельной сцены" button (design
        spec, "Клипы"): every scene from `idx` onward carries an automatic keyframe derived,
        transitively, from scene `idx`'s own clip, so reusing any of them after scene `idx`
        changes would silently keep stale downstream clips -- clearing the whole tail is the
        "честное предупреждение" the design spec calls for, not a convenience.

        Also resets the `scenes` and `assembly` stages back to `"draft"` (the pending-equivalent
        for a stage -- `STAGE_STATUSES` has no separate `"pending"`) and clears
        `assembly.final_path` to `None`. Both are downstream of the scene(s) just invalidated: the
        `scenes` stage's gate no longer reflects reality once a scene it covered goes back to
        `pending`, and any existing `assembly.final_path` was rendered from clips at least one of
        which this call just invalidated, so leaving it in place would silently keep serving a
        stale final video. `stages.script`/`stages.track` are untouched -- a scene invalidation has
        nothing to say about either of those.
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
            data["stages"]["scenes"] = "draft"
            data["stages"]["assembly"] = "draft"
            data["assembly"]["final_path"] = None
            write_json_durably(self.path, data)
            self._apply(data)
        return self

    def update_track(self, **fields) -> "Project":
        """Partial update of `self.track`'s own fields (`_TRACK_FIELDS`:
        `lyrics`/`caption`/`wav`/`mp3`/`mastered_mp3`/`sections`/`status`/`source`/`undersung`/
        `seed`/`duration`/`lyrics_auto`/`raw_segments`) -- the `track`-scoped sibling of
        `set_scene_status`, for a caller
        (task 3's chat integration, the mastering step, the worker's song-job dispatch) that has
        learned one or two of these facts and must not clobber the rest via a blind `save()` (see
        `save`'s own docstring for why that stops being safe once other writers exist).

        Unlike `set_scene_status`'s `None`-means-unchanged convention, `**fields` already tells
        "not passed" and "passed as `None`" apart for free: a keyword simply absent from `fields`
        leaves that field untouched on disk, while e.g. `update_track(lyrics=None)` genuinely
        clears it -- no sentinel needed. An unrecognized keyword raises `ProjectError` rather than
        silently writing a stray key into `track`. Same lock -> re-read -> merge -> write ->
        `_apply` shape as the module's other locked mutators.
        """
        unknown = set(fields) - set(_TRACK_FIELDS)
        if unknown:
            raise ProjectError(
                f"unknown track field(s) {sorted(unknown)}, expected one of {_TRACK_FIELDS}")
        with _project_lock(self.path.parent, exclusive=True):
            data = _read_data(self.path)
            data["track"].update(fields)
            write_json_durably(self.path, data)
            self._apply(data)
        return self

    def update_assembly(self, **fields) -> "Project":
        """Partial update of `self.assembly`'s own fields (`_ASSEMBLY_FIELDS`:
        `audio_mode`/`final_path`) -- the `assembly`-scoped sibling of `update_track`. `audio_mode`,
        if given, must be one of `ASSEMBLY_AUDIO_MODES`; `final_path` accepts any value including
        `None` (clearing a stale render, e.g. by hand for the same reason
        `invalidate_scene_chain` clears it automatically -- see that method's own docstring). An
        unrecognized keyword raises `ProjectError`, exactly like `update_track`. Same lock ->
        re-read -> merge -> write -> `_apply` shape as the module's other locked mutators.
        """
        unknown = set(fields) - set(_ASSEMBLY_FIELDS)
        if unknown:
            raise ProjectError(
                f"unknown assembly field(s) {sorted(unknown)}, expected one of {_ASSEMBLY_FIELDS}")
        if "audio_mode" in fields and fields["audio_mode"] not in ASSEMBLY_AUDIO_MODES:
            raise ProjectError(f"unknown audio_mode {fields['audio_mode']!r}, expected one of "
                                f"{ASSEMBLY_AUDIO_MODES}")
        with _project_lock(self.path.parent, exclusive=True):
            data = _read_data(self.path)
            data["assembly"].update(fields)
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

    Checks `path.is_file()` **before** taking the project lock, deliberately: `_project_lock`
    itself `mkdir(parents=True, exist_ok=True)`s the directory it locks, which is exactly right for
    every locked mutator (their project directory already exists by construction) but wrong here --
    a caller probing a project that was never created (a stale id, a typo'd path) must not have
    that probe itself create the directory and a `project.lock` file inside it as a side effect of
    failing. Bailing out first, before `_project_lock` ever runs, keeps a failed `load_project`
    leaving no trace on disk.
    """
    path = Path(path)
    if path.is_dir():
        path = path / PROJECT_FILENAME
    if not path.is_file():
        raise ProjectNotFound(f"no project.json at {path}")
    with _project_lock(path.parent, exclusive=False):
        data = _read_data(path)
    return Project(path, data)


def list_projects(outdir) -> list[Project]:
    """Every project under `<outdir>/projects/`, sorted by directory name (which sorts by
    creation time first, since every name starts with `_dir_stamp`'s `YYYYmmdd-HHMM`).

    A subdirectory that is not readable as a project -- corrupt JSON, JSON missing a required
    field, or a required field present but holding the wrong *type* (`_validate_shape`'s type
    checks: `"scenes"` not a list, `"stages"` not an object, a `scenes` element not an object, ...)
    -- is **skipped with a `warnings.warn`, not raised**: one broken project must not take down a
    page meant to list every *other* one. A subdirectory with no `project.json` at all is skipped
    silently, not warned about -- that is `create_project`'s own claimed-but-not-yet-written
    directory, a normal (if narrow) window, not corruption.

    `Project(path, data)` construction is inside the same `try`/`except` as the read itself, not
    just `_read_data` -- `data` has already passed `_validate_shape` by the time `Project()` sees
    it here, so this is defense in depth rather than a case this module's own tests currently
    exercise, but it keeps the promise ("one broken project cannot take the whole list down")
    honest even if a future field gets added to `Project._apply` without a matching
    `_validate_shape` check to go with it.

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
            project = Project(path, data)
        except ProjectError as exc:
            warnings.warn(f"skipping corrupt project at {path}: {exc}", stacklevel=2)
            continue
        projects.append(project)
    return projects
