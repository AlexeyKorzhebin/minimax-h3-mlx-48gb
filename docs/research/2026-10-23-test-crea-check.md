# Test CREA после переездов на сервере: проверка 23.10.2026

Проект: /Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/Test CREA (не правился). Проверка только чтением, настоящий запуск НЕ делался: он гарантированно упал бы (см. п.2-3).

## Вердикт
Сейчас пользоваться нельзя. Три независимые поломки, каждая блокирует; ни одна не связана с удалением Z-Image/Qwen GGUF или диспетчером.

## Что используется (пути на alex-neuro) и состояние
| Что | Путь | Есть |
|---|---|---|
| Веса, которые грузит resident | Turbo INT8-ConvRot: /home/alex/Models/Image/Krea-2/INT8-ConvRot/Turbo/checkpoint.safetensors (через native-int8-Turbo-artifact.json) + Turbo/ (TE, VAE) | да |
| Raw / Derived-Comfy | Raw/, Derived-Comfy/Raw-bf16.safetensors (resident не грузит) | да |
| Identity-Edit | /home/alex/Models/Image/Krea-2/LoRA/Identity-Edit/krea2_identity_edit_v1_2.safetensors | да |
| KNP (сила 1.5) | /home/alex/Models/Image/Krea-2/LoRA/KNP/KNP_000003000.safetensors | да |
| Python/venv воркера | /home/alex/Projects/comfy/.venv/bin/python + код /home/alex/Projects/comfy/ComfyUI (это рабочий LTX-comfy: зависимость от чужого проекта) | да |
| Рабочая папка сессий | /home/alex/Projects/krea2-comparison (gate-*.json, native-sequential.py) | да |
| Выход | /home/alex/Outputs/krea-comparison/<session> | да |
| Lock | сервер: /home/alex/Projects/qwen-image21-lab/generation.lock (inode 11807247, совпадает; этот же lock у диспетчера); Мак: ~/.hermes/image-studio/generation.lock | да |
| Порт | у resident порта нет: управление через ssh + файлы inbox/, live.json | - |
Старые пути миграции resolver не понадобились: проект уже пишет новые /home/alex/Models/..., /home/alex/Projects/...

## Поломки
1. Мак: ./python3 в проекте = симлинк на ~/.pyenv/versions/3.12.3/bin/python3 -> python3.12, а самого бинарника python3.12 в этой папке больше нет (папка bin обновлена 13.10; Pillow 10.1.0 лежит в lib/python3.12/site-packages, интерпретатора нет). `zsh run.zsh --dry-run` падает: "run.zsh:5: no such file or directory .../Test CREA/python3". Обход для проверки: PYTHONPATH=~/.pyenv/versions/3.12.3/lib/python3.12/site-packages /opt/homebrew/bin/python3.12 work/krea-comparison/run_user.py --dry-run: печатает конфиг (identity, 864x1152, 12 шагов, ref-boost 2, фото references/portrait.jpg, seed случайный). Починка: заново создать интерпретатор (pyenv install 3.12.3 + pip install Pillow, или симлинк на homebrew python3.12 + Pillow).
2. Сервер: gate-файлы заморожены 01-02.10 и сверяются строго. resident-controller.py при старте требует: (а) id контейнера Qwen == gate (38b8d886...), сейчас 564a4645... (контейнер qwen38-sglang-dflash2-compose пересоздан 22.10, StartedAt другой); (б) MainPID systemd-сервисов fan-controller/gpu-thermal-guard/cooler-mqtt == gate (1679/1684/3981), сейчас 1697/1702/2965 (после перезагрузки). Проверено сравнением gate-identity-Turbo-20261002-120643-00ab4414.json с живым docker inspect/systemctl: все три "False". Контроллер упадёт на assert, не дойдя до загрузки весов. Обход: заново выпустить gate под текущие идентичности (правка серверных файлов; я не делал).
3. Мак: work/krea-comparison/resident-session.json остался от провалившейся сессии 06.10 (resident-20261006-181543-588ce164, closed.json: "Resident worker failed" после load_end). `resident.py start` начинается с assert not POINTER.exists() и откажется. Указатель можно переименовать вручную (как делает stop), но это правка проекта; я не делал. Причина сбоя 06.10 по логам не видна.
Также qwen.py stop/start сверяют Qwen с gate-post-full (тот же Id) и упадут; Qwen теперь надо вести через ~/Projects/qwen/qwen.sh (профиль shared32).

