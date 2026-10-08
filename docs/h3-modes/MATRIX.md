# MiniMax H3 на sglang (alex-neuro): режимы, условия, ограничения

Дата: 2026-10-08. Источник истины — код sglang, который реально исполняется сервером на порту 30020:
`/home/alex/Projects/h3-lab/sglang-src` (venv `h3-lab/.venv312` ставит его editable:
`sglang.__file__ = /home/alex/Projects/h3-lab/sglang-src/python/sglang/__init__.py`), коммит `afe90a8bc9`.

Ниже `MH3/` = `python/sglang/multimodal_gen/runtime/pipelines_core/stages/model_specific_stages/minimax_h3/`,
`CFG/` = `python/sglang/multimodal_gen/configs/`, `PKG/` = `/home/alex/Models/Video/MiniMax-H3/model-package/`.

Статусы: **проба** — подтверждено живым сервером (с файлом лога); **по коду** — прочитан путь
исполнения; **предположение** — вывод без прямого доказательства.

## 0. Локальные патчи — от них зависят числа в UI

`git diff` в `sglang-src` показывает три незакоммиченные правки:

| Что | Апстрим | Локально | Где |
|---|---|---|---|
| Минимальная длительность | 4.0 с | **3.0 с** | `MH3/constants.py:30` |
| Список aspect для t2va/ref2va | 21:9, 16:9, 4:3, 1:1, 3:4, 9:16 | + **3:2, 2:3** | `MH3/task_profiles.py:59-68` |
| Выбор пайплайна по `--model-id` | по пути | по model-id | `multimodal_gen/registry.py:687` |

Если sglang переустановят или обновят, 3-секундные сцены и 3:2/2:3 начнут получать 400. Панель
(`h3_48gb/engines/sglang_args.py:18,29`) уже завязана на 3 с и на 3:2/2:3, то есть на эти патчи.

## 1. Разделы весов (partition) и задачи (task)

Задачи, которые раздел принимает, задаёт не sglang, а `model_index.json` раздела, поле `_minimax_h3`:

