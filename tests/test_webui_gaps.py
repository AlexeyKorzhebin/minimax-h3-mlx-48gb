"""Wave 1.5 UI (spec docs/superpowers/specs/2026-10-07-panel-ui-gaps-design.md): pure functions of
app.js through node with exact expected values, DOM wiring through tests/_ui_gaps_check.mjs."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from h3_48gb import web

from test_web import _needs_node, _node_eval, _page_text

_GAPS = Path(__file__).resolve().parent / "_ui_gaps_check.mjs"
_APP_URL = (Path(__file__).resolve().parent.parent / "h3_48gb" / "webui" / "app.js").as_uri()


def _gaps(scenario: str):
    env = {k: v for k, v in os.environ.items() if k != "NODE_OPTIONS"}
    result = subprocess.run([shutil.which("node"), str(_GAPS), _APP_URL, scenario],
                            capture_output=True, text=True, timeout=60, env=env)
    assert result.returncode == 0, f"{scenario}: {result.stderr}"
    return json.loads(result.stdout)


def _js(expr: str):
    return _node_eval(f"console.log(JSON.stringify({expr}));")


#: Every pure markup function whose classes must have a rule in style.css. Tasks 7-12 append.
CLASS_SOURCES = [
    "app.libraryCardsHtml([{tag: '@a', kind: 'person', version: 1, latest_version: 1, "
    "description: 'd', assets: ['/o/library/a/v1/01-a.png'], versions: []}], '/o')",
    "app.projectSettingsHtml({id: 'p1', i2v_prefix: '', seed: null}, 'sglang')",
    "app.projectReferencesHtml({id: 'p1'}, [{tag: '@a', kind: 'person', version: 1, "
    "latest_version: 1, versions: []}], [{tag: '@a', version: 1}])",
    "app.projectRouteHtml({id: 'p1', route: [{stage: 'upscale', enabled: true}]}, 'sglang')",
    "app.projectUpscaleHtml({id: 'p1', stages: {upscale: 'failed'}, "
    "route: [{stage: 'upscale', enabled: true}]}, 'sglang')",
    "app.projectTagWarningsHtml({kind: 'video', references: [], scenes: [{idx: 0, prompt: 'x'}]}, "
    "'sglang')",
    "app.sceneEditorHtml([{prompt: 'a', duration: 8, fresh_start: false, seed: null, steps: null, "
    "start_image: null, refs: []}, {prompt: 'b', duration: 5, fresh_start: true, seed: null, "
    "steps: null, start_image: null, refs: []}], {id: 'p1', engine: 'sglang', projectSeed: null, "
    "dirty: true, epoch: 0})",
    "app.sceneEditorHtml([{prompt: 'a', duration: 8, fresh_start: false, seed: null, steps: null, "
    "start_image: null, refs: []}], {id: 'p1', engine: 'mlx', projectSeed: null, dirty: false, "
    "epoch: 0})",
    "app.sceneEditorHtml([{prompt: 'a', duration: 8, fresh_start: false, seed: -1, steps: null, "
    "start_image: null, refs: []}], {id: 'p1', engine: 'sglang', projectSeed: null, dirty: true, "
    "epoch: 0, error: {idx: 0, message: 'm'}})",
    "app.startImageFieldHtml('@arena', [{tag: '@arena', kind: 'environment', "
    "assets: ['/o/library/arena/v1/01-o.png']}], '/o')",
    "app.sceneRefsHtml({refs: ['@hero']}, 1, ['@hero', '@arena'])",
    "app.tagHintHtml('fight @a', 8, [{tag: '@arena'}])",
    "app.scenarioJsonHtml('p1')",
]


#: Classes that only hook a handler and carry no look of their own -- a rule for them would be an
#: empty one written to please the test. Each entry says why; tasks 7-12 append.
HOOK_CLASSES = {
    "i2v-prefix": "focusout handler of the settings textarea",
    "ref-pin": "change handler of the reference checkbox",
    "lib-edit-desc": "read by saveLibraryDescription",
    "route-upscale-box": "change handler of the upscale tick",
    "draft-assembly": "click handler (button look comes from .ghost)",
    "upscale-retry": "click handler (button look comes from .ghost)",
    "lib-save": "click handler",
    "scene-edit-prompt": "the scene prompt field (look comes from .inp)",
    "scenario-json-text": "the pasted JSON field (look comes from .inp)",
    "grid-hint": "address of the in-place update of the grid hint (look comes from .hint)",
}


def _classes(html: str) -> list[str]:
    return sorted({c for group in re.findall(r'class="([^"]*)"', html) for c in group.split()})


def _styled_classes(css: str) -> set[str]:
    """Classes that are the *last* compound of some selector: `.lib-card img` styles the img, not
    `.lib-card`; `.lib-card:hover`, `.a.lib-card` and `.lib-card` itself count."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    styled = set()
    for selectors in re.findall(r"([^{}]+)\{", css):
        if selectors.strip().startswith("@"):
            continue
        for selector in selectors.split(","):
            last = re.split(r"[\s>+~]+", selector.strip())[-1]
            styled.update(re.findall(r"\.([A-Za-z0-9_-]+)", re.sub(r"::?[\w-]+(\([^)]*\))?", "", last)))
    return styled


