"""Slice S2, the worker on the keyframe (spec §8.5 lifecycle, §8.4 GPU gate, И9): the real API,
worker and ComfyUI adapter against FakeComfyStudio and FakeDispatcher (`_studio_rig`)."""
from __future__ import annotations

import hashlib
import time

import pytest

from _studio_rig import READY, Rig

PORTRAIT = "a young woman with a loose dark braid in a pale linen dress"


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
    kinds = [k for k, _ in rig.events() if k.startswith("task_")]
    assert kinds == ["task_queued", "task_running"] + ["task_progress"] * len(
        [k for k in kinds if k == "task_progress"]) + ["task_succeeded"]
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


def test_the_old_panels_free_the_card_button_counts_as_a_restart(rig):
    """Revision 4 (coordinator 23.10, review of task 1): the old panel's «Освободить карту»
    (`release(everything=True)`, web.py:5899; also `released_by_user`, `qwen_restore`) stops
    `comfy-studio` mid-task and leaves no record of it in the dispatcher's /status. Accepted —
    and the studio must read it as a restart (§8.5): `restarted_since` is True without a record,
    so the task is retried once, not left as submit_unknown."""
    rig.comfy.hold = True
    _, _, _, task_id = _queued_keyframe(rig)
    real = rig.executor.status
    released = []

    def status(ref):
        answer = real(ref)
        if answer["state"] == "running" and not released:
            released.append(ref)
            rig.comfy.restart()             # the panel's release killed the studio's ComfyUI
            rig.comfy.hold = False
            rig.dispatcher.own = {}         # ...and the dispatcher keeps no record of it
        return answer
    rig.executor.status = status
    rig.drain()
    task = rig.task(task_id)
    assert (task["status"], task["attempts"], len(rig.comfy.prompts)) == ("succeeded", 1, 2)
    assert rig.events("task_requeued") == [("task_requeued", {"reason": "lost"})]


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
    rig.worker.clock, rig.worker.sleep, rig.worker.poll_s = fake.clock, fake.sleep, 1.0
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
    rig.worker.clock, rig.worker.sleep, rig.worker.poll_s = fake.clock, fake.sleep, 1.0
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