| Раздел | `tasks` в model_index | Файл весов на диске | Источник |
|---|---|---|---|
| `fl2va` | `["t2va", "fl2va"]` | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | `PKG/FL2VA/model_index.json` |
| `ref2va` | `["ref2va"]` | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` | `PKG/Ref2VA/model_index.json` |
| FastH3 (отдельный чекпойнт) | только t2va, 4 шага, `--model-variant` запрещён | на диске нет | `CFG/pipeline_configs/minimax_h3.py:296-320` |

- Других разделов не бывает: `release_metadata.py:65` допускает только `fl2va` и `ref2va`, а
  `task_profiles.py:32-38` жёстко отображает t2va→fl2va, fl2va→fl2va, ref2va→ref2va. **По коду.**
- В README из комплекта Comfy (`runtime-components/README.md`) перечислены варианты весов для каждого раздела:
  bf16, int8_convrot, pruned_bf16, pruned_int8_convrot, pruned_fp8_scaled. На диске есть только `pruned_int8_convrot`.
- **t2va (чистый текст) обслуживает раздел fl2va, а не ref2va.** Сервер `ref2va` принимает t2va с HTTP 200,
  а потом job падает с ошибкой `task 't2va' is not served by MiniMax H3 partition 'ref2va'; supported tasks: ['ref2va']`
  (**проба** 07.10, `~/Projects/h3-bench/logs/probe-t2va-20261007.log`; текст из `release_metadata.py:131`).
- Проверка раздела выполняется **асинхронно**, в стадии планировщика (`release_metadata.py:146-150`), а не в HTTP-валидации.
  Поэтому «чужая» задача выглядит как принятая (200, `queued`), а потом приходит `status: failed`.
  UI должен решать сам, какой вариант сервера нужен, и не полагаться на 400.

## 2. Матрица: раздел × task × условия

Общая форма условия: `{"type", "uri", "role", "frame_index"?, "start_time_seconds"?}`. Другие ключи запрещены
(`request_validation.py:41-43,190-192`). `role` ∈ {`keyframe`, `reference`}. Ролей `first`/`last` нет:
первый и последний кадр задаются через `frame_index` 0 и −1.

### 2.1 Раздел fl2va

| # | task | Условия | Кол-во | Позиция кадра | Геометрия при `aspect_ratio:"auto"` | Статус |
|---|---|---|---|---|---|---|
| F1 | `t2va` | нет (`conditions` пуст или отсутствует) | 0, иначе 400 | — | 16:9 (policy default) | **проба** (эмпирика: `h3-bench/results-crf25.jsonl`, `results-ab.jsonl`, `logs/*tango-story*` task=t2va) |
| F2 | `fl2va` | image/keyframe | 1 | `[0]` — первый кадр | по пропорциям кадра | **проба** (эмпирика: `h3-bench/results-fight.jsonl`, `results-fight2.jsonl`, `runner.py:39`) |
| F3 | `fl2va` | image/keyframe | 1 | `[-1]` — последний кадр | по пропорциям кадра | по коду |
| F4 | `fl2va` | image/keyframe ×2 | 2 | `[0, -1]`, строго в этом порядке | по первому кадру; второй кадр cover-crop | по коду |
| F× | `fl2va` | что угодно, кроме image/keyframe (reference, audio, video) | — | — | — | запрещено, 400 (`task_profiles.py:130-137`) |
| F× | `fl2va` | кадр в середине (`frame_index` 5, 40 …), `[-1, 0]`, три кадра | — | — | — | запрещено, 400 (`request_validation.py:262-267`, `task_profiles.py:74-78`) |

Аспект у fl2va: явный `W:H` **не проверяется** по списку `MINIMAX_H3_FINITE_ASPECT_RATIOS`, потому что проверка
`request_validation.py:102-111` охватывает только t2va и ref2va. Годится любое целое `W:H` в диапазоне 1:4…4:1
(`resolved_plan.py:147-151`). По коду; например `5:4` для fl2va пройдёт, а для ref2va получит 400.

Как кадр ложится на холст (`canvas.py:211-228`): **первый** по порядку кадр запроса (в том числе одиночный `[-1]`)
**растягивается** (stretch) на целевой холст без сохранения пропорций. Второй кадр (`-1` в паре `[0,-1]`)
масштабируется с обрезкой (cover-crop, апскейл разрешён). Отсюда следствие для UI: если явно задать aspect,
не совпадающий с кадром, картинка будет **искажена**, а не обрезана. При `auto` у fl2va холст берётся из
пропорций первого кадра (`prequeue.py:78-124`), и искажение ограничено округлением до 32 px.

### 2.2 Раздел ref2va

Правила допуска (`task_profiles.py:188-243`): каждое условие — одна из пяти пар (role, type).

| (role, type) | Что это | material chain | Идёт в Qwen как | Идёт в DiT как |
|---|---|---|---|---|
| reference / image | референс картинкой (персонаж, предмет, стиль) | `image.reference_preserve`, короткая сторона 2048 px, апскейл | `<Picture i>: ` + vision-блок | латент картинки |
| reference / audio | аудио-референс (голос, трек) | `audio`, стерео 32 кГц | только метка `<Audio j>: ` | аудио-латент, чистый (cond timestep 1.0) |
| reference / video | видео-референс; его звук, если есть, — тоже аудио-референс | `video.reference_preserve` | `<Audio j>` (если есть звук) + `<Video k>` с таймкодами, 2 fps | видео-латент + аудио-латент (или пустой, если звука нет) |
| reference / video_audio | то же, но звук обязателен (нет аудиодорожки — ошибка) | `video_audio.reference_preserve` | `<Audio j>` + `<Video k>` | видео- и аудио-латент |
| keyframe / image | «гибридный» ключевой кадр поверх референсов | `image.target_canvas` | **не идёт** (`resolved_plan.py:375-379`) | guide-латент на позиции кадра |

Источники: `stages/text_encoding.py:375-470`, `presentation.py:215-290`, `stages/audio_encoding.py:132-208`,
`reference_encoding.py:1-15`.

| # | Комбинация в ref2va | Ограничения | Статус |
|---|---|---|---|
| R1 | ≥1 image/reference | Число не ограничено кодом (`max_condition_count=None`, `task_profiles.py:188-243`). Официальные лимиты (9 картинок / 3 видео / 3 аудио / 12 всего, см. §2.4) сервер **не проверяет**. Реальный предел здесь — VRAM: по комментарию панели, 6 портретов при 10 с заняли 63.6 из ~64.9 ГБ (`sglang_args.py:24`) | **проба** (эмпирика: до 3 ref + keyframe, `h3-bench/amz-*-jobs.json`, 11 completed из 12) |
| R2 | только audio/reference, без картинок | `has_image=False` — vision-блока нет, визуальная стадия пропускается (`stages/visual_encoding.py:84-87`). README Sol-H3 утверждает обратное («аудио не может быть единственным референсом», `Sol-H3/README.md:148`), но это другой рантайм | **проба** (эмпирика: `tango-ref-p1`, `tango-suno-p1` — `conds=['reference:audio']`, completed, `h3-bench/results-tango-*.jsonl`) |
| R3 | image + audio | метки нумеруются раздельно: `<Picture 1>`, `<Audio 1>` в порядке запроса | **проба** (эмпирика: `h3-bench/clip-jobs-v2.json`, `runner_clip2.py:43-44`, completed) |
| R4 | `duration_seconds` не задан + ровно один носитель звука (audio / video со звуком / video_audio) | длительность = длительность звука − `start_time_seconds`, должна попасть в 3…15 с, округляется вверх до 17n+5 (`prequeue.py:127-176`) | **проба** для одиночного audio (`tango-ref-p1`: `target` без duration, вышло 10.125 с = 243 кадра; `runner_tango_ref.py:35`); для video — по коду |
| R5 | `duration_seconds` задан, звук длиннее | звук обрезается до длины ролика (`stages/audio_encoding.py:146-151`, `-t` в ffmpeg) | по коду |
| R6 | video/reference (с `start_time_seconds` или без) | видео декодируется в 24 fps и обрезается до числа кадров ролика; `start_time_seconds` меньше длины видео и меньше длины звука (`prequeue.py:179-214`) | по коду |
| R7 | video/reference **без звуковой дорожки** | пустой аудиоблок, метка `<Audio>` не ставится (`stages/audio_encoding.py:176-185`) | по коду |
| R8 | video_audio/reference без звука | ошибка на этапе probe: `MiniMax H3 audio material has no audio stream` (`material_io.py:425-426`) | по коду |
| R9 | ≥1 reference + keyframe `[0]` | keyframe **не задаёт** геометрию: при `auto` холст 16:9 (`task_profiles.py:236-240`), и кадр **растягивается** на 16:9. Reference может быть и одним audio (сцепка клипов: последний кадр предыдущей части + трек) | **проба** (эмпирика: `fight-armored-40-jobs.json`, 5 completed; keyframe+audio — `tango-suno-p2/p3`, `clip-jobs-v2.json`); растяжение при несовпадении aspect — по коду |
| R10 | ≥1 reference + keyframe `[-1]` или `[0,-1]` | те же сигнатуры, что у fl2va (`request_validation.py:319-323`) | по коду |
| R11 | только keyframe(ы), без reference | 400 `ref2va keyframes require at least one reference condition; use task 'fl2va' for keyframe-only generation` | **проба** 07.10 (тот же лог) |
| R12 | `conditions` пуст | 400 `conditions requires at least one entry for task 'ref2va'` (`request_validation.py:160-163`) | по коду |
| R13 | несколько video-референсов | допускаются, каждый получает `<Video k>`; длину по звуку вывести нельзя, если носителей звука больше одного (`request_validation.py:348-352`) | по коду |

Порядок условий значим и не переупорядочивается (`request_validation.py:6-7`): от него зависят номера
`<Picture i>`/`<Audio j>`/`<Video k>`, на которые может ссылаться промпт. Keyframe-условия нумерацию не сдвигают.

### 2.4 Официальные лимиты, которые этот sglang не проверяет

Из документации других рантаймов: Sol-H3-Spark README и узлы ComfyUI, найдены на alex-neuro.
Для UI их стоит **соблюдать**: модель обучалась в этих рамках, а сервер их пропустит молча.

- Ref2VA: не больше **9 картинок, 3 видео, 3 аудио, 12 референсов всего**. Нужен хотя бы один image или video
  (`~/.cache/uv/git-v0/checkouts/361f015435602b4e/8e0db4f/models/minimax_h3/Sol-H3-Spark/README.md:134-135`;
  `~/Projects/comfy/ComfyUI/comfy_extras/nodes_minimax_h3.py:268-283`; облачный узел `comfy_api_nodes/nodes_minimax.py:911-923,1108`).
  Наш sglang аудио-only при этом принимает и генерирует (R2).
- Видео-референс: 2–15 с, 24 fps, не короче 5 кадров (`nodes_minimax_h3.py:268-283,329`). Облачный API требует
  ещё и суммарную длину видео-референсов не больше 15 с.
- У видео-референса его звук идёт отдельным аудио-референсом **перед** кадрами видео. Это сдвигает нумерацию
  `<Audio j>` (`Sol-H3-Spark/README.md:136-138`; у нас то же самое в `stages/text_encoding.py:443-466`).
- Обученный диапазон длины — 124–362 кадра, то есть ~5–15 с (`nodes_minimax_h3.py:104`). 3–4 с — это экстраполяция
  нашего патча; облачный API допускает 4–15 с (`nodes_minimax.py:488-512`).
- Официальный холст — 768p (1344x768). 512 — непроверенная экстраполяция (`constants.py:33-38`).
- В ComfyUI (`MiniMaxH3AddGuide`, `nodes_minimax_h3.py:183-217`) ключевые кадры можно ставить **в любую позицию**,
  в том числе вставлять клип. Поэтому «нет среднего кадра» — ограничение **этого sglang**, а не модели.

### 2.3 Чего нет ни в одном разделе

- **Video-to-video / продолжения ролика / inpainting**: видео-вход существует только как *референс* в ref2va.
  Роли «исходное видео для продолжения» в контракте нет. По коду.
- **Ключевой кадр в середине ролика**: только позиции 0 и −1 (`task_profiles.py:74-78`), хотя валидация
  `frame_index` формально допускает любые индексы `[0, N)` — их отсекает следующая проверка сигнатуры. По коду.
- **Негативный промпт и guidance**: чекпойнт CFG-дистиллирован, `guidance_scale`, `guidance_scale_2`,
  `true_cfg_scale`, `negative_prompt`, `audio_guidance_scale` дают 400 (`video_adapter.py:118-136,201-205`).
- **Генерация без звука**: выход всегда MP4 с одной видео- и одной аудиодорожкой: h264 yuv420p, AAC 32 кГц стерео,
  24 fps, расхождение A/V ≤ 0.25 с (`video_adapter.py:356-551`, `CFG/pipeline_configs/minimax_h3.py:74-76`).
  Тишину можно только попросить промптом.
- **t2va на сервере ref2va и ref2va на сервере fl2va**: нужен перезапуск с другим `VARIANT`.

## 3. Входные параметры запроса

| Поле | Допустимо | Умолчание | Источник |
|---|---|---|---|
| `task` | `t2va`, `fl2va`, `ref2va`, обязателен | — | `video_adapter.py:70-83` |
| `prompt` | непустая строка | — | `request_validation.py:302` |
| `target.short_edge` | целое > 0; 768 — единственное проверенное MiniMax, остальное с предупреждением в лог | — (обязателен) | `request_validation.py:87-94`, `constants.py:38` |
| `target.aspect_ratio` | `auto` или `W:H`. t2va/ref2va: только 21:9, 16:9, 4:3, 3:2, 1:1, 2:3, 3:4, 9:16. fl2va: любое `W:H` от 1:4 до 4:1 | — (обязателен) | `request_validation.py:95-111`, `resolved_plan.py:79-151` |
| `target.duration_seconds` | 3…15 (локальный патч). Обязателен везде, кроме ref2va с одним носителем звука | — | `request_validation.py:101-137` |
| `num_inference_steps` | ≥ 2 (проверка асинхронная). Верхней границы нет, пока `minimax_h3_adaln_online=false` (так в serve.sh), иначе ≤ 65 | 50 | `release_metadata.py:151-168`, `CFG/sample/minimax_h3.py:41` |
| `seed` | целое 0…2^63−1. При `num_outputs_per_prompt>1` у i-го выхода сид = seed+i | случайный | `request_validation.py:369-377`, `CFG/sample/minimax_h3.py:244-255` |
| `num_outputs_per_prompt` / `n` | ≥ 1; сервер держит `--batching-max-size 1`, выходы идут по очереди | 1 | `CFG/sample/minimax_h3.py:98-105` |
| `flow_shift` | > 0, конечное | 12.0 (видео) | `task_profiles.py:156`, `PKG/*/model_index.json` sigma_shift_scales |
| `audio_flow_shift` | > 0, конечное | 3.0 | там же |
| `quality` | `lossless`, `extra-high`, `high` | `lossless` | `release_metadata.py:169-226` |
| `imgvid_cond_noise_aug_for_inference` | 0…1 | 0.999 | `stages/denoising.py:60-84`, `denoise_loop.py:30` |
| `audio_cond_noise_aug_for_inference` | 0…1 | 1.0 (чистый референс) | `denoise_loop.py:32` |
| `output_mode` | только `decoded_files` | — | `video_adapter.py:206-211` |
| `uri` условия | локальный путь, `file://` (только localhost), `http(s)://`, `data:`/`base64:`, `tar+offset:` / `tar+b64header:`. `s3://` не поддерживается | — | `material_io.py:761-878` |

