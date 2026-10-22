"""ComfyUI's HTTP API, the part both callers need (studio spec §8.2): the old panel's LTX upscale
(`engines/ltx.py`) and the studio's executor (`studio/executors/comfy.py`). Synchronous `urllib`
(spec §5), JSON in and out; every failure is one exception type the caller chooses."""
from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid


#: ComfyUI is on the loopback or the LAN: never through a proxy from the environment (a SOCKS or
#: HTTP proxy variable turned "port closed" into "HTTP 502 from the proxy" — seen in the sandbox).
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ComfyError(Exception):
    pass


class ComfyApi:
    #: The exception every failure is raised as; the LTX client keeps its own `UpscaleError`.
    error: type[Exception] = ComfyError

    def __init__(self, base_url: str, timeout: float = 60.0, boundary: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.boundary = boundary

    def _open(self, method, path, data=None, headers=None):
        request = urllib.request.Request(self.base_url + path, data=data, method=method,
                                         headers=headers or {})
        try:
            with _DIRECT.open(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise self.error(f"ComfyUI {method} {path}: HTTP {exc.code}: "
                             f"{exc.read().decode('utf-8', 'replace')[:2000]}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise self.error(f"ComfyUI {method} {path} недоступен: {exc}") from None

    def _post_json(self, path: str, body: dict):
        return self._open("POST", path, json.dumps(body).encode(),
                          {"Content-Type": "application/json"})

    # -- reads ------------------------------------------------------------------------------
    def system_stats(self) -> dict:
        return self._open("GET", "/system_stats")

    def object_info(self) -> dict:
        return self._open("GET", "/object_info")

    def models(self, folder: str) -> list[str]:
        return list(self._open("GET", f"/models/{urllib.parse.quote(folder)}"))

    def queue(self) -> dict:
        return self._open("GET", "/queue")

    def history(self, prompt_id: str) -> dict | None:
        return self._open("GET", f"/history/{urllib.parse.quote(prompt_id, safe='')}").get(prompt_id)

    def all_history(self) -> dict:
        return self._open("GET", "/history")

    def view(self, filename: str, folder_type: str = "input", subfolder: str = "") -> bytes:
        """The bytes ComfyUI holds under a name (`GET /view`) — the live contract compares an
        uploaded input with its source this way."""
        query = urllib.parse.urlencode({"filename": filename, "type": folder_type,
                                        "subfolder": subfolder})
        request = urllib.request.Request(f"{self.base_url}/view?{query}")
        try:
            with _DIRECT.open(request, timeout=self.timeout) as response:
                return response.read()
        except (urllib.error.URLError, OSError) as exc:
            raise self.error(f"ComfyUI GET /view {filename} недоступен: {exc}") from None

    # -- writes -----------------------------------------------------------------------------
    def upload_image(self, data: bytes, name: str) -> str:
        """`POST /upload/image` into ComfyUI's input folder under exactly `name`
        (`overwrite=true`: a retried upload replaces its own earlier copy)."""
        if any(ch in name for ch in '"\r\n/\\') or not name:
            raise self.error(f"ComfyUI: недопустимое имя файла для загрузки: {name!r}")
        boundary = self.boundary or uuid.uuid4().hex
        b = boundary.encode()
        mime = (mimetypes.guess_type(name)[0] or "application/octet-stream").encode()
        body = (b"--" + b + b"\r\nContent-Disposition: form-data; name=\"image\"; filename=\""
                + name.encode() + b"\"\r\nContent-Type: " + mime + b"\r\n\r\n" + data
                + b"\r\n--" + b + b"\r\nContent-Disposition: form-data; name=\"type\"\r\n\r\ninput"
                + b"\r\n--" + b + b"\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\n"
                + b"true\r\n--" + b + b"--\r\n")
        answer = self._open("POST", "/upload/image", body,
                            {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return answer["name"]

    def submit_prompt(self, payload: dict) -> str:
        """`POST /prompt` with a whole payload (`prompt`, `client_id`, `extra_data`)."""
        answer = self._post_json("/prompt", payload)
        if "prompt_id" not in answer:
            raise self.error(f"ComfyUI отклонил воркфлоу: {json.dumps(answer)[:2000]}")
        return answer["prompt_id"]

    def interrupt(self, prompt_id: str | None = None) -> None:
        """Stop what ComfyUI is executing (triage of task 10/11: a cancelled part otherwise runs
        its 3.5 min to the end and the next job queues behind it). With `prompt_id` only that
        prompt, and only if it runs now (server.py `post_interrupt`); without — whatever runs."""
        body = json.dumps({"prompt_id": prompt_id}).encode() if prompt_id else b"{}"
        self._open("POST", "/interrupt", body, {"Content-Type": "application/json"})

    def delete_queued(self, prompt_ids: list[str]) -> None:
        self._post_json("/queue", {"delete": list(prompt_ids)})
