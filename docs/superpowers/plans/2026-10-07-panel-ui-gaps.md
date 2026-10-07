# Монтажная панель, волна 1.5 (дыры интерфейса): план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** владелец сам, в браузере, без ssh/curl/правки файлов проходит конвейер ролика на sglang: референсы → проект → сценарий → сцены → апскейл → сборка → «Скачать» финал.

**Architecture:** новое поведение — в JSON-API `h3_48gb/web.py` (+ `library.py`, `project.py`, `provider.py`); старый интерфейс `h3_48gb/webui/{index.html,app.js,style.css}` получает минимальные блоки, каждый — экспортируемая чистая функция `app.js` (`export function …Html(data, ctx)`) плюс тонкий обработчик в DOM-половине. Волна 2 (IA, variant-a) переносит функции, заменяя только вёрстку.

**Tech Stack:** Python 3.12 stdlib, node (проверки фронта через pytest), playwright MCP (только приёмка).

**Spec:** `docs/superpowers/specs/2026-10-07-panel-ui-gaps-design.md` (читать целиком перед любой задачей). Спека W1: `docs/superpowers/specs/2026-10-06-panel-on-alex-neuro-design.md`. Аудит: `~/worktrees/battle/ui-audit/REPORT.md`.

**Ветка:** `feat/panel-ui-gaps`, worktree `~/worktrees/panel-ui-gaps`. В том же worktree параллельно работает агент Task 0 — перед каждой задачей `git status` и `git log -3`; чужие незакоммиченные изменения не трогать и не коммитить вместе со своими (`git add <свои файлы>`, никогда `git add -A`).

## Global Constraints

- **Task 0 (внешний) готов:** коммиты `4005de38` и `626db48d`. Контракт: у сцены в `PUT …/scenes` необязательные `seed` (int ≥ 0), `steps` (2..100, только sglang), `refs` (список подключённых `@`-тегов: условия без `<Subject N>`, идут первыми); `PUT …/settings {i2v_prefix?, seed?}`, `project.seed` в выдаче; эффективный сид `scene.seed → project.seed → 42`. Если до начала задачи контракт изменился (`git log -- tests/test_sglang_scene_params.py tests/test_sglang_scene_refs.py`) — поправить только соответствующее поле своей задачи и записать в отчёт.
- Окружение: `PY=~/venvs/h3-panel/bin/python`. Полный прогон перед каждым коммитом: `env -u NODE_OPTIONS $PY -m pytest -q -p no:cacheprovider` в форграунде, timeout 600000, 0 failed. Точечные прогоны — `env -u NODE_OPTIONS $PY -m pytest -q -p no:cacheprovider tests/<файл>::<тест>`.
- **Тест не написан, пока не видел его красным** (`CLAUDE.md`): шаг «увидеть красным» до кода и шаг мутации после — удалить/обратить защищаемую строку, увидеть красный, вернуть. Текст assertion-ошибки мутации — в отчёт задачи. Без этого задача не сдана.
- Тесты пиннят содержание: тела ответов, разметку, тела запросов — `==` целиком. Не `in`, не `len()`, не «ключ есть». Единственное исключение — проверка строки внутри системного промпта LLM (там проверяется целая строка `"\n<строка>\n" in system`).
- **JS-моки — только точное сравнение:** `closest(sel)`/`match(sel)` сравнивают `sel === "…"`, карты селекторов харнесса — по точному ключу (`Object.hasOwn`), никаких `.includes()`/`startsWith` в моках (память проекта `js-driver-mocks-need-exact-match`: три выжившие мутации за два дня).
- Моки ленивого конвейера не нужны (код задач не трогает MLX), но **порядок асинхронных ответов** — нужен: где баг зависит от порядка (`/api/state` против `/api/library`), харнесс задерживает ответ явно.
- Сообщения человеку — по-русски; комментарии, докстринги, идентификаторы — по-английски, как во всём пакете.
- Новый код ошибки — в `h3_48gb/cli.py:ERROR_CODES` (или `library.ERROR_CODES`, если так уже устроено для `library_*` — проверить, как `test_cli.py:1735` собирает коды) и статус в `web.ERROR_STATUS` (`web.py:495`), если не 400.
- CSS — только токены `:root` (`style.css:51`), без новых цветов; правила на 390 px — в существующем `@media (max-width: 420px)` (`style.css:1563`).
- Сервер alex-neuro, GPU, sglang, ComfyUI, Qwen в задачах 1–12 не трогаются. Задача 13 — живой проход координатором.
- Каждая задача — свой коммит (`git add` только своих файлов). Сообщение — по-русски, как в истории ветки.

## Review Focus

Входы, которые легко пропустить. Тест на каждый вписан в задачу-владельца.

1. **Правка сцены теряется при перерисовке.** `renderProjectModal` пересобирает `#project-body` целиком; «+ Сцена», «↑/↓», «Удалить», «случайный» обязаны сначала снять значения полей в черновик. Тест `editor_dom_edits_survive_add` (задача 7).
2. **«Утвердить» при несохранённой правке** утверждает старый сценарий. Тест `editor_approve_saves_first` и `editor_approve_stops_on_save_error` (задача 7).
3. **Перестановка сцен и стартовый кадр.** `start_image` — свойство первого кадра ролика, остаётся на позиции 0; `fresh_start` сцены, ставшей нулевой, снимается. Тест `test_move_scene_keeps_start_image_at_position_0` (задача 7).
4. **Одно правило — две реализации.** Сетка длительностей (JS-подсказка против `web._snap_video_scenes_sglang`) и каскад пересъёмки (JS против `Project.invalidate_scene_chain`) сверяются таблицей через Python и node. Тесты `test_grid_hint_matches_the_server_snap`, `test_retry_cascade_matches_invalidate_scene_chain` (задачи 7, 10).
5. **Тексты блокировки сервера и UI расходятся.** `web.PROJECT_LOCK_TEXT` и `app.PROJECT_LOCK_TEXT` сверяются равенством. Тест `test_lock_texts_are_the_same_on_both_sides` (задача 10).
6. **Отказ пересъёмки не должен иметь побочных эффектов:** проверка тела до отмены хвоста очереди. Тест `test_a_refused_retry_touches_nothing` (задача 2).
7. **Превью библиотеки и порядок ответов:** `/api/library` раньше `/api/state`. Тест `library_preview_after_late_state` (задача 9).
8. **`shares_gpu: null` — не «внешний».** Тест `test_plate_text_for_each_gpu_sharing_answer` (задача 12).
9. **Удаление карточки, подключённой к готовому проекту** — отказ, а не тихая поломка будущей пересъёмки. Тест `test_a_card_pinned_by_a_finished_project_is_not_deleted` (задача 3).

---

## Карта файлов

| Файл | Ответственность | Задачи |
|---|---|---|
| `h3_48gb/web.py` | блокировки идущего проекта, пересъёмка с правкой, DELETE карточки, `versions`, чат проекта, длительности чата по движку, `shares_gpu`, `upscale_report` | 1, 2, 3, 4, 5 |
| `h3_48gb/project.py` | `invalidate_scene_chain(idx, edits=…)` | 2 |
| `h3_48gb/library.py` | `card_history`, `delete_card`, `library_card_in_use` | 3 |
| `h3_48gb/provider.py` | `shares_gpu(cfg)` | 4 |
| `h3_48gb/cli.py` | коды ошибок | 3 |
| `h3_48gb/webui/app.js` | чистые функции разметки и обработчики | 6–12 |
| `h3_48gb/webui/index.html` | кнопки зоны «Проекты», скрытый `#scene0-file` | 6, 8 |
| `h3_48gb/webui/style.css` | правила новых и неоформленных блоков, 390 px | 6–12 |
| `tests/_ui_harness.mjs` (новый) | общий фейковый DOM/fetch для node-сценариев | 6 |
| `tests/_panel_ui_check.mjs` | переезд на харнесс без смены поведения | 6 |
| `tests/_ui_gaps_check.mjs` (новый) | сценарии проводки DOM волны 1.5 | 6–12 |
| `tests/test_project_locks.py`, `tests/test_scene_retry_edit.py`, `tests/test_library_delete.py`, `tests/test_chat_project.py`, `tests/test_upscale_report.py` (новые) | серверные тесты | 1–5 |
| `tests/test_webui_gaps.py` (новый) | чистые функции и сценарии UI | 6–12 |

