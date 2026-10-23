"""Slice S2, the worker on the keyframe (spec §8.5 lifecycle, §8.4 GPU gate, И9): the real API,
worker and ComfyUI adapter against FakeComfyStudio and FakeDispatcher (`_studio_rig`)."""
from __future__ import annotations

import hashlib
import time

import pytest

from _studio_rig import READY, Rig

PORTRAIT = "a young woman with a loose dark braid in a pale linen dress"


@pytest.fixture(autouse=True)
def _no_hang():
    """A hung test dumps every thread's stack and kills the run after 60 s instead of waiting
    for an outer timeout (review of task 8, item 8)."""
    import faulthandler
    faulthandler.dump_traceback_later(60, exit=True)
    yield
    faulthandler.cancel_dump_traceback_later()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def _engine_started(rig, at: float) -> None:
    """What the dispatcher's /status says of our engine (restart evidence, §8.2)."""
    rig.dispatcher.own = {"comfy-studio": {"pid": 1, "owner": "studio-worker", "started_at": at,
                                           "ready": True}}


def _scene1(rig, *, frame_format="16:9", state_in="She stands at the edge of the lake at dawn."):
    p = rig.project(frame_format)
    cast = rig.client.post(f"/api/studio/projects/{p['id']}/cast", json={
        "expected_revision": 1, "tag": "anna", "description_ru": "девушка",
        "description_en": "a young woman", "portrait_phrase": PORTRAIT}).json()
    scenes = rig.client.put(f"/api/studio/projects/{p['id']}/scenes", json={
        "expected_revision": cast["project"]["revision"], "scenes": [{
            "mode": "from_frame", "action_text": f"{PORTRAIT} walks to the water.",
            "state_in_text": state_in, "requested_s": 8, "refs": ["anna"]}]}).json()
    return p, scenes["scenes"][0]


def _queued_keyframe(rig):
    p, s1 = _scene1(rig)
    plan = rig.post(f"/projects/{p['id']}/plan", {"kind": "image", "scene_id": s1["id"]},
                    key=False)
    task = rig.post(f"/projects/{p['id']}/image", {"plan_id": plan["plan_id"]}, expect=201)
    return p, s1, plan["items"][0], task["task_id"]


def test_the_keyframe_is_made_recorded_and_attached_to_scene_1(rig):
    p, s1, item, task_id = _queued_keyframe(rig)
    assert rig.drain() == 1
    task = rig.task(task_id)
    asset_id = task["result_asset_id"]
    assert {k: task[k] for k in ("status", "kind", "project_id", "template", "progress_step",
                                 "progress_total", "error", "attempts", "result_url")} == {
        "status": "succeeded", "kind": "image", "project_id": p["id"], "template": "krea2-t2i",
        "progress_step": 8, "progress_total": 8, "error": None, "attempts": 0,
        "result_url": f"/api/studio/assets/{asset_id}/file"}
    [job] = rig.comfy.prompts
    graph = job["prompt"]
    assert (graph["4"]["inputs"]["text"], graph["7"]["inputs"]["seed"],
            graph["6"]["inputs"]["width"], graph["6"]["inputs"]["height"],
            job["extra_data"]["studio_task_id"]) == (item["prompt"], item["seed"], 1344, 768,
                                                     task_id)
    path = rig.home / "projects" / p["id"] / "keyframes" / task_id / "f_00001_.png"
    asset = rig.conn.execute("SELECT * FROM asset WHERE id = ?", (asset_id,)).fetchone()
    engine_file = rig.comfy.output_dir / "studio" / task_id / "f_00001_.png"
    # revision 4: `asset.path` is relative to the studio home, as S1 stores uploads
    assert (asset["kind"], asset["path"], asset["sha256"], asset["origin_kind"],
            asset["origin_task_id"], asset["mime"]) == (
        "image", str(path.relative_to(rig.home)),
        hashlib.sha256(engine_file.read_bytes()).hexdigest(), "take_output", task_id,
        "image/png")
    # every step of the lifecycle is an event, in order (R5), and the one-off events sit between
    lifecycle = [(k, p.get("status")) for k, p in rig.events()
                 if k in ("task_queued", "task_status", "task_running", "task_succeeded")]
    assert lifecycle == [
        ("task_queued", None), ("task_status", "waiting_gpu"), ("task_status", "waiting_ready"),
        ("task_status", "submitting"), ("task_status", "running"), ("task_running", None),
        ("task_status", "downloading"), ("task_status", "verifying"),
        ("task_status", "succeeded"), ("task_succeeded", None)]
    steps = [p for k, p in rig.events("task_progress")]
    assert steps and all(s["total"] == 8 and 1 <= s["step"] <= 8 for s in steps)
    assert [s["step"] for s in steps] == sorted({s["step"] for s in steps})   # no repeats
    assert rig.events("task_succeeded") == [("task_succeeded",
                                             {"kind": "image", "asset_id": asset_id})]
    sample = rig.conn.execute("SELECT executor, template, machine, width, height, frames, steps "
                              "FROM estimate_sample").fetchall()
    assert [tuple(r) for r in sample] == [("comfy", "krea2-t2i", "alex-neuro", 1344, 768, 0, 8)]
    r = rig.client.get(task["result_url"])
    assert r.content == engine_file.read_bytes()
    patched = rig.client.patch(f"/api/studio/scenes/{s1['id']}", json={
        "expected_edit_version": s1["edit_version"],
        "source_frame": {"kind": "asset", "asset_id": asset_id}}).json()
    assert patched["scene"]["source_frame"] == {"kind": "asset", "scene_id": None,
                                                "asset_id": asset_id, "auto": False}
    snap = rig.client.get(f"/api/studio/projects/{p['id']}").json()
    assert [t["id"] for t in snap["tasks"]] == [task_id]


def test_engine_ref_is_written_before_the_first_status_poll(rig):
    """И9: a crash between the answer and the first poll must not lose the ref."""
    _, _, _, task_id = _queued_keyframe(rig)
    seen = []
    real = rig.executor.status

    def status(ref):
        row = rig.conn.execute("SELECT engine_ref, status, submit_attempt_at FROM task "
                               "WHERE id = ?", (task_id,)).fetchone()
        seen.append((row["engine_ref"], row["status"], row["submit_attempt_at"] is not None))
        return real(ref)
    rig.executor.status = status
    rig.drain()
    [job] = rig.comfy.prompts
    assert seen[0] == (job["prompt_id"], "running", True)


