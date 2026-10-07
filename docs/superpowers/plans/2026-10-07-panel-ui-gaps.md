# Монтажная панель, волна 1.5 (дыры интерфейса): план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** владелец сам, в браузере, без ssh/curl/правки файлов проходит конвейер ролика на sglang: референсы → проект → сценарий → сцены → апскейл → сборка → «Скачать» финал.

**Architecture:** новое поведение — в JSON-API `h3_48gb/web.py` (+ `library.py`, `project.py`, `provider.py`); старый интерфейс `h3_48gb/webui/{index.html,app.js,style.css}` получает минимальные блоки, каждый — экспортируемая чистая функция `app.js` (`export function …Html(data, ctx)`) плюс тонкий обработчик в DOM-половине. Волна 2 (IA, variant-a) переносит функции, заменяя только вёрстку.

**Tech Stack:** Python 3.12 stdlib, node (проверки фронта через pytest), playwright MCP (только приёмка).

**Spec:** `docs/superpowers/specs/2026-10-07-panel-ui-gaps-design.md` (читать целиком перед любой задачей). Спека W1: `docs/superpowers/specs/2026-10-06-panel-on-alex-neuro-design.md`. Аудит: `~/worktrees/battle/ui-audit/REPORT.md`.

**Ветка:** `feat/panel-ui-gaps`, worktree `~/worktrees/panel-ui-gaps`. В том же worktree параллельно работает агент Task 0.

**Запуск плана — только при чистом worktree.** Первый исполнитель стартует, когда фикс-раунд Task 0 закоммичен и `git status` не показывает `M`-файлов. Перед каждой задачей — `git status` и `git log -3`. **Если в файле, который задача правит, есть чужие незакоммиченные изменения — остановиться и сообщить координатору**, не делать `git add` этого файла (файловой гранулярности не хватит: в коммит уедут чужие куски, в худшем случае с чужой мутацией). `git add -A` запрещён.

**Номера строк `web.py`** сверены 07.10 после коммита фикс-раунда Task 0 (`c8b34e52`); где осталось «сверить» — номер не проверялся. Исполнитель всё равно ищет функцию по имени: задачи 1–5 сдвигают строки друг другу.

## Global Constraints

- **Task 0 (внешний):** коммиты `4005de38`, `626db48d`, фикс-раунд `3829a5cb`, `84cbbcba`, `c8b34e52`. Контракт: **seed, steps, refs и сид проекта — только sglang** (на MLX `scenes[i].seed/steps/refs` и `PUT …/settings {seed: N}` — 400 `args_invalid` «`…` is only for the sglang engine», проверка движка **до** проверки значения); у сцены в `PUT …/scenes` необязательные `seed` (int ≥ 0), `steps` (2..100), `refs` (подключённые `@`-теги, дубли убираются при сохранении; условия без `<Subject N>`, идут первыми); `PUT …/settings {i2v_prefix?, seed?}` (`seed: null` — на любом движке), `project.seed` в выдаче; эффективный сид `scene.seed → project.seed → 42`; оценка времени учитывает `steps`. Если до начала задачи контракт изменился (`git log -- tests/test_sglang_scene_params.py tests/test_sglang_scene_refs.py`) — поправить только соответствующее поле своей задачи и записать в отчёт.
- Окружение: `PY=~/venvs/h3-panel/bin/python`. Полный прогон перед каждым коммитом: `env -u NODE_OPTIONS $PY -m pytest -q -p no:cacheprovider` в форграунде, timeout 600000, 0 failed. Точечные прогоны — `env -u NODE_OPTIONS $PY -m pytest -q -p no:cacheprovider tests/<файл>::<тест>`.
- **Тест не написан, пока не видел его красным** (`CLAUDE.md`): шаг «увидеть красным» до кода и шаг мутации после — удалить/обратить защищаемую строку, увидеть красный, вернуть. Текст assertion-ошибки мутации — в отчёт задачи. Без этого задача не сдана.
- Тесты пиннят содержание: тела ответов, разметку, тела запросов — `==` целиком. Не `in`, не `len()`, не «ключ есть». Единственное исключение — проверка строки внутри системного промпта LLM (там проверяется целая строка `"\n<строка>\n" in system`). Кусок большой страницы (`index.html`) вырезается регуляркой и сравнивается `==`.
- **Никаких заглушек в тестах плана:** каждый сценарий харнесса, на который ссылается задача, написан в плане кодом. Если исполнитель видит в плане прозу вместо кода теста — пишет код сам и показывает его в отчёте вместе с красным прогоном.
- **JS-моки — только точное сравнение:** `closest(sel)`/`match(sel)` сравнивают `sel === "…"`, карты селекторов харнесса — по точному ключу (`Object.hasOwn`), никаких `.includes()`/`startsWith` в моках (память проекта `js-driver-mocks-need-exact-match`: три выжившие мутации за два дня).
- Моки ленивого конвейера не нужны (код задач не трогает MLX), но **порядок асинхронных ответов** — нужен: где баг зависит от порядка (`/api/state` против `/api/library`), харнесс задерживает ответ явно.
- Сообщения человеку — по-русски; комментарии, докстринги, идентификаторы — по-английски, как во всём пакете.
- Новый код ошибки — в `h3_48gb/cli.py:ERROR_CODES` (или `library.ERROR_CODES`, если так уже устроено для `library_*` — проверить, как `test_cli.py:1735` собирает коды) и статус в `web.ERROR_STATUS` (`web.py:495`), если не 400.
- CSS — только токены `:root` (`style.css:51`), без новых цветов; правила на 390 px — в существующем `@media (max-width: 420px)` (`style.css:1563`).
- Сервер alex-neuro, GPU, sglang, ComfyUI, Qwen в задачах 1–12 не трогаются. Задача 13 — живой проход координатором.
- Каждая задача — свой коммит (`git add` только своих файлов). Сообщение — по-русски, как в истории ветки.

## Review Focus

Входы, которые легко пропустить. Тест на каждый вписан в задачу-владельца.

1. **Правка сцены теряется при любой перерисовке модалки**, а не только при структурных действиях: `renderProjectModal` пересобирает `#project-body` после каждой галочки референса, `i2v_prefix`, загрузки кадра, «сохранено ✓». `syncDraftFromDom()` — первая строка `renderProjectModal`. Тесты `editor_dom_edits_survive_add`, `editor_edits_survive_ref_pin` (задача 7).
2. **«Утвердить» при несохранённой правке** утверждает старый сценарий. Тест `editor_approve_saves_first` и `editor_approve_stops_on_save_error` (задача 7).
3. **Перестановка сцен и стартовый кадр.** `start_image` — свойство первого кадра ролика, остаётся на позиции 0; `fresh_start` сцены, ставшей нулевой, снимается. Тест `test_move_scene_keeps_start_image_at_position_0` (задача 7).
4. **Одно правило — две реализации.** Сетка длительностей (JS-подсказка против `web._snap_video_scenes_sglang`) и каскад пересъёмки (JS против `Project.invalidate_scene_chain`) сверяются таблицей через Python и node. Тесты `test_grid_hint_matches_the_server_snap`, `test_retry_cascade_matches_invalidate_scene_chain` (задачи 7, 10).
5. **Тексты блокировки сервера и UI расходятся.** `web.PROJECT_LOCK_TEXT` и `app.PROJECT_LOCK_TEXT` сверяются равенством. Тест `test_lock_texts_are_the_same_on_both_sides` (задача 10).
6. **Отказ пересъёмки не должен иметь побочных эффектов:** проверка тела до отмены хвоста очереди. Тест `test_a_refused_retry_touches_nothing` (задача 2).
7. **Превью библиотеки и порядок ответов:** `/api/library` раньше `/api/state`. Тест `library_preview_after_late_state` (задача 9).
8. **`shares_gpu: null` — не «внешний».** Тест `test_plate_text_for_each_gpu_sharing_answer` (задача 12).
9. **Удаление карточки, подключённой к готовому проекту** — отказ, а не тихая поломка будущей пересъёмки. Тест `test_a_card_pinned_by_a_finished_project_is_not_deleted` (задача 3).
10. **MLX и поля sglang.** Редактор на MLX не показывает и `scenesPayload` не шлёт `seed/steps/refs`, сид проекта не показывается — иначе сервер отклонит всё тело. Тест `test_scenes_payload_sends_only_what_is_set` (MLX-ветка) и `test_editor_html_on_mlx` (задача 7).
11. **Устаревший отчёт апскейла.** `report.json` переживает пересъёмку; сила и движение — только при `stages.upscale === "done"`. Тест `test_stale_upscale_report_is_not_shown` (задача 11).
12. **CSS-тест и правила-потомки.** `.lib-card img` не считается правилом для `.lib-card`. Тест `test_every_class_the_new_blocks_emit_has_a_rule` (задача 6), мутация показывается красной при живом `.lib-card img`.

---

## Карта файлов

| Файл | Ответственность | Задачи |
|---|---|---|
| `h3_48gb/web.py` | блокировки идущего проекта, пересъёмка с правкой, DELETE карточки, `versions`, чат проекта, длительности чата по движку, `shares_gpu`, `upscale_report`, `h3-prompt` сцены | 1, 2, 3, 4, 5, 8b |
| `h3_48gb/project.py` | `invalidate_scene_chain(idx, edits=…)` | 2 |
| `h3_48gb/library.py` | `card_history`, `delete_card`, `library_card_in_use` | 3 |
| `h3_48gb/provider.py` | `shares_gpu(cfg)` | 4 |
| `h3_48gb/cli.py` | коды ошибок | 3 |
| `h3_48gb/webui/app.js` | чистые функции разметки и обработчики | 6–12, 8b |
| `h3_48gb/webui/index.html` | кнопки зоны «Проекты», скрытый `#scene0-file` | 6, 8 |
| `h3_48gb/webui/style.css` | правила новых и неоформленных блоков, 390 px | 6–12 |
| `tests/_ui_harness.mjs` (новый) | общий фейковый DOM/fetch для node-сценариев | 6 |
| `tests/_panel_ui_check.mjs` | переезд на харнесс без смены поведения | 6 |
| `tests/_ui_gaps_check.mjs` (новый) | сценарии проводки DOM волны 1.5 | 6–12 |
| `tests/test_project_locks.py`, `tests/test_scene_retry_edit.py`, `tests/test_library_delete.py`, `tests/test_chat_project.py`, `tests/test_upscale_report.py`, `tests/test_scene_h3_prompt.py` (новые) | серверные тесты | 1–5, 8b |
| `tests/test_webui_gaps.py` (новый) | чистые функции и сценарии UI | 6–12 |

## Порядок и зависимости

Фикс-раунд Task 0 закоммичен (`3829a5cb`, `84cbbcba`, `c8b34e52`); старт — при чистом `git status`. `1 → 2 → 3 → 4 → 5` (сервер, по смыслу независимы, но правят `web.py` — строго последовательно), затем `6` (харнесс и основа UI), `7 → 8 → 8b` (редактор; 8b — сервер+UI «Промпт для H3»), `9` (зависит от 1 — блокировка референсов, и 3 — `versions`/DELETE; после 8 — общий `uploadToServer`), `10` (от 1, 2, 9 — `lock` в `projectReferencesHtml`), `11` (от 5, 10 — «Черновая сборка» переезжает), `12` (от 4, 7), `13` — приёмка после всех.

---

## Task 0 (внешний): seed/steps сцены, сид проекта, явные refs сцены

Сделано другим агентом: `4005de38` (seed/steps, seed проекта), `626db48d` (refs), фикс-раунд `3829a5cb` (`steps` в ключе оценки), `84cbbcba` (seed/steps/refs и сид проекта — только sglang; дубли `refs` убираются), `c8b34e52`. В этом плане — только зависимость. Исполнитель задач плана файлы Task 0 не правит.

- [ ] **Проверка перед задачей 1:** фикс-раунд закоммичен (`git log --oneline -5`), `git status` без `M`-файлов; `env -u NODE_OPTIONS $PY -m pytest -q -p no:cacheprovider tests/test_sglang_scene_params.py tests/test_sglang_scene_refs.py` — зелёный; `grep -n "only for the sglang engine" h3_48gb/web.py` показывает запрет и для `settings.seed`, и для `scenes[i].seed/steps/refs`. Если контракт в коде иной, чем в Global Constraints, — сообщить координатору до старта.

---

## Task 1: Идущий проект: запрет правки референсов, настроек и галочки апскейла

**Files:**
- Modify: `h3_48gb/web.py` — `_put_project_references` (`web.py:4255`), `_put_project_settings` (`web.py:3597`), `_put_project_route` (`web.py:3558`); новая константа `PROJECT_LOCK_TEXT` рядом с `ERROR_STATUS`
- Test: `tests/test_project_locks.py` (новый)

**Interfaces:**
- Consumes: `_project_active_job(proj, jobs)` (`web.py:2138`), `_project_job_by_args(jobs, path, kind)`, `q.scan`, код `project_running` (409, уже в `ERROR_STATUS`).
- Produces:
  - `web.PROJECT_LOCK_TEXT: dict[str, str]` с ключами `"references"`, `"settings"`, `"route"` — тексты дословно:
    - `references`: `"Проект считается — референсы меняются после конца прогона. Чтобы поменять для части сцен: дождитесь конца и пересчитайте с нужной сцены."`
    - `settings`: `"Проект считается — начало сцепленной сцены и сид меняются после конца прогона. Чтобы поменять для части сцен: дождитесь конца и пересчитайте с нужной сцены."`
    - `route`: `"Идёт апскейл или сборка — галочку апскейла можно поменять после них."`
  - `PUT …/references`, `PUT …/settings` → 409 `project_running` с `PROJECT_LOCK_TEXT[...]`, `detail {"id", "active": <kind активной задачи>}`, пока `_project_active_job(...) is not None`.
  - `PUT …/route` → 409 `project_running` с `PROJECT_LOCK_TEXT["route"]`, пока есть pending/running задача `KIND_UPSCALE` или `KIND_ASSEMBLE` этого проекта.

- [ ] **Step 1: Написать падающие тесты**

`tests/test_project_locks.py`:
```python
"""Wave 1.5, spec §5.5: while a project has a job in the queue its references and settings are
refused (the chained scenes would silently get two halves); the upscale tick only while the
upscale or the assembly runs."""
import pytest

from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import queue as q
from test_web import _call, _pending, _serve

PNG = b"\x89PNG\r\n\x1a\n"
REFS = ("Проект считается — референсы меняются после конца прогона. Чтобы поменять для части "
        "сцен: дождитесь конца и пересчитайте с нужной сцены.")
SETTINGS = ("Проект считается — начало сцепленной сцены и сид меняются после конца прогона. "
            "Чтобы поменять для части сцен: дождитесь конца и пересчитайте с нужной сцены.")
ROUTE = "Идёт апскейл или сборка — галочку апскейла можно поменять после них."


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    outdir = tmp_path / "outdir"
    (outdir / "uploads").mkdir(parents=True)
    (outdir / "uploads" / "face.png").write_bytes(PNG)
    lib.create_card(outdir, tag="@amazon", kind="person", description="an armored amazon",
                    assets=[outdir / "uploads" / "face.png"])
    server = _serve(q.layout(outdir / "queue")["root"], outdir)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def _ready(live):
    proj = p.create_project(live.outdir, "video", "Бой")
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/scenes", {
        "scenes": [{"prompt": "@amazon walks", "duration": 8.0},
                   {"prompt": "@amazon runs", "duration": 8.0}],
        "references": [{"tag": "@amazon"}]})
    assert status == 200, body
    return proj


def _error(body):
    error = body.get("error") or {}
    return error.get("code"), error.get("message")


def test_references_and_settings_are_refused_while_a_scene_is_queued(live):
    proj = _ready(live)
    assert _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})[0] == 200
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/references",
                         {"references": [{"tag": "@amazon"}]})
    assert (status, _error(body), body["error"]["detail"]) == (
        409, ("project_running", REFS), {"id": proj.id, "active": "scene"})
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/settings", {"i2v_prefix": "x"})
    assert (status, _error(body)) == (409, ("project_running", SETTINGS))
    assert p.load_project(proj.path).i2v_prefix == p.DEFAULT_I2V_PREFIX


def test_the_same_edits_pass_when_nothing_is_queued(live):
    proj = _ready(live)
    assert _call(live, "PUT", f"/api/projects/{proj.id}/references",
                 {"references": [{"tag": "@amazon", "version": 1}]})[0] == 200
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/settings", {"i2v_prefix": "x"})
    assert (status, body["project"]["i2v_prefix"]) == (200, "x")


def test_route_is_open_during_scenes(live):
    proj = _ready(live)
    assert _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})[0] == 200
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/route", {"upscale": False})
    assert status == 200, body


@pytest.mark.parametrize("kind, args0, note, stem, active", [
    (q.KIND_UPSCALE, "upscale", "upscale project {id}", "upscale/job-upscale", "upscale"),
    (q.KIND_ASSEMBLE, "assemble", "assemble project {id}", "assembly/job-final", "assembly"),
])
def test_route_is_closed_while_the_upscale_or_the_assembly_runs(live, kind, args0, note, stem,
                                                                 active):
    other = p.create_project(live.outdir, "video", "Другой")
    q.submit(live.queue_root, [args0, "--project", str(other.path)], note.format(id=other.id),
             {"output_stem": str(other.path.parent / stem)}, {}, kind=kind)
    status, body = _call(live, "PUT", f"/api/projects/{other.id}/route", {"upscale": False})
    assert (status, body["error"]) == (409, {
        "code": "project_running", "message": ROUTE, "detail": {"id": other.id, "active": active}})
    assert [e for e in p.load_project(other.path).route if e["stage"] == "upscale"] == [
        {"stage": "upscale", "enabled": True}]
```