@_needs_node
def test_every_class_the_new_blocks_emit_has_a_rule():
    html = _js("[" + ",".join(CLASS_SOURCES) + "].join('')")
    styled = _styled_classes(_page_text("style.css"))
    assert [c for c in _classes(html) if c not in styled and c not in HOOK_CLASSES] == []


def test_styled_classes_ignores_descendant_rules():
    assert _styled_classes(".lib-card img { x: 1 } .a .b:hover, .c > .d.e { y: 2 }") == {
        "b", "d", "e"}


@_needs_node
def test_default_project_title_and_request():
    when = "new Date(2026, 9, 7, 14, 5)"
    assert _js(f"app.defaultProjectTitle({when})") == "Ролик 07.10 14:05"
    assert _js(f"app.newVideoRequest('  ', {when})") == {"kind": "video", "title": "Ролик 07.10 14:05"}
    assert _js(f"app.newVideoRequest(' Бой ', {when})") == {"kind": "video", "title": "Бой"}


def test_the_projects_zone_has_the_two_new_buttons():
    page = _page_text("index.html")
    zone = re.search(r'<section class="panel section-gap" id="projects">\s*(<div class="panel-head">.*?</div>)\s*<div class="proj-list"',
                     page, flags=re.S).group(1)
    assert re.sub(r"\s+", " ", zone) == (
        '<div class="panel-head"> <span class="num" aria-hidden="true">3</span> <h2>Проекты</h2> '
        '<div class="spacer"></div> <span class="eyebrow" id="projects-sum"></span> '
        '<button class="ghost" id="project-new-chat" type="button">Новый через диалог</button> '
        '<button class="inverse" id="project-new-video" type="button">+ Новый проект</button> </div>')


@_needs_node
def test_new_video_creates_and_opens_the_project():
    assert _gaps("new_video") == {
        "prompts": ["Название проекта (пусто — по дате):"],
        "posts": [["/api/projects", {"kind": "video", "title": "Бой"}]],
        "opened": True, "title": "Бой"}


@_needs_node
def test_new_video_cancelled_creates_nothing():
    assert _gaps("new_video_cancel") == {"posts": [], "alerts": [], "errHtml": ""}


@_needs_node
def test_new_chat_opens_an_empty_session_on_sglang():
    assert _gaps("new_chat") == {"posts": [["/api/chat", {
        "source": {"kind": "new"}, "prompt": "", "mode": "", "image": "", "end_image": "",
        "duration": 10}]]}


# the last two are ties (x.5 frames): Python rounds them to even, Math.round up
GRID_TABLE = [3, 4, 5, 8, 10.3, 15, 3.3541666666666665, 4.104166666666667]


@_needs_node
def test_grid_hint_matches_the_server_snap():
    server = [[web._snap_video_scenes_sglang([{"idx": idx, "duration": d, "fresh_start": fs}])[0]
               ["duration"] for idx, fs in ((0, False), (1, False), (1, True))] for d in GRID_TABLE]
    page = _js(f"{GRID_TABLE}.map((d) => [app.sglangGridSeconds(d, false), "
               "app.sglangGridSeconds(d, true), app.sglangGridSeconds(d, false)])")
    assert page == server
    # the literal table, so a change of the rule on both sides is still seen
    assert server[2] == [5.166666666666667, 5.125, 5.166666666666667]
    assert server[5] == [14.375, 14.333333333333334, 14.375]
    assert server[6][0] == 3.0416666666666665 and server[7][0] == 3.75


