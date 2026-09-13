"""Задача 5, волна «перенос латента»: цепочка сцен в `assemble.py` едет на латентном хвосте
предыдущей сцены, а не на PNG-кейфрейме (`docs/FEASIBILITY-latent-handoff.md` §1.4, §3, §4.1,
схема A).

Три факта, вокруг которых всё:

* **сцепленная сцена** (`idx > 0` И не `fresh_start`) получает `--latent <хвост предыдущей>`
  ВМЕСТО `--image`, без `SCENE_I2V_INSTRUCTION` (фраза обещает картинку, которой в запросе нет),
  и запрашивает на `OVERLAP_PIXEL_FRAMES = 22` кадра больше, чем отдаёт в сборку;
* **головные кадры режутся по полю сцены** `head_drop_frames` (22 / 5 / 0), а не по «idx > 0»;
* **отказ `--save-latent-tail` не деградирует цепочку молча**: один-два пропущенных хвоста
  чинятся кейфрейм-фолбэком, три подряд -- честный `AssembleError`.

Фикстуры и фейки переиспользуются из `tests/test_assemble.py` (тот же `run`-шов, тот же
`submit`-фейк), а не переизобретаются -- расхождение между двумя наборами не должно проходить
незамеченным, то же правило, по которому `test_web_projects.py` тянет фейки из `test_worker.py`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from h3_48gb import assemble
from h3_48gb import project as project_module
from h3_48gb import web
from test_assemble import _FakeRun, _RecordingSubmit, _make_project, _make_scene

#: Длительность, лежащая на сетке СЦЕПЛЕННОЙ сцены (`17k`): 170 кадров = 17*10. Её запрос --
#: 170 + 22 = 192 = 17*11 + 5 -- ровно на H3-сетке, `align_num_frames` не сдвинет ничего.
_CHAINED_DURATION = 170 / 24

#: Длительность на сетке НЕсцепленной сцены (`17n + 5`): 175 кадров = 17*10 + 5.
_PLAIN_DURATION = 175 / 24


def _tail_for(clip_path) -> Path:
    return Path(str(clip_path)[:-4] + "-latent-tail.safetensors")


def _write_clip_with_tail(path: Path) -> Path:
    path.write_bytes(b"fake mp4")
    tail = _tail_for(path)
    tail.write_bytes(b"fake safetensors")
    return tail


def _arg(args, flag):
    return args[args.index(flag) + 1]


# == Аргументы задачи сцепленной сцены ============================================================


def test_chained_scene_is_submitted_with_the_previous_scenes_latent_tail(tmp_path, monkeypatch):
    """Схема A целиком, на одном submit: `--latent` вместо `--image`, промпт без
    `SCENE_I2V_INSTRUCTION`, запрос на 22 кадра длиннее доставляемого, и `_extract_keyframe` не
    вызывается вовсе (замонкипатчен на взрыв -- иначе тест мог бы пройти просто потому, что
    кейфрейм не нашёлся).
    """
    def _boom(*a, **k):
        raise AssertionError("_extract_keyframe не должен вызываться, когда хвост на месте")
    monkeypatch.setattr(assemble, "_extract_keyframe", _boom)
    clip0 = tmp_path / "scene0.mp4"
    tail = _write_clip_with_tail(clip0)
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="done", clip_path=str(clip0), duration=_PLAIN_DURATION),
        _make_scene(1, status="pending", prompt="вторая сцена", duration=_CHAINED_DURATION),
        _make_scene(2, status="pending", duration=_CHAINED_DURATION),
    ])
    submit = _RecordingSubmit()

    result = assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                                       run=_FakeRun())

    assert result["action"] == "submitted_scene" and result["idx"] == 1
    args = submit.calls[0]["args"]
    assert "--image" not in args, "сцепленная сцена стартует с латента, а не с картинки"
    assert _arg(args, "--latent") == str(tail)
    assert args[1] == "вторая сцена", (
        "SCENE_I2V_INSTRUCTION обещает <Picture 1>, которой в латентном запросе нет")
    assert assemble.SCENE_I2V_INSTRUCTION not in args[1]


def test_chained_scenes_duration_asks_for_the_overlap_on_top_of_what_it_delivers(tmp_path):
    """Арифметика §4.1: `project.json` несёт ДОСТАВЛЯЕМУЮ длительность, а запрос -- она же плюс
    `OVERLAP_PIXEL_FRAMES`, потому что голова рендера воспроизводит хвост, на котором её
    кондиционировали, и эти кадры срежет `_drop_head_frames`. Проверяются кадры, а не секунды:
    именно кадры -- то, на что смотрит `align_num_frames`.
    """
    clip0 = tmp_path / "scene0.mp4"
    _write_clip_with_tail(clip0)
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="done", clip_path=str(clip0), duration=_PLAIN_DURATION),
        _make_scene(1, status="pending", duration=_CHAINED_DURATION),
        _make_scene(2, status="pending", duration=_CHAINED_DURATION),
    ])
    submit = _RecordingSubmit()

    assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                              run=_FakeRun())

    requested = round(float(_arg(submit.calls[0]["args"], "--duration")) * 24)
    assert requested == 170 + assemble.OVERLAP_PIXEL_FRAMES == 192
    assert (requested - 5) % 17 == 0, (
        f"запрос {requested} кадров обязан лежать на H3-сетке 17j+5, иначе align_num_frames "
        f"растянет клип и сборка не сойдётся")


def test_chained_scene_refuses_a_duration_that_would_land_off_the_h3_grid(tmp_path):
    """P1-4: проверяется именно СЕТКА, а не потолок в 10 с. Длительность 7.0 с (168 кадров) не
    лежит на сетке сцепленной сцены -- запрос 190 кадров упал бы мимо `17j+5`, `align_num_frames`
    округлил бы его вверх, и клип пришёл бы длиннее обещанного. Честный отказ вместо тихой
    растяжки.
    """
    clip0 = tmp_path / "scene0.mp4"
    _write_clip_with_tail(clip0)
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="done", clip_path=str(clip0), duration=_PLAIN_DURATION),
        _make_scene(1, status="pending", duration=7.0),
        _make_scene(2, status="pending", duration=_CHAINED_DURATION),
    ])

    with pytest.raises(assemble.AssembleError, match="17"):
        assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out",
                                  submit=_RecordingSubmit(), run=_FakeRun())


def test_a_request_above_ten_seconds_is_legal_for_a_chained_scene(tmp_path):
    """Обратная сторона того же P1-4: сцепленная сцена на верхнем краю сетки (238 кадров, 9.917 с)
    запрашивает 260 кадров = 10.833 с. Это ЛЕГАЛЬНО -- `cli.py` верхней границы длительности не
    имеет, а `17j+5` соблюдено. Проверка «<= 10 с» вместо проверки сетки зарубила бы ночь на
    первой же длинной сцене.
    """
    clip0 = tmp_path / "scene0.mp4"
    _write_clip_with_tail(clip0)
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="done", clip_path=str(clip0), duration=_PLAIN_DURATION),
        _make_scene(1, status="pending", duration=238 / 24),
        _make_scene(2, status="pending", duration=_CHAINED_DURATION),
    ])
    submit = _RecordingSubmit()

    assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                              run=_FakeRun())

    requested = round(float(_arg(submit.calls[0]["args"], "--duration")) * 24)
    assert requested == 260 and requested / 24 > 10.0


def test_fresh_start_scene_gets_neither_a_latent_nor_a_picture_but_still_saves_its_tail(tmp_path,
                                                                                        monkeypatch):
    """§3: сцена с разрывом цепочки латент не получает по определению -- он несёт предыдущую
    сцену ПОЛНЕЕ кейфрейма, а рвём мы стык ровно затем, чтобы предыдущая сцена не протекла. Но
    свой хвост она сохранить обязана, иначе следующая за ней останется без входа.
    """
    def _boom(*a, **k):
        raise AssertionError("_extract_keyframe не должен вызываться для fresh_start-сцены")
    monkeypatch.setattr(assemble, "_extract_keyframe", _boom)
    clip0 = tmp_path / "scene0.mp4"
    _write_clip_with_tail(clip0)
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="done", clip_path=str(clip0), duration=_PLAIN_DURATION),
        _make_scene(1, status="pending", duration=_PLAIN_DURATION, fresh_start=True),
        _make_scene(2, status="pending", duration=_CHAINED_DURATION),
    ])
    submit = _RecordingSubmit()

    assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                              run=_FakeRun())

    args = submit.calls[0]["args"]
    assert "--latent" not in args and "--image" not in args
    assert _arg(args, "--save-latent-tail") == str(assemble.SCENE_LATENT_TAIL_FRAMES)
    assert round(float(_arg(args, "--duration")) * 24) == 175, (
        "разорванная сцена ничего не перекрывает -- её запрос равен её доставке")
    assert project_module.load_project(proj.path).scenes[1]["head_drop_frames"] == 0


def test_every_scene_but_the_last_saves_a_latent_tail(tmp_path):
    """`--save-latent-tail` ставится всем, кроме последней сцены: последней некому передавать
    хвост, а 331 кБ на 448x288 (и 1.18 МБ на 896x512) писать впустую незачем.
    """
    clip0 = tmp_path / "scene0.mp4"
    _write_clip_with_tail(clip0)
    clip1 = tmp_path / "scene1.mp4"
    _write_clip_with_tail(clip1)
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="done", clip_path=str(clip0), duration=_PLAIN_DURATION),
        _make_scene(1, status="done", clip_path=str(clip1), duration=_CHAINED_DURATION),
        _make_scene(2, status="pending", duration=_CHAINED_DURATION),
    ])
    submit = _RecordingSubmit()

    assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                              run=_FakeRun())

    assert "--save-latent-tail" not in submit.calls[0]["args"], (
        "последняя сцена никому не передаёт хвост")

    # Та же сцена 2, но теперь за ней есть третья -- флаг обязан появиться. Иначе «не последняя»
    # было бы неотличимо от «никогда».
    proj2 = _make_project(tmp_path / "second", "video", scenes=[
        _make_scene(0, status="done", clip_path=str(clip0), duration=_PLAIN_DURATION),
        _make_scene(1, status="done", clip_path=str(clip1), duration=_CHAINED_DURATION),
        _make_scene(2, status="pending", duration=_CHAINED_DURATION),
        _make_scene(3, status="pending", duration=_CHAINED_DURATION),
    ])
    submit2 = _RecordingSubmit()

    assemble.advance_project(proj2, tmp_path / "queue", tmp_path / "out", submit=submit2,
                              run=_FakeRun())

    assert _arg(submit2.calls[0]["args"], "--save-latent-tail") == \
        str(assemble.SCENE_LATENT_TAIL_FRAMES)


def test_scene_zero_saves_a_tail_and_takes_no_latent(tmp_path):
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="pending", duration=_PLAIN_DURATION),
        _make_scene(1, status="pending", duration=_CHAINED_DURATION),
    ])
    submit = _RecordingSubmit()

    assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                              run=_FakeRun())

    args = submit.calls[0]["args"]
    assert "--latent" not in args
    assert _arg(args, "--save-latent-tail") == str(assemble.SCENE_LATENT_TAIL_FRAMES)
    assert project_module.load_project(proj.path).scenes[0]["head_drop_frames"] == 0


def test_chained_scene_records_the_overlap_as_its_own_head_drop(tmp_path):
    """Поле пишется тем же `set_scene_status`, что и `keyframe_path` -- сборка потом режет
    голову ПО НЕМУ, а не по «idx > 0».
    """
    clip0 = tmp_path / "scene0.mp4"
    _write_clip_with_tail(clip0)
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="done", clip_path=str(clip0), duration=_PLAIN_DURATION),
        _make_scene(1, status="pending", duration=_CHAINED_DURATION),
        _make_scene(2, status="pending", duration=_CHAINED_DURATION),
    ])

    assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out",
                              submit=_RecordingSubmit(), run=_FakeRun())

    reloaded = project_module.load_project(proj.path)
    assert reloaded.scenes[1]["head_drop_frames"] == assemble.OVERLAP_PIXEL_FRAMES == 22
    assert reloaded.scenes[1]["keyframe_path"] is None


def test_the_latent_tail_path_is_derived_from_the_previous_scenes_clip_path(tmp_path):
    """P0-3(а): отдельного поля `latent_tail_path` НЕТ. `worker.py` пишет `clip_path` из того же
    `output_stem`, из которого `pipeline` пишет хвост, значит путь производный -- и умирает
    вместе с `clip_path`, который `invalidate_scene_chain` и так чистит.
    """
    clip = tmp_path / "jobs" / "h3-scene-0-abcd-896x512.mp4"
    clip.parent.mkdir(parents=True)

    assert assemble._latent_tail_path_for(str(clip)) == (
        clip.parent / "h3-scene-0-abcd-896x512-latent-tail.safetensors")


# == Фолбэк на кейфрейм, когда хвоста нет =========================================================


def test_a_missing_latent_tail_falls_back_to_a_keyframe_and_warns(tmp_path, capsys):
    """Честная деградация одной сцены: хвоста нет (сцена генерилась до этой волны, диск был полон,
    прогон падал) -- берём старый кейфрейм-путь, но говорим об этом вслух и помечаем сцену
    `head_drop_frames = 5`, а не 22: кейфрейм дублируется ОДНИМ кадром, а не двадцатью двумя.
    """
    clip0 = tmp_path / "scene0.mp4"
    clip0.write_bytes(b"fake mp4")  # хвоста рядом нет
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="done", clip_path=str(clip0), duration=_PLAIN_DURATION),
        _make_scene(1, status="pending", duration=_CHAINED_DURATION),
        _make_scene(2, status="pending", duration=_CHAINED_DURATION),
    ])
    submit = _RecordingSubmit()

    assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                              run=_FakeRun(ffprobe_durations=[6.0]))

    args = submit.calls[0]["args"]
    assert "--latent" not in args
    assert Path(_arg(args, "--image")).name == "keyframe-000.png"
    assert assemble.SCENE_I2V_INSTRUCTION in args[1], (
        "картинка вернулась -- значит вернулась и строка, которая её объявляет")
    reloaded = project_module.load_project(proj.path)
    assert reloaded.scenes[1]["head_drop_frames"] == assemble.KEYFRAME_FALLBACK_HEAD_DROP_FRAMES
    assert reloaded.scenes[1]["head_drop_frames"] == 5
    err = capsys.readouterr().err
    assert "WARNING" in err and "latent tail" in err and str(_tail_for(clip0)) in err


def test_a_keyframe_fallback_asks_for_exactly_what_it_delivers(tmp_path):
    """P0-4, арифметика фолбэка: доставляемая длительность сцены (`17k`) на сетке `17j+5` НЕ
    лежит, `align_num_frames` округлит запрос ровно на 5 кадров вверх -- и эти 5 (1 дубликат
    кейфрейма + 4) снимет `head_drop_frames = 5`. Значит прибавку 22 к фолбэк-сцене добавлять
    НЕЛЬЗЯ: она бы отдала на 17 кадров больше обещанного.
    """
    clip0 = tmp_path / "scene0.mp4"
    clip0.write_bytes(b"fake mp4")
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="done", clip_path=str(clip0), duration=_PLAIN_DURATION),
        _make_scene(1, status="pending", duration=_CHAINED_DURATION),
        _make_scene(2, status="pending", duration=_CHAINED_DURATION),
    ])
    submit = _RecordingSubmit()

    assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                              run=_FakeRun(ffprobe_durations=[6.0]))

    requested = round(float(_arg(submit.calls[0]["args"], "--duration")) * 24)
    assert requested == 170, "фолбэк просит ровно доставляемое; +5 добавит align_num_frames"
    delivered = 175 - assemble.KEYFRAME_FALLBACK_HEAD_DROP_FRAMES
    assert delivered == 170, "175 (после align) минус 5 срезанных = обещанные сеткой 170"


def test_two_consecutive_keyframe_fallbacks_are_still_allowed(tmp_path):
    """Граница снизу: два подряд -- ещё работа, а не отказ. Без этого теста порог «>= 3» и порог
    «>= 2» неразличимы.
    """
    clips = []
    for i in range(2):
        clip = tmp_path / f"scene{i}.mp4"
        clip.write_bytes(b"fake mp4")
        clips.append(clip)
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="done", clip_path=str(clips[0]), duration=_PLAIN_DURATION),
        _make_scene(1, status="done", clip_path=str(clips[1]), duration=_CHAINED_DURATION,
                    head_drop_frames=5),
        _make_scene(2, status="pending", duration=_CHAINED_DURATION),
        _make_scene(3, status="pending", duration=_CHAINED_DURATION),
    ])
    submit = _RecordingSubmit()

    result = assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                                       run=_FakeRun(ffprobe_durations=[6.0]))

    assert result["action"] == "submitted_scene" and result["idx"] == 2
    assert project_module.load_project(proj.path).scenes[2]["head_drop_frames"] == 5


def test_three_consecutive_keyframe_fallbacks_refuse_instead_of_degrading_the_chain(tmp_path):
    """P0-4: системный отказ `--save-latent-tail` (не тот флаг, полный диск, сломанная сборка)
    не должен молча превратить всю ночь обратно в кейфрейм-цепочку -- ту самую, ради ухода от
    которой волна и делалась. Третий подряд фолбэк -- честный `AssembleError`; сцена при этом
    откатывается в `pending` (общий `except` в `_submit_next_scene`), а не застревает
    `running` под плейсхолдерным job_id.
    """
    clips = []
    for i in range(3):
        clip = tmp_path / f"scene{i}.mp4"
        clip.write_bytes(b"fake mp4")
        clips.append(clip)
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="done", clip_path=str(clips[0]), duration=_PLAIN_DURATION),
        _make_scene(1, status="done", clip_path=str(clips[1]), duration=_CHAINED_DURATION,
                    head_drop_frames=5),
        _make_scene(2, status="done", clip_path=str(clips[2]), duration=_CHAINED_DURATION,
                    head_drop_frames=5),
        _make_scene(3, status="pending", duration=_CHAINED_DURATION),
        _make_scene(4, status="pending", duration=_CHAINED_DURATION),
    ])
    submit = _RecordingSubmit()

    with pytest.raises(assemble.AssembleError, match="latent tail"):
        assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                                  run=_FakeRun(ffprobe_durations=[6.0]))

    assert submit.calls == [], "отказ обязан произойти ДО постановки задачи в очередь"
    reloaded = project_module.load_project(proj.path)
    assert reloaded.scenes[3]["status"] == "pending" and reloaded.scenes[3]["job_id"] is None


def test_a_fresh_start_scene_resets_the_consecutive_fallback_counter(tmp_path):
    """Счётчик считает ПОДРЯД идущие фолбэки в одной цепочке. `fresh_start` (как и сцена 0) --
    не фолбэк, а нормальный t2v-старт: он рвёт серию, и следующая за ним сцена начинает счёт
    заново. Иначе проект с частыми разрывами состава упирался бы в отказ на ровном месте.
    """
    clips = []
    for i in range(3):
        clip = tmp_path / f"scene{i}.mp4"
        clip.write_bytes(b"fake mp4")
        clips.append(clip)
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="done", clip_path=str(clips[0]), duration=_PLAIN_DURATION),
        _make_scene(1, status="done", clip_path=str(clips[1]), duration=_CHAINED_DURATION,
                    head_drop_frames=5),
        _make_scene(2, status="done", clip_path=str(clips[2]), duration=_PLAIN_DURATION,
                    fresh_start=True, head_drop_frames=0),
        _make_scene(3, status="pending", duration=_CHAINED_DURATION),
        _make_scene(4, status="pending", duration=_CHAINED_DURATION),
    ])
    submit = _RecordingSubmit()

    result = assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                                       run=_FakeRun(ffprobe_durations=[6.0]))

    assert result["action"] == "submitted_scene" and result["idx"] == 3
    reloaded = project_module.load_project(proj.path)
    assert reloaded.scenes[3]["head_drop_frames"] == 5, (
        "сцена 3 -- сама фолбэк (хвоста у сцены 2 нет), просто первый в новой серии")


# == Подрезка головы по полю сцены ================================================================


def test_drop_head_frames_trims_exactly_what_the_scene_asks_for(tmp_path):
    fake = _FakeRun()

    assemble._drop_head_frames(tmp_path / "in.mp4", tmp_path / "out.mp4", 22, run=fake)

    assert any("trim=start_frame=22" in " ".join(c) for c in fake.calls), fake.calls


def test_build_video_clip_paths_reads_the_head_drop_off_each_scene(tmp_path):
    """Разные сцены -- разные подрезки в одном проекте: латентная 22, фолбэк 5, разорванная 0.
    Именно тот случай, который «idx > 0 -> один кадр» не различал вовсе.
    """
    scenes = [
        _make_scene(0, clip_path=str(tmp_path / "a.mp4"), head_drop_frames=0),
        _make_scene(1, clip_path=str(tmp_path / "b.mp4"), head_drop_frames=22),
        _make_scene(2, clip_path=str(tmp_path / "c.mp4"), head_drop_frames=5),
        _make_scene(3, clip_path=str(tmp_path / "d.mp4"), fresh_start=True, head_drop_frames=0),
    ]
    fake = _FakeRun()

    paths = assemble._build_video_clip_paths(
        scenes, [str(tmp_path / n) for n in ("a.mp4", "b.mp4", "c.mp4", "d.mp4")],
        tmp_path / "trim", run=fake)

    trims = [tok for c in fake.calls for tok in c if tok.startswith("trim=start_frame=")]
    assert trims == ["trim=start_frame=22,setpts=PTS-STARTPTS",
                     "trim=start_frame=5,setpts=PTS-STARTPTS"]
    assert paths[0] == str(tmp_path / "a.mp4"), "head_drop_frames=0 -- клип копируется как есть"
    assert paths[3] == str(tmp_path / "d.mp4"), "разорванная сцена ничего не дублирует"
    assert assemble._chaining_scene_indices(scenes) == [1, 2]


def test_a_scene_with_no_head_drop_frames_field_still_drops_exactly_one(tmp_path):
    """Ретро-совместимость: `project.json` ночи-3 и «Амазонок» этого поля не знает. Отсутствие ->
    1 кадр для сцепленной сцены и 0 для сцены 0 -- ровно то, что делал `_drop_first_frame` до
    этой волны, чтобы их пересборка воспроизводилась байт-в-байт.
    """
    scenes = [_make_scene(0, clip_path=str(tmp_path / "a.mp4")),
              _make_scene(1, clip_path=str(tmp_path / "b.mp4"))]
    for scene in scenes:
        assert "head_drop_frames" not in scene

    assert assemble._scene_head_drop_frames(scenes[0]) == 0
    assert assemble._scene_head_drop_frames(scenes[1]) == 1
    assert assemble._chaining_scene_indices(scenes) == [1]

    fake = _FakeRun()
    assemble._build_video_clip_paths(scenes, [str(tmp_path / "a.mp4"), str(tmp_path / "b.mp4")],
                                      tmp_path / "trim", run=fake)
    trims = [tok for c in fake.calls for tok in c if tok.startswith("trim=start_frame=")]
    assert trims == ["trim=start_frame=1,setpts=PTS-STARTPTS"]


def test_a_head_drop_of_zero_never_re_encodes_the_clip(tmp_path):
    """`n = 0` -- клип едет в concat как есть, без второго прохода libx264: перекодировать ради
    нулевой подрезки значит терять качество и минуты ffmpeg на каждой разорванной сцене
    (в ночной раскладке их семь).
    """
    scenes = [_make_scene(0, clip_path=str(tmp_path / "a.mp4"), head_drop_frames=0),
              _make_scene(1, clip_path=str(tmp_path / "b.mp4"), fresh_start=True,
                          head_drop_frames=0)]
    fake = _FakeRun()

    paths = assemble._build_video_clip_paths(
        scenes, [str(tmp_path / "a.mp4"), str(tmp_path / "b.mp4")], tmp_path / "trim", run=fake)

    assert paths == [str(tmp_path / "a.mp4"), str(tmp_path / "b.mp4")]
    assert fake.calls == [], "ни одного ffmpeg-вызова на сцены без подрезки"


# == Контракты между модулями =====================================================================


def test_assemble_and_web_agree_on_the_overlap_and_the_grid():
    """Константа продублирована в двух модулях намеренно (импортный контракт: ни `assemble`, ни
    `web` не тянут `h3_48gb.pipeline`, у которого `import mlx.core` на уровне модуля). Дубль без
    этой проверки -- источник расхождения, которое увидит только ночь.
    """
    assert assemble.OVERLAP_PIXEL_FRAMES == web._SCENE_LATENT_OVERLAP_FRAMES
    assert web._CHAINED_GRID_REMAINDER == (5 - assemble.OVERLAP_PIXEL_FRAMES) % 17 == 0
    # 17m+5 пиксельных кадров <-> 5m+2 латентных (спека §1.3, ряд {5,22,39,56} / {2,7,12,17})
    m, remainder = divmod(assemble.OVERLAP_PIXEL_FRAMES - 5, 17)
    assert remainder == 0
    assert assemble.SCENE_LATENT_TAIL_FRAMES == 5 * m + 2 == 7


def test_assemble_imports_neither_mlx_nor_the_pipeline():
    """Докстринг `assemble.py` («No `mlx` import, ever»): модуль живёт в воркер-процессе, который
    сутками простаивает между 30-гигабайтными генерациями. Константы волны -- свои, локальные.
    """
    imports = [line.strip() for line in
               Path(assemble.__file__).read_text(encoding="utf-8").splitlines()
               if line.strip().startswith(("import ", "from "))]
    assert not [ln for ln in imports if "mlx" in ln], imports
    assert not [ln for ln in imports if "pipeline" in ln], imports


def test_drop_head_frames_trims_audio_in_lockstep_with_video(tmp_path):
    """Night-4 assembly failure (2026-08-27, live): `_drop_head_frames` trimmed only the video
    stream and mapped the audio through untouched, so every trimmed clip carried 22/24 s more
    audio than picture. The concat demuxer pads each segment to its longest stream, so the
    assembled video grew a freeze-frame at every one of the 33 seams -- +29.9 s, straight past
    `DURATION_TOLERANCE_SECONDS` into AssembleError, after a full night of GPU. The audio of the
    overlap belongs to the previous scene's picture exactly like the frames do: both go.
    """
    import subprocess
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=64x64:rate=24:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)],
        check=True)
    out = tmp_path / "trimmed.mp4"
    assemble._drop_head_frames(src, out, 22, run=subprocess.run)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
         "-of", "csv=p=0", str(out)], capture_output=True, text=True, check=True)
    durations = {}
    for line in probe.stdout.strip().splitlines():
        kind, dur = line.split(",")[:2]
        durations[kind] = float(dur)
    assert "audio" in durations, "the audio stream must survive the trim, not be stripped"
    assert abs(durations["audio"] - durations["video"]) < 0.1, (
        f"audio ({durations['audio']:.3f}s) and video ({durations['video']:.3f}s) must be "
        "trimmed in lockstep -- a longer audio stream makes the concat demuxer pad every seam "
        "with duplicated frames")