Производные величины:

- **Кадры**: `round(duration·24)`, затем вверх до вида 17n+5 (`time_request.py:5-10`). 3 с → 73 кадра (3.0417 с,
  подтверждено пробой: в логе `"seconds":"3.041667"`). Верхняя граница: 15 с → 362 кадра = 15.083 с.
  Допустимые длины: 73, 90, 107, 124 … 362 кадров (n = 4…21). Сервер **сам** округляет вверх. Требование панели
  «длительность ровно на сетке» (`sglang_args.py:170-177`) — её собственная строгость, а не требование сервера.
- **Холст** (`resolved_plan.py:114-182`): короткая сторона = `short_edge`, длинная = `short_edge·ratio`; если площадь
  больше 768·1344, пропорционально ужимается; обе стороны округляются до ближайшего кратного 32. Пример из
  пробы: `short_edge 512 + 16:9` → **896x512**.
- **Картинка-референс** всегда ресайзится до короткой стороны 2048, кратно 32, с апскейлом, без ограничения площади
  (`reference_encoding.py:125-178`). Каждая картинка дорогая по токенам независимо от размера исходника.
  Отсюда VRAM-предел на число референсов. По коду; связь с VRAM — гипотеза, совпадающая с пробой панели.
- `quality:"high"` на этой машине недостижим: он проверен только на 4×H200, `num_gpus=4`, без квантизации,
  с t2va 1344x768x124f и 50 шагами (`CFG/pipeline_configs/minimax_h3.py:133-200`, `release_metadata.py:23-32`).
  `extra-high` включает только фьюзинг ядер (`sampling_params.py:58-59`). По коду.

