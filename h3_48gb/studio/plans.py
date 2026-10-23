"""Dry plans and their confirmation (spec §7 O10–O12). A plan says what will run, how long it will
take and what blocks it; it lives 10 minutes and is confirmed once. Slice S2: the keyframe
(`kind=image`, Krea 2)."""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from h3_48gb.studio import db, estimates, events, ids, settings, tasks, templates
from h3_48gb.studio.director import keyframe_prompt
from h3_48gb.studio.errors import StudioError

PLAN_TTL = timedelta(minutes=10)
EXECUTOR = "comfy"
IMAGE_STEPS = 8          # Krea 2 Turbo, GPU-DAY-20261020 п.2 (card default)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_seed() -> int:
    return secrets.randbelow(2**31 - 1) + 1


def last_engine_template(conn: sqlite3.Connection) -> str | None:
    """The model the engine holds now, as far as the studio knows: the template of the task it
    submitted last (§8.4: a switch costs 16–38 s and the plan shows it)."""
    row = conn.execute("SELECT payload_json FROM task WHERE engine_submitted_at IS NOT NULL "
                       "ORDER BY engine_submitted_at DESC LIMIT 1").fetchone()
    return json.loads(row["payload_json"]).get("template") if row else None


def template_before_new_image(conn: sqlite3.Connection) -> str | None:
    """The model the engine holds when a picture queued NOW starts (§8.4, one `queue_key` with the
    worker): the last of the tasks that run before it — the running one, then the queued pictures
    in the order they were queued; a render never goes before a picture. A switch is paid by the
    first task that causes it, so a plan behind a queued picture on the same model pays nothing."""
    marks = ", ".join("?" for _ in tasks.ON_ENGINE)
    active = conn.execute(f"SELECT payload_json FROM task WHERE status IN ({marks}) "
                          f"ORDER BY rowid DESC LIMIT 1", tasks.ON_ENGINE).fetchone()
    loaded = (json.loads(active["payload_json"]).get("template") if active
              else last_engine_template(conn))
    ahead = tasks.queued_in_order(conn, kinds=("image",), loaded_template=loaded)
    if ahead:
        return json.loads(ahead[-1]["payload_json"]).get("template")
    return loaded


def switches(conn: sqlite3.Connection, order: list[str], *, before: str | None = None
             ) -> list[dict]:
    out = []
    if before is None:
        before = last_engine_template(conn)
    for template in order:
        cost = estimates.switch_cost(before, template)
        if cost:
            out.append({"from": before, "to": template, "seconds": cost})
        before = template
    return out


def _save_plan(conn, project_id: str, *, tier: str | None, items: list[dict],
               versions: dict) -> tuple[str, str]:
    plan_id = ids.new_id("pl")
    expires = (_now() + PLAN_TTL).isoformat(timespec="milliseconds")
    with db.tx(conn):
        conn.execute("INSERT INTO render_plan (id, project_id, tier, items_json, versions_json, "
                     "expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (plan_id, project_id, tier, json.dumps(items, ensure_ascii=False),
                      json.dumps(versions), expires))
    return plan_id, expires


def load_plan(conn: sqlite3.Connection, project_id: str, plan_id: str, kind: str) -> dict:
    """Inside the confirming transaction: the plan, refused if unknown, of another kind,
    expired or already confirmed."""
    row = conn.execute("SELECT * FROM render_plan WHERE id = ? AND project_id = ?",
                       (plan_id, project_id)).fetchone()
    if row is None:
        raise StudioError(404, "plan_not_found", "План не найден", {"plan_id": plan_id})
    items = json.loads(row["items_json"])
    if not items or items[0]["kind"] != kind:
        raise StudioError(400, "plan_kind_mismatch", "Этот план для другой операции",
                          {"plan_id": plan_id})
    if row["used_at"] is not None:
        raise StudioError(409, "plan_used", "Этот план уже подтверждён",
                          {"plan_id": plan_id, "used_at": row["used_at"]})
    if datetime.fromisoformat(row["expires_at"]) < _now():
        raise StudioError(409, "plan_expired", "План устарел (живёт 10 минут) — постройте заново",
                          {"plan_id": plan_id, "expires_at": row["expires_at"]})
    conn.execute("UPDATE render_plan SET used_at = ? WHERE id = ?", (ids.now_iso(), plan_id))
    return {"items": items, "versions": json.loads(row["versions_json"]), "tier": row["tier"]}


# -- O10 kind=image ----------------------------------------------------------------------------

