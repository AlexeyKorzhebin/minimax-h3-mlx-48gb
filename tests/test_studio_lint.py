"""§10: every S1 rule fires on its case, stays silent on a clean scene, and holds its bounds."""
from __future__ import annotations

import dataclasses
import itertools

import pytest

from h3_48gb.studio import lint
from h3_48gb.studio.lint import LintProject, LintScene, lint_scene

#: Written out here, not imported: a field dropped from the linter's own list must fail a test
#: instead of silently shrinking the parametrization (review r2, Н2).
ENGLISH_FIELDS = ["action_text", "performance_text", "sound_text", "music_text", "state_in_text",
                  "state_out_text"]

PHRASE = "a woman with a loose dark braid"
ACTION = (f"{PHRASE}. At dawn she walks out from the birches onto a forest path, stops at the "
          "edge of the water and looks across the lake toward the far shore while the morning "
          "mist lifts slowly above the reeds.")
PERF = ("Her gaze searches the gaps between the trees, then her shoulders drop and her fingers "
        "loosen around the strap of her bag.")
CLEAN = LintScene(id="s1", idx=0, mode="from_frame", action_text=ACTION, performance_text=PERF,
                  sound_text="Soft footsteps on damp grass, linen rustles.", music_text="none",
                  state_in_text="", state_out_text="", requested_s=8.0, frames=192,
                  source_frame_kind="none", source_scene_id=None)
L1_MSG = ("Игра героя не прописана: нужно хотя бы два из — взгляд, мимика, руки, смена "
          "состояния")
L2_MSG = "Звук описан только отрицанием: назовите, что звучит (материал и контакт)"


def _lint(scene, others=(), phrases=None):
    project = LintProject(scenes=[scene, *others], portrait_phrases=phrases or {})
    return [(f["rule"], f["severity"], f["scene_id"], f["field"], f["message"])
            for f in lint_scene(scene, project)]


def _words(n: int) -> str:
    """`n` words for action+performance together; PERF keeps L1 quiet (gaze, then, fingers)."""
    filler = n - len(PERF.split())
    return " ".join(["birch"] * filler)


def test_a_clean_scene_has_no_findings():
    assert _lint(CLEAN) == []


@pytest.mark.parametrize("over, expected", [
    ({"performance_text": ""}, [("L1", "warning", "s1", "performance_text", L1_MSG)]),
    ({"performance_text": "Her gaze is steady and calm throughout the whole long scene here."},
     [("L1", "warning", "s1", "performance_text", L1_MSG)]),
    ({"sound_text": "Silence. No music."}, [("L2", "warning", "s1", "sound_text", L2_MSG)]),
    ({"sound_text": "No music. Nothing is audible."}, [("L2", "warning", "s1", "sound_text", L2_MSG)]),
    ({"sound_text": "Nothing is audible, without voices."},
     [("L2", "warning", "s1", "sound_text", L2_MSG)]),
    ({"sound_text": "No background music."}, [("L2", "warning", "s1", "sound_text", L2_MSG)]),
    ({"sound_text": "No speech is heard."}, [("L2", "warning", "s1", "sound_text", L2_MSG)]),
    ({"sound_text": "No music. This scene is indoors."},          # review r3: a place is no sound
     [("L2", "warning", "s1", "sound_text", L2_MSG)]),
    # statements made only of fillers and hearing words name no source
    ({"sound_text": "No music. The sound is audible there."},
     [("L2", "warning", "s1", "sound_text", L2_MSG)]),
    ({"sound_text": "No voices. Any noise here is heard."},
     [("L2", "warning", "s1", "sound_text", L2_MSG)]),
    ({"sound_text": "Soft footsteps, linen rustles."},
     [("L3", "info", "s1", "sound_text", "Шаги без материала: по чему идут (трава, гравий, доски)?")]),
    ({"action_text": ACTION + " @anna smiles."},
     [("L5", "error", "s1", "action_text", "@тег работает только в режиме «Референсы»")]),
    ({"requested_s": 16.0, "frames": 345},
     [("L7a", "error", "s1", "requested_s", "Длительность 16 с вне 3–15 с")]),
    ({"requested_s": 3.0, "frames": 73},
     [("L7a", "info", "s1", "requested_s", "73 кадра = 3.042 с")]),
    ({"performance_text": PERF + " A calm neutral expression stays."},
     [("L8", "warning", "s1", "performance_text",
       "«Измерительная» формула даёт лицо-маску: опишите состояние и его смену")]),
    ({"action_text": ACTION + " Five birds rise."},
     [("L9", "warning", "s1", "action_text",
       "H3 не умеет считать больше трёх: замените число на «несколько» или уберите")]),
    ({"sound_text": "Soft footsteps on damp grass.\nmode: refs"},
     [("L10", "error", "s1", "sound_text", "Строка «mode:» в тексте: режим задаёт поле сцены")]),
    ({"source_frame_kind": "prev_last_frame", "source_scene_id": None},
     [("L12", "error", "s1", "source_frame",
       "Продолжение без предыдущей сцены: выберите другой источник кадра")]),
])
def test_rule_fires(over, expected):
    assert _lint(dataclasses.replace(CLEAN, **over)) == expected