- [ ] **Step 2: Увидеть красным**

`env -u NODE_OPTIONS $PY -m pytest -q -p no:cacheprovider tests/test_project_locks.py` — ожидается: первый тест и обе параметризации последнего падают (`assert (200, ...) == (409, ...)`), второй и третий зелёные (они фиксируют, что запрет не задел свободный проект и сцены). Форму конверта ошибки (`code/message/detail`) сверить с `_error_bytes` до запуска. Если третий падает на форме `route` (у созданного проекта маршрут другой) — поправить ожидание по `project.default_route("video")`, не по факту ответа.

- [ ] **Step 3: Реализация**

В `web.py` рядом с `ERROR_STATUS`:
```python
#: Wave 1.5, spec §5.5: why an edit of a running project is refused. The page shows the same
#: text before the click (`app.PROJECT_LOCK_TEXT`, pinned equal by a test).
PROJECT_LOCK_TEXT = {
    "references": "Проект считается — референсы меняются после конца прогона. Чтобы поменять "
                  "для части сцен: дождитесь конца и пересчитайте с нужной сцены.",
    "settings": "Проект считается — начало сцепленной сцены и сид меняются после конца прогона. "
                "Чтобы поменять для части сцен: дождитесь конца и пересчитайте с нужной сцены.",
    "route": "Идёт апскейл или сборка — галочку апскейла можно поменять после них.",
}
```
В `_Handler` — один помощник и три вызова в начале трёх обработчиков (после `_load_project`, до `_json_request`, чтобы отказ не зависел от тела):
```python
    def _refuse_while_running(self, proj, what: str) -> None:
        with queue_errors(self.server.queue_root):
            jobs, _broken = q.scan(self.server.queue_root)
        if what == "route":
            busy = (_project_job_by_args(jobs, proj.path, q.KIND_UPSCALE)
                    or _project_job_by_args(jobs, proj.path, q.KIND_ASSEMBLE))
            active = "upscale" if busy and busy.kind == q.KIND_UPSCALE else "assembly"
        else:
            found = _project_active_job(proj, jobs)
            busy, active = found, (found or {}).get("kind")
        if busy:
            raise CliError("project_running", PROJECT_LOCK_TEXT[what],
                           {"id": proj.id, "active": active})
```
(Проверить, что у `Job` есть атрибут `kind`; если нет — сравнивать по `args[0]`.) Отказ стоит **до** `_json_request`, тело остаётся непрочитанным — осознанно: так же устроен `_load_project` (раньше тела), сервер HTTP/1.0 закрывает соединение, тела маленькие. Записать это одной строкой в докстринг помощника.

- [ ] **Step 4: Зелёный** — тот же прогон, 3 passed.
- [ ] **Step 5: Мутация** — закомментировать вызов `_refuse_while_running(proj, "references")` → красный первого теста; убрать `or _project_job_by_args(..., q.KIND_ASSEMBLE)` → красный `[assembly]`-параметризации; обратить `active` → красный по `detail`. Ошибки — в отчёт.
- [ ] **Step 6: Полный прогон** — 0 failed. Если упал старый тест, который правил настройки идущего проекта, — это его прежнее допущение; разобрать и сообщить координатору, не ослаблять запрет.
- [ ] **Step 7: Коммит** — `git add h3_48gb/web.py tests/test_project_locks.py && git commit -m "feat(projects): правка референсов, настроек и галочки апскейла идущего проекта — отказ project_running"`

---

## Task 2: Пересъёмка сцены с новым сидом, промптом, шагами

**Files:**
- Modify: `h3_48gb/project.py:827` (`invalidate_scene_chain`)
- Modify: `h3_48gb/web.py` `_retry_project_scene` (`web.py:5300`); вынести проверки `seed`/`steps`/`prompt` из цикла `_put_project_scenes` (`web.py:4312-4365`) в помощник
- Test: `tests/test_scene_retry_edit.py` (новый)

**Interfaces:**
- Produces:
  - `Project.invalidate_scene_chain(idx: int, *, edits: dict | None = None) -> Project` — `edits` ⊆ `{"prompt", "seed", "steps"}` записываются в сцену `idx` под тем же замком и в той же записи, что сброс хвоста.
  - `web._scene_edit_fields(raw: dict, i: int, *, sglang: bool) -> dict` — проверенные `prompt`/`seed`/`steps` из `raw` (только присутствующие), тексты ошибок и **порядок проверок** те же, что в `PUT …/scenes` после фикс-раунда Task 0: сначала «`scenes[i].<поле>` is only for the sglang engine» на MLX, потом значение; `_put_project_scenes` пользуется им же (проверку `refs` помощник не трогает — она остаётся в `_put_project_scenes`).
  - `POST /api/projects/<id>/scenes/<idx>/retry` — тело `{prompt?, seed?, steps?}` (`_json_request(allowed=("prompt", "seed", "steps"))`); новый промпт проверяется на теги `_scene_reference_errors(proj, [сцена с новым промптом], outdir)` при sglang; отказ при `status == "pending"`: 409 `project_stage_not_ready`, сообщение `"сцена #<idx> ещё не снималась — пересчитывать нечего"`. **Все проверки — до `_cancel_project_scene_tail_jobs`.**

- [ ] **Step 1: Написать падающие тесты**

`tests/test_scene_retry_edit.py`:
```python
"""Wave 1.5, spec §5.6: «Пересчитать сцену» may carry a new prompt, seed and steps; they are
checked like `PUT /scenes`, stored with the invalidation in one write, and a refusal changes
nothing -- neither the project nor the queue."""
import pytest

from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import queue as q
from test_web import _call, _pending, _serve

PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    outdir = tmp_path / "outdir"
    (outdir / "uploads").mkdir(parents=True)
    (outdir / "uploads" / "face.png").write_bytes(PNG)
    lib.create_card(outdir, tag="@amazon", kind="person", description="an armored amazon",
                    assets=[outdir / "uploads" / "face.png"])
    server = _serve(q.layout(outdir / "queue")["root"], outdir)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def _shot_scene_0(live):
    """Two scenes approved; scene 0 marked done with a clip, its queued job left in place, and a
    pending upscale job of the same project (the retry cancels it -- a refusal must not)."""
    proj = p.create_project(live.outdir, "video", "Бой")
    assert _call(live, "PUT", f"/api/projects/{proj.id}/scenes", {
        "scenes": [{"prompt": "@amazon walks", "duration": 8.0},
                   {"prompt": "@amazon runs", "duration": 8.0}],
        "references": [{"tag": "@amazon"}]})[0] == 200
    assert _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})[0] == 200
    clip = proj.path.parent / "scenes" / "s0.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"mp4")
    p.load_project(proj.path).set_scene_status(0, "done", job_id=None, clip_path=str(clip))
    q.submit(live.queue_root, ["upscale", "--project", str(proj.path)], f"upscale project {proj.id}",
             {"output_stem": str(proj.path.parent / "upscale" / "job-upscale")}, {},
             kind=q.KIND_UPSCALE)
    return proj


def _arg(args, flag):
    return args[args.index(flag) + 1]


def test_retry_with_a_new_seed_and_prompt_rewrites_the_scene_and_resubmits(live):
    proj = _shot_scene_0(live)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/scenes/0/retry",
                         {"prompt": "@amazon jumps", "seed": 7, "steps": 30})
    assert status == 200, body
    scene = p.load_project(proj.path).scenes[0]
    assert (scene["prompt"], scene["seed"], scene["steps"], scene["status"]) == (
        "@amazon jumps", 7, 30, "running")
    (job,) = [j for j in _pending(live) if j.args[0] == "generate"]
    assert (_arg(job.args, "--seed"), _arg(job.args, "--steps"), job.args[1]) == (
        "7", "30",
        "subject_definitions:\n<Subject 1> is an armored amazon, appearance from <Picture 1>."
        "\n\n<Subject 1> jumps")


def test_retry_without_a_body_keeps_the_old_behaviour(live):
    proj = _shot_scene_0(live)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/scenes/0/retry", {})
    assert status == 200, body
    scene = p.load_project(proj.path).scenes[0]
    assert (scene["prompt"], "seed" in scene, scene["status"]) == ("@amazon walks", False, "running")


@pytest.mark.parametrize("payload, code, message", [
    ({"seed": -1}, "args_invalid", "`scenes[0].seed` must be an integer >= 0"),
    ({"steps": 1}, "args_invalid", "`scenes[0].steps` must be an integer between 2 and 100"),
    ({"prompt": "  "}, "args_invalid", "`scenes[0].prompt` must be a non-empty string"),
    ({"prompt": "@ghost jumps"}, "unknown_tag", "теги не подключены к проекту: @ghost"),
])
def test_a_refused_retry_touches_nothing(live, payload, code, message):
    proj = _shot_scene_0(live)
    before_jobs = sorted(job.id for job in _pending(live))
    before = p.load_project(proj.path).scenes
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/scenes/0/retry", payload)
    assert (status // 100, body["error"]["code"], body["error"]["message"]) == (4, code, message)
    assert p.load_project(proj.path).scenes == before
    assert sorted(job.id for job in _pending(live)) == before_jobs


def test_seed_and_steps_on_mlx_are_refused_before_their_value(tmp_path):
    """Task 0's fix round: seed/steps are sglang-only, and the engine is checked first -- an
    otherwise valid seed on MLX is refused for the engine, not accepted."""
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    live = _serve(q.layout(outdir / "queue")["root"], outdir)
    try:
        proj = p.create_project(outdir, "video", "Мак")
        proj.scenes = [{"idx": 0, "prompt": "a cat", "duration": 8.0, "status": "done",
                        "job_id": None, "clip_path": "/c.mp4", "keyframe_path": None}]
        proj.save()
        for seed in (7, -1):   # -1: the engine is refused first, not the value
            status, body = _call(live, "POST", f"/api/projects/{proj.id}/scenes/0/retry",
                                 {"seed": seed})
            assert (status, body["error"]["code"], body["error"]["message"]) == (
                400, "args_invalid", "`scenes[0].seed` is only for the sglang engine")
    finally:
        live.httpd.shutdown()
        live.httpd.server_close()


def test_a_never_shot_scene_is_not_retried(live):
    proj = _shot_scene_0(live)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/scenes/1/retry", {})
    assert (status, body["error"]["code"], body["error"]["message"]) == (
        409, "project_stage_not_ready", "сцена #1 ещё не снималась — пересчитывать нечего")
```
Сообщения ошибок в параметризации — **как их сейчас пишет `_put_project_scenes`** (`web.py:4312-4345`) и `library.build_ref2va`; если хоть одно расходится с кодом — исправить ожидание по коду дословно (это перенос одного правила, текст не выдумывается), и записать в отчёт.

- [ ] **Step 2: Увидеть красным** — первый тест: `KeyError: 'seed'` или `("@amazon walks", …) != ("@amazon jumps", …)`; параметризация: 200 вместо отказа; последний — 200 вместо 409.
- [ ] **Step 3: Реализация**

`project.py`, `invalidate_scene_chain`: новый keyword `edits=None`; внутри замка, после `_find_scene`, до цикла:
```python
            if edits:
                target = _find_scene(data["scenes"], idx)
                target.update({key: edits[key] for key in ("prompt", "seed", "steps")
                               if key in edits})
```
`web.py`: `_scene_edit_fields` — тело трёх проверок из цикла `_put_project_scenes` без изменения текстов (цикл вызывает помощник). `_retry_project_scene`: `payload = self._json_request(allowed=("prompt", "seed", "steps"))`; `edits = self._scene_edit_fields(payload, idx, sglang=engine.is_sglang())`; найти сцену (`UnknownScene` → как сейчас); `status == "pending"` → отказ; при `"prompt" in edits` и sglang — `errors = _scene_reference_errors(proj, [{**scene, **edits}], self.server.outdir)`, первая ошибка → `CliError(errors[0]["code"], errors[0]["message"], {"idx": idx})`; только потом отмена хвоста и `proj.invalidate_scene_chain(idx, edits=edits)`.

- [ ] **Step 4: Зелёный.**
- [ ] **Step 5: Мутация** — (а) убрать `target.update(...)` → красный первого теста; (б) перенести вызов `_scene_edit_fields` после `_cancel_project_scene_tail_jobs` **и** `_cancel_project_upscale_jobs` → красный `test_a_refused_retry_touches_nothing` (pending upscale-задача фикстуры исчезла до отказа); (в) убрать отказ для `pending` → красный последнего теста; (г) поменять местами проверку движка и значения → красный `test_seed_and_steps_on_mlx…` на `seed=-1`. Ошибки — в отчёт.
- [ ] **Step 6: Полный прогон.** Старые тесты ретрая шлют `{}` — должны остаться зелёными. Если какой-то старый тест ретраит `pending`-сцену — остановиться и сообщить координатору (это смена контракта, а не поломка теста).
- [ ] **Step 7: Коммит** — `feat(projects): пересъёмка сцены с новым промптом, сидом и шагами; не снятую сцену не пересчитываем`.

---

## Task 3: Библиотека: история версий и безопасное удаление карточки

**Files:**
- Modify: `h3_48gb/library.py` (`list_cards`, новые `card_history`, `delete_card`, код в `ERROR_CODES`)
- Modify: `h3_48gb/web.py` — `_route_delete` (`web.py:3612`), новый `_delete_card`, `ERROR_STATUS` (`library_card_in_use: 409`); `h3_48gb/cli.py:ERROR_CODES`, если коды библиотеки собираются туда
- Test: `tests/test_library_delete.py` (новый)

**Interfaces:**
- Produces:
  - `library.card_history(outdir, tag) -> list[dict]` — `[{version, kind, description, assets (абсолютные пути), created}]` по возрастанию версии.
  - `library.list_cards(outdir)` — у каждой карточки дополнительно `"versions": card_history(...)`.
  - `library.delete_card(outdir, tag, *, pinned_by: list[dict], now: str | None = None) -> Path` — `pinned_by` непуст → `LibraryError("library_card_in_use", "<tag> подключена к проектам: «T1» (id1), «T2» (id2) — отключите её там или удалите проекты", {"tag", "projects": pinned_by})`; иначе каталог карточки переносится в `<outdir>/library/.trash/<name>-<now|YYYYMMDDHHMMSS>` и путь возвращается. Неизвестный тег → `library_card_not_found`.
  - `DELETE /api/library/<name>` → `200 {"ok": true, "trashed": "<путь>"}`; `pinned_by` = все проекты из `project.list_projects(outdir)`, у которых тег в `references` (`[{"id", "title"}]`, порядок `list_projects`).

- [ ] **Step 1: Написать падающие тесты**

