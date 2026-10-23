"""The studio worker's GPU gate (spec §8.4): the old panel's parameters — 30 s / 5 s retries,
80→72 °C — as exact equalities (test_gpu_gate.py is the specification), plus the Mac flock and
the 15-minute idle release."""
from __future__ import annotations

import pytest

from _fake_dispatcher import FakeDispatcher
from h3_48gb.engines.dispatcher_client import DispatcherClient, DispatcherUnavailable
from h3_48gb.studio.gpu import DispatcherGate, GpuEngineFailed, IdleRelease, LocalGate

READY = {"ok": True, "state": "ready", "engine": "comfy-studio"}


def _gate(fake):
    return DispatcherGate(DispatcherClient(fake.url, client="studio-worker"), "comfy-studio")


def _recorder(cancel_after=None):
    waits = []

    def wait(reason, seconds):
        waits.append((reason, seconds))
        return cancel_after is not None and len(waits) >= cancel_after
    return waits, wait


def test_the_gate_waits_with_the_reason_and_the_panels_intervals():
    fake = FakeDispatcher(acquire=(
        {"ok": True, "state": "wait", "engine": "comfy-studio",
         "reason": "карту держит ComfyUI клиента panel-worker"},
        {"ok": True, "state": "wait_qwen", "engine": "comfy-studio", "reason": "Qwen держит карту"},
        {"ok": True, "state": "starting", "engine": "comfy-studio", "log": "/l.log"},
        READY))
    waits, wait = _recorder()
    try:
        gate = _gate(fake)
        assert gate.acquire(wait) is False
        assert gate.acquired.is_set()
    finally:
        fake.close()
    assert waits == [("ждём GPU: карту держит ComfyUI клиента panel-worker", 30.0),
                     ("ждём GPU: Qwen держит карту", 30.0),
                     ("ждём GPU: поднимается comfy-studio", 5.0)]
    assert [c[2] for c in fake.calls if c[1] == "/acquire"] == [
        {"engine": "comfy-studio", "client": "studio-worker"}] * 4


def test_a_hot_card_is_waited_down_to_72():
    fake = FakeDispatcher(acquire=(READY,), temps=(80, 80, 73, 72, 40))
    waits, wait = _recorder()
    try:
        assert _gate(fake).acquire(wait) is False
    finally:
        fake.close()
    # 80 is hot; wait while > 72 (80, 73), stop at 72, ask again, 40 is cool
    assert waits == [("остываем, 80 °C", 30.0), ("остываем, 80 °C", 30.0),
                     ("остываем, 73 °C", 30.0)]


def test_79_degrees_is_not_hot_and_unknown_temperature_never_blocks():
    for kwargs in ({"temps": (79,)}, {"gpu_null": True}):
        fake = FakeDispatcher(acquire=(READY,), **kwargs)
        waits, wait = _recorder()
        try:
            assert _gate(fake).acquire(wait) is False
        finally:
            fake.close()
        assert waits == []


def test_a_cancel_ends_the_wait():
    fake = FakeDispatcher(acquire=({"ok": True, "state": "wait", "engine": "comfy-studio",
                                    "reason": "x"},))
    waits, wait = _recorder(cancel_after=2)
    try:
        assert _gate(fake).acquire(wait) is True
    finally:
        fake.close()
    assert waits == [("ждём GPU: x", 30.0)] * 2


def test_a_failed_engine_raises_with_its_log():
    fake = FakeDispatcher(acquire=({"ok": True, "state": "failed", "engine": "comfy-studio",
                                    "reason": "движок не поднялся, смотрите лог",
                                    "log": "/home/alex/Projects/comfy-h3/logs/x.log"},))
    try:
        with pytest.raises(GpuEngineFailed) as err:
            _gate(fake).acquire(_recorder()[1])
    finally:
        fake.close()
    assert (err.value.reason, err.value.log) == ("движок не поднялся, смотрите лог",
                                                 "/home/alex/Projects/comfy-h3/logs/x.log")


def test_a_dead_dispatcher_is_a_wait_not_a_failure():
    waits, wait = _recorder(cancel_after=1)
    gate = DispatcherGate(DispatcherClient("http://127.0.0.1:9", timeout=0.5,
                                           client="studio-worker"), "comfy-studio")
    assert gate.acquire(wait) is True
    assert [w[1] for w in waits] == [30.0]
    assert waits[0][0].startswith("ждём GPU: диспетчер недоступен (")


def test_an_answer_without_a_reason_is_shown_as_it_came():
    fake = FakeDispatcher(acquire=({"ok": True, "state": "odd"}, READY))
    waits, wait = _recorder()
    try:
        _gate(fake).acquire(wait)
    finally:
        fake.close()
    assert waits == [('ждём GPU: непонятный ответ диспетчера {"ok": true, "state": "odd"}', 30.0)]


