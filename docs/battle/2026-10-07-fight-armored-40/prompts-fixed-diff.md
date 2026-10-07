# Бой fight-armored-40-fixed: правка нумерации картинок в промптах

Источник: `/Volumes/home/Projects/h3-bench/fight-armored-40-jobs.json` (5 сцен, в каждой conditions = кейфрейм + 1 reference, лицо амазонки).
Генератор: `gen_fixed_scenes.py` (каждая замена проверяется по числу вхождений). Проверка: `offline_check_fixed.py`, лог в `offline-check.log`.

## Почему

Проба sglang 07.10.2026 (h3-bench `a98a7aa`, `docs/h3-shell-howto.md`, `docs/PIPELINE.md`): кейфрейм (`role: keyframe`, `frame_index: 0`) номера не получает,
`<Picture N>` нумерует только `role: reference` с 1 в порядке conditions. В первом бою промпт называл кейфрейм `<Picture 1>`, а лицо `<Picture 2>`.
На сервере `<Picture 1>` был лицом, `<Picture 2>` не существовал. В итоге текст про кейфрейм («fully_preserved», «Begin exactly from», «арена и костюм в <Picture 1>») приходился на кадр с лицом,
а строка про лицо («partially_preserved, только лицо и волосы») ни на какую картинку не указывала.

## Что поменялось (одинаково во всех 5 сценах)

| где | было | стало |
|---|---|---|
| строка-инструкция I2VA | `<Picture 1> (from [Shot 1]) is fully referenced.` | `the provided first frame (from [Shot 1]) is fully referenced.` |
| subject_definitions, кейфрейм | `<Picture 1> is the first frame of [Shot 1]: …` | `The provided first frame of [Shot 1] shows: …` |
| subject_definitions, лицо | `<Picture 2> is a cropped adult woman's face …` | `<Picture 1> is a cropped adult woman's face …` |
| `<Subject 1>` (арена) | `… arena in <Picture 1>, …` | `… arena in the provided first frame, …` |
| `<Subject 2>` (амазонка) | `face and dark hair of <Picture 2> and … costume in <Picture 1>:` | `face and dark hair of <Picture 1> and … costume in the provided first frame:` |
| `<Subject 3>` (гладиатор) | `… mid-thirties in <Picture 1>, …` | `… mid-thirties in the provided first frame, …` |
| summary | `Begin exactly from <Picture 1> and …` | `Begin exactly from the provided first frame and …` |
| retention_analysis, кейфрейм | `<Picture 1> ([Shot 1] first frame): fully_preserved - …` | `The provided first frame of [Shot 1]: fully_preserved - …` |
| retention_analysis, лицо | `<Picture 2>: partially_preserved - …` | `<Picture 1>: partially_preserved - …` |

Без изменений: шесть секций и их порядок, `<Subject 1..3>`, `[keyframe completion]`, весь `detailed_description` (там нет `<Picture N>`, кейфрейм назван словами: «the opening image», «the supplied final frame»), `overall_soundscape`, `non_diegetic_music`.
Кейфрейм везде называется одинаково: «the provided first frame».

## Как это проходит через панель

Панели нужен хотя бы один @тег в каждой сцене, иначе approve отвечает `ref2va_needs_reference` (проверено мутацией M2). `build_ref2va` (`h3_48gb/library.py:228`) превращает @тег в `<Subject N>` по порядку первого упоминания,
а картинки его карточки добавляет reference-условиями как `<Picture k>`. Свой блок `subject_definitions` панель не вставляет, если он уже есть в промпте (`_OWN_DEFINITIONS_RE`, `library.py:225`).
Как и в первом бою, первое вхождение `<Subject 1>` (строка определения арены) в теле PUT заменено на `@amazon`. Это единственный тег, он получает номер 1 и превращается обратно в `<Subject 1>`, так что текст совпадает побайтно.
Карточка `@amazon` v1 содержит одну картинку, лицо, и она становится единственным reference-условием, то есть `<Picture 1>` на сервере. `@arena` в тексте не упоминается, она даёт только `start_image` сцены 0, то есть кейфрейм без номера.
Порядок conditions в запросе: `[keyframe, reference(лицо)]` (`engines/sglang.py:160`).