## Порядок и зависимости

`1 → 2 → 3 → 4 → 5` (сервер, независимы друг от друга, но правят `web.py` — последовательно), затем `6` (харнесс и основа UI), `7 → 8` (редактор), `9`, `10` (зависит от 1 и 2), `11` (от 5), `12` (от 4), `13` — приёмка после всех.

---

## Task 0 (внешний): seed/steps сцены, сид проекта, явные refs сцены — готово

Сделано другим агентом: `4005de38` (seed/steps, seed проекта), `626db48d` (refs). В этом плане — только зависимость. Исполнитель задач плана файлы Task 0 не правит.

- [ ] **Проверка перед задачей 7:** `env -u NODE_OPTIONS $PY -m pytest -q -p no:cacheprovider tests/test_sglang_scene_params.py tests/test_sglang_scene_refs.py` — зелёный.

---

## Task 1: Идущий проект: запрет правки референсов, настроек и галочки апскейла

**Files:**
- Modify: `h3_48gb/web.py` — `_put_project_references` (`web.py:4251`), `_put_project_settings` (`web.py:3596`), `_put_project_route` (`web.py:3557`); новая константа `PROJECT_LOCK_TEXT` рядом с `ERROR_STATUS`
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


def test_route_is_open_during_scenes_and_closed_during_upscale(live):
    proj = _ready(live)
    assert _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})[0] == 200
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/route", {"upscale": False})
    assert status == 200, body
    other = p.create_project(live.outdir, "video", "Другой")
    q.submit(live.queue_root, ["upscale", "--project", str(other.path)],
             f"upscale project {other.id}",
             {"output_stem": str(other.path.parent / "upscale" / "job-upscale")}, {},
             kind=q.KIND_UPSCALE)
    status, body = _call(live, "PUT", f"/api/projects/{other.id}/route", {"upscale": False})
    assert (status, _error(body)) == (409, ("project_running", ROUTE))
    assert [e for e in p.load_project(other.path).route if e["stage"] == "upscale"] == [
        {"stage": "upscale", "enabled": True}]
```

- [ ] **Step 2: Увидеть красным**

`env -u NODE_OPTIONS $PY -m pytest -q -p no:cacheprovider tests/test_project_locks.py` — ожидается: первый и третий тесты падают (`assert (200, ...) == (409, ...)`), второй зелёный (он фиксирует, что запрет не задел свободный проект). Если третий падает на форме `route` (у созданного проекта маршрут другой) — поправить ожидание по `project.default_route("video")`, не по факту ответа.

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
(Проверить, что у `Job` есть атрибут `kind`; если нет — сравнивать по `args[0]`.)

- [ ] **Step 4: Зелёный** — тот же прогон, 3 passed.
- [ ] **Step 5: Мутация** — закомментировать вызов `_refuse_while_running(proj, "references")`, увидеть красный первого теста, вернуть; то же для `"route"` и третьего теста. Ошибки — в отчёт.
- [ ] **Step 6: Полный прогон** — 0 failed. Если упал старый тест, который правил настройки идущего проекта, — это его прежнее допущение; разобрать и сообщить координатору, не ослаблять запрет.
- [ ] **Step 7: Коммит** — `git add h3_48gb/web.py tests/test_project_locks.py && git commit -m "feat(projects): правка референсов, настроек и галочки апскейла идущего проекта — отказ project_running"`

---

## Task 2: Пересъёмка сцены с новым сидом, промптом, шагами

**Files:**
- Modify: `h3_48gb/project.py:827` (`invalidate_scene_chain`)
- Modify: `h3_48gb/web.py:5281` (`_retry_project_scene`); вынести проверки `seed`/`steps`/`prompt` из цикла `_put_project_scenes` (`web.py:4304-4346`) в помощник
- Test: `tests/test_scene_retry_edit.py` (новый)

**Interfaces:**
- Produces:
  - `Project.invalidate_scene_chain(idx: int, *, edits: dict | None = None) -> Project` — `edits` ⊆ `{"prompt", "seed", "steps"}` записываются в сцену `idx` под тем же замком и в той же записи, что сброс хвоста.
  - `web._scene_edit_fields(raw: dict, i: int, *, sglang: bool) -> dict` — проверенные `prompt`/`seed`/`steps` из `raw` (только присутствующие), тексты ошибок те же, что в `PUT …/scenes`; `_put_project_scenes` пользуется им же.
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
    """Two scenes approved; scene 0 marked done with a clip, its queued job left in place."""
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
    (job,) = _pending(live)
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
- [ ] **Step 5: Мутация** — (а) убрать `target.update(...)` → красный первого теста; (б) перенести вызов `_scene_edit_fields` после `_cancel_project_scene_tail_jobs` → красный `test_a_refused_retry_touches_nothing` (очередь изменилась до отказа); (в) убрать отказ для `pending` → красный последнего теста. Ошибки — в отчёт.
- [ ] **Step 6: Полный прогон.** Старые тесты ретрая шлют `{}` — должны остаться зелёными. Если какой-то старый тест ретраит `pending`-сцену — остановиться и сообщить координатору (это смена контракта, а не поломка теста).
- [ ] **Step 7: Коммит** — `feat(projects): пересъёмка сцены с новым промптом, сидом и шагами; не снятую сцену не пересчитываем`.

---

## Task 3: Библиотека: история версий и безопасное удаление карточки

**Files:**
- Modify: `h3_48gb/library.py` (`list_cards`, новые `card_history`, `delete_card`, код в `ERROR_CODES`)
- Modify: `h3_48gb/web.py` — `_route_delete` (`web.py:3609`), новый `_delete_card`, `ERROR_STATUS` (`library_card_in_use: 409`); `h3_48gb/cli.py:ERROR_CODES`, если коды библиотеки собираются туда
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
- Modify: `h3_48gb/web.py` — `CHAT_SOURCE_KINDS` (`web.py:268`), `_create_chat`, `_create_project` (`web.py:4408`, проверка длительностей `web.py:4478`), системный блок `## Context` (`web.py:~6113`), `_providers`
- Modify: `h3_48gb/provider.py` (новая `shares_gpu`)
- Test: `tests/test_chat_project.py` (новый)

**Interfaces:**
- Produces:
  - `POST /api/chat {source: {kind: "project", id}}` — проект должен существовать (`project_not_found` иначе); сессия получает `tags = [ref.tag for ref in proj.references]`, `source` хранится как прислан.
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
    assert (session["source"], session["tags"]) == ({"kind": "project", "id": proj.id}, ["@amazon"])


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
Помощники `srv.get_json`/`srv.post_json_raw` и сигнатуру фикстуры `_serve` взять из `tests/test_chat_web.py`/`tests/test_web_projects.py` (если у `_serve` нет `get_json` — `srv.request("GET", …)` того же файла). Строку `"\nduration: 10 s\n"` сверить с тем, как `_locked_turn` печатает длительность по умолчанию (`DEFAULT_CHAT_DURATION = 10`, формат `{duration:g}`); строка `mode:` идёт до неё и не проверяется. Плюс тест `GET /api/providers`: у каждой строки ключ `shares_gpu` равен `provider.shares_gpu(cfg)` для её записи из `load_providers(outdir)` — список значений сравнить целиком.

- [ ] **Step 2: Увидеть красным** — `args_invalid` для `source.kind="project"`, `[12.0, 3.0]` отказано по 5–10, нет строки в системном сообщении, нет `provider.shares_gpu`.
- [ ] **Step 3: Реализация** — по Interfaces. Длительность в `_create_project`: `low, high = (sglang_args.MIN_SECONDS, sglang_args.MAX_SECONDS) if engine.is_sglang() else (SCENE_MIN_SECONDS, SCENE_MAX_SECONDS)`; текст отказа прежний с подставленными границами. Теги: до `create_project` — `pinned = [{"tag": c["tag"], "version": c["version"]} for c in (self._library_call(library_module.get_card, outdir, t) for t in session.get("tags") or [])]`, после — `proj.references = pinned` до `proj.save()` (проверить, что `save()` пишет `references`). Хост для `shares_gpu` — `urllib.parse.urlsplit(base_url).hostname`. Если `load_providers` отбрасывает незнакомые ключи записи — разрешить `shares_gpu` там же (bool), иначе `None`.
- [ ] **Step 4: Зелёный.**
- [ ] **Step 5: Мутация** — вернуть `SCENE_MIN/MAX` в `_create_project` → красный второго теста; убрать строку контекста → красный параметризации; убрать ветку `localhost` → красный `test_shares_gpu[...localhost...]`.
- [ ] **Step 6: Полный прогон** (особенно `tests/test_web_projects.py` — C3 про 5–10 на MLX должен остаться зелёным).
- [ ] **Step 7: Коммит** — `feat(chat): чат проекта, длительности чат-проекта по движку и его теги, честный shares_gpu у провайдера`.