## 4. Что код явно запрещает: тексты ошибок для валидации в UI

Колонка «Когда»: **400** — синхронный ответ на POST `/v1/videos` (валидация и pre-queue probe,
`video_api.py:733-737,756-783`); **async** — job принят, потом `status: failed`, текст в `error.message`.

### 4.1 Задача и раздел

| Условие | Текст | Когда | Где |
|---|---|---|---|
| нет `task` | `task is required for MiniMax H3; supported tasks: fl2va, ref2va, t2va` | 400 | `video_adapter.py:70-74` |
| неизвестная task | `unsupported MiniMax H3 task 'x'; supported tasks: fl2va, ref2va, t2va` | 400 | `video_adapter.py:79-83` |
| task не из этого раздела | `task 't2va' is not served by MiniMax H3 partition 'ref2va'; supported tasks: ['ref2va']` | **async** (проба) | `release_metadata.py:129-133` |
| `num_inference_steps` < 2 | `MiniMax H3 requires num_inference_steps >= 2 because its video/audio sigma schedules include both interval endpoints` | async | `release_metadata.py:151-155` |
| `quality:"high"` | `quality="high" ...` (проверка развёртывания: нужно 4 GPU) | async | `release_metadata.py:174-226` |
| `quality` вне списка | `quality must be one of ['lossless', 'extra-high', 'high'], got ...` | 400 | `video_adapter.py:111-114` |