Тег `@amazon` стоит на месте `<Subject 1>` (арены), а не `<Subject 2>` (амазонки). Для sglang это безразлично: там тот же текст. Это только носитель тега, панели он нужен, чтобы подключить картинку лица.

## Дифф по сценам (задуманный промпт, то есть то, что уйдёт в sglang; `-` было в jobs.json, `+` стало)

### Сцена 0 (fight-armored-40-01, кейфрейм: opening.png (@arena))
```diff
--- jobs.json
+++ fixed
@@ -1 +1 @@
-For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.
+For the target video, at 0.00 seconds into the target video, the provided first frame (from [Shot 1]) is fully referenced.
@@ -4,5 +4,5 @@
-<Picture 1> is the first frame of [Shot 1]: two fully clothed adult fighters facing each other on sand, the Amazon at left and the helmeted gladiator at right, with raised swords; pale stone walls, a central black iron gate and sparse seated spectators behind them.
-<Picture 2> is a cropped adult woman's face reference only, not a costume or body reference.
-<Subject 1> is the sandy ancient arena in <Picture 1>, with pale weathered stone walls, central iron gate, low stone tiers and spectators in tunics, all kept as shown.
-<Subject 2> is an adult Amazon warrior in her mid-twenties with the face and dark hair of <Picture 2> and the dark braid and complete opaque battle costume in <Picture 1>: pale tunic, bronze cuirass, layered leather battle skirt over loose opaque trousers, bracers and leather boots. Her chest, torso, pelvis and buttocks remain covered. She holds one bronze training sword. Her boots make muffled sand scuffs, no hard-floor heel clicks.
-<Subject 3> is the adult gladiator in his mid-thirties in <Picture 1>, retaining his beard, open-faced bronze helmet, pale opaque tunic, bronze cuirass, layered leather skirt over loose opaque trousers, bracers, boots and single iron training sword. His torso and pelvis remain covered. His boots compress sand without hard-floor heel clicks.
+The provided first frame of [Shot 1] shows: two fully clothed adult fighters facing each other on sand, the Amazon at left and the helmeted gladiator at right, with raised swords; pale stone walls, a central black iron gate and sparse seated spectators behind them.
+<Picture 1> is a cropped adult woman's face reference only, not a costume or body reference.
+<Subject 1> is the sandy ancient arena in the provided first frame, with pale weathered stone walls, central iron gate, low stone tiers and spectators in tunics, all kept as shown.
+<Subject 2> is an adult Amazon warrior in her mid-twenties with the face and dark hair of <Picture 1> and the dark braid and complete opaque battle costume in the provided first frame: pale tunic, bronze cuirass, layered leather battle skirt over loose opaque trousers, bracers and leather boots. Her chest, torso, pelvis and buttocks remain covered. She holds one bronze training sword. Her boots make muffled sand scuffs, no hard-floor heel clicks.
+<Subject 3> is the adult gladiator in his mid-thirties in the provided first frame, retaining his beard, open-faced bronze helmet, pale opaque tunic, bronze cuirass, layered leather skirt over loose opaque trousers, bracers, boots and single iron training sword. His torso and pelvis remain covered. His boots compress sand without hard-floor heel clicks.
@@ -11 +11 @@
-[keyframe completion] One continuous eight-second live-action shot, part 1 of five in a new connected forty-second nonlethal duel. Begin exactly from <Picture 1> and continue the fight in the same arena. Fast normal-speed attacks, parries and counterattacks, with a respectful bloodless resolution in part five. No dialogue or music.
+[keyframe completion] One continuous eight-second live-action shot, part 1 of five in a new connected forty-second nonlethal duel. Begin exactly from the provided first frame and continue the fight in the same arena. Fast normal-speed attacks, parries and counterattacks, with a respectful bloodless resolution in part five. No dialogue or music.
@@ -14,2 +14,2 @@
-<Picture 1> ([Shot 1] first frame): fully_preserved - retain the exact opening geometry, poses, costumes, lighting and camera position before continuing naturally.
-<Picture 2>: partially_preserved - retain facial identity and dark hair only.
+The provided first frame of [Shot 1]: fully_preserved - retain the exact opening geometry, poses, costumes, lighting and camera position before continuing naturally.
+<Picture 1>: partially_preserved - retain facial identity and dark hair only.
```
В теле PUT отличие от строки выше одно: `<Subject 1> is the sandy ancient arena` записано как `@amazon is the sandy ancient arena`.

