"""The reference library (spec §3.5): cards shared by every project, each project pinning a card
at a version, and the deterministic Ref2VA prompt/conditions assembly from the @tags in a scene.

On disk: `<outdir>/library/<name>/card.json`, assets of version N in `<outdir>/library/<name>/vN/`;
`<name>` is the tag without its `@`. Old versions' files are never deleted (spec: "не удаляются,
пока на неё ссылается хоть один проект" -- the cheapest correct reading is "never").

Numbering follows the sglang server's own code, not the h3-bench prompts: the keyframe is a guide
latent and gets no label; reference pictures are `<Picture 1..>` in condition order; audio
references are `<Audio 1..>` (presentation.py:230-270 on alex-neuro).
"""
from __future__ import annotations

import fcntl
import json
import re
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from h3_48gb.queue import write_json_durably

TAG_RE = re.compile(r"^@[a-z0-9-]{2,32}$")
_TAG_IN_TEXT_RE = re.compile(r"(?<![\w@.])@([A-Za-z0-9-]+)")
KINDS = ("person", "object", "environment", "style", "voice")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
AUDIO_SUFFIXES = (".mp3", ".wav")
MAX_IMAGES = 4
MAX_DESCRIPTION = 400
_DESCRIPTION_FORBIDDEN = ("\n", "\r", "<", ">", "@")
CARD_NAME = "card.json"

ERROR_CODES = {
    "library_tag_invalid": "a reference tag is not @ followed by 2-32 of [a-z0-9-]",
    "library_tag_exists": "a reference card with this tag already exists",
    "library_kind_invalid": "a reference card kind outside person/object/environment/style/voice",
    "library_description_invalid": "a reference card description is empty or too long",
    "library_assets_invalid": "a reference card needs 1-4 png/jpg pictures, or exactly one mp3/wav for a voice",
    "library_card_not_found": "no reference card with this tag",
    "library_card_in_use": "a reference card is pinned by at least one project (finished ones included) and is not deleted",
    "library_version_not_found": "the reference card has no such version",
    "tag_invalid": "an @tag in scene text is not lowercase [a-z0-9-]{2,32}",
    "unknown_tag": "an @tag in scene text is not pinned to the project",
    "start_image_invalid": "scene 0's start_image is not a picture on disk (no such file, or a voice card)",
}


class LibraryError(ValueError):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = dict(detail or {})


@dataclass(frozen=True)
class Ref2VAScene:
    prompt: str
    images: tuple[str, ...]
    audios: tuple[str, ...]
    subjects: tuple[str, ...]


def library_root(outdir) -> Path:
    return Path(outdir) / "library"


def _check_tag(tag) -> str:
    if not isinstance(tag, str) or not TAG_RE.match(tag):
        raise LibraryError("library_tag_invalid",
                           f"тег {tag!r}: нужен @ и 2–32 символа из a-z, 0-9, -", {"tag": tag})
    return tag


def _card_dir(outdir, tag) -> Path:
    return library_root(outdir) / _check_tag(tag)[1:]


@contextmanager
def _card_lock(card_dir: Path, tag: str):
    """Exclusive lock on one card. The directory must exist: a writer that was waiting while the
    card was deleted must find it gone, not recreate an empty one (which `create_card` would then
    refuse as `library_tag_exists` forever)."""
    try:
        handle = open(card_dir / "card.lock", "a+")
    except FileNotFoundError:
        raise LibraryError("library_card_not_found", f"нет карточки {tag}", {"tag": tag}) from None
    with handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _check_fields(kind, description, assets) -> list[Path]:
    if kind not in KINDS:
        raise LibraryError("library_kind_invalid", f"тип {kind!r}: можно {KINDS}", {"kind": kind})
    if not isinstance(description, str) or not description.strip() \
            or len(description) > MAX_DESCRIPTION \
            or any(ch in description for ch in _DESCRIPTION_FORBIDDEN):
        # The description is pasted into `subject_definitions` as-is, so a newline, an angle
        # bracket or an @ would let it forge a `<Subject N>` line or a tag of its own.
        raise LibraryError("library_description_invalid",
                           f"описание: 1–{MAX_DESCRIPTION} символов по-английски, одной строкой, "
                           "без < > @", {})
    paths = [Path(a) for a in assets]
    suffixes = [path.suffix.lower() for path in paths]
    if kind == "voice":
        ok = len(paths) == 1 and suffixes[0] in AUDIO_SUFFIXES
    else:
        ok = 1 <= len(paths) <= MAX_IMAGES and all(s in IMAGE_SUFFIXES for s in suffixes)
    if not ok or not all(path.is_file() for path in paths):
        raise LibraryError("library_assets_invalid",
                           "нужно 1–4 картинки png/jpg, а для голоса — ровно один mp3/wav",
                           {"kind": kind, "assets": [str(p) for p in paths]})
    return paths


