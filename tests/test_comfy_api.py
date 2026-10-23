"""The shared ComfyUI client (studio spec §8.2): the studio's upload and prompt on the wire."""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import time
from urllib.parse import urlsplit

import pytest

from _fake_comfy_studio import FakeComfyStudio
from h3_48gb.engines.comfy_api import ComfyApi, ComfyError


@pytest.fixture
def fake(tmp_path):
    comfy = FakeComfyStudio(tmp_path / "out")
    yield comfy
    comfy.close()


def test_upload_image_sends_the_bytes_under_the_given_name(fake):
    api = ComfyApi(fake.url, boundary="B")
    assert api.upload_image(b"\x89PNG-bytes", "k_1-1.png") == "k_1-1.png"
    assert fake.uploads == {"k_1-1.png": b"\x89PNG-bytes"}


def test_submit_prompt_sends_the_whole_payload(fake):
    api = ComfyApi(fake.url)
    graph = {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}}}
    fake.hold = True
    prompt_id = api.submit_prompt({"prompt": graph, "client_id": "k_1",
                                   "extra_data": {"studio_task_id": "k_1"}})
    [job] = fake.prompts
    assert (job["prompt_id"], job["prompt"], job["client_id"], job["extra_data"]) == (
        prompt_id, graph, "k_1", {"client_id": "k_1", "studio_task_id": "k_1"})
    api.delete_queued(["nothing"])
    assert fake.deleted == ["nothing"]


def test_a_refused_graph_and_a_dead_engine_are_comfy_errors(fake):
    api = ComfyApi(fake.url)
    with pytest.raises(ComfyError, match=r"ComfyUI POST /prompt: HTTP 400: .*unknown \['Nope'\]"):
        api.submit_prompt({"prompt": {"1": {"class_type": "Nope", "inputs": {}}},
                           "client_id": "k"})
    with pytest.raises(ComfyError, match="ComfyUI GET /queue недоступен"):
        ComfyApi("http://127.0.0.1:9", timeout=1).queue()


def test_models_and_object_info(fake):
    api = ComfyApi(fake.url)
    assert api.models("vae") == ["minimax_h3_audio_vae_fp32.safetensors",
                                 "minimax_h3_video_vae_fp16.safetensors",
                                 "qwen_image_vae.safetensors"]
    assert "MiniMaxH3ImageToVideo" in api.object_info()
    assert api.system_stats()["system"]["comfyui_version"] == "0.42.0"


def test_the_ltx_client_keeps_its_wire_and_its_error(fake, tmp_path):
    """R-S2-2: the old panel's client on the shared base — the same `client_id`, the same
    exception type its callers catch (`ltx.run_upscale` catches UpscaleError)."""
    from h3_48gb.engines import ltx
    client = ltx.ComfyClient(fake.url)
    fake.hold = True
    graph = {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}}}
    prompt_id = client.prompt(graph)
    [job] = fake.prompts
    assert (job["prompt_id"], job["client_id"], job["extra_data"]) == (
        prompt_id, "h3-panel", {"client_id": "h3-panel"})
    with pytest.raises(ltx.UpscaleError, match="ComfyUI GET /history/x недоступен"):
        ltx.ComfyClient("http://127.0.0.1:9", timeout=1).history("x")


def test_view_returns_an_uploaded_input(fake):
    api = ComfyApi(fake.url)
    api.upload_image(b"bytes-of-a-frame", "k_2-1.png")
    assert api.view("k_2-1.png") == b"bytes-of-a-frame"
    with pytest.raises(ComfyError, match="ComfyUI GET /view nope.png недоступен"):
        api.view("nope.png")


# -- the wire, byte for byte ---------------------------------------------------------------
SAVE = {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}}}


def _wait_running(fake, timeout=5.0):
    deadline = time.monotonic() + timeout
    while fake._running is None:
        assert time.monotonic() < deadline, "the fake never started the prompt"
        time.sleep(0.005)