@pytest.mark.parametrize("sound", [
    "No music and wind rustles through reeds.",        # a negation next to a real source
    "No speech, only the creak of oars in rowlocks.",
    "Nothing is audible but wind rustles through reeds.",
    "Wind rustles through reeds without music.",                   # review r3
    "Gravel under her boots, no music.",                           # an L3 material is a source
    "No music, only wind.",                                        # «wind» is the only source word
])
def test_l2_is_quiet_when_a_positive_source_is_named(sound):
    assert _lint(dataclasses.replace(CLEAN, sound_text=sound)) == []


@pytest.mark.parametrize("field_name", ENGLISH_FIELDS)
def test_l4_checks_every_english_field(field_name):
    scene = dataclasses.replace(CLEAN, **{field_name: getattr(CLEAN, field_name) + " Тишина"})
    assert [f for f in _lint(scene) if f[0] == "L4"] == [
        ("L4", "error", "s1", field_name, "Кириллица в поле промпта: H3 ждёт английский")]


@pytest.mark.parametrize("words, fires", [(39, True), (40, False), (500, False), (501, True)])
def test_l11_bounds(words, fires):
    scene = dataclasses.replace(CLEAN, action_text=_words(words))
    got = [f for f in _lint(scene) if f[0] == "L11"]
    assert got == ([("L11", "warning", "s1", "action_text",
                     f"Действие и игра — {words} слов; H3 лучше держит 40–500")] if fires else [])


def test_tag_is_fine_in_refs_mode():
    assert _lint(dataclasses.replace(CLEAN, mode="refs", action_text=ACTION + " @anna nods.")) == []


@pytest.mark.parametrize("source_idx", [1, 0])
def test_l12_source_that_is_not_earlier(source_idx):
    """A later scene and a scene at the same index are both not «earlier» (§10 L12 `>=`)."""
    other = dataclasses.replace(CLEAN, id="s2", idx=source_idx)
    scene = dataclasses.replace(CLEAN, source_frame_kind="prev_last_frame", source_scene_id="s2")
    assert _lint(scene, [other]) == [
        ("L12", "error", "s1", "source_frame",
         f"Кадр берётся из сцены {source_idx + 1}, а она идёт не раньше этой")]


def _chain(first_action=ACTION, second_action=ACTION, second_tags=("anna",), linked=True):
    first = dataclasses.replace(CLEAN, action_text=first_action, cast_tags=("anna",))
    second = dataclasses.replace(
        CLEAN, id="s2", idx=1, action_text=second_action, cast_tags=second_tags,
        source_frame_kind="prev_last_frame" if linked else "none",
        source_scene_id="s1" if linked else None)
    return first, second


def l6(scene_id, tag="anna", phrase=PHRASE):
    return ("L6", "warning", scene_id, "action_text",
            f"Описание @{tag} расходится с другими сценами цепочки: вставьте дословно «{phrase}»")
DRIFTED = ACTION.replace("a loose dark braid", "a dark plait")
DRIFTED_OTHER = ACTION.replace("a loose dark braid", "short grey hair")


def _both(first, second):
    return _lint(first, [second], {"anna": PHRASE}), _lint(second, [first], {"anna": PHRASE})


def test_l6_flags_the_drifted_scene_at_either_end_of_the_chain():
    assert _both(*_chain(second_action=DRIFTED)) == ([], [l6("s2")])
    assert _both(*_chain(first_action=DRIFTED)) == ([l6("s1")], [])   # review r1, finding 8


def test_l6_the_same_description_in_both_scenes_is_not_a_difference():
    """Review r2, Н4: both scenes carry the same edited phrase — nothing differs between them."""
    assert _both(*_chain(first_action=DRIFTED, second_action=DRIFTED)) == ([], [])


