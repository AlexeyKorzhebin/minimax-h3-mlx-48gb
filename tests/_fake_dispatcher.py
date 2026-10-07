"""A stand-in for tools/gpu-dispatcher's HTTP API (task 7's contract), scripted per test."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeDispatcher:
    def __init__(self, *, acquire=({"ok": True, "state": "ready", "engine": "h3"},),
                 temps=(44,), own=None, restore_status=200, gpu_null=False,
                 on_release=None):
        self.headers: list[dict] = []
        self.calls: list[tuple[str, str, dict]] = []
        self._acquire = list(acquire)
        self._temps = list(temps)
        self.own = own if own is not None else {}
        self.restore_status = restore_status
        self.gpu_null = gpu_null
        self.on_release = on_release
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @staticmethod
    def _next(items):
        return items.pop(0) if len(items) > 1 else items[0]

    def status_body(self) -> dict:
        if self.gpu_null:   # nvidia-smi is down on the host (dispatcher: gpu null + gpu_error)
            gpu = None
        else:
            gpu = {"temperature_c": self._next(self._temps), "memory_used_mb": 100,
                   "memory_total_mb": 65536}
        return {"ok": True, "own": self.own, "foreign": [], "gpu": gpu,
                **({"gpu_error": "nvidia-smi недоступен"} if self.gpu_null else {}),
                "qwen": {"running": False, "unloaded_by_us": False},
                "lock": {"held_by_us": bool(self.own), "path": "/x/generation.lock"},
                "server_outputs_bytes": 0}

    def _handler(fake):  # noqa: N805
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body):
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                fake.calls.append(("GET", self.path, {}))
                fake.headers.append(dict(self.headers))
                self._send(200, fake.status_body())

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                fake.calls.append(("POST", self.path, body))
                fake.headers.append(dict(self.headers))
                if self.path == "/acquire":
                    return self._send(200, fake._next(fake._acquire))
                if self.path == "/release":
                    if fake.on_release:
                        fake.on_release()
                    return self._send(200, {"ok": True, "stopped": ["h3"]})
                if self.path == "/qwen/unload":
                    return self._send(200, {"ok": True, "was_running": True, "exit_code": 0})
                if self.path == "/qwen/restore":
                    if fake.restore_status == 409:
                        return self._send(409, {"ok": False, "error": {
                            "code": "qwen_was_not_running",
                            "message": "Qwen не был запущен до выгрузки"}})
                    if fake.restore_status == 500:
                        return self._send(500, {"ok": False, "error": {
                            "code": "weird", "message": "что-то своё"}})
                    if fake.restore_status == "already_running":
                        return self._send(200, {"ok": True, "state": "already_running"})
                    return self._send(200, {"ok": True, "state": "starting"})
                self._send(404, {"ok": False})
        return Handler