def test_a_running_keyframe_is_cancelled_on_the_engine(rig):
    rig.comfy.hold = True
    _, _, _, task_id = _queued_keyframe(rig)
    real = rig.executor.status

    def status(ref):
        answer = real(ref)
        if answer["state"] == "running":
            rig.client.post(f"/api/studio/tasks/{task_id}/cancel", json={"reason": "передумал"})
        return answer
    rig.executor.status = status
    rig.drain()
    task = rig.task(task_id)
    assert (task["status"], task["engine_stop"], task["cancel_requested"], task["result_asset_id"],
            rig.comfy.interrupts) == ("cancelled", "confirmed", True, None, 1)
    assert rig.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 0
    assert rig.events("task_cancel_requested") == [("task_cancel_requested",
                                                    {"reason": "передумал"})]


def test_a_result_after_the_cancel_is_never_attached(rig):
    """§8.5 logical cancel: the engine finished anyway — the picture is not recorded."""
    _, _, _, task_id = _queued_keyframe(rig)
    real = rig.executor.result

    def result(ref, dest):
        files = real(ref, dest)
        rig.client.post(f"/api/studio/tasks/{task_id}/cancel", json={"reason": "поздно"})
        return files
    rig.executor.result = result
    rig.drain()
    task = rig.task(task_id)
    assert (task["status"], task["engine_stop"], task["result_asset_id"]) == (
        "cancelled", "finished", None)
    assert rig.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 0


def test_a_queued_keyframe_is_cancelled_at_once_and_never_runs(rig):
    _, _, _, task_id = _queued_keyframe(rig)
    r = rig.client.post(f"/api/studio/tasks/{task_id}/cancel", json={"reason": ""})
    assert (r.status_code, r.json()["status"]) == (200, "cancelled")
    assert rig.drain() == 0
    assert rig.comfy.prompts == []
    r = rig.client.post(f"/api/studio/tasks/{task_id}/cancel", json={"reason": ""})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "task_finished")


def test_waiting_for_the_card_shows_why(tmp_path):
    rig = Rig(tmp_path, acquire=({"ok": True, "state": "wait_qwen", "engine": "comfy-studio",
                                  "reason": "Qwen держит карту"},
                                 {"ok": True, "state": "starting", "engine": "comfy-studio"},
                                 READY))
    try:
        _, _, _, task_id = _queued_keyframe(rig)
        rig.drain()
        assert rig.events("task_waiting") == [
            ("task_waiting", {"wait_reason": "ждём GPU: Qwen держит карту"}),
            ("task_waiting", {"wait_reason": "ждём GPU: поднимается comfy-studio"})]
        assert rig.task(task_id)["status"] == "succeeded"
        assert rig.task(task_id)["wait_reason"] is None
        r = rig.client.post("/api/studio/gpu/qwen/unload")
        assert (r.status_code, r.json()) == (200, {"ok": True, "was_running": True,
                                                   "exit_code": 0})
    finally:
        rig.close()


def test_an_engine_error_fails_the_task_with_its_text(tmp_path):
    rig = Rig(tmp_path, comfy_kwargs={"fail": "CUDA out of memory"})
    try:
        _, _, _, task_id = _queued_keyframe(rig)
        rig.drain()
        assert (rig.task(task_id)["status"], rig.task(task_id)["error"]) == (
            "failed", "ComfyUI: CUDA out of memory")
        assert rig.events("task_failed") == [("task_failed",
                                              {"error": "ComfyUI: CUDA out of memory"})]
    finally:
        rig.close()


def test_a_failed_engine_start_fails_the_task_with_the_log(tmp_path):
    rig = Rig(tmp_path, acquire=({"ok": True, "state": "failed", "engine": "comfy-studio",
                                  "reason": "движок не поднялся за 600 с, смотрите лог",
                                  "log": "/l.log"},))
    try:
        _, _, _, task_id = _queued_keyframe(rig)
        rig.drain()
        assert rig.task(task_id)["error"] == \
            "движок не поднялся: движок не поднялся за 600 с, смотрите лог (лог /l.log)"
        assert rig.comfy.prompts == []
    finally:
        rig.close()


def test_a_task_lost_in_a_restart_is_retried_once(rig):
    rig.comfy.hold = True
    _, _, _, task_id = _queued_keyframe(rig)
    real = rig.executor.status
    restarted = []

    def status(ref):
        answer = real(ref)
        if answer["state"] == "running" and not restarted:
            restarted.append(ref)
            rig.comfy.restart()             # kill -9: queue, history and the running prompt gone
            rig.comfy.hold = False
            _engine_started(rig, time.time() + 60)   # the dispatcher's proof of the restart
        return answer
    rig.executor.status = status
    rig.drain()
    task = rig.task(task_id)
    assert (task["status"], task["attempts"], len(rig.comfy.prompts)) == ("succeeded", 1, 2)
    assert rig.events("task_requeued") == [("task_requeued", {"reason": "lost"})]


def test_an_engine_that_dies_under_a_live_worker_is_submit_unknown_not_a_retry(rig):
    """Final review (coordinator ruling 23.10): the studio's ComfyUI dies mid-task (the old panel's
    «Освободить карту», an OOM) while the worker lives and holds the gate; the dispatcher keeps no
    record of it. The port is closed — ComfyUI cannot be asked, `lost` is never proven, so the
    task ends as `submit_unknown` with the ref kept and NO automatic retry. A retry happens only
    after a worker restart (test_a_task_lost_in_a_restart_is_retried_once). Making the dispatcher
    say «живой pid → lost» is slice S5."""
    rig.comfy.hold = True
    _, _, _, task_id = _queued_keyframe(rig)
    real = rig.executor.status
    killed = []

    def status(ref):
        answer = real(ref)
        if answer["state"] == "running" and not killed:
            killed.append(ref)
            rig.comfy.close()               # the process is gone: connection refused from now on
            rig.dispatcher.own = {}         # ...and the dispatcher keeps no record of it
        return answer
    rig.executor.status = status
    rig.drain()
    task = rig.task(task_id)
    kept = rig.conn.execute("SELECT engine_ref FROM task WHERE id = ?", (task_id,)).fetchone()
    assert (task["status"], task["attempts"], kept["engine_ref"] == killed[0],
            len(rig.comfy.prompts)) == ("submit_unknown", 0, True, 1)
    assert task["wait_reason"].startswith("ComfyUI не отвечает на проверку задачи: ComfyUI GET "
                                          f"/history/{killed[0]} недоступен: ")
    assert rig.events("task_requeued") == []
    assert [k for k, _ in rig.events("task_submit_unknown")] == ["task_submit_unknown"]


