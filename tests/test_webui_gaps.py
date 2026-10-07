"""Wave 1.5 UI (spec docs/superpowers/specs/2026-10-07-panel-ui-gaps-design.md): pure functions of
app.js through node with exact expected values, DOM wiring through tests/_ui_gaps_check.mjs."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

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
    assert _gaps("new_video_cancel") == {"posts": []}


@_needs_node
def test_new_chat_opens_an_empty_session_on_sglang():
    assert _gaps("new_chat") == {"posts": [["/api/chat", {
        "source": {"kind": "new"}, "prompt": "", "mode": "", "image": "", "end_image": "",
        "duration": 10}]]}
