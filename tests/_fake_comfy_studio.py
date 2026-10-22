"""A stand-in for the studio's ComfyUI (studio spec §12.1): the HTTP API the adapter uses and the
WebSocket progress stream, with an execution thread that behaves like ComfyUI's one queue.

Separate from `_fake_comfy.py` (the LTX fake): the 22 tests of test_ltx_upscale.py stay on their
fake unchanged. What this one adds, per §12.1:
- `prompts` — the journal of every accepted `POST /prompt`: graph, client_id, extra_data;
- `uploads` — the bytes of every uploaded file by name;
- the result carries its inputs: a PNG with a text chunk / an mp4 with a `comment` tag holding
  seed, steps, length and the sha256 of every uploaded picture, so swapping any input changes it;
  the mp4 has exactly `length` frames that change over time (its last frame differs from frame 0);
- scenarios: `fail` (error in /history), `hold` (execution waits on `release()`), `restart()`
  (queue and history emptied, files stay), `drop_ws` (sockets closed before progress),
  `flat` (the clip is one flat colour — a «залитый» result);
- pictures are read through the graph's links (`first_frame`, `ref_images.ref_image_<i>` of the
  H3 node, in that order) — an unconnected or swapped LoadImage changes the result;
- `upload_fields` — the form fields of each upload (`type`, `overwrite`); `GET /view` serves
  an uploaded input back (only `type=input`, no subfolder, as an upload lands);
- as ComfyUI's server.py answers (0.36): `POST /interrupt` and `POST /queue` reply 200 with an
  EMPTY body, a targeted interrupt (`{"prompt_id": ...}`) only touches the prompt that runs now,
  `extra_data` gets the top-level `client_id` over its own and no `client_id` key without one;
  a graph with no save node or a LoadImage the server never received is a 400 on `/prompt`;
- more scenarios: `no_prompt_id` (200 without `prompt_id`), `non_json` (200, body not JSON),
  `post_timeout=s` (the prompt is accepted, the answer comes `s` seconds late),
  `history_pending=n` (the first `n` polls of a finished prompt find no entry yet),
  `rename_to` (an upload is stored and answered under another name);
- WebSocket, in ComfyUI's order: `status` on connect, `execution_start`, `executing` with the
  node, `progress` + a binary preview frame per step, `executed`, `executing` with `node: null`;
  `restart()` closes the sockets.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import socket
import struct
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PIL import Image, PngImagePlugin

STUDIO_CLASSES = ["BasicGuider", "BasicScheduler", "CLIPLoader", "CLIPTextEncode",
                  "ConditioningZeroOut", "CreateVideo", "EmptyLatentImage", "KSampler",
                  "KSamplerSelect", "LoadImage", "MiniMaxH3ImageToVideo",
                  "MiniMaxH3ReferenceToVideo", "RandomNoise", "SamplerCustomAdvanced", "SaveImage",
                  "SaveVideo", "UNETLoader", "VAEDecode", "VAEDecodeAudio", "VAELoader"]
STUDIO_MODELS = {
    "diffusion_models": ["krea2_turbo_int8_convrot.safetensors",
                         "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
                         "minimax_h3_ref2va_pruned_int8_convrot.safetensors"],
    "text_encoders": ["qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
                      "qwen3vl_4b_int8_convrot.safetensors"],
    "vae": ["minimax_h3_audio_vae_fp32.safetensors", "minimax_h3_video_vae_fp16.safetensors",
            "qwen_image_vae.safetensors"],
}
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_PREVIEW = b"\xff\xd8preview"


def read_comment(mp4: Path) -> dict:
    """The inputs the fake wrote into a result mp4."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags=comment",
                          "-of", "default=nw=1:nk=1", str(mp4)], capture_output=True, text=True,
                         check=True)
    return json.loads(out.stdout.strip())