def test_the_picture_task_reads_its_values_by_the_card_and_drops_nulls(rig):
    """Revision 4 (spec §19.3, §14 seams 3–4): the payload is flat; the values are the card's
    inputs present in it and not null — not a fixed list. S2b's keys outside the card
    (profile, variant_id, batch_id) stay out, a `lora: null` never reaches the substituter."""
    import json

    from h3_48gb.studio.images import ImageKind
    p = rig.project()
    payload = {"template": "krea2-t2i", "template_sha256": "s", "prompt": "a cat",
               "width": 1344, "height": 768, "seed": 7, "steps": 8, "scene_id": None,
               "lora": None, "profile": None, "variant_id": "v_1", "batch_id": "b_1"}
    rig.conn.execute("INSERT INTO task (id, kind, executor, gpu_scope, payload_json, status, "
                     "project_id, created_at, updated_at) VALUES ('k_x', 'image', 'comfy', 'x', "
                     "?, 'queued', ?, 'n', 'n')", (json.dumps(payload), p["id"]))
    row = rig.conn.execute("SELECT * FROM task WHERE id = 'k_x'").fetchone()
    spec = ImageKind(rig.home, machine="alex-neuro").spec(rig.conn, row)
    assert (spec.template, spec.template_sha256, spec.values, spec.dest_dir) == (
        "krea2-t2i", "s", {"prompt": "a cat", "seed": 7, "steps": 8, "width": 1344,
                           "height": 768},
        rig.home / "projects" / p["id"] / "keyframes" / "k_x")


def test_a_task_lost_twice_fails(rig):
    """§8.5: lost -> one automatic retry; the second loss is a failure, not a loop."""
    rig.comfy.hold = True
    _, _, _, task_id = _queued_keyframe(rig)
    real = rig.executor.status

    def status(ref):
        answer = real(ref)
        if answer["state"] == "running":
            rig.comfy.restart()
            rig.comfy.hold = True
        return answer
    rig.executor.status = status
    rig.drain()
    task = rig.task(task_id)
    assert (task["status"], task["attempts"], task["error"], len(rig.comfy.prompts)) == (
        "failed", 1, "ComfyUI потерял задачу дважды", 2)


def test_a_lost_task_whose_files_exist_is_taken_from_the_files(rig):
    _, _, _, task_id = _queued_keyframe(rig)
    real = rig.executor.status

    def status(ref):
        answer = real(ref)
        if answer["state"] == "done":
            rig.comfy.restart()             # finished, then killed: history gone, files stay
            return real(ref)
        return answer
    rig.executor.status = status
    rig.drain()
    task = rig.task(task_id)
    assert (task["status"], task["attempts"], len(rig.comfy.prompts)) == ("succeeded", 0, 1)
    assert rig.conn.execute("SELECT engine_ref FROM task").fetchone()[0] == f"file:{task_id}"


def test_a_picture_of_the_wrong_size_fails_verification(rig):
    p, s1 = _scene1(rig)
    plan = rig.post(f"/projects/{p['id']}/plan", {"kind": "image", "prompt": "x", "width": 1344,
                                                  "height": 768})
    task_id = rig.post(f"/projects/{p['id']}/image", {"plan_id": plan["plan_id"]},
                       expect=201)["task_id"]
    real = rig.executor.result

    def result(ref, dest):
        files = real(ref, dest)
        from PIL import Image
        Image.new("RGB", (64, 64)).save(files[0])
        return files
    rig.executor.result = result
    rig.drain()
    assert (rig.task(task_id)["status"], rig.task(task_id)["error"]) == (
        "failed", "результат не прошёл проверку: кадр 64×64 вместо 1344×768")


def test_a_restarted_worker_follows_the_accepted_task_on_without_resubmitting(rig):
    """Review Focus 1: the container is restarted while ComfyUI renders. The new worker does not
    submit again (the engine has it, `engine_ref` is written — И9) and records the result."""
    from h3_48gb.studio.worker import Worker
    rig.comfy.hold = True
    _, _, _, task_id = _queued_keyframe(rig)
    real = rig.executor.status

    class Crash(Exception):
        pass

    def status(ref):
        answer = real(ref)
        if answer["state"] == "running":
            raise Crash                        # the process dies here
        return answer
    rig.executor.status = status
    with pytest.raises(Crash):
        rig.drain()
    assert rig.task(task_id)["status"] == "running"
    rig.executor.status = real
    rig.comfy.release()
    reborn = Worker(rig.db_path, executor=rig.executor, gate=rig.gate, kinds=rig.worker.kinds,
                    poll_s=0.01, probe_s=0.01, sleep=rig._sleep)
    try:
        assert reborn.run_once() is True
        assert reborn.run_once() is False
    finally:
        reborn.conn.close()
    assert (rig.task(task_id)["status"], len(rig.comfy.prompts)) == ("succeeded", 1)


def test_a_restart_puts_waiting_tasks_back_and_never_resubmits_an_unknown_one(rig):
    _, _, _, waiting = _queued_keyframe(rig)
    _, _, _, cut = _queued_keyframe(rig)
    rig.conn.execute("UPDATE task SET status = 'waiting_gpu', wait_reason = 'x' WHERE id = ?",
                     (waiting,))
    rig.conn.execute("UPDATE task SET status = 'submitting', submit_attempt_at = 1 WHERE id = ?",
                     (cut,))
    assert rig.worker.recover() == 0
    assert (rig.task(waiting)["status"], rig.task(waiting)["wait_reason"]) == ("queued", None)
    assert (rig.task(cut)["status"], rig.task(cut)["wait_reason"]) == (
        "submit_unknown", "Воркер перезапущен во время постановки: не знаю, принял ли ComfyUI "
                          "задачу")
    rig.worker._recovered = True
    rig.drain()
    assert (rig.task(waiting)["status"], rig.task(cut)["status"], len(rig.comfy.prompts)) == (
        "succeeded", "submit_unknown", 1)


def test_a_lost_task_without_proof_of_a_restart_is_not_retried(rig):
    """§8.5 / Codex-4: ComfyUI forgot the task, but the dispatcher shows the same process as
    before the submit — no proof the task died. `submit_unknown`, the ref kept, no resubmit."""
    _engine_started(rig, 1.0)
    rig.comfy.hold = True
    _, _, _, task_id = _queued_keyframe(rig)
    real = rig.executor.status

    def status(ref):
        answer = real(ref)
        if answer["state"] == "running":
            rig.comfy.restart()
        return answer
    rig.executor.status = status
    rig.drain()
    task = rig.task(task_id)
    [job] = rig.comfy.prompts
    assert (task["status"], task["attempts"], task["wait_reason"]) == (
        "submit_unknown", 0,
        "ComfyUI не знает задачу, а перезапуска движка не было — не повторяю вслепую")
    assert rig.conn.execute("SELECT engine_ref FROM task").fetchone()[0] == job["prompt_id"]


