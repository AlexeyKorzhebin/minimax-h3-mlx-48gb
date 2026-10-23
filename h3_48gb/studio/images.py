"""The keyframe task (`task.kind = 'image'`, Krea 2, spec §7 O12, §9.1) for the worker: what the
executor gets, and how a finished picture becomes an asset."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from PIL import Image

# `as asset_files`: `finish(…, files: list[Path], …)` would shadow the module (revision 4.1)
from h3_48gb.studio import estimates, ids, templates
from h3_48gb.studio import files as asset_files
from h3_48gb.studio.executors.base import TaskSpec


class VerifyError(Exception):
    """The engine said done, but the file is not what was asked for: the task fails (§8.5)."""


class ImageKind:
    kind = "image"

    def __init__(self, home: Path, *, machine: str):
        self.home = Path(home)
        self.machine = machine

    def prepare(self, conn: sqlite3.Connection) -> None:
        """Nothing waits on a keyframe."""

    def spec(self, conn: sqlite3.Connection, task: sqlite3.Row) -> TaskSpec:
        """The payload is flat (spec §19.3). The values are the template card's inputs found in it
        and not null (revision 4, §14 seam 3): the keyframe gives prompt, seed, steps, width,
        height as before; S2b's batches add cfg, negative, lora without a change here, and a
        `lora: null` never reaches the substituter (no LoRA node, `templates.build_payload`)."""
        payload = json.loads(task["payload_json"])
        inputs = templates.load(payload["template"]).card["inputs"]
        return TaskSpec(task_id=task["id"], template=payload["template"],
                        template_sha256=payload.get("template_sha256"),
                        values={k: payload[k] for k in inputs
                                if k != "task_id" and payload.get(k) is not None},
                        dest_dir=self.home / "projects" / task["project_id"] / "keyframes"
                        / task["id"])

    def on_running(self, conn, task) -> None:
        pass

    def on_requeued(self, conn, task) -> None:
        pass

    def on_failed(self, conn, task) -> None:
        pass

    def finish(self, conn: sqlite3.Connection, task: sqlite3.Row, files: list[Path],
               spec: TaskSpec, wall_s: float) -> dict:
        """Inside the worker's transaction. One PNG of exactly the planned size."""
        pngs = [f for f in files if f.suffix.lower() == ".png"]
        if len(pngs) != 1:
            raise VerifyError(f"ждали один PNG, пришло: {', '.join(f.name for f in files)}")
        path = pngs[0]
        try:
            with Image.open(path) as image:
                image.load()
                size = image.size
        except Exception:                      # noqa: BLE001 -- Pillow raises many types
            raise VerifyError(f"кадр не читается: {path.name}") from None
        want = (spec.values["width"], spec.values["height"])
        if size != want:
            raise VerifyError(f"кадр {size[0]}×{size[1]} вместо {want[0]}×{want[1]}")
        data = path.read_bytes()
        asset_id = ids.new_id("a")
        conn.execute(
            "INSERT INTO asset (id, kind, path, sha256, bytes, mime, origin_kind, origin_task_id, "
            "created_at) VALUES (?, 'image', ?, ?, ?, 'image/png', 'take_output', ?, ?)",
            (asset_id, asset_files.stored_path(self.home, path),
             hashlib.sha256(data).hexdigest(), len(data), task["id"], ids.now_iso()))
        conn.execute("UPDATE task SET result_asset_id = ? WHERE id = ?", (asset_id, task["id"]))
        estimates.record(conn, executor=task["executor"], template=spec.template,
                         machine=self.machine, width=want[0], height=want[1], frames=0,
                         steps=spec.values["steps"], wall_s=wall_s)
        return {"kind": "image", "asset_id": asset_id}
