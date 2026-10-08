"""Wave 1.5 UI (spec docs/superpowers/specs/2026-10-07-panel-ui-gaps-design.md): pure functions of
app.js through node with exact expected values, DOM wiring through tests/_ui_gaps_check.mjs."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from h3_48gb import project as project_module
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
    "app.h3PromptHtml({idx: 0, prompt: 'P', pictures: [{label: '<Picture 1>', path: '/o/library/a/v1/x.png'}], "
    "audios: [], keyframe: {kind: null, path: null}, duration: 8, seed: 1, steps: 50}, '/o')",
    "app.projectUpscaleHtml({id: 'p1', stages: {upscale: 'failed'}, route: [{stage: 'upscale', enabled: true}], "
    "scenes: [], upscale_report: {status: 'failed', error: 'x'}}, 'sglang')",
    "app.projectAssemblyHtml({id: 'p1', title: 'T', created_at: '2026-10-05T10:00:00', "
    "stages: {assembly: 'done'}, assembly: {final_path: '/o/projects/p1/assembly/final.mp4', v: 1}, "
    "scenes: [{idx: 0, status: 'done'}]}, '/o')",
    "app.projectSettingsHtml({id: 'p1', i2v_prefix: '', seed: 3}, 'sglang', 'locked', 'seed')",
    "app.projectRouteHtml({id: 'p1', route: [{stage: 'upscale', enabled: true}]}, 'sglang', 'locked')",
    "app.retryPanelHtml({idx: 0, prompt: 'a', steps: null}, {id: 'p1', engine: 'sglang', "
    "effectiveSeed: 42, cascade: [0]})",
    "app.sceneCardHtml({idx: 0, prompt: 'x'.repeat(300), duration: 8, status: 'done', seed: 1, steps: 30, "
    "clip_path: '/o/p/a.mp4', ltx_path: '/o/p/b.mp4'}, {projId: 'p1', outdir: '/o', deadMedia: new Set(), "
    "engine: 'sglang', projectSeed: null})",
    "app.libraryCardsHtml([{tag: '@a', kind: 'person', version: 1, latest_version: 1, "
    "description: 'd', assets: ['/o/library/a/v1/01-a.png'], versions: [{version: 1}, {version: 2}]}], '/o')",
    "app.projectReferencesHtml({id: 'p1'}, [{tag: '@a', kind: 'person', version: 1, latest_version: 1, "
    "versions: [{version: 1}]}], [], 'locked')",
    "app.sceneRefsHtml({refs: ['@gone']}, 0, ['@hero'])",
]


#: Classes that only hook a handler and carry no look of their own -- a rule for them would be an
#: empty one written to please the test. Each entry says why; tasks 7-12 append.
HOOK_CLASSES = {
    "i2v-prefix": "focusout handler of the settings textarea",
    "ref-pin": "change handler of the reference checkbox",
    "lib-edit-desc": "read by saveLibraryDescription",
    "route-upscale-box": "change handler of the upscale tick",
    "upscale-retry": "click handler (button look comes from .ghost)",
    "lib-save": "click handler",
    "scene-edit-prompt": "the scene prompt field (look comes from .inp)",
    "draft-assembly": "click handler (button look comes from .ghost)",
    "project-seed": "the project seed field (look comes from .inp)",
    "retry-prompt": "the retry prompt field (look comes from .inp)",
    "retry-seed": "the retry seed field (look comes from .inp)",
    "retry-steps": "the retry steps field (look comes from .inp)",
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
        '<button type="button" class="ghost" data-act="scene-h3-prompt" data-id="p1" data-idx="0">Промпт для H3</button>'
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
    assert _js("[1, 2, 5].map((n) => app.scenarioReplaceConfirm(n))") == [
        "Заменить 1 сцену сценария?", "Заменить 2 сцены сценария?", "Заменить 5 сцен сценария?"]
    assert _js("[app.scenarioReplaceConfirm(2, true), app.scenarioReplaceConfirm(0, true)]") == [
        "Заменить 2 сцены сценария? Несохранённые правки пропадут.",
        "Заменить несохранённый сценарий? Правки пропадут."]


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
    # the typed JSON survives an unrelated redraw (the person adds a scene meanwhile)
    "json_text_survives_redraw": {"text": '{"scenes": [{"prompt": "@a", "duration": 5}]}', "open": True},
    # a ref the project no longer pins stays ticked, with a note, and the save is refused in Russian
    "stray_ref_kept_and_refused": {"puts": [], "kept": '<label><input type="checkbox" '
        'data-scene-field="refs" data-idx="1" data-tag="@gone" checked> @gone '
        '<span class="ref-missing">не подключён к проекту</span></label>',
        "inline": '<span class="hint bad scene-edit-error" data-idx="1">Сцена #1: референс @gone не '
        'подключён к проекту — снимите галочку или подключите карточку</span>'},
    # the saved order of refs numbers <Picture k>: opening and saving must not reorder it
    "refs_order_kept": {"refs": ["@b", "@a", "@c"]},
    "json_load_asks_for_unsaved_draft": {"confirms": ["Заменить 2 сцены сценария? Несохранённые правки пропадут."],
                                         "puts": []},
    "json_load_asks_for_unsaved_in_empty_project": {
        "confirms": ["Заменить несохранённый сценарий? Правки пропадут."], "puts": []},
    "prompt_tag_issues_highlight": {"unknown": [["has-tag-issues", True]], "unknownTitle": "незнакомый тег @gone",
                                    "clean": [["has-tag-issues", False]], "cleanTitle": ""},
    "refs_known_when_library_down": {"box": '<label><input type="checkbox" data-scene-field="refs" '
        'data-idx="0" data-tag="@a" checked> @a</label>'},
    "scene0_wrong_extension": {"uploads": [], "error": "<b>Кадр не загружен</b><pre>Стартовый кадр — "
                                                       "только png или jpg</pre>"},
    "scene0_upload_after_close": {"errHidden": True},
    "refs_ride_along": {"refs": [None, ["@arena"]]},
    "json_load_bad": {"puts": [], "error": "<b>Сценарий не разобран</b><pre>Ожидается {&quot;scenes&quot;: […]} "
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
        assert _js(f"app.startImageFieldHtml({json.dumps(bad)}, [], '/o')") == (
            '<div class="start-image"><label>Стартовый кадр '
            '<select class="inp" data-scene-field="start_image" data-idx="0">'
            f'<option value="">без кадра</option><option value="{bad}" selected>{bad.split("/")[-1]}</option>'
            '</select></label> '
            '<button type="button" class="ghost" data-act="scene0-upload">Загрузить кадр…</button></div>'), bad


@_needs_node
def test_editor_html_with_pinned_cards_and_refs():
    html = _js("app.sceneEditorHtml([{prompt: 'a', duration: 8, fresh_start: false, seed: null, "
               "steps: null, start_image: '@arena', refs: ['@hero']}], {id: 'p1', engine: 'sglang', "
               f"projectSeed: null, dirty: false, epoch: 0, pinned: {PINNED}, outdir: '/o'}})")
    refs = ('<div class="scene-refs"><span class="scene-refs-label">Референсы без упоминания:</span> '
            '<label><input type="checkbox" data-scene-field="refs" data-idx="0" data-tag="@hero" checked> @hero</label> '
            '<label><input type="checkbox" data-scene-field="refs" data-idx="0" data-tag="@arena"> @arena</label> '
            '<span class="hint">их картинки идут первыми: &lt;Picture 1…&gt;</span></div>')
    assert html == (
        '<div class="scene-editor" data-id="p1" data-epoch="0">'
        '<p class="hint scene-editor-note">Шаги: по умолчанию 50; 25 — черновик, вдвое быстрее, '
        'мягче лица и руки.</p>'
        '<div class="scene-edit" data-idx="0"><div class="scene-edit-head">'
        '<span class="idx">#0</span><div class="spacer"></div>'
        '<button type="button" class="ghost" data-act="scene-up" data-idx="0" disabled>↑</button>'
        '<button type="button" class="ghost" data-act="scene-down" data-idx="0" disabled>↓</button>'
        '<button type="button" class="ghost" data-act="scene-h3-prompt" data-id="p1" data-idx="0">Промпт для H3</button>'
        '</div>'
        '<textarea class="inp scene-edit-prompt" data-scene-field="prompt" data-idx="0" rows="4">a</textarea>'
        '<div class="tag-hint-slot" data-idx="0"></div>' + refs +
        '<div class="scene-edit-row">'
        '<label>Длительность <input class="inp num" type="number" step="0.5" min="3" max="15" '
        'data-scene-field="duration" data-idx="0" value="8"> с</label>'
        '<span class="hint grid-hint" data-idx="0">на сетке: 8 с</span>'
        '<label>Сид <input class="inp num" type="number" min="0" data-scene-field="seed" '
        'data-idx="0" value="" placeholder="по умолчанию: 42"></label>'
        '<button type="button" class="ghost" data-act="scene-seed-random" data-idx="0">случайный</button>'
        '<label>Шаги <input class="inp num" type="number" min="2" max="100" '
        'data-scene-field="steps" data-idx="0" value="" placeholder="50"></label>'
        '</div>'
        '<div class="start-image"><label>Стартовый кадр '
        '<select class="inp" data-scene-field="start_image" data-idx="0">'
        '<option value="">без кадра</option>'
        '<option value="@hero">@hero — кадр карточки</option>'
        '<option value="@arena" selected>@arena — кадр карточки</option></select></label> '
        '<button type="button" class="ghost" data-act="scene0-upload">Загрузить кадр…</button>'
        '<img class="start-thumb" src="/media/library/arena/v1/01-o.png" alt=""></div>'
        '</div>'
        '<div class="scene-editor-acts">'
        '<button type="button" class="ghost" data-act="scene-add" data-id="p1">+ Сцена</button>'
        '<button type="button" class="inverse" data-act="scenes-save" data-id="p1">Сохранить сценарий</button>'
        '<span class="dirty-note" hidden>не сохранено</span>'
        '</div></div>')


@_needs_node
def test_scenario_json_block():
    assert _js("app.scenarioJsonHtml('p1')") == (
        '<details class="adv scenario-json"><summary>Вставить сценарий JSON</summary>'
        '<textarea class="inp scenario-json-text" rows="6" placeholder=\'{"scenes": [{"prompt": "…", '
        '"duration": 5}]}\'></textarea> '
        '<button type="button" class="ghost" data-act="scenario-json-load" data-id="p1">'
        'Загрузить сценарий</button></details>')


@_needs_node
def test_scenario_json_block_keeps_the_typed_text():
    assert _js("app.scenarioJsonHtml('p1', '[{\"prompt\": \"a <b>\"}]')") == (
        '<details class="adv scenario-json" open><summary>Вставить сценарий JSON</summary>'
        '<textarea class="inp scenario-json-text" rows="6" placeholder=\'{"scenes": [{"prompt": "…", '
        '"duration": 5}]}\'>[{&quot;prompt&quot;: &quot;a &lt;b&gt;&quot;}]</textarea> '
        '<button type="button" class="ghost" data-act="scenario-json-load" data-id="p1">'
        'Загрузить сценарий</button></details>')


@_needs_node
def test_a_ref_that_left_the_project_stays_visible_and_ticked():
    # the scene's own order first (it numbers <Picture k>), the unticked ones after it
    assert _js("app.sceneRefsHtml({refs: ['@gone', '@hero']}, 1, ['@hero', '@arena'])") == (
        '<div class="scene-refs"><span class="scene-refs-label">Референсы без упоминания:</span> '
        '<label><input type="checkbox" data-scene-field="refs" data-idx="1" data-tag="@gone" checked> @gone '
        '<span class="ref-missing">не подключён к проекту</span></label> '
        '<label><input type="checkbox" data-scene-field="refs" data-idx="1" data-tag="@hero" checked> @hero</label> '
        '<label><input type="checkbox" data-scene-field="refs" data-idx="1" data-tag="@arena"> @arena</label> '
        '<span class="hint">их картинки идут первыми: &lt;Picture 1…&gt;</span></div>')
    # nothing pinned at all: the stray ref is still shown
    assert "@gone" in _js("app.sceneRefsHtml({refs: ['@gone']}, 0, [])")
    assert _js("app.sceneRefsHtml({refs: []}, 0, [])") == ""


@_needs_node
def test_client_error_names_a_ref_that_is_not_pinned():
    def err(refs, pinned, engine="sglang"):
        return _js("app.scenesClientError([{prompt: 'x', duration: 8}, {prompt: 'y', duration: 8, "
                   f"refs: {refs}}}], '{engine}', {pinned})")
    assert err("['@gone']", "['@hero']") == (
        "Сцена #1: референс @gone не подключён к проекту — снимите галочку или подключите карточку")
    assert err("['@hero']", "['@hero']") is None
    assert err("['@gone']", "['@hero']", "mlx") is None     # refs are never sent on mlx
    assert err("['@gone']", "undefined") is None            # no pinned list given: not checked


@_needs_node
def test_references_payload_takes_the_chosen_version():
    assert _js("app.referencesPayload(['@a', '@b', '@c'], [{tag: '@a', version: 1}, {tag: '@b', version: 2}], {'@b': 1})") \
        == [{"tag": "@a", "version": 1}, {"tag": "@b", "version": 1}, {"tag": "@c"}]
    assert _js("app.referencesPayload(['@a'], [{tag: '@a', version: 1}])") == [{"tag": "@a", "version": 1}]


@_needs_node
def test_library_card_html_has_thumb_versions_and_actions():
    html = _js("app.libraryCardsHtml([{tag: '@alice', kind: 'person', version: 2, latest_version: 2, "
               "description: 'a woman', assets: ['/o/library/alice/v1/01-a.png'], "
               "versions: [{version: 1}, {version: 2}]}], '/o')")
    assert html == (
        '<div class="lib-card"><img class="lib-thumb" src="/media/library/alice/v1/01-a.png" alt="">'
        '<b>@alice</b> <span class="muted">person, v2, картинок: 1 · версий: 2</span><p>a woman</p>'
        '<input class="lib-edit-desc" value="a woman"> '
        '<button type="button" class="lib-save" data-tag="@alice">Сохранить описание</button>'
        '<input class="lib-new-files" type="file" multiple accept=".png,.jpg,.jpeg,.mp3,.wav"> '
        '<button type="button" class="ghost" data-act="lib-new-version" data-tag="@alice">Новая версия</button> '
        '<button type="button" class="ghost" data-act="lib-delete" data-tag="@alice">Удалить</button>'
        '<p class="why lib-card-error" hidden></p></div>')


@_needs_node
def test_library_card_caption_for_voice_and_one_version():
    html = _js("app.libraryCardsHtml([{tag: '@v', kind: 'voice', version: 1, description: '', "
               "assets: ['/o/library/v/v1/01-v.mp3'], versions: [{version: 1}]}], '/o')")
    assert '<span class="muted">voice, v1, аудио</span>' in html
    assert "<img" not in html


REF_CARDS = ("[{tag: '@a', kind: 'person', version: 2, latest_version: 2, "
             "versions: [{version: 1}, {version: 2}]}]")


@_needs_node
def test_project_references_html_with_versions_and_lock():
    assert _js(f"app.projectReferencesHtml({{id: 'p1'}}, {REF_CARDS}, [{{tag: '@a', version: 1}}], null)") == (
        '<div class="project-refs" data-id="p1"><h4>Референсы проекта</h4>'
        '<label><input type="checkbox" class="ref-pin" data-tag="@a" checked> @a '
        '<span class="muted">person, v1 (есть v2)</span></label> '
        '<select class="inp ref-version" data-tag="@a"><option value="1" selected>v1</option>'
        '<option value="2">v2</option></select></div>')
    lock = web.PROJECT_LOCK_TEXT["references"]
    assert _js(f"app.projectReferencesHtml({{id: 'p1'}}, {REF_CARDS}, [], {json.dumps(lock)})") == (
        '<div class="project-refs" data-id="p1"><h4>Референсы проекта</h4>'
        '<label><input type="checkbox" class="ref-pin" data-tag="@a" disabled> @a '
        '<span class="muted">person</span></label> '
        '<select class="inp ref-version" data-tag="@a" disabled><option value="1">v1</option>'
        '<option value="2" selected>v2</option></select>'
        f'<p class="why lock-note">{lock}</p></div>')


LIB_EXPECTED = {
    "library_preview_after_late_state": {
        "img": '<img class="lib-thumb" src="/media/library/alice/v1/01-a.png" alt="">'},
    "library_delete_in_use": {
        "confirms": ["Удалить карточку @alice? Файлы уйдут в library/.trash."],
        "cardError": {"hidden": False, "textContent": "@alice подключена к проектам: «Бой» (p1) — "
                      "отключите её там или удалите проекты"},
        "deletes": ["/api/library/alice"]},
    "library_delete_declined": {"confirms": ["Удалить карточку @alice? Файлы уйдут в library/.trash."],
                                "deletes": []},
    "library_new_version": {"uploads": ["b.png"],
                            "puts": [["/api/library/alice", {"description": "a woman",
                                                             "assets": ["/o/uploads/b.png"]}]],
                            "cardError": {"hidden": True, "textContent": ""}},
    "refs_version_change": {"puts": [["/api/projects/p1/references",
                                      {"references": [{"tag": "@a", "version": 1}]}]]},
}


@_needs_node
@pytest.mark.parametrize("scenario", sorted(LIB_EXPECTED))
def test_library_wiring(scenario):
    assert _gaps(scenario) == LIB_EXPECTED[scenario]


@_needs_node
def test_refs_keep_the_saved_order_and_unknown_is_judged_by_the_project():
    both = ('<div class="scene-refs"><span class="scene-refs-label">Референсы без упоминания:</span> '
            '<label><input type="checkbox" data-scene-field="refs" data-idx="0" data-tag="@arena" checked> @arena</label> '
            '<label><input type="checkbox" data-scene-field="refs" data-idx="0" data-tag="@hero" checked> @hero</label> '
            '<span class="hint">их картинки идут первыми: &lt;Picture 1…&gt;</span></div>')
    assert _js("app.sceneRefsHtml({refs: ['@arena', '@hero']}, 0, ['@hero', '@arena'])") == both
    # a voice tag or a card the library lost is still pinned by the project: ticked, no note
    voice = ('<div class="scene-refs"><span class="scene-refs-label">Референсы без упоминания:</span> '
             '<label><input type="checkbox" data-scene-field="refs" data-idx="0" data-tag="@voice" checked> @voice</label> '
             '<span class="hint">их картинки идут первыми: &lt;Picture 1…&gt;</span></div>')
    assert _js("app.sceneRefsHtml({refs: ['@voice']}, 0, [], ['@voice'])") == voice


@_needs_node
def test_lock_texts_are_the_same_on_both_sides():
    assert _js("app.PROJECT_LOCK_TEXT") == web.PROJECT_LOCK_TEXT


@_needs_node
def test_project_locks():
    assert _js("app.projectLocks({}, null)") == {"references": None, "settings": None, "route": None}
    assert _js("app.projectLocks({}, {kind: 'scene'})") == {
        "references": web.PROJECT_LOCK_TEXT["references"],
        "settings": web.PROJECT_LOCK_TEXT["settings"], "route": None}
    assert _js("app.projectLocks({}, {kind: 'upscale'})")["route"] == web.PROJECT_LOCK_TEXT["route"]


SG_JOB = ("{id: 'j1', args: ['generate', 'p', '--width', '896', '--height', '576', '--duration', "
          "'5.166666666666667', '--steps', '50', '--seed', '305'], started_at: '2026-10-07T12:30:00Z', "
          "estimate: {seconds: 540, source: 'history', samples: 3}, note: 'project scene p1 #2'}")


@_needs_node
def test_sglang_run_view_has_no_zeros():
    now = "Date.parse('2026-10-07T12:33:10Z')"
    assert _js(f"app.sglangRunView({SG_JOB}, {now})") == {
        "spec": "896×576 · 5,17 с · сид 305 · 50 шагов", "elapsed": "3 мин", "total": "≈9 мин",
        "share": 35, "leftSeconds": 350, "over": None, "waiting": False}
    # past the estimate (acceptance D1: "99 % · осталось 0 с" for ten minutes looked like a hang):
    # no percent, no end time, only how far over the estimate it already is
    late = "Date.parse('2026-10-07T12:45:00Z')"
    over = _js(f"app.sglangRunView({SG_JOB}, {late})")
    assert (over["share"], over["leftSeconds"], over["over"]) == (None, 0, "дольше оценки на 6 мин")
    assert _js(f"app.sglangRunView({SG_JOB}, {now})")["over"] is None
    chained = SG_JOB.replace("'--seed', '305']", "'--seed', '305', '--aspect', 'auto']")
    assert _js(f"app.sglangRunView({chained}, {now})")["spec"] == "896×576 · 5,13 с · сид 305 · 50 шагов"
    no_estimate = SG_JOB.replace("estimate: {seconds: 540, source: 'history', samples: 3}", "estimate: {}")
    assert _js(f"app.sglangRunView({no_estimate}, {now})")["share"] == 0


@_needs_node
def test_project_settings_html():
    proj = "{id: 'p1', i2v_prefix: 'Go.', seed: 305}"
    lock = web.PROJECT_LOCK_TEXT["settings"]
    assert _js(f"app.projectSettingsHtml({proj}, 'sglang', null, 'seed')") == (
        '<div class="project-settings" data-id="p1">'
        '<label>Начало сцепленной сцены (i2v_prefix) '
        '<textarea class="i2v-prefix" data-id="p1" rows="2">Go.</textarea></label>'
        '<label>Сид проекта <input class="inp num project-seed" type="number" min="0" data-id="p1" '
        'value="305" placeholder="по умолчанию: 42"></label> '
        '<button type="button" class="ghost" data-act="project-seed-save" data-id="p1">Сохранить сид</button>'
        '<span class="saved-mark">сохранено ✓</span></div>')
    assert _js(f"app.projectSettingsHtml({proj}, 'mlx', null, 'i2v_prefix')") == (
        '<div class="project-settings" data-id="p1">'
        '<label>Начало сцепленной сцены (i2v_prefix) '
        '<textarea class="i2v-prefix" data-id="p1" rows="2">Go.</textarea></label>'
        '<span class="saved-mark">сохранено ✓</span></div>')
    assert _js(f"app.projectSettingsHtml({proj}, 'sglang', {json.dumps(lock)}, null)") == (
        '<div class="project-settings" data-id="p1">'
        '<label>Начало сцепленной сцены (i2v_prefix) '
        '<textarea class="i2v-prefix" data-id="p1" rows="2" disabled>Go.</textarea></label>'
        '<label>Сид проекта <input class="inp num project-seed" type="number" min="0" data-id="p1" '
        'value="305" placeholder="по умолчанию: 42" disabled></label> '
        '<button type="button" class="ghost" data-act="project-seed-save" data-id="p1" disabled>Сохранить сид</button>'
        f'<p class="why lock-note">{lock}</p></div>')


@_needs_node
def test_route_html_with_a_lock():
    proj = "{id: 'p1', route: [{stage: 'upscale', enabled: true}]}"
    lock = web.PROJECT_LOCK_TEXT["route"]
    assert _js(f"app.projectRouteHtml({proj}, 'sglang', {json.dumps(lock)})") == (
        '<label class="route-upscale"><input type="checkbox" class="route-upscale-box" data-id="p1" '
        'checked disabled> Апскейл LTX после всех сцен</label>'
        f'<p class="why lock-note">{lock}</p>')


@_needs_node
def test_banner_names_what_the_card_is_doing_during_a_run():
    gpu = {"ok": True, "dispatcher_error": None, "idle_release_at": None,
           "dispatcher": {"own": {"h3": {"owner": "panel-worker"}}, "foreign": [],
                          "qwen": {"running": False, "unloaded_by_us": False},
                          "gpu": {"memory_used_mb": 41984, "memory_total_mb": 65536}},
           "queue": {"pending": 1, "paused": False, "running": {
               "id": "j1", "kind": "generate", "note": "project scene p1 #2",
               "started_at": "2026-10-07T12:26:00Z", "wait_reason": None}}}
    projects = [{"id": "p1", "title": "Бой"}]
    assert _js(f"app.gpuBanner({json.dumps(gpu)}, Date.parse('2026-10-07T12:30:00Z'), "
               f"{json.dumps(projects)})") == {
        "visible": True, "tone": "own", "qwenUnload": False, "qwenRestore": False,
        "text": "Карту держит панель: H3 считает «Бой», сцена #2 — 4 мин, 41,0 ГБ"}
    gpu["dispatcher"]["own"] = {}
    assert _js(f"app.gpuBanner({json.dumps(gpu)}, Date.parse('2026-10-07T12:30:00Z'), "
               f"{json.dumps(projects)})")["text"] == "H3 поднимается для «Бой», сцена #2"


@_needs_node
def test_banner_run_notes():
    def text(note, projects="[]"):
        gpu = {"ok": True, "dispatcher_error": None, "idle_release_at": None,
               "dispatcher": {"own": {"h3": {"owner": "panel-worker"}}, "foreign": [], "gpu": None,
                              "qwen": {"running": False, "unloaded_by_us": False}},
               "queue": {"pending": 0, "paused": False, "running": {
                   "id": "j1", "note": note, "started_at": "2026-10-07T12:29:00Z", "wait_reason": None}}}
        return _js(f"app.gpuBanner({json.dumps(gpu)}, Date.parse('2026-10-07T12:30:00Z'), {projects})")["text"]
    titles = "[{id: 'p1', title: 'Бой'}]"
    assert text("upscale project p1", titles) == "Карту держит панель: LTX апскейлит «Бой» — 1 мин"
    assert text("assemble project p1", titles) == "Карту держит панель: сборка «Бой» — 1 мин"
    assert text("project track p1", titles) == "Карту держит панель: H3 считает трек «Бой» — 1 мин"
    assert text("project scene p9 #0") == "Карту держит панель: H3 считает «p9», сцена #0 — 1 мин"
    # a note the page does not know is never printed raw
    assert text("some other job") == "Карту держит панель: H3 считает задачу — 1 мин"


@_needs_node
def test_retry_cascade_matches_invalidate_scene_chain(tmp_path):
    flags = [False, False, True, False, False, True]
    scenes = [{"idx": i, "prompt": "x", "duration": 8.0, "status": "done", "job_id": None,
               "clip_path": f"/c{i}.mp4", "keyframe_path": None, "fresh_start": f}
              for i, f in enumerate(flags)]
    for idx in range(len(flags)):
        proj = project_module.create_project(tmp_path / str(idx), "video", "T")
        proj.scenes = [dict(s) for s in scenes]
        proj.save()
        reset = [s["idx"] for s in project_module.load_project(proj.path)
                 .invalidate_scene_chain(idx).scenes if s["status"] == "pending"]
        assert _js(f"app.retryCascade({json.dumps(scenes)}, {idx})") == reset
    assert _js(f"app.retryCascade({json.dumps(scenes)}, 0)") == [0, 1]


@_needs_node
def test_retry_body_sends_only_changes():
    scene = "{prompt: '@a walks', seed: 305, steps: null}"
    assert _js(f"app.retryBody({scene}, {{prompt: '@a walks', seed: '', steps: ''}})") == {}
    assert _js(f"app.retryBody({scene}, {{prompt: '@a jumps', seed: '7', steps: '30'}})") == {
        "prompt": "@a jumps", "seed": 7, "steps": 30}
    assert _js(f"app.retryBody({scene}, {{prompt: '@a walks', seed: '305', steps: ''}})") == {}


@_needs_node
def test_scene_card_html():
    ctx = "{projId: 'p1', outdir: '/o', deadMedia: new Set(), engine: 'sglang', projectSeed: null}"
    pending = "{idx: 1, prompt: '@a runs', duration: 5.125, status: 'pending', clip_path: null}"
    assert _js(f"app.sceneCardHtml({pending}, {ctx})") == (
        '<div class="scene-card"><div class="frame"></div><div class="info"><div class="row1">'
        '<span class="m wait" aria-hidden="true"></span><span class="idx">#1</span>'
        '<span class="sdur">5 с</span></div><div class="prompt">@a runs</div>'
        '<div class="scene-params mono">сид 42 · 50 шагов</div><div class="acts"></div></div></div>')
    done = ("{idx: 0, prompt: 'x'.repeat(300), duration: 8, status: 'done', seed: 305, steps: 30, "
            "clip_path: '/o/projects/p1/scenes/a.mp4', ltx_path: '/o/projects/p1/scenes/a-ltx.mp4'}")
    clip = "/media/projects/p1/scenes/a.mp4"
    assert _js(f"app.sceneCardHtml({done}, {ctx})") == (
        f'<div class="scene-card"><div class="frame"><video src="{clip}" preload="metadata" controls '
        f'data-media-url="{clip}"></video></div><div class="info"><div class="row1">'
        '<span class="m done" aria-hidden="true"></span><span class="idx">#0</span>'
        '<span class="sdur">8 с</span></div>'
        f'<details class="scene-prompt"><summary>{"x" * 260}…</summary>{"x" * 300}</details>'
        '<div class="scene-params mono">сид 305 · 30 шагов</div><div class="acts">'
        '<a class="clip" href="/media/projects/p1/scenes/a-ltx.mp4" target="_blank" rel="noopener">LTX</a>'
        '<button type="button" data-act="retry-scene" data-id="p1" data-idx="0">Пересчитать сцену</button>'
        '</div></div></div>')
    failed = "{idx: 2, prompt: 'p', duration: 3, status: 'failed', error: 'boom', clip_path: null}"
    mlx = ctx.replace("'sglang'", "'mlx'")
    assert _js(f"app.sceneCardHtml({failed}, {mlx})") == (
        '<div class="scene-card"><div class="frame"></div><div class="info"><div class="row1">'
        '<span class="m fail" aria-hidden="true"></span><span class="idx">#2</span>'
        '<span class="sdur">3 с</span></div><div class="prompt">p</div>'
        '<div class="scene-error why">boom</div><div class="acts">'
        '<button type="button" data-act="retry-scene" data-id="p1" data-idx="2">Пересчитать сцену</button>'
        '</div></div></div>')


@_needs_node
def test_retry_panel_html():
    scene = "{idx: 0, prompt: '@a <b>', steps: null}"
    tail = ('<p class="hint">Пересчитает сцены #0, #1</p>'
            '<button type="button" class="inverse" data-act="retry-scene-go" data-id="p1" data-idx="0">Пересчитать</button>'
            '<button type="button" class="ghost" data-act="retry-scene-cancel" data-idx="0">Отмена</button></div>')
    assert _js(f"app.retryPanelHtml({scene}, {{id: 'p1', engine: 'sglang', effectiveSeed: 305, cascade: [0, 1]}})") == (
        '<div class="retry-panel" data-idx="0"><textarea class="inp retry-prompt" rows="4">@a &lt;b&gt;</textarea>'
        '<div class="scene-edit-row"><label>Сид <input class="inp num retry-seed" type="number" min="0" '
        'value="" placeholder="сейчас: 305"></label>'
        '<button type="button" class="ghost" data-act="retry-seed-random" data-idx="0">новый случайный</button>'
        '<label>Шаги <input class="inp num retry-steps" type="number" min="2" max="100" value="" '
        'placeholder="сейчас: 50"></label></div>' + tail)
    assert _js(f"app.retryPanelHtml({scene}, {{id: 'p1', engine: 'mlx', effectiveSeed: 42, cascade: [0, 1]}})") == (
        '<div class="retry-panel" data-idx="0"><textarea class="inp retry-prompt" rows="4">@a &lt;b&gt;</textarea>'
        + tail)


RUN_EXPECTED = {
    "retry_with_new_seed": {"opened": True, "confirms": [],
                            "posts": [["/api/projects/p1/scenes/0/retry", {"seed": 7}]]},
    "locked_while_a_scene_runs": {"settings": True, "refs": True, "route": True},
    "locked_route_during_upscale": {"route": True},
    "settings_saved_mark": {"puts": [["/api/projects/p1/settings", {"i2v_prefix": "Go on."}]],
                            "mark": True},
    "seed_saved": {"puts": [["/api/projects/p1/settings", {"seed": 305}]], "mark": True},
    "seed_cleared": {"puts": [["/api/projects/p1/settings", {"seed": None}]]},
    # a version picked on an unticked card is remembered (no PUT, no reset), and rides with the tick
    "version_chosen_before_the_tick": {"putsAfterPick": [], "selectedAfterRedraw": True,
        "putsAfterTick": [["/api/projects/p1/references", {"references": [{"tag": "@a", "version": 1}]}]]},
    "retry_panel_keeps_typing": {"prompt": "@a walks far", "seed": "9"},
    # a refusal keeps the panel open with what was typed, and says why next to it
    "retry_refused_keeps_panel": {"open": True, "prompt": "@a walks far", "seed": "9",
        "error": "Проект считается — референсы меняются после конца прогона."},
    # the dead-clip mark of a retried scene comes off once the server took the retry
    "retry_clears_dead_clip_marks": {"deadBefore": True, "videoAfter": True, "deadAfter": False},
    "saved_mark_fades_on_edit": {"markRemoved": True, "after": True},
}


@_needs_node
@pytest.mark.parametrize("scenario", sorted(RUN_EXPECTED))
def test_run_wiring(scenario):
    assert _gaps(scenario) == RUN_EXPECTED[scenario]


@_needs_node
def test_retry_panel_names_a_single_scene():
    assert '<p class="hint">Пересчитает сцену #5</p>' in _js(
        "app.retryPanelHtml({idx: 5, prompt: 'p', steps: null}, {id: 'p1', engine: 'mlx', "
        "effectiveSeed: 42, cascade: [5]})")


@_needs_node
def test_pipeline_notes_include_the_upscale():
    assert _js("['upscale project 20261007-ab', 'project scene p #1', 'assemble project p', "
               "'upscale projects x'].map((n) => app.isProjectPipelineNote(n))") == [True, True, False, False]


UPSCALED = ("{id: 'p1', stages: {upscale: 'done'}, route: [{stage: 'upscale', enabled: true}], "
            "scenes: [{idx: 0, ltx_path: '/a'}, {idx: 1, ltx_path: '/b'}, {idx: 2}], "
            "upscale_report: {status: 'done', strength: 0.6, motion: 0.1, attempted: [0, 1, 2], error: null}}")


@_needs_node
def test_upscale_line_shows_strength_and_parts():
    assert _js(f"app.projectUpscaleHtml({UPSCALED}, 'sglang')") == (
        '<div class="upscale-status" data-id="p1">Апскейл LTX: готов · сила 0,6 · 2 из 3 частей</div>')


@_needs_node
def test_stale_upscale_report_is_not_shown():
    running = UPSCALED.replace("stages: {upscale: 'done'}", "stages: {upscale: 'running'}")
    assert _js(f"app.projectUpscaleHtml({running}, 'sglang')") == (
        '<div class="upscale-status" data-id="p1">Апскейл LTX: идёт</div>')


@_needs_node
def test_failed_upscale_shows_its_error():
    failed = ("{id: 'p1', stages: {upscale: 'failed'}, route: [{stage: 'upscale', enabled: true}], "
              "scenes: [], upscale_report: {status: 'failed', strength: null, motion: null, "
              "attempted: [], error: 'ComfyUI 500 <x>'}}")
    assert _js(f"app.projectUpscaleHtml({failed}, 'sglang')") == (
        '<div class="upscale-status" data-id="p1">Апскейл LTX: упал '
        '<button type="button" class="upscale-retry" data-id="p1">Повторить апскейл</button>'
        '<p class="why upscale-error">ComfyUI 500 &lt;x&gt;</p></div>')


@_needs_node
def test_download_name():
    assert _js("app.downloadName({id: 'p1', title: 'Бой на арене', created_at: '2026-10-05T10:00:00', "
               "assembly: {v: 1791374400}})") == "boy-na-arene-20261007-final.mp4"
    assert _js("app.downloadName({id: 'p1', title: 'Бой на арене', created_at: '2026-10-05T10:00:00', "
               "assembly: {}})") == "boy-na-arene-20261005-final.mp4"
    assert _js("app.downloadName({id: 'p1', title: '!!!', created_at: '2026-10-05T10:00:00', "
               "assembly: {}})") == "p1-20261005-final.mp4"


@_needs_node
def test_assembly_block_offers_download_and_draft_only_when_due():
    done = ("{id: 'p1', title: 'Бой на арене', created_at: '2026-10-05T10:00:00', "
            "stages: {assembly: 'done'}, assembly: {final_path: '/o/projects/p1/assembly/final.mp4', "
            "v: 1791374400}, scenes: [{idx: 0, status: 'done'}]}")
    url = "/media/projects/p1/assembly/final.mp4?v=1791374400"
    assert _js(f"app.projectAssemblyHtml({done}, '/o')") == (
        '<div class="proj-stage"><div class="proj-stage-head"><span class="t">Сборка</span>'
        '<span class="proj-stage-status">готово</span><div class="spacer"></div></div>'
        '<div class="proj-stage-body"><p class="proj-final">'
        f'<a class="clip" href="{url}" target="_blank" rel="noopener">final.mp4</a> '
        f'<a class="ghost" href="{url}" download="boy-na-arene-20261007-final.mp4">Скачать</a></p>'
        '<button type="button" class="ghost draft-assembly" data-id="p1">Черновая сборка</button>'
        '</div></div>')
    pending = ("{id: 'p1', title: 'T', created_at: '2026-10-05T10:00:00', stages: {assembly: 'draft'}, "
               "assembly: {}, scenes: [{idx: 0, status: 'done'}, {idx: 1, status: 'running'}]}")
    assert _js(f"app.projectAssemblyHtml({pending}, '/o')") == ""
    ready = pending.replace("{idx: 1, status: 'running'}", "{idx: 1, status: 'done'}")
    assert _js(f"app.projectAssemblyHtml({ready}, '/o')") == (
        '<div class="proj-stage"><div class="proj-stage-head"><span class="t">Сборка</span>'
        '<span class="proj-stage-status">не начата</span><div class="spacer"></div></div>'
        '<div class="proj-stage-body">'
        '<button type="button" class="ghost draft-assembly" data-id="p1">Черновая сборка</button>'
        '</div></div>')
    running = ready.replace("stages: {assembly: 'draft'}", "stages: {assembly: 'running'}")
    assert "draft-assembly" not in _js(f"app.projectAssemblyHtml({running}, '/o')")


@_needs_node
def test_a_project_assembly_tile_has_no_chat_or_copy():
    job = ("{id: 'j9', kind: 'assemble', exit_code: 0, note: 'assemble project p1', "
           "output_stem: '/o/projects/p1/assembly/job-final', estimate: {}, "
           "started_at: '2026-10-07T12:00:00Z', finished_at: '2026-10-07T12:01:00Z'}")
    html = _js(f"app.finishedRowHtml({job}, '/o', [], new Set(), 'Бой')")
    assert re.search(r'<div class="acts">.*?</div>', html).group(0) == (
        '<div class="acts"><button data-act="reveal" data-id="j9">Показать в Finder</button>'
        '<button data-act="delrun" data-id="j9">Удалить</button></div>')


@_needs_node
def test_retry_panel_shows_its_error():
    scene = "{idx: 0, prompt: 'p', steps: null}"
    html = _js(f"app.retryPanelHtml({scene}, {{id: 'p1', engine: 'mlx', effectiveSeed: 42, cascade: [0], "
               "error: 'Проект считается'})")
    assert html.endswith('<p class="hint">Пересчитает сцену #0</p>'
                         '<p class="why retry-error">Проект считается</p>'
                         '<button type="button" class="inverse" data-act="retry-scene-go" data-id="p1" '
                         'data-idx="0">Пересчитать</button>'
                         '<button type="button" class="ghost" data-act="retry-scene-cancel" data-idx="0">Отмена</button></div>')


def test_retry_number_fields_show_the_whole_placeholder():
    css = re.sub(r"\s+", " ", _page_text("style.css"))
    assert ".scene-edit-row input.retry-seed, .scene-edit-row input.retry-steps { width: calc(20ch + 24px); }" in css


@_needs_node
def test_plate_text_for_each_gpu_sharing_answer():
    assert _js("[app.llmPlateText('down', {external: false, sharesGpu: true}), "
               "app.llmPlateText('busy', {external: false, sharesGpu: true, runningSeconds: 300}), "
               "app.llmPlateText('down', {external: true, sharesGpu: true}), "
               "app.llmPlateText('down', {external: true, sharesGpu: false}), "
               "app.llmPlateText('down', {external: true, sharesGpu: null}), "
               "app.llmPlateText('down', {external: true})]") == [
        "модель не поднята — поднимется при первом сообщении",
        "идёт прогон — модель поднимется после него (~5 мин)",
        "делит видеокарту с H3: пока модель поднята, рендер ждёт",
        "внешний провайдер — память этой машины не занимает",
        "где считает провайдер, не указано (shares_gpu в providers.json)",
        "где считает провайдер, не указано (shares_gpu в providers.json)"]


@_needs_node
def test_chat_apply_body_and_confirm():
    assert _js("app.chatApplyBody({kind: 'video', scenes: [{prompt: '@a', duration: 7, extra: 1}]})") \
        == {"scenes": [{"prompt": "@a", "duration": 7}]}
    assert _js("[[0, 3], [1, 3], [2, 3], [5, 1]].map(([c, i]) => app.chatApplyConfirm(c, i))") == [
        None, "Заменить 1 сцену проекта на 3 из диалога?", "Заменить 2 сцены проекта на 3 из диалога?",
        "Заменить 5 сцен проекта на 1 из диалога?"]


CHAT_EXPECTED = {
    "project_chat_opens": {"posts": [["/api/chat", {
        "source": {"kind": "project", "id": "p1"}, "prompt": "", "mode": "", "image": "",
        "end_image": "", "duration": 10}]]},
    "chat_apply_to_project": {
        "label": "Применить к проекту",
        "confirms": ["Заменить 2 сцены проекта на 3 из диалога?"],
        "puts": [["/api/projects/p1/scenes", {"scenes": [
            {"prompt": "@a jumps", "duration": 6}, {"prompt": "@a lands", "duration": 4},
            {"prompt": "@a bows", "duration": 3}]}]],
        "posts": []},
    "chat_apply_declined": {"puts": []},
    "chat_button_in_the_script_stage": {"button": '<button type="button" class="ghost" '
        'data-act="project-chat" data-id="p1">Чат по сценарию</button>'},
    "chat_apply_refused": {"error": "<b>Запрос не прошёл</b><pre>плохая сцена</pre>", "panelHidden": False},
    "chat_plain_keeps_its_label": {"label": "Сделать проектом"},
}


@_needs_node
@pytest.mark.parametrize("scenario", sorted(CHAT_EXPECTED))
def test_chat_wiring(scenario):
    assert _gaps(scenario) == CHAT_EXPECTED[scenario]


DRAFT_JOB = ("{id: 'j9', kind: 'assemble', exit_code: 0, note: 'draft assemble project p1', "
             "output_stem: '/o/projects/p1/assembly/job-draft', estimate: {}, "
             "started_at: '2026-10-07T12:00:00Z', finished_at: '2026-10-07T12:01:00Z'}")


@_needs_node
def test_the_draft_assembly_tile_points_at_its_own_file():
    final = DRAFT_JOB.replace("draft assemble project p1", "assemble project p1").replace("job-draft", "job-final")
    assert _js(f"[app.assembleFinalUrl({DRAFT_JOB}, '/o'), app.assembleFinalUrl({final}, '/o')]") == [
        "/media/projects%2Fp1%2Fassembly/draft.mp4?v=2026-10-07T12%3A01%3A00Z",
        "/media/projects%2Fp1%2Fassembly/final.mp4?v=2026-10-07T12%3A01%3A00Z"]
    html = _js(f"app.finishedRowHtml({DRAFT_JOB}, '/o', [], new Set(), undefined)")
    assert re.search(r'<div class="link">.*?</div>', html).group(0) == (
        '<div class="link"><a class="clip" href="/media/projects%2Fp1%2Fassembly/draft.mp4'
        '?v=2026-10-07T12%3A01%3A00Z">job-draft.mp4</a></div>')


@_needs_node
def test_an_assembly_outside_the_outdir_gets_no_link():
    outside = DRAFT_JOB.replace("/o/projects", "/elsewhere/projects")
    assert _js(f"app.assembleFinalUrl({outside}, '/o')") is None
    assert 'href="/media' not in _js(f"app.finishedRowHtml({outside}, '/o', [], new Set(), undefined)")


@_needs_node
def test_h3_prompt_html():
    answer = ("{idx: 1, prompt: 'Continue.\\n\\n<Subject 1> runs <b>', pictures: [{label: '<Picture 1>', "
              "path: '/o/library/arena/v1/01-o.png'}], audios: [], keyframe: {kind: 'previous_scene', "
              "path: null}, duration: 5.125, seed: 42, steps: 30}")
    assert _js(f"app.h3PromptHtml({answer}, '/o')") == (
        '<div class="h3-prompt" data-idx="1">'
        '<p class="hint">доставляется 5,13 с · сид 42 · 30 шагов · первый кадр: последний кадр сцены #0</p>'
        '<pre>Continue.\n\n&lt;Subject 1&gt; runs &lt;b&gt;</pre>'
        '<div class="h3-pictures"><figure><img src="/media/library/arena/v1/01-o.png" alt="">'
        '<figcaption>&lt;Picture 1&gt;</figcaption></figure></div></div>')
    start = ("{idx: 0, prompt: 'P', pictures: [], audios: [], keyframe: {kind: 'start_image', "
             "path: '/o/library/arena/v1/01-o.png'}, duration: 8, seed: 305, steps: 50}")
    assert _js(f"app.h3PromptHtml({start}, '/o')") == (
        '<div class="h3-prompt" data-idx="0">'
        '<p class="hint">доставляется 8 с · сид 305 · 50 шагов · первый кадр: 01-o.png</p><pre>P</pre></div>')
    none = start.replace("{kind: 'start_image', path: '/o/library/arena/v1/01-o.png'}",
                         "{kind: null, path: null}")
    assert _js(f"app.h3PromptHtml({none}, '/o')") == (
        '<div class="h3-prompt" data-idx="0">'
        '<p class="hint">доставляется 8 с · сид 305 · 50 шагов · без первого кадра</p><pre>P</pre></div>')


@_needs_node
def test_h3_prompt_pictures_outside_the_outdir_get_no_image():
    answer = ("{idx: 0, prompt: 'P', pictures: [{label: '<Picture 1>', path: '/etc/x.png'}, "
              "{label: '<Picture 2>', path: '/o/../etc/y.png'}], audios: [], keyframe: {kind: null, path: null}, "
              "duration: 8, seed: 1, steps: 50}")
    assert _js(f"app.h3PromptHtml({answer}, '/o')") == (
        '<div class="h3-prompt" data-idx="0">'
        '<p class="hint">доставляется 8 с · сид 1 · 50 шагов · без первого кадра</p><pre>P</pre>'
        '<div class="h3-pictures"><figure><figcaption>&lt;Picture 1&gt;</figcaption></figure>'
        '<figure><figcaption>&lt;Picture 2&gt;</figcaption></figure></div></div>')


H3_EXPECTED = {
    "h3_prompt_saves_first": {"order": ["PUT /api/projects/p1/scenes",
                                        "GET /api/projects/p1/scenes/0/h3-prompt"], "shown": True},
    "h3_prompt_clean_skips_save": {"order": ["GET /api/projects/p1/scenes/0/h3-prompt"], "shown": True},
    "h3_prompt_refused": {"shown": False, "errorHidden": False,
                          "error": "<b>Запрос не прошёл</b><pre>scene 0: 190 frames is off sglang's grid</pre>"},
    "h3_prompt_goes_out_on_edit": {"removed": 1, "shownAfterRedraw": False},
    # a field moved while the GET was in flight: its answer is stale and is never drawn
    "h3_prompt_late_answer_dropped": {"shown": False},
    "h3_prompt_dropped_by_reference_change": {"shown": False},
    "h3_prompt_answer_kept_when_nothing_moved": {"shown": True},
    "h3_prompt_refused_off_grid": {"error": "<b>Длительность сцены не на сетке H3</b><pre>Сохраните "
                                            "сценарий заново: длительность подгонится под сетку.</pre>"},
}


@_needs_node
@pytest.mark.parametrize("scenario", sorted(H3_EXPECTED))
def test_h3_prompt_wiring(scenario):
    assert _gaps(scenario) == H3_EXPECTED[scenario]


@_needs_node
def test_error_text_for_the_h3_prompt_refusals():
    assert _js("[app.errorText({error: {code: 'duration_off_grid', message: \"scene 0: 190 frames is off sglang's 17n+5 grid\"}}), "
               "app.errorText({error: {code: 'project_scene_not_found', message: 'scene 9'}})]") == [
        {"title": "Длительность сцены не на сетке H3",
         "pre": "Сохраните сценарий заново: длительность подгонится под сетку."},
        {"title": "Такой сцены нет в сохранённом проекте",
         "pre": "Сохраните сценарий и повторите."}]


@_needs_node
def test_h3_prompt_lists_its_audio():
    answer = ("{idx: 0, prompt: 'P', pictures: [], audios: ['/o/library/v/v1/01-v.mp3', '/o/t.wav'], "
              "keyframe: {kind: null, path: null}, duration: 8, seed: 1, steps: 50}")
    assert _js(f"app.h3PromptHtml({answer}, '/o')") == (
        '<div class="h3-prompt" data-idx="0">'
        '<p class="hint">доставляется 8 с · сид 1 · 50 шагов · без первого кадра</p><pre>P</pre>'
        '<p class="hint">аудио: 01-v.mp3, t.wav</p></div>')


def _z_index(css: str, selector: str):
    """The `z-index` a rule whose selector list is exactly `selector` gives (last one wins)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    found = None
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in [s.strip() for s in selectors.split(",")]:
            m = re.search(r"z-index:\s*(\d+)", body)
            if m:
                found = int(m.group(1))
    return found


