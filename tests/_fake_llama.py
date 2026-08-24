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
import time


class _FakeLlama:
    """llama-server, который ничего не грузит: /health 200 и захардкоженный чат-ответ.

    Поток обрывается close(); порт выдаёт ядро (port=0), чтобы тесты не дрались.

    `delay` — сколько секунд «думать» перед ответом. Настоящий ход занимает десятки секунд, и
    ровно в это окно приходит второй запрос той же сессии; без задержки гонку не поймать —
    первый ход успевает закончиться раньше, чем второй начнётся.

    `models_payload`/`models_status` (Task 2, "выбор провайдера для сценария"): `GET /v1/models`,
    the cheap probe's own request. `models_payload=None` (the default) answers 404, exactly the
    old "anything but /health is 404" behaviour -- a test that never passes it sees no change at
    all. Passing a dict answers `models_status` (200 by default) with that dict as the JSON body,
    so a test can shape both the success case (`{"data": [...]}}`) and the "answered, but not a
    models list" one (any other shape, still 200) without a second mock class.

    `models_raw`: the one shape a JSON-encodable `dict` cannot make -- a 200 whose body is not
    JSON at all (a provider or a proxy in front of one answering with an HTML error page, say).
    Takes priority over `models_payload` when both are given; either raw `bytes` or `str`
    (encoded utf-8 here for convenience).

    Every `/v1/models` request is recorded into `seen`/`requests` the same way a POST chat turn
    is -- `/health` is polled every 0.2s by
    `port_alive`/`ensure_up` and deliberately stays unlogged, or every existing `(req,) =
    fake.requests`/`fake.requests == []` assertion in the other provider/chat tests would break
    the moment this class started being used anywhere near a running local provider.

    `stream_chunks` (task: SSE streaming): when the incoming chat POST body carries `"stream":
    true` *and* this is not `None`, the response is `text/event-stream` instead of one JSON body
    -- each item is written out in order, flushed individually so a test can assert on partial
    delivery. A `dict` item is wrapped as one `data: <json>\n\n` frame (the ordinary case: a
    chunk shaped like `{"choices": [{"delta": {...}, "finish_reason": ...}]}`); a `str` item is
    written verbatim, for the cases a dict cannot express -- a malformed non-JSON `data:` line, a
    comment line, or the `"data: [DONE]\n\n"` terminator itself. Deliberately no `Content-Length`
    and no chunked-transfer-encoding: this handler answers HTTP/1.0 (the class default,
    unchanged), so the socket simply closes once every item has been written, and a
    `stream_chunks` list with no `[DONE]` frame in it is exactly how a test simulates a connection
    that closed mid-stream. `chat_payload` and `health`/`delay` behave unchanged when the request
    is not a stream request, or `stream_chunks` is `None` -- existing callers that never pass it
    see no change at all.
    """

    def __init__(self, chat_payload=None, health: int = 200, delay: float = 0.0,
                 models_payload=None, models_status: int = 200, models_raw=None,
                 stream_chunks=None):
        handler_cls = self._make_handler(chat_payload, health, delay, models_payload,
                                         models_status, models_raw, stream_chunks)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.httpd.server_address[1]
        self.requests: list[dict] = []
        handler_cls.seen = self.requests
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def _make_handler(self, chat_payload, health, delay=0.0, models_payload=None,
                      models_status=200, models_raw=None, stream_chunks=None):
        class Handler(http.server.BaseHTTPRequestHandler):
            seen: list = []

            def log_message(self, *a):  # тишина в выводе pytest
                pass

            def do_GET(self):
                if self.path == "/health":
                    self.send_response(health); self.end_headers()
                    return
                if self.path == "/v1/models":
                    type(self).seen.append({"path": self.path, "headers": dict(self.headers)})
                    if models_raw is None and models_payload is None:
                        self.send_response(404); self.end_headers()
                        return
                    if models_raw is not None:
                        out = (models_raw.encode() if isinstance(models_raw, str) else models_raw)
                    else:
                        out = json.dumps(models_payload).encode()
                    self.send_response(models_status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
                self.send_response(404); self.end_headers()

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                type(self).seen.append({"path": self.path, "body": body,
                                         "headers": dict(self.headers)})
                if delay:
                    time.sleep(delay)
                if body.get("stream") and stream_chunks is not None:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for item in stream_chunks:
                        frame = item if isinstance(item, str) else f"data: {json.dumps(item)}\n\n"
                        self.wfile.write(frame.encode())
                        self.wfile.flush()
                    return
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