`tests/test_library_delete.py`:
```python
"""Wave 1.5, spec §5.3: the library lists every version of a card, and a card is deleted only when
no project pins it -- moved to library/.trash, never erased."""
from pathlib import Path

import pytest

from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import queue as q
from test_web import _call, _serve

PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def outdir(tmp_path):
    out = tmp_path / "outdir"
    (out / "uploads").mkdir(parents=True)
    (out / "uploads" / "a.png").write_bytes(PNG)
    lib.create_card(out, tag="@alice", kind="person", description="a woman",
                    assets=[out / "uploads" / "a.png"], now="2026-10-07T10:00:00")
    lib.update_card(out, "@alice", description="a woman in green", now="2026-10-07T11:00:00")
    return out


@pytest.fixture
def live(outdir, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    server = _serve(q.layout(outdir / "queue")["root"], outdir)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def test_the_listing_carries_every_version(live, outdir):
    status, body = _call(live, "GET", "/api/library")
    v1 = str(outdir / "library" / "alice" / "v1" / "01-a.png")
    assert status == 200
    assert body["cards"][0]["versions"] == [
        {"version": 1, "kind": "person", "description": "a woman", "assets": [v1],
         "created": "2026-10-07T10:00:00"},
        {"version": 2, "kind": "person", "description": "a woman in green", "assets": [v1],
         "created": "2026-10-07T11:00:00"}]


def test_delete_moves_the_card_to_the_trash(outdir):
    card_json = (outdir / "library" / "alice" / "card.json").read_text(encoding="utf-8")
    trashed = lib.delete_card(outdir, "@alice", pinned_by=[], now="20261007120000")
    assert trashed == outdir / "library" / ".trash" / "alice-20261007120000"
    assert (trashed / "card.json").read_text(encoding="utf-8") == card_json
    assert lib.list_cards(outdir) == []


def test_the_route_deletes_a_free_card_and_the_tag_can_be_made_again(live, outdir):
    status, body = _call(live, "DELETE", "/api/library/alice")
    assert status == 200, body
    trashed = Path(body["trashed"])
    assert (trashed.parent, sorted(x.name for x in trashed.parent.iterdir())) == (
        outdir / "library" / ".trash", [trashed.name])
    assert _call(live, "GET", "/api/library")[1]["cards"] == []
    lib.create_card(outdir, tag="@alice", kind="person", description="again",
                    assets=[outdir / "uploads" / "a.png"])
    assert [c["version"] for c in lib.list_cards(outdir)] == [1]


def test_a_card_pinned_by_a_finished_project_is_not_deleted(live, outdir):
    proj = p.create_project(outdir, "video", "Бой")
    proj.set_references([{"tag": "@alice", "version": 1}])
    proj.set_stage_status("assembly", "done")
    status, body = _call(live, "DELETE", "/api/library/alice")
    assert (status, body["error"]) == (409, {
        "code": "library_card_in_use",
        "message": f"@alice подключена к проектам: «Бой» ({proj.id}) — отключите её там или "
                   "удалите проекты",
        "detail": {"tag": "@alice", "projects": [{"id": proj.id, "title": "Бой"}]}})
    assert [c["tag"] for c in lib.list_cards(outdir)] == ["@alice"]


def test_an_unknown_card_is_404(live):
    status, body = _call(live, "DELETE", "/api/library/nobody")
    assert (status, body["error"]["code"]) == (404, "library_card_not_found")
```
Форму `body["error"]` (есть ли в ней `detail`, как называются ключи) сверить с `_error_bytes` (`web.py`) и уже существующими тестами библиотеки (`tests/test_library_web.py`) до запуска; ожидание менять только по форме конверта, не по тексту.

- [ ] **Step 2: Увидеть красным** — `KeyError: 'versions'`, `AttributeError: module 'h3_48gb.library' has no attribute 'delete_card'`, `404 no route for DELETE`.
- [ ] **Step 3: Реализация** — `card_history` через `_read` + `card["versions"]`, пути через `_card_dir(...) / rel`; `list_cards` → `{**get_card(...), "versions": card_history(...)}`; `delete_card` — проверка `pinned_by`, `_read` (404), `(library_root(outdir) / ".trash").mkdir(exist_ok=True)`, `card_dir.rename(target)` под `_card_lock(card_dir)`. В `web.py`: в `_route_delete` — `if path.startswith("/api/library/"): return self._delete_card(path[len("/api/library/"):])`; `_delete_card` собирает `pinned_by` и зовёт `self._library_call(library_module.delete_card, ...)`.
- [ ] **Step 4: Зелёный.**
- [ ] **Step 5: Мутация** — убрать проверку `pinned_by` → красный `test_a_card_pinned_by_a_finished_project_is_not_deleted`; заменить `rename` на `shutil.rmtree` → красный `test_delete_moves_the_card_to_the_trash`.
- [ ] **Step 6: Полный прогон** (включая `tests/test_cli.py` — реестр кодов ошибок).
- [ ] **Step 7: Коммит** — `feat(library): история версий в списке и удаление карточки в .trash с отказом, если она подключена к проекту`.

---

## Task 4: LLM-путь на сервере: чат проекта, длительности по движку, теги сессии, `shares_gpu`

**Files:**
- Modify: `h3_48gb/web.py` — `CHAT_SOURCE_KINDS` (`web.py:268`), `_create_chat`, `_create_project` (`web.py:4427`, проверка длительностей — внутри него, `SCENE_MIN_SECONDS <= duration`), системный блок `## Context` (`web.py:6135`), `_providers`
- Modify: `h3_48gb/provider.py` (новая `shares_gpu`)
- Test: `tests/test_chat_project.py` (новый)

**Interfaces:**
- Produces:
  - `POST /api/chat {source: {kind: "project", id}}` — проект должен существовать (`project_not_found` иначе); сессия получает `tags = [ref.tag for ref in proj.references]` и `kind = "video"` (тогда строка `kind: video` стоит в контексте модели с первого хода, `_locked_turn`), `source` хранится как прислан.
  - `POST /api/projects {session_id}` для видео: длительность сцены в `[sglang_args.MIN_SECONDS, MAX_SECONDS]` при sglang, иначе прежние `[SCENE_MIN_SECONDS, SCENE_MAX_SECONDS]`; `references` проекта = теги сессии, каждый на последней версии (`library.get_card`), неизвестный → `library_card_not_found`, проект не создаётся.
  - Строка контекста модели: `scene duration: 3–15 s` (sglang) / `scene duration: 5–10 s` (MLX) сразу после строки `duration: …`.
  - `provider.shares_gpu(cfg: dict) -> bool | None` — явный `cfg["shares_gpu"]` (bool) побеждает; иначе `True` для `type == "llama-local"` и для `base_url` с хостом `localhost`/`127.0.0.1`/`::1`/`host.docker.internal`; иначе `None`.
  - `GET /api/providers` — у строки ключ `shares_gpu`.

- [ ] **Step 1: Написать падающие тесты**

`tests/test_chat_project.py`:
```python
"""Wave 1.5, spec §5.4: the chat of a project, chat-made projects on sglang's own duration range
with the session's tags pinned, the duration range in the model's context, and an honest
`shares_gpu` per provider."""
import json
from pathlib import Path

import pytest

from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import provider
from test_chat_web import _serve, fake_llama  # noqa: F401  (fixtures)

PNG = b"\x89PNG\r\n\x1a\n"


def _card(root):
    (root / "uploads").mkdir(exist_ok=True)
    (root / "uploads" / "f.png").write_bytes(PNG)
    return lib.create_card(root, tag="@amazon", kind="person", description="an armored amazon",
                           assets=[root / "uploads" / "f.png"])


def _session_with(srv, project, tags=None):
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    path = Path(srv.root) / "chat" / f"{sid}.json"
    session = json.loads(path.read_text(encoding="utf-8"))
    session.update({"project": project, "kind": project["kind"]})
    if tags is not None:
        session["tags"] = tags
    path.write_text(json.dumps(session), encoding="utf-8")
    return sid


def test_a_project_chat_starts_with_the_projects_tags(_serve, monkeypatch):  # noqa: F811
    monkeypatch.setenv("H3_ENGINE", "sglang")
    srv = _serve()
    _card(srv.root)
    proj = p.create_project(srv.root, "video", "Бой")
    proj.set_references([{"tag": "@amazon", "version": 1}])
    sid = srv.post_json("/api/chat", {"source": {"kind": "project", "id": proj.id}})["id"]
    session = srv.get_json(f"/api/chat/{sid}")
    assert (session["source"], session["tags"], session["kind"]) == (
        {"kind": "project", "id": proj.id}, ["@amazon"], "video")


def test_a_project_chat_for_a_missing_project_is_refused(_serve):  # noqa: F811
    srv = _serve()
    status, payload = srv.post_json_raw("/api/chat", {"source": {"kind": "project", "id": "nope"}})
    assert (status, payload["error"]["code"]) == (404, "project_not_found")
    assert list((Path(srv.root) / "chat").glob("*.json")) == []


def test_a_project_chat_tells_the_model_it_is_a_video(_serve, fake_llama, monkeypatch):  # noqa: F811
    monkeypatch.setenv("H3_ENGINE", "sglang")
    srv = _serve(providers_port=fake_llama.port)
    proj = p.create_project(srv.root, "video", "Бой")
    sid = srv.post_json("/api/chat", {"source": {"kind": "project", "id": proj.id}})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "ролик", "prompt": ""})
    system = fake_llama.requests[-1]["body"]["messages"][0]["content"]
    assert "\nkind: video\n" in system


def test_a_chat_made_video_takes_sglang_durations_and_pins_the_tags(_serve, monkeypatch):  # noqa: F811
    monkeypatch.setenv("H3_ENGINE", "sglang")
    srv = _serve()
    _card(srv.root)
    sid = _session_with(srv, {"kind": "video", "scenes": [
        {"prompt": "@amazon walks", "duration": 12}, {"prompt": "@amazon runs", "duration": 3}]},
        tags=["@amazon"])
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    loaded = p.load_project(Path(srv.root) / "projects" / pid)
    assert ([s["duration"] for s in loaded.scenes], loaded.references) == (
        [12.0, 3.0], [{"tag": "@amazon", "version": 1}])


def test_a_chat_made_video_with_an_unknown_tag_is_refused(_serve, monkeypatch):  # noqa: F811
    monkeypatch.setenv("H3_ENGINE", "sglang")
    srv = _serve()
    sid = _session_with(srv, {"kind": "video", "scenes": [{"prompt": "@ghost", "duration": 5}]},
                        tags=["@ghost"])
    status, payload = srv.post_json_raw("/api/projects", {"session_id": sid})
    assert (status, payload["error"]["code"]) == (404, "library_card_not_found")
    assert list((Path(srv.root) / "projects").glob("*")) == []


def test_mlx_keeps_five_to_ten(_serve):  # noqa: F811
    srv = _serve()
    sid = _session_with(srv, {"kind": "video", "scenes": [{"prompt": "a cat", "duration": 12}]})
    status, payload = srv.post_json_raw("/api/projects", {"session_id": sid})
    assert (status, payload["error"]["code"]) == (400, "args_invalid")


@pytest.mark.parametrize("engine, line", [("sglang", "scene duration: 3–15 s"),
                                          ("mlx", "scene duration: 5–10 s")])
def test_the_model_is_told_the_scene_duration_range(_serve, fake_llama, monkeypatch, engine, line):  # noqa: F811
    monkeypatch.setenv("H3_ENGINE", engine)
    srv = _serve(providers_port=fake_llama.port)
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "ролик", "prompt": ""})
    system = fake_llama.requests[-1]["body"]["messages"][0]["content"]
    assert "\nduration: 10 s\n" + line + "\n" in system


@pytest.mark.parametrize("cfg, expected", [
    ({"type": "llama-local"}, True),
    ({"type": "openai", "base_url": "http://127.0.0.1:8000"}, True),
    ({"type": "openai", "base_url": "http://localhost:8000/v1"}, True),
    ({"type": "openai", "base_url": "http://[::1]:8000"}, True),
    ({"type": "openai", "base_url": "http://host.docker.internal:30000"}, True),
    ({"type": "openai", "base_url": "https://caila.io/api"}, None),
    ({"type": "openai", "base_url": "http://192.168.100.50:8000", "shares_gpu": True}, True),
    ({"type": "llama-local", "shares_gpu": False}, False),
])
def test_shares_gpu(cfg, expected):
    assert provider.shares_gpu(cfg) is expected
```
Помощники `srv.get_json`/`srv.post_json_raw`/`srv.root` есть у фикстуры `_serve` из `tests/test_chat_web.py` (сверено проверкой плана). Путь сессии `<root>/chat/<sid>.json` сверить с `_chat_path` до запуска. Строку `"\nduration: 10 s\n"` сверить с тем, как `_locked_turn` печатает длительность по умолчанию (`DEFAULT_CHAT_DURATION = 10`, формат `{duration:g}`); порядок строк `kind:` и `scene duration:` в `## Context` задаёт реализация — тест проверяет каждую целой строкой. Если `_locked_turn` на sglang с `llama-local` в тестовом `_serve` требует диспетчер — первым прогоном выяснить и подставить фейк из `tests/_fake_dispatcher.py`, не выключать sglang в тесте.

Тест `GET /api/providers` (провайдеры фикстуры — `providers.json`, который пишет `_serve(providers_port=…)`; если запись одна — дописать в тест вторую через тот же файл):
```python
def test_providers_carry_shares_gpu(_serve, fake_llama):  # noqa: F811
    srv = _serve(providers_port=fake_llama.port)
    roster = provider.load_providers(srv.root)["providers"]
    body = srv.get_json("/api/providers")
    assert [(row["name"], row["shares_gpu"]) for row in body["providers"]] == [
        (name, provider.shares_gpu(cfg)) for name, cfg in roster.items()]
    assert [row["shares_gpu"] for row in body["providers"]] == [True]   # fake llama on 127.0.0.1
```
(Последнюю строку поправить под фактический состав `providers.json` фикстуры литералом, не вычислением.)

- [ ] **Step 2: Увидеть красным** — `args_invalid` для `source.kind="project"`, `[12.0, 3.0]` отказано по 5–10, нет строки в системном сообщении, нет `provider.shares_gpu`.
- [ ] **Step 3: Реализация** — по Interfaces. Длительность в `_create_project`: `low, high = (sglang_args.MIN_SECONDS, sglang_args.MAX_SECONDS) if engine.is_sglang() else (SCENE_MIN_SECONDS, SCENE_MAX_SECONDS)`; текст отказа прежний с подставленными границами. Теги: до `create_project` — `pinned = [{"tag": c["tag"], "version": c["version"]} for c in (self._library_call(library_module.get_card, outdir, t) for t in session.get("tags") or [])]`, после — `proj.references = pinned` до `proj.save()` (проверить, что `save()` пишет `references`). Хост для `shares_gpu` — `urllib.parse.urlsplit(base_url).hostname`. Если `load_providers` отбрасывает незнакомые ключи записи — разрешить `shares_gpu` там же (bool), иначе `None`.
- [ ] **Step 4: Зелёный.**
- [ ] **Step 5: Мутация** — вернуть `SCENE_MIN/MAX` в `_create_project` → красный второго теста; убрать строку контекста → красный параметризации; убрать ветку `localhost` → красный `test_shares_gpu[...localhost...]`.
- [ ] **Step 6: Полный прогон** (особенно `tests/test_web_projects.py` — C3 про 5–10 на MLX должен остаться зелёным).
- [ ] **Step 7: Коммит** — `feat(chat): чат проекта, длительности чат-проекта по движку и его теги, честный shares_gpu у провайдера`.

---

## Task 5: Отчёт апскейла в выдаче проекта

**Files:**
- Modify: `h3_48gb/web.py:2234` (`_project_payload`)
- Test: `tests/test_upscale_report.py` (новый)

**Interfaces:**
- Produces: в каждом ответе с проектом `project.upscale_report` = `{"status", "strength", "motion", "attempted": [idx…], "error"}` из `<project>/upscale/report.json` (пишет `h3_48gb/engines/ltx.py:208`, `_write_report`), где отсутствующие ключи → `None`, `attempted` — `[part["idx"] for part in report["parts"]]` (часть попадает в отчёт **до** апскейла, а `done` пишется и после обрыва на пересъёмке, `ltx.py:242-243, 270` — это начатые, не готовые части; готовые UI считает по `ltx_path`); нет файла или он не читается как JSON-объект → `None`.

- [ ] **Step 1: Написать падающие тесты**

`tests/test_upscale_report.py`:
```python
"""Wave 1.5, spec §5.7: the project payload says what the LTX upscale did -- strength, motion,
parts, error -- from the report the upscale job writes."""
import json

import pytest

from h3_48gb import project as p
from h3_48gb import queue as q
from test_web import _call, _serve


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    server = _serve(q.layout(outdir / "queue")["root"], outdir)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def _report(proj, body):
    path = proj.path.parent / "upscale" / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")


def test_a_done_report_is_summarised(live):
    proj = p.create_project(live.outdir, "video", "Бой")
    _report(proj, {"attempt": "a1", "started_at": 1.0, "motion": 0.12, "strength": 0.6,
                   "parts": [{"idx": 0, "clip": "x", "comfy_s": 3}, {"idx": 1, "clip": "y"}],
                   "status": "done", "finished_at": 9.0})
    status, body = _call(live, "GET", f"/api/projects/{proj.id}")
    assert (status, body["project"]["upscale_report"]) == (200, {
        "status": "done", "strength": 0.6, "motion": 0.12, "attempted": [0, 1], "error": None})


def test_a_failed_report_carries_its_error(live):
    proj = p.create_project(live.outdir, "video", "Бой")
    _report(proj, {"attempt": "a1", "parts": [], "status": "failed", "error": "ComfyUI 500"})
    assert _call(live, "GET", f"/api/projects/{proj.id}")[1]["project"]["upscale_report"] == {
        "status": "failed", "strength": None, "motion": None, "attempted": [], "error": "ComfyUI 500"}


@pytest.mark.parametrize("body", [None, "{broken", "[1, 2]"])
def test_no_or_unreadable_report_is_null(live, body):
    proj = p.create_project(live.outdir, "video", "Бой")
    if body is not None:
        _report(proj, body)
    assert _call(live, "GET", f"/api/projects/{proj.id}")[1]["project"]["upscale_report"] is None
```
- [ ] **Step 2: Увидеть красным** — `KeyError: 'upscale_report'`.
- [ ] **Step 3: Реализация** — `_upscale_report(proj) -> dict | None` в `web.py` (try `json.loads` / `OSError, ValueError` → `None`; не-dict → `None`), вызов в `_project_payload`: `result["upscale_report"] = _upscale_report(proj)`.
- [ ] **Step 4: Зелёный.**
- [ ] **Step 5: Мутация** — `"attempted": report.get("parts")` (без извлечения `idx`) → красный первого теста.
- [ ] **Step 6: Полный прогон** (тесты, сравнивающие весь `project`-payload целиком, получат новый ключ — дописать `"upscale_report": None` в их ожидание и перечислить такие тесты в отчёте).
- [ ] **Step 7: Коммит** — `feat(projects): upscale_report (сила, движение, части, ошибка) в выдаче проекта`.