def test_a_network_outage_while_following_is_not_a_loss(rig):
    """The engine lives (same started_at) but the studio cannot reach it for a while: never a
    retry — the first run may still be rendering."""
    _engine_started(rig, 1.0)
    rig.comfy.hold = True
    _, _, _, task_id = _queued_keyframe(rig)
    from h3_48gb.studio.executors.base import ExecutorError
    real = rig.executor.status
    calls = []

    def status(ref):
        calls.append(ref)
        raise ExecutorError("ComfyUI GET /history/x недоступен: timed out")
    rig.executor.status = status
    rig.drain()
    task = rig.task(task_id)
    assert (task["status"], task["attempts"], len(rig.comfy.prompts), len(calls)) == (
        "submit_unknown", 0, 1, 30)
    assert task["wait_reason"] == ("ComfyUI не отвечает на проверку задачи: "
                                   "ComfyUI GET /history/x недоступен: timed out")
    rig.executor.status = real


def test_a_loss_that_cannot_be_checked_is_not_retried(rig):
    """find() could not ask the engine (and no files): «could not check» is not «not found»."""
    _engine_started(rig, 1.0)
    rig.comfy.hold = True
    _, _, _, task_id = _queued_keyframe(rig)
    from h3_48gb.studio.executors.base import ExecutorError
    real_status = rig.executor.status

    def status(ref):
        return {"state": "lost", "progress": None, "error": None}

    def find(spec):
        raise ExecutorError("очередь и история ComfyUI недоступны: timed out")
    rig.executor.status, rig.executor.find = status, find
    rig.drain()
    task = rig.task(task_id)
    assert (task["status"], task["attempts"], task["wait_reason"]) == (
        "submit_unknown", 0, "ComfyUI не знает задачу, а проверить не вышло: очередь и история "
                             "ComfyUI недоступны: timed out")
    rig.executor.status = real_status


def test_the_task_records_the_engine_version_and_the_template_it_ran(rig):
    """Revision 3 (Codex r2, finding 10): what ran is on the task — the ComfyUI version from
    /system_stats at submit and the template's sha256, side by side."""
    rig.comfy.comfyui_version = "0.42.0+stub.7"
    _, _, _, task_id = _queued_keyframe(rig)
    rig.drain()
    task = rig.task(task_id)
    assert (task["status"], task["engine_version"], task["template_sha256"]) == (
        # task 5 made the recorded version «ComfyUI · torch · GPU» (spec rev 3.2, И15)
        "succeeded", "ComfyUI 0.42.0+stub.7 · torch 2.9.1+cu128 · "
                     "cuda:0 NVIDIA CMP 170HX 64GB : cudaMallocAsync",
        "b16051ff44452751a5bf5f9773e1d46ad1e3a2dbd5b2358b0237e443b1f00fb2")


def _hold_and_press_cancel(rig, task_id):
    """The first status poll that sees the task running presses «cancel» through the API."""
    real = rig.executor.status
    pressed = []

    def status(ref):
        answer = real(ref)
        if answer["state"] == "running" and not pressed:
            pressed.append(ref)
            rig.client.post(f"/api/studio/tasks/{task_id}/cancel", json={"reason": "стоп"})
        return answer
    rig.executor.status = status
    return real


def test_a_cancelled_task_the_engine_no_longer_knows_is_cancelled_not_lost(rig):
    """Coordinator, review of task 5: a prompt taken off the engine's queue answers `lost`. In
    `cancelling` that is the engine confirming the cancel — not a loss to retry or to wait out."""
    rig.comfy.hold = True
    _, _, _, task_id = _queued_keyframe(rig)
    real = _hold_and_press_cancel(rig, task_id)
    pressed_status = rig.executor.status

    def status(ref):
        if rig.conn.execute("SELECT status FROM task WHERE id = ?", (task_id,)
                            ).fetchone()[0] == "cancelling":
            return {"state": "lost", "progress": None, "error": None}
        return pressed_status(ref)
    rig.executor.status = status
    rig.drain()
    task = rig.task(task_id)
    assert (task["status"], task["engine_stop"], task["attempts"], len(rig.comfy.prompts),
            rig.events("task_requeued")) == ("cancelled", "confirmed", 0, 1, [])
    rig.executor.status = real


class _Time:
    def __init__(self):
        import time as _t
        self.now = _t.time()
        self.slept = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept += seconds
        self.now += seconds


def _stuck_engine(rig, task_id):
    """The engine keeps answering `running` after the cancel: it never confirms."""
    real = _hold_and_press_cancel(rig, task_id)
    pressed_status = rig.executor.status

    def status(ref):
        answer = pressed_status(ref)
        if rig.conn.execute("SELECT status FROM task WHERE id = ?", (task_id,)
                            ).fetchone()[0] == "cancelling":
            return {"state": "running", "progress": None, "error": None}
        return answer
    rig.executor.status = status
    return real


def test_an_unconfirmed_cancel_ends_30_seconds_after_the_first_press(rig):
    rig.comfy.hold = True
    _, _, _, task_id = _queued_keyframe(rig)
    fake = _Time()
    rig.worker.clock, rig.worker.monotonic, rig.worker.sleep, rig.worker.poll_s = (
        fake.clock, fake.clock, fake.sleep, 1.0)
    rig.worker.cancel_wait_s = 30.0
    _stuck_engine(rig, task_id)
    rig.worker.run_once()
    task = rig.task(task_id)
    row = rig.conn.execute("SELECT cancel_requested_at FROM task WHERE id = ?",
                           (task_id,)).fetchone()
    assert (task["status"], task["engine_stop"], task["cancel_requested"]) == (
        "cancelled", "unconfirmed", True)
    # the 30 s are counted from the press (`cancel_requested_at`), the loop stops at the deadline
    assert fake.now >= row["cancel_requested_at"] + 30.0
    assert fake.now - row["cancel_requested_at"] < 32.0


def test_the_cancel_wait_is_counted_from_the_first_press_not_from_the_worker_taking_it(rig):
    """A press made 25 s ago leaves 5 s, not 30: a restart does not extend the wait."""
    rig.comfy.hold = True
    _, _, _, task_id = _queued_keyframe(rig)
    fake = _Time()
    rig.worker.clock, rig.worker.monotonic, rig.worker.sleep, rig.worker.poll_s = (
        fake.clock, fake.clock, fake.sleep, 1.0)
    rig.worker.cancel_wait_s = 30.0
    _stuck_engine(rig, task_id)
    real_stop = rig.worker._stop_engine

    def stop(task_id_, ref):
        rig.conn.execute("UPDATE task SET cancel_requested_at = ? WHERE id = ?",
                         (fake.now - 25.0, task_id_))
        slept_before = fake.slept
        real_stop(task_id_, ref)
        stop.slept = fake.slept - slept_before
    rig.worker._stop_engine = stop
    rig.worker.run_once()
    assert (rig.task(task_id)["engine_stop"], stop.slept) == ("unconfirmed", 5.0)