@_needs_node
def test_grid_hint_text_and_bounds():
    assert _js("[app.gridHint(5.166666666666667), app.gridHint(8), app.gridHint(3.0416666666666665)]") \
        == ["на сетке: 5,17 с", "на сетке: 8 с", "на сетке: 3,04 с"]
    assert _js("[app.sceneBounds('sglang'), app.sceneBounds('mlx')]") == [
        {"min": 3, "max": 15}, {"min": 5, "max": 10}]


DRAFT = ("[{prompt: '@a walks', duration: 8, fresh_start: false, seed: null, steps: null, "
         "start_image: '@arena', refs: []}, {prompt: '@a runs', duration: 5, fresh_start: true, "
         "seed: 7, steps: 30, start_image: null, refs: []}]")


@_needs_node
def test_scenes_payload_sends_only_what_is_set():
    assert _js(f"app.scenesPayload({DRAFT}, 'sglang')") == {"scenes": [
        {"prompt": "@a walks", "duration": 8, "start_image": "@arena"},
        {"prompt": "@a runs", "duration": 5, "fresh_start": True, "seed": 7, "steps": 30}]}
    assert _js(f"app.scenesPayload({DRAFT}, 'mlx')") == {"scenes": [
        {"prompt": "@a walks", "duration": 8, "start_image": "@arena"},
        {"prompt": "@a runs", "duration": 5, "fresh_start": True}]}
    with_refs = DRAFT.replace("refs: []}]", "refs: ['@a', '@b']}]")
    assert _js(f"app.scenesPayload({with_refs}, 'sglang')")["scenes"][1]["refs"] == ["@a", "@b"]
    assert "refs" not in _js(f"app.scenesPayload({with_refs}, 'mlx')")["scenes"][1]


@_needs_node
def test_move_scene_keeps_start_image_at_position_0():
    assert _js(f"app.moveScene({DRAFT}, 1, -1)") == [
        {"prompt": "@a runs", "duration": 5, "fresh_start": False, "seed": 7, "steps": 30,
         "start_image": "@arena", "refs": []},
        {"prompt": "@a walks", "duration": 8, "fresh_start": False, "seed": None, "steps": None,
         "start_image": None, "refs": []}]
    assert _js(f"app.moveScene({DRAFT}, 0, -1)") == _js(DRAFT)   # off the edge: unchanged
    assert _js(f"app.removeScene({DRAFT}, 0)") == [
        {"prompt": "@a runs", "duration": 5, "fresh_start": False, "seed": 7, "steps": 30,
         "start_image": "@arena", "refs": []}]
    assert _js(f"app.addScene({DRAFT})")[2] == {
        "prompt": "", "duration": 5, "fresh_start": False, "seed": None, "steps": None,
        "start_image": None, "refs": []}


@_needs_node
def test_client_error_names_the_scene():
    assert _js("app.scenesClientError([{prompt: 'x', duration: 8}, {prompt: ' ', duration: 8}], 'sglang')") \
        == "Сцена #1: пустой промпт"
    assert _js("app.scenesClientError([{prompt: 'x', duration: 16}], 'sglang')") \
        == "Сцена #0: длительность 3–15 с"
    assert _js("app.scenesClientError([{prompt: 'x', duration: 4}], 'mlx')") \
        == "Сцена #0: длительность 5–10 с"
    assert _js("app.scenesClientError([{prompt: 'x', duration: 4}], 'sglang')") is None