---

## Task 6: UI-основа: общий харнесс, «+ Новый проект», «Новый через диалог», CSS неоформленных блоков

**Files:**
- Create: `tests/_ui_harness.mjs` (вынести из `tests/_panel_ui_check.mjs` строки 10–90 без изменения поведения + расширения ниже)
- Modify: `tests/_panel_ui_check.mjs` (импорт харнесса вместо своих копий)
- Create: `tests/_ui_gaps_check.mjs`, `tests/test_webui_gaps.py`
- Modify: `h3_48gb/webui/index.html:390-398` (шапка зоны «Проекты»), `h3_48gb/webui/app.js` (обработчики, `defaultProjectTitle`), `h3_48gb/webui/style.css`

**Interfaces:**
- Produces (харнесс, `tests/_ui_harness.mjs`): `routes`, `calls` (`{method, url, body, headers}`; не-JSON тело → `body: {raw: <имя файла или "blob">}`), `alerts`, `confirms`, `prompts`, `answers` (`{confirm: true, prompt: null}`), `queryAll`/`queryOne` (точный селектор → фейковые элементы; пусто/`null` по умолчанию), `getElementById`, `fire(type, target)`, `clickable(props)`, `ok`, `err`, `sleep`, `countCalls`, `PROJECT(over)`, `SGLANG`, `start(appUrl, extra) -> app` (возвращает импортированный модуль; маршруты по умолчанию ставятся **только для ключей, которых ещё нет** в `routes`, затем `Object.assign(routes, extra)` — так сценарий может заранее задать задержанный `GET /api/state`). Маршрут fetch может быть функцией, возвращающей **промис** ответа (для задержек).
- Produces (`app.js`): `export function defaultProjectTitle(date: Date) -> string` — `"Ролик ДД.ММ ЧЧ:ММ"` по локальному времени; `export function newVideoRequest(title: string, now: Date) -> {kind: "video", title}`.
- Produces (`index.html`, точная разметка шапки зоны «Проекты», решение координатора — «+ Новый проект»):
  ```html
  <div class="panel-head">
    <span class="num" aria-hidden="true">3</span>
    <h2>Проекты</h2>
    <div class="spacer"></div>
    <span class="eyebrow" id="projects-sum"></span>
    <button class="ghost" id="project-new-chat" type="button">Новый через диалог</button>
    <button class="inverse" id="project-new-video" type="button">+ Новый проект</button>
  </div>
  ```
  `#projects-empty`: «Проектов пока нет — «+ Новый проект»».

- [ ] **Step 1: Рефактор харнесса без смены поведения.** Вынести код, перевести `_panel_ui_check.mjs` на импорт, прогнать `env -u NODE_OPTIONS $PY -m pytest -q -p no:cacheprovider tests/test_webui_panel.py` — зелёный до и после (это не TDD-шаг, а перенос; красного тут не бывает). Ключевые места харнесса:
```js
export const queryAll = {};
export const queryOne = {};
globalThis.document = {
  body: makeEl("body"), documentElement: makeEl("html"), getElementById,
  querySelectorAll: (sel) => (Object.hasOwn(queryAll, sel) ? queryAll[sel] : []),
  querySelector: (sel) => (Object.hasOwn(queryOne, sel) ? queryOne[sel] : null),
  createElement: () => makeEl("new"),
  addEventListener(type, fn) { (docListeners[type] ||= []).push(fn); }, removeEventListener() {},
};
export const answers = { confirm: true, prompt: null };
export const prompts = [];
globalThis.confirm = (m) => { confirms.push(m); return answers.confirm; };
globalThis.prompt = (m) => { prompts.push(m); return answers.prompt; };
globalThis.window = { confirm: globalThis.confirm, alert: globalThis.alert, prompt: globalThis.prompt,
  location: { hash: "", host: "x" }, addEventListener() {}, removeEventListener() {}, scrollTo() {} };
globalThis.fetch = async (url, opts) => {
  const method = (opts && opts.method) || "GET";
  const key = `${method} ${url}`;
  const raw = opts && opts.body;
  calls.push({ method, url, headers: (opts && opts.headers) || null,
               body: typeof raw === "string" ? JSON.parse(raw) : (raw ? { raw: raw.name || "blob" } : null) });
  let entry = routes[key];
  if (Array.isArray(entry)) entry = entry.length > 1 ? entry.shift() : entry[0];
  if (typeof entry === "function") entry = await entry();
  if (!entry) entry = { status: 404, body: { error: { code: "no_route", message: key } } };
  return { ok: entry.status >= 200 && entry.status < 300, status: entry.status, json: async () => entry.body };
};
export const clickable = (props) => ({ ...props, closest(sel) { return props.match(sel) ? this : null; } });
```
(`match` в каждом сценарии — только `(s) => s === "…"`.)

- [ ] **Step 2: Написать падающие тесты**

`tests/test_webui_gaps.py` (начало файла, дальше его дополняют задачи 7–12):
```python
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
```
`tests/_ui_gaps_check.mjs`:
```js
// Wave 1.5 DOM wiring scenarios; usage: node _ui_gaps_check.mjs <appUrl> <scenario>
import { routes, calls, prompts, answers, getElementById, start, ok, PROJECT, sleep } from "./_ui_harness.mjs";

const [, , appUrl, scenario] = process.argv;
const fail = (m) => { process.stderr.write(`${m}\n`); process.exit(1); };
const posts = () => calls.filter((c) => c.method === "POST").map((c) => [c.url, c.body]);

const SCENARIOS = {
  async new_video() {
    answers.prompt = "Бой";
    await start(appUrl, { "POST /api/projects": ok({ ok: true, id: "p9", project: {} }),
      "GET /api/projects/p9": ok(PROJECT({ id: "p9", title: "Бой", stages: { script: "draft",
        scenes: "draft", upscale: "draft", assembly: "draft" } })) });
    getElementById("project-new-video").__listeners.click[0]();
    await sleep(80);
    return { prompts, posts: posts(), opened: getElementById("project-modal").hidden === false,
             title: getElementById("project-title").textContent };
  },
  async new_video_cancel() {
    answers.prompt = null;
    await start(appUrl, {});
    getElementById("project-new-video").__listeners.click[0]();
    await sleep(80);
    return { posts: posts() };
  },
  async new_chat() {
    await start(appUrl, { "POST /api/chat": ok({ ok: true, id: "c1" }) });
    getElementById("project-new-chat").__listeners.click[0]();
    await sleep(80);
    return { posts: posts() };
  },
};

const run = SCENARIOS[scenario];
if (!run) fail(`unknown scenario ${scenario}`);
run().then((out) => { process.stdout.write(JSON.stringify(out)); process.exit(0); },
           (e) => fail(`${e && e.stack || e}`));
```
- [ ] **Step 3: Увидеть красным** — `test_every_class…`: `assert ['lib-card', 'muted', 'project-refs', 'project-settings', 'route-upscale', 'tag-warnings', 'upscale-status', …] == []` (фактический список — в отчёт); `test_styled_classes_ignores_descendant_rules` — `NameError` до появления помощника (это тест самого помощника); остальные — нет функций/кнопок, сценарии падают `Cannot read properties of undefined (reading '0')`.
- [ ] **Step 4: Реализация.** `index.html` — две кнопки; `app.js` — две функции наверху, обработчики:
```js
  $("project-new-video").addEventListener("click", async () => {
    const title = window.prompt("Название проекта (пусто — по дате):", "");
    if (title === null) return;
    try {
      const created = await api("POST", "/api/projects", newVideoRequest(title, new Date()));
      await poll();
      await openProjectModal(created.id);
    } catch (error) { showError(error.payload || { error: { message: "сервер не ответил" } }); }
  });
  $("project-new-chat").addEventListener("click", () =>
    openChatModal({ kind: "new" }, { prompt: "", mode: "", image: "", endImage: "", duration: 10 }));
```
`style.css` — правила для всех классов из `missing` на токенах, в духе `.proj-stage` (карточка: `background: var(--card); border: 1px solid var(--hairline); border-radius: …; padding: …`; `.muted { color: var(--ink-3) }` — имя токена приглушённого текста взять из `:root`; `.tag-warnings` — `background: var(--warn-soft); color: var(--warn-ink)`), `.lib-card img` — `width: 96px; height: 96px; object-fit: cover`; в `@media (max-width: 420px)`: `#library form, .project-settings, .project-refs { display: grid; grid-template-columns: minmax(0, 1fr); }`, `.lib-card input, .project-settings textarea { width: 100%; }`.
- [ ] **Step 5: Зелёный.**
- [ ] **Step 6: Мутация** — удалить правило `.lib-card { … }`, **оставив** `.lib-card img` и `.lib-card input` → красный `test_every_class…` с `['lib-card']` (это и есть проверка, что правила-потомки не засчитываются); в `newVideoRequest` убрать `.trim()` → красный `test_default_project_title_and_request`; переставить кнопки в `index.html` → красный теста шапки.
- [ ] **Step 7: Полный прогон; коммит** — `feat(webui): «+ Новый проект» и «Новый через диалог» в зоне проектов, общий node-харнесс, CSS референсов и настроек проекта`.

---

## Task 7: Редактор сцен видеопроекта — ядро (промпт, длительность с сеткой, fresh_start, сид, шаги, порядок, сохранение)

**Files:**
- Modify: `h3_48gb/webui/app.js` — новые чистые функции наверху; `projectScriptStageHtml` (`app.js:3035`) показывает редактор для видео при `stages.scenes === "draft"` и `stages.script ∈ {draft, awaiting_approval}`; обработчики; состояние `sceneDraft`, `sceneDraftDirty`; `approve-script` сохраняет черновик первым
- Modify: `h3_48gb/webui/style.css`; `tests/_ui_gaps_check.mjs`, `tests/test_webui_gaps.py`

**Interfaces:**
- Consumes: Task 0 после фикс-раунда (`seed`, `steps`, `refs`, `project.seed` — только sglang), `PUT /api/projects/<id>/scenes`.
- Produces (все `export function` в `app.js`):
  - `sceneBounds(engine) -> {min, max}` — `{min: 3, max: 15}` на `"sglang"`, иначе `{min: 5, max: 10}`.
  - `sglangGridSeconds(duration, chained) -> number` — зеркало `web._snap_video_scenes_sglang` для одной сцены (24 к/с, `17n+5` запрошенных кадров, у сцепленной поставка на кадр меньше, пределы 73..345 запрошенных).
  - `gridHint(seconds) -> string` — `"на сетке: 5,17 с"` (`toFixed(2)`, хвостовые нули и запятая убираются: `8` → `"на сетке: 8 с"`).
  - `draftFromScenes(scenes) -> Draft[]`, где `Draft = {prompt, duration, fresh_start, seed, steps, start_image, refs}` (`null` для отсутствующих `seed/steps/start_image`, `[]` для `refs`, `false` для `fresh_start`).
  - `addScene(draft)`, `removeScene(draft, idx)`, `moveScene(draft, idx, delta)` — новые массивы; новая сцена `{prompt: "", duration: <последней или 8>, …}` (решение координатора); при перестановке **и удалении** `start_image` остаётся у позиции 0, у сцены, ставшей нулевой, `fresh_start = false`.
  - `scenesClientError(draft, engine) -> string | null` — `"Сцена #N: пустой промпт"`, `"Сцена #N: длительность 3–15 с"` (границы `sceneBounds`), первая найденная.
  - `scenesPayload(draft, engine) -> {scenes: […]}` — `prompt`, `duration` всегда; `fresh_start` у `idx > 0` всегда (bool); `start_image` только у 0 и если задан; **только при `engine === "sglang"`**: `seed` если не `null`, `steps` если не `null`, `refs` если непуст (задача 8). На MLX эти три поля не уходят никогда — Task 0 их отклоняет.
  - `randomSeed(rand = Math.random) -> number` — `Math.floor(rand() * 2 ** 31)`.
  - `seedPlaceholder(projectSeed) -> string` — `"по проекту: 305"` / `"по умолчанию: 42"`.
  - `sceneEditorHtml(draft, ctx) -> string`, `ctx = {id, engine, projectSeed, dirty}` (задача 8 добавит `pinned`, `outdir`); поля несут `data-scene-field="<поле>"` и `data-idx`, кнопки — `button[data-act]` со значениями `scene-up`, `scene-down`, `scene-del`, `scene-seed-random`, `scene-add`, `scenes-save`. Сид и шаги — **только на sglang**; подсказка сетки — `<span class="hint grid-hint" data-idx="N">`, только на sglang.
- DOM-половина:
  - `syncDraftFromDom()` читает `document.querySelectorAll("#project-body [data-scene-field]")` (селектор ровно такой). Вызывается **первой строкой `renderProjectModal`** (любая перерисовка — после галочки референса, `i2v_prefix`, загрузки кадра, «сохранено ✓» — сначала снимает поля в черновик), а также в начале обработчиков `scene-*`, `scenes-save`, `approve-script`.
  - `sceneDraft` сбрасывается из сервера при `openProjectModal` и после успешного `PUT`.
  - Обработчик `input` на `[data-scene-field="duration"]` (через `event.target.closest('[data-scene-field="duration"]')`) обновляет `textContent` у `document.querySelector('#project-body .grid-hint[data-idx="N"]')` — `gridHint(sglangGridSeconds(value, chained))`, без перерисовки модалки.
  - «закрыть» модалки при `sceneDraftDirty` (после `syncDraftFromDom`) спрашивает `confirm("Закрыть без сохранения сценария?")`; «нет» — модалка остаётся.

- [ ] **Step 1: Написать падающие тесты** (дописать в `tests/test_webui_gaps.py`)
```python
from h3_48gb import web

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
               "projectSeed: null, dirty: false})")
    assert html == (
        '<div class="scene-editor" data-id="p1">'
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
```
Точную разметку `sceneEditorHtml` исполнитель **может** поменять (порядок атрибутов, классы), но тогда тесты обновляются на новую разметку целиком, и в отчёте — почему; правила, которые пинят тесты (экранирование промпта, `min/max` из `sceneBounds`, подсказка сетки с `data-idx`, `placeholder` сида и шагов, `disabled` у крайних стрелок, нет «Удалить» у единственной сцены, нет `fresh_start` у сцены 0, нет сида/шагов/подсказки на MLX), — обязательны. Задача 8 расширяет эту разметку (кадр сцены 0, `refs`) и обновляет оба ожидания целиком.

```python
@_needs_node
def test_editor_html_on_mlx():
    html = _js("app.sceneEditorHtml([{prompt: 'a cat', duration: 8, fresh_start: false, seed: null, "
               "steps: null, start_image: null, refs: []}, {prompt: 'a dog', duration: 6, "
               "fresh_start: true, seed: null, steps: null, start_image: null, refs: []}], "
               "{id: 'p1', engine: 'mlx', projectSeed: null, dirty: true})")
    assert html == (
        '<div class="scene-editor" data-id="p1">'
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
```

