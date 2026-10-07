"""A stand-in for ComfyUI's /upload/image, /prompt and /history (spec §6), writing PNG frames
where a SaveImage node would -- `<output>/<filename_prefix>_NNNNN_.png`."""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw


class FakeComfy:
    def __init__(self, output_dir: Path, *, frames=(25,), history_pending=1, fail=False):
        self.output_dir = Path(output_dir)
        self.uploads: list[tuple[str, bytes]] = []
        self.prompts: list[dict] = []
        self.history_calls: list[str] = []
        self._frames = list(frames)
        self.history_pending = history_pending
        self.fail = fail
        self._pending: dict[str, int] = {}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _write_frames(self, prefix: str) -> None:
        count = self._frames.pop(0) if len(self._frames) > 1 else self._frames[0]
        directory = self.output_dir / Path(prefix).parent
        directory.mkdir(parents=True, exist_ok=True)
        stem = Path(prefix).name
        for index in range(1, count + 1):
            Image.new("RGB", (128, 128), (index % 255, 40, 90)).save(
                directory / f"{stem}_{index:05d}_.png")

    def _handler(fake):  # noqa: N805
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body):
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/upload/image":
                    name = re.search(rb'filename="([^"]+)"', body).group(1).decode()
                    fake.uploads.append((name, body))
                    return self._send(200, {"name": name, "subfolder": "", "type": "input"})
                if self.path == "/prompt":
                    workflow = json.loads(body)["prompt"]
                    fake.prompts.append(workflow)
                    prompt_id = f"p{len(fake.prompts)}"
                    fake._pending[prompt_id] = fake.history_pending
                    if not fake.fail:
                        fake._write_frames(workflow["42"]["inputs"]["filename_prefix"])
                    return self._send(200, {"prompt_id": prompt_id, "number": len(fake.prompts),
                                            "node_errors": {}})
                self._send(404, {})

            def do_GET(self):
                prompt_id = self.path.rsplit("/", 1)[-1]
                fake.history_calls.append(prompt_id)
                if fake._pending.get(prompt_id, 0) > 0:
                    fake._pending[prompt_id] -= 1
                    return self._send(200, {})
                status = ({"status_str": "error", "completed": False,
                           "messages": [["execution_error", {"exception_message": "OOM"}]]}
                          if fake.fail else {"status_str": "success", "completed": True,
                                             "messages": []})
                self._send(200, {prompt_id: {"status": status, "outputs": {}}})
        return Handler
