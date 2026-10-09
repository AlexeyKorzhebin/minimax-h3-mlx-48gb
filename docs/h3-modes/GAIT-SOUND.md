# Звук шагов задаётся походкой: проба 09.10.2026

Вопрос: меняется ли громкость и характер шагов в звуке H3, если менять только манеру походки (обувь та же).
Прогон: fl2va-сервер, task t2va, без условий, 896x512, 15 с, 50 шагов, сиды 21 и 22, один ролик на сид с тремя манерами (0-5 крадётся, 5-10 на носках, 10-15 топает). Рендер 1741 с и 1746 с (около 29 мин, 34,9 с на шаг), пик памяти 55984 МБ. HTTP 200, 15 с сервер принял.

Ролики: /Users/aleksey.korzhebin/Research/TestVideo/h3-modes/gait/gait-s21.mp4, gait-s22.mp4; кадры в fr/. Скрипты в h3-bench (ветка probe/modes-20261008): gait_build.py, gait_run.py, gait_eval.py.

## Промпт (полный запрос)

```
integrated_multimodal_description: Live-action, natural colours, soft even daylight from a window, a completely static camera on a tripod.
[Shot 1] Wide side view at knee height of a long, empty corridor in an old wooden country house: bare, worn pine floorboards run from left to right across the whole frame, a pale plastered wall behind, a closed white door at the far left edge. A young woman of about twenty-five with dark wavy hair tied back, in a grey knitted cardigan, dark loose trousers and soft grey felt slippers, crosses the corridor from right to left; her whole body, her feet on the boards and her face in profile stay visible the entire time.
From 0 to 5 seconds she sneaks: bent low, knees soft, she places each foot slowly from heel to toe, testing the board before putting weight on it, arms held out a little for balance; she is tense and holds her breath, her eyes flick again and again to the closed door ahead, her lips pressed together.
From 5 to 10 seconds she straightens up and walks quietly on tiptoe, heels high off the floor, quick light little steps, arms lifted slightly from her sides; she is playful now, biting back a smile as if in a game of hide-and-seek, glancing back over her shoulder once.
From 10 to 15 seconds she suddenly gives up the game: her face turns irritated, her jaw sets, her hands clench into fists, and she stamps the rest of the way flat-footed, heavy and deliberately loud, each foot slammed down on the boards, her shoulders jolting with every step, until she reaches the door.
Nothing else is in the frame: no other people, no animals, no text.

overall_soundscape: Only the footsteps on the old wooden floor and the quiet of the house. 0-5 s: almost silent footsteps, a soft slow press of felt on wood, one faint creak of a board under her weight, her held breath. 5-10 s: very light quick taps of the slipper toes on the boards, barely audible, a tiny suppressed giggle of breath. 10-15 s: loud heavy thuds of each foot slammed onto the floorboards, the boards booming and creaking, a small rattle of the door in its frame on the last step. Between steps: the hush of an empty house, a distant clock. No speech, no music.

non_diegetic_music: None.
```

## Звук по сегментам (моно 24 кГц; онсеты: пики огибающей 10 мс выше пола +12 дБ, интервал 150 мс; грубая оценка)

| сид | сегмент | RMS, дБ | пик, дБ | онсетов | энергия < 200 Гц |
|---|---|---|---|---|---|
| 21 | 0-5 с крадётся | -49.2 | -21.0 | 2 | 15% |
| 21 | 5-10 с на носках | -47.2 | -21.5 | 2 | 18% |
| 21 | 10-15 с топает | -18.9 | -0.1 | 8 | 14% |
| 22 | 0-5 с крадётся | -58.0 | -38.1 | 2 | 20% |
| 22 | 5-10 с на носках | -52.6 | -28.7 | 2 | 20% |
| 22 | 10-15 с топает | -31.9 | -8.3 | 6 | 21% |

Сегмент 3 громче сегментов 1-2: по RMS на 28-30 дБ (сид 21) и на 21-26 дБ (сид 22); по пику на 21 и 20-30 дБ. Сегменты 1 и 2 между собой различаются мало (на 2-5 дБ, носки чуть громче), хотя в промпте они противопоставлены. Доля энергии ниже 200 Гц от походки не зависит (14-21%).

## Что видно глазами (кадры 2.5 / 7.5 / 12.5 с)

- Сид 21: 2.5 с согнута, руки в стороны, осторожный шаг, тапки на полу, лицо напряжённое, профиль, обе ноги в кадре. 7.5 с всё ещё согнута, пятки на полу, на носках не идёт, лицо не играет. 12.5 с выпрямлена, идёт обычным шагом, слабая улыбка вместо раздражения, кулаки не сжаты; топота в кадре нет.
- Сид 22: 2.5 с согнута, руки в стороны, напряжённое лицо, у края кадра справа тело обрезано. 7.5 с идёт чуть выпрямившись, улыбается, шаг лёгкий, пятки не высоко. 12.5 с идёт ровно, лицо опущено, спокойное, ноги плоско; раздражения и тяжёлого топота не видно.
- Обувь и ноги видны во всех кадрах. Крадущаяся манера видна в обоих сидах; носки и топанье в картинке выражены слабо, а звук при этом резко громче в третьей трети.

## Вывод

Манера походки меняет громкость звука кардинально только для топанья (третий сегмент громче на 20-30 дБ при 6-8 ударах против 2), а крадущийся и «на носках» звучат почти одинаково тихо, причём видео не всегда показывает заявленную манеру.

## Прослушка владельцем (09.10) — вопрос закрыт

- Топот звучит правильно: походка управляет громкостью шагов.
- **Ограничение модели:** громкий шаг H3 озвучивает как шаг в тяжёлой обуви. Героиня в войлочных тапочках грохочет, как в сапогах. Обувь, указанная в промпте (тапочки, босиком), на звук не влияет, промптом это не исправить. Принимаем как есть.
- Громкость задаём манерой походки: крадётся, тихо идёт, топает. Отдельно обувь в звуке не настраиваем.