# ---- review of task 8, round 1 --------------------------------------------------------------

import threading  # noqa: E402

from h3_48gb.engines.comfy_api import ComfyError  # noqa: E402
from h3_48gb.studio.executors.base import ExecutorError  # noqa: E402
from h3_48gb.studio.worker import Worker  # noqa: E402


def _status_of(rig, task_id) -> str:
    return rig.conn.execute("SELECT status FROM task WHERE id = ?", (task_id,)).fetchone()[0]


def _crash_while_running(rig):
    """Queue a keyframe and kill the worker (an exception out of `status`) while ComfyUI holds
    it: the row is `running` with its `engine_ref`, as after a container restart."""
    rig.comfy.hold = True
    _, _, _, task_id = _queued_keyframe(rig)
    real = rig.executor.status

    class Crash(Exception):
        pass

    def status(ref):
        answer = real(ref)
        if answer["state"] == "running":
            raise Crash
        return answer
    rig.executor.status = status
    with pytest.raises(Crash):
        rig.drain()
    rig.executor.status = real
    rig.comfy.release()
    return task_id


def _reborn(rig, **kwargs) -> Worker:
    return Worker(rig.db_path, executor=rig.executor, gate=rig.gate, kinds=rig.worker.kinds,
                  poll_s=0.01, probe_s=0.01, cancel_wait_s=2.0, sleep=rig._sleep, **kwargs)


def _run_forever(rig, until, seconds=20.0) -> None:
    stop = threading.Event()
    thread = threading.Thread(target=rig.worker.run_forever, args=(stop,), daemon=True)
    thread.start()
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline and not until():
            time.sleep(0.02)
    finally:
        stop.set()
        thread.join(5)
    assert not thread.is_alive()


# -- 1. a cancelled task never goes to the card (R1, R2) ----------------------------------------

@pytest.mark.parametrize("state", ["waiting_gpu", "waiting_ready"])
def test_recover_cancels_a_task_that_was_cancelled_while_it_waited(rig, state):
    _, _, _, task_id = _queued_keyframe(rig)
    rig.conn.execute("UPDATE task SET status = ?, cancel_requested_at = ? WHERE id = ?",
                     (state, time.time(), task_id))
    assert rig.worker.recover() == 0
    assert _status_of(rig, task_id) == "cancelled"          # by the recovery itself, not the claim
    rig.drain()
    task = rig.task(task_id)
    assert (task["status"], task["engine_stop"], len(rig.comfy.prompts)) == (
        "cancelled", None, 0)
    assert rig.events("task_cancelled") == [("task_cancelled", {"engine_stop": None})]


@pytest.mark.parametrize("state", ["waiting_gpu", "waiting_ready"])
def test_recover_puts_a_waiting_task_back_in_the_queue(rig, state):
    _, _, _, task_id = _queued_keyframe(rig)
    rig.conn.execute("UPDATE task SET status = ?, wait_reason = 'x' WHERE id = ?",
                     (state, task_id))
    assert rig.worker.recover() == 0
    assert (_status_of(rig, task_id), rig.task(task_id)["wait_reason"]) == ("queued", None)


def test_a_cancel_pressed_while_the_card_is_acquired_never_reaches_the_engine(rig):
    _, _, _, task_id = _queued_keyframe(rig)
    real = rig.gate.acquire

    def acquire(wait):
        rig.client.post(f"/api/studio/tasks/{task_id}/cancel", json={"reason": "x"})
        return real(wait)
    rig.gate.acquire = acquire
    rig.drain()
    task = rig.task(task_id)
    assert (task["status"], task["engine_stop"], len(rig.comfy.prompts)) == (
        "cancelled", None, 0)


def test_a_queued_task_with_a_cancel_flag_is_cancelled_by_the_claim(rig):
    _, _, _, task_id = _queued_keyframe(rig)
    rig.conn.execute("UPDATE task SET cancel_requested_at = ? WHERE id = ?",
                     (time.time(), task_id))
    assert rig.worker.run_once() is False
    assert (_status_of(rig, task_id), len(rig.comfy.prompts)) == ("cancelled", 0)


def test_a_cancel_pressed_before_the_retry_is_not_retried(rig):
    rig.comfy.hold = True
    _, _, _, task_id = _queued_keyframe(rig)
    real_status, real_find = rig.executor.status, rig.executor.find
    restarted = []

    def status(ref):
        answer = real_status(ref)
        if answer["state"] == "running" and not restarted:
            restarted.append(1)
            rig.comfy.restart()
            rig.comfy.hold = False
            _engine_started(rig, time.time() + 60)
        return answer

    def find(spec):
        found = real_find(spec)
        rig.client.post(f"/api/studio/tasks/{task_id}/cancel", json={"reason": "поздно"})
        return found
    rig.executor.status, rig.executor.find = status, find
    rig.drain()
    task = rig.task(task_id)
    assert (task["status"], task["attempts"], len(rig.comfy.prompts),
            rig.events("task_requeued")) == ("cancelled", 0, 1, [])


# -- 2. an exception in one task is not the death of the worker (R3) ----------------------------

def test_comfyui_down_during_the_download_fails_the_task_and_not_the_worker(rig):
    _, _, _, task_id = _queued_keyframe(rig)
    real_result = rig.executor.result

    def refuse(ref):
        raise ComfyError("connection refused")

    def result(ref, dest):
        rig.executor.api.history = refuse
        return real_result(ref, dest)
    rig.executor.result = result
    rig.drain()
    task = rig.task(task_id)
    assert (task["status"], task["error"]) == ("failed", "connection refused")


def test_an_exception_before_the_engine_has_the_task_fails_it_and_the_loop_goes_on(rig):
    _, _, _, first = _queued_keyframe(rig)
    _, _, _, second = _queued_keyframe(rig)
    real = rig.executor.probe
    calls = []

    def probe(spec):
        calls.append(spec.task_id)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(spec)
    rig.executor.probe = probe
    _run_forever(rig, lambda: _status_of(rig, second) == "succeeded")
    assert (rig.task(first)["status"], rig.task(first)["error"], rig.task(second)["status"]) == (
        "failed", "воркер упал: boom", "succeeded")


