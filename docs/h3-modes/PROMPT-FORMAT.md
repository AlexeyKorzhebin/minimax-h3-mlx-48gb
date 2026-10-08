# Формат промпта ref2va: три поля панели против шести секций гайда (09.10.2026)

Вопрос из `BRIEF-prompt-format.md`. Ответ: на этом сюжете и этом референсе заметной разницы нет ни по лицу, ни по сюжету. Единственное различие, которое повторяется, находится в звуке (§2). Роликов восемь, выводы слабые.

## 1. Как панель собирает промпт

- Сборка: `h3_48gb/library.py:272` `build_ref2va`. Тег `@tag` в тексте становится `<Subject N>`, строка определения `"<Subject N> is {description}, appearance from <Picture k>."` строится на `library.py:318`, блок `subject_definitions:` ставится впереди текста на `library.py:324` (если в промпте уже есть свой блок, `library.py:269`, второй не добавляется).
- Три поля `integrated_multimodal_description` / `overall_soundscape` / `non_diegetic_music` пишет LLM-чат панели; панель сама их не добавляет.
- P3 собран вызовом самой `build_ref2va` (скрипт `~/Projects/h3-bench/prompt_format_build.py`, карточка скопирована во временную библиотеку, описание «a young woman»; в боевой карточке `@slavicgirl` стоит «девушка»).
- P6: шесть секций гайда (`subject_definitions`, `summary` с `[reference generation]`, `retention_analysis` с `partially_preserved`, `detailed_description`, `overall_soundscape`, `non_diegetic_music`). `newscene.py` не использован как есть: он собирает сценарий с ключевым кадром (`[keyframe completion]`, `<Picture 1>` как кадр), а у нас только референс. Секции собраны вручную по его шаблону и гайду.
- Одинаково в обоих: текст сцены (396 слов, `integrated`/`detailed`), звук, `non_diegetic_music: N/A`, референс (`les.png`: женщина с рыжим котом и кружкой, свитер), 896x512, 8 с. Различается только обвязка: в P6 есть `summary` и `retention_analysis`, а `subject_definitions` длиннее (внешность прописана в самом определении).
- Полные JSON запросов: `/Users/aleksey.korzhebin/Research/TestVideo/h3-modes/prompt-format/P3-s1.request.json`, `/Users/aleksey.korzhebin/Research/TestVideo/h3-modes/prompt-format/P6-s1.request.json` (остальные рядом, различаются сидом и шагами).

Начало P3 (текст сцены сокращён здесь, в файле полный):
```
subject_definitions:
<Subject 1> is a young woman, appearance from <Picture 1>.

integrated_multimodal_description: Live-action, natural colors, soft diffused morning daylight, a smooth steady camera on a slow dolly-out.
[Shot 1] Medium shot at chest height, eye level. <Subject 1>, a young woman of about twenty-five with fair freckled skin, grey-brown eyes, a calm neutral expres […]

overall_soundscape: Bare footsteps land on dry pine needles and packed earth with a soft muffled crunch at an even walking rhythm, no click, no heel tap, no hard sole. Several birds sing and answer each other high in the canopy, and a faint steady breeze moves through the birch leaves and pine needles. There is no speech and no music.

non_diegetic_music: N/A

```
Начало P6:
```
subject_definitions:
<Subject 1> is a young woman of about twenty-five with fair freckled skin, grey-brown eyes and loose wavy shoulder-length dark-brown hair; her face and hair appearance come from <Picture 1>.

summary:
[reference generation] A generation task producing one continuous 8-second live-action shot in which <Subject 1> walks straight toward the camera along a forest trail in the early morning while the camera slowly moves backward, her face fully visible and front-on for most of the shot. <Subject 1> keeps the face and hair of the reference picture and wears a linen dress. There is no dialogue and no music.

retention_analysis:
<Subject 1> (appears in [Shot 1]): partially_preserved - the face, the skin, the eye colour and the wavy dark-brown hair are carried over from <Picture 1> and stay steady for the whole […]

overall_soundscape: Bare footsteps land on dry pine needles and packed earth with a soft muffled crunch at an even walking rhythm, no click, no heel tap, no hard sole. Several birds sing and answer each other high in the canopy, and a faint steady breeze moves through the birch leaves and pine needles. There is no speech and no music.

non_diegetic_music: N/A
```

## 2. Результаты

Лицо: insightface buffalo_l (CPU), venv `~/Projects/h3-bench/.venv-face`. Кадры на 1, 2, 4, 6, 7.9 с. Контроль: другой человек (`ref1.jpg`) против референса даёт косинус -0.07. Лицо найдено на всех 40 кадрах. Лицо на видео занимает 47-66 px против 345 px на референсе, поэтому абсолютные косинусы низкие (0.3-0.5); сравнивать можно только между собой.

| Прогон | Время | Лицо найдено | Косинус среднее / мин | Глазами | Звук |
|---|---|---|---|---|---|
| P3-s1 | 970 с | 5/5 (100%) | 0.473 / 0.350 | да, лицо анфас | -51.9 дБ, <200 Гц 64%, тональность 0.44 |
| P6-s1 | 980 с | 5/5 | 0.510 / 0.479 | да | -53.9 дБ, <200 Гц 4%, тональность 0.0 |
| P3-s2 | 970 с | 5/5 | 0.470 / 0.428 | да | -48.4 дБ, <200 Гц 92%, тональность 0.93 |
| P6-s2 | 980 с | 5/5 | 0.475 / 0.414 | да | -54.7 дБ, <200 Гц 47%, тональность 0.25 |
| P3d-s3 | 390 с | 5/5 | 0.359 / 0.330 | да | -50.7 дБ, <200 Гц 71%, тональность 0.71 |
| P6d-s3 | 395 с | 5/5 | 0.343 / 0.314 | да, платье короче, видны колени | -41.6 дБ, <200 Гц 3%, тональность 0.59 |
| P3d-s4 | 390 с | 5/5 | 0.300 / 0.221 | да | -53.8 дБ, <200 Гц 49%, тональность 0.47 |
| P6d-s4 | 395 с | 5/5 | 0.334 / 0.279 | да | -55.5 дБ, <200 Гц 13%, тональность 0.15 |