## Состояние сервера на момент проверки
Диспетчер :8790 — own пуст, foreign пуст, Qwen остановлен, GPU 15 МиБ, 43 C. ComfyUI :8189 не запущен (запускается диспетчером по acquire). Docker: студия (studio-api/worker, h3-panel) работают, Qwen и Draw Things exited. Мак: pmset sleep 1 (но caffeinate сейчас держит).

## Совместимость с диспетчером и безопасный порядок
- resident диспетчер не спрашивает. Но берёт тот же generation.lock (flock EX NB) на всё время сессии, требует пустой nvidia-smi и остановленный Qwen. Диспетчер этот lock уважает ("busy" -> wait), а чужой GPU-процесс называет foreign: ждёт, не трогает. Студия при живом resident получит "ждать"; задачи не пропадут, не сломаются.
- Обратный риск: если comfy-studio поднят/держится владельцем, контроллер resident упадёт на assert (lock/gpu_apps) — безопасно, но молча для владельца. Контроллер НЕ видит клиентов диспетчера и не вызывает /release.
- Порядок для владельца (после починки п.1-3): 1) `curl 127.0.0.1:8766/api/studio/health` и nvidia-smi — нет идущей задачи студии; 2) на сервере POST 127.0.0.1:8790/release для owner (и studio-worker, если пуст); 3) Qwen остановлен (qwen.sh status); 4) caffeinate -dimsu zsh run.zsh --count N; 5) resident.py stop; nvidia-smi пуст; 6) студия снова берёт карту сама. Не оставлять resident висеть: студия будет ждать.

## Вариант через ComfyUI студии
- Уже работает без разрушения: `ssh alex-neuro python3 /home/alex/Outputs/studio/tools/make_image.py krea2 "<prompt>" --lora KNP/KNP_000003000.safetensors:1.5 --release` (берёт карту через диспетчер, Turbo INT8 ComfyOrg, шаблон krea2-t2i). Файлы LoRA видны ComfyUI через extra_model_paths (comfy_krea2_lora -> Krea-2/LoRA, имена KNP/..., Identity-Edit/...). ComfyUI при проверке не был запущен, /models/loras не опрашивал.
- Фото-референс там НЕ сделать как есть. Шаблон krea2-t2i — только t2i (CLIPLoader/KSampler/..., нет LoadImage); make_image.py флага референса не имеет. Identity-Edit в resident работает не как обычная LoRA: свои хуки (identity_nodes.py/identity_inputs.py, 256 хуков, ref-boost, grounding Qwen3-VL 1024 px) поверх native-comfy-worker. Стандартного узла ComfyUI для него нет.
- Что нужно: (1) кастомный узел ComfyUI в ~/Projects/comfy-h3/custom_nodes из identity_nodes.py (+ LoadImage, ref-boost, grounding) и граф krea2-identity-edit.graph.json + card в студийных шаблонах; (2) параметр --reference в make_image.py (загрузка фото в ComfyUI input); (3) отдельная проверка, что Identity-Edit-веса (1.8 ГБ, формат diffusers/PEFT?) грузятся без resident-кода; (4) тест на тех же seed/промпте против результата resident (outputs/archmage-reference-comparison/resident). Это реальная разработка, не конфигурация.
- Альтернатива дешевле: починить п.1-3 (новый gate под текущие Qwen/PID, интерпретатор, указатель) — resident заработает как раньше.

---
# Дополнение: переделка на ComfyUI студии (сделано делом, 23.10 вечер)