def _copy_assets(card_dir: Path, version: int, paths: list[Path]) -> list[str]:
    version_dir = card_dir / f"v{version}"
    version_dir.mkdir(parents=True, exist_ok=False)
    relative = []
    for index, src in enumerate(paths, start=1):
        name = f"{index:02d}-{src.name}"
        shutil.copyfile(src, version_dir / name)
        relative.append(f"v{version}/{name}")
    return relative


def _read(card_dir: Path, tag: str) -> dict:
    path = card_dir / CARD_NAME
    if not path.is_file():
        raise LibraryError("library_card_not_found", f"нет карточки {tag}", {"tag": tag})
    return json.loads(path.read_text(encoding="utf-8"))


def _view(card_dir: Path, card: dict, version: int) -> dict:
    entry = card["versions"].get(str(version))
    if entry is None:
        raise LibraryError("library_version_not_found",
                           f"у {card['tag']} нет версии {version}", {"tag": card["tag"],
                                                                     "version": version})
    return {"tag": card["tag"], "kind": entry["kind"], "description": entry["description"],
            "version": version, "latest_version": card["version"],
            "assets": [str(card_dir / rel) for rel in entry["assets"]]}


def create_card(outdir, *, tag, kind, description, assets, now=None) -> dict:
    card_dir = _card_dir(outdir, tag)
    paths = _check_fields(kind, description, assets)
    library_root(outdir).mkdir(parents=True, exist_ok=True)
    try:
        card_dir.mkdir()
    except FileExistsError:
        raise LibraryError("library_tag_exists", f"тег {tag} уже есть", {"tag": tag}) from None
    stamp = now or _now()
    with _card_lock(card_dir, tag):
        relative = _copy_assets(card_dir, 1, paths)
        card = {"tag": tag, "version": 1, "created": stamp, "updated": stamp,
                "versions": {"1": {"kind": kind, "description": description.strip(),
                                   "assets": relative, "created": stamp}}}
        write_json_durably(card_dir / CARD_NAME, card)
    return _view(card_dir, card, 1)


def update_card(outdir, tag, *, kind=None, description=None, assets=None, now=None) -> dict:
    card_dir = _card_dir(outdir, tag)
    _read(card_dir, tag)
    with _card_lock(card_dir, tag):
        card = _read(card_dir, tag)
        previous = card["versions"][str(card["version"])]
        new_kind = kind if kind is not None else previous["kind"]
        new_description = description if description is not None else previous["description"]
        version = card["version"] + 1
        if assets is None:
            paths = [card_dir / rel for rel in previous["assets"]]
            _check_fields(new_kind, new_description, paths)
            relative = list(previous["assets"])
        else:
            paths = _check_fields(new_kind, new_description, assets)
            relative = _copy_assets(card_dir, version, paths)
        stamp = now or _now()
        card["versions"][str(version)] = {"kind": new_kind, "description": new_description.strip(),
                                          "assets": relative, "created": stamp}
        card["version"] = version
        card["updated"] = stamp
        write_json_durably(card_dir / CARD_NAME, card)
    return _view(card_dir, card, version)


def get_card(outdir, tag, version=None) -> dict:
    card_dir = _card_dir(outdir, tag)
    card = _read(card_dir, tag)
    return _view(card_dir, card, card["version"] if version is None else int(version))


def card_history(outdir, tag) -> list[dict]:
    """Every version of the card, oldest first, with absolute asset paths."""
    card_dir = _card_dir(outdir, tag)
    card = _read(card_dir, tag)
    return [{"version": int(number), "kind": entry["kind"], "description": entry["description"],
             "assets": [str(card_dir / rel) for rel in entry["assets"]],
             "created": entry["created"]}
            for number, entry in sorted(card["versions"].items(), key=lambda kv: int(kv[0]))]


def delete_card(outdir, tag, *, pinned_by, now: str | None = None) -> Path:
    """Move the card's directory to `library/.trash/<name>-<stamp>[-N]`; never erase it. A card some
    project pins is refused. `pinned_by` is a list of `{"id", "title"}` or a zero-argument callable
    returning one -- the callable is evaluated under the card lock, so a project cannot pin the
    card between the check and the move."""
    card_dir = _card_dir(outdir, tag)
    _read(card_dir, tag)
    with _card_lock(card_dir, tag):
        _read(card_dir, tag)  # a concurrent delete may have moved it while we waited
        pins = pinned_by() if callable(pinned_by) else pinned_by
        if pins:
            listed = ", ".join(f"«{item['title']}» ({item['id']})" for item in pins)
            raise LibraryError("library_card_in_use",
                               f"{tag} подключена к проектам: {listed} — отключите её там или "
                               "удалите проекты", {"tag": tag, "projects": pins})
        trash = library_root(outdir) / ".trash"
        trash.mkdir(exist_ok=True)
        base = f"{card_dir.name}-{now or datetime.now().strftime('%Y%m%d%H%M%S')}"
        target, number = trash / base, 1
        while target.exists():
            number += 1
            target = trash / f"{base}-{number}"
        card_dir.rename(target)
    return target