def test_an_exception_after_the_engine_took_the_task_keeps_its_ref(rig):
    _, _, _, task_id = _queued_keyframe(rig)

    def result(ref, dest):
        raise RuntimeError("boom")
    rig.executor.result = result
    _run_forever(rig, lambda: _status_of(rig, task_id) == "submit_unknown")
    task = rig.task(task_id)
    [job] = rig.comfy.prompts
    assert (task["status"], task["wait_reason"]) == (
        "submit_unknown", "Воркер упал, ComfyUI задачу принял: boom")
    assert rig.conn.execute("SELECT engine_ref FROM task").fetchone()[0] == job["prompt_id"]


def test_a_task_whose_inputs_cannot_be_read_does_not_stop_the_recovery(rig):
    task_id = _crash_while_running(rig)
    rig.conn.execute("UPDATE task SET payload_json = '{\"template\": \"no-such\"}' WHERE id = ?",
                     (task_id,))
    reborn = _reborn(rig)
    try:
        assert reborn.recover() == 0
        assert reborn.run_once() is False
    finally:
        reborn.conn.close()
    task = rig.task(task_id)
    assert task["status"] == "submit_unknown"
    assert task["wait_reason"].startswith("Воркер упал, ComfyUI задачу принял: ")


# -- 3. /gpu/release under a task (R6) -----------------------------------------------------------

@pytest.mark.parametrize("state", ["waiting_ready", "submitting", "running", "downloading",
                                   "verifying", "cancelling"])
def test_release_is_refused_under_a_task_on_the_engine(rig, state):
    _, _, _, task_id = _queued_keyframe(rig)
    rig.conn.execute("UPDATE task SET status = ? WHERE id = ?", (state, task_id))
    r = rig.client.post("/api/studio/gpu/release")
    assert (r.status_code, r.json()) == (409, {"error": {
        "code": "task_running", "message": "Идёт рендер — сначала отмените задачу",
        "detail": {"task_ids": [task_id]}}})


def test_release_is_allowed_when_no_task_holds_the_engine(rig):
    _, _, _, task_id = _queued_keyframe(rig)
    r = rig.client.post("/api/studio/gpu/release")
    assert r.status_code == 200, r.text


def test_a_task_in_the_download_is_the_engines_current_model_for_the_plan(rig):
    from h3_48gb.studio import plans
    _, _, _, task_id = _queued_keyframe(rig)
    rig.conn.execute("UPDATE task SET status = 'downloading', payload_json = ? WHERE id = ?",
                     ('{"template": "h3-fl2va"}', task_id))
    assert plans.template_before_new_image(rig.conn) == "h3-fl2va"


# -- 4. the keyframe plan and the versions it was made from (R4) ---------------------------------

def _plan_of_scene(rig):
    p, s1 = _scene1(rig)
    plan = rig.post(f"/projects/{p['id']}/plan", {"kind": "image", "scene_id": s1["id"]},
                    key=False)
    return p, s1, plan


def test_a_plan_survives_nothing_but_the_scene_it_was_made_from(rig):
    p, s1, plan = _plan_of_scene(rig)
    r = rig.client.patch(f"/api/studio/scenes/{s1['id']}", json={
        "expected_edit_version": s1["edit_version"], "state_in_text": "Night, in the rain."})
    assert r.status_code == 200, r.text
    r = rig.client.post(f"/api/studio/projects/{p['id']}/image",
                        json={"plan_id": plan["plan_id"]}, headers={"Idempotency-Key": "k1"})
    scene = r.json()["error"]["detail"]
    assert (r.status_code, r.json()["error"]["code"]) == (409, "plan_stale")
    assert scene == {"scene_id": s1["id"],
                     "planned": {"edit_version": s1["edit_version"], "project_revision": 3},
                     "current": {"edit_version": s1["edit_version"] + 1, "project_revision": 3}}
    # the refused press did not spend the plan
    assert rig.conn.execute("SELECT used_at FROM render_plan WHERE id = ?",
                            (plan["plan_id"],)).fetchone()[0] is None
    assert rig.conn.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 0


def test_a_plan_is_stale_after_a_change_of_the_project_too(rig):
    p, s1, plan = _plan_of_scene(rig)
    r = rig.client.patch(f"/api/studio/projects/{p['id']}", json={
        "expected_revision": 3, "title": "Другой лес"})
    assert r.status_code == 200, r.text
    r = rig.client.post(f"/api/studio/projects/{p['id']}/image",
                        json={"plan_id": plan["plan_id"]}, headers={"Idempotency-Key": "k1"})
    assert (r.status_code, r.json()["error"]["code"],
            r.json()["error"]["detail"]["current"]["project_revision"]) == (409, "plan_stale", 4)


def test_an_untouched_plan_is_confirmed_and_a_free_plan_has_no_versions(rig):
    p, s1, plan = _plan_of_scene(rig)
    rig.post(f"/projects/{p['id']}/image", {"plan_id": plan["plan_id"]}, expect=201)
    free = rig.post(f"/projects/{p['id']}/plan", {"kind": "image", "prompt": "x", "width": 1344,
                                                  "height": 768}, key=False)
    assert rig.conn.execute("SELECT versions_json FROM render_plan WHERE id = ?",
                            (free["plan_id"],)).fetchone()[0] == "{}"
    rig.post(f"/projects/{p['id']}/image", {"plan_id": free["plan_id"]}, expect=201)


# -- 5. every transition is an event (R5) --------------------------------------------------------

def test_waiting_for_the_card_is_told_and_cleared_by_events(tmp_path):
    rig = Rig(tmp_path, acquire=({"ok": True, "state": "wait_qwen", "engine": "comfy-studio"},
                                 READY))
    try:
        _, _, _, task_id = _queued_keyframe(rig)
        rig.drain()
        told = [(k, p) for k, p in rig.events("task_status", "task_waiting")
                if k == "task_waiting" or p["status"] in ("waiting_gpu", "waiting_ready")]
        why = "ждём GPU: Qwen держит карту"
        assert told == [
            ("task_status", {"task_id": task_id, "status": "waiting_gpu", "wait_reason": None}),
            ("task_status", {"task_id": task_id, "status": "waiting_gpu", "wait_reason": why}),
            ("task_waiting", {"wait_reason": why}),
            ("task_status", {"task_id": task_id, "status": "waiting_ready",
                             "wait_reason": None})]
    finally:
        rig.close()


# -- 6. submit_unknown: one look, and a cancel -----------------------------------------------------

