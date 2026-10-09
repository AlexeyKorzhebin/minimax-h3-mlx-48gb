# Задание: звук шагов задаётся походкой — крадётся / на носках / топает (09.10.2026)

Отвечай по-русски. Владелец одобрил сцену и разрешил GPU alex-neuro. Скрипт коммить в h3-bench,
ветка `probe/modes-20261008`; push с alex-neuro не работает, поэтому только коммит. Панель и
диспетчер НЕ правишь.

## Вопрос

Меняется ли громкость и характер шагов в звуке H3, если менять только манеру походки (обувь та
же)? Владелец будет слушать сам; твоя задача — отрендерить и дать грубые цифры по сегментам.

## Прогоны

- `VARIANT=fl2va`, task `t2va`, без условий, 896×512, **15 с**, 50 шагов, сиды 21 и 22.
- Сервер fl2va, скорее всего, уже поднят предыдущей пробой (`probe-draft-final-20261009`). Если он
  поднят под её `flock`, а она закончилась, сначала проверь, кто держит lock и сервер. Если сервера
  нет, подними `VARIANT=fl2va ~/Projects/h3-bench/serve.sh` под `flock` на
  `/home/alex/Projects/qwen-image21-lab/generation.lock`, как описано в
  `docs/h3-modes/BRIEF-probes.md`.
- Проверь заранее: очередь панели на паузе (`/api/state` → `paused: true`, running пуст). Иначе
  остановись и напиши. Паузу не снимай.
- В конце: fl2va убит, lock свободен, nvidia-smi пуст. ref2va не поднимай.

## Промпт (дословно, три поля fl2va)

integrated_multimodal_description:
```
Live-action, natural colours, soft even daylight from a window, a completely static camera on a tripod.
[Shot 1] Wide side view at knee height of a long, empty corridor in an old wooden country house: bare, worn pine floorboards run from left to right across the whole frame, a pale plastered wall behind, a closed white door at the far left edge. A young woman of about twenty-five with dark wavy hair tied back, in a grey knitted cardigan, dark loose trousers and soft grey felt slippers, crosses the corridor from right to left; her whole body, her feet on the boards and her face in profile stay visible the entire time.
From 0 to 5 seconds she sneaks: bent low, knees soft, she places each foot slowly from heel to toe, testing the board before putting weight on it, arms held out a little for balance; she is tense and holds her breath, her eyes flick again and again to the closed door ahead, her lips pressed together.
From 5 to 10 seconds she straightens up and walks quietly on tiptoe, heels high off the floor, quick light little steps, arms lifted slightly from her sides; she is playful now, biting back a smile as if in a game of hide-and-seek, glancing back over her shoulder once.
From 10 to 15 seconds she suddenly gives up the game: her face turns irritated, her jaw sets, her hands clench into fists, and she stamps the rest of the way flat-footed, heavy and deliberately loud, each foot slammed down on the boards, her shoulders jolting with every step, until she reaches the door.
Nothing else is in the frame: no other people, no animals, no text.
```

overall_soundscape:
```
Only the footsteps on the old wooden floor and the quiet of the house. 0-5 s: almost silent footsteps, a soft slow press of felt on wood, one faint creak of a board under her weight, her held breath. 5-10 s: very light quick taps of the slipper toes on the boards, barely audible, a tiny suppressed giggle of breath. 10-15 s: loud heavy thuds of each foot slammed onto the floorboards, the boards booming and creaking, a small rattle of the door in its frame on the last step. Between steps: the hush of an empty house, a distant clock. No speech, no music.
```

non_diegetic_music:
```
None.
```

Если сервер отклонит длительность 15 с — запиши тело ошибки и сделай 12 с, сдвинув таймкоды на 0–4/4–8/8–12.

## Оценка

1. По каждому сегменту (0–5, 5–10, 10–15 с): RMS в дБ, пиковая громкость, число различимых
   ударов (онсеты), доля энергии ниже 200 Гц. Ожидание: сегмент 3 громче сегментов 1–2 на
   заметную величину.
2. Кадры 2.5, 7.5, 12.5 с (ffmpeg) посмотри Read'ом: правда ли крадётся / на носках / топает, видны
   ли ноги, какая игра лица.
3. Время рендера.

## Правила

- Логи и выходы: `~/Projects/h3-bench/logs/probe-gait-20261009.log`,
  `~/Projects/h3-bench/outputs/probe-gait-20261009/`. Не /tmp.
- **Без run_in_background и фоновых ожиданий.** Генерацию запускай на сервере через nohup, жди
  foreground-циклом, timeout ≤ 10 мин на вызов Bash. «Идёт» — по tqdm в логе сервера.
- Ролики на Мак: `/Users/aleksey.korzhebin/Research/TestVideo/h3-modes/gait/gait-s21.mp4`,
  `gait-s22.mp4`, запросы `.request.json` рядом, кадры в `fr/`.

## Отчёт

`docs/h3-modes/GAIT-SOUND.md` в репо панели на Маке: промпт, таблица сегментов по обоим сидам,
что видно глазами, вывод одной фразой. В репо панели не коммить. Мне верни 8–12 строк.