@_needs_node
def test_client_error_checks_seed_and_steps_on_sglang_only():
    def err(field, value, engine="sglang"):
        return _js("app.scenesClientError([{prompt: 'x', duration: 8}, {prompt: 'y', duration: 8, "
                   f"{field}: {'null' if value is None else value}}}], '{engine}')")
    assert err("seed", 1.5) == "Сцена #1: сид — целое число от 0"
    assert err("seed", -1) == "Сцена #1: сид — целое число от 0"
    assert err("steps", 1) == "Сцена #1: шаги — целое число от 2 до 100"
    assert err("steps", 101) == "Сцена #1: шаги — целое число от 2 до 100"
    assert err("steps", 30.5) == "Сцена #1: шаги — целое число от 2 до 100"
    assert [err("seed", 0), err("seed", 305), err("steps", 2), err("steps", 100), err("seed", None)] \
        == [None] * 5
    assert err("seed", 1.5, "mlx") is None     # the field is not on the page and never sent


@_needs_node
def test_editor_html_marks_the_scene_with_a_client_error():
    html = _js("app.sceneEditorHtml([{prompt: 'a', duration: 8, fresh_start: false, seed: 1.5, "
               "steps: null, start_image: null, refs: []}], {id: 'p1', engine: 'sglang', "
               "projectSeed: null, dirty: true, epoch: 0, error: {idx: 0, "
               "message: 'Сцена #0: сид — целое число от 0'}})")
    assert '</div><span class="hint bad scene-edit-error" data-idx="0">' \
        'Сцена #0: сид — целое число от 0</span></div><div class="scene-editor-acts">' in html


def test_disabled_ghost_buttons_look_disabled():
    css = re.sub(r"\s+", " ", _page_text("style.css"))
    assert "button.ghost:disabled {" in css


@_needs_node
def test_seed_helpers():
    assert _js("[app.randomSeed(() => 0.5), app.seedPlaceholder(305), app.seedPlaceholder(null)]") \
        == [1073741824, "по проекту: 305", "по умолчанию: 42"]


@_needs_node
def test_editor_html_for_one_scene_on_sglang():
    html = _js("app.sceneEditorHtml([{prompt: 'a <b>', duration: 5, fresh_start: false, seed: null, "
               "steps: null, start_image: null, refs: []}], {id: 'p1', engine: 'sglang', "
               "projectSeed: null, dirty: false, epoch: 0})")
    assert html == (
        '<div class="scene-editor" data-id="p1" data-epoch="0">'
        '<p class="hint scene-editor-note">Шаги: по умолчанию 50; 25 — черновик, вдвое быстрее, '
        'мягче лица и руки.</p>'
        '<div class="scene-edit" data-idx="0"><div class="scene-edit-head">'
        '<span class="idx">#0</span><div class="spacer"></div>'
        '<button type="button" class="ghost" data-act="scene-up" data-idx="0" disabled>↑</button>'
        '<button type="button" class="ghost" data-act="scene-down" data-idx="0" disabled>↓</button>'
        '</div>'
        '<textarea class="inp scene-edit-prompt" data-scene-field="prompt" data-idx="0" rows="4">'
        'a &lt;b&gt;</textarea>'
        '<div class="tag-hint-slot" data-idx="0"></div>'
        '<div class="scene-edit-row">'
        '<label>Длительность <input class="inp num" type="number" step="0.5" min="3" max="15" '
        'data-scene-field="duration" data-idx="0" value="5"> с</label>'
        '<span class="hint grid-hint" data-idx="0">на сетке: 5,17 с</span>'
        '<label>Сид <input class="inp num" type="number" min="0" data-scene-field="seed" '
        'data-idx="0" value="" placeholder="по умолчанию: 42"></label>'
        '<button type="button" class="ghost" data-act="scene-seed-random" data-idx="0">случайный</button>'
        '<label>Шаги <input class="inp num" type="number" min="2" max="100" '
        'data-scene-field="steps" data-idx="0" value="" placeholder="50"></label>'
        '</div>'
        '<div class="start-image"><label>Стартовый кадр '
        '<select class="inp" data-scene-field="start_image" data-idx="0">'
        '<option value="" selected>без кадра</option></select></label> '
        '<button type="button" class="ghost" data-act="scene0-upload">Загрузить кадр…</button></div>'
        '</div>'
        '<div class="scene-editor-acts">'
        '<button type="button" class="ghost" data-act="scene-add" data-id="p1">+ Сцена</button>'
        '<button type="button" class="inverse" data-act="scenes-save" data-id="p1">Сохранить сценарий</button>'
        '<span class="dirty-note" hidden>не сохранено</span>'
        '</div></div>')


