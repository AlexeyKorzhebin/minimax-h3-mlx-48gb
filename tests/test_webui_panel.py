"""The wave-1 UI minimum (spec §3.3.13-14, §3.5, §10): pure functions of app.js called through
node with exact expected values, plus source checks for the DOM wiring that has no pure seam."""
import json
import shutil
import time
from datetime import datetime
from pathlib import Path

import pytest

from h3_48gb import queue as q
from h3_48gb import worker
from test_queue import _external_lock
from test_web import _call, _needs_node, _node_eval, _page_text, _serve
from _fake_dispatcher import FakeDispatcher

NOW = "Date.parse('2026-10-07T12:30:00Z')"


def _banner(gpu: dict):
    return _node_eval(f"console.log(JSON.stringify(app.gpuBanner({json.dumps(gpu)}, {NOW})));")


def _dispatcher(own=None, foreign=(), qwen=None, used_mb=40960):
    return {"ok": True, "own": own or {}, "foreign": list(foreign),
            "qwen": qwen or {"running": False, "unloaded_by_us": False},
            "lock": {"held_by_us": bool(own), "path": "/x"},
            "gpu": {"temperature_c": 50, "memory_used_mb": used_mb, "memory_total_mb": 65536},
            "server_outputs_bytes": 0}


@_needs_node
def test_banner_while_waiting_names_the_reason_and_the_holder():
    gpu = {"ok": True, "dispatcher_error": None, "idle_release_at": None,
           "dispatcher": _dispatcher(foreign=[{"pid": 777, "name": "comfy", "memory_mb": 30720,
                                               "first_seen": 1791374400}]),   # 12:00Z
           "queue": {"pending": 2, "paused": False, "running": {
               "id": "j1", "kind": "generate", "note": "project scene P #3",
               "started_at": "2026-10-07T12:00:00Z",
               "wait_reason": "ждём GPU: GPU занята: comfy (pid 777, 30720 МБ)"}}}
    assert _banner(gpu) == {
        "visible": True, "tone": "wait", "qwenUnload": False, "qwenRestore": False,
        "text": "Очередь стоит 30 мин: GPU занята: comfy (pid 777, 30720 МБ) — карту держит "
                "comfy (pid 777, 30,0 ГБ, уже 30 мин)"}


@_needs_node
def test_banner_offers_qwen_restore_only_with_an_empty_queue():
    unloaded = _dispatcher(qwen={"running": False, "unloaded_by_us": True})
    idle = {"ok": True, "dispatcher_error": None, "idle_release_at": None, "dispatcher": unloaded,
            "queue": {"pending": 0, "paused": True, "running": None}}
    assert _banner(idle) == {"visible": True, "tone": "", "qwenUnload": False, "qwenRestore": True,
                             "text": "Qwen выгружен панелью — его можно вернуть"}
    busy = {**idle, "queue": {"pending": 1, "paused": False, "running": None}}
    assert _banner(busy)["qwenRestore"] is False


@_needs_node
def test_banner_offers_qwen_unload_when_qwen_is_the_reason():
    gpu = {"ok": True, "dispatcher_error": None, "idle_release_at": None,
           "dispatcher": _dispatcher(qwen={"running": True, "unloaded_by_us": False}),
           "queue": {"pending": 0, "paused": False, "running": {
               "id": "j1", "kind": "generate", "note": "", "started_at": "2026-10-07T12:29:00Z",
               "wait_reason": "ждём GPU: Qwen держит карту — выгрузите Qwen в панели"}}}
    banner = _banner(gpu)
    assert (banner["qwenUnload"], banner["text"]) == (
        True, "Очередь стоит 1 мин: Qwen держит карту — выгрузите Qwen в панели")


@_needs_node
def test_banner_when_the_panel_holds_the_card_and_the_queue_is_empty():
    gpu = {"ok": True, "dispatcher_error": None, "idle_release_at": "2026-10-07T15:42:00+03:00",
           "dispatcher": _dispatcher(own={"h3": {"pid": 1, "variant": "ref2va", "started_at": 1,
                                                 "log": "/l", "ready": True}}),
           "queue": {"pending": 0, "paused": True, "running": None}}
    assert _banner(gpu) == {
        "visible": True, "tone": "own", "qwenUnload": False, "qwenRestore": False,
        "text": "Карту держит панель: H3, 40,0 ГБ — освободится через 12 мин или кнопкой"}