def test_an_uncertain_submit_that_the_engine_did_take_is_followed_on(rig):
    _, _, _, task_id = _queued_keyframe(rig)
    real = rig.executor.api.submit_prompt

    def submit(payload):
        real(payload)                         # ComfyUI got it...
        raise ComfyError("timed out")         # ...the answer did not come back
    rig.executor.api.submit_prompt = submit
    rig.drain()
    [job] = rig.comfy.prompts
    assert (_status_of(rig, task_id), len(rig.comfy.prompts)) == ("succeeded", 1)
    assert rig.conn.execute("SELECT engine_ref FROM task").fetchone()[0] == job["prompt_id"]


def test_an_uncertain_submit_the_engine_did_not_take_stays_submit_unknown(rig):
    _, _, _, task_id = _queued_keyframe(rig)

    def submit(payload):
        raise ComfyError("timed out")
    rig.executor.api.submit_prompt = submit
    assert rig.drain() == 1
    task = rig.task(task_id)
    assert (task["status"], task["wait_reason"], task["attempts"], len(rig.comfy.prompts)) == (
        "submit_unknown", "Не знаю, принял ли ComfyUI задачу: timed out", 0, 0)
    assert rig.events("task_submit_unknown") == [("task_submit_unknown",
                                                  {"error": "timed out"})]


def test_a_task_cut_off_while_submitting_is_looked_for_once_on_restart(rig):
    from h3_48gb.studio.images import ImageKind
    _, _, _, task_id = _queued_keyframe(rig)
    rig.comfy.hold = True
    row = rig.conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
    rig.executor.submit(ImageKind(rig.home, machine="alex-neuro").spec(rig.conn, row))
    rig.conn.execute("UPDATE task SET status = 'submitting', submit_attempt_at = ? WHERE id = ?",
                     (time.time(), task_id))
    rig.comfy.release()
    assert rig.worker.recover() == 1
    assert (_status_of(rig, task_id), len(rig.comfy.prompts)) == ("succeeded", 1)


def test_a_submit_unknown_task_can_be_cancelled(rig):
    _, _, _, task_id = _queued_keyframe(rig)
    rig.conn.execute("UPDATE task SET status = 'submit_unknown' WHERE id = ?", (task_id,))
    r = rig.client.post(f"/api/studio/tasks/{task_id}/cancel", json={"reason": "не нужно"})
    assert (r.status_code, r.json()["status"], r.json()["cancel_requested"]) == (
        200, "cancelled", True)
    r = rig.client.post(f"/api/studio/tasks/{task_id}/cancel", json={"reason": ""})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "task_finished")


# -- 7. what the review's mutations must not survive ---------------------------------------------

def test_without_proof_of_a_restart_unknown_is_not_a_restart(rig):
    """M2: a dispatcher that does not answer is `None`, not `True`."""
    rig.comfy.hold = True
    _, _, _, task_id = _queued_keyframe(rig)
    real = rig.executor.status

    def status(ref):
        answer = real(ref)
        if answer["state"] == "running":
            rig.comfy.restart()
        return answer
    rig.executor.status = status
    rig.executor.restarted_since = lambda t: None
    rig.drain()
    task = rig.task(task_id)
    assert (task["status"], task["attempts"], len(rig.comfy.prompts)) == (
        "submit_unknown", 0, 1)


@pytest.mark.parametrize("state", ["running", "downloading", "verifying"])
def test_a_reborn_worker_takes_the_task_on_from_any_state_of_the_engine(rig, state):
    task_id = _crash_while_running(rig)
    rig.conn.execute("UPDATE task SET status = ? WHERE id = ?", (state, task_id))
    reborn = _reborn(rig)
    try:
        assert reborn.recover() == 1
    finally:
        reborn.conn.close()
    assert (rig.task(task_id)["status"], len(rig.comfy.prompts)) == ("succeeded", 1)


def test_a_reborn_worker_finishes_the_cancel_it_found_half_done(rig):
    task_id = _crash_while_running(rig)
    rig.comfy.hold = True
    rig.conn.execute("UPDATE task SET status = 'cancelling', cancel_requested_at = ? "
                     "WHERE id = ?", (time.time(), task_id))
    reborn = _reborn(rig)
    try:
        assert reborn.recover() == 1
    finally:
        reborn.conn.close()
    task = rig.task(task_id)
    assert (task["status"], task["engine_stop"], rig.comfy.interrupts) == (
        "cancelled", "confirmed", 1)


def test_a_reborn_worker_takes_the_gpu_gate_before_it_follows(rig):
    task_id = _crash_while_running(rig)
    calls = []
    real_acquire, real_done = rig.gate.acquire, rig.gate.done
    rig.gate.acquire = lambda wait: (calls.append("acquire"), real_acquire(wait))[1]
    rig.gate.done = lambda: (calls.append("done"), real_done())[1]
    reborn = _reborn(rig)
    try:
        assert reborn.recover() == 1
    finally:
        reborn.conn.close()
    assert (calls, rig.task(task_id)["status"]) == (["acquire", "done"], "succeeded")


def test_a_cancel_pressed_while_waiting_for_the_card_ends_the_wait(tmp_path):
    """M4: `_wait` answers True at once; the gate then leaves, nothing is submitted."""
    rig = Rig(tmp_path, acquire=({"ok": True, "state": "wait_qwen", "engine": "comfy-studio"},
                                 READY))
    try:
        _, _, _, task_id = _queued_keyframe(rig)
        real_sleep = rig.worker.sleep

        def sleep(seconds):
            rig.client.post(f"/api/studio/tasks/{task_id}/cancel", json={"reason": "x"})
            real_sleep(seconds)
        rig.worker.sleep = sleep
        rig.drain()
        # the gate left at once: the dispatcher was asked ONCE (a wait that ignores the press
        # would ask again, get READY, and only the later re-read would stop the task)
        asked = [c for c in rig.dispatcher.calls if c[1] == "/acquire"]
        assert (_status_of(rig, task_id), len(rig.comfy.prompts), len(asked)) == (
            "cancelled", 0, 1)
    finally:
        rig.close()


def test_isolated_status_errors_never_add_up_to_a_loss_of_contact(rig):
    """M5: 30 failures in a row are a verdict, 30 failures with answers between them are not."""
    from h3_48gb.studio.executors.base import ExecutorError
    rig.comfy.hold = True
    _, _, _, task_id = _queued_keyframe(rig)
    real = rig.executor.status
    calls = []

    def status(ref):
        calls.append(ref)
        if len(calls) == 70:
            rig.comfy.release()
        if len(calls) % 2:
            raise ExecutorError("blip")
        return real(ref)
    rig.executor.status = status
    rig.drain()
    assert (rig.task(task_id)["status"], len(calls) > 70) == ("succeeded", True)


