"""gpu-dispatcher (spec §3.4) against a fake host: never touches a foreign pid, owns only what it
started, survives its own restart, honours generation.lock (held by a separate process here --
flock is only honest across processes), and touches Qwen only on request."""
import importlib.util
import json
import signal
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

from test_queue import _external_lock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gpu_dispatcher",
                                               ROOT / "tools" / "gpu-dispatcher" / "dispatcher.py")
gd = importlib.util.module_from_spec(_spec)
sys.modules["gpu_dispatcher"] = gd   # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(gd)

H3_READY = "http://127.0.0.1:30020/v1/models"
LTX_READY = "http://127.0.0.1:8188/system_stats"


class FakeHost:
    def __init__(self):
        self.apps, self.ok_urls, self.spawned, self.killed, self.qwen_calls = [], set(), [], [], []
        self.cmdlines, self.pgids, self.alive_groups = {}, {}, set()
        self.t = 1000.0
        self.next_pid = 4242
        self.die_on_term = True
        self.smi_down = None
        self.stats = {"temperature_c": 44, "memory_used_mb": 15, "memory_total_mb": 65536}
        self.exec_cmdlines = {"h3": "/home/alex/Projects/h3-lab/.venv312/bin/python3 /home/alex/Projects/h3-lab/.venv312/bin/sglang serve --model-type diffusion --model-path /home/alex/Models/Video/MiniMax-H3/model-package --model-id minimax-h3 --model-variant ref2va --num-gpus 1 --host 127.0.0.1 --port 30020",
                              "ltx": "/home/alex/Projects/comfy/.venv/bin/python main.py --port 8188 --output-directory /home/alex/Outputs/comfy/output --listen 127.0.0.1"}

    def gpu_apps(self):
        if self.smi_down:
            raise gd.HostError(self.smi_down)
        return [dict(app) for app in self.apps]

    def gpu_stats(self):
        if self.smi_down:
            raise gd.HostError(self.smi_down)
        return dict(self.stats)

    def url_ok(self, url):
        return url in self.ok_urls

    def spawn(self, spec, log_path, pass_fds):
        pid = self.next_pid
        self.next_pid += 1
        self.spawned.append((spec.name, tuple(spec.cmd), str(spec.cwd), dict(spec.env),
                             str(log_path), len(pass_fds)))
        # serve.sh execs sglang: by the time anyone looks, the pid's cmdline is sglang's own
        self.cmdlines[pid] = self.exec_cmdlines[spec.name]
        self.pgids[pid] = pid
        self.alive_groups.add(pid)
        return pid, pid

    def cmdline(self, pid):
        return self.cmdlines.get(pid)

    def pgid_of(self, pid):
        return self.pgids.get(pid)

    def killpg(self, pgid, sig):
        self.killed.append((pgid, sig))
        if sig == signal.SIGKILL or self.die_on_term:
            self.alive_groups.discard(pgid)
            for pid in [p for p, g in self.pgids.items() if g == pgid]:
                self.cmdlines.pop(pid, None)

    def group_alive(self, pgid):
        return pgid in self.alive_groups

    def run_qwen(self, action):
        self.qwen_calls.append(action)
        return 0

    def start_qwen(self):
        self.qwen_calls.append("start-shared32")

    def dir_size(self, path):
        return 123

    def monotonic(self):
        return self.t

    def wall(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


@pytest.fixture
def host():
    return FakeHost()


def _dispatcher(tmp_path, host):
    return gd.Dispatcher(host=host, specs=gd.engine_specs(), state_path=tmp_path / "state.json",
                         lock_path=tmp_path / "generation.lock", server_outputs=tmp_path / "so")


def _lock_is_free(path) -> bool:
    script = ("import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
              "try:\n    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n    print('free')\n"
              "except BlockingIOError:\n    print('busy')\n")
    out = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True)
    return out.stdout.strip() == "free"


def test_parse_nvidia_smi_outputs():
    assert gd.parse_compute_apps("1234, /usr/bin/python3, 40000\n777, sglang::scheduler, 512\n") == [
        {"pid": 1234, "name": "/usr/bin/python3", "memory_mb": 40000},
        {"pid": 777, "name": "sglang::scheduler", "memory_mb": 512}]
    assert gd.parse_compute_apps("No running processes found\n") == []
    assert gd.parse_gpu_stats("44, 15, 65536\n") == \
        {"temperature_c": 44, "memory_used_mb": 15, "memory_total_mb": 65536}