@_needs_node
def test_banner_when_the_dispatcher_is_down_and_when_nothing_to_say():
    assert _banner({"ok": True, "dispatcher": None, "dispatcher_error": "GET /status: refused",
                    "idle_release_at": None,
                    "queue": {"pending": 0, "paused": True, "running": None}}) == {
        "visible": True, "tone": "bad", "qwenUnload": False, "qwenRestore": False,
        "text": "Диспетчер GPU не отвечает: GET /status: refused"}
    assert _banner({"ok": True, "dispatcher": _dispatcher(), "dispatcher_error": None,
                    "idle_release_at": None,
                    "queue": {"pending": 0, "paused": True, "running": None}})["visible"] is False
    assert _node_eval(f"console.log(JSON.stringify(app.gpuBanner(null, {NOW})));")["visible"] is False


def _events(prev, nxt, now_ms):
    return _node_eval("console.log(JSON.stringify(app.notificationEvents("
                      f"{json.dumps(prev)}, {json.dumps(nxt)}, {now_ms})));")


EMPTY = {"failedIds": [], "readyProjectIds": [], "awaitingProjectIds": [], "waitReason": None,
         "waitingSinceMs": None, "lastWaitNotifyMs": None}


@_needs_node
def test_notifications_wait_over_ten_minutes_then_hourly():
    waiting = {**EMPTY, "waitReason": "GPU занята", "waitingSinceMs": 0}
    assert _events(EMPTY, waiting, 9 * 60_000) == {"events": [], "lastWaitNotifyMs": None}
    first = _events(EMPTY, waiting, 10 * 60_000)
    assert first == {"events": [{"kind": "wait", "title": "Очередь стоит 10 мин",
                                 "body": "GPU занята"}], "lastWaitNotifyMs": 600000}
    again = {**waiting, "lastWaitNotifyMs": 600000}
    assert _events(again, again, 600000 + 59 * 60_000)["events"] == []
    assert _events(again, again, 600000 + 60 * 60_000)["events"][0]["kind"] == "wait"


@_needs_node
def test_notifications_failed_ready_and_decision_fire_once():
    nxt = {**EMPTY, "failedIds": ["j9"], "readyProjectIds": ["p1"], "awaitingProjectIds": ["p2"]}
    assert _events(EMPTY, nxt, 0)["events"] == [
        {"kind": "failed", "title": "Сцена упала", "body": "задача j9"},
        {"kind": "ready", "title": "Проект готов", "body": "p1"},
        {"kind": "decision", "title": "Нужно решение", "body": "p2"}]
    assert _events(nxt, nxt, 1)["events"] == []


@_needs_node
def test_a_scene_that_failed_without_a_job_notifies_once_and_shows_its_reason():
    """Final review C2: a chained scene the worker could not submit has no failed job."""
    projects = [{"id": "p1", "title": "Бой", "scene_errors": [{"idx": 4, "error": "нет кадра"}]},
                {"id": "p2", "title": "Пусто"}]
    keys = _node_eval(f"console.log(JSON.stringify(app.sceneFailureKeys({json.dumps(projects)})));")
    assert keys == ["Бой, сцена 4: нет кадра"]
    nxt = {**EMPTY, "failedSceneKeys": keys}
    assert _events(EMPTY, nxt, 0)["events"] == [
        {"kind": "failed", "title": "Сцена не поставлена", "body": "Бой, сцена 4: нет кадра"}]
    assert _events(nxt, nxt, 1)["events"] == []
    assert _node_eval("console.log(JSON.stringify(app.sceneErrorHtml("
                      "{status: 'failed', error: 'a<b'})));") == \
        '<div class="scene-error why">a&lt;b</div>'
    assert _node_eval("console.log(JSON.stringify(app.sceneErrorHtml("
                      "{status: 'pending', error: 'a'})));") == ""


