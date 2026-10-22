# Проверка Codex: план S2–S3 ред. 4

2026-10-23, коммит c3ff0d80

Нашла **2 блокера, 3 важных замечания и 2 мелких**. Проверяла спеку именно ред. 3.2 из `c88648bb`; решения журналов ред. 2–4 не пересматривала.

**Блокеры**

1. **Сохранение любого успешного результата роняет воркер.** В `ImageKind.finish` и `RenderKind.finish` параметр `files: list[Path]` затеняет импортированный модуль `files`. Новые вызовы `files.stored_path(...)` дают `AttributeError: 'list' object has no attribute 'stored_path'`. Воспроизведено на извлечённых методах в памяти. Исключение не перехватывается `_collect`, задача остаётся в `verifying`. Нужно переименовать параметр либо импорт. Задачи 8 и 12: [стр. 4597](/Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb/docs/superpowers/plans/2026-10-22-studio-s2-s3.md:4597), [стр. 7422](/Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb/docs/superpowers/plans/2026-10-22-studio-s2-s3.md:7422).

2. **Диффы задачи 7 не применяются к указанному `studio-s2`.** Реальный `git apply --check` возвращает ошибки для `app.py:289`, `ops.py:6`, `schemas.py:112`. Сигнатура `create_app` уже соответствует исправлению, но остальные контексты требуют отсутствующих частей S1: маршрута применения предложения, импорта `scenarist`, класса `ApplyRequest`. Поэтому утверждение о проверке диффов на `5f3feedd` неверно. Нужен явный SHA завершённой базы S1 и повторная проверка всей последовательности. [Стр. 3084](/Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb/docs/superpowers/plans/2026-10-22-studio-s2-s3.md:3084), [стр. 3378](/Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb/docs/superpowers/plans/2026-10-22-studio-s2-s3.md:3378), [стр. 3553](/Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb/docs/superpowers/plans/2026-10-22-studio-s2-s3.md:3553).

**Важное**

3. **Общий `queue_order` подключён только к воркеру; расчёт O10 остался прежним.** `plan_render` сортирует сцены по `idx`, затем передаёт их шаблоны в `switches`, обходя `queued_in_order`. Пример: загружен fl2va, первая независимая сцена — refs, вторая — from_frame. План считает две смены, **32 с**; воркер выполняет вторую сцену первой и делает одну смену, **16 с**. Расхождение подтверждено на извлечённых функциях и SQLite в памяти. Это незакрытый стык §14 и нарушение §8.4. [Стр. 6997](/Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb/docs/superpowers/plans/2026-10-22-studio-s2-s3.md:6997), [стр. 7086](/Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb/docs/superpowers/plans/2026-10-22-studio-s2-s3.md:7086).

4. **Новый тест плоского payload проверяет ключи, но не сохранность значений из плана.** `sorted(json.loads(...))` пропускает подмену любого значения. Изолированно проверила мутацию `payload["scene_id"] = None`: утверждение остаётся зелёным. Для контракта §19.3 нужно сравнить весь словарь с ожидаемыми значениями, включая сид из O10. [Стр. 3012–3016](/Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb/docs/superpowers/plans/2026-10-22-studio-s2-s3.md:3012).

5. **Тест миниатюр по-прежнему проверяет форму файла вместо выбранного кадра.** URL и первые два JPEG-байта не подтверждают, что миниатюра «2 с» извлечена на второй секунде. Замена `str(second)` на `"0"` сохраняет все эти условия. Нужна проверка содержимого на ролике с различающимися кадрами. Задачи 12–13: [извлечение, стр. 7409](/Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb/docs/superpowers/plans/2026-10-22-studio-s2-s3.md:7409), [тест, стр. 7595](/Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb/docs/superpowers/plans/2026-10-22-studio-s2-s3.md:7595).

**Мелкое**

6. **Список файлов коммита задачи 14 пропускает сквозной тест.** В `git add` отсутствуют создаваемые `screens/RenderFlow.test.tsx` и `test/renderServer.ts`. При следовании этому списку заявленное покрытие останется вне коммита. [Стр. 8941](/Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb/docs/superpowers/plans/2026-10-22-studio-s2-s3.md:8941).

7. **В остаточных ограничениях сохранилось утверждение об абсолютном `asset.path`.** Оно противоречит редакции 4 и новому `files.py`; может направить исполнителя обратно к устранённой ошибке. [Стр. 9016](/Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb/docs/superpowers/plans/2026-10-22-studio-s2-s3.md:9016).

Миграция 003 и переименование `KEYFRAME_TEMPLATE` проходят `git apply --check`. Исправления запросов без `auto` согласованы с `extra=forbid`. Python-блоки синтаксически компилируются; полный pytest/сборку не запускала в окружении только для чтения. Файлы не меняла.