@_needs_node
def test_editor_html_on_mlx():
    html = _js("app.sceneEditorHtml([{prompt: 'a cat', duration: 8, fresh_start: false, seed: null, "
               "steps: null, start_image: null, refs: []}, {prompt: 'a dog', duration: 6, "
               "fresh_start: true, seed: null, steps: null, start_image: null, refs: []}], "
               "{id: 'p1', engine: 'mlx', projectSeed: null, dirty: true, epoch: 0})")
    assert html == (
        '<div class="scene-editor" data-id="p1" data-epoch="0">'
        '<div class="scene-edit" data-idx="0"><div class="scene-edit-head">'
        '<span class="idx">#0</span><div class="spacer"></div>'
        '<button type="button" class="ghost" data-act="scene-up" data-idx="0" disabled>↑</button>'
        '<button type="button" class="ghost" data-act="scene-down" data-idx="0">↓</button>'
        '<button type="button" class="ghost" data-act="scene-del" data-idx="0">Удалить</button>'
        '</div>'
        '<textarea class="inp scene-edit-prompt" data-scene-field="prompt" data-idx="0" rows="4">'
        'a cat</textarea>'
        '<div class="scene-edit-row">'
        '<label>Длительность <input class="inp num" type="number" step="0.5" min="5" max="10" '
        'data-scene-field="duration" data-idx="0" value="8"> с</label>'
        '</div>'
        '<div class="start-image"><label>Стартовый кадр '
        '<select class="inp" data-scene-field="start_image" data-idx="0">'
        '<option value="" selected>без кадра</option></select></label> '
        '<button type="button" class="ghost" data-act="scene0-upload">Загрузить кадр…</button></div>'
        '</div>'
        '<div class="scene-edit" data-idx="1"><div class="scene-edit-head">'
        '<span class="idx">#1</span><div class="spacer"></div>'
        '<button type="button" class="ghost" data-act="scene-up" data-idx="1">↑</button>'
        '<button type="button" class="ghost" data-act="scene-down" data-idx="1" disabled>↓</button>'
        '<button type="button" class="ghost" data-act="scene-del" data-idx="1">Удалить</button>'
        '</div>'
        '<textarea class="inp scene-edit-prompt" data-scene-field="prompt" data-idx="1" rows="4">'
        'a dog</textarea>'
        '<div class="scene-edit-row">'
        '<label>Длительность <input class="inp num" type="number" step="0.5" min="5" max="10" '
        'data-scene-field="duration" data-idx="1" value="6"> с</label>'
        '<label class="fresh-start-toggle"><input type="checkbox" data-scene-field="fresh_start" '
        'data-idx="1" checked> начать с чистого листа</label>'
        '</div></div>'
        '<div class="scene-editor-acts">'
        '<button type="button" class="ghost" data-act="scene-add" data-id="p1">+ Сцена</button>'
        '<button type="button" class="inverse" data-act="scenes-save" data-id="p1">Сохранить сценарий</button>'
        '<span class="dirty-note">не сохранено</span>'
        '</div></div>')


