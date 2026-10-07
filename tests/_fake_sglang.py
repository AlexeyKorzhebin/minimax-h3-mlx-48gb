"""A stand-in for sglang's `/v1/videos` API, shaped after `video_api.py` on alex-neuro: POST
answers `{"id", "status": "queued"}`; GET answers `queued`/`completed`/`failed` (no `running`
exists there); an unknown id is 404 `{"detail": "Video not found"}`; a 4xx body is
`{"detail": "..."}`; DELETE only forgets the record (it never stops the GPU)."""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeSglang:
    def __init__(self, *, statuses=("completed",), content=b"MP4-BYTES", post_status=200,
                 post_body=None, error=None, drop_gets=0, truncate_contents=0, get_error=None,
                 keep_alive=False):
        self.posts: list[dict] = []
        self.gets: list[str] = []
        self.deletes: list[str] = []
        self.contents: list[str] = []
        self.ids: list[str] = []
        self.forget_all = False
        self._statuses = list(statuses)
        self.content = content
        self.post_status = post_status
        self.post_body = post_body
        self.error = error
        self.drop_gets = drop_gets
        self.get_error = get_error          # (status, body) answered to every status GET
        self.truncate_contents = truncate_contents
        # uvicorn (the real sglang) answers HTTP/1.1 and keeps the connection open; the stdlib
        # default HTTP/1.0 closes it, which hides a client that never closes its own end.
        self.keep_alive = keep_alive
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _next_status(self) -> str:
        return self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]

    def _handler(fake):  # noqa: N805 -- closes over the fake, not a method of the handler
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1" if fake.keep_alive else "HTTP/1.0"

            def log_message(self, *args):
                pass

            def _send_json(self, status, body):
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                fake.posts.append(json.loads(self.rfile.read(length)))
                if fake.post_status != 200:
                    return self._send_json(fake.post_status, fake.post_body or {"detail": "bad"})
                video_id = f"vid-{len(fake.posts)}"
                fake.ids.append(video_id)
                self._send_json(200, {"id": video_id, "object": "video", "status": "queued",
                                      "progress": 0})

            def do_GET(self):
                parts = self.path.strip("/").split("/")
                video_id = parts[2]
                if fake.forget_all or video_id not in fake.ids:
                    return self._send_json(404, {"detail": "Video not found"})
                if len(parts) == 4 and parts[3] == "content":
                    fake.contents.append(video_id)
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    if fake.truncate_contents > 0:
                        fake.truncate_contents -= 1
                        self.send_header("Content-Length", str(len(fake.content) + 100))
                        self.end_headers()
                        self.wfile.write(fake.content[:3])
                        self.wfile.flush()
                        self.close_connection = True
                        # Cut the body short with a clean FIN, then wait for the client to
                        # hang up: closing at once races the client's read (an RST can swallow
                        # the bytes already sent and leave it waiting for its timeout).
                        self.connection.shutdown(socket.SHUT_WR)
                        self.connection.settimeout(5)
                        try:
                            self.connection.recv(1)
                        except OSError:
                            pass
                        return
                    self.send_header("Content-Length", str(len(fake.content)))
                    self.end_headers()
                    self.wfile.write(fake.content)
                    return
                fake.gets.append(video_id)
                if fake.get_error:
                    return self._send_json(*fake.get_error)
                if fake.drop_gets > 0:
                    fake.drop_gets -= 1
                    self.close_connection = True
                    return
                status = fake._next_status()
                body = {"id": video_id, "status": status,
                        "progress": 100 if status == "completed" else 0}
                if status == "completed":
                    body.update({"inference_time_s": 300.5, "peak_memory_mb": 47000.0})
                if status == "failed":
                    body["error"] = fake.error or {"message": "boom"}
                self._send_json(200, body)

            def do_DELETE(self):
                video_id = self.path.strip("/").split("/")[2]
                fake.deletes.append(video_id)
                self._send_json(200, {"id": video_id, "status": "deleted"})
        return Handler