---

## Task 5: Отчёт апскейла в выдаче проекта

**Files:**
- Modify: `h3_48gb/web.py:2233` (`_project_payload`)
- Test: `tests/test_upscale_report.py` (новый)

**Interfaces:**
- Produces: в каждом ответе с проектом `project.upscale_report` = `{"status", "strength", "motion", "parts": [idx…], "error"}` из `<project>/upscale/report.json` (`ltx._write_report`), где отсутствующие ключи → `None`, `parts` — `[part["idx"] for part in report["parts"]]`; нет файла или он не читается как JSON-объект → `None`.

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
        "status": "done", "strength": 0.6, "motion": 0.12, "parts": [0, 1], "error": None})


def test_a_failed_report_carries_its_error(live):
    proj = p.create_project(live.outdir, "video", "Бой")
    _report(proj, {"attempt": "a1", "parts": [], "status": "failed", "error": "ComfyUI 500"})
    assert _call(live, "GET", f"/api/projects/{proj.id}")[1]["project"]["upscale_report"] == {
        "status": "failed", "strength": None, "motion": None, "parts": [], "error": "ComfyUI 500"}


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
- [ ] **Step 5: Мутация** — `"parts": report.get("parts")` (без извлечения `idx`) → красный первого теста.
- [ ] **Step 6: Полный прогон** (тесты, сравнивающие весь `project`-payload целиком, получат новый ключ — дописать `"upscale_report": None` в их ожидание и перечислить такие тесты в отчёте).
- [ ] **Step 7: Коммит** — `feat(projects): upscale_report (сила, движение, части, ошибка) в выдаче проекта`.

---

## Task 6: UI-основа: общий харнесс, «+ Новый ролик», «Ролик через диалог», CSS неоформленных блоков

**Files:**
- Create: `tests/_ui_harness.mjs` (вынести из `tests/_panel_ui_check.mjs` строки 10–90 без изменения поведения + расширения ниже)
- Modify: `tests/_panel_ui_check.mjs` (импорт харнесса вместо своих копий)
- Create: `tests/_ui_gaps_check.mjs`, `tests/test_webui_gaps.py`
- Modify: `h3_48gb/webui/index.html:390-398` (шапка зоны «Проекты»), `h3_48gb/webui/app.js` (обработчики, `defaultProjectTitle`), `h3_48gb/webui/style.css`

**Interfaces:**
- Produces (харнесс, `tests/_ui_harness.mjs`): `routes`, `calls` (`{method, url, body, headers}`; не-JSON тело → `body: {raw: <имя файла или "blob">}`), `alerts`, `confirms`, `prompts`, `answers` (`{confirm: true, prompt: null}`), `queryAll`/`queryOne` (точный селектор → фейковые элементы; пусто/`null` по умолчанию), `getElementById`, `fire(type, target)`, `clickable(props)`, `ok`, `err`, `sleep`, `countCalls`, `PROJECT(over)`, `SGLANG`, `start(appUrl, extra)`. Маршрут fetch может быть функцией, возвращающей **промис** ответа (для задержек).
- Produces (`app.js`): `export function defaultProjectTitle(date: Date) -> string` — `"Ролик ДД.ММ ЧЧ:ММ"` по локальному времени; `export function newVideoRequest(title: string, now: Date) -> {kind: "video", title}`.
- Produces (`index.html`, точная разметка в шапке зоны «Проекты» после `#projects-sum`):
  `<button class="ghost" id="project-new-video" type="button">+ Новый ролик</button>` и
  `<button class="ghost" id="project-new-chat" type="button">Ролик через диалог</button>`; `#projects-empty`: «Проектов пока нет — «+ Новый ролик»».

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
    "app.projectSettingsHtml({id: 'p1', i2v_prefix: '', seed: null})",
    "app.projectReferencesHtml({id: 'p1'}, [{tag: '@a', kind: 'person', version: 1, "
    "latest_version: 1, versions: []}], [{tag: '@a', version: 1}])",
    "app.projectRouteHtml({id: 'p1', route: [{stage: 'upscale', enabled: true}]}, 'sglang')",
    "app.projectUpscaleHtml({id: 'p1', stages: {upscale: 'failed'}, "
    "route: [{stage: 'upscale', enabled: true}]}, 'sglang')",
    "app.projectTagWarningsHtml({kind: 'video', references: [], scenes: [{idx: 0, prompt: 'x'}]}, "
    "'sglang')",
]


def _classes(html: str) -> list[str]:
    return sorted({c for group in re.findall(r'class="([^"]*)"', html) for c in group.split()})


@_needs_node
def test_every_class_the_new_blocks_emit_has_a_rule():
    html = _js("[" + ",".join(CLASS_SOURCES) + "].join('')")
    css = _page_text("style.css")
    missing = [c for c in _classes(html)
               if not re.search(r"\." + re.escape(c) + r"(?![\w-])", css)]
    assert missing == []


@_needs_node
def test_default_project_title_and_request():
    when = "new Date(2026, 9, 7, 14, 5)"
    assert _js(f"app.defaultProjectTitle({when})") == "Ролик 07.10 14:05"
    assert _js(f"app.newVideoRequest('  ', {when})") == {"kind": "video", "title": "Ролик 07.10 14:05"}
    assert _js(f"app.newVideoRequest(' Бой ', {when})") == {"kind": "video", "title": "Бой"}


def test_the_projects_zone_has_the_two_new_buttons():
    page = _page_text("index.html")
    assert ('<button class="ghost" id="project-new-video" type="button">+ Новый ролик</button>'
            in page)
    assert ('<button class="ghost" id="project-new-chat" type="button">Ролик через диалог</button>'
            in page)