EDITOR_EXPECTED = {
    "editor_dom_edits_survive_add": {"puts": [["/api/projects/p1/scenes", {"scenes": [
        {"prompt": "@a jumps", "duration": 8, "start_image": "@arena"},
        {"prompt": "@a runs", "duration": 5, "fresh_start": True, "seed": 305},
        {"prompt": "@a rests", "duration": 5, "fresh_start": False}]}]]},
    "editor_edits_survive_ref_pin": {"puts": [["/api/projects/p1/scenes", {"scenes": [
        {"prompt": "@a jumps", "duration": 8, "start_image": "@arena"},
        {"prompt": "@a runs", "duration": 5, "fresh_start": True}]}]]},
    "editor_approve_saves_first": {"writes": [["PUT", "/api/projects/p1/scenes"],
                                              ["POST", "/api/projects/p1/approve/script"]]},
    "editor_approve_stops_on_save_error": {"writes": [["PUT", "/api/projects/p1/scenes"]],
                                           "errorHidden": False},
    "editor_client_error": {"puts": [],
                            "error": "<b>Запрос не прошёл</b><pre>Сцена #2: пустой промпт</pre>"},
    "editor_grid_hint_live": {"hint": "на сетке: 3,75 с"},
    # a scene that stops being fresh is chained: 5 s lands on 17n+4 frames, not 17n+5
    "editor_grid_hint_follows_fresh_start": {"hint": "на сетке: 5,13 с"},
    # the save answer replaces the draft at once (the re-read may fail): the second save sends the
    # server's text, not the one typed before the first
    "editor_save_resets_from_answer": {"puts": [
        ["/api/projects/p1/scenes", ["@a jumps", "@a runs"]],
        ["/api/projects/p1/scenes", ["@a server text", "@a runs"]]]},
    "editor_save_resets_when_reread_fails": {"puts": [
        ["/api/projects/p1/scenes", ["@a jumps", "@a runs"]],
        ["/api/projects/p1/scenes", ["@a server text", "@a runs"]]]},
    "editor_client_error_seed": {"puts": [], "inline": '<span class="hint bad scene-edit-error" '
        'data-idx="1">Сцена #1: сид — целое число от 0</span>'},
    "editor_dirty_note_on_input": {"hiddenBefore": True, "hiddenAfter": False},
    "editor_close_unsaved": {"confirms": ["Закрыть без сохранения сценария?"], "hidden": False},
    # Н1: after «↓» the stale DOM (old idx 0 = «@a walks far») must not overwrite the moved draft
    "editor_move_keeps_scenes": {"puts": [["/api/projects/p1/scenes", {"scenes": [
        {"prompt": "@a runs fast", "duration": 5, "start_image": "@arena"},
        {"prompt": "@a walks far", "duration": 8, "fresh_start": False}]}]]},
    # Н3: deleting a project with an unsaved draft asks only the delete question
    "delete_project_no_draft_question": {"confirms": [
        "Удалить проект целиком? Файлы (клипы, трек, сборка) удаляются с диска."]},
}


@_needs_node
@pytest.mark.parametrize("scenario", sorted(EDITOR_EXPECTED))
def test_editor_wiring(scenario):
    assert _gaps(scenario) == EDITOR_EXPECTED[scenario]


@_needs_node
def test_insert_tag_at_the_caret():
    assert _js("app.insertTagAt('@ama walks', 4, '@amazon')") == {"text": "@amazon walks", "caret": 8}
    assert _js("app.insertTagAt('walks ', 6, '@amazon')") == {"text": "walks @amazon ", "caret": 14}
    assert _js("app.insertTagAt('fight @a', 8, '@amazon')") == {"text": "fight @amazon ", "caret": 14}


@_needs_node
def test_tag_hint_html_lists_pinned_matches_as_buttons():
    cards = "[{tag: '@amazon'}, {tag: '@arena'}, {tag: '@bob'}]"
    assert _js(f"app.tagHintHtml('fight @a', 8, {cards})") == (
        '<div class="tag-hint">'
        '<button type="button" class="tag-pick" data-act="tag-pick" data-tag="@amazon">@amazon</button>'
        '<button type="button" class="tag-pick" data-act="tag-pick" data-tag="@arena">@arena</button>'
        '</div>')
    assert _js(f"app.tagHintHtml('fight', 5, {cards})") == ""


@_needs_node
def test_a_scene_with_refs_needs_no_tag_in_the_text():
    assert _js("app.sceneTagIssues('a cat', ['@a'], {needsTag: true, refs: ['@a']})") == []
    assert _js("app.sceneTagIssues('a cat', ['@a'], {needsTag: true})") == [
        {"tag": None, "problem": "missing"}]


PINNED = ("[{tag: '@hero', kind: 'person', assets: ['/o/library/hero/v1/01-h.png']}, "
          "{tag: '@arena', kind: 'environment', assets: ['/o/library/arena/v1/01-o.png']}, "
          "{tag: '@voice', kind: 'voice', assets: ['/o/library/voice/v1/01-v.mp3']}]")