def test_progress_is_told_once_per_value(rig):
    """M6: the expected events come from the input — the script of values the engine reports."""
    _, _, _, task_id = _queued_keyframe(rig)
    real = rig.executor.status
    script = [("running", (1, 8)), ("running", (1, 8)), ("running", (2, 8)), ("running", (2, 8))]

    def status(ref):
        if script:
            state, progress = script.pop(0)
            return {"state": state, "progress": progress, "error": None}
        answer = real(ref)
        return {**answer, "progress": (8, 8) if answer["state"] == "done" else None}
    rig.executor.status = status
    rig.drain()
    assert rig.events("task_progress") == [("task_progress", {"step": 1, "total": 8}),
                                           ("task_progress", {"step": 2, "total": 8}),
                                           ("task_progress", {"step": 8, "total": 8})]


def test_the_idle_release_is_told_whether_the_worker_was_busy(rig):
    """M7."""
    ticks = []

    class Idle:
        def tick(self, busy):
            ticks.append(busy)
    rig.worker.idle = Idle()
    assert rig.worker.run_once() is False
    _queued_keyframe(rig)
    assert rig.worker.run_once() is True
    assert ticks == [False, True]


def test_the_asset_records_the_size_of_the_file(rig):
    """M8."""
    _, _, _, task_id = _queued_keyframe(rig)
    rig.drain()
    asset = rig.conn.execute("SELECT bytes FROM asset").fetchone()
    engine_file = rig.comfy.output_dir / "studio" / task_id / "f_00001_.png"
    assert asset["bytes"] == engine_file.stat().st_size > 1000


def test_an_interrupt_by_somebody_else_fails_the_task_it_does_not_cancel_it(rig):
    """M9 / review small item: nobody pressed cancel; the engine was interrupted from outside."""
    rig.comfy.hold = True
    _, _, _, task_id = _queued_keyframe(rig)
    real = rig.executor.status
    done = []

    def status(ref):
        answer = real(ref)
        if answer["state"] == "running" and not done:
            done.append(1)
            rig.executor.api.interrupt()
        return answer
    rig.executor.status = status
    rig.drain()
    task = rig.task(task_id)
    assert (task["status"], task["error"], task["cancel_requested"], task["engine_stop"]) == (
        "failed", "ComfyUI прервал задачу не по нашей просьбе", False, None)
    assert rig.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 0


def test_a_task_the_engine_finished_while_we_cancelled_is_reported_as_finished(rig):
    """M10: the cancel loop sees `done`: engine_stop = finished, never an attached result."""
    rig.comfy.hold = True
    _, _, _, task_id = _queued_keyframe(rig)
    real = _hold_and_press_cancel(rig, task_id)
    pressed_status = rig.executor.status

    def status(ref):
        if _status_of(rig, task_id) == "cancelling":
            return {"state": "done", "progress": None, "error": None}
        return pressed_status(ref)
    rig.executor.status = status
    rig.drain()
    task = rig.task(task_id)
    assert (task["status"], task["engine_stop"], task["result_asset_id"]) == (
        "cancelled", "finished", None)
    rig.executor.status = real


def test_the_gate_is_given_back_after_every_task(tmp_path):
    """M12: after a success and after a failure."""
    for kwargs in ({}, {"fail": "CUDA out of memory"}):
        rig = Rig(tmp_path / str(len(kwargs)), comfy_kwargs=kwargs)
        try:
            _queued_keyframe(rig)
            done = []
            real = rig.gate.done
            rig.gate.done = lambda: (done.append(1), real())[1]
            rig.drain()
            assert done == [1]
        finally:
            rig.close()


# -- small items of the review -------------------------------------------------------------------

def test_a_blank_picture_fails_verification(tmp_path):
    rig = Rig(tmp_path, comfy_kwargs={"flat": True})
    try:
        _, _, _, task_id = _queued_keyframe(rig)
        rig.drain()
        assert (rig.task(task_id)["status"], rig.task(task_id)["error"]) == (
            "failed", "результат не прошёл проверку: кадр залит одним цветом")
        assert rig.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 0
    finally:
        rig.close()


def test_the_files_of_a_retried_task_are_only_the_newest_run(rig):
    from PIL import Image
    folder = rig.comfy.output_dir / "studio" / "k_a"
    other = rig.comfy.output_dir / "studio" / "k_b"
    for place, name in ((folder, "f_00001_.png"), (folder, "f_00002_.png"),
                        (other, "f_00001_.png")):
        place.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4)).save(place / name)
    dest = rig.home / "x"
    got = rig.executor.result("file:k_a", dest)
    assert [p.name for p in got] == ["f_00002_.png"]
    assert sorted(p.name for p in dest.iterdir()) == ["f_00002_.png"]


def test_a_cancel_pressed_long_ago_still_gets_one_look_at_the_engine(rig):
    """After a restart the 30 s may be long over: the engine is asked once before «unconfirmed»."""
    task_id = _crash_while_running(rig)
    rig.conn.execute("UPDATE task SET cancel_requested_at = ? WHERE id = ?",
                     (time.time() - 100.0, task_id))
    rig.executor.cancel = lambda ref: None
    asked = []

    def status(ref):
        asked.append(ref)
        return {"state": "cancelled", "progress": None, "error": None}
    rig.executor.status = status
    reborn = _reborn(rig)
    try:
        assert reborn.recover() == 1
    finally:
        reborn.conn.close()
    assert (rig.task(task_id)["engine_stop"], len(asked)) == ("confirmed", 1)


def test_the_readiness_deadline_does_not_follow_the_wall_clock(rig):
    """Deadlines run on the monotonic clock: a step of the wall clock changes nothing."""
    _, _, _, task_id = _queued_keyframe(rig)
    real = rig.executor.probe
    calls = []

    def probe(spec):
        calls.append(1)
        return real(spec) if len(calls) > 3 else {"alive": True, "ready": False, "detail": "x"}
    rig.executor.probe = probe
    jumps = []

    def clock():
        jumps.append(1)
        return time.time() + 1e6 * len(jumps)
    rig.worker.clock = clock
    rig.drain()
    assert (_status_of(rig, task_id), len(calls)) == ("succeeded", 4)


def test_a_status_that_stays_lost_while_find_names_the_ref_ends_in_submit_unknown(rig):
    _, _, _, task_id = _queued_keyframe(rig)
    rig.executor.status = lambda ref: {"state": "lost", "progress": None, "error": None}
    rig.drain()
    task = rig.task(task_id)
    assert (task["status"], task["wait_reason"]) == (
        "submit_unknown", "ComfyUI называет задачу, но не отдаёт её состояние")
