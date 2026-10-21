"""Prompt linter (spec §10). Slice S1 carries L1–L6, L7a, L8–L12; L7b/L7d/L13/L14 need templates,
keyframes and takes and arrive with them. Each rule is `(scene, project, limits) -> findings`."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from h3_48gb.studio.duration import MAX_SECONDS, MIN_SECONDS

TEXT_FIELDS = ("action_text", "performance_text", "sound_text", "music_text", "state_in_text",
               "state_out_text")


@dataclass(frozen=True)
class LintScene:
    id: str
    idx: int
    mode: str
    action_text: str
    performance_text: str
    sound_text: str
    music_text: str
    state_in_text: str
    state_out_text: str
    requested_s: float
    frames: int
    source_frame_kind: str
    source_scene_id: str | None
    cast_tags: tuple[str, ...] = ()        # who is on screen (SCENE_REF), in ref order


@dataclass(frozen=True)
class LintProject:
    scenes: list[LintScene]
    portrait_phrases: dict[str, str] = field(default_factory=dict)   # tag -> phrase

    def scene(self, scene_id: str | None) -> LintScene | None:
        return next((s for s in self.scenes if s.id == scene_id), None)


def finding(rule: str, severity: str, scene: LintScene, field_name: str | None,
            message: str) -> dict:
    return {"rule": rule, "severity": severity, "scene_id": scene.id, "field": field_name,
            "message": message}


_GAZE = re.compile(r"\b(gaze|eyes|looks?|looking|glanc\w*|stares?|blinks?)\b", re.I)
_FACE = re.compile(r"\b(face|facial|smil\w*|frown\w*|brows?|lips|jaw|cheeks?|mouth)\b", re.I)
_HANDS = re.compile(r"\b(hands?|fingers?|fists?|palms?|grip\w*|clutch\w*)\b", re.I)
_CHANGE = re.compile(r"\b(then|until|gradually|shifts?|gives way|softens?|hardens?|becomes?)\b",
                     re.I)


def l1_performance(scene, project, limits):
    text = scene.performance_text
    hits = sum(bool(p.search(text)) for p in (_GAZE, _FACE, _HANDS, _CHANGE))
    if not text.strip() or hits < 2:
        return [finding("L1", "warning", scene, "performance_text",
                        "Игра героя не прописана: нужно хотя бы два из — взгляд, мимика, руки, "
                        "смена состояния")]
    return []


#: §10 L2, narrowed (ruling R21a): «the sound is not described physically». It fires only when the
#: sound text negates something ("no …", "without …", "nothing …", "silence", "not … heard") AND
#: names no sound at all — no word from the sound vocabulary below and no material from L3. A
#: negation next to a described source ("Wind rustles through reeds without music.") is fine.
#: The vocabulary is deliberately the reliable part: a source outside it can produce a spurious
#: warning, never an error; the list grows when the scenarist's real texts show a miss.
_NEGATION = re.compile(r"\b(?:no|without|nothing|none|silence|silent|mute|muted|soundless)\b|"
                       r"\bnot\b[^.;,]*\b(?:heard|audible)\b", re.I)
_SOUND_WORDS = re.compile(
    r"\b(?:wind|rain|water|waves?|stream|river|splash\w*|drip\w*|rustl\w*|creak\w*|crunch\w*|"
    r"crackl\w*|thud\w*|tap\w*|knock\w*|click\w*|clatter\w*|clink\w*|ring\w*|hum\w*|buzz\w*|"
    r"whistl\w*|hiss\w*|roar\w*|rumbl\w*|patter\w*|swish\w*|squeak\w*|scrap\w*|thump\w*|"
    r"footsteps?|footfalls?|steps?|heels?|breath\w*|sigh\w*|laugh\w*|voices?|whisper\w*|"
    r"murmur\w*|chirp\w*|birds?|birdsong|bells?|engines?|traffic|thunder|fire|flames?|leaves|"
    r"oars?|rowlocks?|bark\w*|song|sing\w*|hums?)\b", re.I)


#: "no background music", "without voices": the negated words are not a described source.
_NEGATED_PHRASE = re.compile(r"\b(?:no|without)\s+(?:[a-z-]+\s+)?[a-z-]+", re.I)


def l2_mute_sound(scene, project, limits):
    text = scene.sound_text
    if not _NEGATION.search(text):
        return []
    rest = _NEGATED_PHRASE.sub(" ", text)
    if _SOUND_WORDS.search(rest) or _MATERIAL.search(rest):
        return []
    return [finding("L2", "warning", scene, "sound_text",
                    "Звук описан только отрицанием: назовите, что звучит (материал и контакт)")]


_STEPS = re.compile(r"\b(steps?|footsteps?|footfalls?|walk\w*)\b", re.I)
_MATERIAL = re.compile(r"\b(grass|gravel|wood\w*|stone\w*|concrete|sand|snow|mud|leaves|floor\w*|"
                       r"tiles?|asphalt|carpet|water|metal|dirt|earth|moss|planks?|boards?)\b",
                       re.I)


def l3_material(scene, project, limits):
    if _STEPS.search(scene.sound_text) and not _MATERIAL.search(scene.sound_text):
        return [finding("L3", "info", scene, "sound_text",
                        "Шаги без материала: по чему идут (трава, гравий, доски)?")]
    return []


_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def l4_cyrillic(scene, project, limits):
    return [finding("L4", "error", scene, name, "Кириллица в поле промпта: H3 ждёт английский")
            for name in TEXT_FIELDS if _CYRILLIC.search(getattr(scene, name))]


_TAG = re.compile(r"@[\w-]+")


def l5_tag_mode(scene, project, limits):
    if scene.mode not in ("text", "from_frame"):
        return []
    return [finding("L5", "error", scene, name,
                    "@тег работает только в режиме «Референсы»")
            for name in TEXT_FIELDS if _TAG.search(getattr(scene, name))]


def _chain(project: LintProject, scene: LintScene) -> list[LintScene]:
    """Every scene connected to `scene` through `prev_last_frame` links, in either direction —
    a branch (two scenes continuing the same one) and a loop are both allowed by the data and
    must not break the linter. Always contains `scene`; ordered by `idx`."""
    by_id = {s.id: s for s in project.scenes}
    neighbours: dict[str, set[str]] = {s.id: set() for s in project.scenes}
    for s in project.scenes:
        if s.source_frame_kind == "prev_last_frame" and s.source_scene_id in by_id:
            neighbours[s.id].add(s.source_scene_id)
            neighbours[s.source_scene_id].add(s.id)
    seen, todo = {scene.id}, [scene.id]
    neighbours.setdefault(scene.id, set())
    while todo:
        for nxt in neighbours.get(todo.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return sorted((by_id.get(i, scene) for i in seen), key=lambda s: (s.idx, s.id))


def _content(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2}


def _portrait(scene: LintScene, phrase: str) -> frozenset[str]:
    """What this scene says of the hero's looks, narrowed to what can be told reliably (ruling
    R21): the words of the card's phrase that the scene's text still carries. The action around
    the portrait («walks» / «sits») never enters it."""
    return frozenset(_content(phrase) & _content(scene.action_text))


def l6_portrait_drift(scene, project, limits):
    """§10 L6, narrowed (R21): within the scenes of one `prev_last_frame` chain that have the hero
    on screen (SCENE_REF), a scene whose portrait words differ from another's is flagged — the
    reference is the card's full phrase if some scene keeps all its words, else the first scene
    of the chain. The same portrait with a different action is not a finding. Drift that only
    ADDS words absent from the card («… and a red scarf») is not detected."""
    chain = _chain(project, scene)
    out = []
    for tag in scene.cast_tags:
        phrase = project.portrait_phrases.get(tag, "")
        present = [s for s in chain if tag in s.cast_tags]
        if not phrase or len(present) < 2:
            continue
        portraits = {s.id: _portrait(s, phrase) for s in present}
        full = frozenset(_content(phrase))
        reference = full if full in portraits.values() else portraits[present[0].id]
        if portraits[scene.id] != reference:
            out.append(finding("L6", "warning", scene, "action_text",
                               f"Описание @{tag} расходится с другими сценами цепочки: вставьте "
                               f"дословно «{phrase}»"))
    return out


def ru_frames(n: int) -> str:
    """«1 кадр», «2–4 кадра», «5+ кадров», «11–14 кадров»."""
    if 11 <= n % 100 <= 14:
        return "кадров"
    if n % 10 == 1:
        return "кадр"
    if 2 <= n % 10 <= 4:
        return "кадра"
    return "кадров"


def l7a_duration(scene, project, limits):
    if not (MIN_SECONDS <= scene.requested_s <= MAX_SECONDS):
        return [finding("L7a", "error", scene, "requested_s",
                        f"Длительность {scene.requested_s:g} с вне 3–15 с")]
    if scene.frames / 24 != scene.requested_s:
        return [finding("L7a", "info", scene, "requested_s",
                        f"{scene.frames} {ru_frames(scene.frames)} = {scene.frames / 24:.3f} с")]
    return []


_MEASURED = re.compile(r"calm neutral expression|neutral expression|expressionless|"
                       r"lips stay closed|mouth stays closed", re.I)


def l8_measured(scene, project, limits):
    return [finding("L8", "warning", scene, name,
                    "«Измерительная» формула даёт лицо-маску: опишите состояние и его смену")
            for name in ("action_text", "performance_text")
            if _MEASURED.search(getattr(scene, name))]


_BIG_NUMBER = re.compile(r"\b([4-9]|\d{2,}|four|five|six|seven|eight|nine|ten|eleven|twelve|"
                         r"dozens?|hundreds?)\b", re.I)


def l9_counting(scene, project, limits):
    if _BIG_NUMBER.search(scene.action_text):
        return [finding("L9", "warning", scene, "action_text",
                        "H3 не умеет считать больше трёх: замените число на «несколько» или "
                        "уберите")]
    return []


_MODE_LINE = re.compile(r"^\s*mode\s*:", re.I | re.M)


def l10_mode_line(scene, project, limits):
    return [finding("L10", "error", scene, name, "Строка «mode:» в тексте: режим задаёт поле сцены")
            for name in TEXT_FIELDS if _MODE_LINE.search(getattr(scene, name))]


def l11_length(scene, project, limits):
    words = len(f"{scene.action_text} {scene.performance_text}".split())
    if words < 40 or words > 500:
        return [finding("L11", "warning", scene, "action_text",
                        f"Действие и игра — {words} слов; H3 лучше держит 40–500")]
    return []


def l12_chain(scene, project, limits):
    if scene.source_frame_kind != "prev_last_frame":
        return []
    source = project.scene(scene.source_scene_id)
    if source is None:
        return [finding("L12", "error", scene, "source_frame",
                        "Продолжение без предыдущей сцены: выберите другой источник кадра")]
    if source.idx >= scene.idx:
        return [finding("L12", "error", scene, "source_frame",
                        f"Кадр берётся из сцены {source.idx + 1}, а она идёт не раньше этой")]
    return []


RULES = (l1_performance, l2_mute_sound, l3_material, l4_cyrillic, l5_tag_mode, l6_portrait_drift,
         l7a_duration, l8_measured, l9_counting, l10_mode_line, l11_length, l12_chain)


def lint_scene(scene: LintScene, project: LintProject, limits: dict | None = None) -> list[dict]:
    """Never raises: the linter runs after an operation has committed, so a rule that breaks on
    some scene graph must become a finding, not a 500 for a change that was saved."""
    out: list[dict] = []
    for rule in RULES:
        try:
            out.extend(rule(scene, project, limits))
        except Exception as err:                 # a bug in a rule, reported where it is seen
            out.append(finding("lint_internal", "info", scene, None,
                               f"Правило {rule.__name__} не смогло проверить сцену "
                               f"({type(err).__name__})"))
    return out


def has_errors(findings: list[dict]) -> bool:
    return any(f["severity"] == "error" for f in findings)