def test_upload_image_wire_is_exact(fake):
    api = ComfyApi(fake.url, timeout=5, boundary="B")
    api.upload_image(b"PNGDATA", "k_1-1.png")
    api.upload_image(b"JPGDATA", "f.jpg")
    assert fake.upload_bodies[0] == (
        b'--B\r\nContent-Disposition: form-data; name="image"; filename="k_1-1.png"\r\n'
        b"Content-Type: image/png\r\n\r\nPNGDATA\r\n"
        b'--B\r\nContent-Disposition: form-data; name="type"\r\n\r\ninput\r\n'
        b'--B\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n--B--\r\n')
    assert fake.upload_content_types == ["multipart/form-data; boundary=B"] * 2
    assert fake.upload_fields == {"k_1-1.png": {"type": "input", "overwrite": "true"},
                                  "f.jpg": {"type": "input", "overwrite": "true"}}
    assert fake.upload_mimes == {"k_1-1.png": "image/png", "f.jpg": "image/jpeg"}


def test_upload_image_returns_the_name_comfyui_answers_with():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        comfy = FakeComfyStudio(Path(tmp), rename_to="served (1).png")
        try:
            assert ComfyApi(comfy.url, timeout=5).upload_image(b"x", "a.png") == "served (1).png"
        finally:
            comfy.close()


@pytest.mark.parametrize("name", ['a"b.png', "a\rb.png", "a\nb.png", "a/b.png", "a\\b.png", ""])
def test_upload_image_refuses_a_name_that_breaks_the_form(fake, name):
    with pytest.raises(ComfyError, match="^ComfyUI: недопустимое имя файла для загрузки: "):
        ComfyApi(fake.url, timeout=5).upload_image(b"x", name)
    assert (fake.uploads, fake.upload_bodies) == ({}, [])


def test_history_quotes_the_prompt_id(fake):
    assert ComfyApi(fake.url, timeout=5).history("../queue?x=1#f") is None
    assert fake.requests == [("GET", "/history/..%2Fqueue%3Fx%3D1%23f")]


def test_view_sends_its_parameters_and_only_serves_the_input_folder(fake):
    api = ComfyApi(fake.url, timeout=5)
    api.upload_image(b"frame", "k.png")
    assert api.view("k.png") == b"frame"
    for kwargs in ({"folder_type": "output"}, {"subfolder": "sub"}):
        with pytest.raises(ComfyError, match="ComfyUI GET /view k.png недоступен"):
            api.view("k.png", **kwargs)
    assert fake.view_requests == [
        {"filename": "k.png", "type": "input", "subfolder": ""},
        {"filename": "k.png", "type": "output", "subfolder": ""},
        {"filename": "k.png", "type": "input", "subfolder": "sub"}]


def test_the_ltx_client_raises_its_own_error_from_view(fake):
    from h3_48gb.engines import ltx
    with pytest.raises(ltx.UpscaleError, match="ComfyUI GET /view nope.png недоступен"):
        ltx.ComfyClient(fake.url, timeout=5).view("nope.png")


def test_a_trailing_slash_in_the_base_url_is_dropped(fake):
    api = ComfyApi(fake.url + "/", timeout=5)
    assert api.base_url == fake.url
    assert api.system_stats()["system"]["comfyui_version"] == "0.42.0"
    assert fake.requests == [("GET", "/system_stats")]


# -- failures the client must name -------------------------------------------------------------
def test_a_200_without_a_prompt_id_is_a_refusal(fake):
    fake.no_prompt_id = True
    with pytest.raises(ComfyError, match=r'^ComfyUI отклонил воркфлоу: \{"node_errors": \{\}\}$'):
        ComfyApi(fake.url, timeout=5).submit_prompt({"prompt": SAVE, "client_id": "k"})
    assert fake.prompts == []


