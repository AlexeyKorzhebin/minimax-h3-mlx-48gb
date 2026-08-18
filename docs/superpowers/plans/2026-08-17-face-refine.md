# Боевой face-refine — план

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Проход «улучшить лица» по готовому ролику: CLI-ядро `h3 face-refine <mp4>` +
кнопка на готовой задаче в веб-морде (kind: "face-refine" в существующей очереди).

**Архитектура:** спека `docs/superpowers/specs/2026-08-17-face-refine-design.md` — там
таблица зафиксированных параметров (sigma 0.25 потолок, окно 56, шаг 40–44, кроссфейд ≥12,
рамка = функция кадра) и референс-скрипты экспериментов. Механика v2v переносится из
`~/Research/TestVideo/face-refine-exp/v2v_face_refine.py`, не переизобретается.

## Global Constraints

- Чекаут: `/Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb`, ветка main.
- Тесты: `env -u NODE_OPTIONS .venv/bin/python -m pytest tests/ -q` перед каждым коммитом
  (в форграунде, timeout 300000+). Известный фон: 20 падений test_cli.py из-за удалённого
  4-битного чекпойнта — они не от тебя, число прочих падений должно быть 0.
- GPU-задачи: перед инференсом проверить `curl -s --noproxy '*' http://localhost:8765/api/state`
  (paused, running пуст) и `pgrep -fl "h3_48gb|generate"`. Боевой сервер 8765 не трогать;
  для веб-смоука порты 18765/18766.
- Параметры из таблицы спеки — зашить умолчаниями, sigma клиппится потолком 0.25.
- Комментарии в коде — в стиле проекта (плотные докстринги «почему», см. h3_48gb/queue.py).

## Task 1: facetrack — детекция и трек рамки (CPU)

**Files:** Create `h3_48gb/facetrack.py`, `tests/test_facetrack.py`.
**Produces:** `detect_track(frames_iter, every=5) -> FaceTrack | None`, где FaceTrack —
dataclass со `box(frame_idx) -> (x, y, w, h)` (float, сглаженная функция кадра),
`detected(frame_idx) -> bool` (был ли реальный детект рядом, для fade_out),
`median_area`, `n_frames`. YuNet ONNX из `~/models/yunet/face_detection_yunet_2023mar.onnx`;
файла нет → RuntimeError с URL скачивания (образец — cli.py checkpoint_not_found).
Несколько лиц → самое крупное по медиане площади. Нет детектов вообще → None.
Детект раз в 5 кадров, линейная интерполяция между, savgol по центру и размеру
(scipy уже в зависимостях), экстраполяция константой на хвостах без детекта.
Тесты: синтетические кадры с нарисованным «лицом» не годятся для YuNet — тестировать
трек-математику отдельно от детектора (детектор мокается списком боксов), плюс один
интеграционный тест с реальным кадром из tests/data (вырезать кадр с лицом из
~/Research/TestVideo/face-refine-exp/round2/SOURCE-crop.mp4, положить png в tests/data/).
Скачай ONNX (curl с гитхаба opencv_zoo, ~230 КБ) в ~/models/yunet/ в начале задачи.

## Task 2: кроп/вклейка (CPU)

**Files:** Create `h3_48gb/facepaste.py`, `tests/test_facepaste.py`.
**Consumes:** FaceTrack из Task 1. **Produces:**
`crop_window(frames, track, scale=2.75, out_size=(448, 288)) -> (crops, CropGeometry)` —
кроп ×scale вокруг рамки с клипом у границ кадра, лансцош до out_size; CropGeometry хранит
прямоугольники по кадрам для обратной вклейки.
`paste_back(frames, refined, geometry, track, feather=0.10) -> frames` — даунскейл
результата к рамке, вклейка по растушёванной прямоугольной маске (мягкая рамка 10%
стороны), на кадрах track.detected()==False — линейный fade_out вклейки за 6 кадров.
Тесты: круговая проверка (кроп→вклейка без рефайна ≈ исходник вне маски побитово, внутри
близко), клип у краёв кадра, fade_out.

## Task 3: оконный v2v-движок (GPU)