class FakeComfyStudio:
    def __init__(self, output_dir: Path, *, classes=STUDIO_CLASSES, models=STUDIO_MODELS,
                 step_s: float = 0.002, fail: str | None = None, hold: bool = False,
                 drop_ws: bool = False, flat: bool = False, comfyui_version: str = "0.42.0",
                 no_prompt_id: bool = False, non_json: bool = False, post_timeout: float = 0.0,
                 history_pending: int = 0, rename_to: str | None = None,
                 torch_version: str = "2.9.1+cu128",
                 gpu: str = "cuda:0 NVIDIA CMP 170HX 64GB : cudaMallocAsync"):
        self.output_dir = Path(output_dir)
        self.classes = list(classes)
        self.models = {k: list(v) for k, v in models.items()}
        self.step_s = step_s
        self.fail = fail
        self.hold = hold
        self.drop_ws = drop_ws
        self.flat = flat
        self.comfyui_version = comfyui_version
        self.torch_version = torch_version
        self.gpu = gpu
        self.no_prompt_id = no_prompt_id
        self.non_json = non_json
        self.post_timeout = post_timeout
        self.history_pending = history_pending
        self.rename_to = rename_to
        self.upload_bodies: list[bytes] = []
        self.upload_content_types: list[str] = []
        self.upload_mimes: dict[str, str] = {}
        self.view_requests: list[dict] = []
        self.interrupt_bodies: list[dict] = []
        self.requests: list[tuple[str, str]] = []
        self._history_polls: dict[str, int] = {}
        self._ws_lock = threading.Lock()
        self.upload_fields: dict[str, dict] = {}
        self.uploads: dict[str, bytes] = {}
        self.prompts: list[dict] = []
        self.interrupts = 0
        self.deleted: list[str] = []
        self.foreign_running = False
        self._released = threading.Event()
        self._lock = threading.Condition()
        self._pending: list[dict] = []
        self._running: dict | None = None
        self._interrupt = False
        self.history: dict[str, dict] = {}
        self._sockets: dict[str, list] = {}
        self._number = 0
        self._stop = False
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self._worker = threading.Thread(target=self._execute_loop, daemon=True)
        self._worker.start()

    # -- control ----------------------------------------------------------------------------
    def close(self):
        with self._lock:
            self._stop = True
            self._lock.notify_all()
        self._released.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    def release(self):
        """Let a held execution (`hold=True`) run on."""
        self._released.set()

    def restart(self):
        """kill -9 and start again (GPU-DAY-20261021 п.5): queue and history are empty, the
        files already written stay, a running prompt never finishes."""
        with self._lock:
            self._pending.clear()
            self._running = None
            self.history.clear()
            self._interrupt = True
            self._lock.notify_all()
        self._released.set()
        with self._ws_lock:
            sockets = [s for socks in self._sockets.values() for s in socks]
            self._sockets.clear()
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def wait_idle(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._pending or self._running:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("fake ComfyUI still busy")
                self._lock.wait(left)

    # -- execution --------------------------------------------------------------------------
    def _execute_loop(self):
        while True:
            with self._lock:
                while not self._pending and not self._stop:
                    self._lock.wait()
                if self._stop:
                    return
                job = self._pending.pop(0)
                self._running = job
                self._interrupt = False
            try:
                self._run(job)
            except Exception as exc:  # a graph the fake cannot run is a failed prompt, not a dead thread
                self._finish(job, {"status_str": "error", "completed": False, "messages": [
                    ["execution_error", {"prompt_id": job["prompt_id"],
                                         "exception_message": f"{type(exc).__name__}: {exc}"}]]}, {})
            with self._lock:
                self._lock.notify_all()

    def _run(self, job: dict) -> None:
        pid, graph = job["prompt_id"], job["prompt"]
        self._send(job["client_id"], {"type": "execution_start", "data": {"prompt_id": pid}})
        steps = _steps(graph)
        sampler = next((k for k, n in graph.items()
                        if n["class_type"] in ("BasicScheduler", "KSampler")), None)
        node_id = sampler or next(k for k, n in graph.items()
                                  if n["class_type"] in ("SaveImage", "SaveVideo"))
        self._send(job["client_id"], {"type": "executing",
                                      "data": {"node": node_id, "prompt_id": pid}})
        for value in range(1, steps + 1):
            if self.hold and not self._released.is_set():
                self._released.wait()
            if self._interrupted(job):
                return
            self._send(job["client_id"], {"type": "progress", "data": {
                "value": value, "max": steps, "prompt_id": pid, "node": node_id}})
            self._send_binary(job["client_id"], struct.pack(">II", 1, 1) + _PREVIEW)
            time.sleep(self.step_s)
        if self._interrupted(job):
            return
        if self.fail:
            status = {"status_str": "error", "completed": False, "messages": [
                ["execution_start", {"prompt_id": pid}],
                ["execution_error", {"prompt_id": pid, "exception_message": self.fail}]]}
            self._finish(job, status, {})
            return
        outputs = self._write_outputs(graph)
        for save_id, output in outputs.items():
            self._send(job["client_id"], {"type": "executed", "data": {
                "node": save_id, "display_node": save_id, "output": output, "prompt_id": pid}})
        self._finish(job, {"status_str": "success", "completed": True, "messages": [
            ["execution_start", {"prompt_id": pid}], ["execution_success", {"prompt_id": pid}]]},
            outputs)

    def _interrupted(self, job) -> bool:
        with self._lock:
            if self._running is not job:          # restart() dropped it
                return True
            if not self._interrupt:
                return False
            self._running = None
        self.history[job["prompt_id"]] = {
            "prompt": [job["number"], job["prompt_id"], job["prompt"], job["extra_data"], []],
            "outputs": {}, "status": {"status_str": "error", "completed": False, "messages": [
                ["execution_interrupted", {"prompt_id": job["prompt_id"]}]]}}
        self._send(job["client_id"], {"type": "execution_interrupted",
                                      "data": {"prompt_id": job["prompt_id"]}})
        return True

    def _finish(self, job, status, outputs):
        with self._lock:
            if self._running is not job:
                return
            self.history[job["prompt_id"]] = {
                "prompt": [job["number"], job["prompt_id"], job["prompt"], job["extra_data"], []],
                "outputs": outputs, "status": status}
            self._running = None
        self._send(job["client_id"], {"type": "executing",
                                      "data": {"node": None, "prompt_id": job["prompt_id"]}})

    def _inputs_record(self, graph) -> dict:
        pictures = [graph[link[0]]["inputs"]["image"] for link in _picture_links(graph)]
        record = {"seed": _seed(graph), "steps": _steps(graph), "length": _length(graph),
                  "prompt": _prompt(graph),
                  "pictures": [{"name": p, "sha256": hashlib.sha256(self.uploads[p]).hexdigest()}
                               for p in pictures]}
        return record

    def _write_outputs(self, graph) -> dict:
        save_id, save = next((k, n) for k, n in graph.items()
                             if n["class_type"] in ("SaveImage", "SaveVideo"))
        prefix = save["inputs"]["filename_prefix"]
        folder = self.output_dir / Path(prefix).parent
        folder.mkdir(parents=True, exist_ok=True)
        stem = Path(prefix).name
        record = self._inputs_record(graph)
        width, height = _size(graph)
        if save["class_type"] == "SaveImage":
            name = f"{stem}_00001_.png"
            shade = int(hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:6],
                        16)
            info = PngImagePlugin.PngInfo()
            info.add_text("studio", json.dumps(record, sort_keys=True))
            Image.new("RGB", (width, height), (shade >> 16, (shade >> 8) & 255, shade & 255)).save(
                folder / name, pnginfo=info)
        else:
            name = f"{stem}_00001_.mp4"
            hue = int(hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:4],
                      16) % 360
            source = (f"color=c=0x404040:size={width}x{height}:rate=24" if self.flat
                      else f"testsrc2=size={width}x{height}:rate=24")
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", source, "-f", "lavfi", "-i",
                 "sine=frequency=440:sample_rate=48000", "-frames:v", str(record["length"]),
                 "-vf", f"hue=h={hue}", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p", "-c:a", "aac",
                 "-metadata", f"comment={json.dumps(record, sort_keys=True)}",
                 str(folder / name)], check=True)
        sub = "" if str(Path(prefix).parent) == "." else str(Path(prefix).parent)
        return {save_id: {"images": [{"filename": name, "subfolder": sub, "type": "output"}]}}

    # -- websocket --------------------------------------------------------------------------
    def _send(self, client_id: str, message: dict) -> None:
        self._frame(client_id, 0x81, json.dumps(message).encode())

    def _send_binary(self, client_id: str, payload: bytes) -> None:
        self._frame(client_id, 0x82, payload)

    def _frame(self, client_id: str, opcode: int, payload: bytes) -> None:
        header = bytes([opcode]) + (bytes([len(payload)]) if len(payload) < 126
                                    else bytes([126]) + struct.pack(">H", len(payload)))
        with self._ws_lock:
            for sock in list(self._sockets.get(client_id, [])):
                try:
                    sock.sendall(header + payload)
                except OSError:
                    self._sockets[client_id].remove(sock)

    def _handler(fake):  # noqa: N805
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, status, body):
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _raw(self, status, raw, content_type="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _queue_entry(self, job):
                return [job["number"], job["prompt_id"], job["prompt"], job["extra_data"], []]

            def do_GET(self):
                fake.requests.append(("GET", self.path))
                parts = urlsplit(self.path)
                path = parts.path
                if path == "/ws":
                    return self._websocket(parse_qs(parts.query).get("clientId", [""])[0])
                if path == "/system_stats":
                    system = {"comfyui_version": fake.comfyui_version,
                              "pytorch_version": fake.torch_version}
                    system = {k: v for k, v in system.items() if v is not None}
                    return self._send(200, {"system": system, "devices": [
                        {"name": fake.gpu, "type": "cuda", "index": 0}] if fake.gpu else []})
                if path == "/object_info":
                    return self._send(200, {c: {"input": {}} for c in fake.classes})
                if path.startswith("/models/"):
                    return self._send(200, fake.models.get(path[len("/models/"):], []))
                if path == "/queue":
                    with fake._lock:
                        running = [self._queue_entry(fake._running)] if fake._running else []
                        if fake.foreign_running:
                            running.append([0, "foreign", {}, {"client_id": "x"}, []])
                        pending = [self._queue_entry(j) for j in fake._pending]
                    return self._send(200, {"queue_running": running, "queue_pending": pending})
                if path == "/view":
                    query = parse_qs(parts.query, keep_blank_values=True)
                    name = query.get("filename", [""])[0]
                    fake.view_requests.append({"filename": name,
                                               "type": query.get("type", [""])[0],
                                               "subfolder": query.get("subfolder", [""])[0]})
                    if (name not in fake.uploads or query.get("type", ["output"])[0] != "input"
                            or query.get("subfolder", [""])[0] != ""):
                        return self._send(404, {})
                    raw = fake.uploads[name]
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if path == "/history":
                    return self._send(200, dict(fake.history))
                if path.startswith("/history/"):
                    pid = path[len("/history/"):]
                    if pid in fake.history:
                        polls = fake._history_polls.get(pid, 0)
                        fake._history_polls[pid] = polls + 1
                        if polls < fake.history_pending:
                            return self._send(200, {})
                    return self._send(200, {pid: fake.history[pid]} if pid in fake.history else {})
                self._send(404, {})

            def do_POST(self):
                fake.requests.append(("POST", self.path))
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/upload/image":
                    name = re.search(rb'filename="([^"]+)"', body).group(1).decode()
                    fake.upload_bodies.append(body)
                    fake.upload_content_types.append(self.headers["Content-Type"])
                    mime = re.search(rb'Content-Type: ([^\r]+)', body).group(1).decode()
                    name = fake.rename_to or name
                    fake.upload_mimes[name] = mime
                    boundary = self.headers["Content-Type"].split("boundary=")[1].encode()
                    parts_ = body.split(b"--" + boundary)[1:-1]
                    fields = {}
                    for part in parts_:
                        head, _, value = part.partition(b"\r\n\r\n")
                        field = re.search(rb'name="([^"]+)"', head).group(1).decode()
                        if field == "image":
                            fake.uploads[name] = value[:-2]
                        else:
                            fields[field] = value[:-2].decode()
                    fake.upload_fields[name] = fields
                    return self._send(200, {"name": name, "subfolder": "", "type": "input"})
                if self.path == "/prompt":
                    payload = json.loads(body)
                    graph = payload["prompt"]
                    if fake.no_prompt_id:
                        return self._send(200, {"node_errors": {}})
                    if fake.non_json:
                        return self._raw(200, b"<html>not json</html>", "text/html")
                    unknown = sorted({n.get("class_type") for n in graph.values()
                                      if isinstance(n, dict)} - set(fake.classes) - {None})
                    if unknown:
                        return self._send(400, {"error": {"type": "invalid_prompt",
                                                          "message": f"unknown {unknown}"},
                                                "node_errors": {}})
                    bad = _invalid(graph, fake.uploads)
                    if bad:
                        return self._send(400, {"error": {"type": "prompt_outputs_failed_validation",
                                                          "message": bad}, "node_errors": {}})
                    with fake._lock:
                        fake._number += 1
                        job = {"prompt_id": str(uuid.uuid4()), "number": fake._number,
                               "prompt": graph, "client_id": payload.get("client_id"),
                               "extra_data": dict(payload.get("extra_data") or {})}
                        if "client_id" in payload:
                            job["extra_data"]["client_id"] = payload["client_id"]
                        fake.prompts.append(job)
                        fake._pending.append(job)
                        fake._lock.notify_all()
                    if fake.post_timeout:
                        time.sleep(fake.post_timeout)
                    return self._send(200, {"prompt_id": job["prompt_id"],
                                            "number": job["number"], "node_errors": {}})
                if self.path == "/interrupt":
                    try:
                        asked = json.loads(body or b"{}")
                    except ValueError:
                        asked = {}
                    with fake._lock:
                        fake.interrupts += 1
                        fake.interrupt_bodies.append(asked)
                        target = asked.get("prompt_id")
                        hit = (fake._running is not None
                               and (not target or fake._running["prompt_id"] == target))
                        if hit:
                            fake._interrupt = True
                    if hit:
                        fake._released.set()
                    return self._raw(200, b"", "text/plain")
                if self.path == "/queue":
                    delete = json.loads(body).get("delete", [])
                    with fake._lock:
                        fake.deleted.extend(delete)
                        fake._pending = [j for j in fake._pending if j["prompt_id"] not in delete]
                        fake._lock.notify_all()
                    return self._raw(200, b"", "text/plain")
                self._send(404, {})

            def _websocket(self, client_id):
                key = self.headers.get("Sec-WebSocket-Key", "")
                accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest())
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept.decode())
                self.end_headers()
                self.wfile.flush()
                sock = self.connection
                if fake.drop_ws:
                    sock.close()
                    self.close_connection = True
                    return
                with fake._ws_lock:
                    fake._sockets.setdefault(client_id, []).append(sock)
                with fake._lock:
                    remaining = len(fake._pending) + (1 if fake._running else 0)
                fake._send(client_id, {"type": "status", "data": {
                    "status": {"exec_info": {"queue_remaining": remaining}}, "sid": client_id}})
                try:
                    while True:
                        head = self.rfile.read(2)
                        if len(head) < 2:
                            break
                        opcode, length = head[0] & 0x0F, head[1] & 0x7F
                        if length == 126:
                            length = struct.unpack(">H", self.rfile.read(2))[0]
                        elif length == 127:
                            length = struct.unpack(">Q", self.rfile.read(8))[0]
                        mask = self.rfile.read(4) if head[1] & 0x80 else b"\0\0\0\0"
                        data = bytes(b ^ mask[i % 4] for i, b in enumerate(self.rfile.read(length)))
                        if opcode == 0x8:
                            sock.sendall(b"\x88\x00")
                            break
                        if opcode == 0x9:
                            sock.sendall(bytes([0x8A, len(data)]) + data)
                except (OSError, socket.timeout):
                    pass
                finally:
                    with fake._ws_lock:
                        if sock in fake._sockets.get(client_id, []):
                            fake._sockets[client_id].remove(sock)
                    self.close_connection = True
        return Handler