### Сцена 1 (fight-armored-40-02, кейфрейм: последний кадр сцены 0)
```diff
--- jobs.json
+++ fixed
@@ -1 +1 @@
-For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.
+For the target video, at 0.00 seconds into the target video, the provided first frame (from [Shot 1]) is fully referenced.
@@ -4,5 +4,5 @@
-<Picture 1> is the first frame of [Shot 1]: the exact ending pose, camera framing, blade positions and arena layout of the preceding new part, carried forward without a reset.
-<Picture 2> is a cropped adult woman's face reference only, not a costume or body reference.
-<Subject 1> is the sandy ancient arena in <Picture 1>, with pale weathered stone walls, central iron gate, low stone tiers and spectators in tunics, all kept as shown.
-<Subject 2> is an adult Amazon warrior in her mid-twenties with the face and dark hair of <Picture 2> and the dark braid and complete opaque battle costume in <Picture 1>: pale tunic, bronze cuirass, layered leather battle skirt over loose opaque trousers, bracers and leather boots. Her chest, torso, pelvis and buttocks remain covered. She holds one bronze training sword. Her boots make muffled sand scuffs, no hard-floor heel clicks.
-<Subject 3> is the adult gladiator in his mid-thirties in <Picture 1>, retaining his beard, open-faced bronze helmet, pale opaque tunic, bronze cuirass, layered leather skirt over loose opaque trousers, bracers, boots and single iron training sword. His torso and pelvis remain covered. His boots compress sand without hard-floor heel clicks.
+The provided first frame of [Shot 1] shows: the exact ending pose, camera framing, blade positions and arena layout of the preceding new part, carried forward without a reset.
+<Picture 1> is a cropped adult woman's face reference only, not a costume or body reference.
+<Subject 1> is the sandy ancient arena in the provided first frame, with pale weathered stone walls, central iron gate, low stone tiers and spectators in tunics, all kept as shown.
+<Subject 2> is an adult Amazon warrior in her mid-twenties with the face and dark hair of <Picture 1> and the dark braid and complete opaque battle costume in the provided first frame: pale tunic, bronze cuirass, layered leather battle skirt over loose opaque trousers, bracers and leather boots. Her chest, torso, pelvis and buttocks remain covered. She holds one bronze training sword. Her boots make muffled sand scuffs, no hard-floor heel clicks.
+<Subject 3> is the adult gladiator in his mid-thirties in the provided first frame, retaining his beard, open-faced bronze helmet, pale opaque tunic, bronze cuirass, layered leather skirt over loose opaque trousers, bracers, boots and single iron training sword. His torso and pelvis remain covered. His boots compress sand without hard-floor heel clicks.
@@ -11 +11 @@
-[keyframe completion] One continuous eight-second live-action shot, part 2 of five in a new connected forty-second nonlethal duel. Begin exactly from <Picture 1> and continue the fight in the same arena. Fast normal-speed attacks, parries and counterattacks, with a respectful bloodless resolution in part five. No dialogue or music.
+[keyframe completion] One continuous eight-second live-action shot, part 2 of five in a new connected forty-second nonlethal duel. Begin exactly from the provided first frame and continue the fight in the same arena. Fast normal-speed attacks, parries and counterattacks, with a respectful bloodless resolution in part five. No dialogue or music.
@@ -14,2 +14,2 @@
-<Picture 1> ([Shot 1] first frame): fully_preserved - retain the exact opening geometry, poses, costumes, lighting and camera position before continuing naturally.
-<Picture 2>: partially_preserved - retain facial identity and dark hair only.
+The provided first frame of [Shot 1]: fully_preserved - retain the exact opening geometry, poses, costumes, lighting and camera position before continuing naturally.
+<Picture 1>: partially_preserved - retain facial identity and dark hair only.
```
В теле PUT отличие от строки выше одно: `<Subject 1> is the sandy ancient arena` записано как `@amazon is the sandy ancient arena`.