@_needs_node
def test_start_image_field():
    head = ('<div class="start-image"><label>Стартовый кадр '
            '<select class="inp" data-scene-field="start_image" data-idx="0">')
    tail = ('</select></label> '
            '<button type="button" class="ghost" data-act="scene0-upload">Загрузить кадр…</button>')
    assert _js(f"app.startImageFieldHtml(null, {PINNED}, '/o')") == (
        head + '<option value="" selected>без кадра</option>'
        '<option value="@hero">@hero — кадр карточки</option>'
        '<option value="@arena">@arena — кадр карточки</option>' + tail + '</div>')
    assert _js(f"app.startImageFieldHtml('@arena', {PINNED}, '/o')") == (
        head + '<option value="">без кадра</option>'
        '<option value="@hero">@hero — кадр карточки</option>'
        '<option value="@arena" selected>@arena — кадр карточки</option>' + tail
        + '<img class="start-thumb" src="/media/library/arena/v1/01-o.png" alt=""></div>')
    assert _js(f"app.startImageFieldHtml('/o/uploads/open.png', {PINNED}, '/o')") == (
        head + '<option value="">без кадра</option>'
        '<option value="@hero">@hero — кадр карточки</option>'
        '<option value="@arena">@arena — кадр карточки</option>'
        '<option value="/o/uploads/open.png" selected>open.png</option>' + tail
        + '<img class="start-thumb" src="/media/uploads/open.png" alt=""></div>')


@_needs_node
def test_scene_refs_field():
    assert _js("app.sceneRefsHtml({refs: ['@hero']}, 1, ['@hero', '@arena'])") == (
        '<div class="scene-refs"><span class="scene-refs-label">Референсы без упоминания:</span> '
        '<label><input type="checkbox" data-scene-field="refs" data-idx="1" data-tag="@hero" checked> @hero</label> '
        '<label><input type="checkbox" data-scene-field="refs" data-idx="1" data-tag="@arena"> @arena</label> '
        '<span class="hint">их картинки идут первыми: &lt;Picture 1…&gt;</span></div>')


@_needs_node
@pytest.mark.parametrize("text, expected", [
    ('[{"prompt": "@a", "duration": 5}]', {"body": {"scenes": [{"prompt": "@a", "duration": 5}]}}),
    ('{"scenes": [{"prompt": "@a", "duration": 5}], "references": [{"tag": "@a", "version": 1}], "x": 1}',
     {"body": {"scenes": [{"prompt": "@a", "duration": 5}], "references": [{"tag": "@a", "version": 1}]}}),
    ('42', {"error": 'Ожидается {"scenes": […]} или список сцен'}),
    ('{"scenes": 3}', {"error": 'Ожидается {"scenes": […]} или список сцен'}),
    ('{"scenes": [', {"error": "JSON не разобрался — проверьте запятые и кавычки"}),
])
def test_parse_scenario_json(text, expected):
    assert _js(f"app.parseScenarioJson({json.dumps(text)})") == expected


@_needs_node
def test_replace_confirm_counts_scenes():
    assert _js("[1, 2, 5].map(app.scenarioReplaceConfirm)") == [
        "Заменить 1 сцену сценария?", "Заменить 2 сцены сценария?", "Заменить 5 сцен сценария?"]


PICK = ('<div class="tag-hint">'
        '<button type="button" class="tag-pick" data-act="tag-pick" data-tag="@amazon">@amazon</button>'
        '<button type="button" class="tag-pick" data-act="tag-pick" data-tag="@arena">@arena</button></div>')