def _invalid(graph, uploads) -> str | None:
    """What ComfyUI's validate_prompt would refuse that the fake can see: no save node, a node
    without a class, a picture input that is not a LoadImage the server holds the file of."""
    if not isinstance(graph, dict) or not all(isinstance(n, dict) and "class_type" in n
                                              and isinstance(n.get("inputs"), dict)
                                              for n in graph.values()):
        return "malformed graph"
    if not any(n["class_type"] in ("SaveImage", "SaveVideo") for n in graph.values()):
        return "Prompt has no outputs"
    for link in _picture_links(graph):
        source = graph.get(link[0]) if isinstance(link, list) and link else None
        if source is None or source["class_type"] != "LoadImage":
            return f"picture input {link} is not a LoadImage"
        if source["inputs"].get("image") not in uploads:
            return f"Invalid image file: {source['inputs'].get('image')}"
    return None


def _picture_links(graph) -> list[list]:
    """The pictures the H3 node actually uses, in input order: `first_frame`, then
    `ref_images.ref_image_0..`; Krea uses none."""
    node = _node(graph, "MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo")
    if node is None:
        return []
    inputs = node["inputs"]
    links = [inputs["first_frame"]] if "first_frame" in inputs else []
    refs = sorted((k for k in inputs if k.startswith("ref_images.ref_image_")),
                  key=lambda k: int(k.rsplit("_", 1)[1]))
    return links + [inputs[k] for k in refs]


def _node(graph, *classes):
    return next((n for n in graph.values() if n["class_type"] in classes), None)


def _steps(graph) -> int:
    node = _node(graph, "BasicScheduler", "KSampler")
    return int(node["inputs"]["steps"]) if node else 1


def _seed(graph) -> int:
    noise, sampler = _node(graph, "RandomNoise"), _node(graph, "KSampler")
    return int(noise["inputs"]["noise_seed"] if noise else sampler["inputs"]["seed"] if sampler
               else 0)


def _length(graph) -> int | None:
    node = _node(graph, "MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo")
    return int(node["inputs"]["length"]) if node else None


def _prompt(graph) -> str:
    node = _node(graph, "MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo")
    text = _node(graph, "CLIPTextEncode")
    return node["inputs"]["prompt"] if node else text["inputs"]["text"] if text else ""


def _size(graph) -> tuple[int, int]:
    node = _node(graph, "MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo", "EmptyLatentImage")
    return (int(node["inputs"]["width"]), int(node["inputs"]["height"])) if node else (64, 64)
