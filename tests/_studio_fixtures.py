"""Helpers for the studio tests, imported by name (not a conftest: a fixture out of thin air is
harder to follow). `LiveServer` runs the real app under uvicorn on a kernel-chosen port — the SSE
stream is tested over a socket, the way the browser reads it, not through an in-process client."""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from pathlib import Path

import uvicorn
from fastapi.testclient import TestClient

from h3_48gb.studio.app import create_app



#: Requests go to an allowed Host (the app refuses others, R26).
BASE_URL = "http://127.0.0.1:8766"


def make_client(tmp_path: Path, **kwargs) -> TestClient:
    return TestClient(create_app(tmp_path / "out", **kwargs), base_url=BASE_URL)


class LiveServer:
    def __init__(self, app):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
                                                    log_level="warning",
                                                    timeout_graceful_shutdown=1))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.port = 0

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start in 10 s")
            time.sleep(0.02)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)


def read_sse(port: int, path: str, *, headers: dict | None = None, count: int,
             timeout: float = 3.0) -> list[dict]:
    """Up to `count` events from an SSE stream, or fewer if `timeout` passes first. Each event is
    `{"id": int, "event": str, "data": dict}`; comment lines (`: ping`) are skipped."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    assert resp.status == 200, resp.status
    got: list[dict] = []
    current: dict = {}
    deadline = time.monotonic() + timeout
    try:
        while len(got) < count and time.monotonic() < deadline:
            try:
                line = resp.fp.readline().decode("utf-8")
            except (socket.timeout, TimeoutError):
                break
            if not line:
                break
            line = line.rstrip("\n")
            if line.startswith(":"):
                continue
            if line == "":
                if current:
                    got.append(current)
                    current = {}
                continue
            name, _, value = line.partition(": ")
            if name == "id":
                current["id"] = int(value)
            elif name == "event":
                current["event"] = value
            elif name == "data":
                current["data"] = json.loads(value)
    finally:
        conn.close()
    return got
