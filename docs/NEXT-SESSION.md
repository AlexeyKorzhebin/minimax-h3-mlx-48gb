# С чего начать следующую сессию (записано 23.10.2026)

## Где всё лежит

| Что | Где | Состояние |
|---|---|---|
| Студия S0–S2 | `main` (e9863c78 и позже), сервер alex-neuro :8766 | выложено, работает; шаг 4 — кадр Krea 2 |
| Срез S2b «Картинка» | worktree `~/worktrees/studio-s2b`, ветка `studio/s2b`; план `docs/superpowers/plans/2026-10-23-studio-s2b-image.md` (ред. 2.1) | задачи 1–3 приняты; **задача 4 сделана (d8faa8e3), ревью не проведено**; дальше 5–15 |
| llm-kit | `~/Projects/llm-kit`, github.com/AlexeyKorzhebin/llm-kit, тег `v0.1.0` | готов; задача 10 (перенос части B → план S2c в студию) ждёт слияния S2b в main |
| Журналы SDD (решения, парковки) | `~/worktrees/studio-s2b/.superpowers/sdd/2026-10-23-studio-s2b-image/progress.md`; `~/Projects/llm-kit/.superpowers/sdd/2026-10-23-llm-kit/progress.md`; S2 — `~/worktrees/studio-s2/.superpowers/sdd/2026-10-22-studio-s2-s3/progress.md` | читать первыми, `git log` — вторым |
| Инструкция владельцу | `docs/HOWTO-owner-video-and-images.md` | владелец тестирует сам |

## Первые шаги

1. Спросить владельца, что он увидел при самостоятельных пробах (студия, `make_image.py`, ComfyUI) — замечания важнее плана.
2. S2b: ревью задачи 4 (`d8faa8e3`, линтер LI1–LI8; известное: мутация «незакрытая кавычка» краснеет зависанием —
   нужен тест с тайм-аутом и исправление цикла), затем задачи 5–15 по SDD (исполнитель sonnet-dev, ревью с мутациями,
   задачи 8–12 и выкладка — opus). Перед каждым диффом плана — `git apply --check`.
3. Задача 10 S2b (воркер): подключить `InventoryLoop` (из ревью задачи 3). Задача 12: диспетчер — Qwen-LLM рядом с Krea/Z-Image.
4. После S2b: итоговое ревью ветки, слияние в main, выкладка, приёмка владельца; затем S2c (часть B плана llm-kit → свой план
   в студии, проверка Codex) и S3 (эскизы H3, дубли, цепочка).

## Решение владельца 23.10: фото-референс и выбор LoRA — в S2b

Владелец: «да, включать». Режим «Картинка» в S2b получает:
- **фото-референс** для Krea 2 через Identity-Edit (узлы `ComfyUI-Krea2Edit` уже в `~/Projects/comfy-h3/custom_nodes/`;
  проверенный граф — `docs/research/2026-10-23-krea2-identity-edit-knp.graph.json`; параметры ref-boost 2, grounding 1024,
  Identity-Edit 1.0; ловушка: два bypass-LoRA подряд = активен только последний — KNP через `LoraLoaderModelOnly`);
- **выбор LoRA** в интерфейсе (список из ComfyUI `/models/loras` по папкам модели, сила; KNP 1.5 как в Test CREA).

Что сделать первым делом: спека ред. 3.5 (§19 + снять из §2 не-цели «картинка по референсу» и «выбор LoRA в UI»;
отпечаток весов LoRA во входах варианта, И15; загрузка фото — через существующий O6/ресурсы), затем план S2b ред. 3
(новые задачи: шаблон `krea2-ref` + карточка, O29/O30 с `reference_asset_id` и `loras[]`, мастер — выбор фото и LoRA),
проверка Codex, потом продолжить реализацию (задачи 4-ревью, 5+). Образец CLI — Test CREA `run-comfy.zsh`
и `make_image.py --reference`.

## Что поменялось на сервере 23.10 (вне кода)

- Диспетчер переключён на клон студии `~/Projects/h3-studio` (откат — `scripts/studio_dispatcher_rollback.py`).
- Ключ CAILA смонтирован в контейнер как `/run/secrets/studio.env:ro` (из `/home/alex/Outputs/h3-panel/.env`).
- `~/Projects/comfy-h3/extra_model_paths.yaml`: добавлены LoRA Krea 2 и Z-Image (копия до правки — `.bak-20261023`).
- `/home/alex/Outputs/studio/tools/make_image.py` — скрипт владельца для картинок (с фото-референсом Identity-Edit).
- `~/Projects/comfy-h3/custom_nodes/ComfyUI-Krea2Edit/` — узлы Identity-Edit для Krea 2 (откат: удалить папку, перезапустить движок).
- Test CREA (`~/Yandex.Disk.localized/Projects/Test CREA`): добавлен `run-comfy.zsh` через ComfyUI студии; старый `run.zsh` (resident) не работает, не чинили.
- Старые комплекты Z-Image (Draw Things) и Qwen-Image 2.1 (GGUF) удалены 22.10 (LoRA сохранены); Hermes image-studio,
  `zimage.sh`, `smoke.sh` поэтому не работают.
- Контейнер Qwen-LLM пересоздан с профилем shared32, остановлен.
- Старая панель :8765 не используется (решение владельца), её не защищаем и не удаляем.
