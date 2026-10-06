# Монтажная панель на alex-neuro (волна 1): план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** панель `h3 web` + `h3 worker` работает на alex-neuro в Docker поверх H3 (sglang) и LTX (ComfyUI): путь «сценарий → сцены → апскейл → сборка» из браузера без ssh, при этом Мак-режим (`H3_ENGINE=mlx`, по умолчанию) не меняется.

**Architecture:** движок выбирается переменной `H3_ENGINE`. При `sglang` сцена — это та же generate-задача файловой очереди, но воркер исполняет её в своём процессе адаптером `h3_48gb/engines/sglang.py` (HTTP к sglang), а не подпроцессом MLX. Карту делит хостовый `gpu-dispatcher` (stdlib, systemd): он поднимает и гасит только свои H3/ComfyUI, держит `generation.lock`, видит чужое и по кнопке выгружает/возвращает Qwen. Новые вещи (библиотека референсов, маршрут проекта, апскейл, плашка GPU) — JSON-API; старый интерфейс получает только минимум кнопок.

**Tech Stack:** Python 3.12 stdlib (http.server, urllib, fcntl, subprocess), numpy/pillow/safetensors/jsonschema (как сейчас), ffmpeg/ffprobe, node (проверки фронта), Docker + compose, systemd.

**Spec:** `docs/superpowers/specs/2026-10-06-panel-on-alex-neuro-design.md` (читать целиком перед любой задачей; план спорит со спекой только в разделе «Расхождения со спекой» в конце).

**Ветка:** вся работа идёт в ветке `feat/panel-alex-neuro` этого репозитория. Ветку создаёт исполнитель до задачи 0 (`git switch -c feat/panel-alex-neuro` от `main`); план её не создаёт. В `main` не коммитить, не пушить без слова владельца.

## Global Constraints

- Мак-режим не ломается: `H3_ENGINE` не задан или `mlx` → поведение байт-в-байт прежнее; всё «при sglang» включается только `H3_ENGINE=sglang` (спека §3.3.1).
- Окружение тестов на Маке: `PY=~/venvs/h3-panel/bin/python` (создаётся в задаче 0). Старого `~/venvs/minimax-h3-mlx-48gb` нет, весов MLX нет. Полный прогон — `env -u NODE_OPTIONS $PY -m pytest -q -p no:cacheprovider` в форграунде одним вызовом с явным timeout 600000, 0 failed, перед каждым коммитом.
- **Тест не написан, пока не видел его красным** (`CLAUDE.md`): у каждой задачи есть шаг мутации — убрать или обратить защищаемую строку, увидеть красный, вернуть. Текст assertion-ошибки вставить в отчёт задачи. Без этого задача не сдана.
- Тесты пиннят содержание, а не форму: payload, argv, ответы API, тексты промптов — точным равенством (`==` со всем словарём/списком), а не `in`, не `len()`, не «ключ есть».
- Фейковые серверы (sglang, ComfyUI, диспетчер) — stdlib `http.server.ThreadingHTTPServer` на `127.0.0.1:0` в потоке, по образцу `tests/_fake_llama.py`. Реальные GPU, sglang, ComfyUI, Qwen, nvidia-smi в тестах не трогаются.
- Сообщения человеку — по-русски; комментарии, докстринги, идентификаторы — по-английски (как во всём пакете).
- Новый код ошибки `CliError` обязан быть в `h3_48gb/cli.py:ERROR_CODES` (`CliError.__init__` делает `assert code in ERROR_CODES`, `cli.py:270`), статус — в `web.ERROR_STATUS`, если не 400.
- Значения из спеки дословно: порт панели `8765`; `H3_ALLOWED_HOSTS=192.168.100.50:8765,alex-neuro:8765`; `H3_SGLANG_URL=http://127.0.0.1:30020`; `H3_COMFY_URL=http://127.0.0.1:8188`; `H3_DISPATCHER_URL=http://127.0.0.1:8790`; диспетчер слушает `127.0.0.1:8790`; `generation.lock` = `/home/alex/Projects/qwen-image21-lab/generation.lock`; `H3_IDLE_RELEASE_MIN` = 15; опрос sglang каждые 20 с; повтор acquire каждые 30 с; пропажа sglang — 5 попыток через 30 с; термозащита ≥ 80 °C → ждать до 72 °C; шаги по умолчанию 50; `flow_shift 12.0`, `audio_flow_shift 3.0`, `quality "lossless"`, `num_outputs_per_prompt 1`, `model "MiniMaxAI/MiniMax-H3"`; сетка кадров `17j+5` при 24 к/с; LTX `denoise 0.10`, сид 42, дополнение до `8k+1` кадров; SIGTERM группе своих процессов, ожидание до 120 с, SIGKILL.
- Факты sglang, проверенные по коду сервера (`/home/alex/Projects/h3-lab/sglang-src/python/sglang/multimodal_gen/runtime/...`), обязательны для адаптера: `task` всегда `ref2va` — сервер с `VARIANT=ref2va` обслуживает только его (проба 07.10, `h3-bench/logs/probe-t2va-20261007.log`: `t2va` принимается с HTTP 200 и падает при выполнении «task 't2va' is not served by MiniMax H3 partition 'ref2va'»), поэтому **у каждой сцены должен быть хотя бы один reference** (`@`-тег с картинкой или, в клипе, кусок трека), иначе отказ на валидации «нужен хотя бы один референс (@тег) в сцене»; `duration_seconds` обязателен и в `[3, 15]`; keyframe только `frame_index: 0`; у reference нет `frame_index`; ключи условия только `type/uri/role/frame_index/start_time_seconds`; `aspect_ratio ∈ {auto, 21:9, 16:9, 4:3, 3:2, 1:1, 2:3, 3:4, 9:16}`; ref2va с keyframe без reference (картинка или аудио) → 400 «ref2va keyframes require at least one reference condition»; пути условий — как в проверенных прогонах: картинки голым абсолютным путём, аудио `file://` (`chain_beach.py`, `runner_clip.py`); keyframe не нумеруется, `<Picture N>` — только reference-картинки с 1 в порядке условий, аудио — `<Audio j>` отдельно (`presentation.py:230-270`, `stages/text_encoding.py:385`); `DELETE /v1/videos/{id}` не останавливает GPU; неизвестный id → 404 `Video not found`; статусы только `queued`/`completed`/`failed`; тело 400 — `{"detail": "..."}`, сервер его не логирует — адаптер сохраняет его в задачу.
- Docker-образ: `python:3.12-slim` + ffmpeg + node, без CUDA и без `mlx`/`mlx-vlm`/opencv/scipy; пользователь uid/gid 1000; `network_mode: host`; тома по тем же путям, что на хосте.
- Ни один шаг плана не запускает GPU-работу на alex-neuro, кроме проб задачи 14, помеченных «требует свободной GPU».

## Review Focus

Пять входов, которые спека подразумевает, но которые легко пропустить. Тест на каждый уже вписан в задачу-владельца.

1. **Перезапуск диспетчера при поднятом своём H3.** Владелец ждёт, что панель продолжит пользоваться своим H3, а не сочтёт его чужим и не будет ждать вечно. Тест `test_restarted_dispatcher_still_owns_its_engine` (задача 7).
2. **Теги в тексте сцены на границах.** `@alice,` в конце фразы — это тег `@alice`; `anna@alice.com` — не тег; `@Alice` — ошибка «тег пишется строчными», а не тихий пропуск. Тест `test_scene_tags_edge_cases` (задача 4).
3. **Старые кадры ComfyUI от прошлой попытки той же сцены.** Повторный апскейл не должен склеить чужие PNG: префикс вывода уникален на попытку, число кадров сверяется с `8k+1`. Тест `test_upscale_prefix_is_unique_per_attempt_and_frame_count_is_checked` (задача 10).
4. **Обрыв скачивания `/content`.** Недокачанный mp4 не должен выглядеть готовым для `queue.reconcile` (он считает «есть `.mp4`» успехом, `queue.py:1165`). Тест `test_download_is_atomic` (задача 6).
5. **Импортированный трек короче минимальной сцены или длиннее, чем покрывают сцены.** 4-секундный mp3 должен дать понятный отказ, а не сцену-огрызок. Тест `test_import_shorter_than_one_scene_is_refused` (задача 9).

---

## Карта файлов

| Файл | Ответственность | Задача |
|---|---|---|
| `requirements-panel.txt` (новый) | зависимости панели без MLX | 0 |
| `conftest.py` (корень) | маркер `mlx`/`cv`, счётчик пропусков | 0 |
| `pyproject.toml` | регистрация маркеров | 0 |
| `h3_48gb/web.py` | `--host`, Host/Origin, валидация при sglang, библиотека, маршрут, GPU-API | 1,3,4,5,8,9,11 |
| `h3_48gb/cli.py` | `--host`, коды ошибок | 1,3,4 |
| `h3_48gb/engine.py` (новый) | `H3_ENGINE` | 2 |
| `h3_48gb/worker.py` | caffeinate под Darwin, sglang-ветка, ожидание GPU, idle-release, upscale-задача | 2,6,8,10 |
| `h3_48gb/engines/__init__.py` (новый) | пакет движков | 3 |
| `h3_48gb/engines/sglang_args.py` (новый) | парсер/валидатор argv при sglang | 3 |
| `h3_48gb/engines/estimate.py` (новый) | оценка времени сцены при sglang | 3 |
| `h3_48gb/library.py` (новый) | библиотека референсов, сборка Ref2VA | 4 |
| `h3_48gb/project.py` | `references`, `route`, `i2v_prefix`, этап `upscale`, `set_scene_fields` | 4,11 |
| `h3_48gb/assemble.py` | `_scene_generate_args_sglang`, цепочка при sglang, куски трека, сборка из `-ltx` | 5,9,11 |
| `h3_48gb/queue.py` | `engine_ref`, `wait_reason`, `cancel_reason`, `resumable`, `KIND_UPSCALE` | 6,10 |
| `h3_48gb/engines/sglang.py` (новый) | адаптер sglang | 6 |
| `h3_48gb/engines/dispatcher_client.py` (новый) | клиент диспетчера | 8 |
| `tools/gpu-dispatcher/dispatcher.py` (новый) | диспетчер GPU | 7 |
| `tools/gpu-dispatcher/h3-gpu-dispatcher.service` (новый) | systemd-юнит | 7 |
| `h3_48gb/engines/motion.py` (новый) | движение клипа, сила LTX | 10 |
| `h3_48gb/engines/ltx.py` (новый) | апскейл через ComfyUI | 10 |
| `h3_48gb/engines/ltx_workflow.json` (новый) | шаблон воркфлоу LTX | 10 |
| `h3_48gb/webui/app.js`, `index.html`, `style.css` | минимум UI | 12 |
| `Dockerfile`, `compose.yaml`, `docker/entrypoint.sh`, `.dockerignore` (новые) | образ панели | 13 |
| `tools/probes/sglang_probes.py` (новый) | пробы §6 на живом sglang | 14 |
| `tests/test_*.py` | по задачам | все |

---

## Task 0: Подготовка: лёгкий venv, маркер `mlx`, зелёный базис

**Files:**
- Create: `requirements-panel.txt`
- Modify: `pyproject.toml:52` (маркеры), `conftest.py` (корень)
- Modify (маркировка MLX/opencv-тестов, верх файла): `test_adaln_indexing.py`, `test_lazy_pipeline.py`, `test_preview.py`, `test_text_encoder_quant.py`, `test_vision_patch_embed_layout.py`, `tests/test_attention_levers.py`, `tests/test_checkpoint.py`, `tests/test_checkpoint_preview_integration.py`, `tests/test_decode_video_uint8.py`, `tests/test_dit_loader.py`, `tests/test_latent_tail.py`, `tests/test_pipeline_latent.py`, `tests/test_preview_tae.py`, `tests/test_sigma_grid.py`, `tests/test_tae.py`, `tests/test_vae_decode_eval_patch.py`, `tests/test_packing_latent.py`; opencv/scipy: `tests/test_cli_facerefine.py`, `tests/test_facepaste.py`, `tests/test_facetrack.py`; `tests/test_framecheck.py` — **не целиком** (сам `framecheck` — numpy): верхний `import cv2` (`test_framecheck.py:15`) убрать, в двух тестах, которые читают кадры через OpenCV (`test_is_frame_corrupt_catches_every_flat_frame_from_the_chunk_recon_calibration`, `test_tile_seam_score_catches_the_reference_corrupt_frame_from_the_2026_08_20_incident`), — декоратор `@pytest.mark.cv` и первой строкой `cv2 = pytest.importorskip("cv2", reason="cv: needs opencv-python and scipy, absent here")`
- Modify (маркировка отдельных тестов, которые тянут mlx изнутри тела): `tests/test_bake_adaln.py` (тесты со строк 32, 54, 72, 95-99), `tests/test_facerefine.py` (тесты со строк 318, 455, 474-477, 507-509), `tests/test_cli.py` (тесты со строк 424 и 455), `test_qkv_permutation.py` (проверить прогоном)
- Test: `tests/test_markers.py` (новый)

**Interfaces:**
- Produces: маркеры `@pytest.mark.mlx` и `@pytest.mark.cv`; хук корневого `conftest.py`, пропускающий тест с маркером, если модуля нет; строка итога `N skipped: mlx not installed`. Venv `~/venvs/h3-panel` для всех следующих задач.

- [ ] **Step 1: Создать venv и requirements-panel.txt**

`requirements-panel.txt`:
```text
# The panel's own runtime and test dependencies, without the MLX stack (spec §3.2): the Docker
# image and the Mac test venv both install the package with `pip install --no-deps` and then this
# file, because pyproject.toml keeps mlx/mlx-vlm/opencv/scipy as hard dependencies for the Mac
# render path, which the panel never imports.
numpy>=1.26
pillow>=10.0
safetensors>=0.4
jsonschema>=4.0
pytest>=8.0
```

Run:
```bash
python3.12 -m venv ~/venvs/h3-panel
~/venvs/h3-panel/bin/pip install --no-deps -e /Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb
~/venvs/h3-panel/bin/pip install -r /Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb/requirements-panel.txt
~/venvs/h3-panel/bin/python -c "import h3_48gb.web, h3_48gb.worker, h3_48gb.assemble; print('ok')"
```
Expected: `ok` (web/worker/assemble не тянут mlx — проверено: `web.py:56` тянет `cli` → numpy, `assemble.py:89` numpy).

- [ ] **Step 2: Прогнать полный набор до правок и записать, что падает**

Run: `cd /Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb && env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -40`
Expected: ошибки сбора `ModuleNotFoundError: No module named 'mlx'` / `'cv2'` / `'scipy'` в файлах из списка выше и падения тестов, которые импортируют mlx из тела. Записать в отчёт задачи точный список.

- [ ] **Step 3: Написать падающий тест на хук маркера**

`tests/test_markers.py`:
```python
"""The `mlx`/`cv` markers: a test that needs a module this environment lacks is *skipped with a
reason*, never collected into an ImportError and never silently passed. Driven through a real
`pytest` subprocess on a throwaway test file, because the hook under test runs at collection time
of the outer session and cannot be observed from inside one test.
"""
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run(tmp_path, body: str) -> subprocess.CompletedProcess:
    """The probe file lives *inside* tests/ (and is removed afterwards): a file outside the
    project root would not load the root conftest.py whose hook is under test."""
    test_file = PROJECT_ROOT / "tests" / f"test_zz_probe_marker_{tmp_path.name}.py"
    test_file.write_text(body, encoding="utf-8")
    try:
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs",
             str(test_file)],
            capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=120)
    finally:
        test_file.unlink(missing_ok=True)


def test_a_marked_test_whose_module_is_missing_is_skipped_with_the_marker_reason(tmp_path):
    result = _run(tmp_path, (
        "import pytest\n"
        "@pytest.mark.mlx\n"
        "def test_needs_mlx():\n"
        "    import module_that_never_exists_h3  # noqa: F401\n"
    ))
    # Only meaningful where mlx is absent (the panel venv, the Docker image).
    import importlib.util
    if importlib.util.find_spec("mlx") is not None:
        assert "1 passed" in result.stdout or "1 failed" in result.stdout
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIPPED [1]" in result.stdout, result.stdout
    assert "mlx: needs the MLX stack, absent here" in result.stdout, result.stdout


def test_an_unmarked_test_still_fails_on_a_missing_module(tmp_path):
    result = _run(tmp_path, (
        "def test_unmarked():\n"
        "    import module_that_never_exists_h3  # noqa: F401\n"
    ))
    assert result.returncode == 1, result.stdout
    assert "ModuleNotFoundError" in result.stdout, result.stdout
```

- [ ] **Step 4: Убедиться, что тест красный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_markers.py -q -p no:cacheprovider`
Expected: FAIL первого теста — `PytestUnknownMarkWarning`/тест не пропущен (`assert "SKIPPED [1]" in ...`).

- [ ] **Step 5: Реализовать маркеры и хук**

`pyproject.toml` — заменить строку `markers = [...]`:
```toml
markers = [
    "slow: needs the GPU and minutes of wall time; opt in with an env var",
    "mlx: needs the MLX stack (mlx, mlx-vlm, upstream/); skipped where it is not installed",
    "cv: needs opencv-python and scipy; skipped where they are not installed (the panel image)",
]
```

Корневой `conftest.py` — дописать после импорта `Path`:
```python
import importlib.util

import pytest

#: marker name -> (modules that must all be importable, the skip reason). The reason text is what
#: `pytest_terminal_summary` below counts, so it doubles as a stable tag.
_MODULE_MARKERS = {
    "mlx": (("mlx",), "mlx: needs the MLX stack, absent here"),
    "cv": (("cv2", "scipy"), "cv: needs opencv-python and scipy, absent here"),
}


def _missing(modules) -> bool:
    return any(importlib.util.find_spec(name) is None for name in modules)


def pytest_runtest_setup(item):
    for marker, (modules, reason) in _MODULE_MARKERS.items():
        if item.get_closest_marker(marker) is not None and _missing(modules):
            pytest.skip(reason)
```
и в существующий `pytest_terminal_summary` — первым делом:
```python
    skipped_reports = terminalreporter.stats.get("skipped", [])
    for marker, (_modules, reason) in _MODULE_MARKERS.items():
        count = sum(1 for report in skipped_reports
                    if reason in str(getattr(report, "longrepr", "")))
        if count:
            terminalreporter.write_sep("=", f"{count} skipped: {reason}", yellow=True)
```

- [ ] **Step 6: Пометить модули, которые импортируют mlx/cv2/scipy на верхнем уровне**

В каждом файле из списка «MLX» — самыми первыми строками после докстринга, до любых импортов mlx/upstream:
```python
import pytest

pytestmark = pytest.mark.mlx
pytest.importorskip("mlx.core", reason="mlx: needs the MLX stack, absent here")
```
В каждом файле из списка «opencv/scipy»:
```python
import pytest

pytestmark = pytest.mark.cv
pytest.importorskip("cv2", reason="cv: needs opencv-python and scipy, absent here")
pytest.importorskip("scipy", reason="cv: needs opencv-python and scipy, absent here")
```
`tests/test_packing_latent.py` тянет mlx через `upstream/minimax_h3_mlx/packing.py:28` — та же шапка `mlx`. Отдельным тестам из списка «изнутри тела» поставить декоратор `@pytest.mark.mlx`.

- [ ] **Step 7: Прогнать полный набор, добить остаток**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest -q -p no:cacheprovider -rs 2>&1 | tail -30`
Expected: `0 failed`. Если что-то ещё падает с `ModuleNotFoundError: mlx|cv2|scipy|mlx_vlm` — пометить тем же способом и прогнать снова. Падение по любой другой причине — остановиться и сообщить (это не часть задачи). Записать итог `N passed, M skipped` и строки `K skipped: mlx: ...` — это базис для всех следующих задач.

- [ ] **Step 8: Мутация**

Убрать `@pytest.mark.mlx` с одного теста в `tests/test_bake_adaln.py`, прогнать файл. Expected: FAIL `ModuleNotFoundError: No module named 'mlx'`. Вернуть. Затем в `conftest.py` заменить `pytest.skip(reason)` на `pass` и прогнать `tests/test_markers.py` — Expected: FAIL `assert "SKIPPED [1]" in result.stdout`. Вернуть. Обе ошибки — в отчёт.

- [ ] **Step 9: Commit**

```bash
git add requirements-panel.txt pyproject.toml conftest.py tests/test_markers.py tests/ test_*.py
git commit -m "test: маркеры mlx/cv и лёгкий venv панели — зелёный базис без MLX"
```

---

## Task 1: Сеть: `--host`, `H3_ALLOWED_HOSTS`, Host/Origin

**Files:**
- Modify: `h3_48gb/web.py:5800-5832` (`make_server`), `h3_48gb/web.py:3095` (`_check_host`, сравнение на `3114-3115`), `h3_48gb/web.py:3175-3183` (`_check_origin`)
- Modify: `h3_48gb/cli.py:618-624` (подкоманда `web`), `h3_48gb/cli.py:1509-1543` (`run_web`), `h3_48gb/cli.py:2056-2057` (вызов), `h3_48gb/cli.py:148` (`ERROR_CODES`)
- Modify: `tests/test_web.py:1367-1370` — `test_h3_web_has_no_host_flag` заменяется (защита не удаляется, а переходит в новую форму, шаг 4a)
- Test: `tests/test_web_network.py` (новый)

**Interfaces:**
- Produces:
  - `web.make_server(queue_root, outdir, repo=None, models=None, webui=None, port=DEFAULT_PORT, verbose=False, reveal=None, host=LOOPBACK, allowed_hosts=()) -> ThreadingHTTPServer`
  - `web.allowed_hosts_from_env(environ=None) -> tuple[str, ...]`
  - `cli.run_web(outdir: Path, port: int = 8765, host: str = "127.0.0.1") -> dict`
  - коды `external_bind_without_allowed_hosts`, `allowed_hosts_invalid`

- [ ] **Step 1: Написать падающие тесты**

`tests/test_web_network.py`:
```python
"""LAN access (spec §3.3.6): the server may bind beyond the loopback only together with a
non-empty `H3_ALLOWED_HOSTS`, and Host/Origin are checked against that list by exact equality
(case-insensitive, since browsers lowercase the authority anyway)."""
import pytest

from h3_48gb import queue as q
from h3_48gb import web
from h3_48gb.cli import CliError
from test_web import _call, _json, _serve

LAN = "192.168.100.50:8765"


@pytest.fixture
def lan_server(tmp_path):
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    root = q.layout(outdir / "queue")["root"]
    live = _serve(root, outdir, allowed_hosts=(LAN, "alex-neuro:8765"))
    yield live
    live.httpd.shutdown()
    live.httpd.server_close()


def test_a_lan_host_on_the_list_is_served(lan_server):
    status, body = _json(lan_server, "/api/state", host=LAN)
    assert status == 200, body
    assert body["ok"] is True


def test_a_write_from_the_lan_page_passes_both_checks(lan_server):
    status, body = _call(lan_server, "POST", "/api/queue/pause", {},
                         headers={"Host": LAN, "Origin": f"http://{LAN}"})
    assert (status, body) == (200, {"ok": True, "paused": True})


def test_host_comparison_ignores_case(lan_server):
    status, body = _json(lan_server, "/api/state", host="Alex-Neuro:8765")
    assert status == 200, body


@pytest.mark.parametrize("host", ["192.168.100.51:8765", "192.168.100.50:8766", "evil.example:8765"])
def test_a_host_off_the_list_is_refused(lan_server, host):
    status, body = _json(lan_server, "/api/state", host=host)
    assert status == 403
    assert body["error"]["code"] == "host_not_allowed"


def test_a_foreign_origin_is_refused_even_with_a_good_host(lan_server):
    status, body = _call(lan_server, "POST", "/api/queue/pause", {},
                         headers={"Host": LAN, "Origin": "http://evil.example:8765"})
    assert status == 403
    assert body["error"]["code"] == "origin_not_allowed"


def test_without_a_list_a_lan_host_is_still_refused(tmp_path):
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    live = _serve(q.layout(outdir / "queue")["root"], outdir)
    try:
        status, body = _json(live, "/api/state", host=LAN)
        assert (status, body["error"]["code"]) == (403, "host_not_allowed")
    finally:
        live.httpd.shutdown()
        live.httpd.server_close()


def test_external_bind_without_a_list_is_refused_before_binding(tmp_path):
    with pytest.raises(CliError) as excinfo:
        web.make_server(tmp_path / "queue", tmp_path, port=0, host="0.0.0.0")
    assert excinfo.value.code == "external_bind_without_allowed_hosts"


def test_allowed_hosts_from_env_parses_and_validates():
    assert web.allowed_hosts_from_env({"H3_ALLOWED_HOSTS": " 192.168.100.50:8765, Alex-Neuro:8765 ,"}) \
        == ("192.168.100.50:8765", "alex-neuro:8765")
    assert web.allowed_hosts_from_env({}) == ()
    with pytest.raises(CliError) as excinfo:
        web.allowed_hosts_from_env({"H3_ALLOWED_HOSTS": "192.168.100.50"})
    assert excinfo.value.code == "allowed_hosts_invalid"


def test_cli_web_accepts_host_and_passes_env_list(monkeypatch, tmp_path):
    from h3_48gb import cli

    seen = {}

    class _FakeServer:
        server_address = ("0.0.0.0", 8765)

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    def fake_make_server(root, outdir, **kwargs):
        seen.update(kwargs)
        return _FakeServer()

    monkeypatch.setattr(web, "make_server", fake_make_server)
    monkeypatch.setenv("H3_ALLOWED_HOSTS", LAN)
    (tmp_path / "out").mkdir()
    args = cli.build_parser().parse_args(["web", "--host", "0.0.0.0", "--outdir", str(tmp_path / "out")])
    assert args.host == "0.0.0.0"
    cli.run_web(args.outdir, args.port, args.host)
    assert seen == {"port": 8765, "verbose": True, "host": "0.0.0.0", "allowed_hosts": (LAN,)}
```

- [ ] **Step 2: Убедиться, что тесты красные**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_web_network.py -q -p no:cacheprovider`
Expected: FAIL — `TypeError: make_server() got an unexpected keyword argument 'allowed_hosts'`, `AttributeError: ... allowed_hosts_from_env`.

- [ ] **Step 3: Реализация в web.py**

В `make_server` добавить параметры `host=LOOPBACK, allowed_hosts=()` и заменить тело до `httpd.queue_root`:
```python
    allowed_hosts = tuple(str(name).strip().lower() for name in allowed_hosts if str(name).strip())
    if host != LOOPBACK and not allowed_hosts:
        raise CliError(
            "external_bind_without_allowed_hosts",
            f"--host {host} binds beyond the loopback; set H3_ALLOWED_HOSTS to the exact "
            f"host:port names the page is opened by (e.g. 192.168.100.50:8765)",
            {"host": host})
    httpd = _Server((host, port), _Handler)
    bound = httpd.server_address[1]
    httpd.allowed_hosts = frozenset({f"{LOOPBACK}:{bound}", f"localhost:{bound}", *allowed_hosts})
    httpd.allowed_origins = frozenset(f"http://{name}" for name in httpd.allowed_hosts)
```
Новая функция рядом с `make_server`:
```python
_ALLOWED_HOST_RE = re.compile(r"^[a-z0-9.-]+:\d{1,5}$")


def allowed_hosts_from_env(environ=None) -> tuple[str, ...]:
    """`H3_ALLOWED_HOSTS` as a tuple of lowercase `host:port` names, empty when unset. A name
    without a port is refused: Host always carries the port here, so a portless entry could only
    ever be a typo that silently matches nothing."""
    raw = (os.environ if environ is None else environ).get("H3_ALLOWED_HOSTS", "")
    names = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    bad = [name for name in names if not _ALLOWED_HOST_RE.match(name)]
    if bad:
        raise CliError("allowed_hosts_invalid",
                       f"H3_ALLOWED_HOSTS entries must be host:port, these are not: {bad}",
                       {"invalid": bad})
    return names
```
В `_check_host` строка 3115: `if host is None or host.strip().lower() not in self.server.allowed_hosts:` (сообщение и detail без изменений). В `_check_origin` строка 3176 уже сравнивает `origin.strip().lower()` — не трогать. Проверить, что `re` и `os` уже импортированы в `web.py` (если нет — добавить).

- [ ] **Step 4: Реализация в cli.py**

Заменить комментарий и подкоманду `web` (`cli.py:618-624`):
```python
    # `--host` beyond the loopback is refused unless H3_ALLOWED_HOSTS names the exact host:port
    # the page is opened by (`web.make_server`): the panel has no password (spec §2, "Доступ"),
    # so the allow-list is what keeps a DNS-rebinding page from driving it.
    wb = _subcommand(sub, "web", help="serve the queue page until stopped")
    wb.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    wb.add_argument("--host", default="127.0.0.1",
                    help="address to bind (default 127.0.0.1); anything else needs H3_ALLOWED_HOSTS")
    wb.add_argument("--port", type=int, default=8765,
                    help="port (default 8765); 0 asks the kernel for a free one")
```
`run_web`:
```python
def run_web(outdir: Path, port: int = 8765, host: str = "127.0.0.1") -> dict:
    from h3_48gb import web

    outdir = Path(outdir)
    if not outdir.is_dir():
        raise CliError("outdir_not_found", f"--outdir does not exist: {outdir}",
                       {"outdir": str(outdir)})
    root = queue_root(outdir)
    httpd = web.make_server(root, outdir, port=port, verbose=True, host=host,
                            allowed_hosts=web.allowed_hosts_from_env())
    bound = httpd.server_address[1]
    url = f"http://{host}:{bound}/"
    print(f"страница на {url} — очередь в {root}; остановить: Ctrl-C", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return {"ok": True, "url": url, "queue": str(root)}
```
(Импорт через модуль `web`, а не `from web import make_server`, — чтобы тест подменял `web.make_server`.) Вызов `cli.py:2057`: `report = run_web(args.outdir, args.port, args.host)`. В `ERROR_CODES`:
```python
    "external_bind_without_allowed_hosts": "`h3 web --host` names a non-loopback address but H3_ALLOWED_HOSTS is empty",
    "allowed_hosts_invalid": "an H3_ALLOWED_HOSTS entry is not host:port",
```

- [ ] **Step 4a: Заменить старый тест «у `h3 web` нет `--host`»**

`tests/test_web.py:1367-1370` — `test_h3_web_has_no_host_flag` проверял, что флага нет. Флаг появился, защита — в другой форме: внешний bind без списка отклоняется. Заменить тест на:
```python
def test_h3_web_beyond_the_loopback_needs_an_allow_list(tmp_path, monkeypatch):
    """A bind-address flag is the one way this server could stop being loopback-only, so
    `--host` beyond 127.0.0.1 is refused unless H3_ALLOWED_HOSTS names the page's host:port."""
    from h3_48gb import cli

    monkeypatch.delenv("H3_ALLOWED_HOSTS", raising=False)
    (tmp_path / "out").mkdir()
    with pytest.raises(CliError) as excinfo:
        cli.run_web(tmp_path / "out", 0, "0.0.0.0")
    assert excinfo.value.code == "external_bind_without_allowed_hosts"
```
Мутация: в `make_server` убрать проверку `host != LOOPBACK and not allowed_hosts` → этот тест FAIL `DID NOT RAISE` (вместе с `test_external_bind_without_a_list_is_refused_before_binding`).

- [ ] **Step 5: Зелёный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_web_network.py tests/test_web.py -q -p no:cacheprovider`
Expected: PASS (включая старые тесты Host/Origin `test_web.py:903-933`, `2593-2625`).

- [ ] **Step 6: Мутации**

(а) В `make_server` убрать `*allowed_hosts` из множества → `test_a_lan_host_on_the_list_is_served` FAIL `assert 403 == 200`. (б) Убрать `raise CliError("external_bind_without_allowed_hosts"...)` → `test_external_bind_without_a_list_is_refused_before_binding` FAIL `DID NOT RAISE`. (в) В `_check_host` убрать `.lower()` → `test_host_comparison_ignores_case` FAIL. Вернуть, ошибки — в отчёт.

- [ ] **Step 7: Полный прогон и commit**

```bash
env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest -q -p no:cacheprovider
git add h3_48gb/web.py h3_48gb/cli.py tests/test_web_network.py tests/test_web.py
git commit -m "feat(web): --host и H3_ALLOWED_HOSTS — доступ из домашней сети по списку"
```

---

## Task 2: Движок как настройка и Mac-специфика под условием

**Files:**
- Create: `h3_48gb/engine.py`
- Modify: `h3_48gb/worker.py:73-86` (`job_command`), `h3_48gb/worker.py:171-209` (`_caffeinate_block`), `h3_48gb/worker.py:976` (LLM-ворота в `main_loop`)
- Modify: `h3_48gb/web.py:746` (`build_state` — поля `engine`, `platform`), `h3_48gb/web.py:3716` (`_reveal_job`), `h3_48gb/web.py:487` (`ERROR_STATUS`)
- Modify: `h3_48gb/cli.py:1472` (`run_worker`), `h3_48gb/cli.py:1509` (`run_web`), `ERROR_CODES`
- Modify: `tests/conftest.py` (сброс переменных окружения панели)
- Test: `tests/test_engine.py` (новый)

**Interfaces:**
- Produces:
  - `engine.ENGINES = ("mlx", "sglang")`, `engine.current(environ=None) -> str`, `engine.is_sglang(environ=None) -> bool`, `class engine.UnknownEngine(ValueError)`
  - `worker.job_command(job, python=sys.executable, *, platform=None) -> list[str]`; `worker._caffeinate_block(spawn=subprocess.Popen, *, platform=None)`; модульная `worker._PLATFORM = sys.platform`
  - `web._PLATFORM = sys.platform`; `/api/state` получает ключи `"engine"` и `"platform"`
  - коды `engine_unknown`, `reveal_unsupported` (409)
- Note: «`pkill llama-server` только для llama-local» уже выполнено: `LlamaLocal.shutdown` (`provider.py:355-361`) вызывается только из `_llm_unload` в цикле по провайдерам `type == "llama-local"` (`web.py:5033-5038`). Правка не нужна; тест ниже фиксирует, что в sglang-режиме LLM-ворота воркера не опрашиваются вовсе.

- [ ] **Step 1: Сброс окружения в тестах**

`tests/conftest.py` — новая autouse-фикстура рядом с существующей:
```python
#: Every environment variable the panel reads. Cleared before each test so that a test run inside
#: the Docker image (where H3_ENGINE=sglang is baked in) exercises the same defaults as one on the
#: Mac; a test that needs sglang sets it explicitly with monkeypatch.setenv.
_PANEL_ENV = ("H3_ENGINE", "H3_ALLOWED_HOSTS", "H3_MAX_REF_IMAGES", "H3_SGLANG_URL", "H3_COMFY_URL",
              "H3_DISPATCHER_URL", "H3_IDLE_RELEASE_MIN", "H3_COMFY_OUTPUT_DIR")


@pytest.fixture(autouse=True)
def _panel_env_cleared(monkeypatch):
    for name in _PANEL_ENV:
        monkeypatch.delenv(name, raising=False)
```

- [ ] **Step 2: Написать падающие тесты**

`tests/test_engine.py`:
```python
"""H3_ENGINE (spec §3.3.1) and the Mac-only behaviour it gates (§3.3.7)."""
import threading

import pytest

from h3_48gb import engine
from h3_48gb import queue as q
from h3_48gb import worker
from test_web import _call, server  # noqa: F401  (fixture)
from test_worker import _stop_after


def test_engine_defaults_to_mlx_and_normalises_case():
    assert engine.current({}) == "mlx"
    assert engine.current({"H3_ENGINE": ""}) == "mlx"
    assert engine.current({"H3_ENGINE": " SGLang "}) == "sglang"
    assert engine.is_sglang({"H3_ENGINE": "sglang"}) is True
    assert engine.is_sglang({}) is False


def test_an_unknown_engine_is_refused():
    with pytest.raises(engine.UnknownEngine):
        engine.current({"H3_ENGINE": "cuda"})


class _Job:
    args = ["generate", "кот", "--tag", "a"]


def test_job_command_wraps_caffeinate_only_on_darwin():
    assert worker.job_command(_Job(), python="py", platform="darwin") == \
        ["caffeinate", "-dimsu", "py", "-m", "h3_48gb", "generate", "кот", "--tag", "a"]
    assert worker.job_command(_Job(), python="py", platform="linux") == \
        ["py", "-m", "h3_48gb", "generate", "кот", "--tag", "a"]


def test_caffeinate_block_spawns_nothing_off_darwin():
    spawned = []

    def spawn(cmd, **kw):
        spawned.append(cmd)
        raise AssertionError("caffeinate must not be started on linux")

    with worker._caffeinate_block(spawn, platform="linux"):
        pass
    assert spawned == []


def _count_llm_gate_calls(monkeypatch, tmp_path, engine_name):
    calls = []
    monkeypatch.setattr(worker, "_llm_holds_gpu", lambda outdir: calls.append(outdir) or False)
    if engine_name:
        monkeypatch.setenv("H3_ENGINE", engine_name)
    root = q.layout(tmp_path / "queue")["root"]
    q.set_paused(root, False)
    worker.main_loop(root, poll=0.01, stop=_stop_after(0.3))
    return calls


def test_the_llm_gate_is_consulted_on_mlx(monkeypatch, tmp_path):
    assert len(_count_llm_gate_calls(monkeypatch, tmp_path, None)) >= 1


def test_the_llm_gate_is_never_consulted_on_sglang(monkeypatch, tmp_path):
    assert _count_llm_gate_calls(monkeypatch, tmp_path, "sglang") == []


def test_state_reports_engine_and_platform(server, monkeypatch):  # noqa: F811
    from h3_48gb import web

    monkeypatch.setattr(web, "_PLATFORM", "linux")
    monkeypatch.setenv("H3_ENGINE", "sglang")
    status, body = _call(server, "GET", "/api/state")
    assert status == 200
    assert (body["engine"], body["platform"]) == ("sglang", "linux")


def test_reveal_is_refused_off_darwin(server, monkeypatch):  # noqa: F811
    from h3_48gb import web

    monkeypatch.setattr(web, "_PLATFORM", "linux")
    status, body = _call(server, "POST", "/api/jobs/anything/reveal", {})
    assert status == 409
    assert body["error"]["code"] == "reveal_unsupported"


def test_worker_refuses_an_unknown_engine(monkeypatch, tmp_path):
    from h3_48gb import cli

    monkeypatch.setenv("H3_ENGINE", "cuda")
    with pytest.raises(cli.CliError) as excinfo:
        cli.run_worker(tmp_path, 5.0)
    assert excinfo.value.code == "engine_unknown"
```

- [ ] **Step 3: Красный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_engine.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'h3_48gb.engine'`.

- [ ] **Step 4: Реализация**

`h3_48gb/engine.py`:
```python
"""Which render engine this process drives (spec §3.3.1): `mlx` (the Mac, the default) or
`sglang` (alex-neuro). Read from `H3_ENGINE` on every call rather than cached at import, so a
test can flip it with monkeypatch and the web server and the worker can never disagree about a
value one of them read earlier.

No import from the rest of the package: `web`, `worker`, `assemble` and `cli` all read this.
"""
from __future__ import annotations

import os

ENGINES = ("mlx", "sglang")
DEFAULT_ENGINE = "mlx"


class UnknownEngine(ValueError):
    """`H3_ENGINE` names something outside `ENGINES`."""


def current(environ=None) -> str:
    raw = (os.environ if environ is None else environ).get("H3_ENGINE", "")
    value = raw.strip().lower() or DEFAULT_ENGINE
    if value not in ENGINES:
        raise UnknownEngine(f"H3_ENGINE={raw!r}: expected one of {ENGINES}")
    return value


def is_sglang(environ=None) -> bool:
    return current(environ) == "sglang"
```

`worker.py`: `from h3_48gb import engine` рядом с остальными импортами; модульная константа `_PLATFORM = sys.platform` (под `WORKER_LOCK_NAME`).
```python
def job_command(job, python: str = sys.executable, *, platform: str | None = None) -> list[str]:
    command = [python, "-m", "h3_48gb", *job.args]
    if (platform or _PLATFORM) == "darwin":
        return ["caffeinate", "-dimsu", *command]
    return command
```
(Докстринг сохранить, дописать абзац: «`caffeinate` exists only on macOS; on alex-neuro the job runs bare — nothing there idle-sleeps a server».)
```python
@contextlib.contextmanager
def _caffeinate_block(spawn=subprocess.Popen, *, platform: str | None = None):
    if (platform or _PLATFORM) != "darwin":
        yield
        return
    proc = spawn(["caffeinate", "-dimsu", "-w", str(os.getpid())])
    try:
        yield
    finally:
        proc.terminate()
        proc.wait()
```
`main_loop`, строка 976: `if not engine.is_sglang() and _llm_holds_gpu(outdir):`.

`web.py`: `from h3_48gb import engine`; `_PLATFORM = sys.platform` рядом с `LOOPBACK`; в `build_state` в возвращаемый словарь добавить `"engine": engine.current(), "platform": _PLATFORM,`. В начало тела `_reveal_job`:
```python
        if _PLATFORM != "darwin":
            raise CliError("reveal_unsupported",
                           "«Показать в Finder» есть только на macOS; файл лежит на сервере",
                           {"platform": _PLATFORM})
```
`ERROR_STATUS["reveal_unsupported"] = 409`.

`cli.py`: в `ERROR_CODES`:
```python
    "engine_unknown": "H3_ENGINE names an engine other than mlx or sglang",
    "reveal_unsupported": "reveal-in-Finder was asked for on a machine that is not macOS",
```
и функция:
```python
def _require_known_engine() -> str:
    from h3_48gb import engine

    try:
        return engine.current()
    except engine.UnknownEngine as exc:
        raise CliError("engine_unknown", str(exc), {}) from exc
```
вызывается первой строкой `run_worker` и `run_web`.

- [ ] **Step 5: Зелёный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_engine.py tests/test_worker.py tests/test_web.py -q -p no:cacheprovider`
Expected: PASS. Если тест в `test_worker.py` сверяет `job_command` с `caffeinate` (на Маке `sys.platform == "darwin"`, он остаётся зелёным) — не трогать.

- [ ] **Step 6: Мутации**

(а) В `job_command` заменить `== "darwin"` на `!= "darwin"` → `test_job_command_wraps_caffeinate_only_on_darwin` FAIL. (б) Вернуть в `main_loop` старое `if _llm_holds_gpu(outdir):` → `test_the_llm_gate_is_never_consulted_on_sglang` FAIL `assert [PosixPath(...)] == []`. (в) Убрать проверку в `_reveal_job` → `test_reveal_is_refused_off_darwin` FAIL `assert 404 == 409`. Вернуть, ошибки — в отчёт.

- [ ] **Step 7: Полный прогон и commit**

```bash
env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest -q -p no:cacheprovider
git add h3_48gb/engine.py h3_48gb/worker.py h3_48gb/web.py h3_48gb/cli.py tests/conftest.py tests/test_engine.py
git commit -m "feat: H3_ENGINE=mlx|sglang; caffeinate и Finder только на Darwin, LLM-ворота воркера только для mlx"
```

---

## Task 3: Аргументы сцены при sglang: парсер, таблица форматов, сетка, валидация в вебе, оценка времени

**Files:**
- Create: `h3_48gb/engines/__init__.py`, `h3_48gb/engines/sglang_args.py`, `h3_48gb/engines/estimate.py`
- Modify: `pyproject.toml:33` — `packages = ["h3_48gb", "h3_48gb.engines"]` (список пакетов явный; без этого обычная установка подпакет не увидит)
- Modify: `h3_48gb/web.py:607` (`check_path_flags` — параметр `flags`), `h3_48gb/web.py:2350` (`validate_args`), `h3_48gb/web.py:2478-2504` (`prepare_submission`), `h3_48gb/web.py:3844-3878` (`_estimate_only`)
- Modify: `h3_48gb/cli.py:148` (`ERROR_CODES` дополняется кодами sglang)
- Test: `tests/test_sglang_args.py` (новый), `tests/test_sglang_web.py` (новый)

**Interfaces:**
- Consumes: `engine.is_sglang()` (задача 2).
- Produces (`h3_48gb/engines/sglang_args.py`):
  - константы `FPS=24`, `FRAMES_PER_CHUNK=17`, `FRAME_REMAINDER=5`, `MIN_SECONDS=3.0`, `MAX_SECONDS=15.0`, `DEFAULT_STEPS=50`, `DEFAULT_SEED=42`, `DEFAULT_MAX_REF_IMAGES=6`, `CANVAS_TABLE`, `ASPECT_RATIOS`, `TASKS`, `PATH_FLAGS`, `ERROR_CODES`
  - `class SglangArgsError(ValueError)` с `.code`, `.message`, `.detail`
  - `@dataclass(frozen=True) class SglangSpec(prompt: str, width: int, height: int, duration: float, frames: int, steps: int, seed: int, tag: str, outdir: str, image: str | None, refs: tuple[str, ...], audio: tuple[str, ...], task: str, short_edge: int, aspect_ratio: str)`
  - `grid_frames_up(frames: int) -> int`, `max_ref_images(environ=None) -> int`, `parse(argv, *, environ=None, check_files=True) -> SglangSpec`, `output_stem(spec) -> str`, `dry_run_report(spec) -> dict`
- Produces (`h3_48gb/engines/estimate.py`): `HISTORY_NAME = "sglang-history.jsonl"`, `record(outdir, *, width, height, frames, wall_s) -> None`, `estimate_seconds(outdir, *, width, height, frames) -> dict` (`{"seconds", "source": "history"|"table", "samples"}`)
- Produces (`web`): `check_path_flags(args, roots, flags=PATH_FLAGS)`; при sglang `validate_args` и `prepare_submission` работают без подпроцесса и без MLX-парсера.

- [ ] **Step 1: Падающие тесты парсера**

`tests/test_sglang_args.py`:
```python
"""The sglang argv parser (spec §3.3.2, §4.1.4-5) against hand-written argv. The argv that
`assemble` really builds is tested in test_sglang_scenes.py (task 5) -- spec §6 asks for both."""
import pytest

from h3_48gb.engines import sglang_args as sa
from h3_48gb.engines.sglang_args import SglangArgsError, SglangSpec


def _png(path):
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return str(path)


def _argv(tmp_path, *extra, duration="7.291666666666667"):
    return ["generate", "кот на подоконнике", "--width", "896", "--height", "512",
            "--duration", duration, "--tag", "scene-0-ab12", "--outdir", str(tmp_path), *extra]


def test_the_frame_grid_is_17n_plus_5():
    assert [sa.grid_frames_up(n) for n in (1, 5, 6, 120, 124, 168, 175, 192, 209)] == \
        [5, 5, 22, 124, 124, 175, 175, 192, 209]


def test_a_first_scene_with_one_reference_parses_to_ref2va_with_table_aspect(tmp_path):
    ref = _png(tmp_path / "a.png")
    spec = sa.parse(_argv(tmp_path, "--ref", ref))
    assert spec == SglangSpec(prompt="кот на подоконнике", width=896, height=512,
                              duration=7.291666666666667, frames=175, steps=50, seed=42,
                              tag="scene-0-ab12", outdir=str(tmp_path), image=None, refs=(ref,),
                              audio=(), task="ref2va", short_edge=512, aspect_ratio="16:9")


def test_a_scene_without_any_reference_is_refused(tmp_path):
    """spec §4.1.3: the ref2va server serves only ref2va; a scene with nothing to reference has
    no task it can run as -- refused here, not by a render that fails an hour later."""
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(_argv(tmp_path))
    assert (excinfo.value.code, excinfo.value.message) == \
        ("ref2va_needs_reference", "нужен хотя бы один референс (@тег) в сцене")


def test_a_chained_scene_with_refs_and_audio_parses_to_ref2va(tmp_path):
    kf, r1, r2 = _png(tmp_path / "kf.png"), _png(tmp_path / "a.png"), _png(tmp_path / "b.png")
    wav = tmp_path / "piece.wav"
    wav.write_bytes(b"RIFF")
    spec = sa.parse(_argv(tmp_path, "--image", kf, "--ref", r1, "--ref", r2, "--audio", str(wav),
                          "--aspect", "auto", "--steps", "50", "--seed", "7", "--task", "ref2va"))
    assert (spec.task, spec.image, spec.refs, spec.audio, spec.aspect_ratio, spec.seed) == \
        ("ref2va", kf, (r1, r2), (str(wav),), "auto", 7)


@pytest.mark.parametrize("flag", ["--turbo-strength", "--checkpoint", "--latent", "--save-latent-tail",
                                  "--adaln-cache", "--end-image", "--prompt-file"])
def test_any_flag_outside_the_list_is_refused_by_name(tmp_path, flag):
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(_argv(tmp_path, flag, "1"))
    assert (excinfo.value.code, excinfo.value.message) == \
        ("unsupported_on_sglang", f"unsupported_on_sglang: {flag}")


@pytest.mark.parametrize("width,height", [(1344, 768), (896, 576), (512, 512)])
def test_a_canvas_outside_the_table_is_refused(tmp_path, width, height):
    argv = _argv(tmp_path)
    argv[argv.index("--width") + 1] = str(width)
    argv[argv.index("--height") + 1] = str(height)
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(argv)
    assert excinfo.value.code == "canvas_unsupported_on_sglang"


@pytest.mark.parametrize("duration", ["2.5", "15.5"])
def test_duration_outside_3_to_15_is_refused(tmp_path, duration):
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(_argv(tmp_path, duration=duration))
    assert excinfo.value.code == "duration_out_of_range"


def test_an_off_grid_duration_names_the_next_grid_point(tmp_path):
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(_argv(tmp_path, duration="7.0"))
    assert (excinfo.value.code, excinfo.value.detail) == \
        ("duration_off_grid", {"frames": 168, "next_grid_frames": 175,
                               "next_grid_seconds": 175 / 24})


@pytest.mark.parametrize("task", ["t2va", "fl2va"])
def test_any_task_but_ref2va_is_refused(tmp_path, task):
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(_argv(tmp_path, "--task", task, "--ref", _png(tmp_path / "a.png")))
    assert excinfo.value.code == "sglang_args_invalid"


def test_a_keyframe_without_any_reference_is_refused(tmp_path):
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(_argv(tmp_path, "--image", _png(tmp_path / "kf.png")))
    assert (excinfo.value.code, excinfo.value.message) == \
        ("ref2va_needs_reference", "нужен хотя бы один референс (@тег) в сцене")


def test_an_audio_reference_alone_is_enough(tmp_path):
    piece = tmp_path / "piece.wav"
    piece.write_bytes(b"RIFF")
    assert sa.parse(_argv(tmp_path, "--audio", str(piece))).task == "ref2va"


def test_more_refs_than_the_limit_is_refused(tmp_path):
    refs = []
    for i in range(3):
        refs += ["--ref", _png(tmp_path / f"r{i}.png")]
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(_argv(tmp_path, *refs), environ={"H3_MAX_REF_IMAGES": "2"})
    assert (excinfo.value.code, excinfo.value.detail) == \
        ("too_many_reference_images", {"count": 3, "limit": 2})


def test_the_default_ref_limit_is_six():
    assert sa.max_ref_images({}) == 6


def test_a_missing_condition_file_is_refused_unless_files_are_not_checked(tmp_path):
    argv = _argv(tmp_path, "--ref", str(tmp_path / "nope.png"))
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(argv)
    assert excinfo.value.code == "condition_file_missing"
    assert sa.parse(argv, check_files=False).refs == (str(tmp_path / "nope.png"),)


def test_a_repeated_single_value_flag_is_refused(tmp_path):
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(_argv(tmp_path, "--seed", "1", "--seed", "2"))
    assert excinfo.value.code == "sglang_args_invalid"


def test_aspect_must_be_auto_or_the_table_aspect(tmp_path):
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(_argv(tmp_path, "--aspect", "9:16", "--ref", _png(tmp_path / "a.png")))
    assert excinfo.value.code == "sglang_args_invalid"


def test_dry_run_report_and_output_stem(tmp_path):
    spec = sa.parse(_argv(tmp_path, "--ref", _png(tmp_path / "a.png")))
    assert sa.output_stem(spec) == f"{tmp_path}/h3-scene-0-ab12-896x512"
    assert sa.dry_run_report(spec) == {
        "dry_run": True, "engine": "sglang", "output_stem": f"{tmp_path}/h3-scene-0-ab12-896x512",
        "canvas": "896x512", "duration_seconds": 7.291666666666667, "frames": 175,
        "grid_points": 50, "task": "ref2va"}
```

- [ ] **Step 2: Падающие тесты оценки и веба**

`tests/test_sglang_web.py`:
```python
"""Validation and estimates through the HTTP API when H3_ENGINE=sglang (spec §3.3.4, §3.3.15)."""
import json

import pytest

from h3_48gb.engines import estimate as est
from h3_48gb.engines import sglang_args as sa
from test_web import _call, _pending, queue_server  # noqa: F401  (fixture)


def test_estimate_prefers_the_median_of_the_last_ten_matching_runs(tmp_path):
    for wall in (100, 900, 200, 300):
        est.record(tmp_path, width=896, height=512, frames=175, wall_s=wall)
    est.record(tmp_path, width=896, height=512, frames=124, wall_s=5000)
    assert est.estimate_seconds(tmp_path, width=896, height=512, frames=175) == \
        {"seconds": 250.0, "source": "history", "samples": 4}


def test_estimate_uses_only_the_last_ten_runs(tmp_path):
    for wall in range(1, 13):
        est.record(tmp_path, width=896, height=512, frames=175, wall_s=wall)
    # last ten are 3..12 -> median 7.5; the first ten (1..10) would give 5.5
    assert est.estimate_seconds(tmp_path, width=896, height=512, frames=175) == \
        {"seconds": 7.5, "source": "history", "samples": 10}


def test_estimate_falls_back_to_the_bench_table_by_nearest_frames(tmp_path):
    assert est.estimate_seconds(tmp_path, width=896, height=512, frames=175) == \
        {"seconds": 2810.0, "source": "table", "samples": 0}
    assert est.estimate_seconds(tmp_path, width=896, height=512, frames=130) == \
        {"seconds": 345.0, "source": "table", "samples": 0}
    assert est.estimate_seconds(tmp_path, width=768, height=768, frames=209) == \
        {"seconds": 2820.0, "source": "table", "samples": 0}


def test_estimate_skips_corrupt_history_lines(tmp_path):
    (tmp_path / est.HISTORY_NAME).write_text("not json\n" + json.dumps(
        {"width": 896, "height": 512, "frames": 175, "wall_s": 42}) + "\n", encoding="utf-8")
    assert est.estimate_seconds(tmp_path, width=896, height=512, frames=175)["seconds"] == 42.0


def _sglang_job_args(live, *extra):
    """Every sglang scene carries at least one reference (spec §4.1.3)."""
    ref = live.outdir / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n")
    return ["generate", "кот", "--width", "896", "--height", "512",
            "--duration", str(175 / 24), "--tag", "ночь", "--outdir", str(live.outdir),
            "--ref", str(ref), *extra]


def test_a_sglang_job_is_queued_without_a_dry_run_subprocess(queue_server, monkeypatch):  # noqa: F811
    from h3_48gb import web

    monkeypatch.setenv("H3_ENGINE", "sglang")
    monkeypatch.setattr(web.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no subprocess on the sglang path")))
    status, body = _call(queue_server, "POST", "/api/jobs",
                         {"args": _sglang_job_args(queue_server), "note": ""})
    assert status == 200, body
    job = body["job"]
    # queue.submit relocates --outdir into a per-job subdirectory; the stem the queue stores must
    # be exactly the one the adapter will derive from the relocated argv.
    assert sa.output_stem(sa.parse(job["args"], check_files=False)) == job["output_stem"]
    assert body["estimate"] == {"seconds": 2810.0, "source": "table", "samples": 0}


def test_mlx_flags_are_refused_on_sglang(queue_server, monkeypatch):  # noqa: F811
    monkeypatch.setenv("H3_ENGINE", "sglang")
    status, body = _call(queue_server, "POST", "/api/jobs",
                         {"args": _sglang_job_args(queue_server, "--turbo-strength", "0.45"),
                          "note": ""})
    assert (status, body["error"]["code"], body["error"]["message"]) == \
        (400, "unsupported_on_sglang", "unsupported_on_sglang: --turbo-strength")
    assert _pending(queue_server) == []


def test_a_ref_outside_the_roots_is_refused(queue_server, monkeypatch):  # noqa: F811
    monkeypatch.setenv("H3_ENGINE", "sglang")
    status, body = _call(queue_server, "POST", "/api/jobs",
                         {"args": _sglang_job_args(queue_server, "--ref", "/etc/passwd"), "note": ""})
    assert (status, body["error"]["code"]) == (400, "path_outside_root")


def test_estimate_route_on_sglang(queue_server, monkeypatch):  # noqa: F811
    monkeypatch.setenv("H3_ENGINE", "sglang")
    status, body = _call(queue_server, "POST", "/api/estimate",
                         {"args": _sglang_job_args(queue_server)})
    assert (status, body) == (200, {"ok": True, "estimate":
                                    {"seconds": 2810.0, "source": "table", "samples": 0}})
```

- [ ] **Step 3: Красный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_sglang_args.py tests/test_sglang_web.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'h3_48gb.engines'`.

- [ ] **Step 4: Реализация парсера**

`h3_48gb/engines/__init__.py`:
```python
"""Render engines other than the in-tree MLX pipeline. Nothing here imports mlx."""
```
`pyproject.toml`, секция `[tool.setuptools]`: `packages = ["h3_48gb", "h3_48gb.engines"]`.

`h3_48gb/engines/sglang_args.py`:
```python
"""`h3 generate` argv as the sglang engine understands it (spec §3.3.2, §4.1.4, §4.1.5).

`cli.build_parser` is never used on this path: it fills in MLX defaults (`--turbo-lora`,
`--adaln-cache`, `--steps 8`, `cli.py:52-58`, `542-545`) and `RunSpec` refuses a missing LoRA file
(`cli.py:451`), none of which exist in the panel image. Any flag outside `_VALUE_FLAGS` is refused
by name. The refusals mirror what the sglang server itself rejects with a bare 400
(`request_validation.py`), so a scene fails in the form before it costs a queue slot.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

FPS = 24
FRAMES_PER_CHUNK = 17
FRAME_REMAINDER = 5
MIN_SECONDS = 3.0
MAX_SECONDS = 15.0
DEFAULT_STEPS = 50
DEFAULT_SEED = 42
DEFAULT_MAX_REF_IMAGES = 6

#: Panel canvas -> (short_edge, aspect_ratio) (spec §4.1.4). The delivered frame size is read from
#: the mp4, not from this table: sglang picks its own frame for a short edge and an aspect.
CANVAS_TABLE = {(896, 512): (512, "16:9"), (512, 896): (512, "9:16"), (768, 768): (768, "1:1")}
ASPECT_RATIOS = ("auto", "21:9", "16:9", "4:3", "3:2", "1:1", "2:3", "3:4", "9:16")
#: spec §4.1.3, probe 2026-10-07: the server runs with VARIANT=ref2va and serves only ref2va
#: (t2va is accepted with HTTP 200 and then fails "not served by MiniMax H3 partition 'ref2va'").
TASKS = ("ref2va",)

#: Path policy for `web.check_path_flags` on this engine (the MLX table `web.PATH_FLAGS` is pinned
#: to the MLX parser's flags by test_web.py, so the two tables stay separate).
PATH_FLAGS = {"--image": "read", "--ref": "read", "--audio": "read", "--outdir": "write"}

_VALUE_FLAGS = ("--width", "--height", "--duration", "--steps", "--seed", "--tag", "--outdir",
                "--image", "--ref", "--audio", "--task", "--aspect")
_REPEATABLE = ("--ref", "--audio")

ERROR_CODES = {
    "unsupported_on_sglang": "a generate flag the sglang engine does not implement",
    "sglang_args_invalid": "a sglang generate argument is missing, repeated or malformed",
    "canvas_unsupported_on_sglang": "--width/--height is not a canvas the sglang format table maps",
    "duration_out_of_range": "--duration is outside sglang's 3..15 s",
    "duration_off_grid": "--duration does not land on sglang's 17n+5 frame grid at 24 fps",
    "ref2va_needs_reference": "a scene needs at least one reference (an @tag picture, or a clip's track piece)",
    "too_many_reference_images": "more reference pictures than H3_MAX_REF_IMAGES allows per scene",
    "condition_file_missing": "a --image/--ref/--audio path does not exist",
}


class SglangArgsError(ValueError):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = dict(detail or {})


@dataclass(frozen=True)
class SglangSpec:
    prompt: str
    width: int
    height: int
    duration: float
    frames: int
    steps: int
    seed: int
    tag: str
    outdir: str
    image: str | None
    refs: tuple[str, ...]
    audio: tuple[str, ...]
    task: str
    short_edge: int
    aspect_ratio: str


def grid_frames_up(frames: int) -> int:
    frames = max(int(frames), FRAME_REMAINDER)
    return frames + (FRAME_REMAINDER - frames) % FRAMES_PER_CHUNK


def max_ref_images(environ=None) -> int:
    raw = (os.environ if environ is None else environ).get("H3_MAX_REF_IMAGES", "").strip()
    if not raw:
        return DEFAULT_MAX_REF_IMAGES
    try:
        value = int(raw)
    except ValueError:
        raise SglangArgsError("sglang_args_invalid",
                              f"H3_MAX_REF_IMAGES={raw!r} — не целое число", {"value": raw}) from None
    if value < 1:
        raise SglangArgsError("sglang_args_invalid", "H3_MAX_REF_IMAGES должен быть ≥ 1",
                              {"value": value})
    return value


def _collect(args: list[str]) -> tuple[str | None, dict[str, list[str]]]:
    prompt = None
    values: dict[str, list[str]] = {}
    index = 1
    while index < len(args):
        token = args[index]
        if token.startswith("--"):
            flag, equals, inline = token.partition("=")
            if flag not in _VALUE_FLAGS:
                raise SglangArgsError("unsupported_on_sglang", f"unsupported_on_sglang: {flag}",
                                      {"flag": flag})
            if equals:
                value = inline
            elif index + 1 < len(args):
                index += 1
                value = args[index]
            else:
                raise SglangArgsError("sglang_args_invalid", f"{flag} без значения", {"flag": flag})
            if flag in values and flag not in _REPEATABLE:
                raise SglangArgsError("sglang_args_invalid", f"{flag} указан дважды", {"flag": flag})
            values.setdefault(flag, []).append(value)
        elif prompt is None:
            prompt = token
        else:
            raise SglangArgsError("sglang_args_invalid", "больше одного промпта в аргументах",
                                  {"extra": token})
        index += 1
    return prompt, values


def _one(values, flag, default=None, *, required=False):
    if flag in values:
        return values[flag][0]
    if required:
        raise SglangArgsError("sglang_args_invalid", f"не хватает {flag}", {"flag": flag})
    return default


def _number(values, flag, kind, default=None, *, required=False):
    raw = _one(values, flag, default, required=required)
    try:
        return kind(raw)
    except (TypeError, ValueError):
        raise SglangArgsError("sglang_args_invalid", f"{flag}={raw!r} — не число",
                              {"flag": flag, "value": raw}) from None


def parse(argv, *, environ=None, check_files: bool = True) -> SglangSpec:
    args = [str(item) for item in argv]
    if not args or args[0] != "generate":
        raise SglangArgsError("sglang_args_invalid", "ожидалась команда generate", {"args": args[:1]})
    prompt, values = _collect(args)
    if prompt is None or not prompt.strip():
        raise SglangArgsError("prompt_missing", "нет промпта сцены", {})

    width = _number(values, "--width", int, required=True)
    height = _number(values, "--height", int, required=True)
    if (width, height) not in CANVAS_TABLE:
        raise SglangArgsError("canvas_unsupported_on_sglang",
                              f"холст {width}x{height} не поддержан на sglang; можно: "
                              f"{', '.join(f'{w}x{h}' for w, h in CANVAS_TABLE)}",
                              {"width": width, "height": height})
    short_edge, table_aspect = CANVAS_TABLE[(width, height)]

    duration = _number(values, "--duration", float, required=True)
    if not MIN_SECONDS <= duration <= MAX_SECONDS:
        raise SglangArgsError("duration_out_of_range",
                              f"длительность {duration:g} с вне {MIN_SECONDS:g}–{MAX_SECONDS:g} с",
                              {"duration": duration})
    frames = round(duration * FPS)
    if grid_frames_up(frames) != frames:
        nxt = grid_frames_up(frames)
        raise SglangArgsError("duration_off_grid",
                              f"длительность {duration:g} с = {frames} кадров, не на сетке 17n+5; "
                              f"ближайшая вверх — {nxt / FPS:.3f} с",
                              {"frames": frames, "next_grid_frames": nxt,
                               "next_grid_seconds": nxt / FPS})

    steps = _number(values, "--steps", int, DEFAULT_STEPS)
    seed = _number(values, "--seed", int, DEFAULT_SEED)
    if steps < 1 or seed < 0:
        raise SglangArgsError("sglang_args_invalid", "--steps ≥ 1 и --seed ≥ 0",
                              {"steps": steps, "seed": seed})
    tag = _one(values, "--tag", required=True)
    outdir = _one(values, "--outdir", required=True)
    image = _one(values, "--image")
    refs = tuple(values.get("--ref", []))
    audio = tuple(values.get("--audio", []))
    has_reference = bool(refs or audio)

    task = _one(values, "--task", "ref2va")
    if task not in TASKS:
        raise SglangArgsError("sglang_args_invalid",
                              f"--task {task!r}: сервер H3 поднят как ref2va и обслуживает только его",
                              {"task": task})
    if not has_reference:
        raise SglangArgsError("ref2va_needs_reference", "нужен хотя бы один референс (@тег) в сцене",
                              {"keyframe": image})
    limit = max_ref_images(environ)
    if len(refs) > limit:
        raise SglangArgsError("too_many_reference_images",
                              f"картинок-референсов {len(refs)}, а можно не больше {limit}",
                              {"count": len(refs), "limit": limit})

    aspect = _one(values, "--aspect", table_aspect)
    if aspect not in ("auto", table_aspect):
        raise SglangArgsError("sglang_args_invalid",
                              f"--aspect {aspect!r}: для {width}x{height} можно auto или {table_aspect}",
                              {"aspect": aspect})

    if check_files:
        for path in [image, *refs, *audio]:
            if path is not None and not Path(path).is_file():
                raise SglangArgsError("condition_file_missing", f"нет файла {path}", {"path": path})

    return SglangSpec(prompt=prompt, width=width, height=height, duration=duration, frames=frames,
                      steps=steps, seed=seed, tag=tag, outdir=outdir, image=image, refs=refs,
                      audio=audio, task=task, short_edge=short_edge, aspect_ratio=aspect)


def output_stem(spec: SglangSpec) -> str:
    return f"{spec.outdir}/h3-{spec.tag}-{spec.width}x{spec.height}"


def dry_run_report(spec: SglangSpec) -> dict:
    return {"dry_run": True, "engine": "sglang", "output_stem": output_stem(spec),
            "canvas": f"{spec.width}x{spec.height}", "duration_seconds": spec.duration,
            "frames": spec.frames, "grid_points": spec.steps, "task": spec.task}
```

- [ ] **Step 5: Реализация оценки**

`h3_48gb/engines/estimate.py`:
```python
"""Scene wall-time estimates on sglang (spec §3.3.15): the median `wall_s` of the last ten scenes
of the same canvas and frame count; until there is history, the h3-bench table (medians of
`results-*.jsonl` on alex-neuro, 2026-10-07, completed runs only), marked `source: "table"`."""
from __future__ import annotations

import json
import statistics
from pathlib import Path

HISTORY_NAME = "sglang-history.jsonl"
HISTORY_WINDOW = 10

#: short edge -> {frames: median wall_s}. Counts behind each median: 512/124 ×21, 512/175 ×4,
#: 512/192 ×4, 512/243 ×6, 512/277 ×1, 768/124 ×1, 768/192 ×2, 768/209 ×10. The 512/175 and
#: 512/192 rows are the beach ref2va runs (references scaled to 2048 px) -- kept on purpose: on
#: this server every scene is ref2va now (spec §4.1.3), so they are the closest to what runs.
FALLBACK_SECONDS = {512: {124: 345.0, 175: 2810.0, 192: 2980.0, 243: 580.0, 277: 720.0},
                    768: {124: 1140.0, 192: 1793.0, 209: 2820.0}}


def record(outdir, *, width: int, height: int, frames: int, wall_s: float) -> None:
    path = Path(outdir) / HISTORY_NAME
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"width": width, "height": height, "frames": frames,
                                 "wall_s": float(wall_s)}) + "\n")


def _history(outdir) -> list[dict]:
    path = Path(outdir) / HISTORY_NAME
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("wall_s"), (int, float)):
            rows.append(row)
    return rows


def estimate_seconds(outdir, *, width: int, height: int, frames: int) -> dict:
    same = [row["wall_s"] for row in _history(outdir)
            if (row.get("width"), row.get("height"), row.get("frames")) == (width, height, frames)]
    same = same[-HISTORY_WINDOW:]
    if same:
        return {"seconds": float(statistics.median(same)), "source": "history", "samples": len(same)}
    short = min(width, height)
    edge = short if short in FALLBACK_SECONDS else min(FALLBACK_SECONDS, key=lambda e: abs(e - short))
    table = FALLBACK_SECONDS[edge]
    nearest = min(table, key=lambda n: (abs(n - frames), n))
    return {"seconds": table[nearest], "source": "table", "samples": 0}
```

- [ ] **Step 6: Подключить в web.py и cli.py**

`cli.py`, сразу после закрывающей скобки `ERROR_CODES = {...}`:
```python
from h3_48gb.engines.sglang_args import ERROR_CODES as _SGLANG_ERROR_CODES  # noqa: E402

ERROR_CODES.update(_SGLANG_ERROR_CODES)
```
`web.py`: импорты `from h3_48gb.engines import estimate as sglang_estimate`, `from h3_48gb.engines import sglang_args`. `check_path_flags(args, roots, flags=PATH_FLAGS)` — в теле `PATH_FLAGS` заменить на `flags` (две строки: `if flag not in flags`, `write=flags[flag] == "write"`).

Первой строкой тела `validate_args`: `if engine.is_sglang(): return _validate_args_sglang(args)`; новая функция рядом:
```python
def _validate_args_sglang(args) -> dict:
    """spec §3.3.4: on sglang validation is the adapter's own parser, in process -- known flags,
    the format table, the frame grid -- not a `generate --dry-run` subprocess."""
    try:
        spec = sglang_args.parse(args)
    except sglang_args.SglangArgsError as exc:
        raise CliError(exc.code, exc.message, exc.detail) from exc
    return sglang_args.dry_run_report(spec)
```
Первой строкой тела `prepare_submission`: `if engine.is_sglang(): return _prepare_submission_sglang(args, roots)`;
```python
def _prepare_submission_sglang(args, roots) -> dict:
    args = [str(item) for item in args]
    if not args or args[0] != ALLOWED_COMMAND:
        raise CliError("command_not_allowed",
                       f"only `h3 {ALLOWED_COMMAND}` may be queued",
                       {"command": args[0] if args else None, "allowed": ALLOWED_COMMAND})
    argv = check_path_flags(args, roots, flags=sglang_args.PATH_FLAGS)
    report = validate_args(argv)
    resolve_within(report["output_stem"], roots, write=True)
    spec = sglang_args.parse(argv, check_files=False)
    cost = sglang_estimate.estimate_seconds(roots["outdir"], width=spec.width, height=spec.height,
                                            frames=spec.frames)
    return {"args": argv, "report": report, "estimate": cost,
            "prompt_text": None, "prompt_source": None}
```
В `_estimate_only` сразу после `args = self._args_of(payload)`:
```python
        if engine.is_sglang():
            try:
                spec = sglang_args.parse(args, check_files=False)
            except sglang_args.SglangArgsError as exc:
                raise CliError(exc.code, exc.message, exc.detail) from exc
            return 200, "application/json", _json_bytes({"ok": True, "estimate":
                sglang_estimate.estimate_seconds(self.server.outdir, width=spec.width,
                                                 height=spec.height, frames=spec.frames)})
```

- [ ] **Step 7: Зелёный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_sglang_args.py tests/test_sglang_web.py tests/test_web.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 8: Мутации**

(а) В `grid_frames_up` заменить `(FRAME_REMAINDER - frames)` на `(frames - FRAME_REMAINDER)` → `test_the_frame_grid_is_17n_plus_5` FAIL. (б) Убрать проверку `if not has_reference:` → `test_a_scene_without_any_reference_is_refused` и `test_a_keyframe_without_any_reference_is_refused` FAIL `DID NOT RAISE`. (в) Убрать строку `if engine.is_sglang(): return _prepare_submission_sglang(...)` → `test_a_sglang_job_is_queued_without_a_dry_run_subprocess` FAIL (`AssertionError: no subprocess on the sglang path` → 500). (г) В `estimate_seconds` заменить `same[-HISTORY_WINDOW:]` на `same[:HISTORY_WINDOW]` → `test_estimate_uses_only_the_last_ten_runs` FAIL `'seconds': 5.5 != 7.5`. Ошибки — в отчёт.

- [ ] **Step 9: Полный прогон и commit**

```bash
env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest -q -p no:cacheprovider
git add h3_48gb/engines h3_48gb/web.py h3_48gb/cli.py pyproject.toml tests/test_sglang_args.py tests/test_sglang_web.py
git commit -m "feat(sglang): парсер и валидатор argv, таблица форматов, сетка 17n+5, оценка времени"
```

---

## Task 4: Библиотека референсов: данные, версии, API, сборка Ref2VA

**Files:**
- Create: `h3_48gb/library.py`
- Modify: `h3_48gb/project.py:139-150` (`_OWNED_TOP_LEVEL_FIELDS` += `"references"`), `Project._apply`/`as_dict`, новый метод `Project.set_references`, `create_project` (`"references": []`)
- Modify: `h3_48gb/web.py` — маршруты `GET/POST /api/library`, `PUT /api/library/<name>`, `GET/PUT /api/projects/<id>/references`; `ERROR_STATUS`
- Modify: `h3_48gb/cli.py:148` (`ERROR_CODES` += коды библиотеки)
- Test: `tests/test_library.py` (новый), `tests/test_library_web.py` (новый)

**Interfaces:**
- Produces (`h3_48gb/library.py`):
  - `TAG_RE`, `KINDS = ("person", "object", "environment", "style", "voice")`, `IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")`, `AUDIO_SUFFIXES = (".mp3", ".wav")`, `MAX_IMAGES = 4`, `ERROR_CODES`
  - `class LibraryError(ValueError)` с `.code`, `.message`, `.detail`
  - `create_card(outdir, *, tag, kind, description, assets, now=None) -> dict`
  - `update_card(outdir, tag, *, kind=None, description=None, assets=None, now=None) -> dict`
  - `get_card(outdir, tag, version=None) -> dict` → `{"tag", "kind", "description", "version", "latest_version", "assets": [abs str]}`
  - `list_cards(outdir) -> list[dict]`
  - `scene_tags(text) -> list[str]` (порядок первого упоминания; `LibraryError("tag_invalid")` на `@Alice`)
  - `@dataclass(frozen=True) class Ref2VAScene(prompt: str, images: tuple[str, ...], audios: tuple[str, ...], subjects: tuple[str, ...])`
  - `build_ref2va(scene_prompt, references, outdir) -> Ref2VAScene` (`references` — `[{"tag", "version"}]` проекта)
  - `references_context(cards: list[dict]) -> str` — блок для LLM
- Produces (`project.py`): атрибут `Project.references: list[dict]`, `Project.set_references(references: list[dict]) -> Project` (локированный).
- Правило нумерации (по коду sglang, см. Global Constraints): keyframe не нумеруется; `<Picture N>` — картинки референсов с 1 в порядке условий; `<Audio j>` — аудио-референсы с 1 в порядке условий.

- [ ] **Step 1: Падающие тесты модуля**

`tests/test_library.py`:
```python
"""The reference library (spec §3.5) and the Ref2VA assembly, pinned by exact equality."""
import pytest

from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb.library import LibraryError, Ref2VAScene


def _img(path, data=b"\x89PNG\r\n\x1a\n-"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def out(tmp_path):
    return tmp_path / "outdir"


def test_create_card_copies_assets_into_version_one(out):
    src = _img(out / "uploads" / "front.png")
    card = lib.create_card(out, tag="@alice", kind="person",
                           description="a young woman with golden-blonde wavy hair",
                           assets=[src], now="2026-10-07T10:00:00")
    assert card == {"tag": "@alice", "kind": "person",
                    "description": "a young woman with golden-blonde wavy hair",
                    "version": 1, "latest_version": 1,
                    "assets": [str(out / "library" / "alice" / "v1" / "01-front.png")]}
    assert (out / "library" / "alice" / "v1" / "01-front.png").read_bytes() == src.read_bytes()


@pytest.mark.parametrize("tag", ["alice", "@a", "@Alice", "@" + "x" * 33, "@al ice"])
def test_a_bad_tag_is_refused(out, tag):
    with pytest.raises(LibraryError) as excinfo:
        lib.create_card(out, tag=tag, kind="person", description="d",
                        assets=[_img(out / "uploads" / "a.png")])
    assert excinfo.value.code == "library_tag_invalid"


def test_a_duplicate_tag_is_refused(out):
    lib.create_card(out, tag="@alice", kind="person", description="d",
                    assets=[_img(out / "uploads" / "a.png")])
    with pytest.raises(LibraryError) as excinfo:
        lib.create_card(out, tag="@alice", kind="object", description="d",
                        assets=[_img(out / "uploads" / "b.png")])
    assert excinfo.value.code == "library_tag_exists"


@pytest.mark.parametrize("kind,names", [
    ("person", []), ("person", ["1.png", "2.png", "3.png", "4.png", "5.png"]),
    ("person", ["a.mp3"]), ("voice", ["a.png"]), ("voice", ["a.mp3", "b.mp3"]),
    ("person", ["a.webp"])])
def test_asset_rules(out, kind, names):
    assets = [_img(out / "uploads" / name) for name in names]
    with pytest.raises(LibraryError) as excinfo:
        lib.create_card(out, tag="@x1", kind=kind, description="d", assets=assets)
    assert excinfo.value.code == "library_assets_invalid"


def test_update_makes_a_new_version_and_keeps_the_old_files(out):
    lib.create_card(out, tag="@alice", kind="person", description="old",
                    assets=[_img(out / "uploads" / "a.png", b"A")])
    v2 = lib.update_card(out, "@alice", description="new",
                         assets=[_img(out / "uploads" / "b.png", b"B")])
    assert (v2["version"], v2["description"]) == (2, "new")
    v1 = lib.get_card(out, "@alice", version=1)
    assert v1 == {"tag": "@alice", "kind": "person", "description": "old", "version": 1,
                  "latest_version": 2,
                  "assets": [str(out / "library" / "alice" / "v1" / "01-a.png")]}
    assert (out / "library" / "alice" / "v1" / "01-a.png").read_bytes() == b"A"


def test_update_without_assets_reuses_the_previous_files(out):
    lib.create_card(out, tag="@alice", kind="person", description="old",
                    assets=[_img(out / "uploads" / "a.png")])
    v2 = lib.update_card(out, "@alice", description="new")
    assert v2["assets"] == [str(out / "library" / "alice" / "v1" / "01-a.png")]


def test_scene_tags_edge_cases():
    assert lib.scene_tags("@alice walks on @beach, then @alice waves. anna@bob.com (@sun-1)") == \
        ["@alice", "@beach", "@sun-1"]
    with pytest.raises(LibraryError) as excinfo:
        lib.scene_tags("@Alice walks")
    assert (excinfo.value.code, excinfo.value.detail) == ("tag_invalid", {"tag": "@Alice"})


def _two_tag_library(out):
    lib.create_card(out, tag="@alice", kind="person",
                    description="the young woman, golden-blonde wavy hair.",
                    assets=[_img(out / "uploads" / "face.png"), _img(out / "uploads" / "back.png")])
    lib.create_card(out, tag="@beach", kind="environment",
                    description="a wide empty beach at golden hour",
                    assets=[_img(out / "uploads" / "pano.png")])
    return [{"tag": "@alice", "version": 1}, {"tag": "@beach", "version": 1}]


def test_build_ref2va_two_tags_one_with_two_pictures(out):
    refs = _two_tag_library(out)
    scene = lib.build_ref2va("@beach at sunset; @alice walks along the water, @alice smiles.",
                             refs, out)
    lib_dir = out / "library"
    assert scene == Ref2VAScene(
        prompt=("subject_definitions:\n"
                "<Subject 1> is a wide empty beach at golden hour, appearance from <Picture 1>.\n"
                "<Subject 2> is the young woman, golden-blonde wavy hair, appearance from "
                "<Picture 2>, <Picture 3>.\n\n"
                "<Subject 1> at sunset; <Subject 2> walks along the water, <Subject 2> smiles."),
        images=(str(lib_dir / "beach" / "v1" / "01-pano.png"),
                str(lib_dir / "alice" / "v1" / "01-face.png"),
                str(lib_dir / "alice" / "v1" / "02-back.png")),
        audios=(), subjects=("@beach", "@alice"))


def test_build_ref2va_voice_card_is_an_audio_subject(out):
    lib.create_card(out, tag="@narrator", kind="voice", description="a calm low male voice",
                    assets=[_img(out / "uploads" / "v.mp3", b"ID3")])
    scene = lib.build_ref2va("@narrator speaks", [{"tag": "@narrator", "version": 1}], out)
    assert scene.prompt == ("subject_definitions:\n"
                            "<Subject 1> is a calm low male voice, voice from <Audio 1>.\n\n"
                            "<Subject 1> speaks")
    assert (scene.images, scene.audios) == (
        (), (str(out / "library" / "narrator" / "v1" / "01-v.mp3"),))


def test_build_ref2va_without_tags_returns_the_prompt_untouched(out):
    assert lib.build_ref2va("just a cat", [], out) == Ref2VAScene("just a cat", (), (), ())


def test_build_ref2va_refuses_a_tag_not_pinned_to_the_project(out):
    refs = _two_tag_library(out)
    with pytest.raises(LibraryError) as excinfo:
        lib.build_ref2va("@alice and @bob", refs, out)
    assert (excinfo.value.code, excinfo.value.detail) == ("unknown_tag", {"unknown": ["@bob"]})


def test_build_ref2va_uses_the_pinned_version_not_the_latest(out):
    lib.create_card(out, tag="@alice", kind="person", description="old",
                    assets=[_img(out / "uploads" / "a.png")])
    lib.update_card(out, "@alice", description="new", assets=[_img(out / "uploads" / "b.png")])
    scene = lib.build_ref2va("@alice", [{"tag": "@alice", "version": 1}], out)
    assert scene.images == (str(out / "library" / "alice" / "v1" / "01-a.png"),)
    assert "is old," in scene.prompt


def test_references_context_lists_tags_for_the_llm(out):
    _two_tag_library(out)
    cards = [lib.get_card(out, "@alice"), lib.get_card(out, "@beach")]
    assert lib.references_context(cards) == (
        "## Reference tags\n"
        "Every scene must name who and where is in frame with these tags, written exactly as "
        "below; no other @tags exist.\n"
        "@alice (person): the young woman, golden-blonde wavy hair.\n"
        "@beach (environment): a wide empty beach at golden hour")


def test_project_pins_references_and_round_trips_them(out):
    proj = p.create_project(out, "video", "T")
    assert proj.references == []
    proj.set_references([{"tag": "@alice", "version": 2}])
    assert p.load_project(proj.path).references == [{"tag": "@alice", "version": 2}]
```

- [ ] **Step 2: Красный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_library.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'h3_48gb.library'`.

- [ ] **Step 3: Реализация `h3_48gb/library.py`**

```python
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
CARD_NAME = "card.json"

ERROR_CODES = {
    "library_tag_invalid": "a reference tag is not @ followed by 2-32 of [a-z0-9-]",
    "library_tag_exists": "a reference card with this tag already exists",
    "library_kind_invalid": "a reference card kind outside person/object/environment/style/voice",
    "library_description_invalid": "a reference card description is empty or too long",
    "library_assets_invalid": "a reference card needs 1-4 png/jpg pictures, or exactly one mp3/wav for a voice",
    "library_card_not_found": "no reference card with this tag",
    "library_version_not_found": "the reference card has no such version",
    "tag_invalid": "an @tag in scene text is not lowercase [a-z0-9-]{2,32}",
    "unknown_tag": "an @tag in scene text is not pinned to the project",
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
def _card_lock(card_dir: Path):
    card_dir.mkdir(parents=True, exist_ok=True)
    with open(card_dir / "card.lock", "a+") as handle:
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
            or len(description) > MAX_DESCRIPTION:
        raise LibraryError("library_description_invalid",
                           f"описание: 1–{MAX_DESCRIPTION} символов по-английски", {})
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
    with _card_lock(card_dir):
        relative = _copy_assets(card_dir, 1, paths)
        card = {"tag": tag, "version": 1, "created": stamp, "updated": stamp,
                "versions": {"1": {"kind": kind, "description": description.strip(),
                                   "assets": relative, "created": stamp}}}
        write_json_durably(card_dir / CARD_NAME, card)
    return _view(card_dir, card, 1)


def update_card(outdir, tag, *, kind=None, description=None, assets=None, now=None) -> dict:
    card_dir = _card_dir(outdir, tag)
    _read(card_dir, tag)  # refuse an unknown tag before _card_lock creates its directory
    with _card_lock(card_dir):
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


def list_cards(outdir) -> list[dict]:
    root = library_root(outdir)
    if not root.is_dir():
        return []
    cards = []
    for entry in sorted(root.iterdir()):
        if (entry / CARD_NAME).is_file():
            try:
                cards.append(get_card(outdir, "@" + entry.name))
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


def build_ref2va(scene_prompt: str, references, outdir) -> Ref2VAScene:
    tags = scene_tags(scene_prompt)
    if not tags:
        return Ref2VAScene(scene_prompt, (), (), ())
    pinned = {ref["tag"]: ref for ref in references}
    unknown = [tag for tag in tags if tag not in pinned]
    if unknown:
        raise LibraryError("unknown_tag", f"теги не подключены к проекту: {', '.join(unknown)}",
                           {"unknown": unknown})
    images: list[str] = []
    audios: list[str] = []
    lines: list[str] = []
    for number, tag in enumerate(tags, start=1):
        card = get_card(outdir, tag, pinned[tag].get("version"))
        description = card["description"].strip().rstrip(".")
        if card["kind"] == "voice":
            audios.append(card["assets"][0])
            lines.append(f"<Subject {number}> is {description}, voice from <Audio {len(audios)}>.")
        else:
            labels = []
            for asset in card["assets"]:
                images.append(asset)
                labels.append(f"<Picture {len(images)}>")
            lines.append(f"<Subject {number}> is {description}, appearance from "
                         f"{', '.join(labels)}.")
    body = _TAG_IN_TEXT_RE.sub(lambda m: f"<Subject {tags.index('@' + m.group(1)) + 1}>",
                               scene_prompt)
    prompt = "subject_definitions:\n" + "\n".join(lines) + "\n\n" + body
    return Ref2VAScene(prompt, tuple(images), tuple(audios), tuple(tags))


def references_context(cards: list[dict]) -> str:
    lines = [f"{card['tag']} ({card['kind']}): {card['description']}" for card in cards]
    return ("## Reference tags\nEvery scene must name who and where is in frame with these tags, "
            "written exactly as below; no other @tags exist.\n" + "\n".join(lines))
```

- [ ] **Step 4: `project.py`**

`_OWNED_TOP_LEVEL_FIELDS = _REQUIRED_FIELDS + ("scenario_scenes", "scenario_style_block", "references")`. В `_apply` после `scenario_style_block`: `self.references = [dict(ref) for ref in data.get("references") or []]`. В `as_dict`: `"references": [dict(ref) for ref in self.references],`. В `create_project` в `data`: `"references": [],`. Метод:
```python
    def set_references(self, references: list[dict]) -> "Project":
        """Replace the project's pinned reference cards (spec §3.5: `[{tag, version}]`). Storage
        only -- `web` checks that each card and version exists before calling this. Same lock ->
        re-read -> merge -> write -> `_apply` shape as every other locked mutator here."""
        with _project_lock(self.path.parent, exclusive=True):
            data = _read_data(self.path)
            data["references"] = [{"tag": ref["tag"], "version": int(ref["version"])}
                                  for ref in references]
            write_json_durably(self.path, data)
            self._apply(data)
        return self
```
Если существующий тест `tests/test_project.py` пиннит точный набор ключей `as_dict()`/`project.json` — дописать в ожидание `"references": []` (это законное расширение модели, упомянуть в отчёте).

- [ ] **Step 5: Зелёный по модулю**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_library.py tests/test_project.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 6: Падающие тесты API**

`tests/test_library_web.py`:
```python
"""Library and project-references JSON API (spec §3.5, §10: wave 1 is data + API)."""
import pytest

from h3_48gb import project as p
from h3_48gb import queue as q
from test_web import _call, _serve


@pytest.fixture
def live(tmp_path):
    outdir = tmp_path / "outdir"
    (outdir / "uploads").mkdir(parents=True)
    (outdir / "uploads" / "face.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    server = _serve(q.layout(outdir / "queue")["root"], outdir)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def _create(live, tag="@alice"):
    return _call(live, "POST", "/api/library", {
        "tag": tag, "kind": "person", "description": "a young woman",
        "assets": [str(live.outdir / "uploads" / "face.png")]})


def test_create_and_list(live):
    status, body = _create(live)
    expected_card = {"tag": "@alice", "kind": "person", "description": "a young woman",
                     "version": 1, "latest_version": 1,
                     "assets": [str(live.outdir / "library" / "alice" / "v1" / "01-face.png")]}
    assert (status, body) == (200, {"ok": True, "card": expected_card})
    assert _call(live, "GET", "/api/library") == (200, {"ok": True, "cards": [expected_card]})


def test_assets_outside_the_outdir_are_refused(live):
    status, body = _call(live, "POST", "/api/library", {
        "tag": "@alice", "kind": "person", "description": "d", "assets": ["/etc/hosts"]})
    assert (status, body["error"]["code"]) == (400, "path_outside_root")


def test_duplicate_tag_is_409(live):
    _create(live)
    status, body = _create(live)
    assert (status, body["error"]["code"]) == (409, "library_tag_exists")


def test_put_makes_version_two(live):
    _create(live)
    status, body = _call(live, "PUT", "/api/library/alice", {"description": "older woman"})
    assert status == 200
    assert (body["card"]["version"], body["card"]["description"]) == (2, "older woman")


def test_unknown_card_is_404(live):
    status, body = _call(live, "PUT", "/api/library/nobody", {"description": "x"})
    assert (status, body["error"]["code"]) == (404, "library_card_not_found")
    assert not (live.outdir / "library" / "nobody").exists()


def test_project_references_pin_latest_when_version_omitted(live):
    _create(live)
    _call(live, "PUT", "/api/library/alice", {"description": "v2"})
    proj = p.create_project(live.outdir, "video", "T")
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/references",
                         {"references": [{"tag": "@alice"}]})
    assert status == 200, body
    assert body["references"] == [{
        "tag": "@alice", "kind": "person", "description": "v2", "version": 2,
        "latest_version": 2,
        "assets": [str(live.outdir / "library" / "alice" / "v1" / "01-face.png")]}]
    assert p.load_project(proj.path).references == [{"tag": "@alice", "version": 2}]


def test_project_references_refuse_a_missing_version(live):
    _create(live)
    proj = p.create_project(live.outdir, "video", "T")
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/references",
                         {"references": [{"tag": "@alice", "version": 9}]})
    assert (status, body["error"]["code"]) == (400, "library_version_not_found")
    assert p.load_project(proj.path).references == []


def test_get_project_references(live):
    _create(live)
    proj = p.create_project(live.outdir, "video", "T")
    proj.set_references([{"tag": "@alice", "version": 1}])
    status, body = _call(live, "GET", f"/api/projects/{proj.id}/references")
    assert status == 200
    assert [ref["tag"] for ref in body["references"]] == ["@alice"]
```

- [ ] **Step 7: Красный по API**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_library_web.py -q -p no:cacheprovider`
Expected: FAIL — 404 `not_found` (`no route for POST /api/library`).

- [ ] **Step 8: Реализация API в web.py**

`from h3_48gb import library as library_module`. В `cli.py` после обновления кодами sglang:
```python
from h3_48gb.library import ERROR_CODES as _LIBRARY_ERROR_CODES  # noqa: E402

ERROR_CODES.update(_LIBRARY_ERROR_CODES)
```
`ERROR_STATUS`: `"library_card_not_found": 404`, `"library_tag_exists": 409`.

Маршруты. `_route_get` — до `if path.startswith("/api/projects/")`:
```python
        if path == "/api/library":
            return self._list_library()
        if path.startswith("/api/projects/") and path.endswith("/references"):
            return self._project_references(path[len("/api/projects/"):-len("/references")])
```
`_route_post` — до `if path == "/api/projects"`: `if path == "/api/library": return self._create_card()`.
`_route_put` — первыми:
```python
        if path.startswith("/api/library/"):
            return self._update_card(path[len("/api/library/"):])
        if path.startswith("/api/projects/") and path.endswith("/references"):
            return self._put_project_references(path[len("/api/projects/"):-len("/references")])
```
Методы `_Handler`:
```python
    def _library_call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except library_module.LibraryError as exc:
            raise CliError(exc.code, exc.message, exc.detail) from exc

    def _list_library(self) -> tuple[int, str, bytes]:
        return 200, "application/json", _json_bytes(
            {"ok": True, "cards": library_module.list_cards(self.server.outdir)})

    def _library_assets(self, payload) -> list[Path] | None:
        raw = payload.get("assets")
        if raw is None:
            return None
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise CliError("args_invalid", "`assets` must be a list of paths", {})
        return [resolve_within(item, {"outdir": Path(self.server.outdir)}, write=False)
                for item in raw]

    def _create_card(self) -> tuple[int, str, bytes]:
        payload = self._json_request(allowed=("tag", "kind", "description", "assets"))
        card = self._library_call(
            library_module.create_card, self.server.outdir, tag=payload.get("tag"),
            kind=payload.get("kind"), description=payload.get("description"),
            assets=self._library_assets(payload) or [])
        return 200, "application/json", _json_bytes({"ok": True, "card": card})

    def _update_card(self, name: str) -> tuple[int, str, bytes]:
        payload = self._json_request(allowed=("kind", "description", "assets"))
        card = self._library_call(
            library_module.update_card, self.server.outdir, "@" + name,
            kind=payload.get("kind"), description=payload.get("description"),
            assets=self._library_assets(payload))
        return 200, "application/json", _json_bytes({"ok": True, "card": card})

    def _resolved_references(self, proj) -> list[dict]:
        return [self._library_call(library_module.get_card, self.server.outdir, ref["tag"],
                                   ref["version"]) for ref in proj.references]

    def _project_references(self, raw_id: str) -> tuple[int, str, bytes]:
        proj = self._load_project(raw_id)
        return 200, "application/json", _json_bytes(
            {"ok": True, "references": self._resolved_references(proj)})

    def _put_project_references(self, raw_id: str) -> tuple[int, str, bytes]:
        proj = self._load_project(raw_id)
        payload = self._json_request(allowed=("references",))
        raw = payload.get("references")
        if not isinstance(raw, list) or not all(isinstance(r, dict) and "tag" in r for r in raw):
            raise CliError("args_invalid", "`references` must be a list of {tag, version?}", {})
        pinned = []
        for ref in raw:
            card = self._library_call(library_module.get_card, self.server.outdir, ref["tag"],
                                      ref.get("version"))
            pinned.append({"tag": card["tag"], "version": card["version"]})
        proj.set_references(pinned)
        return self._project_references(raw_id)
```
(`resolve_within` бросает `CliError("path_outside_root", ...)` сама.)

- [ ] **Step 9: Зелёный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_library.py tests/test_library_web.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 10: Мутации**

(а) В `build_ref2va` поменять порядок обхода: `for number, tag in enumerate(reversed(tags), start=1)` → `test_build_ref2va_two_tags_one_with_two_pictures` FAIL (порядок `<Picture N>` и images). (б) В `_view` первую строку `entry = card["versions"].get(str(version))` заменить на `entry = card["versions"].get(str(card["version"]))` (всегда последняя версия, номер в ответе — запрошенный) → `test_build_ref2va_uses_the_pinned_version_not_the_latest` FAIL на `assert scene.images == (str(out / "library" / "alice" / "v1" / "01-a.png"),)` — придёт путь `v2/01-b.png`. (в) В `_TAG_IN_TEXT_RE` убрать lookbehind `(?<![\w@.])` → `test_scene_tags_edge_cases` FAIL (в списке появится `@bob` из адреса `anna@bob.com`). (г) В `_put_project_references` писать `ref.get("version")` вместо `card["version"]` → `test_project_references_pin_latest_when_version_omitted` FAIL. Ошибки — в отчёт.

- [ ] **Step 11: Полный прогон и commit**

```bash
env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest -q -p no:cacheprovider
git add h3_48gb/library.py h3_48gb/project.py h3_48gb/web.py h3_48gb/cli.py tests/test_library.py tests/test_library_web.py tests/test_project.py
git commit -m "feat(library): библиотека референсов с версиями, API и сборка Ref2VA по тегам"
```

---

## Task 5: Сцены при sglang: argv из `assemble`, цепочка через кейфрейм, снап, проверка тегов на гейтах, теги в чате

**Files:**
- Modify: `h3_48gb/assemble.py` — константы рядом с `OVERLAP_PIXEL_FRAMES` (`assemble.py:175`), новые `_extract_last_frame`, `_scene_generate_args_sglang`, `_submit_next_scene_sglang`; первая строка `_submit_next_scene` (`assemble.py:1283`)
- Modify: `h3_48gb/web.py` — `_grid_frames_nearest`, `_snap_video_scenes_sglang`, `_scene_reference_errors` рядом с `_snap_scene_duration` (`web.py:947`); `_approve_project_stage` (`web.py:4264-4304`); `_chat_message` (`web.py:5314`, `allowed=` на `5359-5360`) / `_locked_turn` (`web.py:5364-5447`); новый маршрут `PUT /api/projects/<id>/settings`
- Modify: `h3_48gb/project.py` — `DEFAULT_I2V_PREFIX`, атрибут `Project.i2v_prefix`, `Project.update_settings`, `create_project`, `_OWNED_TOP_LEVEL_FIELDS`
- Modify: `h3_48gb/cli.py` (`ERROR_CODES` += `scene_references_invalid`)
- Test: `tests/test_sglang_scenes.py` (новый), `tests/test_sglang_gates.py` (новый), `tests/test_sglang_chat_tags.py` (новый)

**Interfaces:**
- Consumes: `sglang_args.parse/DEFAULT_STEPS` (задача 3), `sglang_estimate.estimate_seconds` (задача 3), `library.build_ref2va/scene_tags/get_card/references_context`, `Project.references` (задача 4), `engine.is_sglang()` (задача 2).
- Produces:
  - `assemble.SGLANG_OVERLAP_FRAMES = 1`, `assemble.SGLANG_DEFAULT_SEED = 42`
  - `assemble._extract_last_frame(clip_path, dest_dir: Path, source_idx: int, *, run) -> Path`
  - `assemble._scene_generate_args_sglang(scene: dict, *, keyframe: Path | None, chained: bool, ref2va: library.Ref2VAScene, track_piece: Path | None, scenes_dir: Path, i2v_prefix: str = "") -> tuple[list[str], str]`
  - `assemble._submit_next_scene_sglang(proj, scene, queue_root, *, submit, run) -> dict` (тот же словарь, что у `_submit_next_scene`: `action/idx/job_id/keyframe/latent/head_drop_frames`; `latent` всегда `None`)
  - `web._SGLANG_OVERLAP_FRAMES = 1`, `web._grid_frames_nearest(frames, *, remainder) -> int`, `web._snap_video_scenes_sglang(scenes) -> list[dict]`, `web._scene_reference_errors(proj, scenes, outdir) -> list[dict]`
  - `project.DEFAULT_I2V_PREFIX = "The video begins exactly on the provided first frame and continues it seamlessly: same characters, setting, lighting and camera style."` — без метки `<Picture N>`: кейфрейм на sglang не нумеруется; поле проекта `i2v_prefix` (старые проекты без поля получают значение по умолчанию при чтении), `Project.update_settings(*, i2v_prefix: str) -> Project`; `PUT /api/projects/<id>/settings {"i2v_prefix": str}` → `{"ok": true, "project": <payload>}`
  - запрос `POST /api/chat/<id>/message` принимает `"tags": ["@alice", ...]`; блок `library.references_context` дописывается в конец system и сохраняется в сессии.
- Решение: у **каждой** сцены, кроме сцен клипа (у них аудио-референс — кусок трека), должен быть хотя бы один `@`-тег: `task` всегда `ref2va` (спека §4.1.3, проба 07.10). Латентного хвоста нет, кейфрейм — основной путь; лимит `MAX_CONSECUTIVE_KEYFRAME_FALLBACKS` к sglang-пути не относится (отдельная функция его не вызывает); кейфрейм — **буквальный последний кадр** (`-sseof -1`, как `chain_beach.py`), а не `dur - 1.5 с`, потому что sglang повторяет его кадром 0 и сборка срезает ровно 1 кадр.

- [ ] **Step 1: Падающие тесты сцен**

`tests/test_sglang_scenes.py`:
```python
"""Scene submission on sglang (spec §3.3.2, §3.3.5, §4.1.3): the argv `assemble` really builds,
accepted and refused by the adapter's own parser (spec §6), and the keyframe chain."""
import subprocess
from pathlib import Path

import pytest

from h3_48gb import assemble
from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import queue as q
from h3_48gb import web
from h3_48gb.engines import sglang_args as sa


class _Submitted:
    def __init__(self, job_id):
        self.id = job_id


def _png(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return path


@pytest.fixture
def chain(tmp_path, monkeypatch):
    """A 5-scene video project, scenes 0-3 done, scene 4 pending, two pinned cards."""
    monkeypatch.setenv("H3_ENGINE", "sglang")
    monkeypatch.setattr(assemble.secrets, "token_hex", lambda n: "ab12")
    out = tmp_path / "outdir"
    lib.create_card(out, tag="@alice", kind="person", description="a young woman",
                    assets=[_png(out / "uploads" / "face.png")])
    lib.create_card(out, tag="@beach", kind="environment", description="a wide beach",
                    assets=[_png(out / "uploads" / "pano.png")])
    proj = p.create_project(out, "video", "Chain")
    pdir = proj.path.parent
    scenes = []
    for idx in range(5):
        clip = None
        if idx < 4:
            clip = pdir / "scenes" / f"s{idx}.mp4"
            clip.parent.mkdir(parents=True, exist_ok=True)
            clip.write_bytes(b"mp4")
        scenes.append({"idx": idx, "prompt": "@alice walks on @beach",
                       "duration": (175 if idx == 0 else 174) / 24,
                       "status": "done" if idx < 4 else "pending",
                       "job_id": f"j{idx}" if idx < 4 else None,
                       "clip_path": str(clip) if clip else None, "keyframe_path": None})
    proj.scenes = scenes
    proj.stages["scenes"] = "running"
    proj.save()
    proj.set_references([{"tag": "@alice", "version": 1}, {"tag": "@beach", "version": 1}])
    return out, proj


def _fake_run(commands):
    def run(cmd, capture_output=True, text=True):
        commands.append(list(cmd))
        if "-sseof" in cmd:
            Path(cmd[-1]).write_bytes(b"\x89PNG\r\n\x1a\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return run


def test_a_fifth_chained_scene_is_submitted_with_keyframe_refs_and_one_frame_overlap(chain):
    out, proj = chain
    pdir = proj.path.parent
    submitted, commands = [], []

    def submit(root, args, note, report, estimate, kind):
        submitted.append({"args": args, "note": note, "report": report, "estimate": estimate,
                          "kind": kind})
        return _Submitted("j4")

    result = assemble.advance_project(proj, out / "queue", out, submit=submit,
                                      run=_fake_run(commands))
    keyframe = pdir / "keyframes" / "keyframe-003.png"
    assert result == {"action": "submitted_scene", "idx": 4, "job_id": "j4",
                      "keyframe": str(keyframe), "latent": None, "head_drop_frames": 1}
    alice = str(out / "library" / "alice" / "v1" / "01-face.png")
    beach = str(out / "library" / "beach" / "v1" / "01-pano.png")
    prompt = ("The video begins exactly on the provided first frame and continues it seamlessly: same characters, setting, lighting and camera style.\n\n"
              "subject_definitions:\n"
              "<Subject 1> is a young woman, appearance from <Picture 1>.\n"
              "<Subject 2> is a wide beach, appearance from <Picture 2>.\n\n"
              "<Subject 1> walks on <Subject 2>")
    assert submitted == [{
        "args": ["generate", prompt, "--width", "896", "--height", "512",
                 "--duration", str(175 / 24), "--steps", "50", "--seed", "42",
                 "--tag", "scene-4-ab12", "--outdir", str(pdir / "scenes"), "--task", "ref2va",
                 "--image", str(keyframe), "--aspect", "auto", "--ref", alice, "--ref", beach],
        "note": assemble.scene_note(proj, 4),
        "report": {"output_stem": str(pdir / "scenes" / "h3-scene-4-ab12-896x512")},
        "estimate": {"seconds": 2810.0, "source": "table", "samples": 0},
        "kind": q.KIND_GENERATE}]
    assert commands == [["ffmpeg", "-y", "-loglevel", "error", "-sseof", "-1", "-i",
                         str(pdir / "scenes" / "s3.mp4"), "-update", "1", "-q:v", "1",
                         str(keyframe)]]
    scene4 = p.load_project(proj.path).scenes[4]
    assert (scene4["status"], scene4["job_id"], scene4["head_drop_frames"],
            scene4["keyframe_path"]) == ("running", "j4", 1, str(keyframe))
    # spec §6: the adapter's own parser accepts exactly this argv
    spec = sa.parse(submitted[0]["args"])
    assert (spec.task, spec.image, spec.refs, spec.aspect_ratio, spec.frames) == \
        ("ref2va", str(keyframe), (alice, beach), "auto", 175)


def test_a_first_scene_with_a_tag_is_ref2va_with_references_and_no_keyframe(tmp_path, monkeypatch):
    monkeypatch.setattr(assemble.secrets, "token_hex", lambda n: "cd34")
    ref = str(_png(tmp_path / "r.png"))
    args, stem = assemble._scene_generate_args_sglang(
        {"idx": 0, "prompt": "@cat", "duration": 175 / 24}, keyframe=None, chained=False,
        ref2va=lib.Ref2VAScene("body", (ref,), (), ("@cat",)), track_piece=None,
        scenes_dir=tmp_path, i2v_prefix="never on a first scene")
    assert args == ["generate", "body", "--width", "896", "--height", "512",
                    "--duration", str(175 / 24), "--steps", "50", "--seed", "42",
                    "--tag", "scene-0-cd34", "--outdir", str(tmp_path), "--task", "ref2va",
                    "--ref", ref]
    assert stem == str(tmp_path / "h3-scene-0-cd34-896x512")
    assert sa.parse(args).task == "ref2va"


def test_the_parser_refuses_a_real_first_scene_argv_without_references(tmp_path, monkeypatch):
    monkeypatch.setattr(assemble.secrets, "token_hex", lambda n: "0001")
    args, _ = assemble._scene_generate_args_sglang(
        {"idx": 0, "prompt": "a cat", "duration": 175 / 24}, keyframe=None, chained=False,
        ref2va=lib.Ref2VAScene("a cat", (), (), ()), track_piece=None, scenes_dir=tmp_path)
    with pytest.raises(sa.SglangArgsError) as excinfo:
        sa.parse(args)
    assert (excinfo.value.code, excinfo.value.message) == \
        ("ref2va_needs_reference", "нужен хотя бы один референс (@тег) в сцене")


def test_i2v_prefix_is_prepended_only_to_a_chained_scene(tmp_path, monkeypatch):
    monkeypatch.setattr(assemble.secrets, "token_hex", lambda n: "ef56")
    ref = lib.Ref2VAScene("body", (str(_png(tmp_path / "r.png")),), (), ("@a",))
    chained, _ = assemble._scene_generate_args_sglang(
        {"idx": 1, "prompt": "@a", "duration": 174 / 24}, keyframe=_png(tmp_path / "k.png"),
        chained=True, ref2va=ref, track_piece=None, scenes_dir=tmp_path, i2v_prefix="Continue.")
    first, _ = assemble._scene_generate_args_sglang(
        {"idx": 0, "prompt": "@a", "duration": 175 / 24}, keyframe=None, chained=False,
        ref2va=ref, track_piece=None, scenes_dir=tmp_path, i2v_prefix="Continue.")
    assert (chained[1], first[1]) == ("Continue.\n\nbody", "body")


def test_an_unsnapped_duration_is_refused_before_submission(tmp_path):
    with pytest.raises(assemble.AssembleError):
        assemble._scene_generate_args_sglang(
            {"idx": 1, "prompt": "x", "duration": 7.0}, keyframe=_png(tmp_path / "k.png"),
            chained=True, ref2va=lib.Ref2VAScene("x", (str(_png(tmp_path / "r.png")),), (), ()),
            track_piece=None, scenes_dir=tmp_path)


def test_the_parser_refuses_a_real_chained_argv_without_references(tmp_path, monkeypatch):
    monkeypatch.setattr(assemble.secrets, "token_hex", lambda n: "0000")
    args, _ = assemble._scene_generate_args_sglang(
        {"idx": 1, "prompt": "x", "duration": 174 / 24}, keyframe=_png(tmp_path / "k.png"),
        chained=True, ref2va=lib.Ref2VAScene("x", (), (), ()), track_piece=None,
        scenes_dir=tmp_path)
    with pytest.raises(sa.SglangArgsError) as excinfo:
        sa.parse(args)
    assert (excinfo.value.code, excinfo.value.message) == \
        ("ref2va_needs_reference", "нужен хотя бы один референс (@тег) в сцене")


def test_the_parser_refuses_the_real_mlx_argv(tmp_path):
    args, _ = assemble._scene_generate_args({"idx": 0, "prompt": "x", "duration": 5.0}, None,
                                            tmp_path)
    with pytest.raises(sa.SglangArgsError) as excinfo:
        sa.parse(args)
    assert excinfo.value.message == "unsupported_on_sglang: --turbo-strength"


def test_a_corrupt_last_frame_stops_the_chain(chain, monkeypatch):
    out, proj = chain
    monkeypatch.setattr(assemble, "_frame_is_corrupt", lambda *a, **k: True)
    with pytest.raises(assemble.AssembleError):
        assemble.advance_project(proj, out / "queue", out,
                                 submit=lambda *a, **k: _Submitted("x"), run=_fake_run([]))
    assert p.load_project(proj.path).scenes[4]["status"] == "pending"


def test_overlap_constant_is_the_same_in_web_and_assemble():
    assert web._SGLANG_OVERLAP_FRAMES == assemble.SGLANG_OVERLAP_FRAMES == 1
```

- [ ] **Step 2: Красный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_sglang_scenes.py -q -p no:cacheprovider`
Expected: FAIL — `AttributeError: module 'h3_48gb.assemble' has no attribute '_scene_generate_args_sglang'`.

- [ ] **Step 3: Реализация в assemble.py**

Импорты: `from h3_48gb import engine`, `from h3_48gb import library`, `from h3_48gb.engines import estimate as sglang_estimate`, `from h3_48gb.engines import sglang_args`. Константы под `OVERLAP_PIXEL_FRAMES`:
```python
#: How many frames of the previous scene a chained scene repeats at its head on sglang (spec
#: §3.3.5): the keyframe condition at frame_index 0 *is* the previous scene's last frame, so the
#: assembly drops exactly that one frame (`head_drop_frames = 1`). Duplicated in web.py as
#: `_SGLANG_OVERLAP_FRAMES`, pinned equal by test_sglang_scenes.py.
SGLANG_OVERLAP_FRAMES = 1
#: chain_beach.py's seed for every one of its 8/8 chained scenes.
SGLANG_DEFAULT_SEED = 42
```
Функции (рядом с `_extract_keyframe`):
```python
def _extract_last_frame(clip_path, dest_dir: Path, source_idx: int, *, run) -> Path:
    """The literal last frame of the previous scene -- the sglang chain's keyframe
    (`chain_beach.py`: `ffmpeg -sseof -1 ... -update 1`). No lookback on corruption: the next
    scene repeats this exact frame at its head, so any other frame would put a jump in the cut.
    A corrupt one fails the submission instead (`_submit_next_scene_sglang` rolls the claim back).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_path = dest_dir / f"keyframe-{source_idx:03d}.png"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-sseof", "-1", "-i", str(clip_path),
           "-update", "1", "-q:v", "1", str(out_path)]
    _run_ffmpeg(cmd, run, "ffmpeg last-frame extraction")
    if _frame_is_corrupt(out_path, run=run, workdir=dest_dir):
        raise AssembleError(f"the last frame of {clip_path} reads as corrupt (zero-fill/tile-seam)"
                            " -- refusing to chain the next scene off it")
    return out_path


def _scene_generate_args_sglang(scene: dict, *, keyframe, chained: bool, ref2va,
                                track_piece, scenes_dir: Path,
                                i2v_prefix: str = "") -> tuple[list[str], str]:
    """spec §3.3.2: the sglang argv -- prompt (already Ref2VA-assembled from the scene's @tags),
    canvas, duration, steps, seed, tag, outdir, task, keyframe, reference pictures, audio
    references (voice cards, then the clip's own track piece). A chained scene requests one frame
    more than it delivers (the repeated keyframe) and asks for `aspect_ratio: auto`."""
    idx = scene["idx"]
    width, height = DEFAULT_SCENE_CANVAS
    tag = f"scene-{idx}-{secrets.token_hex(2)}"
    delivered = round(scene["duration"] * ASSEMBLY_FPS)
    requested = delivered + (SGLANG_OVERLAP_FRAMES if chained else 0)
    if (requested - _H3_LATENTS_PER_CHUNK) % _H3_FRAMES_PER_CHUNK:
        raise AssembleError(
            f"scene {idx}: {requested} frames is off sglang's 17n+5 grid (delivered {delivered}, "
            f"chained={chained}) -- the scene's duration was never snapped")
    prompt = ref2va.prompt
    if chained and i2v_prefix:
        prompt = f"{i2v_prefix}\n\n{prompt}"
    audios = list(ref2va.audios) + ([str(track_piece)] if track_piece is not None else [])
    task = "ref2va"   # the only task the ref2va server serves (spec §4.1.3)
    args = ["generate", prompt, "--width", str(width), "--height", str(height),
            "--duration", str(requested / ASSEMBLY_FPS),
            "--steps", str(sglang_args.DEFAULT_STEPS),
            "--seed", str(scene.get("seed", SGLANG_DEFAULT_SEED)),
            "--tag", tag, "--outdir", str(scenes_dir), "--task", task]
    if keyframe is not None:
        args += ["--image", str(keyframe)]
    if chained:
        args += ["--aspect", "auto"]
    for image in ref2va.images:
        args += ["--ref", image]
    for audio in audios:
        args += ["--audio", audio]
    return args, str(Path(scenes_dir) / f"h3-{tag}-{width}x{height}")


def _submit_next_scene_sglang(proj, scene: dict, queue_root, *, submit, run) -> dict:
    """`_submit_next_scene`'s sglang twin: same claim-before-submit and rollback discipline (see
    that function's docstring), but the chain is keyframe-first -- there is no latent tail on
    sglang, so there is no fallback and no `MAX_CONSECUTIVE_KEYFRAME_FALLBACKS` limit."""
    idx = scene["idx"]
    claimed = proj.claim_next_scene(_SCENE_CLAIM_PLACEHOLDER_JOB_ID, expected_idx=idx)
    if claimed is None:
        return {"action": "nothing_to_do"}
    if proj.stages.get("scenes") in ("draft", "approved"):
        proj.set_stage_status("scenes", "running")
    outdir = proj.path.parent.parent.parent
    chained = idx > 0 and not scene.get("fresh_start")
    try:
        keyframe = None
        if chained:
            prev = _scene_by_idx(proj.scenes, idx - 1)
            prev_clip = prev.get("clip_path") if prev else None
            if not prev_clip:
                raise AssembleError(f"scene {idx}: the previous scene has no clip to chain from")
            keyframe = _extract_last_frame(prev_clip, proj.path.parent / "keyframes", idx - 1,
                                           run=run)
        elif idx == 0 and proj.as_dict().get("start_image"):
            keyframe = Path(proj.as_dict()["start_image"])
        ref2va = library.build_ref2va(scene["prompt"], proj.references, outdir)
        track_piece = None
        scenes_dir = proj.path.parent / "scenes"
        args, output_stem = _scene_generate_args_sglang(
            scene, keyframe=keyframe, chained=chained, ref2va=ref2va, track_piece=track_piece,
            scenes_dir=scenes_dir, i2v_prefix=proj.i2v_prefix)
        width, height = DEFAULT_SCENE_CANVAS
        frames = round(scene["duration"] * ASSEMBLY_FPS) + (SGLANG_OVERLAP_FRAMES if chained else 0)
        estimate = sglang_estimate.estimate_seconds(outdir, width=width, height=height,
                                                    frames=frames)
        job = submit(queue_root, args, scene_note(proj, idx), {"output_stem": output_stem},
                     estimate, kind=q.KIND_GENERATE)
    except Exception:
        proj.set_scene_status(idx, "pending", job_id=None)
        raise
    head_drop_frames = SGLANG_OVERLAP_FRAMES if chained else 0
    proj.set_scene_status(idx, "running", job_id=job.id,
                          keyframe_path=str(keyframe) if keyframe is not None else None,
                          head_drop_frames=head_drop_frames)
    return {"action": "submitted_scene", "idx": idx, "job_id": job.id,
            "keyframe": str(keyframe) if keyframe is not None else None,
            "latent": None, "head_drop_frames": head_drop_frames}
```
Первая строка тела `_submit_next_scene` (после докстринга):
```python
    if engine.is_sglang():
        return _submit_next_scene_sglang(proj, scene, queue_root, submit=submit, run=run)
```
(Проверить: `library` не импортирует `assemble`, циклов нет.)

- [ ] **Step 4: Зелёный по сценам**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_sglang_scenes.py tests/test_assemble.py tests/test_latent_chain.py -q -p no:cacheprovider`
Expected: PASS (кроме `test_overlap_constant_is_the_same_in_web_and_assemble` — он зеленеет после шага 7).

- [ ] **Step 5: Падающие тесты гейтов и чата**

`tests/test_sglang_gates.py`:
```python
"""Gates on sglang: durations snapped to the delivered grid, @tags checked before a single scene
is queued (spec §3.3.5, §3.5, §4.1.5), and the chat seeing the library tags."""
import pytest

from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import queue as q
from h3_48gb.engines import sglang_args as sa
from test_web import _call, _pending, _serve


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    outdir = tmp_path / "outdir"
    (outdir / "uploads").mkdir(parents=True)
    (outdir / "uploads" / "face.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    lib.create_card(outdir, tag="@alice", kind="person", description="a young woman",
                    assets=[outdir / "uploads" / "face.png"])
    server = _serve(q.layout(outdir / "queue")["root"], outdir)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def _video(live, prompts, durations, fresh=()):
    proj = p.create_project(live.outdir, "video", "Gate")
    proj.scenes = [{"idx": i, "prompt": text, "duration": d, "status": "pending", "job_id": None,
                    "clip_path": None, "keyframe_path": None, "fresh_start": i in fresh}
                   for i, (text, d) in enumerate(zip(prompts, durations))]
    proj.stages["script"] = "awaiting_approval"
    proj.save()
    proj.set_references([{"tag": "@alice", "version": 1}])
    return proj


def test_approving_the_script_snaps_durations_to_the_delivered_grid(live):
    proj = _video(live, ["@alice sits", "@alice waves", "@alice runs"], [7.0, 7.0, 10.0], fresh=(2,))
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert status == 200, body
    assert [s["duration"] for s in p.load_project(proj.path).scenes] == \
        [175 / 24, 174 / 24, 243 / 24]
    (job,) = _pending(live)
    assert sa.parse(job.args, check_files=False).frames == 175


def test_every_scene_without_a_tag_is_refused_and_nothing_is_queued(live):
    """spec §4.1.3: every scene needs a reference -- the first and a fresh_start one too, not
    only a chained one."""
    proj = _video(live, ["a cat", "@alice and a dog", "a bird"], [7.0, 7.0, 7.0], fresh=(2,))
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert status == 400
    assert body["error"]["code"] == "scene_references_invalid"
    assert body["error"]["detail"] == {"scenes": [
        {"idx": 0, "code": "ref2va_needs_reference",
         "message": "нужен хотя бы один референс (@тег) в сцене"},
        {"idx": 2, "code": "ref2va_needs_reference",
         "message": "нужен хотя бы один референс (@тег) в сцене"}]}
    reloaded = p.load_project(proj.path)
    assert reloaded.stages["script"] == "awaiting_approval"
    assert [s["duration"] for s in reloaded.scenes] == [7.0, 7.0, 7.0]
    assert _pending(live) == []


def test_settings_route_changes_the_i2v_prefix(live):
    proj = _video(live, ["@alice"], [7.0])
    assert p.load_project(proj.path).i2v_prefix == p.DEFAULT_I2V_PREFIX
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/settings",
                         {"i2v_prefix": "Continue the shot."})
    assert status == 200, body
    assert (body["project"]["i2v_prefix"], p.load_project(proj.path).i2v_prefix) == \
        ("Continue the shot.", "Continue the shot.")


def test_an_unknown_and_a_malformed_tag_are_named_per_scene(live):
    proj = _video(live, ["@bob in a room", "@Alice waves"], [7.0, 7.0])
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert body["error"]["detail"] == {"scenes": [
        {"idx": 0, "code": "unknown_tag", "message": "теги не подключены к проекту: @bob"},
        {"idx": 1, "code": "tag_invalid",
         "message": "тег @Alice: пишется строчными, 2–32 символа из a-z, 0-9, -"}]}


def test_too_many_pictures_is_refused(live, monkeypatch):
    monkeypatch.setenv("H3_MAX_REF_IMAGES", "0")
    proj = _video(live, ["@alice"], [7.0])
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert body["error"]["detail"]["scenes"][0]["code"] == "sglang_args_invalid"


def test_on_mlx_nothing_is_snapped_or_checked(live, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "mlx")
    proj = _video(live, ["a cat", "a dog"], [7.0, 7.0])   # no tags: fine on mlx
    monkeypatch.setattr("h3_48gb.assemble.advance_project", lambda *a, **k: {"action": "stub"})
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert status == 200, body
    assert [s["duration"] for s in p.load_project(proj.path).scenes] == [7.0, 7.0]


# ---- tests/test_sglang_chat_tags.py (a separate file: test_chat_web's `_serve` is a fixture, and
# test_web's `_serve` above is a plain function of the same name -- one module cannot import both)
from h3_48gb import library as lib
from test_chat_web import _serve, fake_llama  # noqa: F401  (fixtures)


def test_chat_tags_ride_the_system_message(_serve, fake_llama):  # noqa: F811
    srv = _serve(providers_port=fake_llama.port)
    (srv.root / "uploads").mkdir(exist_ok=True)
    (srv.root / "uploads" / "f.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    card = lib.create_card(srv.root, tag="@alice", kind="person", description="a young woman",
                           assets=[srv.root / "uploads" / "f.png"])
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "сцена", "prompt": "", "tags": ["@alice"]})
    system = fake_llama.requests[-1]["body"]["messages"][0]["content"]
    assert system.endswith("\n\n" + lib.references_context([card]))
    # the tags stay with the session: a later turn without `tags` still carries them
    srv.post_json(f"/api/chat/{sid}/message", {"text": "ещё", "prompt": ""})
    assert fake_llama.requests[-1]["body"]["messages"][0]["content"].endswith(
        "\n\n" + lib.references_context([card]))
```
Примечание к `test_too_many_pictures_is_refused`: `H3_MAX_REF_IMAGES=0` отвергается `max_ref_images` как `sglang_args_invalid` — тест закрепляет, что ошибка настройки видна по сцене, а не валит сервер 500. Чтобы проверить сам предел, второй тест (в `tests/test_sglang_gates.py`, не в файле чата):
```python
def test_the_picture_limit_counts_card_assets(live, monkeypatch):
    monkeypatch.setenv("H3_MAX_REF_IMAGES", "1")
    (live.outdir / "uploads" / "back.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    lib.update_card(live.outdir, "@alice", assets=[live.outdir / "uploads" / "face.png",
                                                    live.outdir / "uploads" / "back.png"])
    proj = _video(live, ["@alice"], [7.0])
    proj.set_references([{"tag": "@alice", "version": 2}])
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert body["error"]["detail"] == {"scenes": [
        {"idx": 0, "code": "too_many_reference_images",
         "message": "картинок-референсов 2, а можно не больше 1"}]}
```

- [ ] **Step 6: Красный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_sglang_gates.py tests/test_sglang_chat_tags.py -q -p no:cacheprovider`
Expected: FAIL — durations не снапнуты (`[7.0, 7.0, 10.0] != [...]`), коды ошибок отсутствуют.

- [ ] **Step 7: Реализация в web.py**

Рядом с `_snap_scene_duration`:
```python
#: spec §3.3.5: a chained scene on sglang repeats one frame (the keyframe). Duplicated from
#: `assemble.SGLANG_OVERLAP_FRAMES` for the same reason `_SCENE_LATENT_OVERLAP_FRAMES` is
#: (this module must not import the worker-side graph for one integer); pinned equal by a test.
_SGLANG_OVERLAP_FRAMES = 1


def _grid_frames_nearest(frames: int, *, remainder: int) -> int:
    below = _grid_frames_at_or_below(frames, remainder=remainder)
    above = _grid_frames_at_or_above(frames, remainder=remainder)
    return below if frames - below <= above - frames else above


def _snap_video_scenes_sglang(scenes: list[dict]) -> list[dict]:
    """Every video scene's *delivered* duration onto sglang's grid (spec §4.1.5), nearest point:
    `17n+5` for scene 0 and every `fresh_start` scene, `17n+4` for a chained one (it requests one
    frame more, the repeated keyframe, and H3 renders `17j+5`)."""
    snapped = []
    for scene in scenes:
        chained = scene["idx"] > 0 and not scene.get("fresh_start", False)
        remainder = ((_H3_LATENTS_PER_CHUNK - _SGLANG_OVERLAP_FRAMES) % _H3_FRAMES_PER_CHUNK
                     if chained else _H3_LATENTS_PER_CHUNK)
        frames = _grid_frames_nearest(round(float(scene["duration"]) * _H3_FPS),
                                      remainder=remainder)
        snapped.append({**scene, "duration": frames / _H3_FPS})
    return snapped


def _scene_reference_errors(proj, scenes: list[dict], outdir) -> list[dict]:
    """spec §3.5/§4.1.3, checked at the gate so nothing is queued that sglang would refuse: every
    @tag well-formed and pinned to the project, pictures within H3_MAX_REF_IMAGES, and **every**
    scene names at least one reference -- the server serves only ref2va. A clip scene is exempt
    from the last rule: its track piece is an audio reference."""
    pinned = {ref["tag"]: ref for ref in proj.references}
    errors: list[dict] = []
    for scene in scenes:
        idx = scene["idx"]
        try:
            limit = sglang_args.max_ref_images()
            tags = library_module.scene_tags(scene["prompt"])
        except (library_module.LibraryError, sglang_args.SglangArgsError) as exc:
            errors.append({"idx": idx, "code": exc.code, "message": exc.message})
            continue
        unknown = [tag for tag in tags if tag not in pinned]
        if unknown:
            errors.append({"idx": idx, "code": "unknown_tag",
                           "message": f"теги не подключены к проекту: {', '.join(unknown)}"})
            continue
        images = sum(len(card["assets"]) for card in
                     (library_module.get_card(outdir, tag, pinned[tag]["version"]) for tag in tags)
                     if card["kind"] != "voice")
        if images > limit:
            errors.append({"idx": idx, "code": "too_many_reference_images",
                           "message": f"картинок-референсов {images}, а можно не больше {limit}"})
            continue
        if not tags and proj.kind != "clip":
            errors.append({"idx": idx, "code": "ref2va_needs_reference",
                           "message": "нужен хотя бы один референс (@тег) в сцене"})
    return errors
```
Метод `_Handler`:
```python
    def _refuse_bad_scene_references(self, proj, scenes) -> None:
        errors = _scene_reference_errors(proj, scenes, self.server.outdir)
        if errors:
            raise CliError("scene_references_invalid",
                           "сцены нельзя ставить в очередь: " + "; ".join(
                               f"сцена {e['idx']}: {e['message']}" for e in errors),
                           {"scenes": errors})
```
В `_approve_project_stage`, ветка `stage == "script"`, `proj.kind == "video"` — перед `advance_project`:
```python
                if engine.is_sglang():
                    snapped = _snap_video_scenes_sglang(proj.scenes)
                    self._refuse_bad_scene_references(proj, snapped)
                    proj.scenes = snapped
                    proj.save()
```
Ветка `stage == "scenario"`, `proj.kind == "clip"` — между `build_clip_scenes(...)` и `proj.scenes = built`:
```python
            if engine.is_sglang():
                self._refuse_bad_scene_references(proj, built)
```
`cli.ERROR_CODES["scene_references_invalid"] = "one or more scenes name @tags or references sglang would refuse"`.

`project.py`:
```python
#: spec §4.1.3: the prefix a chained scene's prompt gets on sglang. No `<Picture N>` label -- the
#: server does not number the keyframe (presentation.py:230-270), so "<Picture 1>" would name the
#: first reference picture instead.
DEFAULT_I2V_PREFIX = ("The video begins exactly on the provided first frame and continues it "
                      "seamlessly: same characters, setting, lighting and camera style.")
```
`_OWNED_TOP_LEVEL_FIELDS` += `"i2v_prefix"`; `_apply`: `self.i2v_prefix = data.get("i2v_prefix", DEFAULT_I2V_PREFIX)`; `as_dict`: `"i2v_prefix": self.i2v_prefix,`; `create_project`: `"i2v_prefix": DEFAULT_I2V_PREFIX,`.
```python
    def update_settings(self, *, i2v_prefix: str) -> "Project":
        if not isinstance(i2v_prefix, str):
            raise ProjectError("i2v_prefix must be a string")
        with _project_lock(self.path.parent, exclusive=True):
            data = _read_data(self.path)
            data["i2v_prefix"] = i2v_prefix.strip()
            write_json_durably(self.path, data)
            self._apply(data)
        return self
```
`web.py`, `_route_put` (до ветки `/scenario`): `if path.startswith("/api/projects/") and path.endswith("/settings"): return self._put_project_settings(path[len("/api/projects/"):-len("/settings")])`;
```python
    def _put_project_settings(self, raw_id: str) -> tuple[int, str, bytes]:
        proj = self._load_project(raw_id)
        payload = self._json_request(allowed=("i2v_prefix",))
        if not isinstance(payload.get("i2v_prefix"), str):
            raise CliError("args_invalid", "`i2v_prefix` must be a string", {})
        proj.update_settings(i2v_prefix=payload["i2v_prefix"])
        return 200, "application/json", _json_bytes(
            {"ok": True, "project": _project_payload(project_module.load_project(proj.path))})
```

Чат: `_chat_message` — `allowed=("text", "prompt", "provider", "duration", "image", "set_mode", "tags")`. В `_locked_turn` перед сборкой `system`:
```python
        raw_tags = payload.get("tags")
        if raw_tags is not None:
            if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
                raise CliError("args_invalid", "`tags` must be a list of @tags", {})
            session["tags"] = list(raw_tags)
        references_block = ""
        if session.get("tags"):
            try:
                cards = [library_module.get_card(self.server.outdir, tag) for tag in session["tags"]]
            except library_module.LibraryError as exc:
                raise CliError(exc.code, exc.message, exc.detail) from exc
            references_block = "\n\n" + library_module.references_context(cards)
```
и к выражению `system = (...)` дописать в конец `+ references_block`. Убедиться, что `session` с ключом `tags` сохраняется той же записью сессии, что и `messages` (`web.py:5484` и далее); если сессия пишется до этого места — перенести присваивание выше записи.

- [ ] **Step 8: Зелёный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_sglang_scenes.py tests/test_sglang_gates.py tests/test_sglang_chat_tags.py tests/test_web_projects.py tests/test_chat_web.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 9: Мутации**

(а) Убрать в `_submit_next_scene` строку перехода на sglang → `test_a_fifth_chained_scene_...` FAIL (уйдёт в MLX-путь: `--turbo-strength`, keyframe по `dur-1.5`). (б) `SGLANG_OVERLAP_FRAMES = 0` → тот же тест FAIL (`head_drop_frames` 0, длительность 174/24 вне сетки → `AssembleError`). (в) В `_snap_video_scenes_sglang` взять `remainder = _H3_LATENTS_PER_CHUNK` для всех → `test_approving_the_script_snaps_...` FAIL `[175/24, 175/24, ...]`. (г) Убрать условие `if not tags and proj.kind != "clip"` → `test_every_scene_without_a_tag_is_refused...` FAIL `assert 200 == 400`; заменить его на старое «только сцепленные» (`idx > 0 and not fresh_start`) → тот же тест FAIL на `detail` (нет сцен 0 и 2). (д) В `_locked_turn` не сохранять `session["tags"]` (использовать только `payload`) → вторая половина `test_chat_tags_ride_the_system_message` FAIL. Ошибки — в отчёт.

- [ ] **Step 10: Полный прогон и commit**

```bash
env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest -q -p no:cacheprovider
git add h3_48gb/assemble.py h3_48gb/web.py h3_48gb/cli.py h3_48gb/project.py tests/test_sglang_scenes.py tests/test_sglang_gates.py tests/test_sglang_chat_tags.py
git commit -m "feat(sglang): цепочка сцен через последний кадр, снап на сетку, проверка @-тегов на гейтах, теги в чате"
```

---

## Task 6: Адаптер sglang: payload, опрос, `engine_ref`, возобновление без второго POST, отмена `running`, 4xx, пропажа сервера

**Files:**
- Create: `h3_48gb/engines/sglang.py`, `tests/_fake_sglang.py`
- Modify: `h3_48gb/queue.py:41` (импорт `field`), `queue.py:116-149` (`Job` += 3 поля), `queue.py:177-190` (`Reconciled.resumable`), `queue.py:1148-1172` (`reconcile`), новые `JobNotRunning`, `set_running_fields`, `request_cancel`, `cancel_reason`
- Modify: `h3_48gb/worker.py:641-753` (`run_job` — sglang-ветка), `worker.py:894-989` (`main_loop` — возобновление), новая `_run_sglang_generate_job`
- Modify: `h3_48gb/web.py:3758-3806` (`_cancel_job` — отмена `running` при sglang)
- Test: `tests/test_sglang_adapter.py` (новый), `tests/test_sglang_resume.py` (новый)

**Interfaces:**
- Consumes: `sglang_args.parse/SglangSpec` (задача 3), `sglang_estimate.record` (задача 3), `_scene_generate_args_sglang` (задача 5), `library.build_ref2va` (задача 4), `engine.is_sglang()`.
- Produces (`queue.py`):
  - `Job.engine_ref: str | None = None`, `Job.wait_reason: str | None = None`, `Job.cancel_reason: str | None = None`, `Job.engine_submitted_at: float | None = None` (старые файлы без полей читаются по умолчаниям)
  - `Reconciled.resumable: list[Job]` — задачи в `running` с `engine_ref` и свободной арендой; в `pending` они **не** возвращаются
  - `class JobNotRunning(QueueError)`; `set_running_fields(root, job_id, **fields) -> Job` (только `engine_ref`, `engine_submitted_at`, `wait_reason`); `request_cancel(root, job_id, reason: str) -> Job`; `cancel_reason(root, job_id) -> str | None`
- Produces (`h3_48gb/engines/sglang.py`):
  - `MODEL`, `DEFAULT_URL = "http://127.0.0.1:30020"`, `POLL_SECONDS = 20.0`, `LOST_RETRIES = 5`, `LOST_RETRY_SECONDS = 30.0`
  - `normalize_h3_uri(value)`, `normalize_h3_conditions(conditions)` — перенос из `h3-bench/video_paths.py`
  - `class SglangHTTPError(Exception)` (`.status`, `.detail`, `.body`), `class SglangUnavailable(Exception)`
  - `class SglangClient(base_url: str, timeout: float = 60.0)`: `create(payload) -> dict`, `get(video_id) -> dict`, `delete(video_id) -> None`, `download(video_id, dest: Path) -> None` (атомарно через `.part`, сверка `Content-Length`)
  - `build_payload(spec: SglangSpec) -> dict`
  - `run_generate(job, *, root, outdir, client, gate=None, sleep=time.sleep, clock=time.time) -> tuple[int, str]` (`wall_s` — от принятого POST, и после рестарта тоже: время POST хранится в задаче); `gate(job) -> str | None` (None — карта готова; строка — причина отмены во время ожидания). Задача 8 передаёт настоящий `gate`.
- Produces (`worker.py`): `_run_sglang_generate_job(root, outdir, job, *, gate=None) -> tuple[int, str]`; `run_job` при `H3_ENGINE=sglang` исполняет generate в процессе; `main_loop` первым делом возобновляет `state.resumable` (даже на паузе — это уже начатая сцена).
- Produces (`web.py`): `DELETE /api/jobs/<id>` для `running` при sglang → `200 {"ok": true, "cancelling": true, "job": {...}, "message": "H3 досчитает сцену впустую, следующая задача начнётся после"}`.

- [ ] **Step 1: Фейковый sglang**

`tests/_fake_sglang.py`:
```python
"""A stand-in for sglang's `/v1/videos` API, shaped after `video_api.py` on alex-neuro: POST
answers `{"id", "status": "queued"}`; GET answers `queued`/`completed`/`failed` (no `running`
exists there); an unknown id is 404 `{"detail": "Video not found"}`; a 4xx body is
`{"detail": "..."}`; DELETE only forgets the record (it never stops the GPU)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeSglang:
    def __init__(self, *, statuses=("completed",), content=b"MP4-BYTES", post_status=200,
                 post_body=None, error=None, drop_gets=0, truncate_contents=0):
        self.posts: list[dict] = []
        self.gets: list[str] = []
        self.deletes: list[str] = []
        self.contents: list[str] = []
        self.ids: list[str] = []
        self.forget_all = False
        self._statuses = list(statuses)
        self.content = content
        self.post_status = post_status
        self.post_body = post_body
        self.error = error
        self.drop_gets = drop_gets
        self.truncate_contents = truncate_contents
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _next_status(self) -> str:
        return self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]

    def _handler(fake):  # noqa: N805 -- closes over the fake, not a method of the handler
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send_json(self, status, body):
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                fake.posts.append(json.loads(self.rfile.read(length)))
                if fake.post_status != 200:
                    return self._send_json(fake.post_status, fake.post_body or {"detail": "bad"})
                video_id = f"vid-{len(fake.posts)}"
                fake.ids.append(video_id)
                self._send_json(200, {"id": video_id, "object": "video", "status": "queued",
                                      "progress": 0})

            def do_GET(self):
                parts = self.path.strip("/").split("/")
                video_id = parts[2]
                if fake.forget_all or video_id not in fake.ids:
                    return self._send_json(404, {"detail": "Video not found"})
                if len(parts) == 4 and parts[3] == "content":
                    fake.contents.append(video_id)
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    if fake.truncate_contents > 0:
                        fake.truncate_contents -= 1
                        self.send_header("Content-Length", str(len(fake.content) + 100))
                        self.end_headers()
                        self.wfile.write(fake.content[:3])
                        self.close_connection = True
                        return
                    self.send_header("Content-Length", str(len(fake.content)))
                    self.end_headers()
                    self.wfile.write(fake.content)
                    return
                fake.gets.append(video_id)
                if fake.drop_gets > 0:
                    fake.drop_gets -= 1
                    self.close_connection = True
                    return
                status = fake._next_status()
                body = {"id": video_id, "status": status,
                        "progress": 100 if status == "completed" else 0}
                if status == "completed":
                    body.update({"inference_time_s": 300.5, "peak_memory_mb": 47000.0})
                if status == "failed":
                    body["error"] = fake.error or {"message": "boom"}
                self._send_json(200, body)

            def do_DELETE(self):
                video_id = self.path.strip("/").split("/")[2]
                fake.deletes.append(video_id)
                self._send_json(200, {"id": video_id, "status": "deleted"})
        return Handler
```

- [ ] **Step 2: Падающие тесты payload и адаптера**

`tests/test_sglang_adapter.py`:
```python
"""The sglang adapter against a fake sglang (spec §4.1.3, §4.1.6-7, §5). Every payload is an
exact dict, one per case of §4.1.3."""
import pytest

from h3_48gb import assemble
from h3_48gb import library as lib
from h3_48gb import queue as q
from h3_48gb.engines import sglang as sg
from h3_48gb.engines import sglang_args as sa
from _fake_sglang import FakeSglang

COMMON = {"model": "MiniMaxAI/MiniMax-H3", "num_outputs_per_prompt": 1,
          "num_inference_steps": 50, "flow_shift": 12.0, "audio_flow_shift": 3.0,
          "seed": 42, "quality": "lossless"}


def _png(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return str(path)


def _spec(tmp_path, *extra, duration=175 / 24, prompt="p"):
    return sa.parse(["generate", prompt, "--width", "896", "--height", "512",
                     "--duration", str(duration), "--tag", "t", "--outdir", str(tmp_path),
                     *extra])


def test_payload_first_scene_with_references(tmp_path):
    a, b = _png(tmp_path / "a.png"), _png(tmp_path / "b.png")
    assert sg.build_payload(_spec(tmp_path, "--ref", a, "--ref", b)) == {
        **COMMON, "prompt": "p", "task": "ref2va",
        "conditions": [{"type": "image", "uri": a, "role": "reference"},
                       {"type": "image", "uri": b, "role": "reference"}],
        "target": {"short_edge": 512, "aspect_ratio": "16:9", "duration_seconds": 175 / 24}}


def test_payload_chained_scene_keeps_references_and_asks_auto(tmp_path):
    kf, a = _png(tmp_path / "kf.png"), _png(tmp_path / "a.png")
    assert sg.build_payload(_spec(tmp_path, "--image", kf, "--aspect", "auto", "--ref", a)) == {
        **COMMON, "prompt": "p", "task": "ref2va",
        "conditions": [{"type": "image", "uri": kf, "role": "keyframe", "frame_index": 0},
                       {"type": "image", "uri": a, "role": "reference"}],
        "target": {"short_edge": 512, "aspect_ratio": "auto", "duration_seconds": 175 / 24}}


def test_payload_clip_scene_adds_its_track_piece_as_audio_reference(tmp_path):
    kf = _png(tmp_path / "kf.png")
    piece = tmp_path / "piece.wav"
    piece.write_bytes(b"RIFF")
    assert sg.build_payload(_spec(tmp_path, "--image", kf, "--aspect", "auto",
                                  "--audio", str(piece))) == {
        **COMMON, "prompt": "p", "task": "ref2va",
        "conditions": [{"type": "image", "uri": kf, "role": "keyframe", "frame_index": 0},
                       {"type": "audio", "uri": f"file://{piece}", "role": "reference"}],
        "target": {"short_edge": 512, "aspect_ratio": "auto", "duration_seconds": 175 / 24}}


def test_two_tags_one_with_two_pictures_plus_keyframe_end_to_end(tmp_path, monkeypatch):
    """spec §6: the library fixture through assemble's real argv to the exact payload; a mutation
    of the picture order must fail here."""
    monkeypatch.setattr(assemble.secrets, "token_hex", lambda n: "ab12")
    out = tmp_path / "out"
    lib.create_card(out, tag="@alice", kind="person", description="a young woman",
                    assets=[_png(out / "u" / "face.png"), _png(out / "u" / "back.png")])
    lib.create_card(out, tag="@beach", kind="environment", description="a wide beach",
                    assets=[_png(out / "u" / "pano.png")])
    refs = [{"tag": "@alice", "version": 1}, {"tag": "@beach", "version": 1}]
    ref2va = lib.build_ref2va("@alice walks on @beach", refs, out)
    kf = _png(tmp_path / "kf.png")
    args, _ = assemble._scene_generate_args_sglang(
        {"idx": 1, "prompt": "@alice walks on @beach", "duration": 174 / 24}, keyframe=kf,
        chained=True, ref2va=ref2va, track_piece=None, scenes_dir=tmp_path)
    L = out / "library"
    assert sg.build_payload(sa.parse(args)) == {
        **COMMON,
        "prompt": ("subject_definitions:\n"
                   "<Subject 1> is a young woman, appearance from <Picture 1>, <Picture 2>.\n"
                   "<Subject 2> is a wide beach, appearance from <Picture 3>.\n\n"
                   "<Subject 1> walks on <Subject 2>"),
        "task": "ref2va",
        "conditions": [
            {"type": "image", "uri": kf, "role": "keyframe", "frame_index": 0},
            {"type": "image", "uri": str(L / "alice" / "v1" / "01-face.png"), "role": "reference"},
            {"type": "image", "uri": str(L / "alice" / "v1" / "02-back.png"), "role": "reference"},
            {"type": "image", "uri": str(L / "beach" / "v1" / "01-pano.png"), "role": "reference"}],
        "target": {"short_edge": 512, "aspect_ratio": "auto", "duration_seconds": 175 / 24}}


def test_normalize_rewrites_only_known_legacy_roots():
    assert sg.normalize_h3_uri("/home/alex/h3-bench/inputs/a.png") == \
        "/home/alex/Projects/h3-bench/inputs/a.png"
    assert sg.normalize_h3_uri("file:///home/alex/h3-bench/outputs/x.wav") == \
        "file:///home/alex/Outputs/h3-bench/x.wav"
    assert sg.normalize_h3_uri("/home/alex/Outputs/h3-panel/p/k.png") == \
        "/home/alex/Outputs/h3-panel/p/k.png"


# -- run_generate ------------------------------------------------------------------------------


@pytest.fixture
def queued(tmp_path):
    root = q.layout(tmp_path / "queue")["root"]
    (tmp_path / "scenes").mkdir()
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n")
    args = ["generate", "p", "--width", "896", "--height", "512", "--duration", str(175 / 24),
            "--tag", "t", "--outdir", str(tmp_path / "scenes"), "--ref", str(ref)]
    job = q.submit(root, args, "", {"output_stem": str(tmp_path / "scenes" / "h3-t-896x512")}, {})
    return root, q.claim(root)


class _Clock:
    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


def _run(job, root, tmp_path, fake, **kw):
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("clock", _Clock(0.0, 100.0))
    return sg.run_generate(job, root=root, outdir=tmp_path, client=sg.SglangClient(fake.url), **kw)


def _report(job):
    import json
    from pathlib import Path
    return json.loads(Path(job.output_stem + ".json").read_text(encoding="utf-8"))


def test_completed_downloads_and_reports(queued, tmp_path):
    root, job = queued
    fake = FakeSglang(statuses=("queued", "completed"))
    try:
        code, log = _run(job, root, tmp_path, fake)
    finally:
        fake.close()
    from pathlib import Path
    assert (code, log) == (0, "sglang: id=vid-1\nsglang: готово, 100.0 с\n")
    assert Path(job.output_stem + ".mp4").read_bytes() == b"MP4-BYTES"
    assert not Path(job.output_stem + ".mp4.part").exists()
    payload = sg.build_payload(sa.parse(job.args, check_files=False))
    assert fake.posts == [payload]
    assert fake.gets == ["vid-1", "vid-1"]
    assert _report(job) == {"engine": "sglang", "status": "completed", "id": "vid-1",
                            "wall_s": 100.0, "inference_time_s": 300.5,
                            "peak_memory_mb": 47000.0, "payload": payload}
    assert [j for j in q.scan(root)[0] if j.id == job.id][0].engine_ref == "vid-1"
    import json
    history = (tmp_path / "sglang-history.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in history] == \
        [{"width": 896, "height": 512, "frames": 175, "wall_s": 100.0}]


def test_a_4xx_fails_at_once_and_keeps_the_body(queued, tmp_path):
    root, job = queued
    detail = "target.duration_seconds must be in [3, 15], got 2"
    fake = FakeSglang(post_status=400, post_body={"detail": detail})
    try:
        code, log = _run(job, root, tmp_path, fake)
    finally:
        fake.close()
    assert (code, log) == (1, f"sglang отказал (400): {detail}\n")
    assert fake.gets == []
    report = _report(job)
    assert (report["status"], report["http_status"], report["detail"], report["id"]) == \
        ("rejected", 400, detail, None)


def test_sglang_failed_is_failed_with_its_message(queued, tmp_path):
    root, job = queued
    fake = FakeSglang(statuses=("failed",), error={"message": "CUDA out of memory"})
    try:
        code, log = _run(job, root, tmp_path, fake)
    finally:
        fake.close()
    assert (code, log) == (1, "sglang: id=vid-1\nsglang: сцена упала: CUDA out of memory\n")


def test_one_poll_interval_is_twenty_one_second_slices(queued, tmp_path):
    root, job = queued
    fake = FakeSglang(statuses=("queued", "completed"))
    sleeps = []
    try:
        _run(job, root, tmp_path, fake, sleep=sleeps.append)
    finally:
        fake.close()
    assert sleeps == [1.0] * 20


def test_one_lost_poll_waits_thirty_one_second_slices(queued, tmp_path):
    root, job = queued
    fake = FakeSglang(statuses=("completed",), drop_gets=1)
    sleeps = []
    try:
        _run(job, root, tmp_path, fake, sleep=sleeps.append)
    finally:
        fake.close()
    assert sleeps == [1.0] * 30


def test_wall_time_after_a_resume_counts_from_the_original_post(queued, tmp_path):
    root, job = queued
    fake = FakeSglang(statuses=("completed",))
    fake.ids.append("vid-7")                       # posted by the previous worker
    q.set_running_fields(root, job.id, engine_ref="vid-7", engine_submitted_at=50.0)
    resumed = [j for j in q.scan(root)[0] if j.id == job.id][0]
    try:
        code, log = _run(resumed, root, tmp_path, fake, clock=_Clock(130.0))
    finally:
        fake.close()
    assert (code, log) == (0, "sglang: продолжаю опрос id=vid-7 после рестарта\n"
                              "sglang: готово, 80.0 с\n")
    assert (fake.posts, _report(resumed)["wall_s"]) == ([], 80.0)


def test_two_dropped_polls_are_survived(queued, tmp_path):
    root, job = queued
    fake = FakeSglang(statuses=("completed",), drop_gets=2)
    sleeps = []
    try:
        code, _ = _run(job, root, tmp_path, fake, sleep=sleeps.append)
    finally:
        fake.close()
    assert code == 0
    assert fake.gets == ["vid-1", "vid-1", "vid-1"]


def test_five_dropped_polls_mean_h3_is_gone(queued, tmp_path):
    root, job = queued
    fake = FakeSglang(statuses=("queued",), drop_gets=99)
    try:
        code, log = _run(job, root, tmp_path, fake)
    finally:
        fake.close()
    assert (code, log) == (1, "sglang: id=vid-1\nsglang: H3 пропал — 5 попыток через 30 с без ответа\n")
    assert len(fake.gets) == 5


def test_download_is_atomic(queued, tmp_path):
    """Review Focus 4: a truncated /content must never leave a `<stem>.mp4` that queue.reconcile
    would take for a finished run."""
    from pathlib import Path
    root, job = queued
    fake = FakeSglang(statuses=("completed",), truncate_contents=99)
    try:
        code, log = _run(job, root, tmp_path, fake)
    finally:
        fake.close()
    assert code == 1
    assert not Path(job.output_stem + ".mp4").exists()
    assert not Path(job.output_stem + ".mp4.part").exists()


def test_a_truncated_download_is_retried_and_then_succeeds(queued, tmp_path):
    from pathlib import Path
    root, job = queued
    fake = FakeSglang(statuses=("completed",), truncate_contents=1)
    try:
        code, _ = _run(job, root, tmp_path, fake)
    finally:
        fake.close()
    assert code == 0
    assert Path(job.output_stem + ".mp4").read_bytes() == b"MP4-BYTES"
    assert fake.contents == ["vid-1", "vid-1"]


def test_cancel_while_polling_deletes_and_fails(queued, tmp_path):
    root, job = queued
    fake = FakeSglang(statuses=("queued",))

    def sleep(seconds):
        q.request_cancel(root, job.id, "cancelled_by_user")

    try:
        code, log = _run(job, root, tmp_path, fake, sleep=sleep)
    finally:
        fake.close()
    assert (code, log) == (1, "sglang: id=vid-1\nsglang: cancelled_by_user — H3 досчитает сцену "
                              "впустую, следующая задача начнётся после\n")
    assert fake.deletes == ["vid-1"]
    assert _report(job)["status"] == "cancelled"


def test_a_gate_that_reports_cancel_stops_before_any_post(queued, tmp_path):
    root, job = queued
    fake = FakeSglang()
    try:
        code, log = _run(job, root, tmp_path, fake, gate=lambda j: "cancelled_by_user")
    finally:
        fake.close()
    assert (code, fake.posts) == (1, [])


def test_set_running_fields_refuses_unknown_fields_and_non_running_jobs(queued):
    root, job = queued
    with pytest.raises(ValueError):
        q.set_running_fields(root, job.id, note="x")
    with pytest.raises(q.JobNotRunning):
        q.set_running_fields(root, "nope", engine_ref="x")
```

- [ ] **Step 3: Падающие тесты возобновления и отмены через API**

`tests/test_sglang_resume.py`:
```python
"""Restart in the middle of a render (spec §5, §6): exactly one POST, polling resumes on the
same id; an id sglang no longer knows fails honestly; cancelling a running job from the page."""
import pytest

from h3_48gb import queue as q
from h3_48gb import worker
from h3_48gb.engines import sglang as sg
from _fake_sglang import FakeSglang
from test_web import _call, _serve
from test_worker import _stop_after


class _Crash(BaseException):
    """A worker dying mid-poll (a signal, an OOM kill). BaseException on purpose: the worker's
    own `except Exception` safety net around the adapter must not swallow it."""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    monkeypatch.setattr(sg, "POLL_SECONDS", 0.0)
    monkeypatch.setattr(sg, "LOST_RETRY_SECONDS", 0.0)
    root = q.layout(tmp_path / "queue")["root"]
    (tmp_path / "scenes").mkdir()
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n")
    args = ["generate", "p", "--width", "896", "--height", "512", "--duration", str(175 / 24),
            "--tag", "t", "--outdir", str(tmp_path / "scenes"), "--ref", str(ref)]
    q.submit(root, args, "", {"output_stem": str(tmp_path / "scenes" / "h3-t-896x512")}, {})
    return root, tmp_path


def _crash_after_post(monkeypatch):
    real = sg._sleep_unless_cancelled
    monkeypatch.setattr(sg, "_sleep_unless_cancelled",
                        lambda *a, **k: (_ for _ in ()).throw(_Crash()))
    return real


def test_restart_between_post_and_completed_resumes_without_a_second_post(env, monkeypatch):
    root, tmp_path = env
    fake = FakeSglang(statuses=("queued", "completed"))
    monkeypatch.setenv("H3_SGLANG_URL", fake.url)
    try:
        real = _crash_after_post(monkeypatch)
        job = q.claim(root)
        with pytest.raises(_Crash):
            worker.run_job(root, job, outdir=tmp_path)
        state = q.reconcile(root)
        assert [j.id for j in state.resumable] == [job.id]
        assert state.resumable[0].engine_ref == "vid-1"
        assert q.scan(root)[0][0].state == "running"   # not back in pending
        monkeypatch.setattr(sg, "_sleep_unless_cancelled", real)
        worker.main_loop(root, poll=0.01, stop=_stop_after(1.0), outdir=tmp_path)
    finally:
        fake.close()
    assert len(fake.posts) == 1
    assert set(fake.gets) == {"vid-1"}
    (done,) = [j for j in q.scan(root)[0] if j.id == job.id]
    assert (done.state, done.exit_code) == ("done", 0)


def test_an_id_sglang_forgot_fails_as_lost(env, monkeypatch):
    root, tmp_path = env
    fake = FakeSglang(statuses=("queued",))
    monkeypatch.setenv("H3_SGLANG_URL", fake.url)
    try:
        real = _crash_after_post(monkeypatch)
        job = q.claim(root)
        with pytest.raises(_Crash):
            worker.run_job(root, job, outdir=tmp_path)
        fake.forget_all = True          # H3 restarted: its in-memory store is empty
        monkeypatch.setattr(sg, "_sleep_unless_cancelled", real)
        (resumable,) = q.reconcile(root).resumable
        code = worker.run_job(root, resumable, outdir=tmp_path)
    finally:
        fake.close()
    assert code == 1
    (failed,) = [j for j in q.scan(root)[0] if j.id == job.id]
    assert failed.state == "failed"
    assert "задача потеряна при рестарте H3 (id=vid-1 неизвестен серверу)" in failed.log_tail
    assert len(fake.posts) == 1


def test_a_running_job_without_engine_ref_still_returns_to_pending(env):
    root, tmp_path = env
    job = q.claim(root)                 # claimed, never posted (e.g. died while waiting for GPU)
    state = q.reconcile(root)
    assert (state.resumable, [j.id for j in state.changed]) == ([], [job.id])
    assert q.scan(root)[0][0].state == "pending"


def test_the_page_cancels_a_running_sglang_job(env, monkeypatch):
    root, tmp_path = env
    job = q.claim(root)
    outdir = tmp_path
    live = _serve(root, outdir)
    try:
        status, body = _call(live, "DELETE", f"/api/jobs/{job.id}")
    finally:
        live.httpd.shutdown()
        live.httpd.server_close()
    assert status == 200, body
    assert (body["ok"], body["cancelling"], body["message"], body["job"]["cancel_reason"]) == \
        (True, True, "H3 досчитает сцену впустую, следующая задача начнётся после",
         "cancelled_by_user")
    assert q.cancel_reason(root, job.id) == "cancelled_by_user"


def test_on_mlx_a_running_job_still_cannot_be_cancelled(env, monkeypatch):
    root, tmp_path = env
    monkeypatch.setenv("H3_ENGINE", "mlx")
    job = q.claim(root)
    live = _serve(root, tmp_path)
    try:
        status, body = _call(live, "DELETE", f"/api/jobs/{job.id}")
    finally:
        live.httpd.shutdown()
        live.httpd.server_close()
    assert body["error"]["code"] == "job_not_pending"
```

- [ ] **Step 4: Красный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_sglang_adapter.py tests/test_sglang_resume.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'h3_48gb.engines.sglang'`.

- [ ] **Step 5: Очередь**

`queue.py`: `from dataclasses import asdict, dataclass, field`. В конец полей `Job` (после `log_tail`):
```python
    #: sglang's own job id once the adapter's POST was accepted (spec §4.1.6). A running job that
    #: carries one is resumed by polling, never re-posted (`reconcile`'s `resumable`).
    engine_ref: str | None = None
    #: Why a running job is not computing yet ("ждём GPU: ...", "остываем, 81 °C"), for the page.
    wait_reason: str | None = None
    #: Set by the page (`request_cancel`) on a running sglang job; the adapter stops waiting.
    cancel_reason: str | None = None
    #: Wall-clock time of the accepted POST, so `wall_s` after a resume still counts from it.
    engine_submitted_at: float | None = None
```
`Reconciled`: после `conflicted: list[Broken]` — `resumable: list[Job] = field(default_factory=list)` (докстринг: «running jobs with an `engine_ref` and a free lease: a worker died while sglang was computing; the next worker resumes polling them instead of re-posting»). В `reconcile`: `resumable: list[Job] = []`, во всех `Reconciled(...)` добавить `resumable=resumable`; перед `changed.append(_return_to_pending_locked(root, job.id))`:
```python
                if job.engine_ref:
                    resumable.append(job)
                    continue
```
Новые функции (после `finish`):
```python
class JobNotRunning(QueueError):
    """A running-only mutation was asked of a job that is not in `running/`."""


RUNNING_FIELDS = ("engine_ref", "engine_submitted_at", "wait_reason")


def _mutate_running(root, job_id: str, fields: dict) -> Job:
    root = Path(root)
    with queue_lock(root, exclusive=True):
        running = job_path(root, job_id, "running")
        if not running.exists():
            raise JobNotRunning(f"job {job_id} is not running")
        data = _read_job_dict(running)
        data.update(fields)
        job = _build_job(data, "running")
        write_json_durably(running, data)
        return job


def set_running_fields(root, job_id: str, **fields) -> Job:
    unknown = set(fields) - set(RUNNING_FIELDS)
    if unknown:
        raise ValueError(f"not a running-job field: {sorted(unknown)}")
    return _mutate_running(root, job_id, fields)


def request_cancel(root, job_id: str, reason: str) -> Job:
    return _mutate_running(root, job_id, {"cancel_reason": reason})


def cancel_reason(root, job_id: str) -> str | None:
    """Read without the queue lock: every write to a job file is an atomic replace, so a reader
    sees the old file or the new one, never half of either."""
    try:
        data = _read_job_dict(job_path(root, job_id, "running"))
    except (OSError, QueueError, ValueError):
        return None
    return data.get("cancel_reason")
```
(Если `_read_job_dict` бросает иной тип на отсутствующем файле — поймать и его; проверить по коду `queue.py`.)

- [ ] **Step 6: Адаптер `h3_48gb/engines/sglang.py`**

```python
"""One sglang generate job, run inside the worker process (spec §3.3.3, §4.1, §5).

POST /v1/videos once; write sglang's id into the job (`engine_ref`) at once; poll every 20 s;
download `/content` atomically on `completed`. A worker restart resumes polling the same id (the
queue hands such jobs back as `resumable`, never as pending), so a scene is posted exactly once.
`DELETE` is sent on cancel but does not stop the GPU (video_api.py:821-827) -- the message says so.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

from h3_48gb import queue as q
from h3_48gb.engines import estimate as sglang_estimate
from h3_48gb.engines import sglang_args

MODEL = "MiniMaxAI/MiniMax-H3"
DEFAULT_URL = "http://127.0.0.1:30020"
POLL_SECONDS = 20.0
LOST_RETRIES = 5
LOST_RETRY_SECONDS = 30.0
_CANCEL_SLICE_SECONDS = 1.0

# -- ported from h3-bench/video_paths.py (no import from h3-bench, spec §4.1.3) ----------------
_PROJECTS_ROOT = Path(os.environ.get("ALEX_PROJECTS_ROOT", "/home/alex/Projects"))
_OUTPUTS_ROOT = Path(os.environ.get("ALEX_OUTPUTS_ROOT", "/home/alex/Outputs"))
_OUTPUT_DIR = Path(os.environ.get("H3_BENCH_OUTPUT_DIR", str(_OUTPUTS_ROOT / "h3-bench")))
_H3_BENCH_DIR = Path(os.environ.get("H3_BENCH_DIR", str(_PROJECTS_ROOT / "h3-bench")))
_PUBPORT_IMAGES_DIR = Path(os.environ.get("PUBPORT_IMAGES_DIR",
                                          str(_H3_BENCH_DIR / "inputs/pubport-images")))


def normalize_h3_uri(value):
    """Map only known legacy path roots, keeping file:// and unrelated URIs untouched."""
    if not isinstance(value, str):
        return value
    scheme = "file://" if value.startswith("file://") else ""
    path = value[len(scheme):]
    roots = (("/home/alex/PubPort/h3-bench/outputs", _OUTPUT_DIR),
             ("/home/alex/h3-bench/outputs", _OUTPUT_DIR),
             ("/home/alex/Outputs/h3-bench", _OUTPUT_DIR),
             ("/home/alex/PubPort/images", _PUBPORT_IMAGES_DIR),
             ("/home/alex/Projects/h3-bench", _H3_BENCH_DIR),
             ("/home/alex/h3-bench", _H3_BENCH_DIR))
    for old, new in roots:
        if path == old or path.startswith(old + "/"):
            return scheme + str(new) + path[len(old):]
    return value


def normalize_h3_conditions(conditions):
    return [dict(condition, uri=normalize_h3_uri(condition["uri"]))
            if "uri" in condition else dict(condition) for condition in conditions]


class SglangHTTPError(Exception):
    def __init__(self, status: int, detail: str, body: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail
        self.body = body


class SglangUnavailable(Exception):
    """No HTTP answer at all: refused, reset, timed out, or a body cut short."""


def _detail(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    try:
        detail = json.loads(text).get("detail")
    except (ValueError, AttributeError):
        return text[:2000]
    return detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)[:2000]


class SglangClient:
    def __init__(self, base_url: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _open(self, method: str, path: str, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        request = urllib.request.Request(self.base_url + path, data=data, method=method,
                                         headers=headers)
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            raise SglangHTTPError(exc.code, _detail(raw), raw.decode("utf-8", "replace")) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            raise SglangUnavailable(f"{method} {path}: {exc}") from None

    def _json(self, method: str, path: str, payload=None) -> dict:
        response = self._open(method, path, payload)
        try:
            with response:
                return json.loads(response.read())
        except (http.client.HTTPException, OSError, ValueError) as exc:
            raise SglangUnavailable(f"{method} {path}: {exc}") from None

    def create(self, payload: dict) -> dict:
        return self._json("POST", "/v1/videos", payload)

    def get(self, video_id: str) -> dict:
        return self._json("GET", f"/v1/videos/{video_id}")

    def delete(self, video_id: str) -> None:
        self._json("DELETE", f"/v1/videos/{video_id}")

    def download(self, video_id: str, dest: Path) -> None:
        dest = Path(dest)
        part = dest.with_name(dest.name + ".part")
        response = self._open("GET", f"/v1/videos/{video_id}/content")
        try:
            with response, open(part, "wb") as out:
                expected = response.headers.get("Content-Length")
                shutil.copyfileobj(response, out, 1 << 20)
            if expected is not None and part.stat().st_size != int(expected):
                raise SglangUnavailable(
                    f"download {video_id}: {part.stat().st_size} of {expected} bytes")
        except (http.client.HTTPException, OSError, SglangUnavailable) as exc:
            part.unlink(missing_ok=True)
            if isinstance(exc, SglangUnavailable):
                raise
            raise SglangUnavailable(f"download {video_id}: {exc}") from None
        os.replace(part, dest)


def build_payload(spec) -> dict:
    """spec §4.1.3, after chain_beach.py/runner_clip.py: keyframe first (frame 0), then reference
    pictures in order (they become <Picture 1..>), then audio references as file:// URIs."""
    conditions = []
    if spec.image:
        conditions.append({"type": "image", "uri": spec.image, "role": "keyframe",
                           "frame_index": 0})
    for ref in spec.refs:
        conditions.append({"type": "image", "uri": ref, "role": "reference"})
    for audio in spec.audio:
        conditions.append({"type": "audio", "uri": f"file://{audio}", "role": "reference"})
    return {"model": MODEL, "prompt": spec.prompt, "task": spec.task,
            "conditions": normalize_h3_conditions(conditions),
            "target": {"short_edge": spec.short_edge, "aspect_ratio": spec.aspect_ratio,
                       "duration_seconds": spec.duration},
            "num_outputs_per_prompt": 1, "num_inference_steps": spec.steps,
            "flow_shift": 12.0, "audio_flow_shift": 3.0, "seed": spec.seed,
            "quality": "lossless"}


def _sleep_unless_cancelled(root, job_id: str, seconds: float, sleep) -> str | None:
    """Sleep `seconds` in one-second slices; return the cancel reason as soon as one appears."""
    remaining = seconds
    while True:
        reason = q.cancel_reason(root, job_id)
        if reason or remaining <= 0:
            return reason
        step = min(_CANCEL_SLICE_SECONDS, remaining)
        sleep(step)
        remaining -= step


def _write_report(job, report: dict) -> None:
    q.write_json_durably(Path(job.output_stem + ".json"), report)


def run_generate(job, *, root, outdir, client, gate=None, sleep=time.sleep,
                 clock=time.time) -> tuple[int, str]:
    spec = sglang_args.parse(job.args, check_files=False)
    payload = build_payload(spec)
    log: list[str] = []
    video_id = job.engine_ref

    def done(code: int, line: str, report: dict | None = None) -> tuple[int, str]:
        log.append(line)
        if report is not None:
            _write_report(job, {"engine": "sglang", **report, "payload": payload})
        return code, "".join(log)

    if video_id is None:
        if gate is not None:
            reason = gate(job)
            if reason:
                return done(1, f"sglang: {reason} — сцена не начиналась\n",
                            {"status": "cancelled", "id": None, "reason": reason})
        started = clock()
        try:
            video_id = client.create(payload)["id"]
        except SglangHTTPError as exc:
            return done(1, f"sglang отказал ({exc.status}): {exc.detail}\n",
                        {"status": "rejected", "id": None, "http_status": exc.status,
                         "detail": exc.detail})
        except SglangUnavailable as exc:
            return done(1, f"sglang: H3 недоступен при постановке: {exc}\n",
                        {"status": "unavailable", "id": None, "error": str(exc)})
        q.set_running_fields(root, job.id, engine_ref=video_id, engine_submitted_at=started,
                             wait_reason=None)
        log.append(f"sglang: id={video_id}\n")
    else:
        started = job.engine_submitted_at if job.engine_submitted_at is not None else clock()
        log.append(f"sglang: продолжаю опрос id={video_id} после рестарта\n")

    misses = 0
    while True:
        reason = q.cancel_reason(root, job.id)
        if reason:
            try:
                client.delete(video_id)
            except (SglangHTTPError, SglangUnavailable):
                pass
            return done(1, f"sglang: {reason} — H3 досчитает сцену впустую, следующая задача "
                           f"начнётся после\n",
                        {"status": "cancelled", "id": video_id, "reason": reason})
        try:
            status = client.get(video_id)
            if status.get("status") == "completed":
                client.download(video_id, Path(job.output_stem + ".mp4"))
            misses = 0
        except SglangHTTPError as exc:
            if exc.status == 404:
                return done(1, f"sglang: задача потеряна при рестарте H3 (id={video_id} "
                               f"неизвестен серверу)\n",
                            {"status": "lost", "id": video_id,
                             "error": "задача потеряна при рестарте H3"})
            status = None
        except SglangUnavailable:
            status = None
        if status is None:
            misses += 1
            if misses >= LOST_RETRIES:
                return done(1, f"sglang: H3 пропал — {LOST_RETRIES} попыток через "
                               f"{LOST_RETRY_SECONDS:g} с без ответа\n",
                            {"status": "unavailable", "id": video_id, "error": "H3 пропал"})
            _sleep_unless_cancelled(root, job.id, LOST_RETRY_SECONDS, sleep)
            continue
        if status.get("status") == "completed":
            wall = round(clock() - started, 1)
            sglang_estimate.record(outdir, width=spec.width, height=spec.height,
                                   frames=spec.frames, wall_s=wall)
            return done(0, f"sglang: готово, {wall:.1f} с\n",
                        {"status": "completed", "id": video_id, "wall_s": wall,
                         "inference_time_s": status.get("inference_time_s"),
                         "peak_memory_mb": status.get("peak_memory_mb")})
        if status.get("status") == "failed":
            message = (status.get("error") or {}).get("message") or "без текста"
            return done(1, f"sglang: сцена упала: {message}\n",
                        {"status": "failed", "id": video_id, "error": message})
        _sleep_unless_cancelled(root, job.id, POLL_SECONDS, sleep)
```
Проверить на тестах: `test_five_dropped_polls_mean_h3_is_gone` ожидает ровно 5 GET; `test_download_is_atomic` — 5 неудачных скачиваний подряд дают `failed` («H3 пропал»), `.mp4` и `.part` отсутствуют. Отчёт `completed` пишется после `record` — порядок закреплён тестом `test_completed_downloads_and_reports`.

- [ ] **Step 7: Воркер и веб**

`worker.py` — новая функция рядом с `_run_assemble_job`:
```python
def _run_sglang_generate_job(root, outdir, job, *, gate=None) -> tuple[int, str]:
    """spec §3.3.3: on sglang a generate job runs in this process through the adapter, under the
    lease `run_job` already holds -- no subprocess, no caffeinate, no MLX."""
    from h3_48gb.engines import sglang as sglang_engine

    client = sglang_engine.SglangClient(os.environ.get("H3_SGLANG_URL", sglang_engine.DEFAULT_URL))
    try:
        return sglang_engine.run_generate(job, root=root, outdir=outdir, client=client, gate=gate)
    except Exception as exc:  # noqa: BLE001 -- a bug here must fail the job, not kill the worker
        return 1, f"sglang adapter crashed: {type(exc).__name__}: {exc}\n"
```
Широкий `except Exception` здесь не мешает тесту возобновления: его `_Crash` наследует `BaseException`, как настоящая смерть процесса.

В `run_job` условие первой ветки: `if job.kind == q.KIND_GENERATE and not engine.is_sglang():`; во второй ветке первым случаем:
```python
            if job.kind == q.KIND_GENERATE:
                exit_code, log_text = _run_sglang_generate_job(root, outdir, job)
            elif job.kind == q.KIND_SONG:
```
В `main_loop` сразу после блока `if state.alive: ...`:
```python
            if state.resumable:
                # spec §5: a scene sglang is already computing is resumed by polling its id,
                # paused queue or not -- pausing stops *new* work, this is old work.
                run_job(root, state.resumable[0], spawn=spawn, outdir=outdir)
                ran += 1
                continue
```
`web.py`, `_cancel_job` — после ветки `if pending: ...`:
```python
        if engine.is_sglang():
            with name_too_long_is_a_refusal("the job id"):
                running = q.job_path(self.server.queue_root, job_id, "running").exists()
            if running:
                with queue_write_errors(self.server.queue_root, what="the job id"):
                    job = q.request_cancel(self.server.queue_root, job_id, "cancelled_by_user")
                return 200, "application/json", _json_bytes({
                    "ok": True, "cancelling": True, "job": job.as_dict(),
                    "message": "H3 досчитает сцену впустую, следующая задача начнётся после"})
```

- [ ] **Step 8: Зелёный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_sglang_adapter.py tests/test_sglang_resume.py tests/test_queue.py tests/test_worker.py tests/test_web.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 9: Мутации**

(а) В `reconcile` убрать ветку `if job.engine_ref: resumable.append(job); continue` → `test_restart_between_post_and_completed_...` FAIL (`resumable == []`, задача ушла в pending, затем второй POST: `assert 2 == 1`). (б) В `run_generate` не вызывать `q.set_running_fields(... engine_ref=...)` → тот же тест FAIL. (в) В `build_payload` поставить keyframe после референсов → `test_payload_chained_scene_...` FAIL. (г) В `download` писать сразу в `dest` вместо `.part` → `test_download_is_atomic` FAIL (`.mp4` существует). (д) В `run_generate` при 404 считать промах вместо `lost` → `test_an_id_sglang_forgot_fails_as_lost` FAIL (в логе «H3 пропал»). (е) `LOST_RETRIES = 4` → `test_five_dropped_polls_mean_h3_is_gone` FAIL. (ж) `POLL_SECONDS = 10.0` → `test_one_poll_interval_is_twenty_one_second_slices` FAIL `[1.0]*10 != [1.0]*20`; `LOST_RETRY_SECONDS = 20.0` → `test_one_lost_poll_waits_thirty_one_second_slices` FAIL. (з) На возобновлении брать `started = clock()` → `test_wall_time_after_a_resume...` FAIL `'готово, 0.0 с'`. Ошибки — в отчёт.

- [ ] **Step 10: Полный прогон и commit**

```bash
env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest -q -p no:cacheprovider
git add h3_48gb/engines/sglang.py h3_48gb/queue.py h3_48gb/worker.py h3_48gb/web.py tests/_fake_sglang.py tests/test_sglang_adapter.py tests/test_sglang_resume.py
git commit -m "feat(sglang): адаптер в процессе воркера — один POST, engine_ref, возобновление после рестарта, отмена running"
```

---

## Task 7: `gpu-dispatcher`: свои движки, чужое, `generation.lock`, Qwen по кнопке, systemd

**Files:**
- Create: `tools/gpu-dispatcher/dispatcher.py`, `tools/gpu-dispatcher/h3-gpu-dispatcher.service`, `tools/gpu-dispatcher/README.md`
- Test: `tests/test_gpu_dispatcher.py` (новый)

**Interfaces:**
- Produces (HTTP на `127.0.0.1:8790`, JSON):
  - `GET /status` → `{"ok": true, "own": {name: {"pid", "variant", "started_at", "log", "ready"}}, "foreign": [{"pid", "name", "memory_mb", "first_seen"}], "qwen": {"running", "unloaded_by_us"}, "lock": {"held_by_us", "path"}, "gpu": {"temperature_c", "memory_used_mb", "memory_total_mb"}, "server_outputs_bytes"}`
  - `POST /acquire {"engine": "h3"|"ltx"}` → `{"ok": true, "state": "ready"|"starting"|"wait"|"wait_qwen"|"failed", "engine", "reason"?, "foreign"?, "log"?}`; неизвестный движок → `400 {"ok": false, "error": {"code": "unknown_engine", ...}}`
  - `POST /release` → `{"ok": true, "stopped": [names]}`
  - `POST /qwen/unload` → `{"ok", "was_running", "exit_code"?}`
  - `POST /qwen/restore` → `{"ok": true, "state": "starting"}` или `409 {"ok": false, "error": {"code": "qwen_was_not_running", "message": "Qwen не был запущен до выгрузки"}}`
- Produces (Python, для тестов): `parse_compute_apps(text) -> list[dict]`, `parse_gpu_stats(text) -> dict`, `EngineSpec`, `engine_specs() -> dict[str, EngineSpec]`, `GenerationLock(path)`, `Host`, `Dispatcher(*, host, specs, state_path, lock_path, server_outputs)`, `make_server(dispatcher, host="127.0.0.1", port=8790)`.
- Команды запуска — как `h3-bench/pipeline.sh` (`h3_start`: `VARIANT=ref2va TE=<...nvfp4_awq...> ./serve.sh`; `comfy_start`: `.venv/bin/python main.py --port 8188 --output-directory $COMFY_OUTPUT_DIR --listen 127.0.0.1 --fast-disk --disable-auto-launch` из `ComfyUI/`). Остановка — **не** `pkill -f` (убил бы чужой ComfyUI/H3), а SIGTERM группе своего процесса, 120 с, SIGKILL группе.
- Владение: pid и pgid в `state.json`; «своё» = pid из `state.json` **и** `/proc/<pid>/cmdline` содержит **все** маркеры движка: для H3 — `sglang`, ` serve `, `--model-variant ref2va`, `--port 30020` (`serve.sh:12` делает `exec sglang serve …`, поэтому pid из `Popen` и есть sglang; заодно это проверка варианта), для ComfyUI — `main.py`, `--port 8188`. Переиспользованный pid и чужой H3 с другим вариантом своими не считаются.
- Блокировки: длинные операции (acquire/release/Qwen, ожидание до 120 с, `qwen.sh stop`) идут под `_ops`; `self.state` защищён коротким `_state`, который не держится во время kill/sleep/подпроцесса; `/status` берёт только `_state` и отвечает во время освобождения.
- Чужие процессы: диспетчер помнит, когда впервые увидел каждый чужой pid (`first_seen`, в памяти), и отдаёт это в `foreign` — плашка показывает «уже N мин».
- `generation.lock`: `flock(LOCK_EX|LOCK_NB)`; fd передаётся порождённому движку (`pass_fds`), поэтому замок живёт, пока жив свой движок, даже если диспетчер перезапустили; снимается ядром со смертью держателя.

- [ ] **Step 1: Падающие тесты**

`tests/test_gpu_dispatcher.py`:
```python
"""gpu-dispatcher (spec §3.4) against a fake host: never touches a foreign pid, owns only what it
started, survives its own restart, honours generation.lock (held by a separate process here --
flock is only honest across processes), and touches Qwen only on request."""
import importlib.util
import json
import signal
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

from test_queue import _external_lock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gpu_dispatcher",
                                               ROOT / "tools" / "gpu-dispatcher" / "dispatcher.py")
gd = importlib.util.module_from_spec(_spec)
sys.modules["gpu_dispatcher"] = gd   # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(gd)

H3_READY = "http://127.0.0.1:30020/v1/models"
LTX_READY = "http://127.0.0.1:8188/system_stats"


class FakeHost:
    def __init__(self):
        self.apps, self.ok_urls, self.spawned, self.killed, self.qwen_calls = [], set(), [], [], []
        self.cmdlines, self.pgids, self.alive_groups = {}, {}, set()
        self.t = 1000.0
        self.next_pid = 4242
        self.die_on_term = True
        self.stats = {"temperature_c": 44, "memory_used_mb": 15, "memory_total_mb": 65536}
        self.exec_cmdlines = {"h3": "/home/alex/Projects/h3-lab/.venv312/bin/python3 /home/alex/Projects/h3-lab/.venv312/bin/sglang serve --model-type diffusion --model-path /home/alex/Models/Video/MiniMax-H3/model-package --model-id minimax-h3 --model-variant ref2va --num-gpus 1 --host 127.0.0.1 --port 30020",
                              "ltx": "/home/alex/Projects/comfy/.venv/bin/python main.py --port 8188 --output-directory /home/alex/Outputs/comfy/output --listen 127.0.0.1"}

    def gpu_apps(self):
        return [dict(app) for app in self.apps]

    def gpu_stats(self):
        return dict(self.stats)

    def url_ok(self, url):
        return url in self.ok_urls

    def spawn(self, spec, log_path, pass_fds):
        pid = self.next_pid
        self.next_pid += 1
        self.spawned.append((spec.name, tuple(spec.cmd), str(spec.cwd), dict(spec.env),
                             str(log_path), len(pass_fds)))
        # serve.sh execs sglang: by the time anyone looks, the pid's cmdline is sglang's own
        self.cmdlines[pid] = self.exec_cmdlines[spec.name]
        self.pgids[pid] = pid
        self.alive_groups.add(pid)
        return pid, pid

    def cmdline(self, pid):
        return self.cmdlines.get(pid)

    def pgid_of(self, pid):
        return self.pgids.get(pid)

    def killpg(self, pgid, sig):
        self.killed.append((pgid, sig))
        if sig == signal.SIGKILL or self.die_on_term:
            self.alive_groups.discard(pgid)
            for pid in [p for p, g in self.pgids.items() if g == pgid]:
                self.cmdlines.pop(pid, None)

    def group_alive(self, pgid):
        return pgid in self.alive_groups

    def run_qwen(self, action):
        self.qwen_calls.append(action)
        return 0

    def start_qwen(self):
        self.qwen_calls.append("start-shared32")

    def dir_size(self, path):
        return 123

    def monotonic(self):
        return self.t

    def wall(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


@pytest.fixture
def host():
    return FakeHost()


def _dispatcher(tmp_path, host):
    return gd.Dispatcher(host=host, specs=gd.engine_specs(), state_path=tmp_path / "state.json",
                         lock_path=tmp_path / "generation.lock", server_outputs=tmp_path / "so")


def _lock_is_free(path) -> bool:
    script = ("import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
              "try:\n    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n    print('free')\n"
              "except BlockingIOError:\n    print('busy')\n")
    out = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True)
    return out.stdout.strip() == "free"


def test_parse_nvidia_smi_outputs():
    assert gd.parse_compute_apps("1234, /usr/bin/python3, 40000\n777, sglang::scheduler, 512\n") == [
        {"pid": 1234, "name": "/usr/bin/python3", "memory_mb": 40000},
        {"pid": 777, "name": "sglang::scheduler", "memory_mb": 512}]
    assert gd.parse_compute_apps("No running processes found\n") == []
    assert gd.parse_gpu_stats("44, 15, 65536\n") == \
        {"temperature_c": 44, "memory_used_mb": 15, "memory_total_mb": 65536}


def test_acquire_on_a_free_card_starts_our_h3_with_the_pipeline_command(tmp_path, host):
    d = _dispatcher(tmp_path, host)
    answer = d.acquire("h3")
    spec = gd.engine_specs()["h3"]
    assert answer == {"ok": True, "state": "starting", "engine": "h3", "log": host.spawned[0][4]}
    assert host.spawned[0][:4] == (
        "h3", ("bash", "/home/alex/Projects/h3-bench/serve.sh"), "/home/alex/Projects/h3-bench",
        {"VARIANT": "ref2va", "TE": "/home/alex/Models/Video/MiniMax-H3/runtime-components/"
                                     "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"})
    assert host.spawned[0][5] == 1            # the lock fd is handed to the engine
    assert not _lock_is_free(tmp_path / "generation.lock")
    host.ok_urls.add(H3_READY)
    assert d.acquire("h3") == {"ok": True, "state": "ready", "engine": "h3"}


def test_qwen_holding_the_card_is_wait_qwen_and_nothing_is_touched(tmp_path, host):
    host.ok_urls.add(gd.QWEN_HEALTH)
    host.apps = [{"pid": 900, "name": "python3", "memory_mb": 59000}]
    answer = _dispatcher(tmp_path, host).acquire("h3")
    assert (answer["state"], answer["reason"]) == ("wait_qwen", "Qwen держит карту")
    assert (host.spawned, host.killed, host.qwen_calls) == ([], [], [])


def test_a_foreign_gpu_process_means_wait_and_is_named(tmp_path, host):
    host.apps = [{"pid": 777, "name": "comfy-python", "memory_mb": 30000}]
    host.pgids[777] = 777
    answer = _dispatcher(tmp_path, host).acquire("ltx")
    assert answer == {"ok": True, "state": "wait", "engine": "ltx",
                      "reason": "GPU занята: comfy-python (pid 777, 30000 МБ)",
                      "foreign": [{"pid": 777, "name": "comfy-python", "memory_mb": 30000,
                                   "first_seen": 1000.0}]}
    assert (host.spawned, host.killed) == ([], [])


def test_first_seen_of_a_foreign_process_is_kept_across_calls(tmp_path, host):
    host.apps = [{"pid": 777, "name": "comfy-python", "memory_mb": 30000}]
    host.pgids[777] = 777
    d = _dispatcher(tmp_path, host)
    d.acquire("ltx")
    host.t += 600
    assert d.status()["foreign"] == [{"pid": 777, "name": "comfy-python", "memory_mb": 30000,
                                      "first_seen": 1000.0}]


def test_an_h3_with_another_variant_at_our_pid_is_not_ours(tmp_path, host):
    _dispatcher(tmp_path, host).acquire("h3")
    host.cmdlines[4242] = host.exec_cmdlines["h3"].replace("ref2va", "fl2va")
    reborn = _dispatcher(tmp_path, host)
    assert reborn.release() == {"ok": True, "stopped": []}
    assert host.killed == []


def test_status_answers_while_a_release_is_waiting_for_a_group_to_die(tmp_path, host):
    import time as _time
    d = _dispatcher(tmp_path, host)
    d.acquire("h3")
    entered, finish = threading.Event(), threading.Event()
    real_kill = host.killpg

    def slow_kill(pgid, sig):
        entered.set()
        finish.wait(5)
        real_kill(pgid, sig)

    host.killpg = slow_kill
    worker = threading.Thread(target=d.release)
    worker.start()
    assert entered.wait(5)
    started = _time.monotonic()
    status = d.status()
    assert _time.monotonic() - started < 1.0
    assert status["own"]["h3"]["pid"] == 4242
    finish.set()
    worker.join(5)


def test_a_foreign_h3_on_its_port_is_not_ours(tmp_path, host):
    host.ok_urls.add(H3_READY)
    answer = _dispatcher(tmp_path, host).acquire("h3")
    assert (answer["state"], answer["reason"]) == ("wait", "чужой H3 на :30020")
    assert host.spawned == []


def test_a_busy_generation_lock_means_wait(tmp_path, host):
    with _external_lock(tmp_path, "LOCK_EX", name="generation.lock"):
        answer = _dispatcher(tmp_path, host).acquire("h3")
    assert (answer["state"], answer["reason"]) == ("wait", "generation.lock занят")
    assert host.spawned == []


def test_release_kills_only_our_exact_group(tmp_path, host):
    d = _dispatcher(tmp_path, host)
    d.acquire("h3")
    host.apps = [{"pid": 4242, "name": "sglang", "memory_mb": 47000},
                 {"pid": 777, "name": "comfy-python", "memory_mb": 3000}]
    host.pgids[777] = 777
    assert d.release() == {"ok": True, "stopped": ["h3"]}
    assert host.killed == [(4242, signal.SIGTERM)]
    assert all(pgid != 777 for pgid, _ in host.killed)
    assert _lock_is_free(tmp_path / "generation.lock")


def test_a_group_that_ignores_sigterm_gets_sigkill_after_120_s(tmp_path, host):
    d = _dispatcher(tmp_path, host)
    d.acquire("h3")
    host.die_on_term = False
    start = host.t
    d.release()
    assert host.killed == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert host.t - start >= 120


def test_acquiring_ltx_stops_our_h3_first(tmp_path, host):
    d = _dispatcher(tmp_path, host)
    d.acquire("h3")
    host.apps = [{"pid": 4242, "name": "sglang", "memory_mb": 47000}]
    answer = d.acquire("ltx")
    assert answer["state"] == "starting"
    assert host.killed == [(4242, signal.SIGTERM)]
    assert [s[0] for s in host.spawned] == ["h3", "ltx"]


def test_an_engine_that_died_before_ready_is_failed_with_its_log(tmp_path, host):
    d = _dispatcher(tmp_path, host)
    log = d.acquire("h3")["log"]
    host.alive_groups.clear()
    host.cmdlines.clear()
    assert d.acquire("h3") == {"ok": True, "state": "failed", "engine": "h3", "log": log,
                               "reason": "движок не поднялся, смотрите лог"}


def test_an_engine_not_ready_in_time_is_killed_and_failed(tmp_path, host):
    d = _dispatcher(tmp_path, host)
    log = d.acquire("h3")["log"]
    host.t += 451
    assert d.acquire("h3") == {"ok": True, "state": "failed", "engine": "h3", "log": log,
                               "reason": "движок не поднялся за 450 с, смотрите лог"}
    assert host.killed[0] == (4242, signal.SIGTERM)


def test_restarted_dispatcher_still_owns_its_engine(tmp_path, host):
    """Review Focus 1."""
    _dispatcher(tmp_path, host).acquire("h3")
    host.ok_urls.add(H3_READY)
    reborn = _dispatcher(tmp_path, host)
    assert reborn.acquire("h3") == {"ok": True, "state": "ready", "engine": "h3"}
    assert len(host.spawned) == 1
    assert reborn.status()["own"]["h3"]["pid"] == 4242


def test_a_reused_pid_is_not_ours_and_is_never_killed(tmp_path, host):
    _dispatcher(tmp_path, host).acquire("h3")
    host.cmdlines[4242] = "vim notes.txt"
    reborn = _dispatcher(tmp_path, host)
    assert reborn.release() == {"ok": True, "stopped": []}
    assert host.killed == []


def test_qwen_unload_and_restore_only_on_request(tmp_path, host):
    d = _dispatcher(tmp_path, host)
    assert d.qwen_restore() == (409, {"ok": False, "error": {
        "code": "qwen_was_not_running", "message": "Qwen не был запущен до выгрузки"}})
    host.ok_urls.add(gd.QWEN_HEALTH)
    assert d.qwen_unload() == (200, {"ok": True, "was_running": True, "exit_code": 0})
    host.ok_urls.discard(gd.QWEN_HEALTH)
    d.acquire("h3")
    assert d.qwen_restore() == (200, {"ok": True, "state": "starting"})
    assert host.qwen_calls == ["stop", "start-shared32"]
    assert host.killed == [(4242, signal.SIGTERM)]       # our H3 released before Qwen returns
    assert d.status()["qwen"] == {"running": False, "unloaded_by_us": False}


def test_qwen_unload_when_it_is_not_running_does_nothing(tmp_path, host):
    assert _dispatcher(tmp_path, host).qwen_unload() == (200, {"ok": True, "was_running": False})
    assert host.qwen_calls == []


def test_status_shape(tmp_path, host):
    d = _dispatcher(tmp_path, host)
    d.acquire("h3")
    host.ok_urls.add(H3_READY)
    host.apps = [{"pid": 4242, "name": "sglang", "memory_mb": 47000}]
    status = d.status()
    assert status == {
        "ok": True,
        "own": {"h3": {"pid": 4242, "variant": "ref2va", "started_at": 1000.0,
                       "log": host.spawned[0][4], "ready": True}},
        "foreign": [], "qwen": {"running": False, "unloaded_by_us": False},
        "lock": {"held_by_us": True, "path": str(tmp_path / "generation.lock")},
        "gpu": {"temperature_c": 44, "memory_used_mb": 15, "memory_total_mb": 65536},
        "server_outputs_bytes": 123}


def test_http_layer(tmp_path, host):
    server = gd.make_server(_dispatcher(tmp_path, host), host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(path, body):
        request = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        assert post("/acquire", {"engine": "h3"})[1]["state"] == "starting"
        assert post("/acquire", {"engine": "sd"})[0] == 400
        assert post("/qwen/restore", {})[0] == 409
        with urllib.request.urlopen(base + "/status", timeout=5) as response:
            assert json.loads(response.read())["own"]["h3"]["pid"] == 4242
    finally:
        server.shutdown()
        server.server_close()


def test_systemd_unit_does_not_kill_spawned_engines_on_restart():
    unit = (ROOT / "tools" / "gpu-dispatcher" / "h3-gpu-dispatcher.service").read_text()
    lines = unit.splitlines()
    assert "KillMode=process" in lines
    assert "User=alex" in lines
    assert ("ExecStart=/usr/bin/python3 /home/alex/Projects/h3-panel/tools/gpu-dispatcher/"
            "dispatcher.py --host 127.0.0.1 --port 8790") in lines
```

- [ ] **Step 2: Красный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_gpu_dispatcher.py -q -p no:cacheprovider`
Expected: FAIL — `FileNotFoundError: .../tools/gpu-dispatcher/dispatcher.py`.

- [ ] **Step 3: Реализация `tools/gpu-dispatcher/dispatcher.py`**

```python
#!/usr/bin/env python3
"""gpu-dispatcher for the h3 panel on alex-neuro (spec §3.4). Stdlib only; runs on the host
under systemd, listens on 127.0.0.1:8790.

It is the only thing that starts or stops the panel's engines (H3 on sglang :30020, ComfyUI LTX
:8188). It starts them exactly as h3-bench/pipeline.sh does, but stops only what it started --
SIGTERM to its own process group, up to 120 s, then SIGKILL -- never `pkill -f`, which would take
down a foreign ComfyUI/H3. Everything else on the GPU is foreign: waited for, never touched.
Qwen is stopped and restarted only on an explicit request (a button press).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECTS = Path(os.environ.get("ALEX_PROJECTS_ROOT", "/home/alex/Projects"))
MODELS = Path(os.environ.get("ALEX_MODELS_ROOT", "/home/alex/Models"))
OUTPUTS = Path(os.environ.get("ALEX_OUTPUTS_ROOT", "/home/alex/Outputs"))
H3_BENCH = PROJECTS / "h3-bench"
COMFY = PROJECTS / "comfy"
QWEN_SH = PROJECTS / "qwen" / "qwen.sh"
GENERATION_LOCK = PROJECTS / "qwen-image21-lab" / "generation.lock"
SERVER_OUTPUTS = OUTPUTS / "h3-bench" / "server-outputs"
STATE_PATH = Path(os.environ.get(
    "H3_DISPATCHER_STATE", str(Path.home() / ".local/state/h3-gpu-dispatcher/state.json")))
QWEN_HEALTH = "http://127.0.0.1:8000/health"
STOP_GRACE_SECONDS = 120.0


@dataclass(frozen=True)
class EngineSpec:
    name: str
    cmd: tuple
    cwd: Path
    env: dict = field(default_factory=dict)
    ready_url: str = ""
    port: int = 0
    markers: tuple = ()
    log_dir: Path = Path(".")
    log_prefix: str = ""
    start_timeout: float = 600.0
    variant: str | None = None
    label: str = ""


def engine_specs() -> dict[str, EngineSpec]:
    te = MODELS / ("Video/MiniMax-H3/runtime-components/text_encoders/"
                   "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors")
    return {
        "h3": EngineSpec(
            name="h3", cmd=("bash", str(H3_BENCH / "serve.sh")), cwd=H3_BENCH,
            env={"VARIANT": "ref2va", "TE": str(te)},
            ready_url="http://127.0.0.1:30020/v1/models", port=30020,
            markers=("sglang", " serve ", "--model-variant ref2va", "--port 30020"),
            log_dir=H3_BENCH / "logs",
            log_prefix="serve-panel", start_timeout=450.0, variant="ref2va", label="H3"),
        "ltx": EngineSpec(
            name="ltx",
            cmd=(str(COMFY / ".venv/bin/python"), "main.py", "--port", "8188",
                 "--output-directory", str(OUTPUTS / "comfy/output"), "--listen", "127.0.0.1",
                 "--fast-disk", "--disable-auto-launch"),
            cwd=COMFY / "ComfyUI", ready_url="http://127.0.0.1:8188/system_stats", port=8188,
            markers=("main.py", "--port 8188"), log_dir=COMFY / "logs", log_prefix="comfy-panel",
            start_timeout=600.0, label="ComfyUI"),
    }


def parse_compute_apps(text: str) -> list[dict]:
    apps = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        apps.append({"pid": int(parts[0]), "name": parts[1], "memory_mb": int(float(parts[2]))})
    return apps


def parse_gpu_stats(text: str) -> dict:
    first = text.strip().splitlines()[0]
    temperature, used, total = (int(float(part.strip())) for part in first.split(","))
    return {"temperature_c": temperature, "memory_used_mb": used, "memory_total_mb": total}


class GenerationLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd: int | None = None

    @property
    def held(self) -> bool:
        return self.fd is not None

    def try_acquire(self) -> bool:
        if self.fd is not None:
            return True
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o664)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Host:
    """Everything that touches the real machine. Tests replace it whole."""

    def _run(self, *cmd) -> str:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout

    def gpu_apps(self) -> list[dict]:
        return parse_compute_apps(self._run("nvidia-smi", "--query-compute-apps=pid,process_name,"
                                            "used_memory", "--format=csv,noheader,nounits"))

    def gpu_stats(self) -> dict:
        return parse_gpu_stats(self._run("nvidia-smi", "--query-gpu=temperature.gpu,memory.used,"
                                         "memory.total", "--format=csv,noheader,nounits"))

    def url_ok(self, url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                return response.status == 200
        except Exception:  # noqa: BLE001 -- any failure is "not answering"
            return False

    def spawn(self, spec: EngineSpec, log_path: Path, pass_fds) -> tuple[int, int]:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(list(spec.cmd), cwd=spec.cwd, env={**os.environ, **spec.env},
                                    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True, pass_fds=tuple(pass_fds))
        return proc.pid, os.getpgid(proc.pid)

    def cmdline(self, pid: int) -> str | None:
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
        except OSError:
            return None

    def pgid_of(self, pid: int) -> int | None:
        try:
            return os.getpgid(pid)
        except ProcessLookupError:
            return None

    def killpg(self, pgid: int, sig: int) -> None:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass

    def group_alive(self, pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def run_qwen(self, action: str) -> int:
        return subprocess.run(["bash", str(QWEN_SH), action], timeout=300).returncode

    def start_qwen(self) -> None:
        subprocess.Popen(["bash", str(QWEN_SH), "start-shared32"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def dir_size(self, path: Path) -> int:
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total

    monotonic = staticmethod(time.monotonic)
    wall = staticmethod(time.time)
    sleep = staticmethod(time.sleep)


class Dispatcher:
    """Two locks, on purpose (review): `_ops` serialises the long operations -- acquire, release,
    Qwen -- which may sleep up to 120 s while a group dies or wait on `qwen.sh stop`; `_state`
    guards only reads and writes of `self.state` and is never held across a kill, a sleep or a
    subprocess. `/status` takes `_state` alone, so it answers while a release is in progress."""

    def __init__(self, *, host, specs: dict, state_path: Path, lock_path: Path,
                 server_outputs: Path):
        self.host = host
        self.specs = specs
        self.state_path = Path(state_path)
        self.lock = GenerationLock(lock_path)
        self.server_outputs = Path(server_outputs)
        self._ops = threading.Lock()
        self._state = threading.Lock()
        self._first_seen: dict[int, float] = {}
        self.state = self._load_state()

    # -- state ------------------------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        state.setdefault("engines", {})
        state.setdefault("qwen_was_running", False)
        return state

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _snapshot(self) -> dict:
        with self._state:
            return json.loads(json.dumps(self.state))

    def _update(self, mutate) -> None:
        with self._state:
            mutate(self.state)
            self._save_state()

    def _alive(self, name: str, record: dict) -> bool:
        """Ours = the pid recorded in state.json AND its /proc cmdline carries every marker of
        the engine. serve.sh `exec`s `sglang serve ...` (serve.sh:12), so the pid Popen returned
        *is* sglang; requiring `--model-variant ref2va --port 30020` also proves the variant."""
        command = self.host.cmdline(record["pid"])
        return bool(command) and all(marker in command for marker in self.specs[name].markers)

    def _own_records(self, state: dict) -> dict:
        return {name: record for name, record in state["engines"].items()
                if self._alive(name, record)}

    def _foreign(self, own: dict) -> list[dict]:
        own_pgids = {record["pgid"] for record in own.values()}
        foreign = [app for app in self.host.gpu_apps()
                   if self.host.pgid_of(app["pid"]) not in own_pgids]
        now = self.host.wall()
        seen = {app["pid"] for app in foreign}
        for pid in list(self._first_seen):
            if pid not in seen:
                del self._first_seen[pid]
        return [{**app, "first_seen": self._first_seen.setdefault(app["pid"], now)}
                for app in foreign]

    def _stop(self, name: str, record: dict) -> None:
        """Called with `_ops` held and `_state` free: kill our group, wait, then forget it."""
        if self._alive(name, record):
            pgid = record["pgid"]
            self.host.killpg(pgid, signal.SIGTERM)
            deadline = self.host.monotonic() + STOP_GRACE_SECONDS
            while self.host.group_alive(pgid) and self.host.monotonic() < deadline:
                self.host.sleep(1.0)
            if self.host.group_alive(pgid):
                self.host.killpg(pgid, signal.SIGKILL)
        self._update(lambda state: state["engines"].pop(name, None))

    def _release_all(self) -> list[str]:
        state = self._snapshot()
        stopped = sorted(self._own_records(state))
        for name, record in state["engines"].items():
            self._stop(name, record)
        self.lock.release()
        return stopped

    # -- handles ----------------------------------------------------------------------------
    def status(self) -> dict:
        state = self._snapshot()
        own = {}
        for name, record in self._own_records(state).items():
            own[name] = {"pid": record["pid"], "variant": record.get("variant"),
                         "started_at": record["started_at"], "log": record["log"],
                         "ready": self.host.url_ok(self.specs[name].ready_url)}
        return {"ok": True, "own": own, "foreign": self._foreign(self._own_records(state)),
                "qwen": {"running": self.host.url_ok(QWEN_HEALTH),
                         "unloaded_by_us": bool(state["qwen_was_running"])},
                "lock": {"held_by_us": bool(own) or self.lock.held, "path": str(self.lock.path)},
                "gpu": self.host.gpu_stats(),
                "server_outputs_bytes": self.host.dir_size(self.server_outputs)}

    def acquire(self, engine: str) -> dict:
        with self._ops:
            spec = self.specs[engine]
            state = self._snapshot()
            record = state["engines"].get(engine)
            if record and not self._alive(engine, record):
                self._update(lambda st: st["engines"].pop(engine, None))
                if not record.get("ready"):
                    return {"ok": True, "state": "failed", "engine": engine, "log": record["log"],
                            "reason": "движок не поднялся, смотрите лог"}
                record = None
            if record:
                if self.host.url_ok(spec.ready_url):
                    self._update(lambda st: st["engines"][engine].__setitem__("ready", True))
                    return {"ok": True, "state": "ready", "engine": engine}
                if self.host.wall() - record["started_at"] > spec.start_timeout:
                    self._stop(engine, record)
                    return {"ok": True, "state": "failed", "engine": engine, "log": record["log"],
                            "reason": f"движок не поднялся за {spec.start_timeout:g} с, "
                                      f"смотрите лог"}
                return {"ok": True, "state": "starting", "engine": engine, "log": record["log"]}
            own = self._own_records(state)
            foreign = self._foreign(own)
            if self.host.url_ok(QWEN_HEALTH):
                return {"ok": True, "state": "wait_qwen", "engine": engine,
                        "reason": "Qwen держит карту", "foreign": foreign}
            if foreign:
                names = ", ".join(f"{a['name']} (pid {a['pid']}, {a['memory_mb']} МБ)"
                                  for a in foreign)
                return {"ok": True, "state": "wait", "engine": engine,
                        "reason": f"GPU занята: {names}", "foreign": foreign}
            if self.host.url_ok(spec.ready_url):
                return {"ok": True, "state": "wait", "engine": engine,
                        "reason": f"чужой {spec.label} на :{spec.port}", "foreign": []}
            for other, other_record in own.items():
                if other != engine:
                    self._stop(other, other_record)
            if not self.lock.try_acquire():
                return {"ok": True, "state": "wait", "engine": engine,
                        "reason": "generation.lock занят", "foreign": []}
            stamp = datetime.fromtimestamp(self.host.wall()).strftime("%Y%m%d-%H%M%S")
            log_path = spec.log_dir / f"{spec.log_prefix}-{stamp}.log"
            pid, pgid = self.host.spawn(spec, log_path, pass_fds=(self.lock.fd,))
            new = {"pid": pid, "pgid": pgid, "variant": spec.variant,
                   "started_at": self.host.wall(), "log": str(log_path), "ready": False}
            self._update(lambda st: st["engines"].__setitem__(engine, new))
            return {"ok": True, "state": "starting", "engine": engine, "log": str(log_path)}

    def release(self) -> dict:
        with self._ops:
            return {"ok": True, "stopped": self._release_all()}

    def qwen_unload(self) -> tuple[int, dict]:
        with self._ops:
            if not self.host.url_ok(QWEN_HEALTH):
                return 200, {"ok": True, "was_running": False}
            self._update(lambda st: st.__setitem__("qwen_was_running", True))
            code = self.host.run_qwen("stop")
            return 200, {"ok": code == 0, "was_running": True, "exit_code": code}

    def qwen_restore(self) -> tuple[int, dict]:
        with self._ops:
            if not self._snapshot()["qwen_was_running"]:
                return 409, {"ok": False, "error": {"code": "qwen_was_not_running",
                                                    "message": "Qwen не был запущен до выгрузки"}}
            self._release_all()
            self.host.start_qwen()
            self._update(lambda st: st.__setitem__("qwen_was_running", False))
            return 200, {"ok": True, "state": "starting"}


def make_server(dispatcher: Dispatcher, host: str = "127.0.0.1", port: int = 8790):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status: int, body: dict) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            data = json.loads(self.rfile.read(length))
            return data if isinstance(data, dict) else {}

        def do_GET(self):
            if self.path == "/status":
                return self._send(200, dispatcher.status())
            self._send(404, {"ok": False, "error": {"code": "not_found", "message": self.path}})

        def do_POST(self):
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"ok": False, "error": {"code": "bad_json",
                                                               "message": "тело не JSON"}})
            if self.path == "/acquire":
                engine = body.get("engine")
                if engine not in dispatcher.specs:
                    return self._send(400, {"ok": False, "error": {
                        "code": "unknown_engine", "message": f"движок {engine!r}: h3 или ltx"}})
                return self._send(200, dispatcher.acquire(engine))
            if self.path == "/release":
                return self._send(200, dispatcher.release())
            if self.path == "/qwen/unload":
                return self._send(*dispatcher.qwen_unload())
            if self.path == "/qwen/restore":
                return self._send(*dispatcher.qwen_restore())
            self._send(404, {"ok": False, "error": {"code": "not_found", "message": self.path}})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args(argv)
    dispatcher = Dispatcher(host=Host(), specs=engine_specs(), state_path=STATE_PATH,
                            lock_path=GENERATION_LOCK, server_outputs=SERVER_OUTPUTS)
    server = make_server(dispatcher, args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`tools/gpu-dispatcher/h3-gpu-dispatcher.service`:
```ini
[Unit]
Description=h3 panel GPU dispatcher (spec §3.4)
After=network.target docker.service

[Service]
Type=simple
User=alex
Group=alex
ExecStart=/usr/bin/python3 /home/alex/Projects/h3-panel/tools/gpu-dispatcher/dispatcher.py --host 127.0.0.1 --port 8790
Restart=on-failure
RestartSec=5
# The engines it starts live in their own sessions; a dispatcher restart must not take them down
# with its cgroup -- state.json lets the new process recognise them as its own.
KillMode=process

[Install]
WantedBy=multi-user.target
```

`tools/gpu-dispatcher/README.md` — 15–25 строк: что делает, установка (`sudo cp h3-gpu-dispatcher.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now h3-gpu-dispatcher`), где `state.json`, ручки, правило «чужое не гасится».

- [ ] **Step 4: Зелёный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_gpu_dispatcher.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Мутации**

(а) В `_foreign` убрать фильтр по своим pgid (`return self.host.gpu_apps()`) → `test_acquiring_ltx_stops_our_h3_first` FAIL (свой H3 сочтён чужим → `wait`). (б) В `_alive` вернуть `True` без проверки маркера → `test_a_reused_pid_is_not_ours_and_is_never_killed` FAIL (`killed == [(4242, SIGTERM)]`); заменить `all(...)` на `any(...)` → `test_an_h3_with_another_variant_at_our_pid_is_not_ours` FAIL. (б2) В `_stop` обернуть kill/ожидание в `with self._state:` → `test_status_answers_while_a_release_...` FAIL (`status()` ждёт `_state` дольше 1 с). (в) В `_release_all` гасить `host.gpu_apps()` целиком → `test_release_kills_only_our_exact_group` FAIL (в `killed` появляется 777). (г) Убрать `pass_fds=(self.lock.fd,)` → `test_acquire_on_a_free_card_...` FAIL (`0 == 1`). (д) Убрать проверку `qwen_was_running` в `qwen_restore` → `test_qwen_unload_and_restore_only_on_request` FAIL. (е) Перенести `if not self.lock.try_acquire()` выше цикла остановки своих движков → `test_after_a_restart_switching_engines_frees_the_inherited_lock_first` (ниже) FAIL `'wait' == 'starting'`. Ошибки — в отчёт.

Тест к мутации (е) — дописать в `tests/test_gpu_dispatcher.py` на шаге 1:
```python
_HOLD = ("import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\nsys.stdin.readline()\n")


def test_after_a_restart_switching_engines_frees_the_inherited_lock_first(tmp_path, host):
    """After a dispatcher restart the lock is held only by our own H3 (it inherited the fd).
    Switching to ltx must stop our H3 *before* trying the lock, or the panel waits on itself."""
    first = _dispatcher(tmp_path, host)
    first.acquire("h3")
    first.lock.release()                    # the old dispatcher process is gone ...
    holder = subprocess.Popen([sys.executable, "-c", _HOLD, str(tmp_path / "generation.lock")],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"   # ... "our H3" still holds the lock
    real_kill = host.killpg

    def kill(pgid, sig):
        real_kill(pgid, sig)
        if pgid == 4242 and holder.poll() is None:
            holder.stdin.write("\n")
            holder.stdin.flush()
            holder.wait(timeout=5)

    host.killpg = kill
    try:
        reborn = _dispatcher(tmp_path, host)
        assert reborn.acquire("ltx")["state"] == "starting"
        assert host.killed == [(4242, signal.SIGTERM)]
    finally:
        if holder.poll() is None:
            holder.kill()
```

- [ ] **Step 6: Полный прогон и commit**

```bash
env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest -q -p no:cacheprovider
git add tools/gpu-dispatcher tests/test_gpu_dispatcher.py
git commit -m "feat(dispatcher): gpu-dispatcher — свои движки по pid/cmdline, чужое не гасится, generation.lock, Qwen по кнопке"
```

---

## Task 8: Воркер и карта: acquire/wait/wait_qwen, термозащита, idle-release 15 мин, «Освободить карту» + пауза, GPU-API панели

**Files:**
- Create: `h3_48gb/engines/dispatcher_client.py`, `tests/_fake_dispatcher.py`
- Modify: `h3_48gb/worker.py` — `make_gpu_gate`, `GpuEngineFailed`, `_IdleRelease`, `_wait_for_engine_start_to_settle`; `_run_sglang_generate_job` (задача 6) получает настоящий gate и освобождение по `released_by_user`; `main_loop` — idle-release; `run_job` — ожидание перед сборкой при sglang
- Modify: `h3_48gb/queue.py` — `has_active_jobs(root) -> bool`
- Modify: `h3_48gb/web.py` — `GET /api/gpu`, `POST /api/gpu/release`, `POST /api/qwen/unload`, `POST /api/qwen/restore`; `ERROR_STATUS`
- Modify: `h3_48gb/cli.py` (`ERROR_CODES`)
- Test: `tests/test_gpu_gate.py` (новый), `tests/test_gpu_web.py` (новый)

**Interfaces:**
- Consumes: HTTP-контракт диспетчера (задача 7); `sglang.run_generate(..., gate=...)`, `sglang._sleep_unless_cancelled` (задача 6); `q.set_running_fields/request_cancel/cancel_reason` (задача 6).
- Produces:
  - `dispatcher_client.DEFAULT_URL = "http://127.0.0.1:8790"`, `class DispatcherUnavailable(Exception)`, `class DispatcherClient(base_url=None, timeout=30.0)` с `status() -> dict`, `acquire(engine) -> dict`, `release() -> dict`, `qwen_unload() -> tuple[int, dict]`, `qwen_restore() -> tuple[int, dict]`; `base_url` по умолчанию из `H3_DISPATCHER_URL`
  - `worker.ACQUIRE_RETRY_SECONDS = 30.0`, `worker.STARTING_POLL_SECONDS = 5.0`, `worker.HOT_C = 80`, `worker.COOL_C = 72`
  - `worker.make_gpu_gate(root, engine_name: str, *, client, sleep=time.sleep) -> Callable[[Job], str | None]`; `class worker.GpuEngineFailed(Exception)` (`.reason`, `.log`)
  - `class worker._IdleRelease(minutes: float, client, clock=time.monotonic)` с `tick(root) -> None`
  - `q.has_active_jobs(root) -> bool`
  - Web: `GET /api/gpu` → `{"ok", "dispatcher": <status|null>, "dispatcher_error": <str|null>, "queue": {"pending": int, "paused": bool, "running": {"id", "kind", "note", "started_at", "wait_reason"} | null}}`; `POST /api/gpu/release {"confirm"?: bool}`; `POST /api/qwen/unload`; `POST /api/qwen/restore`
  - коды `engine_not_sglang` (409), `dispatcher_unavailable` (502), `release_needs_confirm` (409), `qwen_was_not_running` (409), `queue_busy` (409)
  - «Освободить карту»: подтверждение и отмена — только для `generate`/`upscale`; если в работе сборка (ffmpeg, не GPU) — движки освобождаются сразу, сборка не трогается. «Вернуть Qwen» — только при пустой очереди.

- [ ] **Step 1: Фейковый диспетчер**

`tests/_fake_dispatcher.py`:
```python
"""A stand-in for tools/gpu-dispatcher's HTTP API (task 7's contract), scripted per test."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeDispatcher:
    def __init__(self, *, acquire=({"ok": True, "state": "ready", "engine": "h3"},),
                 temps=(44,), own=None, restore_status=200):
        self.calls: list[tuple[str, str, dict]] = []
        self._acquire = list(acquire)
        self._temps = list(temps)
        self.own = own if own is not None else {}
        self.restore_status = restore_status
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @staticmethod
    def _next(items):
        return items.pop(0) if len(items) > 1 else items[0]

    def status_body(self) -> dict:
        return {"ok": True, "own": self.own, "foreign": [],
                "qwen": {"running": False, "unloaded_by_us": False},
                "lock": {"held_by_us": bool(self.own), "path": "/x/generation.lock"},
                "gpu": {"temperature_c": self._next(self._temps), "memory_used_mb": 100,
                        "memory_total_mb": 65536},
                "server_outputs_bytes": 0}

    def _handler(fake):  # noqa: N805
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body):
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                fake.calls.append(("GET", self.path, {}))
                self._send(200, fake.status_body())

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                fake.calls.append(("POST", self.path, body))
                if self.path == "/acquire":
                    return self._send(200, fake._next(fake._acquire))
                if self.path == "/release":
                    return self._send(200, {"ok": True, "stopped": ["h3"]})
                if self.path == "/qwen/unload":
                    return self._send(200, {"ok": True, "was_running": True, "exit_code": 0})
                if self.path == "/qwen/restore":
                    if fake.restore_status == 409:
                        return self._send(409, {"ok": False, "error": {
                            "code": "qwen_was_not_running",
                            "message": "Qwen не был запущен до выгрузки"}})
                    return self._send(200, {"ok": True, "state": "starting"})
                self._send(404, {"ok": False})
        return Handler
```

- [ ] **Step 2: Падающие тесты ворот и простоя**

`tests/test_gpu_gate.py`:
```python
"""The worker's side of the GPU (spec §4.1.1-2, §3.4 /release, §3.3.14)."""
import pytest

from h3_48gb import queue as q
from h3_48gb import worker
from h3_48gb.engines import dispatcher_client as dc
from h3_48gb.engines import sglang as sg
from _fake_dispatcher import FakeDispatcher
from _fake_sglang import FakeSglang
from test_worker import _stop_after


@pytest.fixture
def running(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    root = q.layout(tmp_path / "queue")["root"]
    (tmp_path / "scenes").mkdir()
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n")
    args = ["generate", "p", "--width", "896", "--height", "512", "--duration", str(175 / 24),
            "--tag", "t", "--outdir", str(tmp_path / "scenes"), "--ref", str(ref)]
    q.submit(root, args, "", {"output_stem": str(tmp_path / "scenes" / "h3-t-896x512")}, {})
    return root, q.claim(root), tmp_path


def _recording_sleep(root, job_id, seen):
    def sleep(seconds):
        reason = [j for j in q.scan(root)[0] if j.id == job_id][0].wait_reason
        if not seen or seen[-1] != reason:
            seen.append(reason)
    return sleep


def test_gate_waits_for_foreign_then_qwen_then_returns_ready(running):
    root, job, _ = running
    fake = FakeDispatcher(acquire=(
        {"ok": True, "state": "wait", "engine": "h3", "reason": "GPU занята: comfy (pid 7, 3000 МБ)"},
        {"ok": True, "state": "wait_qwen", "engine": "h3", "reason": "Qwen держит карту"},
        {"ok": True, "state": "starting", "engine": "h3", "log": "/l.log"},
        {"ok": True, "state": "ready", "engine": "h3"}))
    seen = []
    try:
        gate = worker.make_gpu_gate(root, "h3", client=dc.DispatcherClient(fake.url),
                                    sleep=_recording_sleep(root, job.id, seen))
        assert gate(job) is None
    finally:
        fake.close()
    assert seen == ["ждём GPU: GPU занята: comfy (pid 7, 3000 МБ)",
                    "ждём GPU: Qwen держит карту — выгрузите Qwen в панели",
                    "ждём GPU: поднимается h3"]
    assert [j for j in q.scan(root)[0] if j.id == job.id][0].wait_reason is None
    assert [c[2] for c in fake.calls if c[1] == "/acquire"] == [{"engine": "h3"}] * 4


def test_retry_intervals_are_thirty_and_five_seconds(running):
    root, job, _ = running
    fake = FakeDispatcher(acquire=(
        {"ok": True, "state": "wait", "engine": "h3", "reason": "x"},
        {"ok": True, "state": "starting", "engine": "h3", "log": "/l"},
        {"ok": True, "state": "ready", "engine": "h3"}))
    sleeps = []
    try:
        gate = worker.make_gpu_gate(root, "h3", client=dc.DispatcherClient(fake.url),
                                    sleep=sleeps.append)
        assert gate(job) is None
    finally:
        fake.close()
    assert sleeps == [1.0] * 30 + [1.0] * 5


def test_a_hot_card_is_waited_down_to_72(running):
    root, job, _ = running
    fake = FakeDispatcher(temps=(81, 75, 72))
    seen = []
    try:
        gate = worker.make_gpu_gate(root, "h3", client=dc.DispatcherClient(fake.url),
                                    sleep=_recording_sleep(root, job.id, seen))
        assert gate(job) is None
    finally:
        fake.close()
    assert seen == ["остываем, 81 °C", "остываем, 75 °C"]


def test_cancel_while_waiting_returns_the_reason(running):
    root, job, _ = running
    fake = FakeDispatcher(acquire=({"ok": True, "state": "wait", "engine": "h3", "reason": "x"},))
    try:
        gate = worker.make_gpu_gate(root, "h3", client=dc.DispatcherClient(fake.url),
                                    sleep=lambda s: q.request_cancel(root, job.id, "cancelled_by_user"))
        assert gate(job) == "cancelled_by_user"
    finally:
        fake.close()


def test_an_engine_that_failed_to_start_fails_the_job_without_posting(running, monkeypatch):
    root, job, tmp_path = running
    disp = FakeDispatcher(acquire=({"ok": True, "state": "failed", "engine": "h3",
                                    "log": "/home/alex/Projects/h3-bench/logs/serve-panel-1.log",
                                    "reason": "движок не поднялся, смотрите лог"},))
    h3 = FakeSglang()
    monkeypatch.setenv("H3_DISPATCHER_URL", disp.url)
    monkeypatch.setenv("H3_SGLANG_URL", h3.url)
    try:
        code, log = worker._run_sglang_generate_job(root, tmp_path, job)
    finally:
        disp.close()
        h3.close()
    assert (code, log) == (1, "движок не поднялся: движок не поднялся, смотрите лог; "
                              "лог: /home/alex/Projects/h3-bench/logs/serve-panel-1.log\n")
    assert h3.posts == []


def test_released_by_user_releases_the_card_after_the_job_stops(running, monkeypatch):
    root, job, tmp_path = running
    disp = FakeDispatcher()
    h3 = FakeSglang(statuses=("queued",))
    monkeypatch.setenv("H3_DISPATCHER_URL", disp.url)
    monkeypatch.setenv("H3_SGLANG_URL", h3.url)

    def cancel_now(root_, job_id, seconds, sleep):
        q.request_cancel(root_, job_id, "released_by_user")
        return "released_by_user"

    monkeypatch.setattr(sg, "_sleep_unless_cancelled", cancel_now)
    try:
        code, _ = worker._run_sglang_generate_job(root, tmp_path, job)
    finally:
        disp.close()
        h3.close()
    assert code == 1
    assert [c[1] for c in disp.calls if c[0] == "POST"] == ["/acquire", "/release"]
    assert h3.deletes == ["vid-1"]


class _FakeClient:
    def __init__(self):
        self.released = 0

    def release(self):
        self.released += 1
        return {"ok": True, "stopped": ["h3"]}


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_idle_release_fires_once_after_15_minutes_of_an_empty_queue(tmp_path):
    root = q.layout(tmp_path / "queue")["root"]
    client, clock = _FakeClient(), _Clock()
    idle = worker._IdleRelease(15, client, clock=clock)
    idle.tick(root)
    clock.t = 899.0
    idle.tick(root)
    assert client.released == 0
    clock.t = 900.0
    idle.tick(root)
    clock.t = 5000.0
    idle.tick(root)
    assert client.released == 1


def test_idle_countdown_restarts_while_anything_is_queued(tmp_path):
    root = q.layout(tmp_path / "queue")["root"]
    client, clock = _FakeClient(), _Clock()
    idle = worker._IdleRelease(15, client, clock=clock)
    idle.tick(root)
    q.submit(root, ["generate", "--tag", "a"], "", {"output_stem": str(tmp_path / "h3-a")}, {})
    clock.t = 1000.0
    idle.tick(root)                                   # pending job: the countdown is cleared
    assert client.released == 0
    for job in q.scan(root)[0]:
        q.cancel(root, job.id)
    clock.t = 1001.0
    idle.tick(root)
    clock.t = 1001.0 + 899.0
    idle.tick(root)
    assert client.released == 0
    clock.t = 1001.0 + 900.0
    idle.tick(root)
    assert client.released == 1


def test_main_loop_releases_an_idle_card_on_sglang(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    monkeypatch.setenv("H3_IDLE_RELEASE_MIN", "0")
    disp = FakeDispatcher()
    monkeypatch.setenv("H3_DISPATCHER_URL", disp.url)
    root = q.layout(tmp_path / "queue")["root"]
    try:
        worker.main_loop(root, poll=0.01, stop=_stop_after(0.5), outdir=tmp_path)
    finally:
        disp.close()
    assert [c[1] for c in disp.calls] == ["/release"]


def test_main_loop_never_talks_to_a_dispatcher_on_mlx(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_IDLE_RELEASE_MIN", "0")
    disp = FakeDispatcher()
    monkeypatch.setenv("H3_DISPATCHER_URL", disp.url)
    root = q.layout(tmp_path / "queue")["root"]
    try:
        worker.main_loop(root, poll=0.01, stop=_stop_after(0.3), outdir=tmp_path)
    finally:
        disp.close()
    assert disp.calls == []


def test_assembly_waits_while_our_engine_is_still_starting(tmp_path):
    class _Status:
        def __init__(self):
            self.answers = [{"own": {"h3": {"ready": False}}}, {"own": {"h3": {"ready": False}}},
                            {"own": {}}]

        def status(self):
            return self.answers.pop(0)

    sleeps = []
    worker._wait_for_engine_start_to_settle(_Status(), sleep=sleeps.append)
    assert sleeps == [worker.ACQUIRE_RETRY_SECONDS] * 2
```

- [ ] **Step 3: Падающие тесты веба**

`tests/test_gpu_web.py`:
```python
"""The panel's GPU routes (spec §3.3.13-14, §3.4): status for the banner, «Освободить карту»
with confirmation and pause, Qwen buttons -- all thin proxies to the dispatcher."""
import pytest

from h3_48gb import queue as q
from _fake_dispatcher import FakeDispatcher
from test_web import _call, _serve


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    disp = FakeDispatcher(own={"h3": {"pid": 4242, "variant": "ref2va", "started_at": 1.0,
                                      "log": "/l", "ready": True}})
    monkeypatch.setenv("H3_DISPATCHER_URL", disp.url)
    root = q.layout(tmp_path / "queue")["root"]
    live = _serve(root, tmp_path)
    yield live, disp, root, tmp_path
    live.httpd.shutdown()
    live.httpd.server_close()
    disp.close()


def _queue_running(root, tmp_path, note="project scene P #3"):
    q.submit(root, ["generate", "--tag", "a"], note, {"output_stem": str(tmp_path / "h3-a")}, {})
    job = q.claim(root)
    q.set_running_fields(root, job.id, wait_reason="ждём GPU: generation.lock занят")
    return job


def test_gpu_state_combines_dispatcher_and_queue(setup):
    live, disp, root, tmp_path = setup
    job = _queue_running(root, tmp_path)
    status, body = _call(live, "GET", "/api/gpu")
    assert status == 200
    assert body == {"ok": True, "dispatcher": disp.status_body(), "dispatcher_error": None,
                    "queue": {"pending": 0, "paused": True,
                              "running": {"id": job.id, "kind": "generate",
                                          "note": "project scene P #3",
                                          "started_at": job.started_at,
                                          "wait_reason": "ждём GPU: generation.lock занят"}}}


def test_gpu_state_reports_an_unreachable_dispatcher(setup, monkeypatch):
    live, disp, root, tmp_path = setup
    monkeypatch.setenv("H3_DISPATCHER_URL", "http://127.0.0.1:9")
    status, body = _call(live, "GET", "/api/gpu")
    assert (status, body["dispatcher"]) == (200, None)
    assert body["dispatcher_error"].startswith("GET /status: ")


def test_release_with_an_empty_queue_releases_now_and_pauses(setup):
    live, disp, root, tmp_path = setup
    q.set_paused(root, False)
    status, body = _call(live, "POST", "/api/gpu/release", {})
    assert (status, body) == (200, {"ok": True, "paused": True, "releasing": False,
                                    "released": ["h3"]})
    assert q.is_paused(root) is True
    assert [c[1] for c in disp.calls] == ["/release"]


def test_release_while_rendering_needs_confirmation(setup):
    live, disp, root, tmp_path = setup
    job = _queue_running(root, tmp_path)
    status, body = _call(live, "POST", "/api/gpu/release", {})
    assert (status, body["error"]["code"], body["error"]["message"]) == (
        409, "release_needs_confirm",
        "H3 считает project scene P #3 — освободить карту? Сцена будет потеряна")
    assert q.cancel_reason(root, job.id) is None
    assert disp.calls == []


def test_confirmed_release_while_rendering_cancels_pauses_and_leaves_release_to_the_worker(setup):
    live, disp, root, tmp_path = setup
    job = _queue_running(root, tmp_path)
    q.set_paused(root, False)
    status, body = _call(live, "POST", "/api/gpu/release", {"confirm": True})
    assert (status, body) == (200, {"ok": True, "paused": True, "releasing": True, "job": job.id})
    assert q.cancel_reason(root, job.id) == "released_by_user"
    assert q.is_paused(root) is True
    assert disp.calls == []


def test_release_during_an_assembly_frees_the_card_now_and_leaves_the_assembly(setup):
    live, disp, root, tmp_path = setup
    q.submit(root, ["assemble", "--project", str(tmp_path / "p" / "project.json")], "assemble P",
             {"output_stem": str(tmp_path / "p" / "job-final")}, {}, kind=q.KIND_ASSEMBLE)
    job = q.claim(root)
    status, body = _call(live, "POST", "/api/gpu/release", {})
    assert (status, body) == (200, {"ok": True, "paused": True, "releasing": False,
                                    "released": ["h3"]})
    assert q.cancel_reason(root, job.id) is None
    assert [c[1] for c in disp.calls] == ["/release"]


def test_qwen_restore_is_refused_while_the_queue_is_busy(setup):
    live, disp, root, tmp_path = setup
    _queue_running(root, tmp_path)
    status, body = _call(live, "POST", "/api/qwen/restore", {})
    assert (status, body["error"]["code"], body["error"]["message"]) == \
        (409, "queue_busy", "вернуть Qwen можно после очереди: в ней ещё есть задачи")
    assert disp.calls == []


def test_qwen_buttons_proxy_and_map_the_refusal(setup):
    live, disp, root, tmp_path = setup
    assert _call(live, "POST", "/api/qwen/unload", {}) == \
        (200, {"ok": True, "was_running": True, "exit_code": 0})
    disp.restore_status = 409
    status, body = _call(live, "POST", "/api/qwen/restore", {})
    assert (status, body["error"]["code"], body["error"]["message"]) == \
        (409, "qwen_was_not_running", "Qwen не был запущен до выгрузки")


def test_gpu_routes_refuse_on_mlx(setup, monkeypatch):
    live, disp, root, tmp_path = setup
    monkeypatch.setenv("H3_ENGINE", "mlx")
    status, body = _call(live, "GET", "/api/gpu")
    assert (status, body["error"]["code"]) == (409, "engine_not_sglang")
```

- [ ] **Step 4: Красный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_gpu_gate.py tests/test_gpu_web.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'h3_48gb.engines.dispatcher_client'`.

- [ ] **Step 5: Клиент диспетчера**

`h3_48gb/engines/dispatcher_client.py`:
```python
"""The panel's client for tools/gpu-dispatcher (spec §3.4). The container has no nvidia-smi;
everything it knows about the card comes from here."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8790"


class DispatcherUnavailable(Exception):
    pass


class DispatcherClient:
    def __init__(self, base_url: str | None = None, timeout: float = 30.0):
        self.base_url = (base_url or os.environ.get("H3_DISPATCHER_URL") or DEFAULT_URL).rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, payload=None) -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            except ValueError:
                raise DispatcherUnavailable(f"{method} {path}: HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise DispatcherUnavailable(f"{method} {path}: {exc}") from None

    def status(self) -> dict:
        return self._call("GET", "/status")[1]

    def acquire(self, engine: str) -> dict:
        return self._call("POST", "/acquire", {"engine": engine})[1]

    def release(self) -> dict:
        return self._call("POST", "/release", {})[1]

    def qwen_unload(self) -> tuple[int, dict]:
        return self._call("POST", "/qwen/unload", {})

    def qwen_restore(self) -> tuple[int, dict]:
        return self._call("POST", "/qwen/restore", {})
```

- [ ] **Step 6: Воркер**

`queue.py`:
```python
def has_active_jobs(root) -> bool:
    """Anything in pending/ or running/ -- the idle-release countdown (spec §3.4) runs only while
    this is False. A directory listing, not a parse: cheap enough for every loop pass."""
    root = Path(root)
    return any(any((root / state).glob("*.json")) for state in ("pending", "running"))
```
`worker.py` (импорт `from h3_48gb.engines import dispatcher_client`; `import time` уже есть):
```python
ACQUIRE_RETRY_SECONDS = 30.0
STARTING_POLL_SECONDS = 5.0
HOT_C = 80
COOL_C = 72


class GpuEngineFailed(Exception):
    def __init__(self, reason: str, log: str | None):
        super().__init__(reason)
        self.reason = reason
        self.log = log


def make_gpu_gate(root, engine_name: str, *, client, sleep=time.sleep):
    """spec §4.1.1-2: ask the dispatcher for `engine_name` until it is ready, every 30 s (5 s while
    our own engine is starting), writing why into the job (`wait_reason`) for the page; no
    timeout -- only a cancel ends the wait. Once ready, a card at ≥ 80 °C is waited down to 72 °C.
    Returns None when the card is ours and cool, or the cancel reason."""
    from h3_48gb.engines import sglang as sglang_engine

    def wait(job, reason: str, seconds: float) -> str | None:
        q.set_running_fields(root, job.id, wait_reason=reason)
        return sglang_engine._sleep_unless_cancelled(root, job.id, seconds, sleep)

    def temperature() -> int | None:
        try:
            return client.status()["gpu"]["temperature_c"]
        except (dispatcher_client.DispatcherUnavailable, KeyError, TypeError):
            return None

    def gate(job) -> str | None:
        while True:
            cancelled = q.cancel_reason(root, job.id)
            if cancelled:
                return cancelled
            try:
                answer = client.acquire(engine_name)
            except dispatcher_client.DispatcherUnavailable as exc:
                cancelled = wait(job, f"ждём GPU: диспетчер недоступен ({exc})",
                                 ACQUIRE_RETRY_SECONDS)
                if cancelled:
                    return cancelled
                continue
            state = answer.get("state")
            if state == "failed":
                raise GpuEngineFailed(answer.get("reason") or "движок не поднялся",
                                      answer.get("log"))
            if state == "ready":
                temp = temperature()
                if temp is None or temp < HOT_C:
                    q.set_running_fields(root, job.id, wait_reason=None)
                    return None
                while temp is not None and temp > COOL_C:
                    cancelled = wait(job, f"остываем, {temp} °C", ACQUIRE_RETRY_SECONDS)
                    if cancelled:
                        return cancelled
                    temp = temperature()
                continue
            if state == "starting":
                reason, seconds = f"ждём GPU: поднимается {engine_name}", STARTING_POLL_SECONDS
            elif state == "wait_qwen":
                reason = "ждём GPU: Qwen держит карту — выгрузите Qwen в панели"
                seconds = ACQUIRE_RETRY_SECONDS
            else:
                reason, seconds = f"ждём GPU: {answer.get('reason')}", ACQUIRE_RETRY_SECONDS
            cancelled = wait(job, reason, seconds)
            if cancelled:
                return cancelled
    return gate


class _IdleRelease:
    """spec §3.4 (б): POST /release once the panel's queue has held no job at all -- neither
    pending nor running -- for H3_IDLE_RELEASE_MIN minutes. Counted from an empty queue, not
    from an idle GPU: while a project renders, the card is never given away."""

    def __init__(self, minutes: float, client, clock=time.monotonic):
        self.limit = float(minutes) * 60.0
        self.client = client
        self.clock = clock
        self.since: float | None = None
        self.released = False

    def tick(self, root) -> None:
        if q.has_active_jobs(root):
            self.since, self.released = None, False
            return
        now = self.clock()
        if self.since is None:
            self.since = now
        if not self.released and now - self.since >= self.limit:
            try:
                self.client.release()
                self.released = True
            except dispatcher_client.DispatcherUnavailable as exc:
                print(f"h3 worker: idle release failed: {exc}", file=sys.stderr)


def _wait_for_engine_start_to_settle(client, *, sleep=time.sleep) -> None:
    """spec §9 (22 GB host RAM): an assembly's ffmpeg must not run next to an engine that is
    still loading. Waits while any of our own engines reports `ready: false`."""
    while True:
        try:
            own = client.status().get("own") or {}
        except dispatcher_client.DispatcherUnavailable:
            return
        if all(record.get("ready") for record in own.values()):
            return
        sleep(ACQUIRE_RETRY_SECONDS)
```
`_run_sglang_generate_job` (заменить тело из задачи 6):
```python
def _run_sglang_generate_job(root, outdir, job, *, gate=None) -> tuple[int, str]:
    from h3_48gb.engines import sglang as sglang_engine

    dispatcher = dispatcher_client.DispatcherClient()
    if gate is None:
        gate = make_gpu_gate(root, "h3", client=dispatcher)
    client = sglang_engine.SglangClient(os.environ.get("H3_SGLANG_URL", sglang_engine.DEFAULT_URL))
    try:
        result = sglang_engine.run_generate(job, root=root, outdir=outdir, client=client, gate=gate)
    except GpuEngineFailed as exc:
        result = (1, f"движок не поднялся: {exc.reason}; лог: {exc.log}\n")
    except Exception as exc:  # noqa: BLE001 -- a bug here must fail the job, not kill the worker
        result = (1, f"sglang adapter crashed: {type(exc).__name__}: {exc}\n")
    if q.cancel_reason(root, job.id) == "released_by_user":
        try:
            dispatcher.release()
        except dispatcher_client.DispatcherUnavailable as exc:
            result = (result[0], result[1] + f"не удалось освободить карту: {exc}\n")
    return result
```
`main_loop`: перед `with hold_worker_lock(...)`:
```python
    idle = None
    if engine.is_sglang():
        idle = _IdleRelease(float(os.environ.get("H3_IDLE_RELEASE_MIN", "15")),
                            dispatcher_client.DispatcherClient())
```
и в цикле сразу после блока `state.resumable` (до проверки паузы): `if idle is not None: idle.tick(root)`.
`run_job`, ветка `KIND_ASSEMBLE`: перед `_run_assemble_job` — `if engine.is_sglang(): _wait_for_engine_start_to_settle(dispatcher_client.DispatcherClient())`.

- [ ] **Step 7: Веб**

`web.py` (импорт `from h3_48gb.engines import dispatcher_client`), маршруты: `_route_get` — `if path == "/api/gpu": return self._gpu_state()`; `_route_post` — `/api/gpu/release` → `_gpu_release`, `/api/qwen/unload` → `_qwen_unload`, `/api/qwen/restore` → `_qwen_restore`.
```python
    def _require_sglang(self) -> None:
        if not engine.is_sglang():
            raise CliError("engine_not_sglang", "это есть только на сервере с sglang (H3_ENGINE=sglang)",
                           {"engine": engine.current()})

    def _running_job(self):
        jobs, _ = q.scan(self.server.queue_root)
        running = [job for job in jobs if job.state == "running"]
        return (running[0] if running else None), sum(1 for job in jobs if job.state == "pending")

    def _gpu_state(self) -> tuple[int, str, bytes]:
        self._require_sglang()
        try:
            status, error = dispatcher_client.DispatcherClient().status(), None
        except dispatcher_client.DispatcherUnavailable as exc:
            status, error = None, str(exc)
        running, pending = self._running_job()
        return 200, "application/json", _json_bytes({
            "ok": True, "dispatcher": status, "dispatcher_error": error,
            "queue": {"pending": pending, "paused": q.is_paused(self.server.queue_root),
                      "running": None if running is None else {
                          "id": running.id, "kind": running.kind, "note": running.note,
                          "started_at": running.started_at, "wait_reason": running.wait_reason}}})

    def _gpu_release(self) -> tuple[int, str, bytes]:
        self._require_sglang()
        payload = self._json_request(allowed=("confirm",))
        root = self.server.queue_root
        running, _pending = self._running_job()
        if running is not None and running.kind in (q.KIND_GENERATE, "upscale"):
            # only GPU work is cancelled; an assembly (ffmpeg, no GPU) keeps running
            if payload.get("confirm") is not True:
                raise CliError("release_needs_confirm",
                               f"H3 считает {running.note or running.id} — освободить карту? "
                               f"Сцена будет потеряна", {"job": running.id})
            with queue_write_errors(root, what="the job id"):
                q.request_cancel(root, running.id, "released_by_user")
                q.set_paused(root, True)
            return 200, "application/json", _json_bytes(
                {"ok": True, "paused": True, "releasing": True, "job": running.id})
        try:
            answer = dispatcher_client.DispatcherClient().release()
        except dispatcher_client.DispatcherUnavailable as exc:
            raise CliError("dispatcher_unavailable", f"диспетчер GPU не отвечает: {exc}", {}) from exc
        with queue_errors(root):
            q.set_paused(root, True)
        return 200, "application/json", _json_bytes(
            {"ok": True, "paused": True, "releasing": False, "released": answer.get("stopped", [])})

    def _qwen_call(self, name: str) -> tuple[int, str, bytes]:
        self._require_sglang()
        self._json_request(allowed=())
        try:
            status, body = getattr(dispatcher_client.DispatcherClient(timeout=600.0), name)()
        except dispatcher_client.DispatcherUnavailable as exc:
            raise CliError("dispatcher_unavailable", f"диспетчер GPU не отвечает: {exc}", {}) from exc
        if status != 200:
            error = body.get("error") or {}
            raise CliError(error.get("code") or "dispatcher_unavailable",
                           error.get("message") or f"диспетчер ответил {status}", {})
        return 200, "application/json", _json_bytes(body)

    def _qwen_unload(self):
        return self._qwen_call("qwen_unload")

    def _qwen_restore(self):
        # spec §2: «вернуть Qwen» is offered *after* the queue -- never under a running scene
        running, pending = self._running_job()
        if running is not None or pending:
            raise CliError("queue_busy", "вернуть Qwen можно после очереди: в ней ещё есть задачи",
                           {"running": running.id if running else None, "pending": pending})
        return self._qwen_call("qwen_restore")
```
(`"upscale"` — литерал до задачи 10, где появится `q.KIND_UPSCALE`; в задаче 10 заменить на константу.)
`ERROR_STATUS`: `"engine_not_sglang": 409, "dispatcher_unavailable": 502, "release_needs_confirm": 409, "qwen_was_not_running": 409, "queue_busy": 409`. `cli.ERROR_CODES`:
```python
    "engine_not_sglang": "a GPU/Qwen route was called on a panel whose engine is not sglang",
    "dispatcher_unavailable": "the host gpu-dispatcher did not answer",
    "release_needs_confirm": "«Освободить карту» while a scene renders needs an explicit confirm",
    "qwen_was_not_running": "«вернуть Qwen» was asked, but Qwen was not running before the panel unloaded it",
    "queue_busy": "«вернуть Qwen» was asked while the panel's queue still holds jobs",
```

- [ ] **Step 8: Зелёный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_gpu_gate.py tests/test_gpu_web.py tests/test_sglang_resume.py tests/test_worker.py -q -p no:cacheprovider`
Expected: PASS. Тесты задачи 6, которые вызывают `worker.run_job` на sglang, теперь идут через gate: в их фикстуру `env` добавить фейковый диспетчер, отвечающий `ready` (`FakeDispatcher()` + `monkeypatch.setenv("H3_DISPATCHER_URL", disp.url)`), иначе они повиснут на «диспетчер недоступен». Это правка фикстуры, не ослабление проверок.

- [ ] **Step 9: Мутации**

(а) В gate убрать ветку `if state == "ready"` температурной проверки (сразу `return None`) → `test_a_hot_card_is_waited_down_to_72` FAIL `[] == ['остываем, 81 °C', ...]`. (б) `COOL_C = 80` → тот же тест FAIL. (в) В `_IdleRelease.tick` не сбрасывать `since` при активных задачах → `test_idle_countdown_restarts_while_anything_is_queued` FAIL (`1 == 0`). (г) В `_gpu_release` убрать проверку `confirm` → `test_release_while_rendering_needs_confirmation` FAIL. (д) В `_run_sglang_generate_job` убрать освобождение по `released_by_user` → `test_released_by_user_releases_the_card...` FAIL `['/acquire'] == ['/acquire', '/release']`. (е) В `main_loop` создавать `_IdleRelease` и на mlx → `test_main_loop_never_talks_to_a_dispatcher_on_mlx` FAIL. (ж) `ACQUIRE_RETRY_SECONDS = 20.0` или `STARTING_POLL_SECONDS = 10.0` → `test_retry_intervals_are_thirty_and_five_seconds` FAIL. (з) Убрать проверку `running.kind in (...)` в `_gpu_release` → `test_release_during_an_assembly...` FAIL (409 `release_needs_confirm`). (и) Убрать отказ `queue_busy` → `test_qwen_restore_is_refused_while_the_queue_is_busy` FAIL. Ошибки — в отчёт.

- [ ] **Step 10: Полный прогон и commit**

```bash
env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest -q -p no:cacheprovider
git add h3_48gb/engines/dispatcher_client.py h3_48gb/worker.py h3_48gb/queue.py h3_48gb/web.py h3_48gb/cli.py tests/_fake_dispatcher.py tests/test_gpu_gate.py tests/test_gpu_web.py tests/test_sglang_resume.py
git commit -m "feat(gpu): ожидание карты через диспетчер, термозащита, автоосвобождение через 15 мин, «Освободить карту», кнопки Qwen"
```

---

## Task 9: Клип под готовый трек без Whisper

**Files:**
- Modify: `h3_48gb/web.py` — `_approve_project_stage` ветка `script` для `clip` (`web.py:4268-4269`); `_scenario_context`/`_scenario_messages` (`web.py:1830-1855`); `_generate_project_scenario` (`web.py:4414-4481`); новая `_equal_scenario_scenes`; `_snap_scene_duration` (`web.py:947-1009`) — параметры `overlap_frames`, `round_up`; `build_clip_scenes` (`web.py:1556-1596`) — снап при sglang
- Modify: `h3_48gb/assemble.py` — новые `_cut_track_piece`, `_trim_video`; `_submit_next_scene_sglang` (задача 5) берёт кусок трека для `clip`; `run` (`assemble.py:771-790`) — обрезка перебора при sglang
- Modify: `h3_48gb/cli.py` (`ERROR_CODES` += `track_too_short`)
- Test: `tests/test_sglang_clip.py` (новый)

**Interfaces:**
- Consumes: `engine.is_sglang()`, `songrun.probe_duration(Path) -> float` (существует, `web.py:4153` уже его зовёт), `library.references_context` (задача 4), `_submit_next_scene_sglang` (задача 5).
- Produces:
  - `web._snap_scene_duration(seconds, carry, *, chained=False, overlap_frames=_SCENE_LATENT_OVERLAP_FRAMES, round_up=False) -> tuple[float, float]` (MLX-вызовы не меняются: значения по умолчанию дают прежний результат)
  - `web._scenario_context(lyrics, raw_segments, caption, duration, *, references_block="") -> str`; `web._scenario_messages(..., *, references_block="")`
  - `web._equal_scenario_scenes(duration: float, caption: str) -> list[dict]`
  - `assemble._cut_track_piece(proj, idx: int, *, run) -> Path` (`<project>/track/pieces/scene-NNN.wav`)
  - `assemble._trim_video(video_path, seconds: float, workdir: Path, *, run) -> Path`
  - код `track_too_short`
- Правило кусков трека (спека §4.1.5, §4.3): границы считаются в кадрах по **снапнутым** длительностям. Сцена `i` начинается на кадре `F_i = Σ_{j<i} round(d_j·24)`; сцепленная сцена запрашивает на 1 кадр больше (повтор кейфрейма, срезается при сборке вместе с `1/24` с аудио, `assemble.py:385-386`), поэтому её кусок начинается на кадре `F_i − 1` и длится `round(d_i·24) + 1` кадров; несцепленная — с `F_i`, `round(d_i·24)` кадров.
- Правило покрытия при sglang (спека §6): сумма снапнутых длительностей `≥` длительности трека и `<` длительность `+ 17/24` с; последняя сцена добирает остаток вверх; при сборке видео обрезается ровно до трека (песня не режется: «песня священна»).

- [ ] **Step 1: Падающие тесты**

`tests/test_sglang_clip.py`:
```python
"""A clip on an imported track, no Whisper (spec §3.3.10, §4.3, §4.1.5)."""
import json
import subprocess
from pathlib import Path

import pytest

from h3_48gb import assemble
from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import queue as q
from h3_48gb import web
from test_web import _call, _pending, _serve


def _mp3(path: Path, seconds: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", "-c:a", "libmp3lame", str(path)],
                   check=True)
    return path


def _probe(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                          "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout)


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    server = _serve(q.layout(outdir / "queue")["root"], outdir)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def _imported_clip(outdir, seconds):
    mp3 = _mp3(outdir / "uploads" / "song.mp3", seconds)
    proj = p.create_project(outdir, "clip", "Clip")
    proj.track["source"] = "import"
    proj.track["mp3"] = str(mp3)
    proj.stages["script"] = "awaiting_approval"
    proj.save()
    return proj, mp3


def test_approving_an_imported_track_measures_it_and_queues_no_song_job(live):
    proj, mp3 = _imported_clip(live.outdir, 12.0)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert status == 200, body
    reloaded = p.load_project(proj.path)
    assert abs(reloaded.track["duration"] - _probe(mp3)) < 0.01
    assert (reloaded.track["mastered_mp3"], reloaded.track["status"]) == (str(mp3), "approved")
    assert (reloaded.stages["script"], reloaded.stages["track"]) == ("approved", "approved")
    assert _pending(live) == []


def test_import_shorter_than_one_scene_is_refused(live):
    """Review Focus 5."""
    proj, _ = _imported_clip(live.outdir, 4.0)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert (status, body["error"]["code"]) == (400, "track_too_short")
    assert p.load_project(proj.path).stages["script"] == "awaiting_approval"


def test_on_mlx_import_still_queues_the_song_job(live, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "mlx")
    proj, _ = _imported_clip(live.outdir, 6.0)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert status == 200, body
    assert [job.kind for job in _pending(live)] == ["song"]


def test_scenario_context_without_lyrics_and_with_references():
    assert web._scenario_context("", [], "warm summer song", 13.0,
                                 references_block="## Reference tags\n@alice (person): a woman") == (
        "## Context\nmode: clip_scenario\nduration: 13 s\n\ncaption:\nwarm summer song\n\n"
        "no lyrics and no transcript: the track is an imported recording; build the scenes from "
        "the caption, the mood and the duration alone\n\n"
        "## Reference tags\n@alice (person): a woman\n\nWrite the clip's scenario now.")


def test_scenario_context_with_lyrics_is_unchanged_without_references():
    assert web._scenario_context("la la", [], "c", 10.0) == (
        "## Context\nmode: clip_scenario\nduration: 10 s\n\ncaption:\nc\n\nlyrics:\nla la\n\n"
        "Write the clip's scenario now.")


def _clip_with_duration(outdir, seconds, caption="warm summer song"):
    proj = p.create_project(outdir, "clip", "Clip")
    proj.track.update({"source": "import", "duration": seconds, "caption": caption,
                       "status": "approved"})
    proj.stages.update({"script": "approved", "track": "approved"})
    proj.save()
    return proj


def test_generate_without_lyrics_reaches_the_llm_on_sglang(live, monkeypatch):
    (live.outdir / "uploads").mkdir(exist_ok=True)
    (live.outdir / "uploads" / "f.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    card = lib.create_card(live.outdir, tag="@alice", kind="person", description="a woman",
                           assets=[live.outdir / "uploads" / "f.png"])
    proj = _clip_with_duration(live.outdir, 13.0)
    proj.set_references([{"tag": "@alice", "version": 1}])
    seen = []
    turn = {"reply": "ok", "scenario": {"style_block": "sunlit", "sections": [
        # `fresh_start` omitted, not null: `_scenario_turn_to_scenes` adds it only when present,
        # and `_typed_scenario_scene` refuses an explicit None (web.py, its own comment)
        {"tag": "a", "start": 0.0, "end": 6.5, "scene": {"prompt": "@alice dances", "duration": 6.5,
                                                         "state_in": "s", "state_out": "t"}},
        {"tag": "b", "start": 6.5, "end": 13.0, "scene": {"prompt": "@alice waves", "duration": 6.5,
                                                          "state_in": "", "state_out": "u"}}]}}
    monkeypatch.setattr(web.provider, "load_providers", lambda outdir: {
        "active": "x", "providers": {"x": {"type": "openai", "available": True,
                                           "base_url": "http://127.0.0.1:9", "model": "m"}}})
    monkeypatch.setattr(web.provider, "load_env", lambda outdir: {})
    monkeypatch.setattr(web.provider, "chat_scenario",
                        lambda cfg, env, messages: seen.append(messages) or turn)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/scenario/generate", {})
    assert status == 200, body
    assert seen[0][1]["content"] == web._scenario_context(
        "", [], "warm summer song", 13.0, references_block=lib.references_context([card]))


def test_procedural_on_sglang_cuts_equal_pieces(live):
    proj = _clip_with_duration(live.outdir, 13.0)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/scenario/generate",
                         {"procedural": True})
    assert status == 200, body
    scenes = p.load_project(proj.path).scenario_scenes
    assert [(s["start"], s["end"], s["duration"], s["prompt"]) for s in scenes] == [
        (0.0, 6.5, 6.5, "warm summer song"), (6.5, 13.0, 6.5, "warm summer song")]


def test_equal_scenes_respect_5_to_10_seconds():
    assert [s["duration"] for s in web._equal_scenario_scenes(25.0, "c")] == \
        [25.0 / 3, 25.0 / 3, 25.0 / 3]
    assert [s["duration"] for s in web._equal_scenario_scenes(5.0, "c")] == [5.0]


def test_build_clip_scenes_on_sglang_covers_the_whole_track(monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    scenario = [{"tag": "a", "start": 0.0, "end": 7.0, "prompt": "a", "duration": 7.0},
                {"tag": "b", "start": 7.0, "end": 14.0, "prompt": "b", "duration": 7.0},
                {"tag": "c", "start": 14.0, "end": 20.0, "prompt": "c", "duration": 6.0}]
    scenes = web.build_clip_scenes({"duration": 20.0}, style_block="", scenario_scenes=scenario)
    assert [s["duration"] for s in scenes] == [158 / 24, 174 / 24, 157 / 24]
    total = sum(s["duration"] for s in scenes)
    assert 20.0 <= total < 20.0 + 17 / 24


def test_build_clip_scenes_on_mlx_is_unchanged(monkeypatch):
    scenario = [{"tag": "a", "start": 0.0, "end": 7.0, "prompt": "a", "duration": 7.0},
                {"tag": "b", "start": 7.0, "end": 14.0, "prompt": "b", "duration": 7.0},
                {"tag": "c", "start": 14.0, "end": 20.0, "prompt": "c", "duration": 6.0}]
    scenes = web.build_clip_scenes({"duration": 20.0}, style_block="", scenario_scenes=scenario)
    assert sum(s["duration"] for s in scenes) <= 20.0


def _clip_project_with_scenes(tmp_path, durations_frames, mp3):
    proj = p.create_project(tmp_path / "out", "clip", "Clip")
    proj.track.update({"mastered_mp3": str(mp3), "duration": _probe(mp3)})
    proj.scenes = [{"idx": i, "prompt": "x", "duration": f / 24, "status": "pending",
                    "job_id": None, "clip_path": None, "keyframe_path": None}
                   for i, f in enumerate(durations_frames)]
    proj.save()
    return proj


def test_track_pieces_follow_snapped_frames_and_the_one_frame_overlap(tmp_path):
    mp3 = _mp3(tmp_path / "song.mp3", 21.0)
    proj = _clip_project_with_scenes(tmp_path, [158, 174, 157], mp3)
    commands = []

    def run(cmd, capture_output=True, text=True):
        commands.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    pieces = proj.path.parent / "track" / "pieces"
    assert assemble._cut_track_piece(proj, 0, run=run) == pieces / "scene-000.wav"
    assemble._cut_track_piece(proj, 1, run=run)
    assert commands == [
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", "0.000000", "-t", f"{158 / 24:.6f}",
         "-i", str(mp3), "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le",
         str(pieces / "scene-000.wav")],
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{157 / 24:.6f}", "-t", f"{175 / 24:.6f}",
         "-i", str(mp3), "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le",
         str(pieces / "scene-001.wav")]]


def test_a_real_piece_has_the_requested_length(tmp_path):
    mp3 = _mp3(tmp_path / "song.mp3", 21.0)
    proj = _clip_project_with_scenes(tmp_path, [158, 174, 157], mp3)
    piece = assemble._cut_track_piece(proj, 1, run=subprocess.run)
    assert abs(_probe(piece) - 175 / 24) < 0.03


def test_a_clip_scene_carries_its_piece_as_the_last_audio(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    monkeypatch.setattr(assemble.secrets, "token_hex", lambda n: "ab12")
    mp3 = _mp3(tmp_path / "song.mp3", 21.0)
    proj = _clip_project_with_scenes(tmp_path, [158, 174, 157], mp3)
    proj.stages["scenes"] = "running"
    proj.save()
    submitted = []

    class _Job:
        id = "j0"

    assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out",
                             submit=lambda root, args, *a, **k: submitted.append(args) or _Job(),
                             run=subprocess.run)
    piece = proj.path.parent / "track" / "pieces" / "scene-000.wav"
    assert submitted[0][-4:] == ["--task", "ref2va", "--audio", str(piece)]
    assert piece.is_file()


def _test_clip(path: Path, seconds: float) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc=size=64x64:rate=24:duration={seconds}", "-f", "lavfi", "-i",
                    f"sine=frequency=220:duration={seconds}", "-c:v", "libx264", "-pix_fmt",
                    "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def _assembly_case(tmp_path, overshoot_track_seconds):
    out = tmp_path / "out"
    proj = p.create_project(out, "clip", "Clip")
    pdir = proj.path.parent
    clips = [_test_clip(pdir / f"c{i}.mp4", 1.0) for i in range(2)]
    mp3 = _mp3(pdir / "song.mp3", overshoot_track_seconds)
    proj.track.update({"mastered_mp3": str(mp3), "duration": _probe(mp3)})
    proj.scenes = [{"idx": i, "prompt": "x", "duration": 1.0, "status": "done", "job_id": f"j{i}",
                    "clip_path": str(c), "keyframe_path": None, "head_drop_frames": 0}
                   for i, c in enumerate(clips)]
    proj.save()
    return proj


def test_an_overshooting_clip_is_trimmed_to_the_track_on_sglang(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _assembly_case(tmp_path, 1.3)
    final = assemble.run(proj.path)
    assert abs(_probe(final) - p.load_project(proj.path).track["duration"]) <= 0.1


def test_the_same_overshoot_still_fails_on_mlx(tmp_path):
    proj = _assembly_case(tmp_path, 1.3)
    with pytest.raises(assemble.AssembleError):
        assemble.run(proj.path)
```

- [ ] **Step 2: Красный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_sglang_clip.py -q -p no:cacheprovider`
Expected: FAIL — `approve/script` ставит `song` (`assert [...] == []`), `TypeError` у `_scenario_context(... references_block=...)`, `AttributeError: _cut_track_piece`.

- [ ] **Step 3: Импорт трека и сценарий в web.py**

`_approve_project_stage`, ветка `stage == "script"`, `proj.kind in ("clip", "song")`:
```python
            elif proj.kind in ("clip", "song"):
                if engine.is_sglang() and proj.kind == "clip" and proj.track.get("source") == "import":
                    result["track"] = self._measure_imported_track(proj)
                else:
                    result["submit"] = self._submit_project_song_job(proj)
```
```python
    def _measure_imported_track(self, proj) -> dict:
        """spec §3.3.10: on sglang an imported track is measured with ffprobe and approved as is
        -- no song job, no Whisper; the mp3 itself is what assembly muxes in (`mastered_mp3`)."""
        mp3 = Path(proj.track["mp3"])
        try:
            duration = songrun.probe_duration(mp3)
        except songrun.SongRunError as exc:
            raise CliError("project_scene_build_failed", f"трек не читается: {exc}",
                           {"id": proj.id}) from exc
        if duration < SCENE_MIN_SECONDS:
            raise CliError("track_too_short",
                           f"трек {duration:.1f} с короче минимальной сцены {SCENE_MIN_SECONDS:g} с",
                           {"id": proj.id, "duration": duration})
        proj.update_track(duration=duration, mastered_mp3=str(mp3), status="approved")
        proj.set_stage_status("track", "approved")
        return {"duration": duration}
```
(`approve_stage("script")` в конце метода остаётся как есть — `script` станет `approved`.)

`_scenario_context` — новая сигнатура и тело:
```python
def _scenario_context(lyrics: str, raw_segments: list[dict], caption: str, duration: float, *,
                      references_block: str = "") -> str:
    if lyrics.strip():
        source = f"lyrics:\n{lyrics}"
    elif raw_segments:
        lines = "\n".join(
            f"[{seg.get('start')}-{seg.get('end')}] {seg.get('text', '')}" for seg in raw_segments)
        source = f"raw transcript with timestamps (Whisper, seconds):\n{lines}"
    else:
        source = ("no lyrics and no transcript: the track is an imported recording; build the "
                  "scenes from the caption, the mood and the duration alone")
    references = f"{references_block}\n\n" if references_block else ""
    return (f"## Context\nmode: clip_scenario\nduration: {duration:g} s\n\n"
            f"caption:\n{caption}\n\n{source}\n\n{references}Write the clip's scenario now.")
```
(Ветка «оба пусто» раньше была недостижима: маршрут отказывал `scenario_no_lyrics`.) `_scenario_messages(lyrics, raw_segments, caption, duration, *, references_block="")` передаёт параметр дальше.

`_generate_project_scenario`: условие отказа — `if not lyrics.strip() and not raw_segments and not engine.is_sglang():`; вызов:
```python
            references_block = ""
            if proj.references:
                cards = [library_module.get_card(self.server.outdir, ref["tag"], ref["version"])
                         for ref in proj.references]
                references_block = library_module.references_context(cards)
            messages = _scenario_messages(lyrics, raw_segments, proj.track.get("caption") or "",
                                          float(duration), references_block=references_block)
```
Ветка `procedural`: `scenes = (_equal_scenario_scenes(float(duration), proj.track.get("caption") or "") if engine.is_sglang() else _procedural_scenario_scenes(proj.track))`.
```python
def _equal_scenario_scenes(duration: float, caption: str) -> list[dict]:
    """spec §3.3.10: no sections, no transcript -- equal pieces, as few as fit the 10 s ceiling
    (each is then ≥ 5 s for any track ≥ 5 s). The human edits the prompts at the gate."""
    count = max(1, math.ceil(duration / SCENE_MAX_SECONDS))
    piece = duration / count
    prompt = caption.strip() or "a music video scene matching the track's mood"
    scenes = []
    for i in range(count):
        start = i * piece
        end = duration if i == count - 1 else (i + 1) * piece
        scenes.append({"tag": f"scene-{i}", "start": start, "end": end, "prompt": prompt,
                       "duration": piece})
    return scenes
```
(Проверить, что `math` импортирован в `web.py`.)

- [ ] **Step 4: Снап клипа при sglang**

`_snap_scene_duration` — сигнатура `(seconds, carry, *, chained=False, overlap_frames=_SCENE_LATENT_OVERLAP_FRAMES, round_up=False)`; тело:
```python
    remainder = ((_H3_LATENTS_PER_CHUNK - overlap_frames) % _H3_FRAMES_PER_CHUNK if chained
                 else _H3_LATENTS_PER_CHUNK)
    target = seconds + carry
    frames = max(_H3_LATENTS_PER_CHUNK, round(target * _H3_FPS))
    if round_up:
        snapped = _grid_frames_at_or_above(frames, remainder=remainder) / _H3_FPS
        return snapped, target - snapped
    snapped = _grid_frames_at_or_below(frames, remainder=remainder) / _H3_FPS
    ...  # the existing SCENE_MIN/SCENE_MAX clamp, unchanged
```
(При `overlap_frames=22` формула даёт прежний `_CHAINED_GRID_REMAINDER`; докстринг дополнить абзацем про sglang: перекрытие 1 кадр → сетка `17k+4`, последняя сцена вверх, спека §4.1.5, §6.)

`build_clip_scenes`, цикл снапа (`web.py:1556-1585`):
```python
    sglang = engine.is_sglang()
    overlap = _SGLANG_OVERLAP_FRAMES if sglang else _SCENE_LATENT_OVERLAP_FRAMES
    carry = 0.0
    scenes = []
    for i, seg in enumerate(expanded):
        chained = i > 0 and not seg.get("fresh_start", False)
        snapped, carry = _snap_scene_duration(seg["end"] - seg["start"], carry, chained=chained,
                                              overlap_frames=overlap,
                                              round_up=sglang and i == len(expanded) - 1)
        ...  # the rest of the loop body unchanged
```
и проверку суммы:
```python
    snapped_total = sum(s["duration"] for s in scenes)
    if sglang:
        ok = duration - _COVERAGE_TOLERANCE_SECONDS <= snapped_total \
            < duration + _H3_FRAMES_PER_CHUNK / _H3_FPS + _COVERAGE_TOLERANCE_SECONDS
        ok = ok and scenes[-1]["duration"] <= sglang_args.MAX_SECONDS
    else:
        ok = (duration - _SNAPPED_COVERAGE_SHORTFALL_SECONDS - _COVERAGE_TOLERANCE_SECONDS
              <= snapped_total <= duration + _COVERAGE_TOLERANCE_SECONDS)
    if not ok:
        raise ProjectSceneBuildError(...)   # the existing message
```

- [ ] **Step 5: Куски трека и обрезка в assemble.py**

```python
def _cut_track_piece(proj, idx: int, *, run) -> Path:
    """spec §4.1.3/§4.3: the slice of the clip's track scene `idx` gets as its audio reference,
    cut on the *snapped* frame grid. A chained scene's slice starts one frame early and runs one
    frame longer -- its frame 0 repeats the previous scene's last frame and is dropped at
    assembly together with its 1/24 s of audio, so what survives lines up with the track."""
    scenes = sorted(proj.scenes, key=lambda scene: scene["idx"])
    frames = [round(scene["duration"] * ASSEMBLY_FPS) for scene in scenes]
    position = [scene["idx"] for scene in scenes].index(idx)
    scene = scenes[position]
    start = sum(frames[:position])
    length = frames[position]
    if position > 0 and not scene.get("fresh_start"):
        start -= SGLANG_OVERLAP_FRAMES
        length += SGLANG_OVERLAP_FRAMES
    start = max(start, 0)
    track = proj.track.get("mastered_mp3") or proj.track.get("mp3")
    out_path = proj.path.parent / "track" / "pieces" / f"scene-{idx:03d}.wav"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start / ASSEMBLY_FPS:.6f}",
           "-t", f"{length / ASSEMBLY_FPS:.6f}", "-i", str(track), "-vn", "-ac", "2",
           "-ar", "48000", "-c:a", "pcm_s16le", str(out_path)]
    _run_ffmpeg(cmd, run, f"ffmpeg track piece for scene {idx}")
    return out_path


def _trim_video(video_path, seconds: float, workdir: Path, *, run) -> Path:
    """sglang only: the last scene rounds *up* onto the grid (spec §4.1.5), so the picture may
    run up to 17/24 s past the track. The picture is cut to the track; the song never is."""
    workdir.mkdir(parents=True, exist_ok=True)
    out_path = workdir / "trimmed.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path), "-t", f"{seconds:.3f}",
           "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-an", str(out_path)]
    _run_ffmpeg(cmd, run, "ffmpeg trim to track")
    return out_path
```
В `_submit_next_scene_sglang`: `track_piece = _cut_track_piece(proj, idx, run=run) if proj.kind == "clip" else None`.
В `run`, сразу после блока `if shortfall > _FREEZE_PAD_EPSILON_SECONDS: ...`:
```python
        if engine.is_sglang() and video_duration - track_duration > _FREEZE_PAD_EPSILON_SECONDS:
            video_only = _trim_video(video_only, track_duration, assembly_dir / "pad", run=run)
            video_duration = _ffprobe_duration(video_only, run=run)
```
`cli.ERROR_CODES["track_too_short"] = "an imported track is shorter than the shortest scene"`.

- [ ] **Step 6: Зелёный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_sglang_clip.py tests/test_web_projects.py tests/test_assemble.py -q -p no:cacheprovider`
Expected: PASS (старые тесты `_snap_scene_duration`/`build_clip_scenes` на MLX — без изменений).

- [ ] **Step 7: Мутации**

(а) В `_cut_track_piece` убрать сдвиг на `SGLANG_OVERLAP_FRAMES` → `test_track_pieces_follow_...` FAIL (`-ss 6.583333`, `-t 7.250000`). (б) В `build_clip_scenes` не передавать `round_up` → `test_build_clip_scenes_on_sglang_covers_the_whole_track` FAIL (`[158/24, 174/24, 140/24]`, сумма < 20). (в) Убрать ветку `_trim_video` в `run` → `test_an_overshooting_clip_is_trimmed...` FAIL `AssembleError`. (г) В `_approve_project_stage` вернуть безусловный `_submit_project_song_job` → `test_approving_an_imported_track...` FAIL (`[Job(kind='song')] == []`). (д) Убрать проверку `track_too_short` → `test_import_shorter_than_one_scene_is_refused` FAIL. Ошибки — в отчёт.

- [ ] **Step 8: Полный прогон и commit**

```bash
env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest -q -p no:cacheprovider
git add h3_48gb/web.py h3_48gb/assemble.py h3_48gb/cli.py tests/test_sglang_clip.py
git commit -m "feat(clip): клип под готовый трек без Whisper — ffprobe, сцены по сценарию, куски трека по снапнутой сетке"
```

---

## Task 10: Апскейл LTX через HTTP ComfyUI: шаблон в репо, одна сила на клип, `8k+1`, `-ltx.mp4`

**Files:**
- Create: `h3_48gb/engines/motion.py`, `h3_48gb/engines/ltx.py`, `h3_48gb/engines/ltx_workflow.json`, `tests/_fake_comfy.py`
- Modify: `h3_48gb/project.py` — `STAGE_NAMES` += `"upscale"` (миграция: нет ключа → `"draft"`), `Project.set_scene_fields`
- Modify: `h3_48gb/queue.py` — `KIND_UPSCALE = "upscale"`, `JOB_KINDS`, `_validate_args_shape_for_kind` (`queue.py:619-630`) знает `upscale`
- Modify: `h3_48gb/worker.py` — `_run_upscale_job`, ветка в `run_job`
- Modify: `h3_48gb/web.py:2020-2025` — активная задача проекта (`_project_active_job`, `web.py:1988`) видит `upscale` (`{"kind": "upscale", "job": ...}`); `_gpu_release` — литерал `"upscale"` заменить на `q.KIND_UPSCALE`
- Modify: `pyproject.toml` — `package-data` += `"engines/*.json"`
- Modify: `tests/test_project.py` — ожидаемый набор этапов (теперь 6)
- Test: `tests/test_ltx_upscale.py` (новый)

**Interfaces:**
- Consumes: `make_gpu_gate(root, "ltx", client=...)`, `DispatcherClient` (задача 8); `_project_arg` (worker).
- Produces:
  - `motion.CALM = 3.0`, `motion.FAST = 7.5`, `motion.clip_motion(paths, *, run) -> float`, `motion.lora_for(m: float) -> float` (0.6 / 0.3 / 0.15 — сила LoRA детейлера, пороги движения 3.0 и 7.5, как `h3-bench/motion.py`)
  - `ltx.DEFAULT_COMFY_URL = "http://127.0.0.1:8188"`, `ltx.DEFAULT_COMFY_OUTPUT = "/home/alex/Outputs/comfy/output"`, `ltx.DENOISE = 0.10`, `ltx.SEED = 42`, `ltx.STEPS = 4`, `ltx.PREFIX_ROOT = "h3panel"`
  - `ltx.pad_frames(n: int) -> int` (`((n + 6) // 8) * 8 + 1`, как `run_ltx.sh`)
  - `ltx.build_workflow(*, input_name, prompt, strength, prefix) -> dict`
  - `class ltx.UpscaleError(Exception)`, `class ltx.ComfyClient(base_url, timeout=60.0, boundary=None)` с `upload(path, name) -> str`, `prompt(workflow) -> str`, `history(prompt_id) -> dict | None`
  - `ltx.upscale_part(clip, *, prompt, strength, prefix, client, comfy_output, run, sleep=time.sleep, cancelled=lambda: None, poll_seconds=5.0) -> Path` → `<stem>-ltx.mp4`
  - `ltx.run_upscale(project_path, *, client, comfy_output, run, attempt, sleep=time.sleep, cancelled=lambda: None) -> tuple[int, str]`
  - `Project.set_scene_fields(idx, **fields) -> Project` (только `ltx_path`)
  - `q.KIND_UPSCALE`; задача `["upscale", "--project", <path>]`
- Решения: вход в ComfyUI — `POST /upload/image` (каталог `ComfyUI/input/` в контейнер не смонтирован); выход — PNG-кадры в `<COMFY_OUTPUT_DIR>/h3panel/<project>/<stem>-<attempt>/f_*.png` (том `ro`), `attempt` = id задачи `upscale`, поэтому кадры прошлой попытки никогда не читаются; готовность — опрос `GET /history/{prompt_id}` (без websocket: панели не нужен aiohttp).

- [ ] **Step 1: Шаблон воркфлоу**

`h3_48gb/engines/ltx_workflow.json` — граф `Projects/comfy/bin/build_ltx_workflow.py:88-153` с нейтральными значениями в пяти подставляемых местах (`10.file`, `20.text`, `2.strength_model`, `33.noise_seed`, `42.filename_prefix`); `32.denoise`/`32.steps` уже равны значениям панели:
```json
{
 "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors", "weight_dtype": "default"}},
 "2": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": "ltx-2-19b-ic-lora-detailer.safetensors", "strength_model": 1.0}},
 "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors", "type": "ltxv", "device": "default"}},
 "4": {"class_type": "VAELoader", "inputs": {"vae_name": "ltx-2.5-video-vae-bf16.safetensors"}},
 "5": {"class_type": "VAELoader", "inputs": {"vae_name": "ltx-2.5-audio-vae-bf16.safetensors"}},
 "6": {"class_type": "LatentUpscaleModelLoader", "inputs": {"model_name": "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors"}},
 "10": {"class_type": "LoadVideo", "inputs": {"file": "input-pad.mp4"}},
 "11": {"class_type": "GetVideoComponents", "inputs": {"video": ["10", 0]}},
 "12": {"class_type": "VAEEncode", "inputs": {"pixels": ["11", 0], "vae": ["4", 0]}},
 "13": {"class_type": "LTXVLatentUpsampler", "inputs": {"samples": ["12", 0], "upscale_model": ["6", 0], "vae": ["4", 0]}},
 "14": {"class_type": "ImageFromBatch", "inputs": {"image": ["11", 0], "batch_index": 0, "length": 1}},
 "15": {"class_type": "LTXVImgToVideoInplace", "inputs": {"vae": ["4", 0], "image": ["14", 0], "latent": ["13", 0], "strength": 1.0, "bypass": false}},
 "16": {"class_type": "LTXVAudioVAEEncode", "inputs": {"audio": ["11", 1], "audio_vae": ["5", 0]}},
 "17": {"class_type": "LTXVConcatAVLatent", "inputs": {"video_latent": ["15", 0], "audio_latent": ["16", 0]}},
 "20": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["3", 0]}},
 "21": {"class_type": "CLIPTextEncode", "inputs": {"text": "bad anatomy, inconsistent look, low resolution,", "clip": ["3", 0]}},
 "22": {"class_type": "LTXVConditioning", "inputs": {"positive": ["20", 0], "negative": ["21", 0], "frame_rate": 24.0}},
 "30": {"class_type": "LTXVDualCFGGuider", "inputs": {"model": ["2", 0], "positive": ["22", 0], "negative": ["22", 1], "video_cfg": 1.0, "audio_cfg": 1.0}},
 "31": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler_ancestral"}},
 "32": {"class_type": "BasicScheduler", "inputs": {"model": ["2", 0], "scheduler": "simple", "steps": 4, "denoise": 0.1}},
 "33": {"class_type": "RandomNoise", "inputs": {"noise_seed": 42}},
 "34": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["33", 0], "guider": ["30", 0], "sampler": ["31", 0], "sigmas": ["32", 0], "latent_image": ["17", 0]}},
 "40": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["34", 0]}},
 "41": {"class_type": "VAEDecodeTiled", "inputs": {"samples": ["40", 0], "vae": ["4", 0], "tile_size": 768, "overlap": 64, "temporal_size": 4096, "temporal_overlap": 32}},
 "42": {"class_type": "SaveImage", "inputs": {"images": ["41", 0], "filename_prefix": "h3panel/input/f"}}
}
```
Сверка с источником (не GPU-работа, только чтение и чистый Python на хосте; ComfyUI не поднимается):
```bash
ssh alex-neuro 'cd /home/alex/Projects/comfy && LTX_PROMPT=x LTX_LORA=1.0 LTX_DENOISE=0.10 LTX_STEPS=4 LTX_SEED=42 LTX_PREFIX=h3panel .venv/bin/python bin/build_ltx_workflow.py input /dev/stdout' > /tmp/ltx-src.json
```
и сравнить граф с шаблоном (`class_type` и ключи `inputs` каждого узла, связи) — расхождение исправить в шаблоне, а не в тесте. Прогон `validate_workflow.py` против шаблона — шаг задачи 14 (он импортирует ComfyUI на хосте).

`pyproject.toml`: `h3_48gb = ["webui/*.html", "webui/*.css", "webui/*.js", "engines/*.json"]`.

- [ ] **Step 2: Фейковый ComfyUI**

`tests/_fake_comfy.py`:
```python
"""A stand-in for ComfyUI's /upload/image, /prompt and /history (spec §6), writing PNG frames
where a SaveImage node would -- `<output>/<filename_prefix>_NNNNN_.png`."""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw


class FakeComfy:
    def __init__(self, output_dir: Path, *, frames=(25,), history_pending=1, fail=False):
        self.output_dir = Path(output_dir)
        self.uploads: list[tuple[str, bytes]] = []
        self.prompts: list[dict] = []
        self.history_calls: list[str] = []
        self._frames = list(frames)
        self.history_pending = history_pending
        self.fail = fail
        self._pending: dict[str, int] = {}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _write_frames(self, prefix: str) -> None:
        count = self._frames.pop(0) if len(self._frames) > 1 else self._frames[0]
        directory = self.output_dir / Path(prefix).parent
        directory.mkdir(parents=True, exist_ok=True)
        stem = Path(prefix).name
        for index in range(1, count + 1):
            Image.new("RGB", (128, 128), (index % 255, 40, 90)).save(
                directory / f"{stem}_{index:05d}_.png")

    def _handler(fake):  # noqa: N805
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body):
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/upload/image":
                    name = re.search(rb'filename="([^"]+)"', body).group(1).decode()
                    fake.uploads.append((name, body))
                    return self._send(200, {"name": name, "subfolder": "", "type": "input"})
                if self.path == "/prompt":
                    workflow = json.loads(body)["prompt"]
                    fake.prompts.append(workflow)
                    prompt_id = f"p{len(fake.prompts)}"
                    fake._pending[prompt_id] = fake.history_pending
                    if not fake.fail:
                        fake._write_frames(workflow["42"]["inputs"]["filename_prefix"])
                    return self._send(200, {"prompt_id": prompt_id, "number": len(fake.prompts),
                                            "node_errors": {}})
                self._send(404, {})

            def do_GET(self):
                prompt_id = self.path.rsplit("/", 1)[-1]
                fake.history_calls.append(prompt_id)
                if fake._pending.get(prompt_id, 0) > 0:
                    fake._pending[prompt_id] -= 1
                    return self._send(200, {})
                status = ({"status_str": "error", "completed": False,
                           "messages": [["execution_error", {"exception_message": "OOM"}]]}
                          if fake.fail else {"status_str": "success", "completed": True,
                                             "messages": []})
                self._send(200, {prompt_id: {"status": status, "outputs": {}}})
        return Handler
```

- [ ] **Step 3: Падающие тесты**

`tests/test_ltx_upscale.py`:
```python
"""LTX upscale (spec §4.2) against a fake ComfyUI, with real ffmpeg on tiny lavfi clips."""
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from h3_48gb import project as p
from h3_48gb.engines import ltx
from h3_48gb.engines import motion
from _fake_comfy import FakeComfy

TEMPLATE = json.loads((Path(ltx.__file__).with_name("ltx_workflow.json")).read_text())


TESTSRC = "testsrc=size=64x64:rate=24:duration=1"
STILL = "color=c=gray:size=64x64:rate=24:duration=1"
BUSY = "color=c=gray:size=64x64:rate=24:duration=1,noise=alls=100:allf=t"


def _clip(path: Path, video: str = TESTSRC) -> Path:
    """A 1 s, 24-frame clip with an audio track (run_ltx.sh muxes the part's own audio back)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", video,
                    "-f", "lavfi", "-i", "sine=frequency=220:duration=1", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def _frames(path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v",
                          "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True)
    return int(out.stdout)


def _has_audio(path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=index", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(out.stdout.strip())


def test_pad_frames_is_8k_plus_1_like_run_ltx_sh():
    assert [ltx.pad_frames(n) for n in (24, 124, 175, 209)] == [25, 129, 177, 209]


def test_build_workflow_changes_exactly_five_inputs():
    wf = ltx.build_workflow(input_name="s-a1-pad.mp4", prompt="a beach", strength=0.3,
                            prefix="h3panel/proj/s-a1")
    expected = json.loads(json.dumps(TEMPLATE))
    expected["10"]["inputs"]["file"] = "s-a1-pad.mp4"
    expected["20"]["inputs"]["text"] = "a beach"
    expected["2"]["inputs"]["strength_model"] = 0.3
    expected["33"]["inputs"]["noise_seed"] = 42
    expected["42"]["inputs"]["filename_prefix"] = "h3panel/proj/s-a1/f"
    assert wf == expected
    assert (wf["32"]["inputs"]["denoise"], wf["32"]["inputs"]["steps"]) == (0.1, 4)


def test_template_graph_is_well_formed_and_matches_the_source_inputs():
    """The validate_workflow.py rules that need no ComfyUI: every link points at a node that
    exists, and every node's input keys are exactly build_ltx_workflow.py's (an unknown input
    passes ComfyUI's own validate_prompt and only fails in execute())."""
    expected_inputs = {
        "1": {"unet_name", "weight_dtype"}, "2": {"model", "lora_name", "strength_model"},
        "3": {"clip_name", "type", "device"}, "4": {"vae_name"}, "5": {"vae_name"},
        "6": {"model_name"}, "10": {"file"}, "11": {"video"}, "12": {"pixels", "vae"},
        "13": {"samples", "upscale_model", "vae"}, "14": {"image", "batch_index", "length"},
        "15": {"vae", "image", "latent", "strength", "bypass"}, "16": {"audio", "audio_vae"},
        "17": {"video_latent", "audio_latent"}, "20": {"text", "clip"}, "21": {"text", "clip"},
        "22": {"positive", "negative", "frame_rate"},
        "30": {"model", "positive", "negative", "video_cfg", "audio_cfg"},
        "31": {"sampler_name"}, "32": {"model", "scheduler", "steps", "denoise"},
        "33": {"noise_seed"}, "34": {"noise", "guider", "sampler", "sigmas", "latent_image"},
        "40": {"av_latent"},
        "41": {"samples", "vae", "tile_size", "overlap", "temporal_size", "temporal_overlap"},
        "42": {"images", "filename_prefix"}}
    assert {nid: set(node["inputs"]) for nid, node in TEMPLATE.items()} == expected_inputs
    for node in TEMPLATE.values():
        for value in node["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[1], int):
                assert value[0] in TEMPLATE


def test_motion_is_measured_over_all_parts_together():
    px = 32 * 24
    still = bytes(3 * px)
    moving = (bytes(px) + bytes([8]) * px + bytes(px))
    answers = {"a.mp4": still, "b.mp4": moving}

    def run(cmd, capture_output=True):
        return subprocess.CompletedProcess(cmd, 0, answers[Path(cmd[cmd.index("-i") + 1]).name], b"")

    assert motion.clip_motion([Path("a.mp4"), Path("b.mp4")], run=run) == 4.0
    assert (motion.clip_motion([Path("a.mp4")], run=run),
            motion.clip_motion([Path("b.mp4")], run=run)) == (0.0, 8.0)
    assert [motion.lora_for(m) for m in (0.0, 2.99, 3.0, 7.49, 7.5, 8.0)] == \
        [0.6, 0.6, 0.3, 0.3, 0.15, 0.15]


def test_upscale_part_pads_uploads_waits_and_muxes(tmp_path):
    clip = _clip(tmp_path / "scenes" / "h3-s0-896x512.mp4")
    out_dir = tmp_path / "comfy-out"
    fake = FakeComfy(out_dir, frames=(25,))
    try:
        result = ltx.upscale_part(clip, prompt="a beach", strength=0.3,
                                  prefix="h3panel/proj/h3-s0-896x512-a1",
                                  client=ltx.ComfyClient(fake.url), comfy_output=out_dir,
                                  run=subprocess.run, sleep=lambda s: None)
    finally:
        fake.close()
    assert result == clip.with_name("h3-s0-896x512-ltx.mp4")
    assert (_frames(result), _has_audio(result)) == (24, True)
    assert [name for name, _ in fake.uploads] == ["h3-s0-896x512-a1-pad.mp4"]
    assert fake.prompts == [ltx.build_workflow(input_name="h3-s0-896x512-a1-pad.mp4",
                                               prompt="a beach", strength=0.3,
                                               prefix="h3panel/proj/h3-s0-896x512-a1")]
    assert fake.history_calls == ["p1", "p1"]
    assert not (out_dir / "h3panel" / "proj" / "h3-s0-896x512-a1").exists()   # frames removed
    assert not (clip.parent / "ltx-work" / "h3-s0-896x512-a1-pad.mp4").exists()


def test_upscale_prefix_is_unique_per_attempt_and_frame_count_is_checked(tmp_path):
    """Review Focus 3: frames left by an earlier attempt are never read; a wrong count fails."""
    clip = _clip(tmp_path / "scenes" / "s.mp4")
    out_dir = tmp_path / "comfy-out"
    stale = out_dir / "h3panel" / "proj" / "s-a1"
    stale.mkdir(parents=True)
    for i in range(1, 40):
        (stale / f"f_{i:05d}_.png").write_bytes(b"old")
    fake = FakeComfy(out_dir, frames=(24,))
    try:
        with pytest.raises(ltx.UpscaleError) as excinfo:
            ltx.upscale_part(clip, prompt="x", strength=0.3, prefix="h3panel/proj/s-a2",
                             client=ltx.ComfyClient(fake.url), comfy_output=out_dir,
                             run=subprocess.run, sleep=lambda s: None)
    finally:
        fake.close()
    assert str(excinfo.value) == "ComfyUI вернул 24 кадров вместо 25 (h3panel/proj/s-a2)"
    assert not clip.with_name("s-ltx.mp4").exists()


def test_a_comfy_execution_error_fails_the_part(tmp_path):
    clip = _clip(tmp_path / "s.mp4")
    fake = FakeComfy(tmp_path / "o", fail=True)
    try:
        with pytest.raises(ltx.UpscaleError) as excinfo:
            ltx.upscale_part(clip, prompt="x", strength=0.3, prefix="h3panel/p/s-a1",
                             client=ltx.ComfyClient(fake.url), comfy_output=tmp_path / "o",
                             run=subprocess.run, sleep=lambda s: None)
    finally:
        fake.close()
    assert str(excinfo.value) == "ComfyUI: OOM"


def test_one_strength_for_the_whole_clip(tmp_path):
    out = tmp_path / "out"
    proj = p.create_project(out, "video", "Up")
    pdir = proj.path.parent
    still = _clip(pdir / "scenes" / "still.mp4", STILL)
    busy = _clip(pdir / "scenes" / "busy.mp4", BUSY)
    per_part = [motion.lora_for(motion.clip_motion([c], run=subprocess.run)) for c in (still, busy)]
    assert per_part[0] != per_part[1], "fixture must make a per-scene choice differ"
    proj.scenes = [{"idx": i, "prompt": f"scene {i}", "duration": 1.0, "status": "done",
                    "job_id": f"j{i}", "clip_path": str(c), "keyframe_path": None}
                   for i, c in enumerate((still, busy))]
    proj.save()
    fake = FakeComfy(tmp_path / "comfy-out", frames=(25,))
    try:
        code, log = ltx.run_upscale(proj.path, client=ltx.ComfyClient(fake.url),
                                    comfy_output=tmp_path / "comfy-out", run=subprocess.run,
                                    attempt="up1", sleep=lambda s: None)
    finally:
        fake.close()
    assert code == 0, log
    strengths = [wf["2"]["inputs"]["strength_model"] for wf in fake.prompts]
    combined = motion.lora_for(motion.clip_motion([still, busy], run=subprocess.run))
    assert strengths == [combined, combined]
    assert [wf["20"]["inputs"]["text"] for wf in fake.prompts] == ["scene 0", "scene 1"]
    assert [wf["42"]["inputs"]["filename_prefix"] for wf in fake.prompts] == \
        [f"h3panel/{proj.id}/still-up1/f", f"h3panel/{proj.id}/busy-up1/f"]
    reloaded = p.load_project(proj.path)
    assert [s["ltx_path"] for s in reloaded.scenes] == \
        [str(still.with_name("still-ltx.mp4")), str(busy.with_name("busy-ltx.mp4"))]
    assert reloaded.stages["upscale"] == "done"


def test_multipart_upload_body_is_exact(tmp_path):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"VIDEO")
    fake = FakeComfy(tmp_path)
    try:
        ltx.ComfyClient(fake.url, boundary="BOUNDARY").upload(clip, "a-pad.mp4")
    finally:
        fake.close()
    assert fake.uploads == [("a-pad.mp4", (
        b"--BOUNDARY\r\nContent-Disposition: form-data; name=\"image\"; filename=\"a-pad.mp4\"\r\n"
        b"Content-Type: video/mp4\r\n\r\nVIDEO\r\n"
        b"--BOUNDARY\r\nContent-Disposition: form-data; name=\"type\"\r\n\r\ninput\r\n"
        b"--BOUNDARY\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n"
        b"--BOUNDARY--\r\n"))]


def test_project_stage_upscale_exists_and_old_projects_migrate_to_draft(tmp_path):
    proj = p.create_project(tmp_path, "video", "T")
    assert proj.stages["upscale"] == "draft"
    data = json.loads(proj.path.read_text())
    del data["stages"]["upscale"]
    proj.path.write_text(json.dumps(data))
    assert p.load_project(proj.path).stages["upscale"] == "draft"
```

- [ ] **Step 4: Красный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_ltx_upscale.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'h3_48gb.engines.ltx'`.

- [ ] **Step 5: `motion.py`**

```python
"""How much a clip moves, and the LTX detailer strength that follows (spec §4.2.2), ported from
h3-bench/motion.py: mean absolute difference of neighbouring frames at 32x24 grey. Measured over
*all* parts of a clip together -- one strength for the whole clip, never per scene."""
from __future__ import annotations

import subprocess

import numpy as np

CALM, FAST = 3.0, 7.5
_W, _H = 32, 24


def _part_totals(path, *, run) -> tuple[float, int]:
    raw = run(["ffmpeg", "-v", "error", "-i", str(path), "-vf",
               f"scale={_W}:{_H},format=gray", "-f", "rawvideo", "-"], capture_output=True).stdout
    px = _W * _H
    frames = np.frombuffer(raw, dtype=np.uint8)[: len(raw) // px * px].reshape(-1, px)
    if len(frames) < 2:
        return 0.0, 0
    diffs = np.abs(np.diff(frames.astype(np.int16), axis=0))
    return float(diffs.sum()), int(diffs.size)


def clip_motion(paths, *, run=subprocess.run) -> float:
    total, count = 0.0, 0
    for path in paths:
        part_total, part_count = _part_totals(path, run=run)
        total += part_total
        count += part_count
    return total / count if count else 0.0


def lora_for(m: float) -> float:
    if m < CALM:
        return 0.6
    if m < FAST:
        return 0.3
    return 0.15
```

- [ ] **Step 6: `ltx.py`**

```python
"""LTX-2.5 upscale through ComfyUI's HTTP API (spec §4.2), no host scripts: the workflow
template ships in this package (`ltx_workflow.json`, the graph of comfy/bin/build_ltx_workflow.py),
each part is padded to 8k+1 frames by cloning its last frame, uploaded, run, and its PNG frames
muxed back with the part's own audio and cut to the original frame count -> `<stem>-ltx.mp4`."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from h3_48gb import project as project_module
from h3_48gb.engines import motion

DEFAULT_COMFY_URL = "http://127.0.0.1:8188"
DEFAULT_COMFY_OUTPUT = "/home/alex/Outputs/comfy/output"
DENOISE = 0.10
SEED = 42
STEPS = 4
FPS = 24
PREFIX_ROOT = "h3panel"
FALLBACK_PROMPT = ("Amateur handheld phone video in natural daylight, sharp and highly detailed, "
                   "real skin texture, real fabric texture, natural light.")
_TEMPLATE_PATH = Path(__file__).with_name("ltx_workflow.json")


class UpscaleError(Exception):
    pass


def pad_frames(n: int) -> int:
    return ((n + 6) // 8) * 8 + 1


def build_workflow(*, input_name: str, prompt: str, strength: float, prefix: str) -> dict:
    workflow = copy.deepcopy(json.loads(_TEMPLATE_PATH.read_text(encoding="utf-8")))
    workflow["10"]["inputs"]["file"] = input_name
    workflow["20"]["inputs"]["text"] = prompt
    workflow["2"]["inputs"]["strength_model"] = strength
    workflow["33"]["inputs"]["noise_seed"] = SEED
    workflow["42"]["inputs"]["filename_prefix"] = f"{prefix}/f"
    return workflow


class ComfyClient:
    def __init__(self, base_url: str, timeout: float = 60.0, boundary: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.boundary = boundary

    def _open(self, method, path, data=None, headers=None):
        request = urllib.request.Request(self.base_url + path, data=data, method=method,
                                         headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise UpscaleError(f"ComfyUI {method} {path}: HTTP {exc.code}: "
                               f"{exc.read().decode('utf-8', 'replace')[:2000]}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise UpscaleError(f"ComfyUI {method} {path} недоступен: {exc}") from None

    def upload(self, path: Path, name: str) -> str:
        boundary = self.boundary or uuid.uuid4().hex
        b = boundary.encode()
        body = (b"--" + b + b"\r\nContent-Disposition: form-data; name=\"image\"; filename=\""
                + name.encode() + b"\"\r\nContent-Type: video/mp4\r\n\r\n" + Path(path).read_bytes()
                + b"\r\n--" + b + b"\r\nContent-Disposition: form-data; name=\"type\"\r\n\r\ninput"
                + b"\r\n--" + b + b"\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\n"
                + b"true\r\n--" + b + b"--\r\n")
        answer = self._open("POST", "/upload/image", body,
                            {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return answer["name"]

    def prompt(self, workflow: dict) -> str:
        answer = self._open("POST", "/prompt",
                            json.dumps({"prompt": workflow, "client_id": "h3-panel"}).encode(),
                            {"Content-Type": "application/json"})
        if "prompt_id" not in answer:
            raise UpscaleError(f"ComfyUI отклонил воркфлоу: {json.dumps(answer)[:2000]}")
        return answer["prompt_id"]

    def history(self, prompt_id: str) -> dict | None:
        return self._open("GET", f"/history/{prompt_id}").get(prompt_id)


def _count_frames(path, *, run) -> int:
    out = run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v",
               "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
              capture_output=True, text=True)
    try:
        return int(out.stdout.strip())
    except ValueError:
        raise UpscaleError(f"ffprobe не посчитал кадры {path}: {out.stderr}") from None


def _ffmpeg(cmd, *, run, what) -> None:
    result = run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise UpscaleError(f"{what}: {(result.stderr or '').strip()[:2000]}")


def upscale_part(clip, *, prompt, strength, prefix, client, comfy_output, run,
                 sleep=time.sleep, cancelled=lambda: None, poll_seconds: float = 5.0) -> Path:
    clip = Path(clip)
    src_n = _count_frames(clip, run=run)
    pad_n = pad_frames(src_n)
    work = clip.parent / "ltx-work"
    work.mkdir(parents=True, exist_ok=True)
    attempt_name = Path(prefix).name
    padded = work / f"{attempt_name}-pad.mp4"
    vf = ["-vf", f"tpad=stop_mode=clone:stop={pad_n - src_n}"] if pad_n > src_n else []
    _ffmpeg(["ffmpeg", "-v", "error", "-y", "-i", str(clip), *vf, "-frames:v", str(pad_n),
             "-c:v", "libx264", "-qp", "0", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             "-c:a", "copy", str(padded)], run=run, what="ffmpeg pad")
    if _count_frames(padded, run=run) != pad_n:
        raise UpscaleError(f"дополнение до {pad_n} кадров не удалось ({padded})")
    name = client.upload(padded, padded.name)
    prompt_id = client.prompt(build_workflow(input_name=name, prompt=prompt, strength=strength,
                                             prefix=prefix))
    while True:
        reason = cancelled()
        if reason:
            raise UpscaleError(reason)
        entry = client.history(prompt_id)
        status = (entry or {}).get("status") or {}
        if status.get("status_str") == "success" and status.get("completed"):
            break
        if status.get("status_str") == "error":
            message = next((m[1].get("exception_message") for m in status.get("messages", [])
                            if m and m[0] == "execution_error"), "ошибка исполнения")
            raise UpscaleError(f"ComfyUI: {message}")
        sleep(poll_seconds)
    frames_dir = Path(comfy_output) / prefix
    frames = sorted(frames_dir.glob("f_*.png"))
    if len(frames) != pad_n:
        raise UpscaleError(f"ComfyUI вернул {len(frames)} кадров вместо {pad_n} ({prefix})")
    out = clip.with_name(f"{clip.stem}-ltx.mp4")
    part = out.with_name(out.stem + ".part.mp4")
    _ffmpeg(["ffmpeg", "-v", "error", "-y", "-framerate", str(FPS), "-pattern_type", "glob",
             "-i", str(frames_dir / "f_*.png"), "-i", str(clip), "-frames:v", str(src_n),
             "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-crf", "12", "-preset", "slow",
             "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(part)],
            run=run, what="ffmpeg mux")
    os.replace(part, out)
    # The frames were only an intermediate; the -ltx part now holds them. They live in our own
    # rw subdirectory of ComfyUI's output (compose.yaml mounts <output>/h3panel rw, the rest ro).
    shutil.rmtree(frames_dir, ignore_errors=True)
    padded.unlink(missing_ok=True)
    return out


def run_upscale(project_path, *, client, comfy_output, run, attempt: str, sleep=time.sleep,
                cancelled=lambda: None) -> tuple[int, str]:
    proj = project_module.load_project(project_path)
    scenes = sorted(proj.scenes, key=lambda scene: scene["idx"])
    clips = [Path(scene["clip_path"]) for scene in scenes]
    proj.set_stage_status("upscale", "running")
    strength = motion.lora_for(motion.clip_motion(clips, run=run))
    log = [f"ltx: сила {strength:g} на весь клип ({len(clips)} частей)\n"]
    try:
        for scene, clip in zip(scenes, clips):
            out = upscale_part(clip, prompt=scene.get("prompt") or FALLBACK_PROMPT,
                               strength=strength,
                               prefix=f"{PREFIX_ROOT}/{proj.id}/{clip.stem}-{attempt}",
                               client=client, comfy_output=comfy_output, run=run, sleep=sleep,
                               cancelled=cancelled)
            proj.set_scene_fields(scene["idx"], ltx_path=str(out))
            log.append(f"ltx: сцена {scene['idx']} -> {out.name}\n")
    except UpscaleError as exc:
        proj.set_stage_status("upscale", "failed")
        log.append(f"ltx: {exc}\n")
        return 1, "".join(log)
    proj.set_stage_status("upscale", "done")
    return 0, "".join(log)
```

- [ ] **Step 7: Проект, очередь, воркер**

`project.py`: `STAGE_NAMES = ("script", "track", "scenario", "scenes", "upscale", "assembly")`; в `_apply` после миграции `scenario`:
```python
        # spec §3.3.8/§4.2: a project.json written before the upscale stage existed has no key
        # for it -- "draft" (not started), unlike scenario's "approved": nothing has upscaled it.
        self.stages.setdefault("upscale", "draft")
```
```python
    _SCENE_FIELDS = ("ltx_path",)

    def set_scene_fields(self, idx: int, **fields) -> "Project":
        unknown = set(fields) - set(self._SCENE_FIELDS)
        if unknown:
            raise ProjectError(f"unknown scene field(s) {sorted(unknown)}")
        with _project_lock(self.path.parent, exclusive=True):
            data = _read_data(self.path)
            scene = _find_scene(data["scenes"], idx)
            scene.update(fields)
            write_json_durably(self.path, data)
            self._apply(data)
        return self
```
(`_find_scene(scenes, idx)` возвращает сам словарь сцены или бросает `UnknownScene`, `project.py:389-403`.) `tests/test_project.py:49` — ожидаемое множество этапов `{"script", "track", "scenario", "scenes", "upscale", "assembly"}`; если тест требует `draft` у всех, кроме `scenario`, — `upscale` тоже `draft`, условие остаётся верным.

`queue.py`: `KIND_UPSCALE = "upscale"`, `JOB_KINDS = (KIND_GENERATE, KIND_SONG, KIND_ASSEMBLE, KIND_UPSCALE)`; в `_validate_args_shape_for_kind` оба кортежа `(KIND_SONG, KIND_ASSEMBLE)` → `(KIND_SONG, KIND_ASSEMBLE, KIND_UPSCALE)` (иначе generate-задача с argv `["upscale", ...]` прошла бы, а upscale-задача без `--project` — тоже). Тест в `tests/test_ltx_upscale.py`:
```python
def test_upscale_job_args_are_shape_checked(tmp_path):
    from h3_48gb import queue as q
    root = q.layout(tmp_path / "queue")["root"]
    with pytest.raises(q.QueueError):
        q.submit(root, ["upscale"], "", {"output_stem": str(tmp_path / "u")}, {}, kind=q.KIND_UPSCALE)
    with pytest.raises(q.QueueError):
        q.submit(root, ["upscale", "--project", "x"], "", {"output_stem": str(tmp_path / "u")}, {})
```
`web.py:2020-2025` (`_project_active_job`, `web.py:1988`), после ветки `song_job`:
```python
    upscale_job = _project_job_by_args(jobs, proj.path, q.KIND_UPSCALE)
    if upscale_job is not None:
        return {"kind": "upscale", "job": upscale_job.as_dict()}
```

`worker.py`:
```python
def _run_upscale_job(root, outdir, job) -> tuple[int, str]:
    """spec §4.2: one job per project -- acquire ltx (the dispatcher stops our H3 and starts our
    ComfyUI), then every part with one strength."""
    from h3_48gb.engines import ltx

    dispatcher = dispatcher_client.DispatcherClient()
    try:
        try:
            reason = make_gpu_gate(root, "ltx", client=dispatcher)(job)
        except GpuEngineFailed as exc:
            return 1, f"движок не поднялся: {exc.reason}; лог: {exc.log}\n"
        if reason:
            return 1, f"ltx: {reason}\n"
        client = ltx.ComfyClient(os.environ.get("H3_COMFY_URL", ltx.DEFAULT_COMFY_URL))
        return ltx.run_upscale(
            _project_arg(job.args), client=client,
            comfy_output=os.environ.get("H3_COMFY_OUTPUT_DIR", ltx.DEFAULT_COMFY_OUTPUT),
            run=subprocess.run,
            # unique per *run*, not per job: a job resumed after a restart is the same job id,
            # and its second attempt must never read frames the first attempt left behind
            attempt=f"{job.id}-{time.time_ns()}",
            cancelled=lambda: q.cancel_reason(root, job.id))
    finally:
        if q.cancel_reason(root, job.id) == "released_by_user":
            try:
                dispatcher.release()
            except dispatcher_client.DispatcherUnavailable:
                pass
```
`run_job`, вторая ветка: `elif job.kind == q.KIND_UPSCALE: exit_code, log_text = _run_upscale_job(root, outdir, job)`; после `q.finish` — `elif job.kind in (q.KIND_SONG, q.KIND_ASSEMBLE, q.KIND_UPSCALE): _advance_project_after_job(...)`.

Тест на воркер — дописать в `tests/test_ltx_upscale.py`:
```python
def test_the_upscale_job_asks_the_dispatcher_for_ltx(tmp_path, monkeypatch):
    from h3_48gb import queue as q
    from h3_48gb import worker
    from _fake_dispatcher import FakeDispatcher

    monkeypatch.setenv("H3_ENGINE", "sglang")
    disp = FakeDispatcher(acquire=({"ok": True, "state": "ready", "engine": "ltx"},))
    out = tmp_path / "out"
    proj = p.create_project(out, "video", "Up")
    clip = _clip(proj.path.parent / "scenes" / "s.mp4")
    proj.scenes = [{"idx": 0, "prompt": "x", "duration": 1.0, "status": "done", "job_id": "j",
                    "clip_path": str(clip), "keyframe_path": None}]
    proj.save()
    comfy = FakeComfy(tmp_path / "co", frames=(25,))
    monkeypatch.setenv("H3_DISPATCHER_URL", disp.url)
    monkeypatch.setenv("H3_COMFY_URL", comfy.url)
    monkeypatch.setenv("H3_COMFY_OUTPUT_DIR", str(tmp_path / "co"))
    root = q.layout(out / "queue")["root"]
    q.submit(root, ["upscale", "--project", str(proj.path)], "", {"output_stem": str(out / "u")},
             {}, kind=q.KIND_UPSCALE)
    job_id = q.scan(root)[0][0].id
    try:
        code = worker.run_job(root, q.claim(root), outdir=out)
    finally:
        disp.close()
        comfy.close()
    assert code == 0
    assert [c[2] for c in disp.calls if c[1] == "/acquire"] == [{"engine": "ltx"}]
    assert p.load_project(proj.path).scenes[0]["ltx_path"] == str(clip.with_name("s-ltx.mp4"))
    (prefix,) = [wf["42"]["inputs"]["filename_prefix"] for wf in comfy.prompts]
    import re
    assert re.fullmatch(rf"h3panel/{proj.id}/s-{re.escape(job_id)}-\d+/f", prefix), prefix
```

- [ ] **Step 8: Зелёный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_ltx_upscale.py tests/test_project.py tests/test_queue.py tests/test_worker.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 9: Мутации**

(а) В `run_upscale` считать силу внутри цикла по одной части (`motion.clip_motion([clip], ...)`) → `test_one_strength_for_the_whole_clip` FAIL (`[0.6, 0.15] != [x, x]`). (б) В `upscale_part` убрать проверку числа кадров → `test_upscale_prefix_is_unique...` FAIL `DID NOT RAISE`. (в) Префикс без `attempt` (`f"{PREFIX_ROOT}/{proj.id}/{clip.stem}"`) → `test_one_strength_for_the_whole_clip` FAIL на строке `filename_prefix`. (г) `pad_frames` → `((n + 7) // 8) * 8 + 1` → `test_pad_frames_is_8k_plus_1_like_run_ltx_sh` FAIL. (д) `SEED = 7` → `test_build_workflow_changes_exactly_five_inputs` FAIL (ожидание 42 записано в тесте явно). (е) Убрать `shutil.rmtree(frames_dir, ...)` → `test_upscale_part_pads_uploads_waits_and_muxes` FAIL на `assert not (... / "h3-s0-896x512-a1").exists()`. (ж) `attempt=job.id` вместо `f"{job.id}-{time.time_ns()}"` → `test_the_upscale_job_asks_the_dispatcher_for_ltx` FAIL на `re.fullmatch`. Ошибки — в отчёт.

- [ ] **Step 10: Полный прогон и commit**

```bash
env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest -q -p no:cacheprovider
git add h3_48gb/engines/motion.py h3_48gb/engines/ltx.py h3_48gb/engines/ltx_workflow.json h3_48gb/project.py h3_48gb/queue.py h3_48gb/worker.py pyproject.toml tests/_fake_comfy.py tests/test_ltx_upscale.py tests/test_project.py
git commit -m "feat(ltx): апскейл LTX через HTTP ComfyUI — шаблон в репо, одна сила на клип, 8k+1, -ltx.mp4"
```

---

## Task 11: Маршрут проекта `route` и сборка из `-ltx`

**Files:**
- Modify: `h3_48gb/project.py` — `ROUTE_STAGES`, `OPTIONAL_ROUTE_STAGES`, `default_route`, проверка в `_validate_shape` (`project.py:322-370`), атрибут `Project.route`, `Project.route_enabled`, `Project.set_route_stage`, `create_project`, `_OWNED_TOP_LEVEL_FIELDS`
- Modify: `h3_48gb/assemble.py` — `_submit_upscale`; `advance_project` (`assemble.py:1512-1519`); выбор частей в `run` (`assemble.py:745-751`)
- Modify: `h3_48gb/project.py:695-748` — `invalidate_scene_chain` сбрасывает `stages.upscale` в `draft` и убирает `ltx_path` у сброшенных сцен; `_ASSEMBLY_FIELDS` += `"draft_path"`
- Modify: `h3_48gb/assemble.py` — `run(..., draft=False)`: черновая сборка из исходных частей в `assembly/draft.mp4`
- Modify: `h3_48gb/worker.py:490-552` — `_run_assemble_job` передаёт `draft="--draft" in job.args`; падение черновой сборки не помечает `stages.assembly` как `failed`
- Modify: `h3_48gb/web.py` — `PUT /api/projects/<id>/route`, `POST /api/projects/<id>/upscale/retry`, `POST /api/projects/<id>/assembly/draft`
- Test: `tests/test_route.py` (новый)

**Interfaces:**
- Consumes: `engine.is_sglang()`; `q.KIND_UPSCALE`, этап `upscale`, `scene["ltx_path"]` (задача 10).
- Produces:
  - `project.ROUTE_STAGES = ("scenario", "scenes", "upscale", "track", "assemble")`, `project.OPTIONAL_ROUTE_STAGES = ("upscale",)`
  - `project.default_route(kind: str) -> list[dict]` — `[{"stage", "enabled"}]`: video → scenario, scenes, upscale, assemble; clip → scenario, scenes, upscale, track, assemble; song → track
  - `Project.route: list[dict]`, `Project.route_enabled(stage) -> bool`, `Project.set_route_stage(stage, enabled: bool) -> Project`
  - `assemble._submit_upscale(proj, queue_root, *, submit) -> dict` (`{"action": "submitted_upscale", "job_id"}`)
  - `PUT /api/projects/<id>/route {"upscale": bool}` → `{"ok": true, "project": <payload>}`; `POST /api/projects/<id>/upscale/retry` → `{"ok": true, "advance": {...}}`; `POST /api/projects/<id>/assembly/draft` → `{"ok": true, "job_id"}` (задача `["assemble", "--project", <path>, "--draft"]`)
  - `assemble.run(project_path, *, run=subprocess.run, log=None, draft: bool = False) -> Path`; черновая: всегда из `clip_path`, результат `assembly/draft.mp4`, пишется `assembly.draft_path`, `stages.assembly` и артефакты проекта не трогаются
- Решение по черновой сборке (после ревью): черновая сборка из исходных частей доступна всегда отдельным действием «Черновая сборка»; при выключенном апскейле обычная сборка и так идёт из исходных; финальная при включённом — из `-ltx`.
- Пересъёмка после апскейла: `invalidate_scene_chain` (кнопка «пересчитать сцену») делает старые `-ltx` недействительными — `stages.upscale = "draft"`, `ltx_path` сброшенных сцен удаляется; когда сцены снова `done`, апскейл ставится заново и сборка не возьмёт старые части.
- Правила: неизвестный этап или этап не своего `kind` в `route` — ошибка загрузки проекта (`ProjectNotFound`, как любой испорченный `project.json`), не тихий пропуск. Проект без `route` получает `default_route(kind)` при чтении. В v1 выключается только `upscale`. Апскейл исполняется только при `H3_ENGINE=sglang` — на Маке включённый этап пропускается (ComfyUI там нет), сборка идёт из исходных частей.

- [ ] **Step 1: Падающие тесты**

`tests/test_route.py`:
```python
"""The project route (spec §3.3.8) and assembly from the -ltx parts (spec §4.2.4)."""
import json
import subprocess
import warnings

import pytest

from h3_48gb import assemble
from h3_48gb import project as p
from h3_48gb import queue as q
from test_web import _call, _serve


def test_default_routes_per_kind():
    assert p.default_route("video") == [
        {"stage": "scenario", "enabled": True}, {"stage": "scenes", "enabled": True},
        {"stage": "upscale", "enabled": True}, {"stage": "assemble", "enabled": True}]
    assert p.default_route("clip") == [
        {"stage": "scenario", "enabled": True}, {"stage": "scenes", "enabled": True},
        {"stage": "upscale", "enabled": True}, {"stage": "track", "enabled": True},
        {"stage": "assemble", "enabled": True}]
    assert p.default_route("song") == [{"stage": "track", "enabled": True}]


def _rewrite(proj, mutate):
    data = json.loads(proj.path.read_text())
    mutate(data)
    proj.path.write_text(json.dumps(data))


def test_an_old_project_without_route_gets_its_kind_default(tmp_path):
    proj = p.create_project(tmp_path, "clip", "Old")
    _rewrite(proj, lambda d: d.pop("route"))
    assert p.load_project(proj.path).route == p.default_route("clip")


@pytest.mark.parametrize("route", [
    [{"stage": "teleport", "enabled": True}],
    [{"stage": "track", "enabled": True}],          # track on a video project
    [{"stage": "upscale"}],                          # no `enabled`
    "upscale"])
def test_a_bad_route_is_a_load_error_not_a_silent_skip(tmp_path, route):
    proj = p.create_project(tmp_path, "video", "Bad")
    _rewrite(proj, lambda d: d.__setitem__("route", route))
    with pytest.raises(p.ProjectNotFound):
        p.load_project(proj.path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert p.list_projects(tmp_path) == []
    assert len(caught) == 1


def test_only_upscale_can_be_switched(tmp_path):
    proj = p.create_project(tmp_path, "video", "T")
    proj.set_route_stage("upscale", False)
    reloaded = p.load_project(proj.path)
    assert (reloaded.route_enabled("upscale"), reloaded.route_enabled("scenes")) == (False, True)
    with pytest.raises(p.ProjectError):
        proj.set_route_stage("scenes", False)


class _Job:
    def __init__(self, job_id):
        self.id = job_id


def _done_project(tmp_path, *, upscale_stage="draft"):
    proj = p.create_project(tmp_path / "out", "video", "Done")
    pdir = proj.path.parent
    scenes = []
    for i in range(2):
        clip = pdir / "scenes" / f"s{i}.mp4"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"raw")
        ltx = pdir / "scenes" / f"s{i}-ltx.mp4"
        ltx.write_bytes(b"ltx")
        scenes.append({"idx": i, "prompt": "x", "duration": 1.0, "status": "done",
                       "job_id": f"j{i}", "clip_path": str(clip), "keyframe_path": None,
                       "head_drop_frames": 0, "ltx_path": str(ltx)})
    proj.scenes = scenes
    proj.stages.update({"scenes": "done", "upscale": upscale_stage})
    proj.save()
    return proj


def _submits():
    calls = []

    def submit(root, args, note, report, estimate, kind):
        calls.append((args, kind))
        return _Job(f"j{len(calls)}")
    return calls, submit


def test_on_sglang_done_scenes_submit_one_upscale_job_then_assembly(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path)
    calls, submit = _submits()
    assert assemble.advance_project(proj, tmp_path / "q", tmp_path / "out", submit=submit) == \
        {"action": "submitted_upscale", "job_id": "j1"}
    assert calls == [(["upscale", "--project", str(proj.path)], q.KIND_UPSCALE)]
    assert p.load_project(proj.path).stages["upscale"] == "running"
    assert assemble.advance_project(proj, tmp_path / "q", tmp_path / "out", submit=submit) == \
        {"action": "nothing_to_do"}
    p.load_project(proj.path).set_stage_status("upscale", "done")
    assert assemble.advance_project(proj, tmp_path / "q", tmp_path / "out",
                                    submit=submit)["action"] == "submitted_assembly"
    assert calls[-1][1] == q.KIND_ASSEMBLE


def test_on_mlx_an_enabled_upscale_is_skipped(tmp_path):
    proj = _done_project(tmp_path)
    calls, submit = _submits()
    assert assemble.advance_project(proj, tmp_path / "q", tmp_path / "out",
                                    submit=submit)["action"] == "submitted_assembly"


def test_a_disabled_upscale_goes_straight_to_assembly(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path)
    proj.set_route_stage("upscale", False)
    calls, submit = _submits()
    assert assemble.advance_project(proj, tmp_path / "q", tmp_path / "out",
                                    submit=submit)["action"] == "submitted_assembly"


def _assembled_inputs(proj, monkeypatch):
    seen = []

    def run(cmd, capture_output=True, text=True):
        if "concat" in cmd:
            listing = open(cmd[cmd.index("-i") + 1], encoding="utf-8").read()
            seen.append([line[len("file '"):-1] for line in listing.splitlines()])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assemble.run(proj.path, run=run, log=lambda line: None)
    return seen


def test_assembly_after_upscale_uses_the_ltx_parts(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path, upscale_stage="done")
    assert _assembled_inputs(proj, monkeypatch) == [[s["ltx_path"] for s in proj.scenes]]


def test_assembly_with_upscale_off_uses_the_raw_parts(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path, upscale_stage="done")
    proj.set_route_stage("upscale", False)
    assert _assembled_inputs(proj, monkeypatch) == [[s["clip_path"] for s in proj.scenes]]


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    outdir = tmp_path / "out"
    outdir.mkdir()
    server = _serve(q.layout(outdir / "queue")["root"], outdir)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def test_put_route_switches_upscale_and_continues_the_project(live, tmp_path):
    proj = _done_project(tmp_path)
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/route", {"upscale": False})
    assert status == 200, body
    assert body["project"]["route"][2] == {"stage": "upscale", "enabled": False}
    assert [job.kind for job in q.scan(live.queue_root)[0]] == ["assemble"]


def test_put_route_refuses_anything_but_upscale(live, tmp_path):
    proj = _done_project(tmp_path)
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/route", {"scenes": False})
    assert (status, body["error"]["code"]) == (400, "args_invalid")


def test_retry_a_failed_upscale(live, tmp_path):
    proj = _done_project(tmp_path, upscale_stage="failed")
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/upscale/retry", {})
    assert status == 200, body
    assert body["advance"]["action"] == "submitted_upscale"
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/upscale/retry", {})
    assert (status, body["error"]["code"]) == (409, "project_stage_not_ready")


def test_a_reshoot_after_upscale_invalidates_the_ltx_parts(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path, upscale_stage="done")
    proj.invalidate_scene_chain(1)
    reloaded = p.load_project(proj.path)
    assert reloaded.stages["upscale"] == "draft"
    assert ["ltx_path" in s for s in reloaded.scenes] == [True, False]
    clip = proj.path.parent / "scenes" / "s1-new.mp4"
    clip.write_bytes(b"raw2")
    reloaded.set_scene_status(1, "done", job_id=None, clip_path=str(clip))
    calls, submit = _submits()
    assert assemble.advance_project(reloaded, tmp_path / "q", tmp_path / "out",
                                    submit=submit)["action"] == "submitted_upscale"
    assert [kind for _, kind in calls] == [q.KIND_UPSCALE]


def test_draft_assembly_always_uses_the_raw_parts(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _done_project(tmp_path, upscale_stage="done")
    seen = []

    def run(cmd, capture_output=True, text=True):
        if "concat" in cmd:
            listing = open(cmd[cmd.index("-i") + 1], encoding="utf-8").read()
            seen.append([line[len("file '"):-1] for line in listing.splitlines()])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    out = assemble.run(proj.path, run=run, log=lambda line: None, draft=True)
    reloaded = p.load_project(proj.path)
    assert out == proj.path.parent / "assembly" / "draft.mp4"
    assert seen == [[s["clip_path"] for s in proj.scenes]]
    assert (reloaded.assembly["draft_path"], reloaded.stages["assembly"]) == (str(out), "draft")


def test_draft_route_queues_a_draft_assembly(live, tmp_path):
    proj = _done_project(tmp_path, upscale_stage="running")
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/assembly/draft", {})
    assert status == 200, body
    (job,) = q.scan(live.queue_root)[0]
    assert (job.kind, job.args, body["job_id"]) == (
        q.KIND_ASSEMBLE, ["assemble", "--project", str(proj.path), "--draft"], job.id)
```
(`_done_project` создаёт проект в `tmp_path / "out"` — тот же каталог, что `outdir` фикстуры `live`.)

- [ ] **Step 2: Красный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_route.py -q -p no:cacheprovider`
Expected: FAIL — `AttributeError: module 'h3_48gb.project' has no attribute 'default_route'`.

- [ ] **Step 3: `project.py`**

```python
#: spec §3.3.8: the stages a project's pipeline may include, in order, each with an on/off flag.
#: `track` exists only for clip/song. Only `upscale` may be switched off in v1.
ROUTE_STAGES = ("scenario", "scenes", "upscale", "track", "assemble")
OPTIONAL_ROUTE_STAGES = ("upscale",)
_ROUTE_BY_KIND = {"video": ("scenario", "scenes", "upscale", "assemble"),
                  "clip": ("scenario", "scenes", "upscale", "track", "assemble"),
                  "song": ("track",)}


def default_route(kind: str) -> list[dict]:
    return [{"stage": stage, "enabled": True} for stage in _ROUTE_BY_KIND[kind]]


def _check_route(path, kind, route) -> None:
    allowed = _ROUTE_BY_KIND.get(kind, ())
    if not isinstance(route, list):
        raise ProjectNotFound(f"{path}: 'route' must be a list, got {type(route).__name__}")
    for entry in route:
        if (not isinstance(entry, dict) or set(entry) != {"stage", "enabled"}
                or not isinstance(entry["enabled"], bool)):
            raise ProjectNotFound(f"{path}: route entry {entry!r} is not {{stage, enabled}}")
        if entry["stage"] not in ROUTE_STAGES or entry["stage"] not in allowed:
            raise ProjectNotFound(f"{path}: route stage {entry['stage']!r} is unknown for "
                                  f"kind={kind!r} (known: {allowed})")
```
В `_validate_shape` в конце: `if "route" in data: _check_route(path, data["kind"], data["route"])`. `_OWNED_TOP_LEVEL_FIELDS` += `"route"`. В `_apply`: `self.route = [dict(e) for e in data["route"]] if "route" in data else default_route(self.kind)`. `as_dict`: `"route": [dict(e) for e in self.route],`. `create_project`: `"route": default_route(kind),`. Методы:
```python
    def route_enabled(self, stage: str) -> bool:
        return any(entry["stage"] == stage and entry["enabled"] for entry in self.route)

    def set_route_stage(self, stage: str, enabled: bool) -> "Project":
        if stage not in OPTIONAL_ROUTE_STAGES:
            raise ProjectError(f"route stage {stage!r} cannot be switched in v1; only "
                               f"{OPTIONAL_ROUTE_STAGES}")
        with _project_lock(self.path.parent, exclusive=True):
            data = _read_data(self.path)
            route = data.get("route") or default_route(data["kind"])
            for entry in route:
                if entry["stage"] == stage:
                    entry["enabled"] = bool(enabled)
            data["route"] = route
            write_json_durably(self.path, data)
            self._apply(data)
        return self
```
Если `tests/test_project.py` пиннит ключи `project.json` — дописать `"route"` в ожидание (законное расширение).

- [ ] **Step 4: `assemble.py`**

```python
def _submit_upscale(proj, queue_root, *, submit) -> dict:
    """spec §4.2: one upscale job per project, once every scene is done and the route has it on.
    Idempotent on `stages.upscale` exactly like `_submit_assembly` is on `stages.assembly`."""
    if proj.stages.get("upscale") != "draft":
        return {"action": "nothing_to_do"}
    output_stem = str(proj.path.parent / "upscale" / "job-upscale")
    job = submit(queue_root, ["upscale", "--project", str(proj.path)],
                 f"upscale project {proj.id}", {"output_stem": output_stem}, {},
                 kind=q.KIND_UPSCALE)
    proj.set_stage_status("upscale", "running")
    return {"action": "submitted_upscale", "job_id": job.id}
```
В `advance_project`, ветка «все сцены done» (`assemble.py:1512-1519`):
```python
    if proj.scenes and all(scene.get("status") == "done" for scene in proj.scenes):
        if proj.stages.get("scenes") != "done":
            proj.set_stage_status("scenes", "done")
        if engine.is_sglang() and proj.route_enabled("upscale"):
            if proj.stages.get("upscale") == "draft":
                return _submit_upscale(proj, queue_root, submit=submit)
            if proj.stages.get("upscale") != "done":
                return {"action": "nothing_to_do"}
        return _submit_assembly(proj, queue_root, submit=submit)
```
В `run`, сбор `clip_paths` (`assemble.py:745-751`):
```python
    use_ltx = (not draft and engine.is_sglang() and proj.route_enabled("upscale")
               and proj.stages.get("upscale") == "done")
    clip_paths = []
    for scene in scenes:
        clip_path = scene.get("ltx_path") if use_ltx else scene.get("clip_path")
        if not clip_path:
            raise AssembleError(f"scene {scene['idx']} of {proj.id!r} is done but has no "
                                f"{'ltx_path' if use_ltx else 'clip_path'}")
        clip_paths.append(clip_path)
```
Черновая сборка — в том же `run`: сигнатура `run(project_path, *, run=subprocess.run, log=None, draft: bool = False)`; `final_path = assembly_dir / ("draft.mp4" if draft else "final.mp4")`; в конце вместо записи финала:
```python
    if draft:
        proj.update_assembly(draft_path=str(final_path))
        _cleanup_intermediate_assembly_files(assembly_dir)
        return final_path
    proj.update_assembly(final_path=str(final_path))
    proj.set_stage_status("assembly", "done")
    ...  # the rest unchanged (intermediate + project artifact cleanup)
```
`project.py`: `_ASSEMBLY_FIELDS = ("audio_mode", "final_path", "draft_path")`.

`project.py:invalidate_scene_chain` — в цикле по сброшенным сценам добавить `scene.pop("ltx_path", None)`, после `data["stages"]["assembly"] = "draft"` — `data["stages"]["upscale"] = "draft"`. Докстринг дополнить: «-ltx parts of the reset scenes are stale as soon as the raw part is: the whole clip is re-upscaled with one strength (spec §4.2.2), so the stage goes back to draft too».

`worker.py`, `_run_assemble_job`: `draft = "--draft" in job.args`; вызов `assemble.run(project_path, run=run, log=log_lines.append, draft=draft)`; оба `_mark_assembly_failed(project_path)` выполнять только при `not draft` (черновая не влияет на стадию сборки).

`web.py`, разбор `parts` в `_route_post`: `if len(parts) == 3 and parts[1] == "assembly" and parts[2] == "draft": return self._draft_project_assembly(parts[0])`;
```python
    def _draft_project_assembly(self, raw_id: str) -> tuple[int, str, bytes]:
        proj = self._load_project(raw_id)
        self._json_request(allowed=())
        if not proj.scenes or any(scene.get("status") != "done" for scene in proj.scenes):
            raise CliError("project_stage_not_ready",
                           f"черновая сборка проекта {raw_id}: не все сцены готовы", {"id": raw_id})
        output_stem = str(proj.path.parent / "assembly" / "job-draft")
        with queue_write_errors(self.server.queue_root, what="the draft assembly"):
            job = q.submit(self.server.queue_root, ["assemble", "--project", str(proj.path), "--draft"],
                           f"draft assemble project {proj.id}", {"output_stem": output_stem}, {},
                           kind=q.KIND_ASSEMBLE)
        return 200, "application/json", _json_bytes({"ok": True, "job_id": job.id})
```

- [ ] **Step 5: `web.py`**

`_route_put`: `if path.startswith("/api/projects/") and path.endswith("/route"): return self._put_project_route(path[len("/api/projects/"):-len("/route")])` (до ветки `/scenario`). `_route_post`, разбор `parts`: `if len(parts) == 3 and parts[1] == "upscale" and parts[2] == "retry": return self._retry_project_upscale(parts[0])`.
```python
    def _put_project_route(self, raw_id: str) -> tuple[int, str, bytes]:
        proj = self._load_project(raw_id)
        payload = self._json_request(allowed=("upscale",))
        if not isinstance(payload.get("upscale"), bool):
            raise CliError("args_invalid", "`upscale` must be true or false", {})
        proj.set_route_stage("upscale", payload["upscale"])
        assemble_module.advance_project(proj, self.server.queue_root, self.server.outdir)
        return 200, "application/json", _json_bytes(
            {"ok": True, "project": _project_payload(project_module.load_project(proj.path))})

    def _retry_project_upscale(self, raw_id: str) -> tuple[int, str, bytes]:
        proj = self._load_project(raw_id)
        self._json_request(allowed=())
        if proj.stages.get("upscale") != "failed":
            raise CliError("project_stage_not_ready",
                           f"апскейл проекта {raw_id} не упал (сейчас "
                           f"{proj.stages.get('upscale')!r})", {"id": raw_id})
        proj.set_stage_status("upscale", "draft")
        advance = assemble_module.advance_project(proj, self.server.queue_root, self.server.outdir)
        return 200, "application/json", _json_bytes({"ok": True, "advance": advance})
```
(`_json_request(allowed=("upscale",))` отвергает лишние ключи кодом `args_invalid`, `web.py:3385-3391` — на это рассчитан `test_put_route_refuses_anything_but_upscale`.)

- [ ] **Step 6: Зелёный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_route.py tests/test_project.py tests/test_assemble.py tests/test_web_projects.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 7: Мутации**

(а) В `_check_route` убрать проверку `entry["stage"] not in allowed` → `test_a_bad_route_is_a_load_error...[route1]` FAIL `DID NOT RAISE`. (б) В `advance_project` убрать `engine.is_sglang() and` → `test_on_mlx_an_enabled_upscale_is_skipped` FAIL (`submitted_upscale`). (в) В `run` всегда брать `clip_path` → `test_assembly_after_upscale_uses_the_ltx_parts` FAIL. (г) В `_submit_upscale` убрать проверку `!= "draft"` → `test_on_sglang_done_scenes_submit_one_upscale_job...` FAIL (второй вызов снова `submitted_upscale`). (д) В `invalidate_scene_chain` не сбрасывать `stages.upscale` → `test_a_reshoot_after_upscale_invalidates_the_ltx_parts` FAIL (`'done' == 'draft'`, затем `submitted_assembly`). (е) В `run` убрать `not draft and` → `test_draft_assembly_always_uses_the_raw_parts` FAIL (в списке `-ltx`). Ошибки — в отчёт.

- [ ] **Step 8: Полный прогон и commit**

```bash
env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest -q -p no:cacheprovider
git add h3_48gb/project.py h3_48gb/assemble.py h3_48gb/web.py h3_48gb/worker.py tests/test_route.py tests/test_project.py
git commit -m "feat(route): маршрут проекта с этапом апскейла; сборка из -ltx частей"
```

---

## Task 12: Минимум интерфейса: плашка GPU, «Освободить карту», Qwen, уведомления, галочка апскейла, референсы, @-теги

**Files:**
- Modify: `h3_48gb/webui/app.js` — новые экспортируемые чистые функции (верх файла, рядом с `formatGb`): `gpuBanner`, `notificationEvents`, `sceneTagIssues`, `tagSuggestions`, `projectRouteHtml`, `projectReferencesHtml`, `libraryCardsHtml`; проводка в DOM-половине: опрос `/api/gpu`, кнопки, уведомления, форма референсов, подсказка тегов, теги чата, скрытие «Показать в Finder»
- Modify: `h3_48gb/webui/index.html` — шапка (`index.html:32-80`, внутри `.rail-clock` перед `#theme-toggle`), секция «Референсы», поле тегов чата у `#chat-input` (`index.html:513`)
- Modify: `h3_48gb/webui/style.css` — стили плашки, правило скрытия reveal
- Modify: `h3_48gb/web.py` — `_gpu_state` получает `idle_release_at`
- Test: `tests/test_webui_panel.py` (новый)

**Interfaces:**
- Consumes (только JSON-API, спека §10): `GET /api/state` (`engine`, `platform`), `GET /api/gpu`, `POST /api/gpu/release`, `POST /api/qwen/unload|restore`, `GET/POST /api/library`, `PUT /api/library/<name>`, `GET/PUT /api/projects/<id>/references`, `PUT /api/projects/<id>/route`, `PUT /api/projects/<id>/settings`, `POST /api/projects/<id>/assembly/draft`, `POST /api/uploads`, `DELETE /api/jobs/<id>`, `POST /api/chat/<id>/message` с `tags`.
- Produces (`app.js`, все чистые, тестируются через node):
  - `gpuBanner(gpu, nowMs) -> {visible: bool, text: string, tone: "" | "wait" | "own" | "bad", qwenUnload: bool, qwenRestore: bool}`
  - `notificationEvents(prev, next, nowMs) -> {events: [{kind, title, body}], lastWaitNotifyMs}` — снимок `{failedIds: [], readyProjectIds: [], awaitingProjectIds: [], waitReason: string|null, waitingSinceMs: number|null, lastWaitNotifyMs: number|null}`; `prev === null` (первый снимок после открытия вкладки) — событий нет, только запоминание
  - `sceneTagIssues(text, pinnedTags, {needsTag = false} = {}) -> [{tag, problem: "unknown"|"invalid"|"missing"}]` (`missing` — ни одного тега, а сцена не клипа: спека §4.1.3)
  - `projectTagWarningsHtml(proj) -> string` — список сцен проекта, которые гейт отклонит
  - `projectSettingsHtml(proj) -> string` — поле `i2v_prefix` и кнопка «Черновая сборка»
  - `tagSuggestions(text, caret, cards) -> cards[]`
  - `projectRouteHtml(proj) -> string`, `projectReferencesHtml(proj, cards, pinned) -> string`, `libraryCardsHtml(cards, outdir) -> string`
- Плашка: у чужого процесса — «уже N мин» по `first_seen` диспетчера; «Вернуть Qwen» показывается только при пустой очереди (сервер в этом случае ещё и отвечает `409 queue_busy`, задача 8).
- Produces (`web.py`): в ответе `/api/gpu` ключ `idle_release_at` — ISO-время автоосвобождения (`max(finished_at) + H3_IDLE_RELEASE_MIN`), только когда очередь пуста и свой движок поднят; иначе `null`.
- Ограничение спеки §10: вёрстку старого интерфейса не улучшаем; новое — минимальные блоки, всё поведение — в JSON-API.

- [ ] **Step 1: Падающие тесты**

`tests/test_webui_panel.py`:
```python
"""The wave-1 UI minimum (spec §3.3.13-14, §3.5, §10): pure functions of app.js called through
node with exact expected values, plus source checks for the DOM wiring that has no pure seam."""
import json

import pytest

from h3_48gb import queue as q
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
    gpu = {"ok": True, "dispatcher_error": None, "idle_release_at": "2026-10-07T12:42:00Z",
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
                      " {stage: 'upscale', enabled: false}]})));")
    assert html == ('<label class="route-upscale"><input type="checkbox" class="route-upscale-box" '
                    'data-id="p1"> Апскейл LTX после всех сцен</label>')
    on = _node_eval("console.log(JSON.stringify(app.projectRouteHtml("
                    "{id: 'p1', route: [{stage: 'upscale', enabled: true}]})));")
    assert on == ('<label class="route-upscale"><input type="checkbox" class="route-upscale-box" '
                  'data-id="p1" checked> Апскейл LTX после всех сцен</label>')


@_needs_node
def test_project_tag_warnings_and_settings_html():
    warn = _node_eval("console.log(JSON.stringify(app.projectTagWarningsHtml({kind: 'video', "
                      "references: [{tag: '@alice', version: 1}], scenes: ["
                      "{idx: 0, prompt: '@alice runs'}, {idx: 1, prompt: 'a dog'}, "
                      "{idx: 2, prompt: '@bob waves'}]})));")
    assert warn == ('<ul class="tag-warnings"><li>Сцена 1: нужен хотя бы один референс (@тег) в сцене</li>'
                    '<li>Сцена 2: незнакомый тег @bob</li></ul>')
    assert _node_eval("console.log(JSON.stringify(app.projectTagWarningsHtml({kind: 'clip', "
                      "references: [], scenes: [{idx: 0, prompt: 'a dog'}]})));") == ""
    settings = _node_eval("console.log(JSON.stringify(app.projectSettingsHtml("
                          "{id: 'p1', i2v_prefix: 'Go <on>.'})));")
    assert settings == ('<div class="project-settings" data-id="p1"><label>Начало сцепленной сцены '
                        '(i2v_prefix) <textarea class="i2v-prefix" data-id="p1" rows="2">Go &lt;on&gt;.'
                        '</textarea></label> <button type="button" class="draft-assembly" '
                        'data-id="p1">Черновая сборка</button></div>')


@_needs_node
def test_project_references_html_marks_pinned_versions():
    cards = [{"tag": "@alice", "kind": "person", "version": 3, "latest_version": 3},
             {"tag": "@beach", "kind": "environment", "version": 1, "latest_version": 1}]
    pinned = [{"tag": "@alice", "version": 2}]
    html = _node_eval("console.log(JSON.stringify(app.projectReferencesHtml("
                      f"{{id: 'p1'}}, {json.dumps(cards)}, {json.dumps(pinned)})));")
    assert html == (
        '<div class="project-refs" data-id="p1"><h4>Референсы проекта</h4>'
        '<label><input type="checkbox" class="ref-pin" data-tag="@alice" checked> '
        '@alice <span class="muted">person, v2 (есть v3)</span></label>'
        '<label><input type="checkbox" class="ref-pin" data-tag="@beach"> '
        '@beach <span class="muted">environment</span></label></div>')


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


def test_gpu_state_reports_when_the_idle_card_will_be_released(live):
    server, root, tmp_path = live
    q.submit(root, ["generate", "--tag", "a"], "", {"output_stem": str(tmp_path / "h3-a")}, {})
    job = q.claim(root)
    q.finish(root, job.id, 0, "", finished_at="2026-10-07T10:00:00")
    status, body = _call(server, "GET", "/api/gpu")
    assert body["idle_release_at"] == "2026-10-07T10:15:00"
```

- [ ] **Step 2: Красный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_webui_panel.py -q -p no:cacheprovider`
Expected: FAIL — `TypeError: app.gpuBanner is not a function`; `KeyError: 'idle_release_at'`.

- [ ] **Step 3: Чистые функции в `app.js`**

После `formatGb`:
```js
/** Плашка GPU (спека §3.3.13–14): что сказать сверху на любой вкладке. */
export function gpuBanner(gpu, nowMs) {
  const hidden = { visible: false, text: "", tone: "", qwenUnload: false, qwenRestore: false };
  if (!gpu || !gpu.ok) return hidden;
  const d = gpu.dispatcher;
  if (!d) {
    return { ...hidden, visible: true, tone: "bad",
             text: `Диспетчер GPU не отвечает: ${gpu.dispatcher_error}` };
  }
  const run = gpu.queue && gpu.queue.running;
  // spec §2: «вернуть Qwen» only after the queue -- never while anything is queued or running
  const queueEmpty = !run && !(gpu.queue && gpu.queue.pending);
  const qwenRestore = Boolean(d.qwen && d.qwen.unloaded_by_us && !d.qwen.running && queueEmpty);
  if (run && run.wait_reason) {
    const since = Date.parse(run.started_at);
    const waited = Number.isFinite(since) ? Math.max(0, (nowMs - since) / 1000) : 0;
    const holders = (d.foreign || []).map((a) => {
      const held = Number.isFinite(a.first_seen)
        ? `, уже ${formatDuration(Math.max(0, nowMs / 1000 - a.first_seen))}` : "";
      return `${a.name} (pid ${a.pid}, ${formatGb(a.memory_mb / 1024)}${held})`;
    }).join(", ");
    const reason = run.wait_reason.replace(/^ждём GPU: /, "");
    return { visible: true, tone: "wait", qwenRestore,
             qwenUnload: run.wait_reason.includes("Qwen"),
             text: `Очередь стоит ${formatDuration(waited)}: ${reason}`
               + (holders ? ` — карту держит ${holders}` : "") };
  }
  const own = Object.keys(d.own || {});
  if (own.length && !run) {
    const names = own.map((n) => (n === "h3" ? "H3" : "ComfyUI")).join(", ");
    const at = Date.parse(gpu.idle_release_at || "");
    const when = Number.isFinite(at)
      ? `через ${Math.max(0, Math.ceil((at - nowMs) / 60000))} мин или кнопкой` : "кнопкой";
    return { visible: true, tone: "own", qwenUnload: false, qwenRestore,
             text: `Карту держит панель: ${names}, ${formatGb(d.gpu.memory_used_mb / 1024)} `
               + `— освободится ${when}` };
  }
  return { ...hidden, visible: qwenRestore, qwenRestore,
           text: qwenRestore ? "Qwen выгружен панелью — его можно вернуть" : "" };
}

const WAIT_NOTIFY_AFTER_MS = 10 * 60_000;
const WAIT_NOTIFY_EVERY_MS = 60 * 60_000;

/** Браузерные уведомления (спека §3.3.13): ожидание > 10 мин (повтор раз в час), сцена упала,
 *  проект готов, нужно решение. Каждое событие — один раз на переход. */
export function notificationEvents(prev, next, nowMs) {
  // The first snapshot after the tab opens only primes the state: everything already failed or
  // finished before the page was opened is not news.
  if (prev === null) {
    const primed = next.waitReason && next.waitingSinceMs !== null ? nowMs : next.lastWaitNotifyMs;
    return { events: [], lastWaitNotifyMs: primed };
  }
  const events = [];
  let lastWait = next.lastWaitNotifyMs;
  if (next.waitReason && next.waitingSinceMs !== null) {
    const waited = nowMs - next.waitingSinceMs;
    const due = lastWait === null ? waited >= WAIT_NOTIFY_AFTER_MS
                                  : nowMs - lastWait >= WAIT_NOTIFY_EVERY_MS;
    if (due) {
      events.push({ kind: "wait", title: `Очередь стоит ${formatDuration(waited / 1000)}`,
                    body: next.waitReason });
      lastWait = nowMs;
    }
  }
  const fresh = (key) => next[key].filter((id) => !prev[key].includes(id));
  for (const id of fresh("failedIds")) events.push({ kind: "failed", title: "Сцена упала", body: `задача ${id}` });
  for (const id of fresh("readyProjectIds")) events.push({ kind: "ready", title: "Проект готов", body: id });
  for (const id of fresh("awaitingProjectIds")) events.push({ kind: "decision", title: "Нужно решение", body: id });
  return { events, lastWaitNotifyMs: lastWait };
}

const TAG_IN_TEXT = /(?<![\w@.])@([A-Za-z0-9-]+)/g;
const TAG_OK = /^@[a-z0-9-]{2,32}$/;

/** @-теги сцены, которые не уйдут в H3 (спека §3.5): незнакомые проекту и написанные не так. */
export function sceneTagIssues(text, pinnedTags, { needsTag = false } = {}) {
  const issues = [];
  const seen = new Set();
  for (const match of String(text || "").matchAll(TAG_IN_TEXT)) {
    const tag = `@${match[1]}`;
    if (seen.has(tag)) continue;
    seen.add(tag);
    if (!TAG_OK.test(tag)) issues.push({ tag, problem: "invalid" });
    else if (!pinnedTags.includes(tag)) issues.push({ tag, problem: "unknown" });
  }
  if (needsTag && seen.size === 0) issues.push({ tag: null, problem: "missing" });
  return issues;
}

const TAG_PROBLEM_TEXT = {
  missing: () => "нужен хотя бы один референс (@тег) в сцене",
  unknown: (tag) => `незнакомый тег ${tag}`,
  invalid: (tag) => `тег ${tag} — только строчные`,
};

/** Сцены, которые гейт отклонит (спека §4.1.3, §3.5), — видно до нажатия «Утвердить». */
export function projectTagWarningsHtml(proj) {
  const pinned = (proj.references || []).map((ref) => ref.tag);
  const needsTag = proj.kind !== "clip";
  const rows = [];
  for (const scene of proj.scenes || []) {
    for (const issue of sceneTagIssues(scene.prompt, pinned, { needsTag })) {
      rows.push(`<li>Сцена ${scene.idx}: ${escapeHtml(TAG_PROBLEM_TEXT[issue.problem](issue.tag))}</li>`);
    }
  }
  return rows.length ? `<ul class="tag-warnings">${rows.join("")}</ul>` : "";
}

export function projectSettingsHtml(proj) {
  const id = escapeHtml(proj.id);
  return `<div class="project-settings" data-id="${id}"><label>Начало сцепленной сцены `
    + `(i2v_prefix) <textarea class="i2v-prefix" data-id="${id}" rows="2">`
    + `${escapeHtml(proj.i2v_prefix || "")}</textarea></label> `
    + `<button type="button" class="draft-assembly" data-id="${id}">Черновая сборка</button></div>`;
}

/** Подсказка на `@`: карточки, чей тег начинается с набранного после последнего `@` до каретки. */
export function tagSuggestions(text, caret, cards) {
  const head = String(text || "").slice(0, caret);
  const match = head.match(/(?:^|[^\w@.])@([a-z0-9-]*)$/);
  if (!match) return [];
  return cards.filter((card) => card.tag.startsWith(`@${match[1]}`));
}

export function projectRouteHtml(proj) {
  const entry = (proj.route || []).find((e) => e.stage === "upscale");
  if (!entry) return "";
  return `<label class="route-upscale"><input type="checkbox" class="route-upscale-box" `
    + `data-id="${escapeHtml(proj.id)}"${entry.enabled ? " checked" : ""}> `
    + `Апскейл LTX после всех сцен</label>`;
}

export function projectReferencesHtml(proj, cards, pinned) {
  const byTag = new Map(pinned.map((ref) => [ref.tag, ref.version]));
  const rows = cards.map((card) => {
    const version = byTag.get(card.tag);
    const note = version === undefined ? escapeHtml(card.kind)
      : `${escapeHtml(card.kind)}, v${version}`
        + (version < card.latest_version ? ` (есть v${card.latest_version})` : "");
    return `<label><input type="checkbox" class="ref-pin" data-tag="${escapeHtml(card.tag)}"`
      + `${version === undefined ? "" : " checked"}> ${escapeHtml(card.tag)} `
      + `<span class="muted">${note}</span></label>`;
  }).join("");
  return `<div class="project-refs" data-id="${escapeHtml(proj.id)}"><h4>Референсы проекта</h4>`
    + rows + `</div>`;
}

export function libraryCardsHtml(cards, outdir) {
  if (!cards.length) return '<p class="empty">Библиотека пуста</p>';
  return cards.map((card) => {
    const first = card.assets[0] || "";
    const thumb = first && /\.(png|jpe?g)$/i.test(first) && outdir && first.startsWith(`${outdir}/`)
      ? `<img src="/media/${escapeHtml(first.slice(outdir.length + 1))}" alt="">` : "";
    return `<div class="lib-card">${thumb}<b>${escapeHtml(card.tag)}</b> `
      + `<span class="muted">${escapeHtml(card.kind)}, v${card.version}</span>`
      + `<p>${escapeHtml(card.description)}</p></div>`;
  }).join("");
}
```
(Проверить, что `/media/<путь от outdir>` отдаёт `library/<name>/vN/NN-*.png` — `_media` принимает любую глубину внутри outdir, `web.py:5559`; если нет — превью не показывать, тест на HTML не меняется.)

- [ ] **Step 4: Проводка DOM в `app.js` и разметка**

`index.html`, внутри `.rail-clock` перед `#theme-toggle`:
```html
<span class="gpu-banner" id="gpu-banner" role="status" hidden></span>
<button class="ghost" type="button" id="gpu-release" hidden>Освободить карту</button>
<button class="ghost" type="button" id="qwen-unload" hidden>Выгрузить Qwen</button>
<button class="ghost" type="button" id="qwen-restore" hidden>Вернуть Qwen</button>
<button class="ghost" type="button" id="notify-enable" hidden>Уведомления</button>
```
Под `#chat-input` (`index.html:513`): `<input id="chat-tags" class="inp" placeholder="теги для сценария: @alice @beach">`. Новая секция в конце `<main>`: `<section id="library"><h2>Референсы</h2><div id="library-cards"></div><form id="library-form"><input id="lib-tag" placeholder="@tag" required> <select id="lib-kind"><option>person</option><option>object</option><option>environment</option><option>style</option><option>voice</option></select> <input id="lib-desc" placeholder="description (English)" required> <input id="lib-files" type="file" multiple accept=".png,.jpg,.jpeg,.mp3,.wav"> <button type="submit">Добавить</button><p id="lib-error" class="why" hidden></p></form></section>`.

`style.css`:
```css
.gpu-banner { padding: 2px 8px; border-radius: 4px; font-size: 0.9em; }
.gpu-banner[data-tone="wait"] { background: var(--warn-bg, #fff3cd); }
.gpu-banner[data-tone="own"] { background: var(--info-bg, #e7f1ff); }
.gpu-banner[data-tone="bad"] { background: var(--bad-bg, #f8d7da); }
.scenario-prompt.has-tag-issues { outline: 2px solid var(--bad, #c0392b); }
.tag-hint { font-size: 0.85em; }
body:not([data-platform="darwin"]) [data-act="reveal"] { display: none; }
```
DOM-половина `app.js` (внутри `if (typeof document !== "undefined")`-блока, рядом с `poll`):
```js
  let gpu = null;
  let notifySnapshot = null;   // null until the first poll: opening the tab is not an event
  let libraryCards = [];

  async function pollGpu() {
    if (!state || state.engine !== "sglang") { gpu = null; renderGpu(); return; }
    try { gpu = await api("GET", "/api/gpu"); } catch { gpu = null; }
    renderGpu();
    notifyFromState();
  }

  function renderGpu() {
    const sglang = Boolean(state && state.engine === "sglang");
    const banner = gpuBanner(gpu, Date.now());
    $("gpu-banner").hidden = !banner.visible;
    $("gpu-banner").textContent = banner.text;
    $("gpu-banner").dataset.tone = banner.tone;
    $("gpu-release").hidden = !sglang;
    $("qwen-unload").hidden = !banner.qwenUnload;
    $("qwen-restore").hidden = !banner.qwenRestore;
    $("notify-enable").hidden = !sglang || typeof Notification === "undefined"
      || Notification.permission !== "default";
  }

  function notifyFromState() {
    const jobs = allQueueJobs();
    const run = gpu && gpu.queue && gpu.queue.running;
    const next = {
      failedIds: jobs.filter((j) => j.state === "failed").map((j) => j.id),
      readyProjectIds: (projects || []).filter((p) => p.stages && p.stages.assembly === "done").map((p) => p.id),
      awaitingProjectIds: (projects || []).filter((p) => p.stages && Object.values(p.stages).includes("awaiting_approval")).map((p) => p.id),
      waitReason: run && run.wait_reason ? run.wait_reason : null,
      waitingSinceMs: run && run.wait_reason ? Date.parse(run.started_at) : null,
      lastWaitNotifyMs: run && run.wait_reason && notifySnapshot ? notifySnapshot.lastWaitNotifyMs : null,
    };
    const { events, lastWaitNotifyMs } = notificationEvents(notifySnapshot, next, Date.now());
    notifySnapshot = { ...next, lastWaitNotifyMs };
    if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
    for (const e of events) new Notification(e.title, { body: e.body });
  }

  async function releaseCard() {
    try {
      await api("POST", "/api/gpu/release", {});
    } catch (err) {
      const code = err.payload && err.payload.error && err.payload.error.code;
      if (code !== "release_needs_confirm") { alert(err.payload ? err.payload.error.message : String(err)); return; }
      if (!confirm(err.payload.error.message)) return;
      await api("POST", "/api/gpu/release", { confirm: true });
    }
    await poll();
  }

  async function qwenAction(route) {
    try { await api("POST", route, {}); }
    catch (err) { alert(err.payload ? err.payload.error.message : String(err)); }
    await pollGpu();
  }

  async function loadLibrary() {
    try { libraryCards = (await api("GET", "/api/library")).cards; } catch { libraryCards = []; }
    $("library-cards").innerHTML = libraryCardsHtml(libraryCards, state && state.outdir);
  }

  async function addLibraryCard(event) {
    event.preventDefault();
    $("lib-error").hidden = true;
    try {
      const assets = [];
      for (const file of $("lib-files").files) {
        const response = await fetch("/api/uploads", { method: "POST", body: file,
          headers: { "Content-Type": "application/octet-stream",
                     "X-Filename": encodeURIComponent(file.name) } });
        const body = await response.json();
        if (!response.ok) throw { payload: body };
        assets.push(body.path);
      }
      await api("POST", "/api/library", { tag: $("lib-tag").value.trim(),
        kind: $("lib-kind").value, description: $("lib-desc").value.trim(), assets });
      $("library-form").reset();
      await loadLibrary();
    } catch (err) {
      $("lib-error").textContent = err.payload ? err.payload.error.message : String(err);
      $("lib-error").hidden = false;
    }
  }
```
В `poll()` после `state = await api(...)`: `document.body.dataset.platform = state.platform;`, в конце `poll()`: `await pollGpu();`. Регистрация (рядом с `$("submit").addEventListener`):
```js
  $("gpu-release").addEventListener("click", releaseCard);
  $("qwen-unload").addEventListener("click", () => qwenAction("/api/qwen/unload"));
  $("qwen-restore").addEventListener("click", () => qwenAction("/api/qwen/restore"));
  $("notify-enable").addEventListener("click", () => Notification.requestPermission().then(renderGpu));
  $("library-form").addEventListener("submit", addLibraryCard);
```
и `loadLibrary()` после первого `poll()`.

Модалка проекта (`renderProjectModal`, `app.js:2671`, объект проекта — `project.project`, переменная `project` объявлена на `app.js:2308`): к сборке HTML добавить `projectTagWarningsHtml(proj) + projectSettingsHtml(proj) + projectRouteHtml(proj) + projectReferencesHtml(proj, libraryCards, proj.references || [])` перед `projectScenesStageHtml`. Общего `change`-обработчика модалки нет: поля модалки пересоздаются при каждой перерисовке, поэтому, как уже сделано для `.scenario-fresh-start` (`app.js:4697`), вешается **новый делегированный** обработчик на `document` рядом с ним:
```js
  document.addEventListener("change", (event) => {
    const target = event.target;
    if (!project || !project.project) return;
    if (target.classList.contains("route-upscale-box")) {
      api("PUT", `/api/projects/${encodeURIComponent(target.dataset.id)}/route`,
          { upscale: target.checked }).then(() => openProjectModal(target.dataset.id))
        .catch((err) => alert(err.payload ? err.payload.error.message : String(err)));
    }
    if (target.classList.contains("ref-pin")) {
      const box = target.closest(".project-refs");
      const refs = [...box.querySelectorAll(".ref-pin")].filter((el) => el.checked)
        .map((el) => ({ tag: el.dataset.tag }));
      api("PUT", `/api/projects/${encodeURIComponent(box.dataset.id)}/references`, { references: refs })
        .then(() => openProjectModal(box.dataset.id))
        .catch((err) => alert(err.payload ? err.payload.error.message : String(err)));
    }
  });

  document.addEventListener("focusout", (event) => {
    const field = event.target.closest(".i2v-prefix");
    if (!field || !project || !project.project) return;
    if (field.value === (project.project.i2v_prefix || "")) return;
    api("PUT", `/api/projects/${encodeURIComponent(field.dataset.id)}/settings`, { i2v_prefix: field.value })
      .then(() => openProjectModal(field.dataset.id))
      .catch((err) => alert(err.payload ? err.payload.error.message : String(err)));
  });

  document.addEventListener("click", (event) => {
    const button = event.target.closest(".draft-assembly");
    if (!button) return;
    api("POST", `/api/projects/${encodeURIComponent(button.dataset.id)}/assembly/draft`, {})
      .then(() => poll())
      .catch((err) => alert(err.payload ? err.payload.error.message : String(err)));
  });
```
(Отметка уже подключённой карточки отправляется без `version` — сервер закрепит последнюю; это и есть явная кнопка «обновить до последней версии»: снять и поставить галочку.)

Подсказка тегов. `input`-обработчика у `.scenario-prompt` нет (на `app.js:2994` — сбор значений в `collectScenarioScenes`, сохранение — делегированный `focusout` на `app.js:4679`); добавить **новый делегированный** `input` рядом с этим `focusout`:
```js
  document.addEventListener("input", (event) => {
    const el = event.target.closest(".scenario-prompt");
    if (!el || !project || !project.project) return;
      const pinned = (project.project.references || []).map((r) => r.tag);
      const issues = sceneTagIssues(el.value, pinned, { needsTag: project.project.kind !== "clip" });
      el.classList.toggle("has-tag-issues", issues.length > 0);
      el.title = issues.map((i) => TAG_PROBLEM_TEXT[i.problem](i.tag)).join("; ");
      const hint = tagSuggestions(el.value, el.selectionStart, libraryCards.filter((c) => pinned.includes(c.tag)));
      let box = el.nextElementSibling && el.nextElementSibling.classList.contains("tag-hint") ? el.nextElementSibling : null;
      if (!box) { box = document.createElement("div"); box.className = "tag-hint"; el.after(box); }
      box.textContent = hint.length ? `теги: ${hint.map((c) => c.tag).join(" ")}` : "";
  });
```
(В клипе подсказка `missing` не показывается: у его сцен аудио-референс — кусок трека.) Теги чата: в отправке сообщения чата к телу добавить `tags: ($("chat-tags").value.match(/@[a-z0-9-]{2,32}/g) || [])`.

- [ ] **Step 5: `idle_release_at` в web.py**

В `_gpu_state` перед ответом:
```python
        idle_release_at = None
        if running is None and pending == 0 and status and status.get("own"):
            jobs, _ = q.scan(self.server.queue_root)
            finished = [job.finished_at for job in jobs if job.finished_at]
            if finished:
                minutes = float(os.environ.get("H3_IDLE_RELEASE_MIN", "15"))
                last = datetime.fromisoformat(max(finished))
                idle_release_at = (last + timedelta(minutes=minutes)).isoformat(timespec="seconds")
```
и `"idle_release_at": idle_release_at` в словарь (проверить импорты `datetime`, `timedelta`). Тест задачи 8 `test_gpu_state_combines_dispatcher_and_queue` дополнить ключом `"idle_release_at": None` (в нём есть running-задача).

- [ ] **Step 6: Зелёный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_webui_panel.py tests/test_gpu_web.py tests/test_web.py tests/test_web_projects.py -q -p no:cacheprovider`
Expected: PASS (включая `test_the_page_asks_for_its_own_routes_in_a_way_the_provenance_check_accepts` и `.mjs`-проверки `test_web_projects.py`).

- [ ] **Step 7: Мутации**

(а) В `gpuBanner` убрать `.replace(/^ждём GPU: /, "")` → `test_banner_while_waiting_...` FAIL. (б) `WAIT_NOTIFY_EVERY_MS = 30 * 60_000` → `test_notifications_wait_over_ten_minutes_then_hourly` FAIL (событие на 59-й минуте). (в) В `notificationEvents` не фильтровать уже виденные (`next[key]` вместо `fresh(key)`) → `test_notifications_failed_ready_and_decision_fire_once` FAIL на втором вызове. (г) Убрать lookbehind в `TAG_IN_TEXT` → `test_scene_tag_issues_and_suggestions` FAIL (`@x` из адреса). (д) В `projectReferencesHtml` проверять `version <= card.latest_version` → FAIL у `@alice` (появится лишняя «есть v3» у актуальной). (е) Убрать ветку `if (prev === null)` → `test_the_first_snapshot_after_opening_the_tab_notifies_nothing` FAIL (события `failed`/`ready`/`wait`). (ж) Убрать `queueEmpty` из `qwenRestore` → `test_banner_offers_qwen_restore_only_with_an_empty_queue` FAIL. (з) В `projectRouteHtml` потерять `checked` → вторая половина `test_project_route_html_is_one_checkbox...` FAIL. (и) Убрать `needsTag && seen.size === 0` → `test_project_tag_warnings_and_settings_html` FAIL (нет «Сцена 1»). Ошибки — в отчёт.

- [ ] **Step 8: Ручная проверка в браузере (Мак, без GPU)**

Поднять панель локально с фейковыми движками нельзя без кода тестов; достаточно Мак-режима и sglang-режима без диспетчера:
```bash
mkdir -p ~/Research/TestVideo/_panel-ui && H3_ENGINE=sglang H3_DISPATCHER_URL=http://127.0.0.1:9 \
  ~/venvs/h3-panel/bin/python -m h3_48gb web --outdir ~/Research/TestVideo/_panel-ui --port 8799
```
Открыть `http://127.0.0.1:8799`: плашка «Диспетчер GPU не отвечает…», кнопка «Освободить карту» видна, «Показать в Finder» скрыта только если `platform` не `darwin` (на Маке видна), секция «Референсы» добавляет карточку из png. Затем без `H3_ENGINE`: плашки и GPU-кнопок нет. Снимки экрана — в отчёт. Остановить сервер.

- [ ] **Step 9: Полный прогон и commit**

```bash
env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest -q -p no:cacheprovider
git add h3_48gb/webui h3_48gb/web.py tests/test_webui_panel.py tests/test_gpu_web.py
git commit -m "feat(ui): минимум волны 1 — плашка GPU, «Освободить карту», Qwen, уведомления, апскейл, референсы, @-теги"
```

---

## Task 13: Docker: образ панели, compose, entrypoint, тесты в контейнере

**Files:**
- Create: `Dockerfile`, `compose.yaml`, `docker/entrypoint.sh`, `.dockerignore`
- Test: `tests/test_docker_files.py` (новый)

**Interfaces:**
- Consumes: `requirements-panel.txt` (задача 0); `h3 web --host` + `H3_ALLOWED_HOSTS` (задача 1); все переменные `H3_*` задач 2–12.
- Produces: образ `h3-panel:latest`; `docker compose up -d` поднимает `h3 web --host 0.0.0.0 --port 8765` и `h3 worker` в одном контейнере; контейнер завершается, если умер любой из двух (код умершего).
- Решения: пакет ставится **editable** (`pip install --no-deps -e .`), а не `pip install .`: `provider.system_prompt()` читает `docs/h3-prompt-system.md` относительно исходников (`provider.py:236-242`), `web.REPO_ROOT` — корень репозитория; обычная установка в site-packages потеряла бы оба. entrypoint — POSIX `sh` без `wait -n` (его нет в bash 3.2 на Маке, где гоняется тест).

- [ ] **Step 1: Падающие тесты**

`tests/test_docker_files.py`:
```python
"""The panel image (spec §3.2): what goes in, what stays out, and the entrypoint's one promise --
the container exits when either process dies."""
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _lines(name):
    return (ROOT / name).read_text(encoding="utf-8").splitlines()


def test_dockerfile_builds_the_panel_without_mlx():
    lines = _lines("Dockerfile")
    assert lines[0] == "FROM python:3.12-slim"
    assert "    && apt-get install -y --no-install-recommends ffmpeg nodejs \\" in lines
    assert "RUN pip install --no-cache-dir -r requirements-panel.txt" in lines
    assert "RUN pip install --no-cache-dir --no-deps -e ." in lines
    assert "USER 1000:1000" in lines
    assert 'ENTRYPOINT ["/app/docker/entrypoint.sh"]' in lines
    text = "\n".join(lines).lower()
    assert "mlx" not in text and "cuda" not in text and "opencv" not in text


def test_compose_mounts_host_paths_at_the_same_paths_and_sets_the_spec_env():
    lines = [line.strip() for line in _lines("compose.yaml")]
    for expected in (
            "network_mode: host", 'user: "1000:1000"', "restart: unless-stopped",
            "- /home/alex/Outputs/h3-panel:/home/alex/Outputs/h3-panel",
            "- /home/alex/Outputs/comfy/output:/home/alex/Outputs/comfy/output:ro",
            "- /home/alex/Outputs/comfy/output/h3panel:/home/alex/Outputs/comfy/output/h3panel",
            "- /home/alex/Projects/h3-bench/inputs:/home/alex/Projects/h3-bench/inputs:ro",
            "H3_ENGINE: sglang", "H3_OUTDIR: /home/alex/Outputs/h3-panel",
            'H3_ALLOWED_HOSTS: "192.168.100.50:8765,alex-neuro:8765"',
            "H3_SGLANG_URL: http://127.0.0.1:30020", "H3_COMFY_URL: http://127.0.0.1:8188",
            "H3_DISPATCHER_URL: http://127.0.0.1:8790",
            "H3_COMFY_OUTPUT_DIR: /home/alex/Outputs/comfy/output",
            'H3_IDLE_RELEASE_MIN: "15"'):
        assert expected in lines, expected


def test_dockerignore_keeps_the_context_small_but_keeps_what_the_panel_reads():
    ignored = set(_lines(".dockerignore"))
    assert {".git", "**/__pycache__", "*.egg-info", ".pytest_cache", "reference"} <= ignored
    assert not ({"docs", "prompts", "upstream", "tests", "h3_48gb"} & ignored)


def _stub_python(tmp_path, worker_exit: int) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "python"
    stub.write_text("#!/bin/sh\n"
                    'echo "$@" >> "$STUB_LOG"\n'
                    'case "$3" in\n'
                    "  web) exec sleep 30 ;;\n"
                    f"  worker) sleep 1; exit {worker_exit} ;;\n"
                    "esac\n")
    stub.chmod(0o755)
    return bin_dir


def test_entrypoint_starts_both_and_exits_with_the_dead_ones_code(tmp_path):
    bin_dir = _stub_python(tmp_path, 3)
    log = tmp_path / "calls.log"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "STUB_LOG": str(log),
           "H3_OUTDIR": str(tmp_path / "out")}
    started = time.monotonic()
    result = subprocess.run([str(ROOT / "docker" / "entrypoint.sh")], env=env, timeout=20)
    assert result.returncode == 3
    assert time.monotonic() - started < 10, "the web stub (sleep 30) was not stopped"
    assert sorted(log.read_text().splitlines()) == sorted([
        f"-m h3_48gb web --host 0.0.0.0 --port 8765 --outdir {tmp_path / 'out'}",
        f"-m h3_48gb worker --outdir {tmp_path / 'out'}"])


def test_entrypoint_refuses_to_start_without_an_outdir(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "H3_OUTDIR"}
    result = subprocess.run([str(ROOT / "docker" / "entrypoint.sh")], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "H3_OUTDIR must be set" in result.stderr
```

- [ ] **Step 2: Красный**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_docker_files.py -q -p no:cacheprovider`
Expected: FAIL — `FileNotFoundError: .../Dockerfile`.

- [ ] **Step 3: Файлы**

`Dockerfile`:
```dockerfile
FROM python:3.12-slim
# spec §3.2: the panel only -- no CUDA, no mlx/mlx-vlm/opencv/scipy. ffmpeg for assembly, the
# track pieces and the LTX pad/mux; node for the front-end checks the test suite runs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-panel.txt /app/
RUN pip install --no-cache-dir -r requirements-panel.txt
COPY . /app
# Editable on purpose: provider.system_prompt() and web.REPO_ROOT read files next to the sources.
RUN pip install --no-cache-dir --no-deps -e .
RUN chmod 0755 /app/docker/entrypoint.sh
ENV H3_ENGINE=sglang PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
```
`compose.yaml`:
```yaml
services:
  h3-panel:
    build: .
    image: h3-panel:latest
    network_mode: host
    user: "1000:1000"
    restart: unless-stopped
    stop_grace_period: 30s
    environment:
      H3_ENGINE: sglang
      H3_OUTDIR: /home/alex/Outputs/h3-panel
      H3_ALLOWED_HOSTS: "192.168.100.50:8765,alex-neuro:8765"
      H3_SGLANG_URL: http://127.0.0.1:30020
      H3_COMFY_URL: http://127.0.0.1:8188
      H3_DISPATCHER_URL: http://127.0.0.1:8790
      H3_COMFY_OUTPUT_DIR: /home/alex/Outputs/comfy/output
      H3_IDLE_RELEASE_MIN: "15"
      H3_MAX_REF_IMAGES: "6"
    volumes:
      - /home/alex/Outputs/h3-panel:/home/alex/Outputs/h3-panel
      - /home/alex/Outputs/comfy/output:/home/alex/Outputs/comfy/output:ro
      # our own frames only, rw, so a finished -ltx part can remove its PNGs (task 10)
      - /home/alex/Outputs/comfy/output/h3panel:/home/alex/Outputs/comfy/output/h3panel
      - /home/alex/Projects/h3-bench/inputs:/home/alex/Projects/h3-bench/inputs:ro
```
`docker/entrypoint.sh` (исполняемый, `chmod +x` до коммита):
```sh
#!/bin/sh
# spec §3.2: `h3 web` and `h3 worker` under one simple supervisor; the container exits when
# either dies, and `restart: unless-stopped` brings both back. POSIX sh, no `wait -n`.
set -u
: "${H3_OUTDIR:?H3_OUTDIR must be set}"
mkdir -p "$H3_OUTDIR"
python -m h3_48gb web --host "${H3_WEB_HOST:-0.0.0.0}" --port "${H3_WEB_PORT:-8765}" --outdir "$H3_OUTDIR" &
web=$!
python -m h3_48gb worker --outdir "$H3_OUTDIR" &
worker=$!
trap 'kill -TERM "$web" "$worker" 2>/dev/null' TERM INT
while kill -0 "$web" 2>/dev/null && kill -0 "$worker" 2>/dev/null; do
    sleep 1
done
kill -TERM "$web" "$worker" 2>/dev/null
status=0
wait "$web" || status=$?
wait "$worker" || status=$?
exit "$status"
```
`.dockerignore`:
```text
.git
**/__pycache__
*.egg-info
.pytest_cache
.claude
.gstack
.superpowers
reference
```

- [ ] **Step 4: Зелёный на Маке**

Run: `chmod +x docker/entrypoint.sh && env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_docker_files.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Мутации**

(а) В entrypoint заменить условие цикла на `while kill -0 "$web" 2>/dev/null; do` → `test_entrypoint_starts_both_...` FAIL (`returncode 143`/таймаут > 10 с). (б) Убрать строку `: "${H3_OUTDIR:?...}"` → `test_entrypoint_refuses_to_start_without_an_outdir` FAIL. (в) В `compose.yaml` убрать `:ro` у тома ComfyUI → тест compose FAIL. Ошибки — в отчёт.

- [ ] **Step 6: Сборка образа и прогон тестов в контейнере**

Ветка А — на Маке есть docker (`docker --version` → 29.4.3, проверено при планировании):
```bash
cd /Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb
docker build -t h3-panel:test .
docker run --rm --entrypoint python -e HOME=/tmp h3-panel:test -m pytest -q -p no:cacheprovider -rs 2>&1 | tail -15
```
Ветка Б — docker на Маке недоступен: тот же `docker build`/`docker run` на alex-neuro из клона ветки (см. задачу 14, шаг 1, — клон без запуска GPU-работ); сборка образа и тесты GPU не трогают.
Expected: `0 failed`; строки `N skipped: mlx: ...` и `M skipped: cv: ...`; число passed совпадает с прогоном в `~/venvs/h3-panel` после задачи 12 (разница допустима только в тестах, зависящих от `sys.platform == "darwin"`, — перечислить их в отчёте поимённо). Образ меньше ~500 МБ (`docker image ls h3-panel:test`), записать размер.

- [ ] **Step 7: Полный прогон и commit**

```bash
env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest -q -p no:cacheprovider
git add Dockerfile compose.yaml docker/entrypoint.sh .dockerignore tests/test_docker_files.py
git commit -m "build: Docker-образ панели — python:3.12-slim + ffmpeg + node, compose с host-сетью и теми же путями"
```

---

## Task 14: Деплой на alex-neuro (без GPU-работ) и пробы §6 на живом sglang

**Files:**
- Create: `tools/probes/sglang_probes.py`
- Test: `tests/test_sglang_probes.py` (новый)
- Без изменений кода панели. Всё, что делается на alex-neuro, — по ssh от пользователя `alex`.

**Interfaces:**
- Consumes: `SglangClient`, `normalize_h3_conditions`, `MODEL` (задача 6), `DispatcherClient` (задача 8), образ и compose (задача 13), диспетчер и юнит (задача 7), шаблон LTX (задача 10).
- Produces: `tools/probes/sglang_probes.py` с `beach_payload(job: dict) -> dict`, `probe_payloads(workdir: Path) -> dict[str, dict]`, `run_probe(name, payload, client, *, poll=10.0, sleep=time.sleep) -> dict`, `main(argv=None) -> int`; результаты проб в `/home/alex/Outputs/h3-panel/probes/<YYYYmmdd-HHMMSS>.jsonl` (на диске, не в `/tmp` — `CLAUDE.md`).

**Метки шагов:** «без GPU» — можно делать в любое время; «**требует свободной GPU; если занята чужим — отложить и сообщить владельцу**» — перед шагом `curl -s 127.0.0.1:8790/status`: если в `foreign` что-то есть или `qwen.running`, шаг не выполняется, владельцу пишется, кто держит карту. Qwen без слова владельца не трогать. Боевые ворота — только с владельцем.

- [ ] **Step 1 (без GPU): Код на сервер**

Пушить ветку можно только со слова владельца. Без пуша — через bundle:
```bash
cd /Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb
git bundle create "$TMPDIR/h3-panel.bundle" feat/panel-alex-neuro
scp "$TMPDIR/h3-panel.bundle" alex-neuro:/home/alex/Projects/h3-panel.bundle
ssh alex-neuro 'test -d /home/alex/Projects/h3-panel \
  && (cd /home/alex/Projects/h3-panel && git fetch /home/alex/Projects/h3-panel.bundle feat/panel-alex-neuro && git checkout -B feat/panel-alex-neuro FETCH_HEAD) \
  || git clone -b feat/panel-alex-neuro /home/alex/Projects/h3-panel.bundle /home/alex/Projects/h3-panel'
ssh alex-neuro 'mkdir -p /home/alex/Outputs/h3-panel/probes /home/alex/Outputs/comfy/output/h3panel && ls -ld /home/alex/Outputs/h3-panel /home/alex/Outputs/comfy/output/h3panel'
```
(Каталог `h3panel` в выводе ComfyUI нужен до `docker compose up`: иначе Docker создаст его от root, и ни ComfyUI, ни панель не смогут в нём писать/удалять.)
Expected: каталог `/home/alex/Projects/h3-panel` на ветке, `/home/alex/Outputs/h3-panel` принадлежит `alex`.

- [ ] **Step 2 (без GPU): Проверка шаблона LTX против ComfyUI**

`validate_workflow.py` импортирует узлы ComfyUI (CPU, без сервера и без рендера):
```bash
ssh alex-neuro 'cd /home/alex/Projects/comfy/ComfyUI && ../.venv/bin/python ../bin/validate_workflow.py /home/alex/Projects/h3-panel/h3_48gb/engines/ltx_workflow.json 2>&1 | grep -E "VALID|UNKNOWN|ERROR|unknown inputs"'
```
Expected: `VALID: True`, `nodes with unknown inputs: 0`. Иначе — правка шаблона (и тест `test_template_graph_is_well_formed...` задачи 10) отдельным коммитом.

- [ ] **Step 3 (без GPU, нужен sudo владельца): Диспетчер**

Попросить владельца выполнить (sudo у агента нет):
```bash
sudo cp /home/alex/Projects/h3-panel/tools/gpu-dispatcher/h3-gpu-dispatcher.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now h3-gpu-dispatcher
```
Проверка (только чтение):
```bash
ssh alex-neuro 'systemctl is-active h3-gpu-dispatcher && curl -s 127.0.0.1:8790/status'
```
Expected: `active`; JSON с `own: {}`, `lock.held_by_us: false`, температурой и памятью. Если в `foreign` есть процессы — это нормально, ничего не делать.

- [ ] **Step 4 (без GPU): Образ и контейнер**

```bash
ssh alex-neuro 'cd /home/alex/Projects/h3-panel && docker compose build && docker run --rm --entrypoint python -e HOME=/tmp h3-panel:latest -m pytest -q -p no:cacheprovider 2>&1 | tail -5'
ssh alex-neuro 'cd /home/alex/Projects/h3-panel && docker compose up -d && sleep 3 && docker compose ps && docker compose logs --tail 20'
```
(Ветка Б задачи 13 выполняется здесь же.) Expected: тесты `0 failed`; контейнер `running`; в логах «страница на http://0.0.0.0:8765/». Очередь новая — создаётся на паузе (`queue.layout`), воркер ничего не берёт; idle-release шлёт `/release` при пустой очереди — у диспетчера своих движков нет, это пустая операция.

`providers.json` для чата — с владельцем: адрес Qwen `http://127.0.0.1:8000` (тип `openai`, имя модели — из `curl 127.0.0.1:8000/v1/models`, когда владелец поднимет Qwen) и/или llama-server на Маке. Файл кладётся в `/home/alex/Outputs/h3-panel/providers.json`.

- [ ] **Step 5 (без GPU): Доступ с Мака**

```bash
curl -s http://192.168.100.50:8765/api/state | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["engine"], d["platform"], d["paused"])'
curl -s http://192.168.100.50:8765/api/gpu | python3 -m json.tool | head -30
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example:8765' http://192.168.100.50:8765/api/state
```
Expected: `sglang linux True`; `/api/gpu` с данными диспетчера; третий запрос — `403`. Открыть `http://192.168.100.50:8765` в браузере на Маке, снять экран с плашкой GPU.

- [ ] **Step 6: Падающий тест скрипта проб**

`tests/test_sglang_probes.py`:
```python
"""The probe script's payload builders (spec §6): the beach replay must be chain_beach.py's own
payload, with only the step count cut so a probe never costs a 45-minute render."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("sglang_probes",
                                               ROOT / "tools" / "probes" / "sglang_probes.py")
probes = importlib.util.module_from_spec(_spec)
sys.modules["sglang_probes"] = probes
_spec.loader.exec_module(probes)


def test_beach_payload_is_chain_beach_head_scene_with_one_step():
    job = {"name": "beach-01", "duration": 7.0, "seed": 42, "short_edge": 512,
           "keyframe": "/home/alex/h3-bench/inputs/pano/beach-h.png",
           "refs": ["/home/alex/h3-bench/inputs/angelina/face_small.jpg"], "prompt": "P"}
    assert probes.beach_payload(job) == {
        "model": "MiniMaxAI/MiniMax-H3", "prompt": "P", "task": "ref2va",
        "conditions": [
            {"type": "image", "uri": "/home/alex/Projects/h3-bench/inputs/pano/beach-h.png",
             "role": "keyframe", "frame_index": 0},
            {"type": "image", "uri": "/home/alex/Projects/h3-bench/inputs/angelina/face_small.jpg",
             "role": "reference"}],
        "target": {"short_edge": 512, "aspect_ratio": "16:9", "duration_seconds": 7.0},
        "num_outputs_per_prompt": 1, "num_inference_steps": 1, "flow_shift": 12.0,
        "audio_flow_shift": 3.0, "seed": 42, "quality": "lossless"}


def test_probe_payloads_cover_the_open_questions(tmp_path):
    payloads = probes.probe_payloads(tmp_path)
    assert sorted(payloads) == ["eight_references", "keyframe_without_reference",
                                "picture_numbering"]
    assert all(p["task"] == "ref2va" for p in payloads.values())
    assert [c["role"] for c in payloads["keyframe_without_reference"]["conditions"]] == ["keyframe"]
    numbering = payloads["picture_numbering"]
    assert [(c["role"], Path(c["uri"]).name) for c in numbering["conditions"]] == [
        ("keyframe", "table.png"), ("reference", "red-cube.png"), ("reference", "blue-ball.png")]
    assert numbering["prompt"] == (
        "subject_definitions:\n"
        "<Subject 1> is the object shown in <Picture 1>.\n"
        "<Subject 2> is the object shown in <Picture 2>.\n\n"
        "On the empty table, <Subject 1> stands at the left edge and <Subject 2> at the right "
        "edge; the camera does not move.")
    assert [c["role"] for c in payloads["eight_references"]["conditions"]] == ["reference"] * 8
    assert all(p["num_inference_steps"] == 8 and p["target"]["duration_seconds"] == 3.0
               for p in payloads.values())
```

- [ ] **Step 7: Красный, затем реализация `tools/probes/sglang_probes.py`**

Run: `env -u NODE_OPTIONS ~/venvs/h3-panel/bin/python -m pytest tests/test_sglang_probes.py -q -p no:cacheprovider` → FAIL (`FileNotFoundError`).

```python
#!/usr/bin/env python3
"""Live probes for spec §6 against the panel's own sglang client. Run inside the panel container
(`docker compose run --rm --entrypoint python h3-panel tools/probes/sglang_probes.py ...`), only
with a free GPU and the owner's OK: it asks the dispatcher for H3 and gives up -- never waits --
if anything foreign holds the card. Results go to /home/alex/Outputs/h3-panel/probes/*.jsonl."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image

from h3_48gb.engines import sglang as sg
from h3_48gb.engines.dispatcher_client import DispatcherClient

OUT_DIR = Path("/home/alex/Outputs/h3-panel/probes")
BEACH_JOBS = Path("/home/alex/Projects/h3-bench/beach-jobs.json")
_COMMON = {"model": sg.MODEL, "num_outputs_per_prompt": 1, "flow_shift": 12.0,
           "audio_flow_shift": 3.0, "seed": 42, "quality": "lossless"}
_PROBE_TARGET = {"short_edge": 512, "aspect_ratio": "16:9", "duration_seconds": 3.0}


def beach_payload(job: dict) -> dict:
    """chain_beach.py:render's head-scene payload (keyframe + refs, 16:9), steps cut to 1."""
    conditions = [{"type": "image", "uri": job["keyframe"], "role": "keyframe", "frame_index": 0}]
    conditions += [{"type": "image", "uri": ref, "role": "reference"} for ref in job.get("refs", [])]
    return {"model": sg.MODEL, "prompt": job["prompt"], "task": "ref2va",
            "conditions": sg.normalize_h3_conditions(conditions),
            "target": {"short_edge": job["short_edge"], "aspect_ratio": "16:9",
                       "duration_seconds": float(job["duration"])},
            "num_outputs_per_prompt": 1, "num_inference_steps": 1, "flow_shift": 12.0,
            "audio_flow_shift": 3.0, "seed": int(job["seed"]), "quality": "lossless"}


def _card(path: Path, colour) -> str:
    Image.new("RGB", (512, 512), colour).save(path)
    return str(path)


def _shape(path: Path, colour, *, ball: bool) -> str:
    image = Image.new("RGB", (512, 512), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    (draw.ellipse if ball else draw.rectangle)((128, 128, 384, 384), fill=colour)
    image.save(path)
    return str(path)


def probe_payloads(workdir: Path) -> dict[str, dict]:
    workdir.mkdir(parents=True, exist_ok=True)
    keyframe = _card(workdir / "kf.png", (200, 120, 40))
    refs = [_card(workdir / f"r{i}.png", ((i * 30) % 255, 80, 160)) for i in range(8)]
    base = {**_COMMON, "num_inference_steps": 8}
    table = _card(workdir / "table.png", (150, 150, 150))
    red = _shape(workdir / "red-cube.png", (220, 20, 20), ball=False)
    blue = _shape(workdir / "blue-ball.png", (20, 40, 220), ball=True)
    return {
        # spec §6 (a), owner's decision after review: confirm by a render that, with a keyframe
        # present, <Picture 1> is the first *reference* picture (the code says the keyframe is
        # not numbered). Red on the left => confirmed; grey/table things on the left => not.
        "picture_numbering": {
            **base, "task": "ref2va",
            "prompt": ("subject_definitions:\n"
                       "<Subject 1> is the object shown in <Picture 1>.\n"
                       "<Subject 2> is the object shown in <Picture 2>.\n\n"
                       "On the empty table, <Subject 1> stands at the left edge and <Subject 2> "
                       "at the right edge; the camera does not move."),
            "conditions": [{"type": "image", "uri": table, "role": "keyframe", "frame_index": 0},
                           {"type": "image", "uri": red, "role": "reference"},
                           {"type": "image", "uri": blue, "role": "reference"}],
            "target": {**_PROBE_TARGET, "aspect_ratio": "auto"}},
        "keyframe_without_reference": {
            **base, "prompt": "The scene continues.", "task": "ref2va",
            "conditions": [{"type": "image", "uri": keyframe, "role": "keyframe",
                            "frame_index": 0}],
            "target": {**_PROBE_TARGET, "aspect_ratio": "auto"}},
        "eight_references": {
            **base, "task": "ref2va",
            "prompt": "subject_definitions:\n" + "\n".join(
                f"<Subject {i + 1}> is the colour card in <Picture {i + 1}>." for i in range(8))
                + "\n\nThe cards lie on a table.",
            "conditions": [{"type": "image", "uri": ref, "role": "reference"} for ref in refs],
            "target": dict(_PROBE_TARGET)},
    }


def run_probe(name: str, payload: dict, client, *, poll: float = 10.0, sleep=time.sleep) -> dict:
    started = time.time()
    try:
        video_id = client.create(payload)["id"]
    except sg.SglangHTTPError as exc:
        return {"probe": name, "result": "http_error", "status": exc.status, "detail": exc.detail}
    while True:
        status = client.get(video_id)
        if status.get("status") in ("completed", "failed"):
            break
        sleep(poll)
    return {"probe": name, "result": status["status"], "id": video_id,
            "wall_s": round(time.time() - started, 1),
            "peak_memory_mb": status.get("peak_memory_mb"), "error": status.get("error")}


def _acquire_h3(dispatcher: DispatcherClient) -> bool:
    while True:
        answer = dispatcher.acquire("h3")
        state = answer.get("state")
        if state == "ready":
            return True
        if state in ("wait", "wait_qwen", "failed"):
            print(f"пробы отложены: {state}: {answer.get('reason')} {answer.get('log') or ''}",
                  file=sys.stderr)
            return False
        time.sleep(10)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="+",
                        help="picture_numbering | keyframe_without_reference | eight_references | beach")
    parser.add_argument("--keep-h3", action="store_true", help="do not release the card after")
    args = parser.parse_args(argv)
    dispatcher = DispatcherClient()
    if not _acquire_h3(dispatcher):
        return 2
    client = sg.SglangClient(sg.DEFAULT_URL)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    payloads = probe_payloads(OUT_DIR / "inputs")
    try:
        for name in args.names:
            if name == "beach":
                jobs = json.loads(BEACH_JOBS.read_text(encoding="utf-8"))["jobs"]
                payload = beach_payload(next(j for j in jobs if j["name"] == "beach-01"))
            else:
                payload = payloads[name]
            result = run_probe(name, payload, client)
            if name == "picture_numbering" and result["result"] == "completed":
                video = OUT_DIR / f"{out_path.stem}-picture-numbering.mp4"
                client.download(result["id"], video)
                frame = video.with_suffix(".png")
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "2", "-i", str(video),
                                "-frames:v", "1", str(frame)], check=True)
                result["frame"] = str(frame)     # the owner looks: red cube on the left?
            print(json.dumps(result, ensure_ascii=False))
            with out_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(result, ensure_ascii=False) + "\n")
    finally:
        if not args.keep_h3:
            dispatcher.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```
Run снова → PASS. Мутация: в `beach_payload` оставить `num_inference_steps: 50` → `test_beach_payload_...` FAIL; вернуть. Commit:
```bash
git add tools/probes/sglang_probes.py tests/test_sglang_probes.py
git commit -m "tools: пробы sglang для §6 — нумерация Picture, кейфрейм без референсов, 8 картинок, повтор beach-01"
```
Обновить код на сервере (шаг 1) и пересобрать образ (`docker compose build`).

- [ ] **Step 8 (требует свободной GPU; если занята чужим — отложить и сообщить владельцу): Пробы**

```bash
ssh alex-neuro 'curl -s 127.0.0.1:8790/status | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[\"foreign\"], d[\"qwen\"])"'
ssh alex-neuro 'cd /home/alex/Projects/h3-panel && docker compose run --rm --entrypoint python h3-panel tools/probes/sglang_probes.py keyframe_without_reference picture_numbering beach'
```
Ожидания и решения по каждой пробе (в отчёт — дословный JSON):
1. `keyframe_without_reference` — ожидается `http_error 400` с `detail` о недостающем reference (по коду `request_validation.py:300-303`). Если `completed` — правило `ref2va_needs_reference` для цепочки ослабить нельзя без владельца: сообщить.
2. `picture_numbering` — проба §6 (а): короткая сцена с кейфреймом (серый стол) и двумя референсами (красный куб — `<Picture 1>`, синий шар — `<Picture 2>`); скрипт скачивает ролик и снимает кадр на 2-й секунде (`result["frame"]`). Красный куб слева — правило нумерации (кейфрейм не нумеруется) подтверждено; иначе — остановиться и сообщить владельцу: сборка `subject_definitions` (задача 4) тогда неверна.
3. `beach` — разбор прошлых 400 (`results-beach.jsonl`): `detail` теперь виден. Если `completed` — причина была в старых путях `/home/alex/h3-bench/...` (их чинит `normalize_h3_conditions`) или в чём-то ином, что пропало; записать.
4. `eight_references` — только если 1–3 прошли и карта свободна: предел картинок и `peak_memory_mb`. По итогу владелец выбирает `H3_MAX_REF_IMAGES` (по умолчанию 6) в `compose.yaml`.
(Проба `t2va` больше не нужна: 07.10 установлено, что сервер `VARIANT=ref2va` обслуживает только `ref2va`, — `h3-bench/logs/probe-t2va-20261007.log`.)

- [ ] **Step 9 (требует свободной GPU; если занята чужим — отложить и сообщить владельцу): Дымовой прогон сцены через панель**

Проект `kind=video` из 2 сцен по 5 с (после снапа `124/24` и `123/24` с), один референс `@probe` (цветная карточка), апскейл выключен (`PUT /route {"upscale": false}`), очередь запустить кнопкой. Проверить: `wait_reason` во время подъёма H3, `engine_ref` в задаче, `<stem>.mp4` и `<stem>.json`, сцена 1 — `ref2va` с кейфреймом, сборка `final.mp4` с длительностью `= сумме сцен − 1 кадр` (±0,05 с). Затем «Освободить карту» → `own: {}` у диспетчера. Отдельно — апскейл включить и `upscale/retry`-путь пройти на тех же двух сценах (поднимется ComfyUI). Отчёт: тексты плашки, тайминги, длительности.

- [ ] **Step 10 (только с владельцем): Боевые ворота (спека §6)**

Не выполнять без владельца. Список для него:
1. Проект из 5 сцен по 10 с с двумя референсами (человек по фото и окружение) — сценарий → сцены → апскейл → сборка в панели; цепочка проходит больше трёх сцепок подряд.
2. Рестарт контейнера (`docker compose restart`) посреди рендера сцены — в логе sglang ровно один `POST /v1/videos` на сцену, задача продолжает опрос того же `id`.
3. Сценарий на Qwen alex-neuro → «Выгрузить Qwen» → рендер → «Вернуть Qwen».

---

## Расхождения со спекой

Ссылки и формулировки спеки сверены с кодом `main` (a5975bf3) и с исходниками sglang/h3-bench/comfy на alex-neuro. План опирается на реальный код.

**Номера строк**
1. §3.3.5: `web._snap_scene_duration` — не `web.py:904-918` (там константы `_SCENE_LATENT_OVERLAP_FRAMES`, `_CHAINED_GRID_REMAINDER`), а `web.py:947-1009`.
2. §3.3.6: `cli.py:618-620` — только комментарий; проверки внешнего bind нет, сервер просто всегда слушает `LOOPBACK` (`web.py:5816`). Host — `web.py:3114` и `5820`, Origin — `web.py:3175` — верно.
3. §3.3.10: импорт трека — разбор `web.py:4056-4087`, применение `4093-4119`, задача `song` — `_submit_project_song_job` `4121-4162`; `web.py:1832-1843` — это `_scenario_context`, нарезка — `build_clip_scenes` `1407-1596`. `songrun.py:63` — константы; `align_track` — `songrun.py:627`.
4. §5: `queue.py:1099-1104` — докстринг `reconcile`; тело — `1148-1172`.
5. §3.3.7: `_llm_holds_gpu` живёт в `worker.py:859`; `pkill llama-server` — только `provider.py:358`, вызывается лишь в цикле по провайдерам `llama-local` (`web.py:5033-5038`) — условие уже выполнено, правки нет.

**Модель проекта**
6. §4.1.3 «`aspect_ratio` проекта» и «`i2v_prefix` проекта»: таких полей в проекте нет. Холст сцен прошит `assemble.DEFAULT_SCENE_CANVAS = (896, 512)` → `"16:9"`; `i2v_prefix` — поле проекта (решение после ревью) со значением по умолчанию `DEFAULT_I2V_PREFIX` = «The video begins exactly on the provided first frame and continues it seamlessly: same characters, setting, lighting and camera style.» (без метки `<Picture N>`), меняется через `PUT /api/projects/<id>/settings` и простым полем в модалке проекта. Строки 512×896 и 768×768 таблицы форматов достижимы только ручными задачами, проекты их не выбирают.
7. MLX-префикс `SCENE_I2V_INSTRUCTION` («`<Picture 1>` … fully referenced» про кейфрейм) на sglang **не используется**: по коду сервера кейфрейм не получает метки, и `<Picture 1>` — первая картинка-референс. Промпты `chain_beach.py` (кейфрейм = `<Picture 1>`, референсы = `<Picture 2..4>`) с кодом сервера расходятся — 8/8 сцен прошли, но подписи в них, вероятно, были сдвинуты. Риск для владельца, не для плана; проба `picture_numbering` (задача 14) проверяет правило генерацией.
8. §3.3.8: для статуса апскейла в `stages` добавлен этап `upscale` (старые проекты мигрируют в `draft`); спека этого не оговаривала.

**Где что делается**
9. §3.5 «сборка промпта адаптером»: Ref2VA собирается в `assemble` (`library.build_ref2va`), argv несёт готовый промпт и `--ref` по порядку, адаптер переводит argv в payload. Поведение то же; место другое, потому что адаптер видит только argv.
10. §3.5 раскладка `<H3_OUTDIR>/library/<tag>/`: каталог — тег без `@` (`library/alice/`), файлы версии — в `vN/`; старые версии не удаляются никогда (строже и проще, чем «пока ссылается проект»).
11. §3.3.14: при идущем рендере `POST /release` делает **воркер** после остановки задачи (страница ставит `cancel_reason=released_by_user` и паузу и отвечает `releasing: true`) — чтобы освобождение не гонялось с опросом sglang.
12. §4.1.1 «повтор через 30 с»: пока свой движок `starting`, опрос каждые 5 с; на `wait`/`wait_qwen` — 30 с.
13. §4.2.3: результат LTX — PNG-кадры SaveImage (не видео через `/view`); вход — `POST /upload/image`, потому что `ComfyUI/input/` в контейнер не смонтирован; готовность — опрос `/history` без websocket. Вывод ComfyUI смонтирован `ro`, **кроме своего подкаталога** `<output>/h3panel` (`rw`): после сборки `-ltx` части её PNG удаляются (решение после ревью).
14. §3.2 `pip install --no-deps .` → `pip install --no-deps -e .`: `provider.system_prompt()` и `web.REPO_ROOT` читают файлы рядом с исходниками.
15. §3.3.13 «освободится через 12 мин»: веб вычисляет время из последнего `finished_at` + `H3_IDLE_RELEASE_MIN`; это приближение отсчёта воркера (он считает от момента, когда увидел пустую очередь).

**Содержательные**
16. §6 «сумма снапнутых длительностей ≥ трека»: противоречит действующему правилу сборки «перебор не обрезается» (допуск 0,5 с, а ячейка сетки 0,708 с, `assemble.py:784-790`). План добавляет **только при sglang** обрезку картинки до длины трека (`_trim_video`); на Маке поведение прежнее.
17. Пути условий — как в проверенных прогонах (`chain_beach.py`, `runner_clip.py`): картинки голым абсолютным путём, аудио — `file://`; `normalize_h3_conditions` перенесён, но для путей панели он ничего не меняет.
18. §6 проба (а) «нумерация `<Picture N>`»: по коду сервера (`presentation.py:230-270`, `stages/text_encoding.py:385`) кейфрейм не нумеруется; по решению после ревью это ещё и подтверждается генерацией — проба `picture_numbering` (задача 14, шаг 8). Проба (б) «предел картинок»: в коде предела нет, картинки масштабируются до 2048 по короткой стороне; `H3_MAX_REF_IMAGES` по умолчанию 6, уточняется пробой `eight_references`.
19. §3.3.10 «если длительностей нет — равные куски»: схема `SCENARIO_SCHEMA` требует длительности, поэтому «равные куски» — путь `procedural: true` при sglang (`_equal_scenario_scenes`).
20. Длительность сцены: sglang принимает 3–15 с, схема сценария — 5–10 с; план держит 5–10, кроме последней сцены клипа, которая при добивании вверх может выйти до 15 с.
21. §4.2.4 «черновая сборка из исходных остаётся» (решение после ревью): черновая сборка из исходных частей доступна всегда отдельным действием «Черновая сборка» (`POST /api/projects/<id>/assembly/draft` → `assembly/draft.mp4`); при выключенном апскейле обычная сборка и так из исходных; финальная при включённом — из `-ltx`.
22. §3.5 «полоска миниатюр под сценой», правка карточек на месте, фильтр и поиск — в волну 1 не входят (§10: в старый интерфейс — только простая форма); в план не включены.
23. §3.3.15: в таблице оценок оставлены и строки ref2va-прогонов beach (512/175 → 2810 с, 512/192 → 2980 с): теперь все сцены — ref2va (решение после ревью).
24. §3.4: установка systemd-юнита требует `sudo` — шаг владельца; логи движков диспетчер пишет как `serve-panel-*.log` / `comfy-panel-*.log` в те же каталоги `logs/`.
25. §4.1.3 (обновлена 07.10): `t2va` убран полностью — `task` всегда `ref2va`, сцена без референса (нет `@`-тега и это не клип) отклоняется на гейте и в парсере для **каждой** сцены, не только сцепленной.
26. Пересъёмка сцены после апскейла (`invalidate_scene_chain`) сбрасывает `stages.upscale` и `ltx_path` сброшенных сцен — спека этого не оговаривала, без этого сборка взяла бы старые `-ltx`.
27. «Освободить карту», когда в работе сборка (ffmpeg, не GPU): движки освобождаются сразу, сборка не отменяется; подтверждение и отмена — только для `generate`/`upscale`. «Вернуть Qwen» при непустой очереди — `409 queue_busy` (спека: «после очереди»).
28. «С какого времени держит карту» чужой процесс — диспетчер помнит `first_seen` в памяти (сбрасывается при его перезапуске), плашка показывает «уже N мин».

## Самопроверка плана (выполнена при написании)

- Покрытие спеки: §3.3.1 — задача 2; §3.3.2, §3.3.4 — 3, 5; §3.3.3, §3.3.12, §5 (4xx, пропажа, рестарт) — 6; §3.3.5, §4.1.5 — 5, 9; §3.3.6 — 1; §3.3.7 — 2, 12; §3.3.8 — 11; §3.3.9, §3.5 — 4, 5, 12; §3.3.10, §4.3 — 9; §3.3.11, §4.2 — 10, 11; §3.3.13–14 — 8, 12; §3.3.15 — 3; §3.4 — 7, 8; §4.1.1–2 — 8; §6 — тесты во всех задачах, контейнер — 13, пробы и ворота — 14; §7 — формы маршрутов `/api/projects*`, `/api/jobs`, `/api/queue/*` только дополнены ключами; §9 (RAM при сборке) — 8.
- Плейсхолдеров нет; одно место помечено «проверить» (раздаёт ли `/media` миниатюры из `library/`, задача 12) — от ответа зависит только показ превью, не тест.
- Имена между задачами сверены: `SglangSpec`, `parse`, `build_payload`, `run_generate(..., gate=)`, `make_gpu_gate`, `set_running_fields/request_cancel/cancel_reason`, `Reconciled.resumable`, `build_ref2va/Ref2VAScene`, `_scene_generate_args_sglang(..., track_piece=)`, `_cut_track_piece`, `set_scene_fields`, `route_enabled`, `KIND_UPSCALE`, `DispatcherClient`.

## Порядок исполнения и передача

Задачи строго последовательны (каждая опирается на интерфейсы предыдущих): 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14. Задача 7 (диспетчер) от 3–6 не зависит и может идти параллельно с ними в отдельном worktree. После каждой задачи — свежий ревьюер (не автор) с требованием показать красный прогон каждой мутации; после задачи 13 — ревью всей ветки. План до первого исполнителя сверяет со спекой не автор плана (`/codex`, `CLAUDE.md`).