@_needs_node
def test_a_stage_the_worker_could_not_submit_notifies_once():
    """Final re-review: an upscale / assembly that failed to be submitted has no failed job."""
    projects = [{"id": "p1", "title": "Бой", "stage_errors": [
        {"stage": "upscale", "error": "апскейл не поставлен: OSError: диск"}]}, {"id": "p2"}]
    keys = _node_eval(f"console.log(JSON.stringify(app.stageFailureKeys({json.dumps(projects)})));")
    assert keys == ["Бой: апскейл не поставлен: OSError: диск"]
    nxt = {**EMPTY, "failedStageKeys": keys}
    assert _events(EMPTY, nxt, 0)["events"] == [
        {"kind": "failed", "title": "Этап не запущен", "body": "Бой: апскейл не поставлен: OSError: диск"}]
    assert _events(nxt, nxt, 1)["events"] == []


@_needs_node
def test_the_first_snapshot_after_opening_the_tab_notifies_nothing():
    nxt = {**EMPTY, "failedIds": ["j9"], "readyProjectIds": ["p1"],
           "waitReason": "GPU занята", "waitingSinceMs": 0}
    assert _node_eval("console.log(JSON.stringify(app.notificationEvents("
                      f"null, {json.dumps(nxt)}, {60 * 60_000})));") == \
        {"events": [], "lastWaitNotifyMs": 3600000}


@_needs_node
def test_scene_tag_issues_and_suggestions():
    issues = _node_eval("console.log(JSON.stringify(app.sceneTagIssues("
                        "'@alice on @beach with @Bob and mail@x.com', ['@alice', '@beach'])));")
    assert issues == [{"tag": "@Bob", "problem": "invalid"}]
    issues = _node_eval("console.log(JSON.stringify(app.sceneTagIssues("
                        "'@alice meets @carol', ['@alice'])));")
    assert issues == [{"tag": "@carol", "problem": "unknown"}]
    assert _node_eval("console.log(JSON.stringify(app.sceneTagIssues('a cat', [], {needsTag: true})));") \
        == [{"tag": None, "problem": "missing"}]
    assert _node_eval("console.log(JSON.stringify(app.sceneTagIssues('a cat', [])));") == []
    cards = [{"tag": "@alice", "kind": "person"}, {"tag": "@alex", "kind": "person"},
             {"tag": "@beach", "kind": "environment"}]
    picks = _node_eval("console.log(JSON.stringify(app.tagSuggestions("
                       f"'walks with @al', 14, {json.dumps(cards)}).map(c => c.tag)));")
    assert picks == ["@alice", "@alex"]
    assert _node_eval("console.log(JSON.stringify(app.tagSuggestions("
                      f"'walks with al', 13, {json.dumps(cards)})));") == []


@_needs_node
def test_project_route_html_is_one_checkbox_bound_to_the_project():
    html = _node_eval("console.log(JSON.stringify(app.projectRouteHtml("
                      "{id: 'p1', route: [{stage: 'scenes', enabled: true},"
                      " {stage: 'upscale', enabled: false}]}, 'sglang')));")
    assert html == ('<label class="route-upscale"><input type="checkbox" class="route-upscale-box" '
                    'data-id="p1"> Апскейл LTX после всех сцен</label>')
    on = _node_eval("console.log(JSON.stringify(app.projectRouteHtml("
                    "{id: 'p1', route: [{stage: 'upscale', enabled: true}]}, 'sglang')));")
    assert on == ('<label class="route-upscale"><input type="checkbox" class="route-upscale-box" '
                  'data-id="p1" checked> Апскейл LTX после всех сцен</label>')


@_needs_node
def test_project_tag_warnings_and_settings_html():
    warn = _node_eval("console.log(JSON.stringify(app.projectTagWarningsHtml({kind: 'video', "
                      "references: [{tag: '@alice', version: 1}], scenes: ["
                      "{idx: 0, prompt: '@alice runs'}, {idx: 1, prompt: 'a dog'}, "
                      "{idx: 2, prompt: '@bob waves'}]}, 'sglang')));")
    assert warn == ('<ul class="tag-warnings"><li>Сцена 1: нужен хотя бы один референс (@тег) в сцене</li>'
                    '<li>Сцена 2: незнакомый тег @bob</li></ul>')
    assert _node_eval("console.log(JSON.stringify(app.projectTagWarningsHtml({kind: 'clip', "
                      "references: [], scenes: [{idx: 0, prompt: 'a dog'}]}, 'sglang')));") == ""
    settings = _node_eval("console.log(JSON.stringify(app.projectSettingsHtml("
                          "{id: 'p1', i2v_prefix: 'Go <on>.'})));")
    assert settings == ('<div class="project-settings" data-id="p1"><label>Начало сцепленной сцены '
                        '(i2v_prefix) <textarea class="i2v-prefix" data-id="p1" rows="2">Go &lt;on&gt;.'
                        '</textarea></label> <button type="button" class="draft-assembly" '
                        'data-id="p1">Черновая сборка</button></div>')