@_needs_node
def test_new_video_creates_and_opens_the_project():
    assert _gaps("new_video") == {
        "prompts": ["Название ролика (пусто — по дате):"],
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
- [ ] **Step 3: Увидеть красным** — `test_every_class…`: `assert ['lib-card', 'muted', 'project-refs', 'project-settings', 'route-upscale', 'route-upscale-box', 'tag-warnings', 'upscale-retry', 'upscale-status', …] == []` (фактический список — в отчёт); остальные — нет функций/кнопок, сценарии падают `Cannot read properties of undefined (reading '0')`.
- [ ] **Step 4: Реализация.** `index.html` — две кнопки; `app.js` — две функции наверху, обработчики:
```js
  $("project-new-video").addEventListener("click", async () => {
    const title = window.prompt("Название ролика (пусто — по дате):", "");
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
- [ ] **Step 6: Мутация** — удалить правило `.lib-card` → красный `test_every_class…` с `['lib-card']`; в `newVideoRequest` убрать `.trim()` → красный `test_default_project_title_and_request`.
- [ ] **Step 7: Полный прогон; коммит** — `feat(webui): «+ Новый ролик» и «Ролик через диалог» в зоне проектов, общий node-харнесс, CSS референсов и настроек проекта`.

---

## Task 7: Редактор сцен видеопроекта — ядро (промпт, длительность с сеткой, fresh_start, сид, шаги, порядок, сохранение)

**Files:**
- Modify: `h3_48gb/webui/app.js` — новые чистые функции наверху; `projectScriptStageHtml` (`app.js:3035`) показывает редактор для видео при `stages.scenes === "draft"` и `stages.script ∈ {draft, awaiting_approval}`; обработчики; состояние `sceneDraft`, `sceneDraftDirty`; `approve-script` сохраняет черновик первым
- Modify: `h3_48gb/webui/style.css`; `tests/_ui_gaps_check.mjs`, `tests/test_webui_gaps.py`

**Interfaces:**
- Consumes: Task 0 (`seed`, `steps`, `project.seed`), `PUT /api/projects/<id>/scenes`.
- Produces (все `export function` в `app.js`):
  - `sceneBounds(engine) -> {min, max}` — `{min: 3, max: 15}` на `"sglang"`, иначе `{min: 5, max: 10}`.
  - `sglangGridSeconds(duration, chained) -> number` — зеркало `web._snap_video_scenes_sglang` для одной сцены (24 к/с, `17n+5` запрошенных кадров, у сцепленной поставка на кадр меньше, пределы 73..345 запрошенных).
  - `gridHint(seconds) -> string` — `"на сетке: 5,17 с"` (`toFixed(2)`, хвостовые нули и запятая убираются: `8` → `"на сетке: 8 с"`).
  - `draftFromScenes(scenes) -> Draft[]`, где `Draft = {prompt, duration, fresh_start, seed, steps, start_image, refs}` (`null` для отсутствующих `seed/steps/start_image`, `[]` для `refs`, `false` для `fresh_start`).
  - `addScene(draft)`, `removeScene(draft, idx)`, `moveScene(draft, idx, delta)` — новые массивы; новая сцена `{prompt: "", duration: <последней или 8>, …}`; при перестановке `start_image` остаётся у позиции 0, у новой нулевой `fresh_start = false`.
  - `scenesClientError(draft, engine) -> string | null` — `"Сцена #N: пустой промпт"`, `"Сцена #N: длительность 3–15 с"` (границы `sceneBounds`), первая найденная.
  - `scenesPayload(draft, engine) -> {scenes: […]}` — `prompt`, `duration` всегда; `fresh_start` у `idx > 0` всегда (bool); `seed` если не `null`; `steps` если не `null` и `engine === "sglang"`; `start_image` только у 0 и если задан; `refs` если непуст (задача 8).
  - `randomSeed(rand = Math.random) -> number` — `Math.floor(rand() * 2 ** 31)`.
  - `seedPlaceholder(projectSeed) -> string` — `"по проекту: 305"` / `"по умолчанию: 42"`.
  - `sceneEditorHtml(draft, ctx) -> string`, `ctx = {id, engine, projectSeed, dirty}`; поля несут `data-scene-field="<поле>"` и `data-idx`, кнопки — `button[data-act]` со значениями `scene-up`, `scene-down`, `scene-del`, `scene-seed-random`, `scene-add`, `scenes-save`.
- DOM-половина: `syncDraftFromDom()` читает `document.querySelectorAll("#project-body [data-scene-field]")` (селектор ровно такой) **перед** любой структурной правкой и сохранением; `sceneDraft` сбрасывается из сервера при `openProjectModal` и после успешного `PUT`.

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
    assert _js(f"app.scenesPayload({DRAFT}, 'mlx')")["scenes"][1] == {
        "prompt": "@a runs", "duration": 5, "fresh_start": True, "seed": 7}


@_needs_node
def test_move_scene_keeps_start_image_at_position_0():
    assert _js(f"app.moveScene({DRAFT}, 1, -1)") == [
        {"prompt": "@a runs", "duration": 5, "fresh_start": False, "seed": 7, "steps": 30,
         "start_image": "@arena", "refs": []},
        {"prompt": "@a walks", "duration": 8, "fresh_start": False, "seed": None, "steps": None,
         "start_image": None, "refs": []}]
    assert _js(f"app.moveScene({DRAFT}, 0, -1)") == _js(DRAFT)   # off the edge: unchanged
    assert [s["prompt"] for s in _js(f"app.removeScene({DRAFT}, 0)")] == ["@a runs"]
    assert _js(f"app.removeScene({DRAFT}, 0)")[0]["start_image"] == "@arena"
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
        '<span class="hint">на сетке: 5,17 с</span>'
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
Точную разметку `sceneEditorHtml` выше исполнитель **может** поменять (порядок атрибутов, классы), но тогда тест обновляется на новую разметку целиком, и в отчёте — почему; правила, которые пиннит тест (экранирование промпта, `min/max` из `sceneBounds`, подсказка сетки, `placeholder` сида и шагов, `disabled` у крайних стрелок, нет «Удалить» у единственной сцены, нет `fresh_start` у сцены 0), — обязательны. Второй вариант того же теста для двух сцен на MLX: у сцены #1 есть `fresh_start`-чекбокс (`data-scene-field="fresh_start"`) и «Удалить» (`data-act="scene-del"`), нет поля шагов и подсказки сетки, `min="5" max="10"`.

Сценарии в `tests/_ui_gaps_check.mjs` (+ тесты-обёртки в `test_webui_gaps.py` с точными ожиданиями):
```js
const DRAFT_PROJECT = PROJECT({ stages: { script: "awaiting_approval", scenes: "draft",
  upscale: "draft", assembly: "draft" }, scenes: [
  { idx: 0, prompt: "@a walks", duration: 8, status: "pending", job_id: null, clip_path: null,
    keyframe_path: null, start_image: "@arena" },
  { idx: 1, prompt: "@a runs", duration: 5, status: "pending", job_id: null, clip_path: null,
    keyframe_path: null, fresh_start: true }], references: [{ tag: "@a", version: 1 }] });
const open = async () => {
  fire("click", clickable({ dataset: { act: "open-project", id: "p1" }, match: (s) => s === "button[data-act]" }));
  await sleep(80);
};
const act = async (name, extra = {}) => {
  fire("click", clickable({ dataset: { act: name, id: "p1", ...extra }, match: (s) => s === "button[data-act]" }));
  await sleep(80);
};
const puts = () => calls.filter((c) => c.method === "PUT").map((c) => [c.url, c.body]);
// SCENARIOS:
async editor_dom_edits_survive_add() {
  await start(appUrl, { "GET /api/projects/p1": ok(DRAFT_PROJECT),
    "PUT /api/projects/p1/scenes": ok({ ok: true, project: DRAFT_PROJECT.project }) });
  await open();
  queryAll["#project-body [data-scene-field]"] = [
    { dataset: { sceneField: "prompt", idx: "0" }, value: "@a jumps" },
    { dataset: { sceneField: "seed", idx: "1" }, value: "305" }];
  await act("scene-add");
  queryAll["#project-body [data-scene-field]"] = [
    { dataset: { sceneField: "prompt", idx: "2" }, value: "@a rests" }];
  await act("scenes-save");
  return { puts: puts() };
},
async editor_approve_saves_first() { /* правка промпта сцены 0, клик approve-script → [PUT scenes, POST approve/script] в этом порядке */ },
async editor_approve_stops_on_save_error() { /* PUT → 400 args_invalid → POST approve не уходит, ошибка в #project-err */ },
async editor_client_error() { /* scene-add и save без промпта → PUT не уходит, #project-err = «Сцена #2: пустой промпт» */ },
```
Ожидание `editor_dom_edits_survive_add`:
```python
assert _gaps("editor_dom_edits_survive_add") == {"puts": [["/api/projects/p1/scenes", {"scenes": [
    {"prompt": "@a jumps", "duration": 8, "start_image": "@arena"},
    {"prompt": "@a runs", "duration": 5, "fresh_start": True, "seed": 305},
    {"prompt": "@a rests", "duration": 5, "fresh_start": False}]}]]}
```
Ожидание `editor_approve_saves_first` — `{"calls": [["PUT", "/api/projects/p1/scenes"], ["POST", "/api/projects/p1/approve/script"]]}` (отфильтрованные не-GET вызовы в порядке); `editor_approve_stops_on_save_error` — `{"calls": [["PUT", "/api/projects/p1/scenes"]], "errorHidden": False}`.

- [ ] **Step 2: Увидеть красным** — `app.sglangGridSeconds is not a function` и т.д.; сценарии: `puts: []`.
- [ ] **Step 3: Реализация** — функции по Interfaces; в DOM-половине:
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
- [ ] **Step 4: Зелёный.** CSS для `scene-editor`, `scene-edit`, `scene-edit-head`, `scene-edit-row`, `scene-editor-acts`, `dirty-note`, `hint` (если нет) — и `CLASS_SOURCES` дополняется вызовом `sceneEditorHtml` (обе версии).
- [ ] **Step 5: Мутация** — (а) в `moveScene` не переносить `start_image` → красный `test_move_scene_keeps_start_image_at_position_0`; (б) убрать `syncDraftFromDom()` из обработчика `scene-add` → красный `editor_dom_edits_survive_add`; (в) убрать сохранение перед утверждением → красный `editor_approve_saves_first`; (г) в `sglangGridSeconds` взять остаток `5` и для сцепленной → красный `test_grid_hint_matches_the_server_snap`.
- [ ] **Step 6: Полный прогон; коммит** — `feat(webui): редактор сцен видеопроекта — промпт целиком, длительность с сеткой, сид, шаги, порядок, сохранение`.

---

## Task 8: Редактор сцен — референсы, `@`-подсказка, стартовый кадр сцены 0, вставка JSON

**Files:**
- Modify: `h3_48gb/webui/app.js`, `h3_48gb/webui/index.html` (скрытый `<input type="file" id="scene0-file" accept="image/png,image/jpeg" hidden>` внутри `#project-modal`), `style.css`, `tests/_ui_gaps_check.mjs`, `tests/test_webui_gaps.py`

**Interfaces:**
- Consumes: `tagSuggestions` (`app.js:223`), `sceneTagIssues` (`app.js:179`), `POST /api/uploads` (тело — файл, заголовок `X-Filename`), Task 0 `refs`.
- Produces:
  - `insertTagAt(text, caret, tag) -> {text, caret}` — заменяет недописанный `@…` перед кареткой на `tag + " "`; без `@` перед кареткой — вставляет `tag + " "` в каретку.
  - `tagHintHtml(text, caret, cards) -> string` — `""` или `<div class="tag-hint">` с кнопками `<button type="button" class="tag-pick" data-act="tag-pick" data-tag="@x">@x</button>`.
  - `startImageFieldHtml(current, pinnedCards, outdir) -> string` — `select[data-scene-field="start_image"][data-idx="0"]` с вариантами `""` («без кадра»), каждый не-voice тег (`"@arena — кадр карточки"`), текущий путь (если это путь — подпись по имени файла); кнопка `data-act="scene0-upload"`; миниатюра `<img class="start-thumb" …>` выбранного (для тега — первая картинка карточки через `/media/…`, для пути — `/media/` + путь относительно `outdir`).
  - `sceneRefsHtml(scene, idx, pinnedTags) -> string` — чекбоксы `data-scene-field="refs"` `data-tag`, только sglang, с подсказкой `<span class="hint">их картинки идут первыми: &lt;Picture 1…&gt;</span>`; `syncDraftFromDom` собирает отмеченные в `refs` по порядку подключения.
  - `sceneTagIssues(text, pinnedTags, {needsTag, refs = []})` — при непустом `refs` проблема `missing` не выдаётся (сцена с референсами без упоминания — законна, `626db48d`); `projectTagWarningsHtml` передаёт `scene.refs`. Тест: `app.sceneTagIssues('a cat', ['@a'], {needsTag: true, refs: ['@a']})` == `[]`, а без `refs` — `[{"tag": None, "problem": "missing"}]`.
  - `parseScenarioJson(text) -> {body, error}` — список → `{body: {scenes: список}}`; объект со `scenes`-списком → `{body: {scenes, references?}}` (только эти два ключа); иначе `{error: "Ожидается {\"scenes\": […]} или список сцен"}`; не JSON → `{error: "JSON не разобрался — проверьте запятые и кавычки"}`.
  - Блок `<details class="adv scenario-json">` с `<textarea class="inp scenario-json-text">` и кнопкой `data-act="scenario-json-load"`: `confirm("Заменить N сцен сценария?")` если сцены есть → `PUT …/scenes` с `body` как есть → черновик из ответа.

- [ ] **Step 1: Тесты**
```python
@_needs_node
def test_insert_tag_at_the_caret():
    assert _js("app.insertTagAt('@ama walks', 4, '@amazon')") == {"text": "@amazon  walks", "caret": 8}
    assert _js("app.insertTagAt('walks ', 6, '@amazon')") == {"text": "walks @amazon ", "caret": 14}


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
```
(в начало файла — `import pytest`.) Плюс точная разметка `startImageFieldHtml` для трёх случаев (ничего / `@arena` / загруженный путь `/o/uploads/open.png` при `outdir="/o"`) и `sceneRefsHtml`. Сценарии:
- `scene0_upload`: открыть черновой проект, `getElementById("scene0-file").files = [{ name: "open.png" }]`, клик `scene0-upload` (кликает по `#scene0-file`), затем `getElementById("scene0-file").__listeners.change[0]()`; маршрут `POST /api/uploads` → `ok({ok: true, path: "/o/uploads/open.png"})`; клик `scenes-save`. Ожидание: `{"upload": {"url": "/api/uploads", "headers": {"Content-Type": "application/octet-stream", "X-Filename": "open.png"}, "body": {"raw": "open.png"}}, "start_image": "/o/uploads/open.png"}` где `start_image` — из тела `PUT`.
- `tag_pick`: `queryOne["#project-body .scene-edit-prompt[data-idx=\"0\"]"]` — фейковое поле `{value: "fight @a", selectionStart: 8, dataset: {idx: "0"}, focus() {}}`, клик `tag-pick` с `data-tag="@amazon"` и `data-idx="0"` → значение поля `"fight @amazon "`, черновик помечен изменённым (сохранение шлёт новый промпт).
- `json_load`: `queryOne["#project-body .scenario-json-text"] = {value: '{"scenes": [{"prompt": "@a", "duration": 5}]}'}`, `answers.confirm = true`, клик `scenario-json-load` → `confirms == ["Заменить 2 сцены сценария?"]`, `puts == [["/api/projects/p1/scenes", {"scenes": [{"prompt": "@a", "duration": 5}]}]]`; вариант `json_load_bad` с `'42'` → `puts == []`, текст ошибки в `#project-err`.
- [ ] **Step 2: Увидеть красным.**
- [ ] **Step 3: Реализация.** Загрузку файла (`fetch("/api/uploads", {method: "POST", body: file, headers: {...}})`) вынести из `addLibraryCard` (`app.js:2747`) в `uploadToServer(file) -> path` и использовать в обоих местах. Обработчик `input` для `.scene-edit-prompt` — тот же, что для `.scenario-prompt` (`app.js:5111`), но подсказка рисуется `tagHintHtml` (кнопки), а не текстом.
- [ ] **Step 4: Зелёный; CSS** (`tag-hint`, `tag-pick`, `start-thumb`, `scene-refs`, `scenario-json`) + `CLASS_SOURCES`.
- [ ] **Step 5: Мутация** — в `insertTagAt` не удалять недописанный хвост → красный; в `scenesPayload` не слать `start_image` → красный `scene0_upload`; убрать `confirm` перед заменой → красный `json_load`.
- [ ] **Step 6: Полный прогон; коммит** — `feat(webui): редактор сцен — @-подсказка кнопками, референсы сцены, стартовый кадр сцены 0, вставка сценария JSON`.

---

## Task 9: Библиотека в UI: превью при загрузке, новая версия с файлом, выбор версии, удаление

**Files:**
- Modify: `h3_48gb/webui/app.js` — `libraryCardsHtml` (`app.js:304`), `projectReferencesHtml` (`app.js:239`), `referencesPayload` (`app.js:298`), `loadLibrary` (`app.js:2742`), `poll`; `style.css`; тесты

**Interfaces:**
- Consumes: `versions` и `DELETE /api/library/<name>` (задача 3), блокировка `references` (задача 1).
- Produces:
  - `libraryCardsHtml(cards, outdir)` — миниатюра `<img class="lib-thumb" src="/media/…" alt="">`, подпись `"<kind>, v<N>"` (+ `" · версий: K"` при K > 1), поле `<input class="lib-new-files" type="file" multiple accept=".png,.jpg,.jpeg,.mp3,.wav">`, кнопки `data-act="lib-new-version"` и `data-act="lib-delete"` (у обеих `data-tag`).
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
        '<b>@alice</b> <span class="muted">person, v2 · версий: 2</span><p>a woman</p>'
        '<input class="lib-edit-desc" value="a woman"> '
        '<button type="button" class="lib-save" data-tag="@alice">Сохранить описание</button>'
        '<input class="lib-new-files" type="file" multiple accept=".png,.jpg,.jpeg,.mp3,.wav"> '
        '<button type="button" class="ghost" data-act="lib-new-version" data-tag="@alice">Новая версия</button> '
        '<button type="button" class="ghost" data-act="lib-delete" data-tag="@alice">Удалить</button>'
        '<p class="why lib-card-error" hidden></p></div>')