def test_l6_two_different_edits_flag_the_one_unlike_the_first_scene():
    assert _both(*_chain(first_action=DRIFTED, second_action=DRIFTED_OTHER)) == ([], [l6("s2")])


def test_l6_is_quiet_when_the_hero_is_not_in_the_other_scene_or_the_chain_is_broken():
    first, second = _chain(first_action=DRIFTED, second_tags=())
    assert _lint(first, [second], {"anna": PHRASE}) == []
    first, second = _chain(first_action=DRIFTED, linked=False)
    assert _lint(first, [second], {"anna": PHRASE}) == []


def test_l6_the_same_portrait_with_a_different_action_is_not_a_finding():
    """Review r3: «… braid walks.» and «… braid sits.» describe the hero the same way."""
    first = dataclasses.replace(CLEAN, action_text=f"{PHRASE} walks to the water.",
                                cast_tags=("anna",))
    second = dataclasses.replace(CLEAN, id="s2", idx=1, action_text=f"{PHRASE} sits on the planks.",
                                 cast_tags=("anna",), source_frame_kind="prev_last_frame",
                                 source_scene_id="s1")
    assert [f for f in _both(first, second)[1] if f[0] == "L6"] == []


def _scene_with(sid, idx, kind="none", source=None, action=ACTION, tags=("anna",)):
    return dataclasses.replace(CLEAN, id=sid, idx=idx, action_text=action, cast_tags=tags,
                               source_frame_kind=kind, source_scene_id=source)


def test_l6_on_a_branch_flags_the_drifted_branch_only():
    """Review r3 blocker: s2 and s3 both continue s1 — the linter used to raise KeyError."""
    s1, s2 = _scene_with("s1", 0), _scene_with("s2", 1, "prev_last_frame", "s1")
    s3 = _scene_with("s3", 2, "prev_last_frame", "s1", action=DRIFTED)
    project = LintProject(scenes=[s1, s2, s3], portrait_phrases={"anna": PHRASE})
    got = {s.id: [f["rule"] for f in lint_scene(s, project) if f["rule"] in ("L6", "lint_internal")]
           for s in (s1, s2, s3)}
    assert got == {"s1": [], "s2": [], "s3": ["L6"]}


def test_l6_on_a_loop_does_not_hang_or_raise():
    s1 = _scene_with("s1", 0, "prev_last_frame", "s2")
    s2 = _scene_with("s2", 1, "prev_last_frame", "s1", action=DRIFTED)
    project = LintProject(scenes=[s1, s2], portrait_phrases={"anna": PHRASE})
    assert [f["rule"] for f in lint_scene(s2, project) if f["rule"] in ("L6", "lint_internal")] \
        == ["L6"]


GRAPH_PATTERNS = [(0, 0, 0, 0), (1, 0, 0, 0), (0, 1, 0, 0), (1, 1, 0, 0), (0, 0, 1, 0),
                  (0, 0, 1, 1)]


def _graphs(n: int):
    """Every scene graph of n scenes the data model allows: each scene's frame source is none, an
    asset, `prev_last_frame` without a scene (first scene) or `prev_last_frame` to ANY other scene
    — later ones and loops included (L12 reports those, they are still storable)."""
    ids = [f"s{i}" for i in range(n)]
    choices = [[("none", None), ("asset", None), ("prev_last_frame", None),
                ("prev_last_frame", "ghost"),            # a dangling reference
                *[("prev_last_frame", other) for other in ids if other != me]] for me in ids]
    for sources in itertools.product(*choices):
        # (odd scenes drift, odd scenes off screen, every scene drifts differently, the first
        # scene off screen): the last two reach the «no scene keeps the full phrase» branch of L6
        # with a chain whose first scene does not carry the hero.
        for drift_odd, off_odd, drift_all, off_first in GRAPH_PATTERNS:
            def action(i):
                if drift_all:
                    return DRIFTED if i % 2 else DRIFTED_OTHER
                return DRIFTED if (drift_odd and i % 2) else ACTION

            def tags(i):
                return () if (off_odd and i % 2) or (off_first and i == 0) else ("anna",)
            yield [_scene_with(sid, i, kind, src, action=action(i), tags=tags(i))
                   for i, (sid, (kind, src)) in enumerate(zip(ids, sources))]


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_the_linter_never_breaks_on_an_allowed_scene_graph(n):
    """Coordinator r4: a pure function — no exception and no lint_internal on any graph of up to
    four scenes. Each rule is called directly, so lint_scene's own safety net hides nothing."""
    checked = 0
    for scenes in _graphs(n):
        project = LintProject(scenes=scenes, portrait_phrases={"anna": PHRASE})
        for scene in scenes:
            for rule in lint.RULES:
                rule(scene, project, None)
            checked += 1
    assert checked == (n + 3) ** n * len(GRAPH_PATTERNS) * n