def test_a_200_that_is_not_json_is_a_comfy_error(fake):
    fake.non_json = True
    with pytest.raises(ComfyError, match="^ComfyUI POST /prompt недоступен: "):
        ComfyApi(fake.url, timeout=5).submit_prompt({"prompt": SAVE, "client_id": "k"})


def test_the_client_timeout_fires_on_a_late_answer_and_the_prompt_is_accepted_anyway(fake):
    fake.post_timeout = 1.5
    started = time.monotonic()
    with pytest.raises(ComfyError, match="^ComfyUI POST /prompt недоступен: "):
        ComfyApi(fake.url, timeout=0.3).submit_prompt({"prompt": SAVE, "client_id": "k"})
    assert time.monotonic() - started < 1.2
    assert [j["client_id"] for j in fake.prompts] == ["k"]


def test_history_has_no_entry_for_the_first_polls_of_a_finished_prompt(tmp_path):
    comfy = FakeComfyStudio(tmp_path / "out", history_pending=2)
    try:
        api = ComfyApi(comfy.url, timeout=5)
        pid = api.submit_prompt({"prompt": SAVE, "client_id": "k"})
        comfy.wait_idle(10)
        assert [api.history(pid) is None for _ in range(3)] == [True, True, False]
        assert api.history(pid)["status"]["status_str"] == "success"
    finally:
        comfy.close()


# -- interrupt ---------------------------------------------------------------------------------
def test_interrupt_accepts_the_empty_answer_comfyui_gives(fake):
    api = ComfyApi(fake.url, timeout=5)
    assert api.interrupt() is None
    assert api.interrupt("p-1") is None
    assert fake.interrupt_bodies == [{}, {"prompt_id": "p-1"}]


def test_a_targeted_interrupt_only_stops_the_prompt_that_runs(fake):
    api = ComfyApi(fake.url, timeout=5)
    fake.hold = True
    pid = api.submit_prompt({"prompt": SAVE, "client_id": "k"})
    _wait_running(fake)
    api.interrupt("someone-else")
    assert fake._interrupt is False
    api.interrupt(pid)
    fake.wait_idle(10)
    assert [m[0] for m in fake.history[pid]["status"]["messages"]] == ["execution_interrupted"]


def test_a_global_interrupt_stops_whatever_runs(fake):
    api = ComfyApi(fake.url, timeout=5)
    fake.hold = True
    pid = api.submit_prompt({"prompt": SAVE, "client_id": "k"})
    _wait_running(fake)
    api.interrupt()
    fake.wait_idle(10)
    assert [m[0] for m in fake.history[pid]["status"]["messages"]] == ["execution_interrupted"]


def test_the_ltx_client_keeps_the_global_interrupt(fake):
    from h3_48gb.engines import ltx
    ltx.ComfyClient(fake.url, timeout=5).interrupt()
    assert fake.interrupt_bodies == [{}]


# -- the fake behaves like ComfyUI where the adapter depends on it ------------------------------
def test_extra_data_takes_the_top_level_client_id_and_creates_none_without_one(fake):
    fake.hold = True
    api = ComfyApi(fake.url, timeout=5)
    api.submit_prompt({"prompt": SAVE, "client_id": "top", "extra_data": {"client_id": "own", "a": 1}})
    api.submit_prompt({"prompt": SAVE, "extra_data": {"a": 2}})
    api.submit_prompt({"prompt": SAVE})
    assert [j["extra_data"] for j in fake.prompts] == [
        {"client_id": "top", "a": 1}, {"a": 2}, {}]