def plan_image(conn: sqlite3.Connection, project_id: str, *, scene_id: str | None,
               prompt: str | None, width: int | None, height: int | None) -> dict:
    """The keyframe of a scene (prompt from `keyframe_prompt`, size by the frame format) or a
    free one (prompt and size given)."""
    project = conn.execute("SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
    if scene_id is not None and (prompt is not None or width is not None or height is not None):
        raise StudioError(400, "args_invalid", "Сцена и свой промпт с размером — взаимоисключающие")
    if scene_id is not None:
        scene = conn.execute("SELECT * FROM scene WHERE id = ? AND project_id = ? "
                             "AND deleted_at IS NULL", (scene_id, project_id)).fetchone()
        if scene is None:
            raise StudioError(404, "scene_not_found", "Сцена не найдена", {"scene_id": scene_id})
        phrases = [r["portrait_phrase"] for r in conn.execute(
            "SELECT c.portrait_phrase FROM scene_ref r JOIN cast_member c ON c.id = r.cast_id "
            "WHERE r.scene_id = ? ORDER BY r.ord", (scene_id,))]
        prompt = keyframe_prompt.keyframe_prompt(
            style_block=project["style_block"], portrait_phrases=phrases,
            state_in=scene["state_in_text"], action_text=scene["action_text"])
        width, height = keyframe_prompt.keyframe_size(project["frame_format"])
    elif not prompt or width is None or height is None:
        raise StudioError(400, "args_invalid", "Нужна сцена или промпт с размером кадра")
    limits = templates.load(templates.KEYFRAME_TEMPLATE).card["inputs"]
    for name, value in (("width", width), ("height", height)):
        spec = limits[name]
        if not (spec["min"] <= value <= spec["max"]) or value % spec["multiple_of"]:
            raise StudioError(400, "size_invalid",
                              f"Сторона кадра {value}: нужно {spec['min']}–{spec['max']}, "
                              f"кратно {spec['multiple_of']}", {name: value})
    estimate = estimates.estimate(conn, executor=EXECUTOR, template=templates.KEYFRAME_TEMPLATE,
                                  machine=settings.get(conn, "machine"), width=width,
                                  height=height, frames=0, steps=IMAGE_STEPS)
    item = {"kind": "image", "scene_id": scene_id, "template": templates.KEYFRAME_TEMPLATE,
            "template_sha256": templates.load(templates.KEYFRAME_TEMPLATE).sha256,
            "prompt": prompt, "width": width, "height": height, "seed": new_seed(),
            "steps": IMAGE_STEPS, "estimate_s": estimate["seconds"],
            "estimate_source": estimate["source"], "status": "ready", "reasons": []}
    switch = switches(conn, [templates.KEYFRAME_TEMPLATE],
                       before=template_before_new_image(conn))
    # a scene's keyframe is planned from its text and the project's cast and style: remember both
    # versions, O12 refuses a plan the owner has edited under (review of task 8, R4)
    versions = ({"scene_id": scene_id, "edit_version": scene["edit_version"],
                 "project_revision": project["revision"]} if scene_id is not None else {})
    plan_id, expires = _save_plan(conn, project_id, tier=None, items=[item], versions=versions)
    return {"plan_id": plan_id, "expires_at": expires, "kind": "image", "items": [item],
            "total_s": round(item["estimate_s"] + sum(s["seconds"] for s in switch), 1),
            "checkpoint_switches": switch, "lint": {}, "blocked": [], "invalid": []}


# -- O12 ---------------------------------------------------------------------------------------

def enqueue_image(conn: sqlite3.Connection, project_id: str, plan_id: str, key: str) -> dict:
    """Inside `idempotency.run_once`'s transaction: one task, `queued`, with the pictures'
    priority (§8.4: a picture waits only for the running task, not for the render queue). The
    payload is flat — S2b's batches write the same keys at the root and add their own (§19.3)."""
    if conn.execute("SELECT 1 FROM task WHERE idempotency_key = ?", (key,)).fetchone():
        raise StudioError(409, "idempotency_key_reused",
                          "Этот ключ уже поставил задачу (запись о нём устарела)", {"key": key})
    plan = load_plan(conn, project_id, plan_id, "image")
    item = plan["items"][0]
    planned = plan["versions"]
    if planned:
        scene = conn.execute("SELECT edit_version FROM scene WHERE id = ? AND deleted_at IS NULL",
                             (planned["scene_id"],)).fetchone()
        revision = conn.execute("SELECT revision FROM project WHERE id = ?",
                                (project_id,)).fetchone()["revision"]
        current = {"edit_version": scene["edit_version"] if scene else None,
                   "project_revision": revision}
        if current != {k: planned[k] for k in current}:
            raise StudioError(409, "plan_stale", "Сцена или проект изменились после плана — "
                              "постройте план заново",
                              {"scene_id": planned["scene_id"],
                               "planned": {k: planned[k] for k in current}, "current": current})
    task_id = ids.new_id("k")
    now = ids.now_iso()
    payload = {k: item[k] for k in ("template", "template_sha256", "prompt", "width", "height",
                                    "seed", "steps", "scene_id")}
    conn.execute(
        "INSERT INTO task (id, kind, executor, gpu_scope, payload_json, idempotency_key, status, "
        "priority, project_id, created_at, updated_at) "
        "VALUES (?, 'image', ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
        (task_id, EXECUTOR, settings.get(conn, "comfy.scope"),
         json.dumps(payload, ensure_ascii=False), key, tasks.IMAGE_PRIORITY, project_id, now, now))
    events.emit(conn, "task_queued", project_id=project_id, task_id=task_id,
                payload={"kind": "image", "plan_id": plan_id})
    return {"task_id": task_id, "status": "queued"}