### Сцена 2 (fight-armored-40-03, кейфрейм: последний кадр сцены 1)
```diff
--- jobs.json
+++ fixed
@@ -1 +1 @@
-For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.
+For the target video, at 0.00 seconds into the target video, the provided first frame (from [Shot 1]) is fully referenced.
@@ -4,5 +4,5 @@
-<Picture 1> is the first frame of [Shot 1]: the exact ending pose, camera framing, blade positions and arena layout of the preceding new part, carried forward without a reset.
-<Picture 2> is a cropped adult woman's face reference only, not a costume or body reference.
-<Subject 1> is the sandy ancient arena in <Picture 1>, with pale weathered stone walls, central iron gate, low stone tiers and spectators in tunics, all kept as shown.
-<Subject 2> is an adult Amazon warrior in her mid-twenties with the face and dark hair of <Picture 2> and the dark braid and complete opaque battle costume in <Picture 1>: pale tunic, bronze cuirass, layered leather battle skirt over loose opaque trousers, bracers and leather boots. Her chest, torso, pelvis and buttocks remain covered. She holds one bronze training sword. Her boots make muffled sand scuffs, no hard-floor heel clicks.
-<Subject 3> is the adult gladiator in his mid-thirties in <Picture 1>, retaining his beard, open-faced bronze helmet, pale opaque tunic, bronze cuirass, layered leather skirt over loose opaque trousers, bracers, boots and single iron training sword. His torso and pelvis remain covered. His boots compress sand without hard-floor heel clicks.
+The provided first frame of [Shot 1] shows: the exact ending pose, camera framing, blade positions and arena layout of the preceding new part, carried forward without a reset.
+<Picture 1> is a cropped adult woman's face reference only, not a costume or body reference.
+<Subject 1> is the sandy ancient arena in the provided first frame, with pale weathered stone walls, central iron gate, low stone tiers and spectators in tunics, all kept as shown.
+<Subject 2> is an adult Amazon warrior in her mid-twenties with the face and dark hair of <Picture 1> and the dark braid and complete opaque battle costume in the provided first frame: pale tunic, bronze cuirass, layered leather battle skirt over loose opaque trousers, bracers and leather boots. Her chest, torso, pelvis and buttocks remain covered. She holds one bronze training sword. Her boots make muffled sand scuffs, no hard-floor heel clicks.
+<Subject 3> is the adult gladiator in his mid-thirties in the provided first frame, retaining his beard, open-faced bronze helmet, pale opaque tunic, bronze cuirass, layered leather skirt over loose opaque trousers, bracers, boots and single iron training sword. His torso and pelvis remain covered. His boots compress sand without hard-floor heel clicks.
@@ -11 +11 @@
-[keyframe completion] One continuous eight-second live-action shot, part 3 of five in a new connected forty-second nonlethal duel. Begin exactly from <Picture 1> and continue the fight in the same arena. Fast normal-speed attacks, parries and counterattacks, with a respectful bloodless resolution in part five. No dialogue or music.
+[keyframe completion] One continuous eight-second live-action shot, part 3 of five in a new connected forty-second nonlethal duel. Begin exactly from the provided first frame and continue the fight in the same arena. Fast normal-speed attacks, parries and counterattacks, with a respectful bloodless resolution in part five. No dialogue or music.
@@ -14,2 +14,2 @@
-<Picture 1> ([Shot 1] first frame): fully_preserved - retain the exact opening geometry, poses, costumes, lighting and camera position before continuing naturally.
-<Picture 2>: partially_preserved - retain facial identity and dark hair only.
+The provided first frame of [Shot 1]: fully_preserved - retain the exact opening geometry, poses, costumes, lighting and camera position before continuing naturally.
+<Picture 1>: partially_preserved - retain facial identity and dark hair only.
```
В теле PUT отличие от строки выше одно: `<Subject 1> is the sandy ancient arena` записано как `@amazon is the sandy ancient arena`.