def test_a_graph_comfyui_would_refuse_is_a_400_and_the_fake_stays_alive(fake):
    api = ComfyApi(fake.url, timeout=5)
    no_save = {"1": {"class_type": "KSampler", "inputs": {"steps": 1, "seed": 1}}}
    h3 = {"1": {"class_type": "LoadImage", "inputs": {"image": "never-uploaded.png"}},
          "2": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
              "first_frame": ["1", 0], "length": 9, "prompt": "p", "width": 64, "height": 64}},
          "3": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "x"}}}
    for graph, message in ((no_save, "Prompt has no outputs"),
                           (h3, "Invalid image file: never-uploaded.png")):
        with pytest.raises(ComfyError, match=f"HTTP 400: .*{message}"):
            api.submit_prompt({"prompt": graph, "client_id": "k"})
    assert fake.prompts == []
    pid = api.submit_prompt({"prompt": SAVE, "client_id": "k"})
    fake.wait_idle(10)
    assert fake.history[pid]["status"]["status_str"] == "success"


def test_a_graph_the_fake_cannot_run_fails_the_prompt_not_the_thread(fake):
    api = ComfyApi(fake.url, timeout=5)
    broken = {"1": {"class_type": "KSampler", "inputs": {"steps": 1}},    # no seed
              "2": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}}}
    pid = api.submit_prompt({"prompt": broken, "client_id": "k"})
    fake.wait_idle(10)
    assert fake.history[pid]["status"]["status_str"] == "error"
    pid2 = api.submit_prompt({"prompt": SAVE, "client_id": "k"})
    fake.wait_idle(10)
    assert fake.history[pid2]["status"]["status_str"] == "success"


# -- the WebSocket, as ComfyUI speaks it -------------------------------------------------------
class _Ws:
    def __init__(self, url, client_id):
        parts = urlsplit(url)
        self.sock = socket.create_connection((parts.hostname, parts.port), timeout=5)
        key = base64.b64encode(b"0123456789abcdef").decode()
        self.sock.sendall((f"GET /ws?clientId={client_id} HTTP/1.1\r\nHost: x\r\n"
                           f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self._read(1)
        assert head.startswith(b"HTTP/1.1 101")

    def _read(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def frame(self):
        first, second = self._read(2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read(2))[0]
        return first & 0x0F, self._read(length)

    def close(self):
        self.sock.close()


def test_websocket_messages_come_in_comfyuis_order(fake):
    api = ComfyApi(fake.url, timeout=5)
    graph = {"1": {"class_type": "KSampler", "inputs": {"steps": 2, "seed": 5}},
             "2": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}}}
    ws = _Ws(fake.url, "c1")
    try:
        seen = []

        def next_message():
            opcode, payload = ws.frame()
            seen.append((opcode, payload if opcode == 2 else json.loads(payload)))
            return seen[-1][1]

        assert next_message() == {"type": "status", "data": {
            "status": {"exec_info": {"queue_remaining": 0}}, "sid": "c1"}}
        pid = api.submit_prompt({"prompt": graph, "client_id": "c1"})
        while not (seen[-1][0] == 1 and seen[-1][1]["type"] == "executing"
                   and seen[-1][1]["data"]["node"] is None):
            next_message()
    finally:
        ws.close()
    done = {"type": "executed", "data": {"node": "2", "display_node": "2", "output": {
        "images": [{"filename": "x_00001_.png", "subfolder": "", "type": "output"}]},
        "prompt_id": pid}}
    preview = (2, struct.pack(">II", 1, 1) + b"\xff\xd8preview")
    assert seen[1:] == [
        (1, {"type": "execution_start", "data": {"prompt_id": pid}}),
        (1, {"type": "executing", "data": {"node": "1", "prompt_id": pid}}),
        (1, {"type": "progress", "data": {"value": 1, "max": 2, "prompt_id": pid, "node": "1"}}),
        preview,
        (1, {"type": "progress", "data": {"value": 2, "max": 2, "prompt_id": pid, "node": "1"}}),
        preview,
        (1, done),
        (1, {"type": "executing", "data": {"node": None, "prompt_id": pid}})]


def test_restart_closes_the_websockets(fake):
    ws = _Ws(fake.url, "c2")
    try:
        assert json.loads(ws.frame()[1])["type"] == "status"
        fake.restart()
        with pytest.raises((EOFError, ConnectionError)):
            ws.frame()
    finally:
        ws.close()