### 4.2 Target

| Условие | Текст | Где |
|---|---|---|
| нет `target` | `target is required and must be an object` | `request_validation.py:82-83` |
| `short_edge` не целое / ≤ 0 | `target.short_edge must be an integer` / `... must be positive, got N` | `:87-89` |
| aspect не из списка (t2va/ref2va) | `target.aspect_ratio for task 'ref2va' must be 'auto' or one of ['21:9', '16:9', '4:3', '3:2', '1:1', '2:3', '3:4', '9:16'], got '5:4'` | `:102-111` |
| aspect не `W:H` (fl2va) | `target.aspect_ratio must be 'W:H' or 'auto', got ...` / `... must be integer 'W:H'` | `resolved_plan.py:79-93` |
| aspect за пределами 1:4…4:1 | `adapt_shape_v1 ratio must be within the inclusive range 1:4 to 4:1, got W:H` | `resolved_plan.py:147-151` |
| нет длительности (кроме ref2va со звуком) | `target.duration_seconds is required` | `request_validation.py:112-114` |
| длительность вне 3…15 | `target.duration_seconds must be in [3, 15], got X` | `:127-136` |
| ref2va без длительности и без носителя звука | `target.duration_seconds is required, or exactly one audio reference to derive duration from (including video/video_audio soundtracks; task 'ref2va')` | `:342-347` |
| ref2va без длительности, носителей звука > 1 | `target.duration_seconds is required when multiple audio-bearing references are provided` | `:348-352` |
| длина звука вне 3…15 (длительность по звуку) | `audio reference duration must be in [3, 15] seconds, got X` | `prequeue.py:155-164` |
| длительность по звуку, но у video нет звука | `audio-derived target duration requires exactly one probed condition with an audio stream, got 0` | `prequeue.py:140-144` |

### 4.3 Conditions

