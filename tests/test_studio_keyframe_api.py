"""Slice S2, the API of the keyframe (spec §7 O10 image, O12, O13, O14, O22, O23; §9.1 the
keyframe prompt; §7 Idempotency-Key). The worker is not here: test_studio_worker.py runs it."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from _fake_comfy_studio import FakeComfyStudio
from _fake_dispatcher import FakeDispatcher
from _studio_fixtures import BASE_URL, ScriptedLlm
from h3_48gb.engines.dispatcher_client import DispatcherClient
from h3_48gb.studio import app as app_module
from h3_48gb.studio import config, db, files, settings, tasks
from h3_48gb.studio.app import create_app
from h3_48gb.studio.director.keyframe_prompt import keyframe_prompt

PORTRAIT = "a young woman with a loose dark braid in a pale linen dress"


class ApiRig:
    """The API with a FakeDispatcher and a FakeComfyStudio behind the settings; no worker."""

    def __init__(self, tmp_path):
        self.outdir = tmp_path / "out"
        self.comfy = FakeComfyStudio(tmp_path / "comfy-out")
        self.dispatcher = FakeDispatcher()
        self.client = TestClient(
            create_app(self.outdir, llm=ScriptedLlm([]),
                       dispatcher_factory=lambda url, client: DispatcherClient(
                           url, client=client, timeout=5)),
            base_url=BASE_URL)
        self.conn = db.connect(config.studio_dir(self.outdir) / "studio.sqlite")
        settings.put(self.conn, "comfy.url", self.comfy.url)
        settings.put(self.conn, "comfy.output_dir", str(self.comfy.output_dir))
        settings.put(self.conn, "dispatcher.url", self.dispatcher.url)

    def close(self):
        self.comfy.close()
        self.dispatcher.close()
        self.conn.close()

    def project(self, frame_format="16:9") -> dict:
        r = self.client.post("/api/studio/projects", json={"title": "Лес",
                                                           "frame_format": frame_format})
        assert r.status_code == 201, r.text
        return r.json()

    def post(self, path, body, key=None, expect=200):
        headers = {"Idempotency-Key": key} if key else {}
        r = self.client.post(f"/api/studio{path}", json=body, headers=headers)
        assert r.status_code == expect, (r.status_code, r.text)
        return r.json()


@pytest.fixture
def rig(tmp_path):
    r = ApiRig(tmp_path)
    yield r
    r.close()


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


def test_the_keyframe_prompt_is_style_portraits_first_moment_and_framing():
    assert keyframe_prompt(style_block="", portrait_phrases=[PORTRAIT],
                           state_in="she stands at the edge of the lake at dawn",
                           action_text="ignored") == (
        "Live-action photograph, natural light, natural colors. "
        "A young woman with a loose dark braid in a pale linen dress. "
        "She stands at the edge of the lake at dawn. "
        "Medium shot at eye level, the face fully visible, sharp focus on the face.")
    assert keyframe_prompt(style_block="Soft 35 mm look", portrait_phrases=[],
                           state_in=" ", action_text="She runs. Then she stops.") == (
        "Soft 35 mm look. She runs. "
        "Medium shot at eye level, the face fully visible, sharp focus on the face.")


def test_a_scene_keyframe_plan(rig):
    p, s1 = _scene1(rig)
    plan = rig.post(f"/projects/{p['id']}/plan", {"kind": "image", "scene_id": s1["id"]})
    [item] = plan["items"]
    assert {k: v for k, v in item.items() if k != "seed"} == {
        "kind": "image", "scene_id": s1["id"], "template": "krea2-t2i",
        # sha256 of krea2-t2i.card.json + .graph.json (22.10) — the version the task must run
        "template_sha256": "b16051ff44452751a5bf5f9773e1d46ad1e3a2dbd5b2358b0237e443b1f00fb2",
        "prompt": "Live-action photograph, natural light, natural colors. "
                  "A young woman with a loose dark braid in a pale linen dress. "
                  "She stands at the edge of the lake at dawn. "
                  "Medium shot at eye level, the face fully visible, sharp focus on the face.",
        "width": 1344, "height": 768, "steps": 8, "estimate_s": 6.0, "estimate_source": "card",
        "status": "ready", "reasons": []}
    assert 1 <= item["seed"] < 2**31
    assert (plan["kind"], plan["total_s"], plan["checkpoint_switches"], plan["blocked"],
            plan["invalid"]) == ("image", 6.0, [], [], [])


def test_portrait_frames_are_768_by_1344_and_the_switch_from_h3_is_counted(rig):
    p, s1 = _scene1(rig, frame_format="9:16")
    rig.conn.execute("INSERT INTO task (id, kind, executor, gpu_scope, payload_json, status, "
                     "engine_submitted_at, created_at, updated_at) VALUES ('k_old', 'render', "
                     "'comfy', 'x', '{\"template\": \"h3-fl2va\"}', 'succeeded', 1.0, 'n', 'n')")
    plan = rig.post(f"/projects/{p['id']}/plan", {"kind": "image", "scene_id": s1["id"]})
    assert (plan["items"][0]["width"], plan["items"][0]["height"]) == (768, 1344)
    assert plan["checkpoint_switches"] == [{"from": "h3-fl2va", "to": "krea2-t2i",
                                            "seconds": 17.0}]
    assert plan["total_s"] == 23.0


def test_the_switch_costs_what_the_target_card_says_and_never_raises(monkeypatch):
    """Revision 4 (spec §8.4 rev. 3, §14 seam 1): the price of a switch belongs to the TARGET
    template — its card's `limits.switch_in_s`; until a card carries it, the measured value of
    that template; a template measured nowhere costs the dearest known load (38 s) — never a
    KeyError, so S2b's Z-Image and Qwen cards cannot break the S2 plan. fl2va <-> ref2va is the
    checkpoint swap inside H3 (16 s)."""
    from h3_48gb.studio import estimates, templates
    real = templates.load
    carded = templates.Template(id="zimage-turbo-t2i", graph={}, sha256="z",
                                card={"requires_json": {"limits": {"switch_in_s": 10.5}}})
    monkeypatch.setattr(templates, "load",
                        lambda t: carded if t == "zimage-turbo-t2i" else real(t))
    pairs = [(None, "h3-fl2va"), ("krea2-t2i", "krea2-t2i"), ("krea2-t2i", "h3-fl2va"),
             ("h3-fl2va", "krea2-t2i"), ("h3-fl2va", "h3-ref2va"), ("h3-ref2va", "h3-fl2va"),
             ("h3-fl2va", "zimage-turbo-t2i"), ("zimage-turbo-t2i", "h3-ref2va"),
             ("krea2-t2i", "qwen-image-t2i")]
    assert [estimates.switch_cost(a, b) for a, b in pairs] == [
        0.0, 0.0, 38.0, 17.0, 16.0, 16.0, 10.5, 38.0, 38.0]


def test_one_queue_order_pictures_first_and_in_order_renders_on_the_loaded_model_first(tmp_path):
    """Revision 4 (spec §8.4 `queue_order`, rev. 3.1, §14 seam 5): one order for the worker and
    the plan. Pictures (priority 10) before renders; pictures strictly in the order they were
    queued whatever the engine holds; among renders the loaded model first, then age."""
    import json

    from h3_48gb.studio import db, tasks
    conn = db.connect(tmp_path / "q.sqlite")
    db.migrate(conn)
    rows = [("r_ref", "render", 0, "h3-ref2va"), ("i_qwen", "image", 10, "qwen-image-t2i"),
            ("r_fl", "render", 0, "h3-fl2va"), ("i_krea", "image", 10, "krea2-t2i"),
            ("i_z", "image", 10, "zimage-turbo-t2i")]
    for task_id, kind, priority, template in rows:
        conn.execute("INSERT INTO task (id, kind, executor, gpu_scope, payload_json, status, "
                     "priority, created_at, updated_at) VALUES (?, ?, 'comfy', 'x', ?, 'queued', "
                     "?, 'n', 'n')", (task_id, kind, json.dumps({"template": template}),
                                      priority))

    def order(loaded, kinds=("image", "render"), limit=None):
        return [r["id"] for r in tasks.queued_in_order(conn, kinds=kinds,
                                                       loaded_template=loaded, limit=limit)]
    assert order("h3-fl2va") == ["i_qwen", "i_krea", "i_z", "r_fl", "r_ref"]
    assert order("krea2-t2i") == ["i_qwen", "i_krea", "i_z", "r_ref", "r_fl"]
    assert order("krea2-t2i", kinds=("image",), limit=1) == ["i_qwen"]


def test_a_frame_fits_the_project_within_two_percent():
    """Revision 4 (spec §10 L7d, rev. 3.1): the ratio of sides within 2 % of the frame format."""
    from h3_48gb.studio import frame_shape
    wide = [(1344, 768), (1920, 1088), (1664, 928), (896, 512), (1024, 1024), (1152, 896),
            (768, 1344), (1280, 768), (1216, 704)]           # the last one is 2.8 % off
    assert [frame_shape.matches(w, h, "16:9") for w, h in wide] == [
        True, True, True, True, False, False, False, False, False]
    assert [frame_shape.matches(w, h, "9:16") for w, h in [(768, 1344), (1088, 1920),
                                                           (544, 960), (1344, 768)]] == [
        True, True, True, False]
    assert [frame_shape.shape_name(w, h) for w, h in [(1344, 768), (1024, 1024), (1152, 896),
                                                      (896, 1152), (512, 896)]] == [
        "16:9", "1:1", "4:3", "3:4", "9:16"]


def test_a_free_keyframe_needs_a_size_on_the_64_grid(rig):
    p = rig.project()
    r = rig.client.post(f"/api/studio/projects/{p['id']}/plan",
                        json={"kind": "image", "prompt": "x", "width": 1000, "height": 768})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "size_invalid"
    r = rig.client.post(f"/api/studio/projects/{p['id']}/plan", json={"kind": "image"})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "args_invalid"


def test_confirming_is_idempotent_and_happens_once(rig):
    p, s1 = _scene1(rig)
    plan = rig.post(f"/projects/{p['id']}/plan", {"kind": "image", "scene_id": s1["id"]})
    body = {"plan_id": plan["plan_id"]}
    r = rig.client.post(f"/api/studio/projects/{p['id']}/image", json=body)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "idempotency_key_required"
    first = rig.post(f"/projects/{p['id']}/image", body, key="k1", expect=201)
    assert first == {"task_id": first["task_id"], "status": "queued"}
    assert rig.post(f"/projects/{p['id']}/image", body, key="k1", expect=200) == first
    other = rig.post(f"/projects/{p['id']}/plan", {"kind": "image", "scene_id": s1["id"]})
    r = rig.client.post(f"/api/studio/projects/{p['id']}/image",
                        json={"plan_id": other["plan_id"]}, headers={"Idempotency-Key": "k1"})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "idempotency_mismatch"
    r = rig.client.post(f"/api/studio/projects/{p['id']}/image", json=body,
                        headers={"Idempotency-Key": "k2"})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "plan_used"
    assert rig.conn.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 1
    # revision 4: a keyframe is a picture — priority 10 (§8.4); its flat payload (§19.3) carries
    # exactly the planned values (revision 4.1, Codex r4 finding 4: values, not only keys)
    item = plan["items"][0]
    row = rig.conn.execute("SELECT priority, payload_json FROM task").fetchone()
    assert (row["priority"], json.loads(row["payload_json"])) == (10, {
        "template": "krea2-t2i", "template_sha256": item["template_sha256"],
        "prompt": item["prompt"], "width": 1344, "height": 768, "seed": item["seed"],
        "steps": 8, "scene_id": s1["id"]})
    stale = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    rig.conn.execute("UPDATE render_plan SET expires_at = ? WHERE id = ?",
                     (stale, other["plan_id"]))
    r = rig.client.post(f"/api/studio/projects/{p['id']}/image",
                        json={"plan_id": other["plan_id"]}, headers={"Idempotency-Key": "k3"})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "plan_expired"
    r = rig.client.post(f"/api/studio/projects/{p['id']}/image",
                        json={"plan_id": other["plan_id"]}, headers={"Idempotency-Key": "k4"})
    assert r.json()["error"]["code"] == "plan_expired"      # a failed confirm stores nothing


def test_a_queued_keyframe_is_cancelled_at_once(rig):
    p, s1 = _scene1(rig)
    plan = rig.post(f"/projects/{p['id']}/plan", {"kind": "image", "scene_id": s1["id"]})
    task_id = rig.post(f"/projects/{p['id']}/image", {"plan_id": plan["plan_id"]}, key="k",
                       expect=201)["task_id"]
    task = rig.client.get(f"/api/studio/tasks/{task_id}").json()
    assert {k: task[k] for k in ("status", "kind", "template", "cancel_requested")} == {
        "status": "queued", "kind": "image", "template": "krea2-t2i", "cancel_requested": False}
    r = rig.client.post(f"/api/studio/tasks/{task_id}/cancel", json={"reason": "передумал"})
    assert (r.status_code, r.json()["status"], r.json()["cancel_requested"]) == (
        200, "cancelled", True)
    r = rig.client.post(f"/api/studio/tasks/{task_id}/cancel", json={"reason": ""})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "task_finished"
    kinds = [r[0] for r in rig.conn.execute("SELECT kind FROM event WHERE task_id = ? "
                                            "ORDER BY seq", (task_id,))]
    assert kinds == ["task_queued", "task_cancel_requested", "task_cancelled"]
    snap = rig.client.get(f"/api/studio/projects/{p['id']}").json()
    assert [(t["id"], t["status"]) for t in snap["tasks"]] == [(task_id, "cancelled")]
    r = rig.client.get("/api/studio/tasks/k_nope")
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "task_not_found"


def test_executor_and_gpu_operations(rig, monkeypatch):
    monkeypatch.setattr(app_module, "EXECUTORS_TTL_S", 0.0)    # this test edits the fake between calls
    [ex] = rig.client.get("/api/studio/executors").json()
    assert {k: ex[k] for k in ("name", "address", "scope", "alive", "ready", "capabilities",
                               "detail")} == {
        "name": "comfy", "address": rig.comfy.url, "scope": "dispatcher:comfy-studio",
        "alive": True, "ready": True,
        "capabilities": {"find": "exact", "restart_evidence": True, "progress": True,
                         "cancel": "hard", "async_submit": True}, "detail": "готов"}
    rig.comfy.classes.remove("MiniMaxH3ReferenceToVideo")
    [ex] = rig.client.get("/api/studio/executors").json()
    assert (ex["ready"], ex["detail"]) == (
        False, "h3-ref2va: в ComfyUI нет узлов: MiniMaxH3ReferenceToVideo")
    gpu = rig.client.get("/api/studio/gpu").json()
    assert gpu == rig.dispatcher.status_body() | {"gpu": gpu["gpu"]}
    assert rig.client.post("/api/studio/gpu/release").json() == {"ok": True, "stopped": ["h3"]}
    r = rig.client.post("/api/studio/gpu/qwen/unload")
    assert (r.status_code, r.json()) == (200, {"ok": True, "was_running": True, "exit_code": 0})
    assert [(c[1], c[2]) for c in rig.dispatcher.calls if c[0] == "POST"] == [
        ("/release", {"client": "studio-worker", "all": False}), ("/qwen/unload", {})]
    assert [h.get("Origin") for h in rig.dispatcher.headers] == [None] * len(rig.dispatcher.headers)
    settings.put(rig.conn, "comfy.scope", "local-mac")
    r = rig.client.get("/api/studio/gpu")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "no_dispatcher"
    settings.put(rig.conn, "comfy.scope", "dispatcher:comfy-studio")
    settings.put(rig.conn, "dispatcher.url", "http://127.0.0.1:9")
    r = rig.client.get("/api/studio/gpu")
    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "dispatcher_unavailable"


# --- review round 1 (task 7) --------------------------------------------------------------------

def _task(rig, task_id, *, status, template="krea2-t2i", project_id=None, kind="image",
          submitted=None, **columns):
    """A task row as the worker would leave it (the worker is not part of this file)."""
    cols = {"id": task_id, "kind": kind, "executor": "comfy", "gpu_scope": "x",
            "payload_json": json.dumps({"template": template}), "status": status,
            "priority": 10 if kind == "image" else 0, "project_id": project_id,
            "engine_submitted_at": submitted, "created_at": "n", "updated_at": "n", **columns}
    rig.conn.execute(f"INSERT INTO task ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                     tuple(cols.values()))


def _event_kinds(rig, task_id):
    return [r[0] for r in rig.conn.execute("SELECT kind FROM event WHERE task_id = ? ORDER BY seq",
                                           (task_id,))]


def test_cancelling_a_taken_task_only_asks_and_a_repeat_asks_nothing_new(rig):
    p = rig.project()
    _task(rig, "k_run", status="running", project_id=p["id"], submitted=1.0)
    r = rig.client.post("/api/studio/tasks/k_run/cancel", json={"reason": "стоп"})
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["cancel_requested"]) == ("running", True)
    assert _event_kinds(rig, "k_run") == ["task_cancel_requested"]
    first = rig.conn.execute("SELECT cancel_requested_at FROM task WHERE id = 'k_run'"
                             ).fetchone()[0]
    r = rig.client.post("/api/studio/tasks/k_run/cancel", json={"reason": "ещё раз"})
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["cancel_requested"]) == ("running", True)
    assert _event_kinds(rig, "k_run") == ["task_cancel_requested"]
    assert rig.conn.execute("SELECT cancel_requested_at FROM task WHERE id = 'k_run'"
                            ).fetchone()[0] == first


def test_a_blocked_task_is_cancelled_at_once(rig):
    p = rig.project()
    _task(rig, "k_blk", status="blocked", project_id=p["id"], kind="render", template="h3-fl2va")
    r = rig.client.post("/api/studio/tasks/k_blk/cancel", json={"reason": ""})
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["cancel_requested"]) == ("cancelled", True)
    assert _event_kinds(rig, "k_blk") == ["task_cancel_requested", "task_cancelled"]


def test_a_plan_belongs_to_its_project(rig):
    pa, sa = _scene1(rig)
    pb = rig.project()
    plan = rig.post(f"/projects/{pa['id']}/plan", {"kind": "image", "scene_id": sa["id"]})
    r = rig.client.post(f"/api/studio/projects/{pb['id']}/image", json={"plan_id": plan["plan_id"]},
                        headers={"Idempotency-Key": "kb"})
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "plan_not_found"
    assert rig.conn.execute("SELECT used_at FROM render_plan WHERE id = ?",
                            (plan["plan_id"],)).fetchone()[0] is None
    rig.post(f"/projects/{pa['id']}/image", {"plan_id": plan["plan_id"]}, key="same", expect=201)
    r = rig.client.post(f"/api/studio/projects/{pb['id']}/image", json={"plan_id": plan["plan_id"]},
                        headers={"Idempotency-Key": "same"})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "idempotency_mismatch"


def test_a_scene_of_another_project_is_not_planned(rig):
    _, scene_a = _scene1(rig)
    pb = rig.project()
    r = rig.client.post(f"/api/studio/projects/{pb['id']}/plan",
                        json={"kind": "image", "scene_id": scene_a["id"]})
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "scene_not_found"


def test_the_task_answer_carries_every_field_of_the_record(rig):
    p = rig.project()
    _task(rig, "k_full", status="running", project_id=p["id"], submitted=1.0,
          wait_reason="ждёт карту", progress_step=3, progress_total=8, error="eh", attempts=2,
          engine_stop="confirmed", engine_version="ComfyUI 0.42.0 · torch 2.9.1 · GPU",
          template_sha256="abc", created_at="c-at", updated_at="u-at")
    rig.conn.execute("INSERT INTO asset (id, kind, path, sha256, bytes, mime, origin_kind, "
                     "created_at) VALUES ('a_1', 'image', 'assets/a_1.png', 'x', 1, 'image/png', "
                     "'take_output', 'n')")
    rig.conn.execute("UPDATE task SET result_asset_id = 'a_1', depends_on_task_id = NULL, "
                     "cancel_requested_at = 5.0 WHERE id = 'k_full'")
    assert rig.client.get("/api/studio/tasks/k_full").json() == {
        "id": "k_full", "kind": "image", "project_id": p["id"], "status": "running",
        "template": "krea2-t2i", "wait_reason": "ждёт карту", "progress_step": 3,
        "progress_total": 8, "error": "eh", "attempts": 2, "engine_stop": "confirmed",
        "engine_version": "ComfyUI 0.42.0 · torch 2.9.1 · GPU", "template_sha256": "abc",
        "cancel_requested": True, "depends_on_task_id": None, "result_asset_id": "a_1",
        "result_url": "/api/studio/assets/a_1/file", "created_at": "c-at", "updated_at": "u-at"}


def test_executors_ask_comfyui_once_keep_the_answer_and_know_their_own_tasks(rig, monkeypatch):
    seen = []
    real = app_module.ComfyApi
    monkeypatch.setattr(app_module, "ComfyApi",
                        lambda url, timeout: seen.append(timeout) or real(url, timeout=timeout))
    [ex] = rig.client.get("/api/studio/executors").json()
    assert sorted(ex["templates"]) == ["h3-fl2va", "h3-ref2va", "krea2-t2i"]
    assert seen == [5.0]
    assert [r for r in rig.comfy.requests if r[1] == "/object_info"] == [("GET", "/object_info")]
    before = list(rig.comfy.requests)
    rig.client.get("/api/studio/executors")
    assert rig.comfy.requests == before             # within 10 s nothing is asked again
    monkeypatch.setattr(app_module, "EXECUTORS_TTL_S", 0.0)
    rig.comfy._running = {"number": 1, "prompt_id": "pid", "prompt": {},
                          "extra_data": {"studio_task_id": "k_own", "client_id": "k_own"}}
    [ex] = rig.client.get("/api/studio/executors").json()
    assert (ex["ready"], ex["detail"]) == (True, "готов")           # the studio's own is not foreign
    rig.comfy.foreign_running = True
    [ex] = rig.client.get("/api/studio/executors").json()
    assert ex["detail"] == "; ".join(f"{t}: в очереди ComfyUI чужие задачи: 1"
                                     for t in ("krea2-t2i", "h3-fl2va", "h3-ref2va"))
    rig.comfy._running = None


def test_the_card_is_not_released_under_a_running_task(rig):
    p = rig.project()
    _task(rig, "k_a", status="running", project_id=p["id"], submitted=1.0)
    r = rig.client.post("/api/studio/gpu/release")
    assert r.status_code == 409, r.text
    assert r.json() == {"error": {"code": "task_running",
                                  "message": "Идёт рендер — сначала отмените задачу",
                                  "detail": {"task_ids": ["k_a"]}}}
    assert [c for c in rig.dispatcher.calls if c[0] == "POST"] == []
    rig.conn.execute("UPDATE task SET status = 'submitting'")
    assert rig.client.post("/api/studio/gpu/release").status_code == 409
    rig.conn.execute("UPDATE task SET status = 'queued'")
    assert rig.client.post("/api/studio/gpu/release").json() == {"ok": True, "stopped": ["h3"]}


def test_the_plan_pays_a_switch_only_when_it_causes_it(rig):
    p, s1 = _scene1(rig)
    # H3 is rendering and a picture is already queued: it pays the 17 s, the new plan behind it none
    _task(rig, "k_h3", status="running", template="h3-fl2va", kind="render", project_id=p["id"],
          submitted=2.0)
    plan = rig.post(f"/projects/{p['id']}/plan", {"kind": "image", "scene_id": s1["id"]})
    assert (plan["checkpoint_switches"], plan["total_s"]) == (
        [{"from": "h3-fl2va", "to": "krea2-t2i", "seconds": 17.0}], 23.0)
    _task(rig, "k_pic", status="queued", project_id=p["id"])
    plan = rig.post(f"/projects/{p['id']}/plan", {"kind": "image", "scene_id": s1["id"]})
    assert (plan["checkpoint_switches"], plan["total_s"]) == ([], 6.0)


def test_the_engine_holds_the_model_of_the_task_submitted_last(rig):
    p, s1 = _scene1(rig)
    _task(rig, "k_old", status="succeeded", template="h3-fl2va", kind="render",
          project_id=p["id"], submitted=1.0)
    _task(rig, "k_new", status="succeeded", template="krea2-t2i", project_id=p["id"],
          submitted=2.0)
    plan = rig.post(f"/projects/{p['id']}/plan", {"kind": "image", "scene_id": s1["id"]})
    assert (plan["checkpoint_switches"], plan["total_s"]) == ([], 6.0)


def test_a_stored_path_stays_inside_the_home(tmp_path):
    conn = db.connect(tmp_path / "home" / "studio.sqlite") if (tmp_path / "home").mkdir() is None \
        else None
    home = files.studio_home(conn)
    assert home == (tmp_path / "home").resolve()
    assert files.stored_path(home, home / "assets" / "a_1.png") == "assets/a_1.png"
    assert files.asset_path(conn, "assets/a_1.png") == home / "assets" / "a_1.png"
    for bad in ("/etc/passwd", "../outside.png", "assets/../../outside.png"):
        with pytest.raises(ValueError):
            files.asset_path(conn, bad)
    with pytest.raises(ValueError):
        files.stored_path(home, tmp_path / "elsewhere" / "a.png")


def test_odd_requests_are_refused_plainly(rig):
    p, s1 = _scene1(rig)
    r = rig.client.post(f"/api/studio/projects/{p['id']}/plan",
                        json={"kind": "image", "scene_id": s1["id"], "prompt": "x"})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "args_invalid"
    r = rig.client.post(f"/api/studio/projects/{p['id']}/plan",
                        json={"kind": "image", "scene_id": s1["id"], "width": 1344})
    assert r.status_code == 400, r.text
    plan = rig.post(f"/projects/{p['id']}/plan", {"kind": "image", "scene_id": s1["id"]})
    r = rig.client.post(f"/api/studio/projects/{p['id']}/image", json={"plan_id": plan["plan_id"]},
                        headers={"Idempotency-Key": "k" * 201})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "idempotency_key_invalid"
    rig.post(f"/projects/{p['id']}/image", {"plan_id": plan["plan_id"]}, key="k" * 200, expect=201)


def test_an_old_key_whose_log_entry_is_gone_is_409_not_500(rig):
    p, s1 = _scene1(rig)
    one = rig.post(f"/projects/{p['id']}/plan", {"kind": "image", "scene_id": s1["id"]})
    rig.post(f"/projects/{p['id']}/image", {"plan_id": one["plan_id"]}, key="old", expect=201)
    rig.conn.execute("DELETE FROM op_log")                          # the 24 h clean-up
    two = rig.post(f"/projects/{p['id']}/plan", {"kind": "image", "scene_id": s1["id"]})
    r = rig.client.post(f"/api/studio/projects/{p['id']}/image", json={"plan_id": two["plan_id"]},
                        headers={"Idempotency-Key": "old"})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "idempotency_key_reused"
    assert rig.conn.execute("SELECT used_at FROM render_plan WHERE id = ?",
                            (two["plan_id"],)).fetchone()[0] is None


def test_the_plan_counts_from_the_task_that_holds_the_engine_now(rig):
    p, s1 = _scene1(rig)
    _task(rig, "k_prev", status="succeeded", project_id=p["id"], submitted=1.0)   # krea, long ago
    _task(rig, "k_now", status="submitting", template="h3-fl2va", kind="render",
          project_id=p["id"])                                  # H3 is being loaded: not stamped yet
    plan = rig.post(f"/projects/{p['id']}/plan", {"kind": "image", "scene_id": s1["id"]})
    assert plan["checkpoint_switches"] == [{"from": "h3-fl2va", "to": "krea2-t2i", "seconds": 17.0}]


def test_a_scene_and_a_free_prompt_are_refused_by_the_schema_and_by_the_plan(rig):
    import pydantic

    from h3_48gb.studio import plans, schemas
    from h3_48gb.studio.errors import StudioError
    with pytest.raises(pydantic.ValidationError):
        schemas.PlanRequest(kind="image", scene_id="s_1", prompt="x")
    p, s1 = _scene1(rig)
    with pytest.raises(StudioError) as caught:
        plans.plan_image(rig.conn, p["id"], scene_id=s1["id"], prompt="x", width=None, height=None)
    assert (caught.value.status, caught.value.code) == (400, "args_invalid")