def test_acquire_on_a_free_card_starts_our_h3_with_the_pipeline_command(tmp_path, host):
    d = _dispatcher(tmp_path, host)
    answer = d.acquire("h3")
    spec = gd.engine_specs()["h3"]
    assert answer == {"ok": True, "state": "starting", "engine": "h3", "log": host.spawned[0][4]}
    assert host.spawned[0][:4] == (
        "h3", ("bash", "/home/alex/Projects/h3-bench/serve.sh"), "/home/alex/Projects/h3-bench",
        {"VARIANT": "ref2va", "TE": "/home/alex/Models/Video/MiniMax-H3/runtime-components/"
                                     "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"})
    assert host.spawned[0][5] == 1            # the lock fd is handed to the engine
    assert not _lock_is_free(tmp_path / "generation.lock")
    host.ok_urls.add(H3_READY)
    assert d.acquire("h3") == {"ok": True, "state": "ready", "engine": "h3"}


def test_qwen_holding_the_card_is_wait_qwen_and_nothing_is_touched(tmp_path, host):
    host.ok_urls.add(gd.QWEN_HEALTH)
    host.apps = [{"pid": 900, "name": "python3", "memory_mb": 59000}]
    answer = _dispatcher(tmp_path, host).acquire("h3")
    assert (answer["state"], answer["reason"]) == ("wait_qwen", "Qwen держит карту")
    assert (host.spawned, host.killed, host.qwen_calls) == ([], [], [])


def test_a_foreign_gpu_process_means_wait_and_is_named(tmp_path, host):
    host.apps = [{"pid": 777, "name": "comfy-python", "memory_mb": 30000}]
    host.pgids[777] = 777
    answer = _dispatcher(tmp_path, host).acquire("ltx")
    assert answer == {"ok": True, "state": "wait", "engine": "ltx",
                      "reason": "GPU занята: comfy-python (pid 777, 30000 МБ)",
                      "foreign": [{"pid": 777, "name": "comfy-python", "memory_mb": 30000,
                                   "first_seen": 1000.0}]}
    assert (host.spawned, host.killed) == ([], [])


def test_first_seen_of_a_foreign_process_is_kept_across_calls(tmp_path, host):
    host.apps = [{"pid": 777, "name": "comfy-python", "memory_mb": 30000}]
    host.pgids[777] = 777
    d = _dispatcher(tmp_path, host)
    d.acquire("ltx")
    host.t += 600
    assert d.status()["foreign"] == [{"pid": 777, "name": "comfy-python", "memory_mb": 30000,
                                      "first_seen": 1000.0}]


def test_an_h3_with_another_variant_at_our_pid_is_not_ours(tmp_path, host):
    _dispatcher(tmp_path, host).acquire("h3")
    host.cmdlines[4242] = host.exec_cmdlines["h3"].replace("ref2va", "fl2va")
    reborn = _dispatcher(tmp_path, host)
    assert reborn.release() == {"ok": True, "stopped": []}
    assert host.killed == []


def test_status_answers_while_a_release_is_waiting_for_a_group_to_die(tmp_path, host):
    import time as _time
    d = _dispatcher(tmp_path, host)
    d.acquire("h3")
    entered, finish = threading.Event(), threading.Event()
    real_kill = host.killpg

    def slow_kill(pgid, sig):
        entered.set()
        finish.wait(5)
        real_kill(pgid, sig)

    host.killpg = slow_kill
    worker = threading.Thread(target=d.release)
    worker.start()
    assert entered.wait(5)
    started = _time.monotonic()
    status = d.status()
    assert _time.monotonic() - started < 1.0
    assert status["own"]["h3"]["pid"] == 4242
    finish.set()
    worker.join(5)


def test_a_foreign_h3_on_its_port_is_not_ours(tmp_path, host):
    host.ok_urls.add(H3_READY)
    answer = _dispatcher(tmp_path, host).acquire("h3")
    assert (answer["state"], answer["reason"]) == ("wait", "чужой H3 на :30020")
    assert host.spawned == []


def test_a_busy_generation_lock_means_wait(tmp_path, host):
    with _external_lock(tmp_path, "LOCK_EX", name="generation.lock"):
        answer = _dispatcher(tmp_path, host).acquire("h3")
    assert (answer["state"], answer["reason"]) == ("wait", "generation.lock занят")
    assert host.spawned == []