@_needs_node
def test_project_references_html_marks_pinned_versions():
    # Task 9: every card also gets a version select built from `versions`
    cards = [{"tag": "@alice", "kind": "person", "version": 3, "latest_version": 3,
              "versions": [{"version": 1}, {"version": 2}, {"version": 3}]},
             {"tag": "@beach", "kind": "environment", "version": 1, "latest_version": 1,
              "versions": [{"version": 1}]}]
    pinned = [{"tag": "@alice", "version": 2}, {"tag": "@beach", "version": 1}]
    html = _node_eval("console.log(JSON.stringify(app.projectReferencesHtml("
                      f"{{id: 'p1'}}, {json.dumps(cards)}, {json.dumps(pinned)})));")
    assert html == (
        '<div class="project-refs" data-id="p1"><h4>Референсы проекта</h4>'
        '<label><input type="checkbox" class="ref-pin" data-tag="@alice" checked> '
        '@alice <span class="muted">person, v2 (есть v3)</span></label> '
        '<select class="inp ref-version" data-tag="@alice"><option value="1">v1</option>'
        '<option value="2" selected>v2</option><option value="3">v3</option></select>'
        '<label><input type="checkbox" class="ref-pin" data-tag="@beach" checked> '
        '@beach <span class="muted">environment, v1</span></label> '
        '<select class="inp ref-version" data-tag="@beach"><option value="1" selected>v1</option></select></div>')
    # a card nobody pinned shows only its kind
    free = _node_eval("console.log(JSON.stringify(app.projectReferencesHtml("
                      f"{{id: 'p1'}}, {json.dumps(cards[1:])}, [])));")
    assert free == ('<div class="project-refs" data-id="p1"><h4>Референсы проекта</h4>'
                    '<label><input type="checkbox" class="ref-pin" data-tag="@beach"> '
                    '@beach <span class="muted">environment</span></label> '
                    '<select class="inp ref-version" data-tag="@beach">'
                    '<option value="1" selected>v1</option></select></div>')


def test_new_routes_are_literal_same_origin_paths():
    script = _page_text("app.js")
    for route in ('"/api/gpu"', '"/api/gpu/release"', '"/api/qwen/unload"', '"/api/qwen/restore"',
                  '"/api/library"'):
        assert route in script, route


def test_reveal_is_hidden_off_darwin_by_css():
    css = _page_text("style.css")
    assert 'body:not([data-platform="darwin"]) [data-act="reveal"] { display: none; }' in css
    assert 'document.body.dataset.platform = state.platform' in _page_text("app.js")


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    monkeypatch.setenv("H3_IDLE_RELEASE_MIN", "15")
    disp = FakeDispatcher(own={"h3": {"pid": 1, "variant": "ref2va", "started_at": 1.0,
                                      "log": "/l", "ready": True}})
    monkeypatch.setenv("H3_DISPATCHER_URL", disp.url)
    root = q.layout(tmp_path / "queue")["root"]
    server = _serve(root, tmp_path)
    yield server, root, tmp_path
    server.httpd.shutdown()
    server.httpd.server_close()
    disp.close()


def test_gpu_state_reports_the_workers_own_idle_deadline(live):
    """Final review I2: the deadline is the worker's countdown (`idle-since`), not one derived
    from queue timestamps -- and there is none without a live worker."""
    server, root, tmp_path = live
    (root / "idle-since").write_text("2026-10-07T10:00:00+03:00", encoding="utf-8")
    with _external_lock(root, "LOCK_EX", "worker.lock"):
        _, alive = _call(server, "GET", "/api/gpu")
    _, dead = _call(server, "GET", "/api/gpu")
    assert [(b["idle_release_at"], b["worker_alive"]) for b in (alive, dead)] == [
        ("2026-10-07T10:15:00+03:00", True), (None, False)]


