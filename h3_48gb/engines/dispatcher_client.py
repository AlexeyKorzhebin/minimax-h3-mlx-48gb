"""The panel's client for tools/gpu-dispatcher (spec §3.4). The container has no nvidia-smi;
everything it knows about the card comes from here."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8790"

#: The dispatcher legitimately holds /acquire and /release for up to 120 s while it stops its own
#: engine (spec §3.4), so a shorter client timeout would report a healthy dispatcher as dead and
#: the worker would re-ask while the first request is still being served.
LONG_TIMEOUT = 130.0
#: /status is a quick read; the page polls it, so it must not hang.
STATUS_TIMEOUT = 10.0


#: Who asks for the card (final review 2026-10-07, C3): the dispatcher stops on /release only the
#: engines the same client acquired; "everything" is reserved for the human's button.
PANEL_WORKER = "panel-worker"
PANEL_WEB = "panel-web"
PROBES = "probes"


class DispatcherUnavailable(Exception):
    pass


class DispatcherClient:
    def __init__(self, base_url: str | None = None, timeout: float = LONG_TIMEOUT, *,
                 client: str = PANEL_WORKER):
        self.base_url = (base_url or os.environ.get("H3_DISPATCHER_URL") or DEFAULT_URL).rstrip("/")
        self.timeout = timeout
        self.client = client

    def _call(self, method: str, path: str, payload=None, *, timeout: float | None = None
              ) -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"} if data else {})
        # No Origin header, ever: the dispatcher answers a request that carries one with 403.
        try:
            with urllib.request.urlopen(
                    request, timeout=self.timeout if timeout is None else timeout) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            except ValueError:
                raise DispatcherUnavailable(f"{method} {path}: HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise DispatcherUnavailable(f"{method} {path}: {exc}") from None

    def _ok(self, method: str, path: str, payload=None, **kwargs) -> dict:
        """The body of a 200; any other answer is the dispatcher being unusable, not a state."""
        status, body = self._call(method, path, payload, **kwargs)
        if status != 200:
            raise DispatcherUnavailable(f"{method} {path}: HTTP {status}")
        return body

    def status(self) -> dict:
        return self._ok("GET", "/status", timeout=min(self.timeout, STATUS_TIMEOUT))

    def acquire(self, engine: str) -> dict:
        return self._ok("POST", "/acquire", {"engine": engine, "client": self.client})

    def release(self, everything: bool = False) -> dict:
        """Stop this client's engines; `everything=True` only for "Освободить карту"."""
        return self._ok("POST", "/release", {"client": self.client, "all": everything})

    def qwen_unload(self) -> tuple[int, dict]:
        return self._call("POST", "/qwen/unload", {})

    def qwen_restore(self) -> tuple[int, dict]:
        return self._call("POST", "/qwen/restore", {})