def test_a_rule_that_breaks_becomes_a_lint_internal_finding(monkeypatch):
    def broken(scene, project, limits):
        raise KeyError("s3")
    broken.__name__ = "l6_portrait_drift"
    monkeypatch.setattr(lint, "RULES", (lint.l1_performance, broken))
    assert lint_scene(CLEAN, LintProject([CLEAN])) == [
        {"rule": "lint_internal", "severity": "info", "scene_id": "s1", "field": None,
         "message": "Правило l6_portrait_drift не смогло проверить сцену (KeyError)"}]


# --- fix round 1 (review of task 7: weak tests) -------------------------------------------------

def test_has_errors_exact():
    def f(severity):
        return {"rule": "L1", "severity": severity, "scene_id": "s1", "field": None, "message": ""}
    assert lint.has_errors([]) is False
    assert lint.has_errors([f("warning"), f("info")]) is False
    assert lint.has_errors([f("warning"), f("error"), f("info")]) is True


def test_a_broken_rule_does_not_stop_the_rules_after_it(monkeypatch):
    def broken(scene, project, limits):
        raise KeyError("s3")
    broken.__name__ = "broken_rule"
    monkeypatch.setattr(lint, "RULES", (broken, lint.l7a_duration))
    scene = dataclasses.replace(CLEAN, requested_s=16.0, frames=345)
    assert lint_scene(scene, LintProject([scene])) == [
        {"rule": "lint_internal", "severity": "info", "scene_id": "s1", "field": None,
         "message": "Правило broken_rule не смогло проверить сцену (KeyError)"},
        {"rule": "L7a", "severity": "error", "scene_id": "s1", "field": "requested_s",
         "message": "Длительность 16 с вне 3–15 с"}]


def test_findings_come_in_the_order_of_RULES():
    scene = dataclasses.replace(
        CLEAN, performance_text="", sound_text="Silence.", action_text=ACTION + " @anna smiles.",
        requested_s=16.0, frames=345, source_frame_kind="prev_last_frame", source_scene_id=None)
    assert [f[0] for f in _lint(scene)] == ["L1", "L2", "L5", "L7a", "L12"]


L7A_MSG = "Длительность {} с вне 3–15 с"


@pytest.mark.parametrize("over, expected", [
    ({"requested_s": 15.0, "frames": 345},
     [("L7a", "info", "s1", "requested_s", "345 кадров = 14.375 с")]),
    ({"requested_s": 2.9, "frames": 73},
     [("L7a", "error", "s1", "requested_s", L7A_MSG.format("2.9"))]),
    ({"requested_s": 14.0, "frames": 345},
     [("L7a", "info", "s1", "requested_s", "345 кадров = 14.375 с")]),
    ({"requested_s": 7.0, "frames": 192},
     [("L7a", "info", "s1", "requested_s", "192 кадра = 8.000 с")]),
    ({"requested_s": 3.0, "frames": 73},
     [("L7a", "info", "s1", "requested_s", "73 кадра = 3.042 с")]),
])
def test_l7a_bounds_and_wording(over, expected):
    assert _lint(dataclasses.replace(CLEAN, **over)) == expected


@pytest.mark.parametrize("n, word", [
    (1, "кадр"), (2, "кадра"), (4, "кадра"), (5, "кадров"), (10, "кадров"), (11, "кадров"),
    (12, "кадров"), (14, "кадров"), (21, "кадр"), (22, "кадра"), (111, "кадров"),
    (112, "кадров"), (114, "кадров"), (121, "кадр"), (73, "кадра"), (192, "кадра"),
    (345, "кадров"),
])
def test_ru_frames(n, word):
    assert lint.ru_frames(n) == word


@pytest.mark.parametrize("extra, fires", [
    (" Three birds rise.", False), (" 3 birds rise.", False),
    (" 4 birds rise.", True), (" 12 birds rise.", True), (" Dozens of birds rise.", True),
])
def test_l9_bounds(extra, fires):
    got = [f for f in _lint(dataclasses.replace(CLEAN, action_text=ACTION + extra))
           if f[0] == "L9"]
    assert got == ([("L9", "warning", "s1", "action_text",
                     "H3 не умеет считать больше трёх: замените число на «несколько» или уберите")]
                   if fires else [])