def test_release_kills_only_our_exact_group(tmp_path, host):
    d = _dispatcher(tmp_path, host)
    d.acquire("h3")
    host.apps = [{"pid": 4242, "name": "sglang", "memory_mb": 47000},
                 {"pid": 777, "name": "comfy-python", "memory_mb": 3000}]
    host.pgids[777] = 777
    assert d.release() == {"ok": True, "stopped": ["h3"]}
    assert host.killed == [(4242, signal.SIGTERM)]
    assert all(pgid != 777 for pgid, _ in host.killed)
    assert _lock_is_free(tmp_path / "generation.lock")


def test_a_group_that_ignores_sigterm_gets_sigkill_after_120_s(tmp_path, host):
    d = _dispatcher(tmp_path, host)
    d.acquire("h3")
    host.die_on_term = False
    start = host.t
    d.release()
    assert host.killed == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert host.t - start >= 120


def test_acquiring_ltx_stops_our_h3_first(tmp_path, host):
    d = _dispatcher(tmp_path, host)
    d.acquire("h3")
    host.apps = [{"pid": 4242, "name": "sglang", "memory_mb": 47000}]
    answer = d.acquire("ltx")
    assert answer["state"] == "starting"
    assert host.killed == [(4242, signal.SIGTERM)]
    assert [s[0] for s in host.spawned] == ["h3", "ltx"]
    assert host.spawned[1][:4] == (
        "ltx", ("/home/alex/Projects/comfy/.venv/bin/python", "main.py", "--port", "8188",
                "--output-directory", "/home/alex/Outputs/comfy/output", "--listen", "127.0.0.1",
                "--fast-disk", "--disable-auto-launch"),
        "/home/alex/Projects/comfy/ComfyUI", {})
    assert host.spawned[1][5] == 1


def test_an_engine_that_died_before_ready_is_failed_with_its_log(tmp_path, host):
    d = _dispatcher(tmp_path, host)
    log = d.acquire("h3")["log"]
    host.alive_groups.clear()
    host.cmdlines.clear()
    assert d.acquire("h3") == {"ok": True, "state": "failed", "engine": "h3", "log": log,
                               "reason": "движок не поднялся, смотрите лог"}
    assert _lock_is_free(tmp_path / "generation.lock")


def test_an_engine_not_ready_in_time_is_killed_and_failed(tmp_path, host):
    d = _dispatcher(tmp_path, host)
    log = d.acquire("h3")["log"]
    host.t += 451
    assert d.acquire("h3") == {"ok": True, "state": "failed", "engine": "h3", "log": log,
                               "reason": "движок не поднялся за 450 с, смотрите лог"}
    assert host.killed[0] == (4242, signal.SIGTERM)
    assert _lock_is_free(tmp_path / "generation.lock")


def test_restarted_dispatcher_still_owns_its_engine(tmp_path, host):
    """Review Focus 1."""
    _dispatcher(tmp_path, host).acquire("h3")
    host.ok_urls.add(H3_READY)
    reborn = _dispatcher(tmp_path, host)
    assert reborn.acquire("h3") == {"ok": True, "state": "ready", "engine": "h3"}
    assert len(host.spawned) == 1
    assert reborn.status()["own"]["h3"]["pid"] == 4242


def test_a_reused_pid_is_not_ours_and_is_never_killed(tmp_path, host):
    _dispatcher(tmp_path, host).acquire("h3")
    host.cmdlines[4242] = "vim notes.txt"
    reborn = _dispatcher(tmp_path, host)
    assert reborn.release() == {"ok": True, "stopped": []}
    assert host.killed == []


def test_qwen_unload_and_restore_only_on_request(tmp_path, host):
    d = _dispatcher(tmp_path, host)
    assert d.qwen_restore() == (409, {"ok": False, "error": {
        "code": "qwen_was_not_running", "message": "Qwen не был запущен до выгрузки"}})
    host.ok_urls.add(gd.QWEN_HEALTH)
    assert d.qwen_unload() == (200, {"ok": True, "was_running": True, "exit_code": 0})
    host.ok_urls.discard(gd.QWEN_HEALTH)
    d.acquire("h3")
    assert d.qwen_restore() == (200, {"ok": True, "state": "starting"})
    assert host.qwen_calls == ["stop", "start-shared32"]
    assert host.killed == [(4242, signal.SIGTERM)]       # our H3 released before Qwen returns
    assert d.status()["qwen"] == {"running": False, "unloaded_by_us": False}


def test_qwen_unload_when_it_is_not_running_does_nothing(tmp_path, host):
    assert _dispatcher(tmp_path, host).qwen_unload() == (200, {"ok": True, "was_running": False})
    assert host.qwen_calls == []