### Сцена 3 (fight-armored-40-04, кейфрейм: последний кадр сцены 2)
```diff
--- jobs.json
+++ fixed
@@ -1 +1 @@
-For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.
+For the target video, at 0.00 seconds into the target video, the provided first frame (from [Shot 1]) is fully referenced.
@@ -4,5 +4,5 @@
-<Picture 1> is the first frame of [Shot 1]: the exact ending pose, camera framing, blade positions and arena layout of the preceding new part, carried forward without a reset.
-<Picture 2> is a cropped adult woman's face reference only, not a costume or body reference.
-<Subject 1> is the sandy ancient arena in <Picture 1>, with pale weathered stone walls, central iron gate, low stone tiers and spectators in tunics, all kept as shown.
-<Subject 2> is an adult Amazon warrior in her mid-twenties with the face and dark hair of <Picture 2> and the dark braid and complete opaque battle costume in <Picture 1>: pale tunic, bronze cuirass, layered leather battle skirt over loose opaque trousers, bracers and leather boots. Her chest, torso, pelvis and buttocks remain covered. She holds one bronze training sword. Her boots make muffled sand scuffs, no hard-floor heel clicks.
-<Subject 3> is the adult gladiator in his mid-thirties in <Picture 1>, retaining his beard, open-faced bronze helmet, pale opaque tunic, bronze cuirass, layered leather skirt over loose opaque trousers, bracers, boots and single iron training sword. His torso and pelvis remain covered. His boots compress sand without hard-floor heel clicks.
+The provided first frame of [Shot 1] shows: the exact ending pose, camera framing, blade positions and arena layout of the preceding new part, carried forward without a reset.
+<Picture 1> is a cropped adult woman's face reference only, not a costume or body reference.
+<Subject 1> is the sandy ancient arena in the provided first frame, with pale weathered stone walls, central iron gate, low stone tiers and spectators in tunics, all kept as shown.
+<Subject 2> is an adult Amazon warrior in her mid-twenties with the face and dark hair of <Picture 1> and the dark braid and complete opaque battle costume in the provided first frame: pale tunic, bronze cuirass, layered leather battle skirt over loose opaque trousers, bracers and leather boots. Her chest, torso, pelvis and buttocks remain covered. She holds one bronze training sword. Her boots make muffled sand scuffs, no hard-floor heel clicks.
+<Subject 3> is the adult gladiator in his mid-thirties in the provided first frame, retaining his beard, open-faced bronze helmet, pale opaque tunic, bronze cuirass, layered leather skirt over loose opaque trousers, bracers, boots and single iron training sword. His torso and pelvis remain covered. His boots compress sand without hard-floor heel clicks.
@@ -11 +11 @@
-[keyframe completion] One continuous eight-second live-action shot, part 4 of five in a new connected forty-second nonlethal duel. Begin exactly from <Picture 1> and continue the fight in the same arena. Fast normal-speed attacks, parries and counterattacks, with a respectful bloodless resolution in part five. No dialogue or music.
+[keyframe completion] One continuous eight-second live-action shot, part 4 of five in a new connected forty-second nonlethal duel. Begin exactly from the provided first frame and continue the fight in the same arena. Fast normal-speed attacks, parries and counterattacks, with a respectful bloodless resolution in part five. No dialogue or music.
@@ -14,2 +14,2 @@
-<Picture 1> ([Shot 1] first frame): fully_preserved - retain the exact opening geometry, poses, costumes, lighting and camera position before continuing naturally.
-<Picture 2>: partially_preserved - retain facial identity and dark hair only.
+The provided first frame of [Shot 1]: fully_preserved - retain the exact opening geometry, poses, costumes, lighting and camera position before continuing naturally.
+<Picture 1>: partially_preserved - retain facial identity and dark hair only.
```
В теле PUT отличие от строки выше одно: `<Subject 1> is the sandy ancient arena` записано как `@amazon is the sandy ancient arena`.