```
Плюс точная разметка `projectReferencesHtml` с двумя версиями (выбрана v1 при `latest_version` 2) и с `lock`. Сценарии:
- `library_preview_after_late_state`: `routes["GET /api/state"] = () => sleep(40).then(() => ok(SGLANG))` (SGLANG.outdir = "/o"), `GET /api/library` отвечает сразу карточкой с `assets: ["/o/library/alice/v1/01-a.png"]`; после `start` и `sleep(120)` — `{"img": <первое вхождение <img …> в #library-cards>}` == `'<img class="lib-thumb" src="/media/library/alice/v1/01-a.png" alt="">'`.
- `library_delete_in_use`: `answers.confirm = true`, `DELETE /api/library/alice` → `err(409, "library_card_in_use", "@alice подключена к проектам: «Бой» (p1) — отключите её там или удалите проекты")`; карточка находится через `button.closest(".lib-card")` (мок: `closest(sel) { return sel === "button[data-act]" ? this : sel === ".lib-card" ? card : null; }`). Ожидание: `confirms == ["Удалить карточку @alice? Файлы уйдут в library/.trash."]`, `cardError == {"hidden": False, "textContent": "<то же сообщение>"}`, `deletes == ["/api/library/alice"]`.
- `library_new_version`: у карточки `querySelector(".lib-new-files")` → `{files: [{name: "b.png"}]}`, `querySelector(".lib-edit-desc")` → `{value: "a woman"}`; `POST /api/uploads` → `ok({path: "/o/uploads/b.png"})`; ожидание `puts == [["/api/library/alice", {"description": "a woman", "assets": ["/o/uploads/b.png"]}]]`.
- `refs_version_change`: открыть проект, `change` на `select.ref-version` (`{classList: {contains: (c) => c === "ref-version"}, dataset: {tag: "@a"}, value: "1", …}`) при отмеченной `@a` → `PUT /api/projects/p1/references` с `[{"tag": "@a", "version": 1}]`.
- [ ] **Step 2: Красный** (в частности `library_preview_after_late_state` на текущем коде: `img: null`).
- [ ] **Step 3: Реализация.** Обработчики `lib-new-version`/`lib-delete` — в главном делегированном `click`; `confirm` удаления: `"Удалить карточку <tag>? Файлы уйдут в library/.trash."`.
- [ ] **Step 4: Зелёный; CSS** (`lib-thumb` 96×96 `object-fit: cover`, `lib-new-files`, `ref-version`, `lock-note`) + `CLASS_SOURCES`.
- [ ] **Step 5: Мутация** — убрать перерисовку из `poll` → красный `library_preview_after_late_state`; в `referencesPayload` поставить подключённую версию выше `chosen` → красный.
- [ ] **Step 6: Полный прогон** (в т.ч. `tests/test_webui_panel.py::…library_save…` — разметка карточки поменялась, сценарий `library_save` ищет поля через `querySelector` по классу, должен остаться зелёным); **коммит** — `feat(webui): превью библиотеки при загрузке, новая версия карточки с файлом, выбор версии в проекте, удаление карточки`.

---

## Task 10: Прогон: прогресс на sglang, плашка «кто держит карту», блокировки, пересъёмка с правкой

**Files:**
- Modify: `h3_48gb/webui/app.js` — `gpuBanner` (`app.js:73`), `renderRunning` (`app.js:3566`), `projectSceneCardHtml` (`app.js:3406`, становится экспортируемой `sceneCardHtml`), `projectSettingsHtml` (`app.js:214`), обработчик `retry-scene` (`app.js:5027`); `style.css`; тесты

**Interfaces:**
- Consumes: задачи 1 (`PROJECT_LOCK_TEXT`), 2 (тело ретрая), Task 0 (`seed`, `steps`, `project.seed`).
- Produces:
  - `export const PROJECT_LOCK_TEXT` — те же три строки, что `web.PROJECT_LOCK_TEXT`.
  - `projectLocks(proj, activeJob) -> {references, settings, route}` — текст или `null`: `references`/`settings` при любом `activeJob`; `route` при `activeJob.kind ∈ {"upscale", "assembly"}`.
  - `sglangRunView(job, nowMs) -> {spec, elapsed, total, share, leftSeconds, waiting}` — `spec = "<W>×<H> · <dur> с · сид <seed> · <steps> шагов"` из `--width/--height/--duration/--seed/--steps` аргументов (`dur` — `gridHint`-формат без «на сетке: »), `elapsed = formatDuration(сек от started_at)`, `total = "≈" + formatDuration(estimate.seconds)`, `share` — целый процент `min(99, floor(100·прошло/оценка))` (0 без оценки), `leftSeconds = max(0, оценка − прошло)`, `waiting = Boolean(job.wait_reason)`.
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
Плюс точная разметка `sceneCardHtml` для `pending` (без кнопки), `done` с `ltx_path` и промптом длиннее 260, `failed`; `retryPanelHtml` для sglang. Сценарии:
- `retry_with_new_seed`: проект с двумя `done`-сценами, клик `retry-scene` (`data-idx="0"`) → панель открыта (в `#project-body` есть `class="retry-panel"`), `queryOne["#project-body .retry-panel"]` отдаёт поля через `querySelector(".retry-prompt"|".retry-seed"|".retry-steps")` (точные ключи), сид `"7"`, клик `retry-scene-go` → `posts == [["/api/projects/p1/scenes/0/retry", {"seed": 7}]]`, `confirms == []` (подтверждение — сама панель).
- `locked_settings_disabled`: `GET /api/projects/p1` с `active_job: {kind: "scene", idx: 0, job: {...}}` → в `#project-body` у `textarea.i2v-prefix` есть `disabled`, у `.route-upscale-box` — нет, есть `<p class="why lock-note">` с текстом `PROJECT_LOCK_TEXT.settings`.
- [ ] **Step 2: Красный.**
- [ ] **Step 3: Реализация.** `renderRunning`: при `state.engine === "sglang"` ветка на `sglangRunView` (ячейки «Идёт», «Оценка», «Доля», «Кончится» (`formatClock(now + leftSeconds)`), без «Проход»/«Пик памяти»; при `waiting` — «ждёт карту» вместо доли; `rail` — та же доля). `renderGpu` передаёт `state.projects`. `projectSettingsHtml(proj, lock)` — `i2v_prefix` + сид проекта (`data-act="project-seed-save"`, `PUT …/settings {seed}`, «сохранено ✓» после ответа), без «Черновой сборки» (она уезжает в задачу 11).
- [ ] **Step 4: Зелёный; CSS** (`retry-panel`, `scene-prompt`, `lock-note`, `run-wait`) + `CLASS_SOURCES`.
- [ ] **Step 5: Мутация** — `retryCascade` без остановки на `fresh_start` → красный сверки с Python; ветка `run && !run.wait_reason` убрана → красный баннера; `share` без `min(99, …)` при прошедшем > оценки — добавить строку теста с `now` позже оценки и увидеть 100 → вернуть.
- [ ] **Step 6: Полный прогон** (`tests/test_webui_panel.py` — старые тесты баннера с `gpuBanner(gpu, now)` без третьего аргумента должны остаться зелёными); **коммит** — `feat(webui): прогресс сцены на sglang, кто держит карту во время прогона, блокировки идущего проекта, пересъёмка с новым сидом и промптом`.

---

## Task 11: Апскейл и сборка в UI: сила и части, «Скачать», черновая сборка по делу, 404 job-upscale

**Files:**
- Modify: `h3_48gb/webui/app.js` — `projectUpscaleHtml` (`app.js:258`), `projectAssemblyStageHtml` (`app.js:3469`, становится экспортируемой `projectAssemblyHtml`), `isProjectPipelineNote` (`app.js:1036`), `finishedRowHtml` (`app.js:1214`); `style.css`; тесты

**Interfaces:**
- Consumes: `upscale_report` (задача 5).
- Produces:
  - `projectUpscaleHtml(proj, engine)` — `"Апскейл LTX: <слово>"` + при отчёте `done`: `" · сила 0,6 · 3 из 3 частей"` (части = сцены с `ltx_path` из `proj.scenes`, всего = сцен); при `failed`: `<p class="why upscale-error">ошибка</p>` и «Повторить апскейл».
  - `projectAssemblyHtml(proj, outdir)` — финал: `final.mp4` + `<a class="ghost" href="<url>" download="<slug(title)>-final.mp4">Скачать</a>`; «Черновая сборка» (`class="draft-assembly"`) только при всех сценах `done` и `stages.assembly !== "running"`; блок рисуется и при `draft` без финала, если черновая сборка доступна.
  - `isProjectPipelineNote` — также `^upscale project \S+$`.
  - `finishedRowHtml` — у `kind === "assemble"` нет кнопок `chat` и `dup`.

- [ ] **Step 1: Тесты**
```python
@_needs_node
def test_pipeline_notes_include_the_upscale():
    assert _js("['upscale project 20261007-ab', 'project scene p #1', 'assemble project p', "
               "'upscale projects x'].map(app.isProjectPipelineNote)") == [True, True, False, False]


@_needs_node
def test_upscale_line_shows_strength_and_parts():
    proj = ("{id: 'p1', stages: {upscale: 'done'}, route: [{stage: 'upscale', enabled: true}], "
            "scenes: [{idx: 0, ltx_path: '/a'}, {idx: 1, ltx_path: '/b'}, {idx: 2}], "
            "upscale_report: {status: 'done', strength: 0.6, motion: 0.1, parts: [0, 1, 2], error: null}}")
    assert _js(f"app.projectUpscaleHtml({proj}, 'sglang')") == (
        '<div class="upscale-status" data-id="p1">Апскейл LTX: готов · сила 0,6 · 2 из 3 частей</div>')


@_needs_node
def test_assembly_offers_download_and_hides_draft_until_scenes_are_done():
    base = ("{id: 'p1', title: 'Бой на арене', stages: {assembly: 'done'}, "
            "assembly: {final_path: '/o/projects/p1/assembly/final.mp4', v: 5}, "
            "scenes: [{idx: 0, status: 'done'}]}")
    html = _js(f"app.projectAssemblyHtml({base}, '/o')")
    assert ('<a class="ghost" href="/media/projects/p1/assembly/final.mp4?v=5" '
            'download="boj-na-arene-final.mp4">Скачать</a>') in html   # one exact tag inside the block
    pending = ("{id: 'p1', title: 'T', stages: {assembly: 'draft'}, assembly: {}, "
               "scenes: [{idx: 0, status: 'done'}, {idx: 1, status: 'running'}]}")
    assert _js(f"app.projectAssemblyHtml({pending}, '/o')") == ""
```
Ожидаемый `href` сверить с тем, что даёт `projectMediaUrl` (`app.js:1477`) для этого пути, а имя файла — с `normalizeSlug` (`app.js:2039`) для «Бой на арене»; после сверки **второй** `assert` о `html` заменить на полное равенство всего блока (`==`), а не `in` — этот `in` в плане стоит только потому, что точную обёртку блока определяет реализация; в отчёте показать итоговую строку. Плюс полная разметка «готово» без отчёта и `failed` с ошибкой; `finishedRowHtml` для `assemble`-задачи — кнопки `reveal` и `delrun` есть, `chat`/`dup` нет (полное равенство блока `<div class="acts">…</div>`, вырезанного регуляркой).
- [ ] **Step 2: Красный.**
- [ ] **Step 3: Реализация.**
- [ ] **Step 4: Зелёный; CSS** (`upscale-error`, `proj-final` при необходимости) + `CLASS_SOURCES`.
- [ ] **Step 5: Мутация** — убрать `^upscale project` из фильтра → красный; показывать «Черновую сборку» всегда → красный второго `assert`.
- [ ] **Step 6: Полный прогон; коммит** — `feat(webui): сила и части апскейла, «Скачать» финал, черновая сборка только по готовым сценам, без 404 job-upscale в «Готово»`.

---

## Task 12: LLM-путь в UI: «Чат по сценарию», «Применить к проекту», честная подпись провайдера

**Files:**
- Modify: `h3_48gb/webui/app.js` — `llmPlateText` (`app.js:2304`), `renderLlmPlate` (`app.js:3879`), `projectScriptStageHtml`, обработчик `chat-make-project` (`app.js:5382`)/`chat-project-create` (`app.js:5384`); `style.css`; тесты

**Interfaces:**
- Consumes: задача 4 (`source.kind = "project"`, `shares_gpu`).
- Produces:
  - `llmPlateText(status, {sharesGpu, external, runningSeconds})` — `sharesGpu === true` → `"делит видеокарту с H3: пока модель поднята, рендер ждёт"` + при `status === "up"` `" (сейчас поднята)"`; `sharesGpu === false` и не локальная → `"внешний провайдер — память этой машины не занимает"`; `sharesGpu === null` и не локальная → `"где считает провайдер, не указано (shares_gpu в providers.json)"`; локальная (`llama-local`) — прежние тексты по `status`.
  - `chatApplyBody(chatProject) -> {scenes: [{prompt, duration}]}` — из `chat.project.scenes`, только эти два поля.
  - В этапе «Сценарий» редактируемого видеопроекта — кнопка `<button type="button" class="ghost" data-act="project-chat" data-id="…">Чат по сценарию</button>` → `openChatModal({kind: "project", id})`.
  - В модалке чата для сессии с `source.kind === "project"` кнопка `#chat-make-project` подписана «Применить к проекту» и по клику: `confirm("Заменить сцены проекта сценами из диалога?")` → `PUT /api/projects/<id>/scenes` с `chatApplyBody` → `closeChat()` → `openProjectModal(id)`; ошибка — в `#chat-project-err`.

- [ ] **Step 1: Тесты**
```python
@_needs_node
def test_plate_text_for_each_gpu_sharing_answer():
    assert _js("[app.llmPlateText('down', {sharesGpu: true}), app.llmPlateText('up', {sharesGpu: true}), "
               "app.llmPlateText('down', {sharesGpu: false, external: true}), "
               "app.llmPlateText('down', {sharesGpu: null, external: true}), "
               "app.llmPlateText('up', {sharesGpu: null, external: false})]") == [
        "делит видеокарту с H3: пока модель поднята, рендер ждёт",
        "делит видеокарту с H3: пока модель поднята, рендер ждёт (сейчас поднята)",
        "внешний провайдер — память этой машины не занимает",
        "где считает провайдер, не указано (shares_gpu в providers.json)",
        "модель поднята"]


@_needs_node
def test_chat_apply_body_keeps_prompt_and_duration_only():
    assert _js("app.chatApplyBody({kind: 'video', scenes: [{prompt: '@a', duration: 7, extra: 1}]})") \
        == {"scenes": [{"prompt": "@a", "duration": 7}]}
```
Сценарии: `project_chat_opens` (клик `project-chat` → `posts == [["/api/chat", {"source": {"kind": "project", "id": "p1"}, "prompt": "", "mode": "", "image": "", "end_image": "", "duration": 10}]]`); `chat_apply_to_project` (сессия `GET /api/chat/c1` с `source: {kind: "project", id: "p1"}`, `project: {kind: "video", scenes: [...]}`; `window.location.hash = "#chat/c1"`; клик по `#chat-make-project` → `confirms == ["Заменить сцены проекта сценами из диалога?"]`, `puts == [["/api/projects/p1/scenes", {"scenes": [...]}]]`, `getElementById("chat-make-project").textContent == "Применить к проекту"`). Старые тесты `llmPlateText` (`tests/test_web.py`, поиск `llmPlateText`) с `{external: true}` без `sharesGpu` — **меняют ожидание** на текст `null`-ветки: это и есть исправляемый баг (аудит, топ-5); перечислить их в отчёте.
- [ ] **Step 2: Красный.**
- [ ] **Step 3: Реализация** — `renderLlmPlate` передаёт `sharesGpu: row ? row.shares_gpu : null`.
- [ ] **Step 4: Зелёный; CSS** при необходимости + `CLASS_SOURCES` (кнопка «Чат по сценарию» внутри `projectScriptStageHtml` — классы уже существующие).
- [ ] **Step 5: Мутация** — вернуть старую ветку `external → «не занимает»` → красный; в «Применить» слать на `POST /api/projects` → красный `chat_apply_to_project`.
- [ ] **Step 6: Полный прогон; коммит** — `feat(webui): чат по сценарию проекта, «Применить к проекту», честная подпись провайдера по shares_gpu`.

---

## Task 13: Приёмка — координатор проходит ролик в браузере целиком

Исполнитель — координатор, не субагент-кодер. Инструмент — playwright MCP (`mcp__plugin_playwright_playwright__*`) против живой панели `http://192.168.100.50:8765` после выкладки ветки (выкладка — по процедуре задачи 14 плана W1, вне этого плана). **Запрещено:** ssh, curl, правка файлов на сервере или в outdir, `browser_evaluate` с `fetch`. Разрешено `browser_evaluate` только для чтения размеров (шаг 12). Все шаги — клики и ввод в браузере. Скриншоты — `~/worktrees/battle/ui-gaps-acceptance/NN-*.png`, протокол — там же `REPORT.md`.

**Предусловия (проверяет координатор глазами на странице, не командами):** плашка GPU не красная; очередь пуста; в `providers.json` у Qwen `shares_gpu` выставлен (иначе подпись в шаге 4 будет «не указано» — это допустимо, отметить); `pmset`/`caffeinate` на Маке не нужны (рендер на alex-neuro), но Мак не должен уснуть до конца — координатор держит сессию под `caffeinate -dimsu`, как требует `CLAUDE.md`.

- [ ] **1. Референсы.** Внизу страницы «Референсы»: загрузить портрет (`browser_file_upload`), тег `@acc-hero`, `person`, описание по-английски → «Добавить». Превью 96×96 видно **без перезагрузки**; перезагрузить страницу — превью видно сразу. Загрузить кадр локации → `@acc-place`, `environment`. Скриншот `01-library.png`.
- [ ] **2. Новая версия и удаление.** У `@acc-hero` — другой файл → «Новая версия» → подпись `v2 · версий: 2`. Карточка `@uiaudit-face` (мусор аудита): «Удалить» → подтвердить → исчезла. Скриншот `02-library-v2.png`.
- [ ] **3. Проект.** Зона «Проекты» → «+ Новый ролик» → название `acc-1507` → открылась модалка проекта. В «Референсах проекта» отметить `@acc-hero` (в селекте выбрать **v1**) и `@acc-place`. Скриншот `03-project-refs.png`.
- [ ] **4. Диалог (проверка входа, не обязательный путь).** «Чат по сценарию» открывает модалку чата; подпись провайдера — не «память не занимает», если провайдер — Qwen на этой карте. Закрыть без отправки (Qwen может быть выгружен — это не провал). Скриншот `04-chat-plate.png`.
- [ ] **5. Сценарий вручную.** В редакторе: сцена #0 — промпт с `@acc-hero` (набрать `@acc-h` и выбрать подсказку кнопкой) и `@acc-place`, длительность 4 → подсказка «на сетке: 3,75 с»; стартовый кадр — `@acc-place`; сид 305. «+ Сцена» → #1: промпт с `@acc-hero`, длительность 3, шаги 30. «+ Сцена» → #2, затем «Удалить» её (остаются 2) — или оставить 3 сцены по 3 с, если хватает времени. «Сохранить сценарий» → «не сохранено» исчезло; перезагрузить страницу, открыть проект — всё на месте (сид, шаги, стартовый кадр). Скриншот `05-editor.png`.
- [ ] **6. JSON (проверка пути, затем отмена).** В «Вставить сценарий JSON» вставить `42` → «Загрузить» → понятная ошибка, сцены не тронуты.
- [ ] **7. Настройки и запуск.** Сид проекта 305 → «сохранено ✓». Галочка «Апскейл LTX после сцен» включена. «Утвердить сценарий» → «▶ Начать расчёт».
- [ ] **8. Во время прогона.** Плашка GPU: «Карту держит панель: H3 считает «acc-1507», сцена #0 — …» (или «ждёт карту: …» с причиной). Карточка «идёт»: канвас, длительность, сид 305, «идёт N мин из ≈M мин», доля **не 0 %** после первой минуты. В модалке проекта поля `i2v_prefix`, сид проекта и «Референсы проекта» — `disabled` с причиной; попытка невозможна. Скриншот `06-running.png`, `07-locks.png`.
- [ ] **9. Пересъёмка.** Когда сцена #0 `done`: «Пересчитать сцену» у #0 → панель: промпт целиком, «новый случайный» сид, строка «Пересчитает сцены #0, #1» → «Пересчитать». Сцена переснимается с новым сидом (видно в карточке «идёт»). Скриншот `08-retry.png`. (Если время дорого — пропустить с пометкой; обязательна только проверка, что панель открывается и показывает каскад.)
- [ ] **10. Апскейл.** После всех сцен: «Апскейл LTX: идёт», затем «готов · сила X · N из N частей»; у карточек сцен ссылки «LTX». В «Готово» нет плитки `job-upscale` с «клип удалён». Скриншот `09-upscale.png`.
- [ ] **11. Сборка и финал.** «Сборка: готово», `final.mp4` открывается, «Скачать» скачивает файл с именем `acc-1507-final.mp4` (playwright: событие download, имя и размер > 0). «Черновая сборка» видна только при всех сценах `done`. Скриншот `10-final.png`.
- [ ] **12. 390 px.** `browser_resize` 390×844: главная, модалка проекта (редактор и готовый проект), библиотека. На каждом экране `browser_evaluate("() => [document.documentElement.scrollWidth, document.querySelector('#project-body') ? document.querySelector('#project-body').scrollWidth : 0]")` — оба ≤ 390. Скриншоты `11-390-main.png`, `12-390-project.png`, `13-390-library.png`.
- [ ] **13. Итог.** `REPORT.md`: каждый шаг — прошёл/нет, скриншот, замечания; отдельный список «пришлось бы обойти через ssh/curl» — должен быть пуст. Любой непройденный обязательный шаг (1, 3, 5, 7, 8, 10, 11, 12) — задача возвращается исполнителю соответствующей задачи плана.

---

## Расхождения с аудитом и IA (сознательные)

- `window.confirm` вместо встроенных подтверждений IA §1.6 — до волны 2 (спека §2); пересъёмка — встроенная панель уже сейчас.
- «+ Новый ролик» вместо IA «+ Новый проект → выбор вида»: на sglang в 1.5 создаётся только видео; волна 2 добавит выбор вида на ту же кнопку.
- Чат проекта — модалка, а не боковая панель IA; контракт API (`source.kind = "project"`, «Применить» = `PUT …/scenes`) переезжает без изменений.
- Сила апскейла — только показ (аудит 5b, IA «сила 0,6»).

## Самопроверка плана

- Каждый пункт объёма задания → задача: «+ Новый ролик» — 6; редактор (промпт, 3–15 с с сеткой, fresh_start, seed, steps, порядок) — 7; refs/@-теги/tagSuggestions, кадр сцены 0, JSON — 8; версия карточки с файлом, выбор версии, удаление (API 3 + UI 9), превью — 9; LLM-путь (чат проекта, подпись, длительности, @теги из сценария) — 4 + 12; прогресс без нулей, плашка карты, пересъёмка, блокировки — 1 + 2 + 10; сила апскейла, части, 404, «Скачать», черновая сборка, «пересчитать» по делу — 5 + 10 + 11; CSS и 390 px — 6–12 + приёмка 13.
- Решение «запрет, а не предупреждение» для `i2v_prefix`/референсов/апскейла — спека §5.5, задачи 1 и 10.
- Тест-образцы плана не используют `in`, кроме двух мест, где это оговорено (строка системного промпта в задаче 4; временный `in` в задаче 11 с обязательной заменой на `==`).
