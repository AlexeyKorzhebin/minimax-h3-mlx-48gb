"""A studio on fakes for the S2–S3 tests (spec §12.1): the API (TestClient), the real worker with
the real ComfyUI adapter, a FakeComfyStudio and a FakeDispatcher; polls shortened, nothing else
replaced. Imported by name, like `_studio_fixtures`."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from _fake_comfy_studio import FakeComfyStudio
from _fake_dispatcher import FakeDispatcher
from _studio_fixtures import BASE_URL, ScriptedLlm
from fastapi.testclient import TestClient
from h3_48gb.engines.dispatcher_client import DispatcherClient
from h3_48gb.studio import config, db, settings
from h3_48gb.studio.app import create_app
from h3_48gb.studio.executors.comfy import ComfyExecutor
from h3_48gb.studio.gpu import DispatcherGate
from h3_48gb.studio.images import ImageKind
from h3_48gb.studio.worker import Worker

READY = {"ok": True, "state": "ready", "engine": "comfy-studio"}


class Rig:
    def __init__(self, tmp_path: Path, *, acquire=(READY,), comfy_kwargs=None, kinds=None,
                 own=None):
        self.outdir = tmp_path / "out"
        self.comfy = FakeComfyStudio(tmp_path / "comfy-out", **(comfy_kwargs or {}))
        self.dispatcher = FakeDispatcher(acquire=acquire, own=own)
        self.client = TestClient(
            create_app(self.outdir, llm=ScriptedLlm([]),
                       dispatcher_factory=lambda url, client: DispatcherClient(
                           self.dispatcher.url, client=client, timeout=5)),
            base_url=BASE_URL)
        self.home = config.studio_dir(self.outdir)
        self.db_path = self.home / "studio.sqlite"
        self.conn = db.connect(self.db_path)
        settings.put(self.conn, "comfy.url", self.comfy.url)
        settings.put(self.conn, "comfy.output_dir", str(self.comfy.output_dir))
        settings.put(self.conn, "dispatcher.url", self.dispatcher.url)
        self.sleeps: list[float] = []
        dclient = DispatcherClient(self.dispatcher.url, client="studio-worker", timeout=5)
        self.executor = ComfyExecutor(url=self.comfy.url, output_dir=self.comfy.output_dir,
                                      scope="dispatcher:comfy-studio", dispatcher=dclient)
        self.gate = DispatcherGate(dclient, "comfy-studio")
        self.worker = Worker(self.db_path, executor=self.executor, gate=self.gate,
                             kinds=kinds(self) if kinds else
                             {"image": ImageKind(self.home, machine="alex-neuro")},
                             poll_s=0.01, probe_s=0.01, cancel_wait_s=2.0,
                             sleep=self._sleep)

    def _sleep(self, seconds):
        import time
        self.sleeps.append(seconds)
        time.sleep(min(seconds, 0.01))

    def close(self):
        self.comfy.close()
        self.dispatcher.close()
        self.conn.close()
        self.worker.conn.close()

    # -- API helpers ------------------------------------------------------------------------
    def project(self, frame_format="16:9") -> dict:
        r = self.client.post("/api/studio/projects", json={"title": "Лес",
                                                           "frame_format": frame_format})
        assert r.status_code == 201, r.text
        return r.json()

    def post(self, path, body, key=None, expect=200):
        headers = {"Idempotency-Key": key or str(uuid.uuid4())} if key is not False else {}
        r = self.client.post(f"/api/studio{path}", json=body, headers=headers)
        assert r.status_code == expect, (r.status_code, r.text)
        return r.json()

    def events(self, *kinds) -> list[tuple[str, dict]]:
        rows = self.conn.execute("SELECT kind, payload_json FROM event ORDER BY seq").fetchall()
        return [(r["kind"], json.loads(r["payload_json"])) for r in rows
                if not kinds or r["kind"] in kinds]

    def task(self, task_id) -> dict:
        return self.client.get(f"/api/studio/tasks/{task_id}").json()

    def drain(self, limit=20) -> int:
        n = 0
        while n < limit and self.worker.run_once():
            n += 1
        return n