| Условие | Текст | Где |
|---|---|---|
| t2va с условиями | `conditions must be empty for task 't2va' (got N entries)` | `request_validation.py:153-158` |
| fl2va/ref2va без условий | `conditions requires at least one entry for task 'fl2va'` | `:160-163` |
| fl2va > 2 условий | `conditions allows at most 2 entries for task 'fl2va', got N` | `:172-179` |
| лишний ключ | `conditions[i] has unknown fields: [...]` | `:190-192` |
| role не keyframe/reference | `conditions[i].role must be keyframe or reference, got 'first'` | `:194-200` |
| пара (role, type) не разрешена задачей | `conditions[i]: task 'fl2va' does not allow condition role='reference' type='image'` | `task_profiles.py:134-137` |
| keyframe без `frame_index` | `conditions[i].frame_index must be an integer` | `request_validation.py:210` |
| `frame_index` вне диапазона | `conditions[i].frame_index must be -1 or in [0, N) after 17n+5 frame alignment, got K` | `:219-224` |
| два кадра на одной позиции | `conditions[i].frame_index resolves to K, already bound by conditions[j]` | `:225-229` |
| сигнатура кадров не [0] / [-1] / [0,-1] | `conditions for task 'fl2va' must include one or two ordered image/keyframe entries with frame_index [0], [-1], or [0, -1], got [...]` | `:262-267` |
| ref2va: keyframe без reference | `ref2va keyframes require at least one reference condition; use task 'fl2va' for keyframe-only generation` (проба) | `:268-275` |
| `frame_index` у reference | `conditions[i].frame_index is not allowed for role='reference'` | `:235-236` |
| `start_time_seconds` у image/audio | `conditions[i].start_time_seconds is only allowed for video or video_audio references` | `:240-245` |
| `start_time_seconds` ≥ длины видео или звука | `conditions[i].start_time_seconds must be less than the video duration X, got Y` / `... soundtrack duration ...` | `prequeue.py:196-214` |

### 4.4 Медиафайлы (pre-queue probe, 400)

| Условие | Текст | Где |
|---|---|---|
| файла нет / пустой | `MiniMax H3 material source does not exist or is not a file: PATH` / `... is empty` | `material_io.py:205-212` |
| картинка не JPEG/PNG/WEBP | `MiniMax H3 image material uses an unsupported format` | `material_io.py:378-379` |
| картинка не читается | `MiniMax H3 image material is invalid` | `:376-377` |
| картинка-референс за пределами 1:4…4:1 | `reference image ratio must be within the inclusive range 1:4 to 4:1, got WxH` | `reference_encoding.py:150-154` |
| контейнер аудио/видео не из mov/mp4/m4a/3gp/3g2/mj2/matroska/webm/wav/mp3/flac/ogg | `MiniMax H3 media container format is not allowed` | `material_io.py:404-420` |
| video без видеопотока / audio без аудиопотока | `MiniMax H3 video material has no video stream` / `MiniMax H3 audio material has no audio stream` | `:423-426` |
| `file://` с чужим хостом | `file URI host must be local, got 'host'` | `:789-790` |
| `s3://` | `MiniMax H3 s3:// material URIs require a configured artifact resolver` | `:804-807` (NotImplementedError — скорее всего 500, а не 400: предположение) |

### 4.5 Транспортные поля, которых у H3 нет (400)

`num_frames`, `fps` — `... is not supported: MiniMax H3 derives the temporal shape from target.duration_seconds`
(`video_adapter.py:139-153`). `guidance_scale`, `guidance_scale_2`, `true_cfg_scale`, `negative_prompt`,
`audio_guidance_scale` — `... is not supported: MiniMax H3 serves only the CFG-distilled single-positive-branch checkpoint`
(`:118-136,201-205`). `enable_frame_interpolation`, `enable_upscaling`, `enable_teacache` — отказ
(`:212-221`, `CFG/sample/minimax_h3.py:220-235`). Поля `size` и `seconds` OpenAI-протокола H3 игнорирует:
время и холст берутся только из `target`.

## 5. Минимальный набор проб на живом сервере

Цель — закрыть все пункты «по коду/предположение» для ref2va и fl2va. Общие параметры:
`short_edge 512`, `aspect 16:9` (→ 896x512), `duration_seconds 3` (73 кадра), `seed 1`, `quality lossless`.
Проба «принимает ли» — `num_inference_steps 2`: важен только допуск и прохождение всех стадий, качество картинки
не нужно. Проба «влияет ли условие» — 20 шагов, потому что результат смотрит человек.