def test_the_chat_modal_stacks_above_the_project_modal():
    """«Чат по сценарию» opens while the project modal is open. Both are `.modal-back` (fixed, same
    z-index) and the project modal comes later in the page, so the chat used to open *behind* it --
    drawn, but not clickable. The page has to say which one is on top."""
    css = _page_text("style.css")
    page = _page_text("index.html")
    assert page.index('id="project-modal"') > page.index('id="chat-modal"')    # why equal z-index loses
    base = _z_index(css, ".modal-back")
    chat = _z_index(css, "#chat-modal")
    assert base == 50
    assert chat is not None and chat > base


@_needs_node
def test_a_remembered_version_is_drawn_selected_on_an_unticked_card():
    cards = ("[{tag: '@a', kind: 'person', version: 2, latest_version: 2, "
             "versions: [{version: 1}, {version: 2}]}]")
    assert _js(f"app.projectReferencesHtml({{id: 'p1'}}, {cards}, [], null, {{'@a': 1}})") == (
        '<div class="project-refs" data-id="p1"><h4>Референсы проекта</h4>'
        '<label><input type="checkbox" class="ref-pin" data-tag="@a"> @a '
        '<span class="muted">person</span></label> '
        '<select class="inp ref-version" data-tag="@a"><option value="1" selected>v1</option>'
        '<option value="2">v2</option></select></div>')
    # a pinned card shows its pinned version whatever was remembered; a stale number is ignored
    assert 'value="2" selected' in _js(f"app.projectReferencesHtml({{id: 'p1'}}, {cards}, [{{tag: '@a', version: 2}}], null, {{'@a': 1}})")
    assert 'value="2" selected' in _js(f"app.projectReferencesHtml({{id: 'p1'}}, {cards}, [], null, {{'@a': 7}})")