## Identity-Edit в ComfyUI v0.42: рабочий путь найден
- Нативного пути нет: в comfy-h3 нет узла Krea2 edit (ReferenceLatent/шаблоны Comfy-Org к Krea 2 edit не подходят, это t2i и Style Reference). Но Identity-Edit в resident и так был ComfyUI-узлами: пакет ComfyUI-Krea2Edit (identity_nodes.py проекта, sha 43c5f9aa…, как закреплено в identity_inputs.py). Я положил его на сервер: /home/alex/Projects/comfy-h3/custom_nodes/ComfyUI-Krea2Edit/__init__.py (единственное изменение comfy-h3; откат: удалить папку и перезапустить движок). Узлы: Krea2EditGroundedEncode (Qwen3-VL с фото), Krea2EditModelPatch (фото-токены, ref-boost).
- Граф: UNETLoader(krea2_turbo_int8) -> LoraLoaderBypassModelOnly(Identity-Edit, 1.0) -> LoraLoaderModelOnly(KNP, 1.5) -> Krea2EditModelPatch(vae, source_image, target_latent, ref_boost 2); два Krea2EditGroundedEncode (промпт и пустой, фото 1024 px); SamplerCustom euler, cfg 1, ManualSigmas = shifted_sigmas(12, mu 1.15) как в resident. Граф: docs/research/2026-10-23-krea2-identity-edit-knp.graph.json.
- Ловушка: два LoraLoaderBypass подряд, активна только последняя (общий ключ инъекции): с обоими bypass получалась бледная копия фото (проверено: только KNP или оба LoRA нулевые = копия; только Identity = рыцарь). Поэтому KNP идёт обычным LoraLoaderModelOnly. С ним картинка по тому же seed/промпту/KNP 1.5 (624x832) практически совпала с resident (рыцарь, поза, фон, лицо).
- Скорость: 624x832 около 12-14 с, 864x1152 около 24 с на картинку тёплым движком (resident 26-34 с).

## Запускатель (файлы проекта владельца: добавлены, старые не менялись)
- Test CREA/run-comfy.zsh, Test CREA/work/comfy/run_comfy.py; раздел в README. Использует run_user.prepare() и compose_prompt.py как есть; ./python3 проекта сломан, поэтому run-comfy.zsh берёт первый рабочий python (сейчас homebrew 3.12) и работает без Pillow.
- Серверная часть: scripts/make_image.py (репо студии, не закоммичено) расширен: --prompt-file, --reference/--ref-boost/--grounding-px (Identity-Edit), --machine (строка RESULT). Прежний интерфейс цел. Скопирован на сервер, бэкап make_image.py.bak-20261023 рядом. Если узлов Krea2Edit нет, make_image с --reference выходит с ошибкой, run_comfy печатает «фото-референс в ComfyUI пока не поддержан…» и код 2.
- Проверено: --dry-run (обе ветки); --no-reference --prompt-file prompts/scene.md --count 2 --seed 145: 896x1152, 28 с (с загрузкой) и 8 с, архимаг с мантией, посох, библиотека, KNP-лицо; серия outputs/series/series-scene-20261023-193918-385565; с фото `--count 1 --seed 145 --release`: 864x1152, 38 с, лицо похоже на portrait.jpg, мантия с серебром, библиотека (series-archmage-20261023-194014-6a8e60). Капюшон на фото есть, хотя в промпте «No hood»: так же вела себя и resident. После последнего прогона карта отдана (nvidia-smi 15 МиБ, диспетчер own пуст).
- Не проверено: ветка «узлов нет» (код 2) только прочитана, не прогонялась (узлы удалять не стал); --release на серии из нескольких картинок (флаг уходит только в последнюю), фото с ориентацией EXIF, вертикальные аспекты на живую (только dry-run 9:16 -> 720x1280).
- Остаточные риски: custom node лежит в рабочем comfy-h3 (обновление ComfyUI может его сломать); входные файлы серий копятся в /home/alex/Outputs/comfy-h3/_testcrea-in/ (чистить вручную); resident-путь по-прежнему сломан (разд. выше).