def test_no_idle_deadline_for_an_engine_someone_else_owns(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    disp = FakeDispatcher(own={"h3": {"pid": 1, "owner": "probes", "ready": True}})
    monkeypatch.setenv("H3_DISPATCHER_URL", disp.url)
    root = q.layout(tmp_path / "queue")["root"]
    (root / "idle-since").write_text("2026-10-07T10:00:00+03:00", encoding="utf-8")
    server = _serve(root, tmp_path)
    try:
        with _external_lock(root, "LOCK_EX", "worker.lock"):
            _, body = _call(server, "GET", "/api/gpu")
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()
        disp.close()
    assert (body["idle_release_at"], body["worker_alive"]) == (None, True)


def test_the_idle_countdown_is_written_for_the_page_and_removed_on_activity(tmp_path):
    from test_gpu_gate import _Clock, _FakeClient, _taken
    root = q.layout(tmp_path / "queue")["root"]
    marker = root / "idle-since"
    client, clock = _FakeClient(), _Clock()
    idle = worker._IdleRelease(15, client, clock=clock, acquired=_taken())
    before = datetime.now().astimezone().replace(microsecond=0)
    idle.tick(root)
    written = datetime.fromisoformat(marker.read_text(encoding="utf-8"))
    assert before <= written <= datetime.now().astimezone()
    assert written.utcoffset() is not None
    q.submit(root, ["generate", "--tag", "a"], "", {"output_stem": str(tmp_path / "h3-a")}, {})
    idle.tick(root)
    assert not marker.exists()
    for job in q.scan(root)[0]:
        q.cancel(root, job.id)
    idle.tick(root)
    assert marker.exists()
    clock.t = 900.0
    idle.tick(root)
    assert (client.released, marker.exists()) == (1, False)


@_needs_node
def test_banner_names_a_foreign_owner_and_a_missing_worker():
    own = {"h3": {"pid": 1, "variant": "ref2va", "owner": "probes", "started_at": 1, "log": "/l",
                  "ready": True}}
    gpu = {"ok": True, "dispatcher_error": None, "idle_release_at": None, "worker_alive": True,
           "dispatcher": _dispatcher(own=own),
           "queue": {"pending": 0, "paused": True, "running": None}}
    assert _banner(gpu)["text"] == "Карту держит панель: H3 (probes), 40,0 ГБ — освободится кнопкой"
    mine = {"h3": {**own["h3"], "owner": "panel-worker"}}
    dead = {**gpu, "worker_alive": False, "dispatcher": _dispatcher(own=mine)}
    assert _banner(dead)["text"] == ("Карту держит панель: H3, 40,0 ГБ — освободится кнопкой "
                                     "(воркер не запущен)")


@_needs_node
def test_references_payload_keeps_pinned_versions_and_pins_new_ones_to_latest():
    # toggling @beach must not silently upgrade @alice from v2 to her latest: a pinned card
    # keeps its version, only a newly ticked one is sent without `version` (server pins latest)
    payload = _node_eval("console.log(JSON.stringify(app.referencesPayload("
                         "['@alice', '@beach'], [{tag: '@alice', version: 2}])));")
    assert payload == [{"tag": "@alice", "version": 2}, {"tag": "@beach"}]


# -- fix round 1 ----------------------------------------------------------------------------------


@_needs_node
def test_banner_when_the_panel_holds_the_card_and_nvidia_smi_is_down():
    dispatcher = {**_dispatcher(own={"h3": {"pid": 1, "variant": "ref2va", "started_at": 1,
                                            "log": "/l", "ready": True}}),
                  "gpu": None, "gpu_error": "nvidia-smi недоступен"}
    gpu = {"ok": True, "dispatcher_error": None, "idle_release_at": None, "dispatcher": dispatcher,
           "queue": {"pending": 0, "paused": True, "running": None}}
    assert _banner(gpu) == {
        "visible": True, "tone": "own", "qwenUnload": False, "qwenRestore": False,
        "text": "Карту держит панель: H3 (память GPU неизвестна: nvidia-smi недоступен) — "
                "освободится кнопкой"}


@_needs_node
def test_mlx_shows_no_tag_demands_and_no_upscale_checkbox():
    proj = ("{kind: 'video', references: [], route: [{stage: 'upscale', enabled: true}], "
            "scenes: [{idx: 0, prompt: 'a dog'}]}")
    for engine in ("mlx", "undefined"):
        arg = "undefined" if engine == "undefined" else json.dumps(engine)
        assert _node_eval(f"console.log(JSON.stringify(app.projectTagWarningsHtml({proj}, {arg})));") == ""
        assert _node_eval(f"console.log(JSON.stringify(app.projectRouteHtml({proj}, {arg})));") == ""
        assert _node_eval(f"console.log(JSON.stringify(app.projectUpscaleHtml({proj}, {arg})));") == ""
        assert _node_eval("console.log(JSON.stringify(app.runCancelHtml({id: 'j1'}, "
                          f"{arg})));") == ""


@_needs_node
def test_upscale_status_and_retry_button():
    def html(status):
        return _node_eval("console.log(JSON.stringify(app.projectUpscaleHtml({id: 'p1', "
                          f"stages: {{upscale: '{status}'}}, "
                          "route: [{stage: 'upscale', enabled: true}]}, 'sglang')));")
    assert html("running") == '<div class="upscale-status" data-id="p1">Апскейл LTX: идёт</div>'
    assert html("failed") == (
        '<div class="upscale-status" data-id="p1">Апскейл LTX: упал <button type="button" '
        'class="upscale-retry" data-id="p1">Повторить апскейл</button></div>')
    assert _node_eval("console.log(JSON.stringify(app.projectUpscaleHtml({id: 'p1', "
                      "stages: {upscale: 'failed'}, route: [{stage: 'upscale', enabled: false}]}, "
                      "'sglang')));") == ""


@_needs_node
def test_cancel_button_and_library_edit_controls_and_request():
    assert _node_eval("console.log(JSON.stringify(app.runCancelHtml({id: 'j<1'}, 'sglang')));") == (
        ' <button type="button" data-act="cancel-run" data-id="j&lt;1">Отменить</button>')
    cards = [{"tag": "@alice", "kind": "person", "version": 2, "description": 'a "red" coat',
              "assets": []}]
    html = _node_eval(f"console.log(JSON.stringify(app.libraryCardsHtml({json.dumps(cards)}, '/o')));")
    # Task 9: caption counts the pictures; the card gets the new-file input and two actions
    assert html == ('<div class="lib-card"><b>@alice</b> <span class="muted">person, v2, картинок: 0</span>'
                    '<p>a &quot;red&quot; coat</p><input class="lib-edit-desc" '
                    'value="a &quot;red&quot; coat"> <button type="button" class="lib-save" '
                    'data-tag="@alice">Сохранить описание</button>'
                    '<input class="lib-new-files" type="file" multiple accept=".png,.jpg,.jpeg,.mp3,.wav"> '
                    '<button type="button" class="ghost" data-act="lib-new-version" data-tag="@alice">'
                    'Новая версия</button> '
                    '<button type="button" class="ghost" data-act="lib-delete" data-tag="@alice">Удалить</button>'
                    '<p class="why lib-card-error" hidden></p></div>')
    assert _node_eval("console.log(JSON.stringify(app.libraryUpdateRequest('@alice', 'new')));") == {
        "name": "alice", "body": {"description": "new"}}


@_needs_node
def test_chat_tags_are_sent_only_when_changed_and_bad_tags_are_reported():
    def body(raw, known):
        return _node_eval(f"console.log(JSON.stringify(app.chatTagsBody({json.dumps(raw)}, "
                          f"{json.dumps(known)})));")
    # untouched field (also empty and known-empty): nothing is sent, the session keeps its tags
    assert body("", "") == {"body": {}, "error": None}
    assert body("@alice @beach", "@alice @beach") == {"body": {}, "error": None}
    assert body(" @alice  @beach ", "@alice") == {"body": {"tags": ["@alice", "@beach"]}, "error": None}
    # cleared on purpose: an explicit empty list
    assert body("", "@alice") == {"body": {"tags": []}, "error": None}
    assert body("@alice @Bob", "") == {
        "body": {}, "error": "тег @Bob: строчные a-z, 0-9 и «-», 2–32 символа, с «@» в начале"}
    assert body("@a_b", "")["error"].startswith("тег @a_b:")
    assert body("alice", "")["error"].startswith("тег alice:")


_PANEL_UI = Path(__file__).resolve().parent / "_panel_ui_check.mjs"
_APP_URL = (Path(__file__).resolve().parent.parent / "h3_48gb" / "webui" / "app.js").as_uri()


def _ui(scenario: str):
    import os
    import subprocess
    env = {k: v for k, v in os.environ.items() if k != "NODE_OPTIONS"}
    result = subprocess.run([shutil.which("node"), str(_PANEL_UI), _APP_URL, scenario],
                            capture_output=True, text=True, timeout=60, env=env)
    assert result.returncode == 0, f"{scenario}: {result.stderr}"
    return json.loads(result.stdout)


CONFIRM = "H3 считает сцену — освободить карту? Сцена будет потеряна"


@_needs_node
def test_release_asks_then_repeats_with_confirm():
    assert _ui("release_confirmed") == {
        "bodies": [{}, {"confirm": True}], "confirms": [CONFIRM], "alerts": [], "repolled": True}


@_needs_node
def test_release_declined_sends_no_second_request():
    assert _ui("release_declined") == {
        "bodies": [{}], "confirms": [CONFIRM], "alerts": [], "repolled": False}


@_needs_node
def test_release_second_request_failing_is_reported_and_repolled():
    assert _ui("release_second_fails") == {
        "bodies": [{}, {"confirm": True}], "confirms": [CONFIRM], "alerts": ["диспетчер лёг"],
        "repolled": True}


@_needs_node
def test_notifications_first_snapshot_silent_then_project_ready_and_wait_not_repeated():
    # 11 minutes of waiting is already over the threshold on the very first snapshot, which only
    # primes `lastWaitNotifyMs`; carrying it over is what keeps the second poll quiet.
    assert _ui("notify") == {"afterFirst": [], "afterSecond": [
        {"title": "Проект готов", "body": "p2"}]}


@_needs_node
def test_a_render_exception_does_not_stop_notifications_or_the_poll():
    assert _ui("render_throws") == {"afterFirst": [], "afterSecond": [
        {"title": "Проект готов", "body": "p2"}]}


@_needs_node
def test_a_refused_route_change_goes_through_withproject_and_the_box_is_put_back():
    assert _ui("route_error") == {
        "puts": [["/api/projects/p1/route", {"upscale": False}]], "alerts": [],
        "projectRereads": 1, "providerReloads": 0, "boxChecked": True,
        "errorHidden": False,
        "errorHtml": '<b>Отказ: route_locked</b><pre>маршрут нельзя менять</pre>'}


@_needs_node
def test_upscale_retry_posts_to_the_retry_route():
    assert _ui("upscale_retry") == {
        "status": True, "posts": [["/api/projects/p1/upscale/retry", {}]]}


@_needs_node
def test_cancelling_the_running_job_shows_the_servers_message():
    assert _ui("cancel_run") == {
        "deletes": ["/api/jobs/j1"],
        "alerts": ["H3 досчитает сцену впустую, следующая задача начнётся после"]}


@_needs_node
def test_saving_a_library_description_puts_it_to_the_card_route():
    assert _ui("library_save") == {
        "puts": [["/api/library/alice", {"description": "a woman in a green coat"}]],
        "cardError": {"hidden": True, "textContent": ""}, "addFormError": False}


@_needs_node
def test_a_refused_card_edit_is_shown_on_that_card_not_in_the_add_form():
    assert _ui("library_save_error") == {
        "cardError": {"hidden": False, "textContent": "карточка занята"},
        "addFormErrorHidden": False}


@_needs_node
def test_scene_prompt_input_demands_a_tag_on_sglang_and_stays_silent_on_mlx():
    assert _ui("input_sglang") == {
        "title": "нужен хотя бы один референс (@тег) в сцене",
        "toggles": [["has-tag-issues", True]]}
    assert _ui("input_mlx") == {"title": "", "toggles": []}


@_needs_node
def test_a_scene_card_shows_why_the_scene_was_not_submitted():
    assert _ui("scene_error") == {"errors": [
        '<div class="scene-error why">сцена не поставлена: AssembleError: кадр &lt;залит&gt;</div>']}


@_needs_node
def test_the_mac_form_is_hidden_on_sglang_and_never_estimated():
    """Final review I3: its MLX defaults made /api/estimate answer 400 on every page load."""
    assert _ui("form_sglang") == {"formHidden": True, "noteHidden": False, "estimates": 0}
    assert _ui("form_mlx") == {"formHidden": False, "noteHidden": True, "estimates": 1}