**Files:** Create `h3_48gb/facerefine.py`, `tests/test_facerefine.py`.
**Consumes:** crops из Task 2. **Produces:**
`refine_clip(crops, *, sigma=0.25, seed=42, window=56, step=42, crossfade=12,
checkpoint, adaln_dir) -> refined_frames`.
Перенос механики из референса `~/Research/TestVideo/face-refine-exp/v2v_face_refine.py`:
encode → сэмпл постериора → f16 round-trip → нормализация → scale_noise(t=1−sigma) →
patchify → цикл n_cond_v=0 → decode; аудио-строки — чистый шум. Окна по 56 кадров шагом
42 (последнее окно прижимается к хвосту), один seed на все окна, кроссфейд 12 кадров по
декодированным пикселям в зонах перекрытия (референс round2/overlap/crossfade.py).
Партиальная таблица: `ensure_partial_table(sigma, checkpoint, adaln_dir)` — ищет
`adaln_face_s{ЧЧЧ}_4pt_turbo.safetensors` в adaln_dir, нет — бейкает через механику
bake_partial.py (обёртка над scripts/bake_adaln.py, tail_split_sigmas, сетка 4 точки).
sigma > 0.25 → ValueError. Хвост короче окна: окно прижимается назад (перекрытие больше),
клип короче 56 кадров — одно окно по фактической длине на нативной сетке (обрезать до
ближайшего 17k+5 сверху вниз, как эксперименты).
Тесты без GPU: нарезка окон/перекрытий на разных длинах (56, 84, 361, 40), клип sigma,
выбор имени таблицы; GPU-смоук (помечен @pytest.mark.slow): refine_clip на 56 серых
кадрах не падает и возвращает форму. Перед GPU-смоуком — проверка занятости из констрейнтов.

## Task 4: CLI `h3 face-refine` (склейка ядра)

**Files:** Modify `h3_48gb/cli.py`, Create `tests/test_cli_facerefine.py`.
**Consumes:** Task 1–3. **Produces:** подкоманда
`face-refine <input.mp4> [--sigma 0.25] [--out <path>] [--checkpoint ...] [--adaln-dir ...]`:
читает кадры (ffmpeg), detect_track → нет лица → честный выход «лиц не найдено», файл не
создаётся; иначе crop → refine_clip → paste_back → сборка mp4 с копией аудио исходника
(`-c:a copy`), выход по умолчанию `<стем>-faces.mp4` рядом. Прогресс — как у generate
(строки стадий). checkpoint_identity_extra: digest исходника (sha256 первых 8 МБ + размер),
sigma, окно/шаг/кроссфейд, версия facetrack. Умолчания чекпойнта/таблиц — как у веб-формы
(8bit-full), НЕ как старые CLI-умолчания.
Тесты: аргументы/умолчания/клип sigma, «лиц не найдено» (мок facetrack → None), сборка
имени выхода; без GPU (refine_clip мокается).

## Task 5: интеграционные ворота этапа 1 (GPU, руками контроллера или субагентом)

Прогнать на двух источниках экспериментов:
`.venv/bin/python -m h3_48gb face-refine ~/Research/TestVideo/29-кот/20260815-1338-athletic-nude-women-sovi-s1/h3-athletic-nude-women-sovi-s1-448x288.mp4`
и на centaur-official (сильное движение, остаточный риск R3). Глазами: лицо лучше
исходника, стыков окон не видно, вне лица ролик побайтово-близок к исходнику, аудио на
месте. Стоп-кадры сравнения — в отчёт. Результаты в
~/Research/TestVideo/face-refine-exp/production-test/.

## Task 6: веб — kind face-refine + кнопка

**Files:** Modify `h3_48gb/web.py`, `h3_48gb/worker.py` (если args-диспетч требует),
`h3_48gb/webui/app.js`, `h3_48gb/webui/index.html`, `h3_48gb/webui/style.css`,
Create/extend `tests/test_web_facerefine.py`.
**Produces:** `POST /api/jobs/<id>/face-refine` — только для state=done задач с mp4;
submit в очередь задачи с args `["face-refine", "<mp4>", "--sigma", "0.25"]` и
kind="face-refine" в note/метаданных; оценка длительности = ceil(кадры/42) × 2.2 мин + 1 мин.
Кнопка «улучшить лица» в карточке готовой задачи (рядом с «дублировать»), confirm с
оговоркой «чинит мыльные мелкие лица, не чинит сломанную анатомию; ~6 мин GPU на 5 с
ролика». Результат-задача показывается в «Закончилось» обычной строкой, output_stem —
`<папка исходной задачи>/<стем>-faces` (та же папка, без релокации в новую).
Дизайн кнопки — по канону variant-reference.html (вторичная кнопка, не янтарная).
Тесты: маршрут (404 на не-done/чужой id, 409 на повтор пока pending), submit-параметры,
оценка; смоук на 18765.

## Task 7: закрепление

BACKLOG: пункт 1 очереди → сделано (коммиты, вердикт). docs/RESULTS.md: секция
«Face-refine» с параметрами и стоимостью. README: подкоманда + маршрут. Полный прогон
тестов, пуш. Рестарт боевого сервера (scripts/web-stop.sh && scripts/web-start.sh)
ПОСЛЕ пуша.