Колонка «Звук»: общий RMS, доля энергии ниже 200 Гц, доля тональных кадров по автокорреляции.

Среднее косинуса: 50 шагов P3 0.472, P6 0.493; 20 шагов P3 0.330, P6 0.339. Сид важнее формата: разброс между сидами внутри формата (0.30-0.36 при 20 шагах) больше разницы между форматами.

Глазами (по 5 кадров на ролик, листы `/Users/aleksey.korzhebin/Research/TestVideo/h3-modes/prompt-format/fr/sheet-*.jpg`): во всех восьми та же героиня, что на референсе (тёмные волнистые волосы до плеч, светлая кожа), льняное платье, лесная тропа, солнце слева. Идёт анфас по направлению к камере, лицо видно на всех кадрах; руки без артефактов на просмотренных кадрах. Отъезд камеры назад заметен слабо: размер героини почти не меняется, поза почти статичная. Кот и кружка из референса в кадр не попали. Различие на кадрах: на 20 шагах P6d-s3 платье короче, видны колени и голые ноги, у остальных платье длиннее; на одном ролике это не вывод.

Звук (грубо, на слух не проверял): во всех 8 роликах он почти тихий, RMS -42...-56 дБ, пик -21...-34 дБ. Для сравнения: сцена «Лес» 08.10 с музыкой в промпте имеет -17 дБ среднее и -6 дБ пик. То есть «музыки нет» убрал и музыку, и почти весь звук; шаги по хвое различимы вряд ли. В P3 на всех четырёх роликах энергия сосредоточена ниже 200 Гц (49-92%) и в части кадров тональная: похоже на низкий гул. В P6 таких роликов один из четырёх (P6d-s3 даёт тональный всплеск 1-3 кГц до -34 дБ на 3-й секунде, то есть музыка или писк, отдельно не слушал). Это повторяющееся различие в пользу P6, но 8 роликов и измерение по спектру, а не на слух.

## 3. Вывод

- Узнаваемость лица, следование сюжету, артефакты: P3 и P6 неразличимы. Разница средних косинусов 0.02 (50 шагов) и 0.01 (20 шагов) при разбросе между сидами 0.05-0.06. Уверенно: «на лице формат не сказывается».
- Звук: P6 чаще даёт шумоподобный фон, P3 чаще низкий тональный гул. Слабая сторона в пользу P6, не проверена на слух.
- Скорость: P6 дольше на 10 с на 50 шагах (980 против 970 с), на практике то же.
- Оговорки: один референс (кот и кружка в кадре), одна сцена, два сида на 50 шагов, лицо на видео 50-65 px. Не проверены близкие планы, где лицо крупнее и разница могла бы проявиться.
- Два общих наблюдения не про формат: «нет музыки» делает ролик почти беззвучным; при `retention_analysis`/референсе одетой героини платье по запросу всё равно сменилось.

## 4. Для спеки волны 2

Формат промпта по режиму сцены ради качества лица вводить не нужно: нет измеримой разницы, а шесть секций требуют от LLM-чата больше полей и проверок. Если вводить, то ради звука, и только после проверки на слух на нескольких сценах. Пока достаточно: оставить трёхполевой формат и `subject_definitions`, а подсказку «без музыки» формулировать так, чтобы не глушить весь звук.

Файлы: ролики и кадры `/Users/aleksey.korzhebin/Research/TestVideo/h3-modes/prompt-format/`; на alex-neuro `~/Projects/h3-bench/outputs/probe-prompt-format-20261009/`, лог `~/Projects/h3-bench/logs/probe-prompt-format-20261009.log`; коммит скриптов в h3-bench, ветка `probe/modes-20261008`.

## Прослушка и просмотр владельцем (09.10)

`P3-s1` против `P6-s1`:
- **Звук.** На слух разницы нет: птицы и шаги.
  - Шаги звучат тяжело, как в сапогах, хотя героиня босиком. Промпт описывал звук физически: «Bare footsteps … soft muffled crunch … no click, no heel tap, no hard sole». Значит, физического описания мало: модель всё равно даёт шаги в обуви.
- **Игра.** Лицо без эмоций, «как у зомби», руки висят «как парализованные».
  - Причина в промпте пробы. Чтобы лицо можно было измерить, в нём стояли «a calm neutral expression», «lips stay closed», «eyes look straight ahead», «arms swinging loosely». Модель выполнила их буквально.

**Уточнение владельца:** босые ноги не стоят отдельной заботы. Громкость шагов зависит от манеры ходьбы, а не от обуви. Слушать топот имеет смысл, когда в промпте «крадётся» или «идёт тихонько».

**Вывод для спеки:** формат промпта не влияет ни на звук, ни на лицо. Мёртвую игру даёт отсутствие в тексте эмоции и действия. В шаблон сцены у LLM чата и в редактор сцены нужно обязательно добавить поле «эмоция и игра». В нём описываются внутреннее состояние, его смена по ходу сцены, мимика, взгляд, руки и микродвижения.