Сценарии в `tests/_ui_gaps_check.mjs` — все кодом (в импорт харнесса добавить `queryAll`, `queryOne`, `fire`, `clickable`, `answers`, `confirms`, `calls`):
```js
const DRAFT_PROJECT = PROJECT({ stages: { script: "awaiting_approval", scenes: "draft",
  upscale: "draft", assembly: "draft" }, scenes: [
  { idx: 0, prompt: "@a walks", duration: 8, status: "pending", job_id: null, clip_path: null,
    keyframe_path: null, start_image: "@arena" },
  { idx: 1, prompt: "@a runs", duration: 5, status: "pending", job_id: null, clip_path: null,
    keyframe_path: null, fresh_start: true }], references: [{ tag: "@a", version: 1 }] });
const FIELDS = "#project-body [data-scene-field]";
const field = (sceneField, idx, value) => ({ dataset: { sceneField, idx: String(idx) }, value });
const open = async () => {
  fire("click", clickable({ dataset: { act: "open-project", id: "p1" }, match: (s) => s === "button[data-act]" }));
  await sleep(80);
};
const act = async (name, extra = {}) => {
  fire("click", clickable({ dataset: { act: name, id: "p1", ...extra }, match: (s) => s === "button[data-act]" }));
  await sleep(80);
};
const puts = () => calls.filter((c) => c.method === "PUT").map((c) => [c.url, c.body]);
const writes = () => calls.filter((c) => c.method !== "GET").map((c) => [c.method, c.url]);
const draftRoutes = (extra = {}) => ({ "GET /api/projects/p1": ok(DRAFT_PROJECT),
  "PUT /api/projects/p1/scenes": ok({ ok: true, project: DRAFT_PROJECT.project }), ...extra });

// SCENARIOS (в объекте SCENARIOS):
async editor_dom_edits_survive_add() {
  await start(appUrl, draftRoutes());
  await open();
  queryAll[FIELDS] = [field("prompt", 0, "@a jumps"), field("seed", 1, "305")];
  await act("scene-add");
  queryAll[FIELDS] = [field("prompt", 2, "@a rests")];
  await act("scenes-save");
  return { puts: puts() };
},
async editor_edits_survive_ref_pin() {
  await start(appUrl, draftRoutes({ "PUT /api/projects/p1/references": ok({ ok: true, references: [] }) }));
  await open();
  queryAll[FIELDS] = [field("prompt", 0, "@a jumps")];
  fire("change", { checked: true, dataset: { tag: "@a", id: "p1" }, closest: () => null,
                   classList: { contains: (c) => c === "ref-pin" } });
  await sleep(120);
  queryAll[FIELDS] = [];          // the modal was redrawn: the fields now hold whatever the draft had
  await act("scenes-save");
  return { puts: puts().filter(([url]) => url === "/api/projects/p1/scenes") };
},
async editor_approve_saves_first() {
  await start(appUrl, draftRoutes({ "POST /api/projects/p1/approve/script": ok({ ok: true }) }));
  await open();
  queryAll[FIELDS] = [field("prompt", 0, "@a jumps")];
  await act("approve-script");
  return { writes: writes() };
},
async editor_approve_stops_on_save_error() {
  await start(appUrl, draftRoutes({ "PUT /api/projects/p1/scenes": err(400, "args_invalid", "плохая сцена"),
    "POST /api/projects/p1/approve/script": ok({ ok: true }) }));
  await open();
  queryAll[FIELDS] = [field("prompt", 0, "@a jumps")];
  await act("approve-script");
  return { writes: writes(), errorHidden: getElementById("project-err").hidden };
},
async editor_client_error() {
  await start(appUrl, draftRoutes());
  await open();
  await act("scene-add");
  await act("scenes-save");
  return { puts: puts(), error: getElementById("project-err").innerHTML };
},
async editor_grid_hint_live() {
  await start(appUrl, draftRoutes());
  await open();
  const hint = { textContent: "на сетке: 8 с" };
  queryOne['#project-body .grid-hint[data-idx="0"]'] = hint;
  const input = { value: "4", dataset: { sceneField: "duration", idx: "0" },
    closest(sel) { return sel === '[data-scene-field="duration"]' ? this : null; } };
  fire("input", input);
  return { hint: hint.textContent };
},
async editor_close_unsaved() {
  await start(appUrl, draftRoutes());
  await open();
  queryAll[FIELDS] = [field("prompt", 0, "@a jumps")];
  answers.confirm = false;
  getElementById("project-close").__listeners.click[0]();
  return { confirms, hidden: getElementById("project-modal").hidden };
},
```
Ожидания (в `test_webui_gaps.py`, каждый — отдельный тест с `_gaps(...) ==`):
```python
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
    "editor_close_unsaved": {"confirms": ["Закрыть без сохранения сценария?"], "hidden": False},
}


@_needs_node
@pytest.mark.parametrize("scenario", sorted(EDITOR_EXPECTED))
def test_editor_wiring(scenario):
    assert _gaps(scenario) == EDITOR_EXPECTED[scenario]
```
(Текст ошибки клиента — из `errorText` (`app.js:1506`): для `{error: {message}}` без кода он даёт заголовок «Запрос не прошёл» и `pre` с сообщением — сверено запуском 07.10.)

- [ ] **Step 2: Увидеть красным** — `app.sglangGridSeconds is not a function` и т.д.; сценарии: `puts: []`.
- [ ] **Step 3: Реализация** — функции по Interfaces; в DOM-половине (первая строка `renderProjectModal` — `syncDraftFromDom();`):
```js
  let sceneDraft = null;        // Draft[] of the open video project, or null
  let sceneDraftDirty = false;
  function syncDraftFromDom() {
    if (!sceneDraft) return;
    document.querySelectorAll("#project-body [data-scene-field]").forEach((el) => {
      const scene = sceneDraft[Number(el.dataset.idx)];
      if (!scene) return;
      const field = el.dataset.sceneField;
      const before = JSON.stringify(scene[field]);
      if (field === "fresh_start") scene.fresh_start = Boolean(el.checked);
      else if (field === "duration") scene.duration = Number(String(el.value).replace(",", "."));
      else if (field === "seed" || field === "steps") scene[field] = el.value === "" ? null : Number(el.value);
      else scene[field] = el.value;
      if (JSON.stringify(scene[field]) !== before) sceneDraftDirty = true;
    });
  }
```
`openProjectModal` и успешный `PUT` → `sceneDraft = draftFromScenes(proj.scenes.length ? proj.scenes : [])`, при пустом — `addScene([])`, `sceneDraftDirty = false`. Обработчики `scene-*`/`scenes-save` — в главном делегированном `click` (`app.js:4887`, по `button.dataset.act`), каждый начинается с `syncDraftFromDom()`. `approve-script`: `syncDraftFromDom(); if (sceneDraftDirty) { await saveScenes(); if (failed) return; }`. `projectScriptStageHtml` для видео в редактируемом состоянии добавляет `sceneEditorHtml(sceneDraft, {...})` и пометку `<span class="dirty-note">не сохранено</span>` при `sceneDraftDirty`.
- [ ] **Step 4: Зелёный.** CSS для `scene-editor`, `scene-edit`, `scene-edit-head`, `scene-edit-row`, `scene-editor-acts`, `dirty-note`, `hint` (если нет), `fresh-start-toggle` (если нет) — и `CLASS_SOURCES` дополняется вызовами `sceneEditorHtml` (sglang и MLX); в `HOOK_CLASSES` — `scene-edit-prompt` (поле ввода, вид от `.inp`), `grid-hint` (адрес для обновления, вид от `.hint`).
- [ ] **Step 5: Мутация** — (а) в `moveScene` не переносить `start_image` → красный `test_move_scene_keeps_start_image_at_position_0`; (б) убрать `syncDraftFromDom()` из обработчика `scene-add` → красный `editor_dom_edits_survive_add`; (в) убрать `syncDraftFromDom()` из `renderProjectModal` → красный `editor_edits_survive_ref_pin`; (г) убрать сохранение перед утверждением → красный `editor_approve_saves_first`; (д) в `sglangGridSeconds` взять остаток `5` и для сцепленной → красный `test_grid_hint_matches_the_server_snap`; (е) слать `seed` на MLX → красный `test_scenes_payload_sends_only_what_is_set`; (ж) в `removeScene` не снимать `fresh_start` → красный `test_move_scene…` (часть про `removeScene`).
- [ ] **Step 6: Полный прогон; коммит** — `feat(webui): редактор сцен видеопроекта — промпт целиком, длительность с сеткой, сид, шаги, порядок, сохранение`.

---

## Task 8: Редактор сцен — референсы, `@`-подсказка, стартовый кадр сцены 0, вставка JSON

**Files:**
- Modify: `h3_48gb/webui/app.js`, `h3_48gb/webui/index.html` (скрытый `<input type="file" id="scene0-file" accept="image/png,image/jpeg" hidden>` внутри `#project-modal`), `style.css`, `tests/_ui_gaps_check.mjs`, `tests/test_webui_gaps.py`

**Interfaces:**
- Consumes: `tagSuggestions` (`app.js:223`), `sceneTagIssues` (`app.js:179`), `POST /api/uploads` (тело — файл, заголовок `X-Filename`), Task 0 `refs` (только sglang, дубли убирает сервер).
- Produces:
  - **`sceneEditorHtml(draft, ctx)` — `ctx` расширяется: `{id, engine, projectSeed, dirty, pinned, outdir}`**, где `pinned` — карточки подключённых тегов (`[{tag, kind, assets}]` в подключённой версии), `outdir` — `state.outdir`. На sglang у каждой сцены под промптом — пустой слот подсказки `<div class="tag-hint-slot" data-idx="N"></div>` и `sceneRefsHtml`; у сцены 0 (любой движок) — `startImageFieldHtml`. **Оба ожидания задачи 7 (`test_editor_html_for_one_scene_on_sglang`, `test_editor_html_on_mlx`) обновляются целиком** под новую разметку, причина — в отчёте.
  - `insertTagAt(text, caret, tag) -> {text, caret}` — заменяет недописанный `@…` перед кареткой на `tag`; после тега пробел добавляется, только если следующий символ не пробельный; каретка — после этого пробела.
  - `tagHintHtml(text, caret, cards) -> string` — `""` или `<div class="tag-hint">` с кнопками `<button type="button" class="tag-pick" data-act="tag-pick" data-tag="@x">@x</button>`.
  - `startImageFieldHtml(current, pinnedCards, outdir) -> string` — точная разметка в тесте ниже: `select[data-scene-field="start_image"][data-idx="0"]` с вариантами `""` («без кадра»), каждый не-voice тег (`"@arena — кадр карточки"`), текущий путь (подпись — имя файла); кнопка `data-act="scene0-upload"`; миниатюра `<img class="start-thumb" …>` выбранного.
  - `sceneRefsHtml(scene, idx, pinnedTags) -> string` — чекбоксы `data-scene-field="refs"` `data-idx` `data-tag`, только sglang, с подсказкой `<span class="hint">их картинки идут первыми: &lt;Picture 1…&gt;</span>`. `syncDraftFromDom` для поля `refs`: у каждой сцены, у которой в DOM есть хоть один такой чекбокс, `refs` = отмеченные теги в порядке DOM.
  - `sceneTagIssues(text, pinnedTags, {needsTag, refs = []})` — при непустом `refs` проблема `missing` не выдаётся (сцена с референсами без упоминания законна, `626db48d`); `projectTagWarningsHtml` передаёт `scene.refs`.
  - `parseScenarioJson(text) -> {body} | {error}` — список → `{body: {scenes: список}}`; объект со `scenes`-списком → `{body: {scenes, references?}}` (только эти два ключа); иначе `{error: 'Ожидается {"scenes": […]} или список сцен'}`; не JSON → `{error: "JSON не разобрался — проверьте запятые и кавычки"}`.
  - `scenarioReplaceConfirm(n) -> string` — `"Заменить 1 сцену сценария?"`, `"Заменить 2 сцены сценария?"`, `"Заменить 5 сцен сценария?"` (`plural`).
  - Блок `<details class="adv scenario-json">` с `<textarea class="inp scenario-json-text">` и кнопкой `data-act="scenario-json-load"`: при непустом сценарии `confirm(scenarioReplaceConfirm(n))` → `PUT …/scenes` с `body` как есть → черновик из ответа.
  - Обработчик `input` на `[data-scene-field="prompt"]` (sglang): подсветка проблем тегов (как у `.scenario-prompt`, `app.js:5111`) и `document.querySelector('#project-body .tag-hint-slot[data-idx="N"]').innerHTML = tagHintHtml(value, selectionStart, подключённые карточки)`. Клик `tag-pick` (`data-tag`, `data-idx`) правит поле `document.querySelector('#project-body [data-scene-field="prompt"][data-idx="N"]')` через `insertTagAt`, ставит каретку и помечает черновик изменённым.

- [ ] **Step 1: Тесты** (в `tests/test_webui_gaps.py`)
```python
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
```
Сценарии (`tests/_ui_gaps_check.mjs`; `DRAFT_PROJECT`, `FIELDS`, `field`, `open`, `act`, `puts`, `draftRoutes` — из задачи 7):
```js
async tag_hint_on_input() {
  const proj = PROJECT({ ...DRAFT_PROJECT.project, references: [{ tag: "@amazon", version: 1 }, { tag: "@arena", version: 1 }] });
  const card = (tag) => ({ tag, kind: "person", version: 1, latest_version: 1, description: "d",
                          assets: [`/o/library/${tag.slice(1)}/v1/01-x.png`], versions: [{ version: 1 }] });
  await start(appUrl, draftRoutes({ "GET /api/projects/p1": ok(proj),
    "GET /api/library": ok({ ok: true, cards: [card("@amazon"), card("@arena"), card("@bob")] }) }));
  await open();
  const slot = { innerHTML: "" };
  queryOne['#project-body .tag-hint-slot[data-idx="0"]'] = slot;
  fire("input", { value: "fight @a", selectionStart: 8, title: "",
    dataset: { sceneField: "prompt", idx: "0" }, classList: { toggle() {}, contains: () => false },
    closest(sel) { return sel === '[data-scene-field="prompt"]' ? this : null; } });
  return { slot: slot.innerHTML };
},
async tag_pick() {
  await start(appUrl, draftRoutes());
  await open();
  const prompt = { value: "fight @a", selectionStart: 8, dataset: { sceneField: "prompt", idx: "0" },
                   focus() {}, setSelectionRange() {} };
  queryOne['#project-body [data-scene-field="prompt"][data-idx="0"]'] = prompt;
  await act("tag-pick", { tag: "@amazon", idx: "0" });
  queryAll[FIELDS] = [prompt];
  await act("scenes-save");
  return { value: prompt.value, prompt0: puts()[0][1].scenes[0].prompt };
},
async scene0_upload() {
  await start(appUrl, draftRoutes({ "POST /api/uploads": ok({ ok: true, path: "/o/uploads/open.png" }) }));
  await open();
  const file = getElementById("scene0-file");
  file.files = [{ name: "open.png" }];
  file.__listeners.change[0]();
  await sleep(120);
  await act("scenes-save");
  const upload = calls.find((c) => c.url === "/api/uploads");
  return { upload: { url: upload.url, headers: upload.headers, body: upload.body },
           start_image: puts().at(-1)[1].scenes[0].start_image };
},
async json_load() {
  await start(appUrl, draftRoutes());
  await open();
  queryOne["#project-body .scenario-json-text"] = { value: '{"scenes": [{"prompt": "@a", "duration": 5}]}' };
  answers.confirm = true;
  await act("scenario-json-load");
  return { confirms, puts: puts() };
},
async json_load_bad() {
  await start(appUrl, draftRoutes());
  await open();
  queryOne["#project-body .scenario-json-text"] = { value: "42" };
  await act("scenario-json-load");
  return { puts: puts(), error: getElementById("project-err").innerHTML };
},
```
Ожидания:
```python
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
    "json_load_bad": {"puts": [], "error": "<b>Запрос не прошёл</b><pre>Ожидается {&quot;scenes&quot;: […]} "
                                          "или список сцен</pre>"},
}


@_needs_node
@pytest.mark.parametrize("scenario", sorted(EDITOR8_EXPECTED))
def test_editor_refs_and_json_wiring(scenario):
    assert _gaps(scenario) == EDITOR8_EXPECTED[scenario]
```
- [ ] **Step 2: Увидеть красным** — `insertTagAt is not a function`, сценарии — `TypeError` на пустых слушателях / `puts: []`.
- [ ] **Step 3: Реализация.** Загрузку файла (`fetch("/api/uploads", {method: "POST", body: file, headers: {...}})`) вынести из `addLibraryCard` (`app.js:2747`) в `uploadToServer(file) -> path` и использовать в обоих местах (задача 9 опирается на это). `#scene0-file` `change` → `uploadToServer` → `sceneDraft[0].start_image = path`, `sceneDraftDirty = true`, `renderProjectModal()`; клик `scene0-upload` → `$("scene0-file").click()`. `renderProjectModal` собирает `pinned` из `libraryCards` и `proj.references` (версия подключённая: `card.versions` из задачи 3, иначе текущая).
- [ ] **Step 4: Зелёный; CSS** (`tag-hint`, `tag-pick`, `tag-hint-slot`, `start-image`, `start-thumb`, `scene-refs`, `scene-refs-label`, `scenario-json`) + `CLASS_SOURCES` (`startImageFieldHtml`, `sceneRefsHtml`, `tagHintHtml`); в `HOOK_CLASSES` — `scenario-json-text` (вид от `.inp`), если правила у него не будет.
- [ ] **Step 5: Мутация** — в `insertTagAt` всегда добавлять пробел → красный (двойной пробел); в `scenesPayload` не слать `start_image` → красный `scene0_upload`; убрать `confirm` перед заменой → красный `json_load`; в `sceneTagIssues` игнорировать `refs` → красный `test_a_scene_with_refs…`; в обработчике `input` не заполнять слот → красный `tag_hint_on_input`.
- [ ] **Step 6: Полный прогон; коммит** — `feat(webui): редактор сцен — @-подсказка кнопками, референсы сцены, стартовый кадр сцены 0, вставка сценария JSON`.