def list_cards(outdir) -> list[dict]:
    root = library_root(outdir)
    if not root.is_dir():
        return []
    cards = []
    for entry in sorted(root.iterdir()):
        if (entry / CARD_NAME).is_file():
            try:
                cards.append({**get_card(outdir, "@" + entry.name),
                              "versions": card_history(outdir, "@" + entry.name)})
            except (LibraryError, ValueError, KeyError):
                continue
    return cards


def scene_tags(text: str) -> list[str]:
    seen: list[str] = []
    for match in _TAG_IN_TEXT_RE.finditer(text or ""):
        tag = "@" + match.group(1)
        if not TAG_RE.match(tag):
            raise LibraryError("tag_invalid",
                               f"тег {tag}: пишется строчными, 2–32 символа из a-z, 0-9, -",
                               {"tag": tag})
        if tag not in seen:
            seen.append(tag)
    return seen


#: A scene prompt that writes its own `subject_definitions:` section (final review 2026-10-07, I5).
_OWN_DEFINITIONS_RE = re.compile(r"(?m)^\s*subject_definitions\s*:")


def build_ref2va(scene_prompt: str, references, outdir, extra_refs=()) -> Ref2VAScene:
    """The scene as sglang's Ref2VA takes it: each @tag, in order of first mention, becomes
    `<Subject N>` in the text and its card's pictures `<Picture k>` (audio `<Audio k>`); the panel
    puts its own `subject_definitions:` block in front.

    `extra_refs` (UI-gap API 2): the scene's explicit `refs` -- cards connected as conditions
    without a mention in the text and without a `<Subject N>`. Conditions go in this order: the
    explicit refs, then the tags the text mentions, each card once -- so `<Picture k>` counts the
    explicit refs' pictures first, and a text tag that is also in `extra_refs` points at the
    pictures it already brought. The keyframe is not a reference and is never numbered.

    I5 (coordinator's ruling): a prompt that already has its own `subject_definitions:` section
    gets no second block -- the owner's definitions stand as written. The @tags in it still
    become `<Subject N>` and still bring their pictures as conditions, numbered exactly as above,
    so the owner's block has to follow that numbering."""
    tags = scene_tags(scene_prompt)
    extra = list(dict.fromkeys(extra_refs))
    if not tags and not extra:
        return Ref2VAScene(scene_prompt, (), (), ())
    pinned = {ref["tag"]: ref for ref in references}
    unknown = [tag for tag in [*extra, *tags] if tag not in pinned]
    if unknown:
        raise LibraryError("unknown_tag", f"теги не подключены к проекту: {', '.join(unknown)}",
                           {"unknown": list(dict.fromkeys(unknown))})
    images: list[str] = []
    audios: list[str] = []
    cards: dict[str, dict] = {}     # tag -> {"card", "pictures": [k...], "audio": k | None}
    for tag in [*extra, *(tag for tag in tags if tag not in extra)]:
        card = get_card(outdir, tag, pinned[tag].get("version"))
        entry = {"card": card, "pictures": [], "audio": None}
        if card["kind"] == "voice":
            audios.append(card["assets"][0])
            entry["audio"] = len(audios)
        else:
            for asset in card["assets"]:
                images.append(asset)
                entry["pictures"].append(len(images))
        cards[tag] = entry
    lines: list[str] = []
    for number, tag in enumerate(tags, start=1):
        entry = cards[tag]
        description = entry["card"]["description"].strip().rstrip(".")
        if entry["audio"] is not None:
            lines.append(f"<Subject {number}> is {description}, voice from <Audio {entry['audio']}>.")
        else:
            labels = ", ".join(f"<Picture {k}>" for k in entry["pictures"])
            lines.append(f"<Subject {number}> is {description}, appearance from {labels}.")
    body = _TAG_IN_TEXT_RE.sub(lambda m: f"<Subject {tags.index('@' + m.group(1)) + 1}>",
                               scene_prompt)
    if not tags or _OWN_DEFINITIONS_RE.search(scene_prompt):
        prompt = body
    else:
        prompt = "subject_definitions:\n" + "\n".join(lines) + "\n\n" + body
    return Ref2VAScene(prompt, tuple(images), tuple(audios), tuple(tags))


def references_context(cards: list[dict]) -> str:
    lines = [f"{card['tag']} ({card['kind']}): {card['description']}" for card in cards]
    return ("## Reference tags\nEvery scene must name who and where is in frame with these tags, "
            "written exactly as below; no other @tags exist.\n" + "\n".join(lines))