def test_status_shape(tmp_path, host):
    d = _dispatcher(tmp_path, host)
    d.acquire("h3")
    host.ok_urls.add(H3_READY)
    host.apps = [{"pid": 4242, "name": "sglang", "memory_mb": 47000}]
    status = d.status()
    assert status == {
        "ok": True,
        "own": {"h3": {"pid": 4242, "variant": "ref2va", "started_at": 1000.0,
                       "log": host.spawned[0][4], "ready": True}},
        "foreign": [], "qwen": {"running": False, "unloaded_by_us": False},
        "lock": {"held_by_us": True, "path": str(tmp_path / "generation.lock")},
        "gpu": {"temperature_c": 44, "memory_used_mb": 15, "memory_total_mb": 65536},
        "server_outputs_bytes": 123}


def test_http_layer(tmp_path, host):
    server = gd.make_server(_dispatcher(tmp_path, host), host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(path, body):
        request = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        assert post("/acquire", {"engine": "h3"})[1]["state"] == "starting"
        assert post("/acquire", {"engine": "sd"})[0] == 400
        assert post("/qwen/restore", {})[0] == 409
        with urllib.request.urlopen(base + "/status", timeout=5) as response:
            assert json.loads(response.read())["own"]["h3"]["pid"] == 4242
    finally:
        server.shutdown()
        server.server_close()


def test_systemd_unit_does_not_kill_spawned_engines_on_restart():
    unit = (ROOT / "tools" / "gpu-dispatcher" / "h3-gpu-dispatcher.service").read_text()
    lines = unit.splitlines()
    assert "KillMode=process" in lines
    assert "User=alex" in lines
    assert ("ExecStart=/usr/bin/python3 /home/alex/Projects/h3-panel/tools/gpu-dispatcher/"
            "dispatcher.py --host 127.0.0.1 --port 8790") in lines


_HOLD = ("import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\nsys.stdin.readline()\n")


def test_after_a_restart_switching_engines_frees_the_inherited_lock_first(tmp_path, host):
    """After a dispatcher restart the lock is held only by our own H3 (it inherited the fd).
    Switching to ltx must stop our H3 *before* trying the lock, or the panel waits on itself."""
    first = _dispatcher(tmp_path, host)
    first.acquire("h3")
    first.lock.release()                    # the old dispatcher process is gone ...
    holder = subprocess.Popen([sys.executable, "-c", _HOLD, str(tmp_path / "generation.lock")],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"   # ... "our H3" still holds the lock
    real_kill = host.killpg

    def kill(pgid, sig):
        real_kill(pgid, sig)
        if pgid == 4242 and holder.poll() is None:
            holder.stdin.write("\n")
            holder.stdin.flush()
            holder.wait(timeout=5)

    host.killpg = kill
    try:
        reborn = _dispatcher(tmp_path, host)
        assert reborn.acquire("ltx")["state"] == "starting"
        assert host.killed == [(4242, signal.SIGTERM)]
    finally:
        if holder.poll() is None:
            holder.kill()


def test_a_comfy_on_another_port_at_our_ltx_pid_is_not_ours(tmp_path, host):
    _dispatcher(tmp_path, host).acquire("ltx")
    host.cmdlines[4242] = host.exec_cmdlines["ltx"].replace("--port 8188", "--port 8189")
    reborn = _dispatcher(tmp_path, host)
    assert reborn.release() == {"ok": True, "stopped": []}
    assert host.killed == []


def test_nvidia_smi_down_is_wait_not_a_free_card(tmp_path, host):
    host.smi_down = "nvidia-smi вернул код 9"
    answer = _dispatcher(tmp_path, host).acquire("h3")
    assert answer == {"ok": True, "state": "wait", "engine": "h3", "foreign": [],
                      "reason": "nvidia-smi недоступен: nvidia-smi вернул код 9"}
    assert host.spawned == []


def test_status_with_nvidia_smi_down_has_null_gpu_and_the_error(tmp_path, host):
    host.smi_down = "nvidia-smi вернул код 9"
    status = _dispatcher(tmp_path, host).status()
    assert status["gpu"] is None
    assert status["foreign"] == []
    assert status["gpu_error"] == "nvidia-smi вернул код 9; nvidia-smi вернул код 9"


def test_empty_nvidia_smi_answer_is_an_error_not_an_index_error():
    with pytest.raises(gd.HostError):
        gd.parse_gpu_stats("")


def test_real_host_run_turns_failures_into_host_error(tmp_path):
    h = gd.Host()
    with pytest.raises(gd.HostError):
        h._run("/nonexistent/nvidia-smi")
    with pytest.raises(gd.HostError):
        h._run(sys.executable, "-c", "import sys; sys.exit(3)")


def test_http_unexpected_exception_is_a_500_json(tmp_path, host):
    d = _dispatcher(tmp_path, host)
    d.status = lambda: (_ for _ in ()).throw(RuntimeError('boom'))
    server = gd.make_server(d, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/status", timeout=5)
        assert caught.value.code == 500
        assert json.loads(caught.value.read())["error"] == {"code": "internal", "message": "boom"}
        d.acquire = lambda engine: (_ for _ in ()).throw(RuntimeError("bang"))
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/acquire",
                                         data=b'{"engine":"h3"}', method="POST")
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        assert (caught.value.code, json.loads(caught.value.read())["error"]["message"]) == (500, "bang")
    finally:
        server.shutdown()
        server.server_close()


def test_http_request_with_an_origin_header_is_403(tmp_path, host):
    server = gd.make_server(_dispatcher(tmp_path, host), host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for method, path, data in (("GET", "/status", None), ("POST", "/acquire", b'{"engine":"h3"}')):
            request = urllib.request.Request(base + path, data=data, method=method,
                                             headers={"Origin": "http://evil.example"})
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            assert caught.value.code == 403
        assert host.spawned == []
    finally:
        server.shutdown()
        server.server_close()


def test_qwen_restore_when_qwen_is_already_healthy_does_not_start_it(tmp_path, host):
    d = _dispatcher(tmp_path, host)
    host.ok_urls.add(gd.QWEN_HEALTH)
    d.qwen_unload()
    assert d.qwen_restore() == (200, {"ok": True, "state": "already_running"})
    assert host.qwen_calls == ["stop"]
    assert d.status()["qwen"]["unloaded_by_us"] is False


def test_first_seen_bookkeeping_happens_under_the_state_lock(tmp_path, host):
    d = _dispatcher(tmp_path, host)

    class Guarded(dict):
        def _check(self):
            assert d._state.locked(), "_first_seen touched without _state"

        def setdefault(self, *a):
            self._check()
            return super().setdefault(*a)

        def pop(self, *a):
            self._check()
            return super().pop(*a)

    d._first_seen = Guarded({1: 1.0})
    host.apps = [{"pid": 777, "name": "x", "memory_mb": 1}]
    host.pgids[777] = 777
    d.status()
    assert dict(d._first_seen) == {777: 1000.0}


def test_real_host_reaps_our_stopped_engine_instead_of_waiting_for_sigkill(tmp_path, monkeypatch):
    """Our engine is the dispatcher's own child; after SIGTERM it is a zombie until waited for,
    and killpg(pgid, 0) succeeds on a zombie. _stop used to sit out all 120 s and SIGKILL."""
    import time as _time
    monkeypatch.setattr(gd, "STOP_GRACE_SECONDS", 3.0)

    class RealHost(gd.Host):
        sent = []

        def gpu_apps(self):
            return []

        def gpu_stats(self):
            return {}

        def url_ok(self, url):
            return False

        def cmdline(self, pid):
            proc = self._procs.get(pid)
            return "sleep 1000" if proc is not None and proc.poll() is None else None

        def killpg(self, pgid, sig):
            self.sent.append(sig)
            try:
                super().killpg(pgid, sig)
            except PermissionError:      # macOS: killpg on a zombie group
                pass

    spec = gd.EngineSpec(name="h3", cmd=("sleep", "1000"), cwd=tmp_path, markers=("sleep",),
                         log_dir=tmp_path, log_prefix="t", variant="ref2va", label="H3")
    real = RealHost()
    d = gd.Dispatcher(host=real, specs={"h3": spec}, state_path=tmp_path / "s.json",
                      lock_path=tmp_path / "generation.lock", server_outputs=tmp_path / "so")
    assert d.acquire("h3")["state"] == "starting"
    pid = d.status()["own"]["h3"]["pid"]
    started = _time.monotonic()
    try:
        assert d.release() == {"ok": True, "stopped": ["h3"]}
        assert _time.monotonic() - started < 2.5
        assert RealHost.sent == [signal.SIGTERM]
    finally:
        try:
            import os as _os
            _os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):   # already reaped / zombie
            pass