@_needs_node
def test_upscale_line_without_a_strength_has_no_null():
    no_strength = UPSCALED.replace("strength: 0.6", "strength: null")
    assert _js(f"app.projectUpscaleHtml({no_strength}, 'sglang')") == (
        '<div class="upscale-status" data-id="p1">Апскейл LTX: готов · 2 из 3 частей</div>')
    missing = UPSCALED.replace("strength: 0.6, ", "")
    assert _js(f"app.projectUpscaleHtml({missing}, 'sglang')") == (
        '<div class="upscale-status" data-id="p1">Апскейл LTX: готов · 2 из 3 частей</div>')


@_needs_node
def test_error_text_names_the_wave_refusals_in_russian():
    codes = ["project_running", "library_card_in_use", "unknown_tag", "ref2va_needs_reference",
             "project_stage_not_ready", "start_image_invalid"]
    got = _js("%s.map((code) => app.errorText({error: {code, message: 'm ' + code}}))" % json.dumps(codes))
    assert got == [
        {"title": "Проект считается — правка закрыта", "pre": "m project_running"},
        {"title": "Карточка подключена к проектам", "pre": "m library_card_in_use"},
        {"title": "Тег не подключён к проекту", "pre": "m unknown_tag"},
        {"title": "Сцене нужен референс", "pre": "m ref2va_needs_reference"},
        {"title": "Этап ещё не готов", "pre": "m project_stage_not_ready"},
        {"title": "Стартовый кадр не подходит", "pre": "m start_image_invalid"}]


def test_panel_head_wraps_so_the_new_project_button_stays_on_the_screen():
    """Acceptance D2: at 390 px the head of the projects zone was 494 px wide in a 368 px panel and
    "+ Новый проект" sat at x 374..505 behind `overflow: hidden`."""
    css = (Path(__file__).resolve().parent.parent / "h3_48gb" / "webui" / "style.css").read_text(
        encoding="utf-8")
    rule = re.search(r"(?m)^\.panel-head\s*\{([^}]*)\}", css).group(1)
    declarations = {k.strip(): v.strip() for k, v in
                    (d.split(":", 1) for d in rule.split(";") if ":" in d)}
    assert declarations["display"] == "flex"
    assert declarations["flex-wrap"] == "wrap"


@_needs_node
def test_finished_tiles_are_not_redrawn_while_nothing_changed():
    """Acceptance D3: both final.mp4 were requested again on every ~40 s poll (6 range requests per
    poll, BrokenPipe in the container log) because `#finished` was rebuilt from scratch."""
    assert _gaps("finished_tiles_stable") == {
        "afterFirst": 1, "writesAfterTwoMorePolls": 1, "writesAfterNewJob": 2, "tiles": 2}