Порядок выбран так, чтобы менять вариант сервера один раз (ref2va → fl2va) и один раз вернуть его обратно
(fl2va → ref2va). Перезапуск стоит ~2.5 мин плюс прогрев (в пробе 07.10: старт 02:09:41, готов 02:12:15).

Подготовка (CPU, без GPU), в `~/Projects/h3-bench/inputs/probe-modes/`:
`kf16x9.png` = `inputs/fight-armored-40/opening.png` (896x512); `kf9x16.png` — любая вертикальная;
`ref1.jpg` = `inputs/ref/pic1-face-front.jpg`; `a3_5.wav` — `clip-music/part04.wav`, обрезанный ffmpeg до 3.5 с;
`a10.wav` = `clip-music/part04.wav` (10.0 с); `v_snd.mp4` = `mac-reference/amazonka3.mp4` (896x576, 10.1 с, есть звук);
`v_mute.mp4` — тот же файл с `-an`. Логи — в `~/Projects/h3-bench/logs/probe-modes-YYYYMMDD.log` (не в /tmp).
Перед началом поставить на паузу диспетчер GPU и очередь панели, иначе пробы встанут в общую очередь или
займут GPU, который им не принадлежит. «Идёт» проверять по логу сервера, а не по ответу на POST.

### Фаза A. Сервер `VARIANT=ref2va` (уже поднят диспетчером, перезапуск не нужен)

**A0. Только валидация, без GPU (секунды).** Каждый запрос должен получить 400 с текстом из §4;
успех — совпадение текста. Одним скриптом:
1. fl2va, keyframe `frame_index 10` → сигнатура.
2. fl2va, image/reference → `does not allow condition role='reference'`.
3. ref2va, aspect `5:4` → список aspect.
4. ref2va, `duration_seconds 2.5` → `[3, 15]`.
5. ref2va, только `ref1.jpg` без duration → `duration_seconds is required, or exactly one audio reference`.
6. ref2va, `a10.wav` + `v_snd.mp4` без duration → `multiple audio-bearing references`.
7. ref2va, image/reference с `start_time_seconds` → `only allowed for video`.
8. ref2va, `guidance_scale: 5` → `CFG-distilled`.
9. ref2va, `role: "first"` → `must be keyframe or reference`.
10. ref2va, `v_mute.mp4` как `video_audio` → `has no audio stream`.
11. ref2va, `v_mute.mp4` как `video` без duration → `requires exactly one probed condition with an audio stream, got 0`.
12. fl2va, `[0,-1]`, aspect `5:4` → **ожидаем 200** (fl2va не проверяет список). Сразу после 200 job должен упасть
    async с «not served by partition 'ref2va'» — это заодно и проба асинхронного отказа для fl2va.
13. ref2va, `num_inference_steps 1` → async `num_inference_steps >= 2`.

**A1. ref2va, аудио длиннее ролика** (`a10.wav`, duration 3, 2 шага). Успех: `completed`, mp4 896x512, 73 кадра. Закрывает R5 (обрезку звука). Сам режим только с аудио уже подтверждён эмпирикой (R2).
**A2. ref2va, image + аудио без duration** (`ref1.jpg`, `a3_5.wav`, 2 шага). Успех: `completed`,
`seconds` = 90/24 = 3.75 (3.5 с → 84 кадра → вверх до 90). Закрывает R3 и R4.
**A3. ref2va, image + keyframe `[-1]`** (`ref1.jpg`, `kf16x9.png`, 2 шага). Успех: `completed`. Закрывает R10.
**A4. ref2va, image + keyframes `[0,-1]`** (`ref1.jpg`, `kf16x9.png`, вторая картинка любая, 2 шага). Успех: `completed`. Закрывает R10.
**A5. ref2va, video со звуком + image, `start_time_seconds 2`** (`v_snd.mp4`, `ref1.jpg`, duration 3, 2 шага).
Успех: `completed`. Закрывает R6.
**A6. ref2va, `video_audio` + `video` без звука** (`v_snd.mp4` как video_audio, `v_mute.mp4` как video, duration 3, 2 шага).
Успех: `completed`. Закрывает R7, R8-позитив и R13.
**A7. Влияние аудио-референса (20 шагов)**: `ref1.jpg` + `a3_5.wav` с речью или вокалом, промпт со словами
«the woman from <Picture 1> speaks/sings with the voice from <Audio 1>»; контроль — тот же сид без аудио.
Успех — на слух меняется голос или трек; оценивает человек. Это единственная проба, которая отвечает
на вопрос «что делает аудио-референс»: клонирует тембр, кладёт трек как есть или задаёт только ритм.
**A8. Искажение кадра в ref2va при `auto` (20 шагов)**: `ref1.jpg` + keyframe `kf9x16.png` `[0]`, aspect `auto`.
Ожидаемо: холст 16:9 и сплющенный первый кадр. Успех пробы — подтвердить или опровергнуть. Если подтвердится,
UI обязан сам выставлять aspect по кадру для гибридного режима (сейчас панель шлёт keyframe как есть).