def test_the_mac_gate_is_an_exclusive_flock(tmp_path):
    first, second = LocalGate(tmp_path / "gpu-mac.lock"), LocalGate(tmp_path / "gpu-mac.lock")
    assert first.acquire(_recorder()[1]) is False
    waits, wait = _recorder(cancel_after=1)
    assert second.acquire(wait) is True
    assert waits == [("ждём GPU: Мак занят (LLM сценариста)", 5.0)]
    first.done()
    assert second.acquire(_recorder()[1]) is False
    second.done()


def test_idle_release_after_15_minutes_of_an_empty_queue_and_only_our_engine():
    fake = FakeDispatcher(acquire=(READY,))
    clock = [0.0]
    try:
        client = DispatcherClient(fake.url, client="studio-worker")
        gate = DispatcherGate(client, "comfy-studio")
        idle = IdleRelease(client, gate, clock=lambda: clock[0])
        gate.acquired.set()
        assert idle.tick(busy=False) is False                 # the countdown starts
        clock[0] = 899.0
        assert idle.tick(busy=False) is False
        clock[0] = 905.0
        assert idle.tick(busy=True) is False                  # a task resets it
        clock[0] = 1000.0
        assert idle.tick(busy=False) is False
        clock[0] = 1900.0
        assert idle.tick(busy=False) is True
        assert not gate.acquired.is_set()
        clock[0] = 9000.0
        assert idle.tick(busy=False) is False                 # nothing taken since: no release
    finally:
        fake.close()
    assert [c for c in fake.calls if c[1] == "/release"] == [
        ("POST", "/release", {"client": "studio-worker", "all": False})]


def test_a_restarted_worker_releases_an_engine_it_still_owns():
    fake = FakeDispatcher(own={"comfy-studio": {"owner": "studio-worker", "ready": True}})
    clock = [0.0]
    try:
        client = DispatcherClient(fake.url, client="studio-worker")
        idle = IdleRelease(client, DispatcherGate(client, "comfy-studio"), clock=lambda: clock[0])
        idle.tick(busy=False)
        clock[0] = 900.0
        assert idle.tick(busy=False) is True
    finally:
        fake.close()


def test_a_restarted_worker_leaves_the_panels_engine_alone():
    fake = FakeDispatcher(own={"ltx": {"owner": "panel-worker", "ready": True}})
    clock = [0.0]
    try:
        client = DispatcherClient(fake.url, client="studio-worker")
        idle = IdleRelease(client, DispatcherGate(client, "comfy-studio"), clock=lambda: clock[0])
        idle.tick(busy=False)
        clock[0] = 900.0
        assert idle.tick(busy=False) is False
    finally:
        fake.close()
    assert [c for c in fake.calls if c[1] == "/release"] == []


def test_a_second_acquire_of_the_same_mac_gate_keeps_one_descriptor(tmp_path):
    first, other = LocalGate(tmp_path / "gpu-mac.lock"), LocalGate(tmp_path / "gpu-mac.lock")
    assert first.acquire(_recorder()[1]) is False
    assert first.acquire(_recorder(cancel_after=1)[1]) is False     # ours already, no waiting
    first.done()
    assert other.acquire(_recorder()[1]) is False                   # a leaked fd would still hold it
    other.done()


def test_a_status_that_failed_is_asked_again_on_the_next_tick():
    fake = FakeDispatcher(own={"comfy-studio": {"owner": "studio-worker", "ready": True}})
    clock = [0.0]

    class Flaky(DispatcherClient):
        failures = 1

        def status(self):
            if self.failures:
                self.failures -= 1
                raise DispatcherUnavailable("down")
            return super().status()
    try:
        client = Flaky(fake.url, client="studio-worker")
        idle = IdleRelease(client, DispatcherGate(client, "comfy-studio"), clock=lambda: clock[0])
        idle.tick(busy=False)
        clock[0] = 900.0
        assert idle.tick(busy=False) is False        # the first /status failed: nothing known yet
        clock[0] = 901.0
        assert idle.tick(busy=False) is True         # the second answered: our engine is released
    finally:
        fake.close()


def test_every_acquire_after_ready_asks_the_dispatcher_again():
    fake = FakeDispatcher(acquire=(READY,))
    try:
        gate = _gate(fake)
        assert gate.acquire(_recorder()[1]) is False
        assert gate.acquire(_recorder()[1]) is False
    finally:
        fake.close()
    assert [c[1] for c in fake.calls if c[1] == "/acquire"] == ["/acquire", "/acquire"]