---

## Task 8b: «Промпт для H3» — итоговый промпт и условия сцены до утверждения

Решение координатора: закрыть обход `fixed/offline_check_fixed.py` — владелец видит до «Утвердить», что уйдёт в sglang (`subject_definitions`, `<Subject N>`, `<Picture k>`, `i2v_prefix` у сцепленной).

**Files:**
- Modify: `h3_48gb/web.py` — `_scene_reference_errors` (`web.py:1009`): тело цикла выносится в `_scene_sglang_args(proj, scene, outdir) -> list[str]` (тот же путь: `build_ref2va(..., extra_refs=scene.refs)` → `scene_start_image` → `assemble._scene_generate_args_sglang` с заглушкой `keyframe.png` у сцепленной → `sglang_args.parse`), гейт зовёт его же; новый маршрут в `_route_get` **до** общего `if path.startswith("/api/projects/")` (`web.py:3444`)
- Modify: `h3_48gb/webui/app.js` — кнопка в `sceneEditorHtml`, `h3PromptHtml`, обработчик; `style.css`
- Test: `tests/test_scene_h3_prompt.py` (новый), `tests/test_webui_gaps.py`, `tests/_ui_gaps_check.mjs`

**Interfaces:**
- Produces:
  - `GET /api/projects/<id>/scenes/<idx>/h3-prompt` (только sglang; MLX → 400 `args_invalid` «Промпт для H3 есть только на sglang») → `{"ok": true, "idx", "prompt", "pictures": [{"label": "<Picture k>", "path"}], "audios": [путь…], "keyframe": {"kind": "start_image"|"previous_scene"|null, "path": путь|null}, "duration", "seed", "steps"}`. Сцена берётся из `project.json` (сохранённая), длительность — после `_snap_video_scenes_sglang([scene])[0]`; `prompt` = `args[1]`, `pictures` — значения `--ref` по порядку, `audios` — `--audio`, `seed`/`steps` — из argv (эффективные). Сцепленная сцена → `keyframe = {"kind": "previous_scene", "path": null}`; сцена 0 с кадром → `{"kind": "start_image", "path": <разрешённый путь>}`; без кадра → `{"kind": null, "path": null}`. Ошибка сборки (`LibraryError`/`SglangArgsError`) → тот же код и текст, что у гейта (400). Нет сцены → 404 `project_scene_not_found`.
  - `app.js`: `h3PromptHtml(answer, outdir) -> string`; кнопка `<button type="button" class="ghost" data-act="scene-h3-prompt" data-idx="N">Промпт для H3</button>` в шапке сцены редактора (только sglang, после стрелок и «Удалить»; **ожидание sglang-разметки редактора из задач 7/8 обновляется целиком**, MLX-ожидание не меняется); клик: `syncDraftFromDom()`, при `sceneDraftDirty` — сначала `PUT …/scenes` (отказ — стоп), потом `GET …/h3-prompt`, ответ кладётся в `h3Prompts[idx]` (сбрасывается при любой правке черновика) и рисуется под сценой.

- [ ] **Step 1: Тесты**

`tests/test_scene_h3_prompt.py`:
```python
"""Wave 1.5, spec §5.2.1: «Промпт для H3» shows, before approval, the prompt and the conditions
sglang will get for a saved scene -- built by the very path the gate and the submission use."""
import pytest

from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import queue as q
from test_web import _call, _serve

PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    outdir = tmp_path / "outdir"
    (outdir / "uploads").mkdir(parents=True)
    for name in ("face.png", "opening.png"):
        (outdir / "uploads" / name).write_bytes(PNG)
    lib.create_card(outdir, tag="@amazon", kind="person", description="an armored amazon",
                    assets=[outdir / "uploads" / "face.png"])
    lib.create_card(outdir, tag="@arena", kind="environment", description="a sand arena",
                    assets=[outdir / "uploads" / "opening.png"])
    server = _serve(q.layout(outdir / "queue")["root"], outdir)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def _project(live):
    proj = p.create_project(live.outdir, "video", "Бой")
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/scenes", {
        "scenes": [{"prompt": "@amazon fights", "duration": 8.0, "start_image": "@arena", "seed": 305},
                   {"prompt": "@amazon runs", "duration": 5.0, "refs": ["@arena"], "steps": 30}],
        "references": [{"tag": "@amazon"}, {"tag": "@arena"}]})
    assert status == 200, body
    assert _call(live, "PUT", f"/api/projects/{proj.id}/settings", {"i2v_prefix": "Continue."})[0] == 200
    return proj


def test_scene_0_shows_its_start_frame_and_its_own_seed(live):
    proj = _project(live)
    amazon = str(live.outdir / "library" / "amazon" / "v1" / "01-face.png")
    arena = str(live.outdir / "library" / "arena" / "v1" / "01-opening.png")
    status, body = _call(live, "GET", f"/api/projects/{proj.id}/scenes/0/h3-prompt")
    assert (status, body) == (200, {
        "ok": True, "idx": 0,
        "prompt": "subject_definitions:\n<Subject 1> is an armored amazon, appearance from "
                  "<Picture 1>.\n\n<Subject 1> fights",
        "pictures": [{"label": "<Picture 1>", "path": amazon}], "audios": [],
        "keyframe": {"kind": "start_image", "path": arena},
        "duration": 8.0, "seed": 305, "steps": 50})


def test_a_chained_scene_carries_the_prefix_and_its_refs_come_first(live):
    proj = _project(live)
    amazon = str(live.outdir / "library" / "amazon" / "v1" / "01-face.png")
    arena = str(live.outdir / "library" / "arena" / "v1" / "01-opening.png")
    status, body = _call(live, "GET", f"/api/projects/{proj.id}/scenes/1/h3-prompt")
    assert (status, body) == (200, {
        "ok": True, "idx": 1,
        "prompt": "Continue.\n\nsubject_definitions:\n<Subject 1> is an armored amazon, appearance "
                  "from <Picture 2>.\n\n<Subject 1> runs",
        "pictures": [{"label": "<Picture 1>", "path": arena}, {"label": "<Picture 2>", "path": amazon}],
        "audios": [], "keyframe": {"kind": "previous_scene", "path": None},
        "duration": 5.125, "seed": 42, "steps": 30})


def test_unknown_scene_and_mlx(live, monkeypatch):
    proj = _project(live)
    status, body = _call(live, "GET", f"/api/projects/{proj.id}/scenes/9/h3-prompt")
    assert (status, body["error"]["code"]) == (404, "project_scene_not_found")
    monkeypatch.setenv("H3_ENGINE", "mlx")
    status, body = _call(live, "GET", f"/api/projects/{proj.id}/scenes/0/h3-prompt")
    assert (status, body["error"]["code"], body["error"]["message"]) == (
        400, "args_invalid", "Промпт для H3 есть только на sglang")
```
Ожидаемые промпты выведены из `library.build_ref2va` после `626db48d` (у `refs` нет `<Subject N>`, их картинки — первыми; тег текста получает следующую `<Picture k>`) и из `_scene_generate_args_sglang` (`f"{i2v_prefix}\n\n{prompt}"` у сцепленной) — сверить с `tests/test_sglang_scene_refs.py`; если расходится — разбираться в причине, а не подгонять литерал. Если `engine.is_sglang()` читает окружение один раз при старте сервера и `monkeypatch.setenv` посреди теста не действует — MLX-часть вынести в отдельный тест со своим сервером без `H3_ENGINE`.

В `tests/test_webui_gaps.py`:
```python
@_needs_node
def test_h3_prompt_html():
    answer = ("{idx: 1, prompt: 'Continue.\\n\\n<Subject 1> runs <b>', pictures: [{label: '<Picture 1>', "
              "path: '/o/library/arena/v1/01-o.png'}], audios: [], keyframe: {kind: 'previous_scene', "
              "path: null}, duration: 5.125, seed: 42, steps: 30}")
    assert _js(f"app.h3PromptHtml({answer}, '/o')") == (
        '<div class="h3-prompt" data-idx="1">'
        '<p class="hint">5,13 с · сид 42 · 30 шагов · первый кадр: последний кадр сцены #0</p>'
        '<pre>Continue.\n\n&lt;Subject 1&gt; runs &lt;b&gt;</pre>'
        '<div class="h3-pictures"><figure><img src="/media/library/arena/v1/01-o.png" alt="">'
        '<figcaption>&lt;Picture 1&gt;</figcaption></figure></div></div>')
```
(варианты подписи кадра: `start_image` → `первый кадр: <имя файла>`, `null` → `без первого кадра` — ещё две строки того же теста, литералами.) Сценарий `h3_prompt_saves_first`: правка промпта сцены 0 в `queryAll[FIELDS]`, клик `scene-h3-prompt` с `idx: "0"`, маршрут `GET /api/projects/p1/scenes/0/h3-prompt` → `ok({...})`; ожидание `writes` в порядке `[["PUT", "/api/projects/p1/scenes"]]` и затем GET этого маршрута среди `calls` после PUT (`calls.findIndex` PUT < `findIndex` GET), в `#project-body` есть `<div class="h3-prompt" data-idx="0">`.
- [ ] **Step 2: Красный** — 404 `no route` / `h3PromptHtml is not a function`.
- [ ] **Step 3: Реализация** по Interfaces. Гейт (`_scene_reference_errors`) после рефактора — тот же список ошибок: прогнать `tests/test_sglang_scenes.py tests/test_sglang_scenario_load.py tests/test_sglang_scene_refs.py` до и после.
- [ ] **Step 4: Зелёный; CSS** (`h3-prompt`, `h3-pictures`; `pre` с `white-space: pre-wrap; overflow-wrap: anywhere`, чтобы 390 px не раздвигались) + `CLASS_SOURCES`.
- [ ] **Step 5: Мутация** — в маршруте не снапить длительность → красный второго теста (`AssembleError`/`duration_off_grid` вместо 200); убрать `i2v_prefix` → красный; в UI не сохранять перед запросом → красный `h3_prompt_saves_first`.
- [ ] **Step 6: Полный прогон; коммит** — `feat(projects): «Промпт для H3» — итоговый промпт и картинки сцены для sglang до утверждения`.

---

## Task 9: Библиотека в UI: превью при загрузке, новая версия с файлом, выбор версии, удаление

**Files:**
- Modify: `h3_48gb/webui/app.js` — `libraryCardsHtml` (`app.js:304`), `projectReferencesHtml` (`app.js:239`), `referencesPayload` (`app.js:298`), `loadLibrary` (`app.js:2742`), `poll`; `style.css`; тесты

**Interfaces:**
- Consumes: `versions` и `DELETE /api/library/<name>` (задача 3), блокировка `references` (задача 1).
- Produces:
  - `libraryCardsHtml(cards, outdir)` — миниатюра `<img class="lib-thumb" src="/media/…" alt="">`, подпись `"<kind>, v<N>, картинок: M"` (M — `assets.length` текущей версии: от него зависит нумерация `<Picture k>`, бой сверял это скриптом; для `voice` — `"аудио"` вместо картинок) (+ `" · версий: K"` при K > 1), поле `<input class="lib-new-files" type="file" multiple accept=".png,.jpg,.jpeg,.mp3,.wav">`, кнопки `data-act="lib-new-version"` и `data-act="lib-delete"` (у обеих `data-tag`).
  - `projectReferencesHtml(proj, cards, pinned, lock = null)` — у каждой карточки `<select class="ref-version" data-tag="…">` с `vN` по `card.versions` (выбрана подключённая, иначе последняя); при `lock` — все `input`/`select` с `disabled` и `<p class="why lock-note">lock</p>`.
  - `referencesPayload(checkedTags, pinned, chosen = {})` — `chosen[tag]` (число) побеждает; иначе уже подключённая версия; иначе без `version`.
  - Перерисовка библиотеки в `poll()`, когда `state.outdir` стал известен или сменился (`renderLibrary()` из `libraryCards` без нового запроса).

- [ ] **Step 1: Тесты**
```python
@_needs_node
def test_references_payload_takes_the_chosen_version():
    assert _js("app.referencesPayload(['@a', '@b', '@c'], [{tag: '@a', version: 1}, {tag: '@b', version: 2}], {'@b': 1})") \
        == [{"tag": "@a", "version": 1}, {"tag": "@b", "version": 1}, {"tag": "@c"}]


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
```
Плюс точная разметка `projectReferencesHtml` (её пишет исполнитель литералом, по образцу теста выше) с двумя версиями (выбрана v1 при `latest_version` 2) и с `lock`. Сценарии (код — в `_ui_gaps_check.mjs`, ожидания — литералами в `test_webui_gaps.py`):
- `library_preview_after_late_state`: `routes["GET /api/state"] = () => sleep(40).then(() => ok(SGLANG))` (SGLANG.outdir = "/o"), `GET /api/library` отвечает сразу карточкой с `assets: ["/o/library/alice/v1/01-a.png"]`; после `start` и `sleep(120)` — `{"img": <первое вхождение <img …> в #library-cards>}` == `'<img class="lib-thumb" src="/media/library/alice/v1/01-a.png" alt="">'`.
- `library_delete_in_use`: `answers.confirm = true`, `DELETE /api/library/alice` → `err(409, "library_card_in_use", "@alice подключена к проектам: «Бой» (p1) — отключите её там или удалите проекты")`; карточка находится через `button.closest(".lib-card")` (мок: `closest(sel) { return sel === "button[data-act]" ? this : sel === ".lib-card" ? card : null; }`). Ожидание: `confirms == ["Удалить карточку @alice? Файлы уйдут в library/.trash."]`, `cardError == {"hidden": False, "textContent": "<то же сообщение>"}`, `deletes == ["/api/library/alice"]`.
- `library_new_version`: у карточки `querySelector(".lib-new-files")` → `{files: [{name: "b.png"}]}`, `querySelector(".lib-edit-desc")` → `{value: "a woman"}`; `POST /api/uploads` → `ok({path: "/o/uploads/b.png"})`; ожидание `puts == [["/api/library/alice", {"description": "a woman", "assets": ["/o/uploads/b.png"]}]]`.
- `refs_version_change`: открыть проект, `change` на `select.ref-version` (`{classList: {contains: (c) => c === "ref-version"}, dataset: {tag: "@a"}, value: "1", …}`) при отмеченной `@a` → `PUT /api/projects/p1/references` с `[{"tag": "@a", "version": 1}]`.
```js
async library_preview_after_late_state() {
  routes["GET /api/state"] = () => sleep(40).then(() => ok(SGLANG));
  await start(appUrl, { "GET /api/library": ok({ ok: true, cards: [{ tag: "@alice", kind: "person",
    version: 1, latest_version: 1, description: "a woman", assets: ["/o/library/alice/v1/01-a.png"],
    versions: [{ version: 1 }] }] }) });
  await sleep(120);
  const m = getElementById("library-cards").innerHTML.match(/<img [^>]*>/);
  return { img: m ? m[0] : null };
},
async library_delete_in_use() {
  answers.confirm = true;
  const message = "@alice подключена к проектам: «Бой» (p1) — отключите её там или удалите проекты";
  await start(appUrl, { "DELETE /api/library/alice": err(409, "library_card_in_use", message) });
  const cardError = { hidden: true, textContent: "" };
  const card = { querySelector: (sel) => (sel === ".lib-card-error" ? cardError : null) };
  fire("click", { dataset: { act: "lib-delete", tag: "@alice" },
    closest(sel) { return sel === "button[data-act]" ? this : sel === ".lib-card" ? card : null; } });
  await sleep(80);
  return { confirms, cardError,
           deletes: calls.filter((c) => c.method === "DELETE").map((c) => c.url) };
},
```
(Работает, потому что `start` харнесса не перетирает уже заданные маршруты — задача 6.)
- [ ] **Step 2: Красный** (в частности `library_preview_after_late_state` на текущем коде: `img: null`).
- [ ] **Step 3: Реализация.** Обработчики `lib-new-version`/`lib-delete` — в главном делегированном `click`; `confirm` удаления: `"Удалить карточку <tag>? Файлы уйдут в library/.trash."`. `renderProjectModal` передаёт в `projectReferencesHtml` четвёртым аргументом `null` — настоящую причину подключает задача 10 (`projectLocks`).
- [ ] **Step 4: Зелёный; CSS** (`lib-thumb` 96×96 `object-fit: cover`, `lib-new-files`, `ref-version`, `lock-note`) + `CLASS_SOURCES`.
- [ ] **Step 5: Мутация** — убрать перерисовку из `poll` → красный `library_preview_after_late_state`; в `referencesPayload` поставить подключённую версию выше `chosen` → красный.
- [ ] **Step 6: Полный прогон** (в т.ч. `tests/test_webui_panel.py::…library_save…` — разметка карточки поменялась, сценарий `library_save` ищет поля через `querySelector` по классу, должен остаться зелёным); **коммит** — `feat(webui): превью библиотеки при загрузке, новая версия карточки с файлом, выбор версии в проекте, удаление карточки`.

