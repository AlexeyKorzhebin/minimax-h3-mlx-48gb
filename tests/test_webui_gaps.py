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


GRID_TABLE = [3, 4, 5, 8, 10.3, 15]


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
        '<div class="scene-edit-row">'
        '<label>Длительность <input class="inp num" type="number" step="0.5" min="3" max="15" '
        'data-scene-field="duration" data-idx="0" value="5"> с</label>'
        '<span class="hint grid-hint" data-idx="0">на сетке: 5,17 с</span>'
        '<label>Сид <input class="inp num" type="number" min="0" data-scene-field="seed" '
        'data-idx="0" value="" placeholder="по умолчанию: 42"></label>'
        '<button type="button" class="ghost" data-act="scene-seed-random" data-idx="0">случайный</button>'
        '<label>Шаги <input class="inp num" type="number" min="2" max="100" '
        'data-scene-field="steps" data-idx="0" value="" placeholder="50"></label>'
        '</div></div>'
        '<div class="scene-editor-acts">'
        '<button type="button" class="ghost" data-act="scene-add" data-id="p1">+ Сцена</button>'
        '<button type="button" class="inverse" data-act="scenes-save" data-id="p1">Сохранить сценарий</button>'
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
        '</div></div>'
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