### Сцена 4 (fight-armored-40-05, кейфрейм: последний кадр сцены 3)
```diff
--- jobs.json
+++ fixed
@@ -1 +1 @@
-For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.
+For the target video, at 0.00 seconds into the target video, the provided first frame (from [Shot 1]) is fully referenced.
@@ -4,5 +4,5 @@
-<Picture 1> is the first frame of [Shot 1]: the exact ending pose, camera framing, blade positions and arena layout of the preceding new part, carried forward without a reset.
-<Picture 2> is a cropped adult woman's face reference only, not a costume or body reference.
-<Subject 1> is the sandy ancient arena in <Picture 1>, with pale weathered stone walls, central iron gate, low stone tiers and spectators in tunics, all kept as shown.
-<Subject 2> is an adult Amazon warrior in her mid-twenties with the face and dark hair of <Picture 2> and the dark braid and complete opaque battle costume in <Picture 1>: pale tunic, bronze cuirass, layered leather battle skirt over loose opaque trousers, bracers and leather boots. Her chest, torso, pelvis and buttocks remain covered. She holds one bronze training sword. Her boots make muffled sand scuffs, no hard-floor heel clicks.
-<Subject 3> is the adult gladiator in his mid-thirties in <Picture 1>, retaining his beard, open-faced bronze helmet, pale opaque tunic, bronze cuirass, layered leather skirt over loose opaque trousers, bracers, boots and single iron training sword. His torso and pelvis remain covered. His boots compress sand without hard-floor heel clicks.
+The provided first frame of [Shot 1] shows: the exact ending pose, camera framing, blade positions and arena layout of the preceding new part, carried forward without a reset.
+<Picture 1> is a cropped adult woman's face reference only, not a costume or body reference.
+<Subject 1> is the sandy ancient arena in the provided first frame, with pale weathered stone walls, central iron gate, low stone tiers and spectators in tunics, all kept as shown.
+<Subject 2> is an adult Amazon warrior in her mid-twenties with the face and dark hair of <Picture 1> and the dark braid and complete opaque battle costume in the provided first frame: pale tunic, bronze cuirass, layered leather battle skirt over loose opaque trousers, bracers and leather boots. Her chest, torso, pelvis and buttocks remain covered. She holds one bronze training sword. Her boots make muffled sand scuffs, no hard-floor heel clicks.
+<Subject 3> is the adult gladiator in his mid-thirties in the provided first frame, retaining his beard, open-faced bronze helmet, pale opaque tunic, bronze cuirass, layered leather skirt over loose opaque trousers, bracers, boots and single iron training sword. His torso and pelvis remain covered. His boots compress sand without hard-floor heel clicks.
@@ -11 +11 @@
-[keyframe completion] One continuous eight-second live-action shot, part 5 of five in a new connected forty-second nonlethal duel. Begin exactly from <Picture 1> and continue the fight in the same arena. Fast normal-speed attacks, parries and counterattacks, with a respectful bloodless resolution in part five. No dialogue or music.
+[keyframe completion] One continuous eight-second live-action shot, part 5 of five in a new connected forty-second nonlethal duel. Begin exactly from the provided first frame and continue the fight in the same arena. Fast normal-speed attacks, parries and counterattacks, with a respectful bloodless resolution in part five. No dialogue or music.
@@ -14,2 +14,2 @@
-<Picture 1> ([Shot 1] first frame): fully_preserved - retain the exact opening geometry, poses, costumes, lighting and camera position before continuing naturally.
-<Picture 2>: partially_preserved - retain facial identity and dark hair only.
+The provided first frame of [Shot 1]: fully_preserved - retain the exact opening geometry, poses, costumes, lighting and camera position before continuing naturally.
+<Picture 1>: partially_preserved - retain facial identity and dark hair only.
```
В теле PUT отличие от строки выше одно: `<Subject 1> is the sandy ancient arena` записано как `@amazon is the sandy ancient arena`.
