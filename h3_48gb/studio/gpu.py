"""The GPU gate of the studio worker (spec §8.4): one task at a time per GPU scope. On alex-neuro
the dispatcher owns the card (`acquire("comfy-studio")`); on the Mac a flock the API also takes
for a local LLM (`<outdir>/studio/gpu-mac.lock`, plan S0–S1 R10).

The dispatcher gate repeats the old panel's parameters (`worker.make_gpu_gate`, `worker.py:524`;
its tests `test_gpu_gate.py` are the specification): ask every 30 s, every 5 s while our engine
is starting, no timeout — only a cancel ends the wait; once ready, a card at ≥ 80 °C is waited
down to 72 °C. It does not import the old worker: that gate writes into the file queue."""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path

from h3_48gb.engines.dispatcher_client import DispatcherUnavailable

ACQUIRE_RETRY_SECONDS = 30.0
STARTING_POLL_SECONDS = 5.0
HOT_C = 80
COOL_C = 72
IDLE_RELEASE_MINUTES = 15.0


class GpuEngineFailed(Exception):
    def __init__(self, reason: str, log: str | None):
        super().__init__(reason)
        self.reason = reason
        self.log = log


class DispatcherGate:
    """`wait(reason, seconds) -> bool` is the worker's: it shows the reason and sleeps, and
    answers True when the task was cancelled meanwhile."""

    def __init__(self, client, engine: str):
        self.client = client
        self.engine = engine
        self.acquired = threading.Event()

    def _temperature(self) -> int | None:
        # Unknown temperature never blocks: a thermal wait with no number would hang for ever.
        try:
            return self.client.status()["gpu"]["temperature_c"]
        except (DispatcherUnavailable, KeyError, TypeError):
            return None

    def acquire(self, wait) -> bool:
        """True = cancelled while waiting; False = the card is ours and cool."""
        while True:
            try:
                answer = self.client.acquire(self.engine)
            except DispatcherUnavailable as exc:
                if wait(f"ждём GPU: диспетчер недоступен ({exc})", ACQUIRE_RETRY_SECONDS):
                    return True
                continue
            state = answer.get("state")
            if state == "failed":
                raise GpuEngineFailed(answer.get("reason") or "движок не поднялся",
                                      answer.get("log"))
            if state == "ready":
                self.acquired.set()
                temp = self._temperature()
                if temp is None or temp < HOT_C:
                    return False
                while temp is not None and temp > COOL_C:
                    if wait(f"остываем, {temp} °C", ACQUIRE_RETRY_SECONDS):
                        return True
                    temp = self._temperature()
                continue
            if state == "starting":
                reason, seconds = f"ждём GPU: поднимается {self.engine}", STARTING_POLL_SECONDS
            elif state == "wait_qwen":
                reason, seconds = "ждём GPU: Qwen держит карту", ACQUIRE_RETRY_SECONDS
            else:
                said = answer.get("reason") or (
                    f"непонятный ответ диспетчера {json.dumps(answer, ensure_ascii=False)[:200]}")
                reason, seconds = f"ждём GPU: {said}", ACQUIRE_RETRY_SECONDS
            if wait(reason, seconds):
                return True

    def done(self) -> None:
        """The engine stays up after a task (§8.4); the idle release gives it back."""


class LocalGate:
    """`local-mac`: an exclusive flock for the task's duration."""

    WAIT_SECONDS = 5.0

    def __init__(self, lock_path: Path):
        self.lock_path = Path(lock_path)
        self.fd: int | None = None
        self.acquired = threading.Event()

    def acquire(self, wait) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o664)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                if wait("ждём GPU: Мак занят (LLM сценариста)", self.WAIT_SECONDS):
                    return True
                continue
            self.fd = fd
            return False

    def done(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class IdleRelease:
    """POST /release (this client's engines only) once the studio has had no task to run for 15
    minutes — counted from an empty queue, not from an idle card (`worker._IdleRelease`, :603).
    Sent only if the gate took the card since the last release; a restarted worker asks /status
    once whether an engine of its own is still up."""

    def __init__(self, client, gate, *, minutes: float = IDLE_RELEASE_MINUTES,
                 clock=time.monotonic):
        self.client = client
        self.gate = gate
        self.limit = minutes * 60.0
        self.clock = clock
        self.since: float | None = None
        self._asked = False

    def _owns_an_engine(self) -> bool:
        if not self.gate.acquired.is_set() and not self._asked:
            self._asked = True
            try:
                own = self.client.status().get("own") or {}
            except DispatcherUnavailable:
                return False
            if any(r.get("owner") == self.client.client for r in own.values()):
                self.gate.acquired.set()
        return self.gate.acquired.is_set()

    def tick(self, busy: bool) -> bool:
        """True when it released the card on this tick."""
        if busy:
            self.since = None
            return False
        now = self.clock()
        if self.since is None:
            self.since = now
        if now - self.since >= self.limit and self._owns_an_engine():
            try:
                self.client.release()
            except DispatcherUnavailable:
                return False
            self.gate.acquired.clear()
            self.since = None
            return True
        return False