@pytest.mark.parametrize("perf, fires", [
    ("Her gaze searches the gaps between the trees, then she waits.", False),    # gaze + change
    ("Her gaze searches the trees while her fingers loosen.", False),            # gaze + hands
    ("A faint smile, then her jaw tightens.", False),                            # face + change
    ("Her fingers loosen around the strap of her bag, slowly.", True),           # hands only
    ("Her gaze searches the gaps between the trees.", True),                     # gaze only
])
def test_l1_needs_exactly_two_categories(perf, fires):
    got = [f for f in _lint(dataclasses.replace(CLEAN, performance_text=perf)) if f[0] == "L1"]
    assert got == ([("L1", "warning", "s1", "performance_text", L1_MSG)] if fires else [])


def test_l5_fires_in_text_mode_and_names_the_field():
    msg = "@тег работает только в режиме «Референсы»"
    assert _lint(dataclasses.replace(CLEAN, mode="text", action_text=ACTION + " @anna nods.")) == [
        ("L5", "error", "s1", "action_text", msg)]
    assert _lint(dataclasses.replace(CLEAN, sound_text="Boots on gravel, @anna breathes.")) == [
        ("L5", "error", "s1", "sound_text", msg)]


@pytest.mark.parametrize("sound, fires", [
    ("Silence.", True), ("Nothing.", True), ("It is not heard here.", True),
    ("Linen.", False),                           # no negation at all: nothing to warn about
])
def test_l2_single_negations(sound, fires):
    got = [f for f in _lint(dataclasses.replace(CLEAN, sound_text=sound)) if f[0] == "L2"]
    assert got == ([("L2", "warning", "s1", "sound_text", L2_MSG)] if fires else [])


def test_l6_transitive_chain_through_a_scene_without_the_hero():
    """s1 (full phrase) -> s2 (hero off screen) -> s3 (drift): s3 is in s1's chain only through s2."""
    s1 = _scene_with("s1", 0)
    s2 = _scene_with("s2", 1, "prev_last_frame", "s1", tags=())
    s3 = _scene_with("s3", 2, "prev_last_frame", "s2", action=DRIFTED)
    project = LintProject(scenes=[s1, s2, s3], portrait_phrases={"anna": PHRASE})
    got = {s.id: [(f["rule"], f["scene_id"]) for f in lint_scene(s, project)] for s in (s1, s2, s3)}
    assert got == {"s1": [], "s2": [], "s3": [("L6", "s3")]}


BOB = "a tall man in a green coat"
BOB_DRIFTED = "a tall man in a grey coat"


def test_l6_two_heroes_each_judged_on_its_own():
    """The second scene carries both heroes; only bob drifted, and anna is on screen there alone."""
    first = dataclasses.replace(CLEAN, action_text=f"{ACTION} {BOB}.", cast_tags=("bob",))
    second = dataclasses.replace(
        CLEAN, id="s2", idx=1, action_text=f"{ACTION} {BOB_DRIFTED}.", cast_tags=("anna", "bob"),
        source_frame_kind="prev_last_frame", source_scene_id="s1")
    project = LintProject(scenes=[first, second], portrait_phrases={"anna": PHRASE, "bob": BOB})
    got = [(f["rule"], f["scene_id"], f["message"]) for f in lint_scene(second, project)]
    assert got == [("L6", "s2", l6("s2", "bob", BOB)[4])]
    assert lint_scene(first, project) == []


def test_l6_two_heroes_both_drifted_are_reported_in_cast_order():
    first = dataclasses.replace(CLEAN, action_text=f"{ACTION} {BOB}.", cast_tags=("anna", "bob"))
    second = dataclasses.replace(
        CLEAN, id="s2", idx=1, action_text=f"{DRIFTED} {BOB_DRIFTED}.", cast_tags=("anna", "bob"),
        source_frame_kind="prev_last_frame", source_scene_id="s1")
    project = LintProject(scenes=[first, second], portrait_phrases={"anna": PHRASE, "bob": BOB})
    got = [(f["rule"], f["scene_id"], f["message"]) for f in lint_scene(second, project)]
    assert got == [("L6", "s2", l6("s2", "anna", PHRASE)[4]), ("L6", "s2", l6("s2", "bob", BOB)[4])]
