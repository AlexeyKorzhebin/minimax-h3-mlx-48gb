"""A llama-server that loads nothing, shared by the provider tests and the chat-route tests.

It lives in its own module rather than in `tests/test_provider.py` because two test files need
it now: importing a fixture out of another *test* module drags that module's whole collection
into the importer's run, and pytest would have to import `test_provider.py` twice under two
names. A plain helper module has neither problem -- `tests/` is on `sys.path` for the same
reason `test_web.py` can write `from test_queue import _external_lock`.

Not named `conftest.py` on purpose: a fixture that appears out of thin air is harder to follow
than one imported by name, and this is a class, not a fixture.
"""
import http.server
import json
import threading


class _FakeLlama:
    """llama-server, который ничего не грузит: /health 200 и захардкоженный чат-ответ.

    Поток обрывается close(); порт выдаёт ядро (port=0), чтобы тесты не дрались.
    """

    def __init__(self, chat_payload=None, health: int = 200):
        handler_cls = self._make_handler(chat_payload, health)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.httpd.server_address[1]
        self.requests: list[dict] = []
        handler_cls.seen = self.requests
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def _make_handler(self, chat_payload, health):
        class Handler(http.server.BaseHTTPRequestHandler):
            seen: list = []

            def log_message(self, *a):  # тишина в выводе pytest
                pass

            def do_GET(self):
                code = health if self.path == "/health" else 404
                self.send_response(code); self.end_headers()

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                type(self).seen.append({"path": self.path, "body": body,
                                         "headers": dict(self.headers)})
                out = json.dumps(chat_payload or {}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
        return Handler

    def close(self):
        self.httpd.shutdown(); self.httpd.server_close()


#: One well-formed turn, the shape `provider.PROMPT_SCHEMA` fixes.
_TURN = {"choices": [{"message": {"content": json.dumps({
    "reply": "Сделал мрачнее.",
    "prompt": {"instruction": None,
               "integrated_multimodal_description": "[Shot 1] Live-action…",
               "overall_soundscape": "Wind.",
               "non_diegetic_music": "N/A"}})}}]}