---

## Task 10: Прогон: прогресс на sglang, плашка «кто держит карту», блокировки, пересъёмка с правкой

**Files:**
- Modify: `h3_48gb/webui/app.js` — `gpuBanner` (`app.js:73`), `renderRunning` (`app.js:3566`), `projectSceneCardHtml` (`app.js:3406`, становится экспортируемой `sceneCardHtml`), `projectSettingsHtml` (`app.js:214`), `projectRouteHtml` (`app.js:230`), `renderProjectModal` (`app.js:3016`), обработчик `retry-scene` (`app.js:5027`), focusout `i2v-prefix` (`app.js:~5075`); `style.css`; тесты

**Interfaces:**
- Consumes: задачи 1 (`PROJECT_LOCK_TEXT`), 2 (тело ретрая), 9 (`projectReferencesHtml(…, lock)`), Task 0 (`seed`, `steps`, `project.seed` — только sglang).
- Produces:
  - `export const PROJECT_LOCK_TEXT` — те же три строки, что `web.PROJECT_LOCK_TEXT`.
  - `projectLocks(proj, activeJob) -> {references, settings, route}` — текст или `null`: `references`/`settings` при любом `activeJob`; `route` при `activeJob.kind ∈ {"upscale", "assembly"}`.
  - **Проводка блокировок:** `renderProjectModal` один раз считает `locks = projectLocks(proj, project.active_job)` и передаёт `locks.settings` в `projectSettingsHtml`, `locks.references` в `projectReferencesHtml` (параметр из задачи 9), `locks.route` в `projectRouteHtml(proj, engine, lock = null)` (новый третий параметр: `disabled` у галочки и `<p class="why lock-note">…</p>` после `label`).
  - `projectSettingsHtml(proj, engine, lock = null, saved = null)` — `i2v_prefix` всегда; сид проекта (`input.project-seed` + `data-act="project-seed-save"`) **только на sglang**; `saved ∈ {null, "i2v_prefix", "seed"}` — `<span class="saved-mark">сохранено ✓</span>` у сохранённого поля; при `lock` — `disabled` у всех полей и кнопки и `<p class="why lock-note">lock</p>`; «Черновой сборки» больше нет (уезжает в задачу 11). Какое поле сохранено последним, DOM-половина хранит в `settingsSaved` и сбрасывает при открытии другого проекта и при следующей правке.
  - `sglangRunView(job, nowMs) -> {spec, elapsed, total, share, leftSeconds, waiting}` — `spec = "<W>×<H> · <dur> с · сид <seed> · <steps> шагов"` из `--width/--height/--duration/--seed/--steps` аргументов (`dur` — **поставляемая** длительность: у сцепленной сцены (в argv `--aspect auto`, `assemble._scene_generate_args_sglang`) на кадр меньше запрошенной — `(round(d·24) − 1)/24`, чтобы совпасть с подсказкой редактора; формат `gridHint` без «на сетке: »), `elapsed = formatDuration(сек от started_at)`, `total = "≈" + formatDuration(estimate.seconds)`, `share` — целый процент `min(99, floor(100·прошло/оценка))` (0 без оценки), `leftSeconds = max(0, оценка − прошло)`, `waiting = Boolean(job.wait_reason)`.
  - `gpuBanner(gpu, nowMs, projects = [])` — новая ветка: `run && !run.wait_reason` → при своём движке `{visible: true, tone: "own", text: "Карту держит панель: H3 считает <что> — <formatDuration> <, память>"}`, без своего движка `"H3 поднимается для <что>"`; `<что>` из `run.note`: `project scene <id> #<n>` → `«<title>», сцена #<n>` (title из `projects`, иначе id), `upscale project <id>` → `апскейл «<title>»`, `assemble project <id>` → `сборка «<title>»`, иначе `note`.
  - `retryCascade(scenes, idx) -> number[]` — `idx` и следующие до первой `fresh_start` после `idx`.
  - `retryBody(scene, edits) -> object` — только поля, отличные от сцены: `prompt` (если строка изменена), `seed`, `steps` (числа; пустое поле — не отправлять).
  - `retryPanelHtml(scene, ctx) -> string` — `ctx = {id, engine, effectiveSeed, cascade}`: `textarea.retry-prompt` (промпт целиком), сид с `placeholder="сейчас: <effectiveSeed>"` и `data-act="retry-seed-random"`, шаги (sglang), строка `"Пересчитает сцены #1, #2, #3"`, кнопки `data-act="retry-scene-go"` и `data-act="retry-scene-cancel"`.
  - `sceneCardHtml(scene, ctx)` — промпт целиком в `<details class="scene-prompt"><summary>первые 260 символов…</summary>полный</details>` (короче 260 — без `details`), строка `сид <N> · <S> шагов` на sglang, ссылка `LTX` при `ltx_path`, «Пересчитать сцену» (`data-act="retry-scene"`) только при `status ∈ {done, failed}`.

- [ ] **Step 1: Тесты**
```python
from h3_48gb import project as project_module


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
        "share": 35, "leftSeconds": 350, "waiting": False}
    late = "Date.parse('2026-10-07T12:45:00Z')"          # past the estimate: never 100 % while running
    assert _js(f"app.sglangRunView({SG_JOB}, {late})")["share"] == 99
    assert _js(f"app.sglangRunView({SG_JOB}, {late})")["leftSeconds"] == 0
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
```
Плюс точная разметка `sceneCardHtml` (литералами, по образцу `test_project_settings_html`) для `pending` (без кнопки), `done` с `ltx_path` и промптом длиннее 260, `failed`; `retryPanelHtml` для sglang и MLX (на MLX нет сида и шагов). `start(appUrl, extra)` харнесса возвращает импортированный модуль `app` — сценарии ниже строят ожидание его же чистой функцией, а сама функция пиннится литералом выше: так сценарий проверяет **проводку** (что `renderProjectModal` передал причину), а не разметку второй раз.

```js
const DONE_PROJECT = PROJECT({ scenes: [
  { idx: 0, prompt: "@a walks", duration: 8, status: "done", job_id: null,
    clip_path: "/o/projects/p1/scenes/a.mp4", keyframe_path: null, seed: 305 },
  { idx: 1, prompt: "@a runs", duration: 5, status: "done", job_id: null,
    clip_path: "/o/projects/p1/scenes/b.mp4", keyframe_path: null }],
  references: [{ tag: "@a", version: 1 }], i2v_prefix: "Go.", seed: null });
const CARD_A = { tag: "@a", kind: "person", version: 1, latest_version: 1, description: "d",
                 assets: ["/o/library/a/v1/01-a.png"], versions: [{ version: 1 }] };
const block = (cls) => {
  const m = getElementById("project-body").innerHTML.match(new RegExp(`<div class="${cls}"[\\s\\S]*?</div>`));
  return m ? m[0] : null;
};
// SCENARIOS:
async retry_with_new_seed() {
  await start(appUrl, { "GET /api/projects/p1": ok(DONE_PROJECT),
    "POST /api/projects/p1/scenes/0/retry": ok({ ok: true, project: DONE_PROJECT.project }) });
  await open();
  await act("retry-scene", { idx: "0" });
  const opened = /<div class="retry-panel" data-idx="0">/.test(getElementById("project-body").innerHTML);
  const fields = { ".retry-prompt": { value: "@a walks" }, ".retry-seed": { value: "7" },
                   ".retry-steps": { value: "" } };
  queryOne['#project-body .retry-panel[data-idx="0"]'] = {
    querySelector: (sel) => (Object.hasOwn(fields, sel) ? fields[sel] : null) };
  await act("retry-scene-go", { idx: "0" });
  return { opened, confirms, posts: posts() };
},
async locked_while_a_scene_runs() {
  const running = { ...DONE_PROJECT, active_job: { kind: "scene", idx: 1, job: { id: "j1" } } };
  const app = await start(appUrl, { "GET /api/projects/p1": ok(running),
    "GET /api/library": ok({ ok: true, cards: [CARD_A] }) });
  await open();
  const p = running.project;
  return {
    settings: block("project-settings") === app.projectSettingsHtml(p, "sglang", app.PROJECT_LOCK_TEXT.settings, null),
    refs: block("project-refs") === app.projectReferencesHtml(p, [CARD_A], p.references, app.PROJECT_LOCK_TEXT.references),
    route: getElementById("project-body").innerHTML.includes(app.projectRouteHtml(p, "sglang", null)),
  };
},
async locked_route_during_upscale() {
  const upscaling = { ...DONE_PROJECT, active_job: { kind: "upscale", job: { id: "j2" } } };
  const app = await start(appUrl, { "GET /api/projects/p1": ok(upscaling) });
  await open();
  return { route: getElementById("project-body").innerHTML.includes(
    app.projectRouteHtml(upscaling.project, "sglang", app.PROJECT_LOCK_TEXT.route)) };
},
async settings_saved_mark() {
  const app = await start(appUrl, { "GET /api/projects/p1": ok(DONE_PROJECT),
    "PUT /api/projects/p1/settings": ok({ ok: true, project: DONE_PROJECT.project }) });
  await open();
  fire("focusout", { value: "Go on.", dataset: { id: "p1" },
    classList: { contains: (c) => c === "i2v-prefix" }, closest: () => null });
  await sleep(120);
  return { puts: puts(),
           mark: block("project-settings") === app.projectSettingsHtml(DONE_PROJECT.project, "sglang", null, "i2v_prefix") };
},
```
(`.includes` здесь — проверка вывода страницы на вхождение строки, которую строит та же чистая функция, а не мок; `closest`/`querySelector` моков — только точные ключи. Форму события `focusout` у `i2v-prefix` сверить с обработчиком `app.js:~5075` — если он ищет поле через `closest(".i2v-prefix")`, мок отвечает `sel === ".i2v-prefix" ? this : null`.)

Ожидания:
```python
RUN_EXPECTED = {
    "retry_with_new_seed": {"opened": True, "confirms": [],
                            "posts": [["/api/projects/p1/scenes/0/retry", {"seed": 7}]]},
    "locked_while_a_scene_runs": {"settings": True, "refs": True, "route": True},
    "locked_route_during_upscale": {"route": True},
    "settings_saved_mark": {"puts": [["/api/projects/p1/settings", {"i2v_prefix": "Go on."}]],
                            "mark": True},
}


@_needs_node
@pytest.mark.parametrize("scenario", sorted(RUN_EXPECTED))
def test_run_wiring(scenario):
    assert _gaps(scenario) == RUN_EXPECTED[scenario]
```
- [ ] **Step 2: Красный.**
- [ ] **Step 3: Реализация.** `renderRunning`: при `state.engine === "sglang"` ветка на `sglangRunView` (ячейки «Идёт», «Оценка», «Доля», «Кончится» (`formatClock(now + leftSeconds)`), без «Проход»/«Пик памяти»; при `waiting` — «ждёт карту» вместо доли; `rail` — та же доля). `renderGpu` передаёт `state.projects`. `projectSettingsHtml`, `projectRouteHtml` и `projectReferencesHtml` получают причины из одного `projectLocks` в `renderProjectModal`. «Сохранить сид» → `PUT …/settings {seed: число | null}` (пустое поле — `null`), успех → `settingsSaved = "seed"`; focusout `i2v-prefix` после успеха → `settingsSaved = "i2v_prefix"`.
- [ ] **Step 4: Зелёный; CSS** (`retry-panel`, `scene-prompt`, `lock-note`, `saved-mark`, `run-wait`) + `CLASS_SOURCES` (`projectSettingsHtml` с блокировкой и отметкой, `projectRouteHtml` с блокировкой, `retryPanelHtml`, `sceneCardHtml`); в `HOOK_CLASSES` — `project-seed`, `retry-prompt`, `retry-seed`, `retry-steps` (поля, вид от `.inp`).
- [ ] **Step 5: Мутация** — `retryCascade` без остановки на `fresh_start` → красный сверки с Python; ветка `run && !run.wait_reason` убрана → красный баннера; `share` без `min(99, …)` → красный строки `late`; не передавать `locks.references` в `renderProjectModal` → красный `locked_while_a_scene_runs`; не выставлять `settingsSaved` → красный `settings_saved_mark`.
- [ ] **Step 6: Полный прогон** (`tests/test_webui_panel.py` — старые тесты баннера с `gpuBanner(gpu, now)` без третьего аргумента должны остаться зелёными); **коммит** — `feat(webui): прогресс сцены на sglang, кто держит карту во время прогона, блокировки идущего проекта, пересъёмка с новым сидом и промптом`.

---

## Task 11: Апскейл и сборка в UI: сила и части, «Скачать», черновая сборка по делу, 404 job-upscale

**Files:**
- Modify: `h3_48gb/webui/app.js` — `projectUpscaleHtml` (`app.js:258`), `projectAssemblyStageHtml` (`app.js:3469`, становится экспортируемой `projectAssemblyHtml`), `isProjectPipelineNote` (`app.js:1036`), `finishedRowHtml` (`app.js:1214`), `sceneCardHtml` (ссылка LTX — задача 10); `style.css`; тесты

**Interfaces:**
- Consumes: `upscale_report` (задача 5, поле `attempted` — здесь **не используется**: готовые части считаются по `ltx_path` сцен).
- Produces:
  - `projectUpscaleHtml(proj, engine)` — `"Апскейл LTX: <слово>"`; **только при `stages.upscale === "done"` и `upscale_report.status === "done"`** добавляется `" · сила 0,6 · 2 из 3 частей"` (сила — `String(x).replace(".", ",")`; части — сцены с `ltx_path` / все сцены); при `stages.upscale === "failed"` — `<p class="why upscale-error">{upscale_report.error}</p>` (если есть) и «Повторить апскейл». Устаревший отчёт (стадия `draft`/`running`, отчёт от прошлой попытки) не показывается.
  - `downloadName(proj) -> string` — `"<normalizeSlug(title) || id>-<YYYYMMDD>-final.mp4"` (решение координатора); дата — локальная дата `assembly.v` (mtime финала в секундах), без него — `created_at`. `normalizeSlug` — существующий (`SLUG_TRANSLIT`: «й» → «y»).
  - `projectAssemblyHtml(proj, outdir)` — точная разметка в тесте ниже: финал — `final.mp4` + `<a class="ghost" href="<url>" download="<downloadName>">Скачать</a>`; «Черновая сборка» только при всех сценах `done` и `stages.assembly !== "running"`; нет финала и черновая недоступна — `""`.
  - `isProjectPipelineNote` — также `^upscale project \S+$`.
  - `finishedRowHtml` — у `kind === "assemble"` нет кнопок `chat` и `dup`.

- [ ] **Step 1: Тесты**
```python
@_needs_node
def test_pipeline_notes_include_the_upscale():
    assert _js("['upscale project 20261007-ab', 'project scene p #1', 'assemble project p', "
               "'upscale projects x'].map(app.isProjectPipelineNote)") == [True, True, False, False]


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


@_needs_node
def test_a_project_assembly_tile_has_no_chat_or_copy():
    job = ("{id: 'j9', kind: 'assemble', exit_code: 0, note: 'assemble project p1', "
           "output_stem: '/o/projects/p1/assembly/job-final', estimate: {}, "
           "started_at: '2026-10-07T12:00:00Z', finished_at: '2026-10-07T12:01:00Z'}")
    html = _js(f"app.finishedRowHtml({job}, '/o', [], new Set(), 'Бой')")
    assert re.search(r'<div class="acts">.*?</div>', html).group(0) == (
        '<div class="acts"><button data-act="reveal" data-id="j9">Показать в Finder</button>'
        '<button data-act="delrun" data-id="j9">Удалить</button></div>')
```
`href` и слаг сверены запуском 07.10 (`projectMediaUrl` → `/media/projects/p1/assembly/final.mp4?v=1791374400`, `normalizeSlug("Бой на арене")` → `boy-na-arene`). `1791374400` — 2026-10-07 12:00Z: локальная дата одна и та же в любом поясе от −11 до +11. Существующую разметку блока сборки (`app.js:3469`) реализация может поменять, только обновив литерал целиком.
- [ ] **Step 2: Красный.**
- [ ] **Step 3: Реализация.** `renderProjectModal` вызывает `projectAssemblyHtml(proj, outdir)` вместо `projectAssemblyStageHtml`; обработчик `.draft-assembly` (`app.js:5082`) остаётся как есть.
- [ ] **Step 4: Зелёный; CSS** (`upscale-error`, `proj-final` при необходимости) + `CLASS_SOURCES` (`projectUpscaleHtml` для `done` и `failed`, `projectAssemblyHtml` для `done`).
- [ ] **Step 5: Мутация** — убрать `^upscale project` из фильтра → красный; показывать силу без проверки `stages.upscale === "done"` → красный `test_stale_upscale_report_is_not_shown`; показывать «Черновую сборку» всегда → красный `pending`-строки; дата из `created_at` даже при `v` → красный `test_download_name`.
- [ ] **Step 6: Полный прогон; коммит** — `feat(webui): сила и части апскейла, «Скачать» финал, черновая сборка только по готовым сценам, без 404 job-upscale в «Готово»`.