EDITOR8_EXPECTED = {
    "tag_hint_on_input": {"slot": PICK},
    "tag_pick": {"value": "fight @amazon ", "prompt0": "fight @amazon "},
    "scene0_upload": {"upload": {"url": "/api/uploads",
                                 "headers": {"Content-Type": "application/octet-stream",
                                             "X-Filename": "open.png"},
                                 "body": {"raw": "open.png"}},
                      "start_image": "/o/uploads/open.png"},
    "json_load": {"confirms": ["Заменить 2 сцены сценария?"],
                  "puts": [["/api/projects/p1/scenes", {"scenes": [{"prompt": "@a", "duration": 5}]}]]},
    # Н1: the redraw after the JSON answer must not pour the old scenes' fields back into the draft
    "json_load_not_clobbered": {"puts": [
        ["/api/projects/p1/scenes", {"scenes": [{"prompt": "@a", "duration": 5}]}],
        ["/api/projects/p1/scenes", {"scenes": [{"prompt": "@a", "duration": 5}]}]]},
    # picking «без кадра» drops the thumbnail at once: the select change redraws from the draft
    "start_image_select_redraws": {"selected": ['<option value="" selected>без кадра</option>'],
                                   "thumb": False},
    "refs_ride_along": {"refs": [None, ["@arena"]]},
    "json_load_bad": {"puts": [], "error": "<b>Запрос не прошёл</b><pre>Ожидается {&quot;scenes&quot;: […]} "
                                          "или список сцен</pre>"},
}


@_needs_node
@pytest.mark.parametrize("scenario", sorted(EDITOR8_EXPECTED))
def test_editor_refs_and_json_wiring(scenario):
    assert _gaps(scenario) == EDITOR8_EXPECTED[scenario]


@_needs_node
def test_pinned_cards_take_the_pinned_version():
    cards = ("[{tag: '@a', kind: 'person', assets: ['/o/library/a/v2/x.png'], versions: ["
             "{version: 1, kind: 'person', assets: ['/o/library/a/v1/x.png']}, "
             "{version: 2, kind: 'person', assets: ['/o/library/a/v2/x.png']}]}, "
             "{tag: '@b', kind: 'voice', assets: ['/o/library/b/v1/v.mp3']}]")
    assert _js(f"app.pinnedCards([{{tag: '@a', version: 1}}, {{tag: '@gone', version: 1}}, "
               f"{{tag: '@b', version: 1}}], {cards})") == [
        {"tag": "@a", "kind": "person", "assets": ["/o/library/a/v1/x.png"]},
        {"tag": "@b", "kind": "voice", "assets": ["/o/library/b/v1/v.mp3"]}]


@_needs_node
def test_start_image_thumb_never_leaves_the_media_root():
    # the project-level start_image is not checked by the server: no URL for what is not an image
    # under the outdir, nor for a path that climbs out of it
    for bad in ("/etc/passwd.png", "/o/../etc/x.png", "/o/uploads/notes.txt", "/other/x.png"):
        html = _js(f"app.startImageFieldHtml({json.dumps(bad)}, [], '/o')")
        assert "<img" not in html, bad
        assert f'<option value="{bad}" selected>' in html


@_needs_node
def test_editor_html_with_pinned_cards_and_refs():
    html = _js("app.sceneEditorHtml([{prompt: 'a', duration: 8, fresh_start: false, seed: null, "
               "steps: null, start_image: '@arena', refs: ['@hero']}], {id: 'p1', engine: 'sglang', "
               f"projectSeed: null, dirty: false, epoch: 0, pinned: {PINNED}, outdir: '/o'}})")
    refs = ('<div class="scene-refs"><span class="scene-refs-label">Референсы без упоминания:</span> '
            '<label><input type="checkbox" data-scene-field="refs" data-idx="0" data-tag="@hero" checked> @hero</label> '
            '<label><input type="checkbox" data-scene-field="refs" data-idx="0" data-tag="@arena"> @arena</label> '
            '<span class="hint">их картинки идут первыми: &lt;Picture 1…&gt;</span></div>')
    assert ('<div class="tag-hint-slot" data-idx="0"></div>' + refs + '<div class="scene-edit-row">') in html
    assert '<option value="@arena" selected>@arena — кадр карточки</option>' in html
    assert '<img class="start-thumb" src="/media/library/arena/v1/01-o.png" alt="">' in html


@_needs_node
def test_scenario_json_block():
    assert _js("app.scenarioJsonHtml('p1')") == (
        '<details class="adv scenario-json"><summary>Вставить сценарий JSON</summary>'
        '<textarea class="inp scenario-json-text" rows="6" placeholder=\'{"scenes": [{"prompt": "…", '
        '"duration": 5}]}\'></textarea> '
        '<button type="button" class="ghost" data-act="scenario-json-load" data-id="p1">'
        'Загрузить сценарий</button></details>')