### Фаза B. Перезапуск с `VARIANT=fl2va` (один раз, ~3 мин)

**B1. t2va** без условий, 2 шага. Успех: `completed`, 896x512, 73 кадра. Закрывает F1.
**B2. fl2va `[0]`**, `kf16x9.png`, aspect `auto`, 2 шага. Успех: `completed`, холст 896x512 (взят из кадра). Закрывает F2.
**B3. fl2va `[-1]`**, `kf9x16.png`, aspect `auto`, 2 шага. Успех: `completed`, холст 512x896 (взят из последнего кадра). Закрывает F3 и геометрию от `[-1]`.
**B4. fl2va `[0,-1]`** (`kf16x9.png` + другая 16:9), aspect `auto`, 20 шагов. Успех: `completed`; первый и
последний кадры mp4 визуально совпадают с входами. Закрывает F4; это главный новый режим для панели.
**B5. fl2va `[0,-1]` с aspect `5:4`**, 2 шага. Успех: `completed`, холст ≈ 640x512 (512·1.25 = 640). Подтверждает,
что у fl2va aspect не ограничен списком.
**B6. ref2va на сервере fl2va**, 2 шага. Ожидаем async-отказ `task 'ref2va' is not served by MiniMax H3 partition 'fl2va'; supported tasks: ['t2va', 'fl2va']`.

Затем вернуть `VARIANT=ref2va` (или отдать GPU диспетчеру) и снять паузу с очереди.

Итого: 13 валидационных запросов, 6 + 5 генераций на 2 шага, 3 генерации на 20 шагов, 2 перезапуска.
Оценка времени — гипотеза, экстраполяция из эмпирики. Опорная точка: 896x512, 243 кадра, 50 шагов = ~570 с
(`tango-ref-p1`). Отсюда ~11 с на шаг при 10 с ролика, при 73 кадрах ~3–4 с на шаг. Получается ~30–60 с на
2-шаговую пробу (вместе с кодированием и декодированием) и ~1.5–2.5 мин на 20-шаговую. Важная оговорка: первый
рендер после старта сервера заметно медленнее (`h3-bench/docs/h3-shell-howto.md`). Всего вместе с перезапусками
— около 40–60 мин.

## 6. Что осталось непроверенным

- Смысл аудио-референса (тембр, трек или ритм) и видео-референса (движение, персонаж или стиль): код
  показывает только, *как* материал подаётся, но не *что* модель из него берёт. Ответ даст A7, плюс аналогичная
  проба для видео, если владельцу нужен этот режим.
- Предел числа референсов: кодом не ограничен; VRAM-предел известен из комментария панели (6 портретов
  при 10 с), а не из лога. Если нужен точный предел для UI, пробовать 6/7/8 картинок при 3 и 10 с.
- Работают ли гибридные keyframes на этом **pruned** чекпойнте ref2va осмысленно. В профиле сказано
  «for hybrid checkpoints» (`task_profiles.py:9-11`), а является ли наш таким — не проверено. A3/A4/A8 покажут.
- Что вернёт `s3://` (`NotImplementedError` — вероятно 500, а не 400).
- Две ошибки из прошлых прогонов без объяснения. Первая — `mat1 and mat2 shapes cannot be multiplied (1x8 and 2688x16)` в
  `MiniMaxH3DenoisingStage`, 6 раз в `h3-bench/logs/serve-20260914-0243.log`. Вторая — 24 отказа HTTP 400 на beach,
  `h3-bench/logs/beach.log:2-9`: тела ответа ранние раннеры не сохраняли. Панель тело 400 должна логировать всегда.
- Режимы, которых **нет ни в одном прошлом прогоне**: video- и video_audio-референсы, `start_time_seconds`, keyframe
  `[-1]` и `[0,-1]` (в обоих разделах), больше 3 картинок-референсов, несколько аудио, aspect 4:3, 21:9, 1:1, 3:2.
  Именно они составляют фазы A3–A6 и B3–B5.
- Официальная карточка модели (HF `MiniMaxAI/MiniMax-H3`) локально не скачана: в `PKG` нет README, а
  `PKG/.cache/huggingface` содержит только служебные файлы загрузки. Шаблоны Comfy (i2v/t2v/r2v) указаны ссылками
  в `runtime-components/README.md`.