---

## Task 12: LLM-путь в UI: «Чат по сценарию», «Применить к проекту», честная подпись провайдера

**Files:**
- Modify: `h3_48gb/webui/app.js` — `llmPlateText` (`app.js:2304`), `renderLlmPlate` (`app.js:3879`), `projectScriptStageHtml`, обработчик `chat-make-project` (`app.js:5382`)/`chat-project-create` (`app.js:5384`), `enterChat` (подпись кнопки по `source.kind`); `style.css`; тесты

**Interfaces:**
- Consumes: задача 4 (`source.kind = "project"`, `kind: "video"` у сессии проекта, `shares_gpu`).
- Produces:
  - `llmPlateText(status, {external, sharesGpu, runningSeconds})` — **порядок ветвей:** (1) `!external` (это `type === "llama-local"`) → прежние тексты по `status`, включая «идёт прогон — модель поднимется после него (~N мин)»; (2) `sharesGpu === true` → `"делит видеокарту с H3: пока модель поднята, рендер ждёт"`; (3) `sharesGpu === false` → `"внешний провайдер — память этой машины не занимает"`; (4) иначе (`null`/нет ключа) → `"где считает провайдер, не указано (shares_gpu в providers.json)"`. `renderLlmPlate` передаёт `external: row.type !== "llama-local"`, `sharesGpu: row.shares_gpu`.
  - `chatApplyBody(chatProject) -> {scenes: [{prompt, duration}]}` — из `chat.project.scenes`, только эти два поля.
  - `chatApplyConfirm(current, incoming) -> string | null` — `null` при `current === 0`; иначе `"Заменить 1 сцену проекта на 3 из диалога?"` / `"Заменить 2 сцены …"` / `"Заменить 5 сцен …"` (`plural`; решение координатора — назвать число заменяемых сцен).
  - В этапе «Сценарий» редактируемого видеопроекта — кнопка `<button type="button" class="ghost" data-act="project-chat" data-id="…">Чат по сценарию</button>` → `openChatModal({kind: "project", id}, {prompt: "", mode: "", image: "", endImage: "", duration: 10})`.
  - В модалке чата для сессии с `source.kind === "project"` кнопка `#chat-make-project` подписана «Применить к проекту» (иначе — прежнее «Сделать проектом») и по клику: `GET /api/projects/<id>` → `chatApplyConfirm(scenes.length, chat.project.scenes.length)` → при не-`null` `confirm(...)` (отказ — ничего) → `PUT /api/projects/<id>/scenes` с `chatApplyBody` → `closeChat()` → `openProjectModal(id)`; ошибка — в `#chat-project-err` (панель раскрывается для показа ошибки).

- [ ] **Step 1: Тесты**
```python
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
```
Сценарии (`DRAFT_PROJECT`, `open`, `act`, `posts`, `puts`, `draftRoutes` — из задачи 7):
```js
const CHAT_SESSION = { ok: true, id: "c1", source: { kind: "project", id: "p1" }, mode: "t2va",
  image: "", end_image: "", duration: 10, messages: [], prompt: "", kind: "video", tags: ["@a"],
  project: { kind: "video", scenes: [{ prompt: "@a jumps", duration: 6 },
    { prompt: "@a lands", duration: 4 }, { prompt: "@a bows", duration: 3 }] } };
// SCENARIOS:
async project_chat_opens() {
  await start(appUrl, draftRoutes({ "POST /api/chat": ok({ ok: true, id: "c1" }),
                                    "GET /api/chat/c1": ok(CHAT_SESSION) }));
  await open();
  await act("project-chat");
  return { posts: posts() };
},
async chat_apply_to_project() {
  globalThis.window.location.hash = "#chat/c1";
  await start(appUrl, draftRoutes({ "GET /api/chat/c1": ok(CHAT_SESSION) }));
  await sleep(120);
  const label = getElementById("chat-make-project").textContent;
  answers.confirm = true;
  getElementById("chat-make-project").__listeners.click[0]();
  await sleep(160);
  return { label, confirms, puts: puts() };
},
async chat_apply_declined() {
  globalThis.window.location.hash = "#chat/c1";
  await start(appUrl, draftRoutes({ "GET /api/chat/c1": ok(CHAT_SESSION) }));
  await sleep(120);
  answers.confirm = false;
  getElementById("chat-make-project").__listeners.click[0]();
  await sleep(160);
  return { puts: puts() };
},
```
Ожидания:
```python
CHAT_EXPECTED = {
    "project_chat_opens": {"posts": [["/api/chat", {
        "source": {"kind": "project", "id": "p1"}, "prompt": "", "mode": "", "image": "",
        "end_image": "", "duration": 10}]]},
    "chat_apply_to_project": {
        "label": "Применить к проекту",
        "confirms": ["Заменить 2 сцены проекта на 3 из диалога?"],
        "puts": [["/api/projects/p1/scenes", {"scenes": [
            {"prompt": "@a jumps", "duration": 6}, {"prompt": "@a lands", "duration": 4},
            {"prompt": "@a bows", "duration": 3}]}]]},
    "chat_apply_declined": {"puts": []},
}


@_needs_node
@pytest.mark.parametrize("scenario", sorted(CHAT_EXPECTED))
def test_chat_wiring(scenario):
    assert _gaps(scenario) == CHAT_EXPECTED[scenario]
```
Если `enterChat` требует в сессии полей сверх `CHAT_SESSION` — дописать их в фикстуру по `_create_chat` (`web.py`), не ослабляя ожидания. Старые тесты `llmPlateText` (`tests/test_web.py`, поиск `llmPlateText`) с `{external: true}` без `sharesGpu` **меняют ожидание** на текст ветки (4): это и есть исправляемый баг (аудит, топ-5); перечислить их в отчёте.
- [ ] **Step 2: Красный.**
- [ ] **Step 3: Реализация** по Interfaces.
- [ ] **Step 4: Зелёный; CSS** при необходимости + `CLASS_SOURCES` (кнопка «Чат по сценарию» внутри `projectScriptStageHtml` — классы уже существующие).
- [ ] **Step 5: Мутация** — поставить ветку `sharesGpu` раньше `!external` → красный второй строки (прогон у локальной модели); вернуть старое `external → «не занимает»` → красный пятой и шестой; убрать `confirm` → красный `chat_apply_declined`; «Применить» шлёт `POST /api/projects` → красный `chat_apply_to_project`.
- [ ] **Step 6: Полный прогон; коммит** — `feat(webui): чат по сценарию проекта, «Применить к проекту» с числом сцен, честная подпись провайдера по shares_gpu`.

---

## Task 13: Приёмка — координатор проходит ролик в браузере целиком

Исполнитель — координатор, не субагент-кодер. Инструмент — playwright MCP (`mcp__plugin_playwright_playwright__*`) против живой панели `http://192.168.100.50:8765` после выкладки ветки (выкладка — по процедуре задачи 14 плана W1, вне этого плана). **Запрещено:** ssh, curl, правка файлов на сервере или в outdir, `browser_evaluate` с `fetch`. Разрешено `browser_evaluate` только для чтения размеров (шаг 12). Все шаги — клики, ввод, `browser_file_upload`, `browser_handle_dialog` (для `prompt`/`confirm`). Скриншоты — `~/worktrees/battle/ui-gaps-acceptance/NN-*.png`, протокол — там же `REPORT.md`.

Модалка проекта не перечитывается по опросу (существующее поведение): на шагах 8–11 статус сцен смотреть, закрыв и заново открыв проект из списка.

**Предусловия (проверяет координатор глазами на странице, не командами):** плашка GPU не красная; очередь пуста; в `providers.json` у Qwen `shares_gpu` выставлен при выкладке (иначе подпись в шаге 4 — «не указано»: допустимо, отметить); Мак не должен уснуть до конца — координатор держит сессию под `caffeinate -dimsu`, как требует `CLAUDE.md`.

**Обязательные шаги:** 1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12. Шаг 4 — обязателен вход и подпись; ход LLM и «Применить» — если Qwen поднят, иначе в итоге «не проверено» (не «прошёл»).

- [ ] **1. Референсы.** Внизу страницы «Референсы»: загрузить портрет (`browser_file_upload`), тег `@acc-hero`, `person`, описание по-английски → «Добавить». Превью 96×96 видно **без перезагрузки**; перезагрузить страницу — превью видно сразу. Подпись карточки — «person, v1, картинок: 1». Загрузить кадр локации → `@acc-place`, `environment`. Скриншот `01-library.png`.
- [ ] **2. Новая версия и удаление.** У `@acc-hero` — другой файл → «Новая версия» → подпись `v2, картинок: 1 · версий: 2`. Карточка `@uiaudit-face` (мусор аудита): «Удалить» → подтвердить. Если ответ — «подключена к проектам: …»: открыть названный проект из списка, снять её галочку в «Референсах проекта» (или удалить проект кнопкой «удалить», если он мусорный), вернуться и удалить карточку снова — всё через UI. Карточка исчезла. Скриншот `02-library-v2.png`.
- [ ] **3. Проект.** Зона «Проекты» → «+ Новый проект» → название `acc-1507` → открылась модалка проекта. В «Референсах проекта» отметить `@acc-hero` (в селекте выбрать **v1**) и `@acc-place`. Скриншот `03-project-refs.png`.
- [ ] **4. Диалог.** «Чат по сценарию» открывает модалку чата; кнопка шапки подписана «Применить к проекту»; подпись провайдера — не «память не занимает», если провайдер — Qwen на этой карте. **Если Qwen поднят:** один ход «напиши сценарий из двух сцен по 4 с с @acc-hero на @acc-place» → «Применить к проекту» → `confirm` называет число сцен → сцены из диалога в редакторе. **Если не поднят:** закрыть без отправки, в итоге — «LLM-ход и Применить: не проверено». Скриншот `04-chat.png`.
- [ ] **5. Сценарий вручную** (поверх того, что дал шаг 4, если дал). В редакторе: сцена #0 — промпт с `@acc-hero` (набрать `@acc-h` и выбрать подсказку **кнопкой под полем**) и `@acc-place`, длительность 4 → подсказка сразу, без сохранения, «на сетке: 3,75 с»; стартовый кадр — `@acc-place`; сид 305. «+ Сцена» → #1: промпт **без** `@`-тега, в «Референсах без упоминания» отметить `@acc-hero`, длительность 3, шаги 30. «+ Сцена» → #2, затем «Удалить» её (остаются 2). Отметить/снять любую галочку «Референсов проекта» и убедиться, что набранный промпт **не пропал**. «Сохранить сценарий» → «не сохранено» исчезло. «Промпт для H3» у #0 и #1: виден `subject_definitions` с `<Picture k>`, полоска миниатюр, у #1 — «первый кадр: последний кадр сцены #0». Перезагрузить страницу, открыть проект — всё на месте (сид, шаги, стартовый кадр, референсы #1). Скриншот `05-editor.png`, `05b-h3-prompt.png`.
- [ ] **6. JSON.** В «Вставить сценарий JSON» вставить `42` → «Загрузить» → понятная ошибка, сцены не тронуты. Затем вставить валидный JSON двух сцен (скопировать из «Промпта для H3» не нужно — набрать `{"scenes": [{"prompt": "@acc-hero walks on @acc-place", "duration": 4, "start_image": "@acc-place", "seed": 305}, {"prompt": "@acc-hero turns", "duration": 3, "steps": 30}]}`) → `confirm` «Заменить 2 сцены сценария?» → «да» → редактор показывает две сцены из JSON. Скриншот `06-json.png`.
- [ ] **7. Настройки и запуск.** Сид проекта **306** (отличается от сида сцены #0) → «сохранено ✓»; `i2v_prefix` — оставить/поправить → «сохранено ✓». Галочка «Апскейл LTX после сцен» включена. «Утвердить сценарий» → «▶ Начать расчёт».
- [ ] **8. Во время прогона.** Плашка GPU: «Карту держит панель: H3 считает «acc-1507», сцена #0 — …» (или «ждёт карту: …» с причиной). Карточка «идёт»: канвас, длительность, **сид 305** (свой у #0, не 306 проекта), «идёт N мин из ≈M мин», доля **не 0 %** после первой минуты. В модалке проекта поля `i2v_prefix`, сид проекта и «Референсы проекта» — `disabled` с причиной; галочка апскейла — активна. Скриншоты `07-running.png`, `08-locks.png`.
- [ ] **9. Пересъёмка.** Когда сцена #0 `done`: «Пересчитать сцену» у #0 → панель: промпт целиком, «новый случайный» сид, строка «Пересчитает сцены #0, #1» → «Пересчитать». Сцена переснимается с **новым** сидом (виден в карточке «идёт»). Скриншот `09-retry.png`.
- [ ] **10. Апскейл.** После всех сцен: «Апскейл LTX: идёт», затем «готов · сила X · N из N частей»; у карточек сцен ссылки «LTX». В «Готово» нет плитки `job-upscale` с «клип удалён». Скриншот `10-upscale.png`.
- [ ] **11. Сборка и финал.** «Сборка: готово», `final.mp4` открывается, «Скачать» скачивает файл с именем `acc-1507-<ГГГГММДД дня сборки>-final.mp4` (playwright: событие download, имя и размер > 0). «Черновая сборка» видна только при всех сценах `done`. Скриншот `11-final.png`.
- [ ] **12. 390 px.** `browser_resize` 390×844: главная, модалка проекта (редактор с «Промптом для H3» и готовый проект), библиотека. На каждом экране `browser_evaluate("() => [document.documentElement.scrollWidth, document.querySelector('#project-body') ? document.querySelector('#project-body').scrollWidth : 0]")` — оба ≤ 390. Скриншоты `12-390-main.png`, `13-390-project.png`, `14-390-library.png`.
- [ ] **13. Итог.** `REPORT.md`: каждый шаг — прошёл / нет / не проверено (только шаг 4, часть LLM), скриншот, замечания; отдельный список «пришлось бы обойти через ssh/curl» — должен быть пуст. Любой непройденный обязательный шаг — задача возвращается исполнителю соответствующей задачи плана.

---

## Расхождения с аудитом и IA (сознательные)

- `window.confirm` вместо встроенных подтверждений IA §1.6 — до волны 2 (спека §2); пересъёмка — встроенная панель уже сейчас.
- «+ Новый проект» (решение координатора, как в IA) в 1.5 создаёт только видеопроект; выбор вида (ролик/клип) — волна 2 на ту же кнопку.
- Чат проекта — модалка, а не боковая панель IA; контракт API (`source.kind = "project"`, «Применить» = `PUT …/scenes`) переезжает без изменений.
- Сила апскейла — только показ (аудит 5b, IA «сила 0,6»).
- «Промпт для H3» — шире IA («полоска миниатюр уходящих в H3 картинок»): показывает ещё и итоговый текст; это решение координатора вместо офлайн-скрипта боя.

## Самопроверка плана

- Каждый пункт объёма задания → задача: «+ Новый проект» — 6; редактор (промпт, 3–15 с с сеткой, fresh_start, seed, steps, порядок) — 7; refs/@-теги/tagSuggestions, кадр сцены 0, JSON — 8; «Промпт для H3» — 8b; версия карточки с файлом, выбор версии, удаление (API 3 + UI 9), превью — 9; LLM-путь (чат проекта, подпись, длительности, @теги из сценария) — 4 + 12; прогресс без нулей, плашка карты, пересъёмка, блокировки — 1 + 2 + 10; сила апскейла, части, 404, «Скачать», черновая сборка, «пересчитать» по делу — 5 + 10 + 11; CSS и 390 px — 6–12 + приёмка 13.
- Решение «запрет, а не предупреждение» для `i2v_prefix`/референсов/апскейла — спека §5.5, задачи 1 и 10 (проводка во все три блока — задача 10).
- Решения координатора: «+ Новый проект» — 6, 13; длительность новой сцены — 7; запрет удаления подключённой карточки — 3; имя `<title>-<YYYYMMDD>-final.mp4` — 11, 13; `confirm` «Применить» с числом сцен — 12; «Промпт для H3» — 8b, 13.
- Контракт Task 0 после фикс-раунда (только sglang) — Global Constraints, задачи 2, 7, 10.
- Тест-образцы плана используют `in` только там, где это оговорено: строка системного промпта (задача 4) и вхождение строки, построенной той же чистой функцией, в вывод страницы (сценарии задачи 10). Заглушек `/* … */` в тестах нет.

