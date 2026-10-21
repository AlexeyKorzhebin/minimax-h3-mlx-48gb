# Шаблоны ComfyUI для адаптера студии (22.10.2026)

Три шаблона: `krea2-t2i`, `h3-fl2va`, `h3-ref2va`. Спека: `docs/superpowers/specs/2026-10-10-studio-part1-core-design.md` §8.2, §10 L7b.
Источник правды: h3-bench, ветка `probe/comfy-templates-20261022` (alex-neuro, `~/Projects/h3-bench`, только коммит):
`comfy_templates_build.py` (генерит графы и карточки по живому `GET /models/<folder>` и `/object_info`), `comfy_template.py`
(подстановщик), `test_comfy_template.py` (15 тестов), `comfy_template_verify.py` (живой прогон). Здесь копия готовых файлов.

## Файлы

- `<id>.graph.json`: граф в формате API ComfyUI с заглушками `__PROMPT__`, `__FIRST_FRAME__`, `__REF_IMAGE__`, `studio/__TASK_ID__/f`.
- `<id>.card.json`: `requires_json` (`class_types`, `models` папка -> файлы, `limits`, `estimate_s`), `inputs` (схема подстановок),
  `head_drop_frames`, `output`, `verified_on`.
- `comfy_template.py`: чистая функция `build_payload(graph, card, values)`, только stdlib, без сети. Выход:
  `{"prompt": <граф>, "client_id": task_id, "extra_data": {"studio_task_id": task_id}}`, прямо в `POST /prompt`.
  CLI: `python3 comfy_template.py X.card.json values.json` (граф берётся рядом по `card.graph`).

## Что подставляется куда (номера узлов)

| Вход | krea2-t2i | h3-fl2va | h3-ref2va |
|---|---|---|---|
| `task_id` (обяз.; `[A-Za-z0-9][A-Za-z0-9._-]{0,79}`) | 9.filename_prefix = `studio/<task_id>/f` | 15.filename_prefix | 15.filename_prefix |
| `prompt` | 4.text | 6.prompt | 6.prompt (с `subject_definitions`, `<Picture N>`) |
| `seed` | 7.seed | 10.noise_seed | 10.noise_seed |
| `steps` | 7.steps (по умолч. 8) | 7.steps | 7.steps |
| `width`, `height` | 6 (кратно 64) | 6 (кратно 32, до 1344) | 6 |
| `length` (кадры) | нет | 6.length, сетка 17k+5, 73..345 | то же |
| `first_frame` | нет | 5.image (имя в input ComfyUI) | нет |
| `ref_images` (1..6 имён) | нет | нет | клоны LoadImage `5_0..5_{N-1}` и `6."ref_images.ref_image_{i}"` |
| `ref_image_size` | нет | нет | 6 (`match` по умолч. или `max`) |
| `lora` `{name, strength}` (необяз.) | вставляет `LoraLoaderModelOnly` между узлами 1 и 7 | нет | нет |

Студия считает кадры сама (`studio/duration.snap_frames`, §6.2: ближайшая точка сетки, при равенстве вверх) и подаёт готовый `length`; `comfy_template.snap_length(сек)` — только для ручных проб (вверх на сетку 17k+5: 3 с = 73, 8 с = 192, 14,375 с = 345). Картинки сначала
`POST /upload/image` (имя `<task_id>-<n>.<ext>`), в подстановщик идут имена.
`TemplateError` на неизвестный или пропущенный вход, значение вне диапазона или сетки, `len(ref_images)` выше
`ref_images_by_frames` (L7b), оставшиеся заглушки `__X__`.

## Факты в карточках

- `ref_images_by_frames = [[345, 6]]` (продуктовый предел 6 — как в мастере «N из 6»; 9 проходили на 64,7 из 64,9 ГБ, это край OOM) (GPU-DAY-20261021 п.2): 2/4/6 картинок до 14,375 с (345 кадров) на 896x512 без OOM; 9 картинок
  (максимум узла) прошли на 345 кадрах даже при 1344x768 `max` (64 671 из 64 911 МБ, 530 с). Выше 345 кадров не проверялось, поэтому `frames` = [73, 345].
  Ограничение практическое по времени, не по памяти.
- `head_drop_frames = 1` у `h3-fl2va` (п.3: кадр 0 это пересжатая копия входа, SSIM 0,91-0,93, не побайтно), 0 у `h3-ref2va`.
- `estimate_s` до накопления истории: krea 6, fl2va 90, ref2va 100. Замеры 22.10: Krea 24 с на холодном старте, fl2va 114 с после Krea (75 с без смены модели), ref2va 100 с.
- `verified_on`: 22.10.2026, alex-neuro, ComfyUI 0.42.0 (b1a247c9). По спеке §8.2 для H3 нужен ещё п.8 (контракт адаптера).
- Живьём не проверено поле `lora`: на comfy-h3 в `/models/loras` нет LoRA Krea 2, проверена только структура графа тестом.

## Как проверить

1. Юнит-тесты подстановщика (GPU не нужен): `cd ~/Projects/h3-bench && python3 -m unittest test_comfy_template -v`.
2. Живой прогон (GPU, под `flock`): `bash gpu_day21_srv.sh start NAME`, затем
   `~/Projects/comfy-h3/.venv/bin/python comfy_template_verify.py [krea2-t2i h3-fl2va h3-ref2va]`, затем `bash gpu_day21_srv.sh stop`.
   Скрипт сверяет `requires_json` с `GET /object_info` и `GET /models/<folder>`, строит payload, шлёт `POST /prompt`, ждёт `/history/<id>` и сравнивает:
   сид, промпт, steps, размер, `length`, имена картинок, `studio_task_id` в `/history`; файлы в `studio/<task_id>/`;
   `ffprobe -count_frames` = `length`; размер файла. При успехе пишет `verified_on` в карточку.
3. Результаты 22.10: `h3-bench/outputs/comfy-templates-20261022/*.verify.json`, листы кадров `~/Research/TestVideo/comfy-templates-20261022/`.
