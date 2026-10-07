#!/usr/bin/env python3
"""gpu-dispatcher for the h3 panel on alex-neuro (spec §3.4). Stdlib only; runs on the host
under systemd, listens on 127.0.0.1:8790.

It is the only thing that starts or stops the panel's engines (H3 on sglang :30020, ComfyUI LTX
:8188). It starts them exactly as h3-bench/pipeline.sh does, but stops only what it started --
SIGTERM to its own process group, up to 120 s, then SIGKILL -- never `pkill -f`, which would take
down a foreign ComfyUI/H3. Everything else on the GPU is foreign: waited for, never touched.
Qwen is stopped and restarted only on an explicit request (a button press).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECTS = Path(os.environ.get("ALEX_PROJECTS_ROOT", "/home/alex/Projects"))
MODELS = Path(os.environ.get("ALEX_MODELS_ROOT", "/home/alex/Models"))
OUTPUTS = Path(os.environ.get("ALEX_OUTPUTS_ROOT", "/home/alex/Outputs"))
H3_BENCH = PROJECTS / "h3-bench"
COMFY = PROJECTS / "comfy"
QWEN_SH = PROJECTS / "qwen" / "qwen.sh"
GENERATION_LOCK = PROJECTS / "qwen-image21-lab" / "generation.lock"
SERVER_OUTPUTS = OUTPUTS / "h3-bench" / "server-outputs"
STATE_PATH = Path(os.environ.get(
    "H3_DISPATCHER_STATE", str(Path.home() / ".local/state/h3-gpu-dispatcher/state.json")))
QWEN_HEALTH = "http://127.0.0.1:8000/health"
STOP_GRACE_SECONDS = 120.0


@dataclass(frozen=True)
class EngineSpec:
    name: str
    cmd: tuple
    cwd: Path
    env: dict = field(default_factory=dict)
    ready_url: str = ""
    port: int = 0
    markers: tuple = ()
    log_dir: Path = Path(".")
    log_prefix: str = ""
    start_timeout: float = 600.0
    variant: str | None = None
    label: str = ""


def engine_specs() -> dict[str, EngineSpec]:
    te = MODELS / ("Video/MiniMax-H3/runtime-components/text_encoders/"
                   "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors")
    return {
        "h3": EngineSpec(
            name="h3", cmd=("bash", str(H3_BENCH / "serve.sh")), cwd=H3_BENCH,
            env={"VARIANT": "ref2va", "TE": str(te)},
            ready_url="http://127.0.0.1:30020/v1/models", port=30020,
            markers=("sglang", " serve ", "--model-variant ref2va", "--port 30020"),
            log_dir=H3_BENCH / "logs",
            log_prefix="serve-panel", start_timeout=450.0, variant="ref2va", label="H3"),
        "ltx": EngineSpec(
            name="ltx",
            cmd=(str(COMFY / ".venv/bin/python"), "main.py", "--port", "8188",
                 "--output-directory", str(OUTPUTS / "comfy/output"), "--listen", "127.0.0.1",
                 "--fast-disk", "--disable-auto-launch"),
            cwd=COMFY / "ComfyUI", ready_url="http://127.0.0.1:8188/system_stats", port=8188,
            markers=("main.py", "--port 8188"), log_dir=COMFY / "logs", log_prefix="comfy-panel",
            start_timeout=600.0, label="ComfyUI"),
    }


class HostError(Exception):
    """The machine could not tell us something we must not guess (nvidia-smi is down)."""


def parse_compute_apps(text: str) -> list[dict]:
    apps = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        apps.append({"pid": int(parts[0]), "name": parts[1], "memory_mb": int(float(parts[2]))})
    return apps


def parse_gpu_stats(text: str) -> dict:
    try:
        first = text.strip().splitlines()[0]
        temperature, used, total = (int(float(part.strip())) for part in first.split(","))
    except (IndexError, ValueError) as exc:
        raise HostError(f"nvidia-smi: непонятный ответ {text.strip()[:80]!r}") from exc
    return {"temperature_c": temperature, "memory_used_mb": used, "memory_total_mb": total}


class GenerationLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd: int | None = None

    @property
    def held(self) -> bool:
        return self.fd is not None

    def try_acquire(self) -> bool:
        if self.fd is not None:
            return True
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o664)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Host:
    """Everything that touches the real machine. Tests replace it whole."""

    def __init__(self):
        self._procs: dict[int, subprocess.Popen] = {}

    def _run(self, *cmd) -> str:
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            raise HostError(f"{cmd[0]} недоступен: {exc}") from exc
        if done.returncode != 0:
            raise HostError(f"{cmd[0]} вернул код {done.returncode}: {done.stderr.strip()[:200]}")
        return done.stdout

    def gpu_apps(self) -> list[dict]:
        return parse_compute_apps(self._run("nvidia-smi", "--query-compute-apps=pid,process_name,"
                                            "used_memory", "--format=csv,noheader,nounits"))

    def gpu_stats(self) -> dict:
        return parse_gpu_stats(self._run("nvidia-smi", "--query-gpu=temperature.gpu,memory.used,"
                                         "memory.total", "--format=csv,noheader,nounits"))

    def url_ok(self, url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                return response.status == 200
        except Exception:  # noqa: BLE001 -- any failure is "not answering"
            return False

    def spawn(self, spec: EngineSpec, log_path: Path, pass_fds) -> tuple[int, int]:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(list(spec.cmd), cwd=spec.cwd, env={**os.environ, **spec.env},
                                    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True, pass_fds=tuple(pass_fds))
        self._procs[proc.pid] = proc     # we are its parent: it must be reaped, or it stays a zombie
        return proc.pid, os.getpgid(proc.pid)

    def cmdline(self, pid: int) -> str | None:
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
        except OSError:
            return None

    def pgid_of(self, pid: int) -> int | None:
        try:
            return os.getpgid(pid)
        except ProcessLookupError:
            return None

    def killpg(self, pgid: int, sig: int) -> None:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass

    def group_alive(self, pgid: int) -> bool:
        # A dead leader that nobody waited for is a zombie, and killpg(pgid, 0) succeeds on a
        # zombie -- reap our own child first so "dead" is reported as dead.
        proc = self._procs.get(pgid)
        if proc is not None:
            if proc.poll() is not None:
                del self._procs[pgid]
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def run_qwen(self, action: str) -> int:
        return subprocess.run(["bash", str(QWEN_SH), action], timeout=300).returncode

    def start_qwen(self) -> None:
        subprocess.Popen(["bash", str(QWEN_SH), "start-shared32"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def dir_size(self, path: Path) -> int:
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total

    monotonic = staticmethod(time.monotonic)
    wall = staticmethod(time.time)
    sleep = staticmethod(time.sleep)


class Dispatcher:
    """Two locks, on purpose (review): `_ops` serialises the long operations -- acquire, release,
    Qwen -- which may sleep up to 120 s while a group dies or wait on `qwen.sh stop`; `_state`
    guards only reads and writes of `self.state` and is never held across a kill, a sleep or a
    subprocess. `/status` takes `_state` alone, so it answers while a release is in progress."""

    def __init__(self, *, host, specs: dict, state_path: Path, lock_path: Path,
                 server_outputs: Path):
        self.host = host
        self.specs = specs
        self.state_path = Path(state_path)
        self.lock = GenerationLock(lock_path)
        self.server_outputs = Path(server_outputs)
        self._ops = threading.Lock()
        self._state = threading.Lock()
        self._first_seen: dict[int, float] = {}
        self.state = self._load_state()

    # -- state ------------------------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        state.setdefault("engines", {})
        state.setdefault("qwen_was_running", False)
        return state

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _snapshot(self) -> dict:
        with self._state:
            return json.loads(json.dumps(self.state))

    def _update(self, mutate) -> None:
        with self._state:
            mutate(self.state)
            self._save_state()

    def _alive(self, name: str, record: dict) -> bool:
        """Ours = the pid recorded in state.json AND its /proc cmdline carries every marker of
        the engine. serve.sh `exec`s `sglang serve ...` (serve.sh:12), so the pid Popen returned
        *is* sglang; requiring `--model-variant ref2va --port 30020` also proves the variant."""
        command = self.host.cmdline(record["pid"])
        return bool(command) and all(marker in command for marker in self.specs[name].markers)

    def _own_records(self, state: dict) -> dict:
        return {name: record for name, record in state["engines"].items()
                if self._alive(name, record)}

    def _foreign(self, own: dict) -> list[dict]:
        own_pgids = {record["pgid"] for record in own.values()}
        foreign = [app for app in self.host.gpu_apps()
                   if self.host.pgid_of(app["pid"]) not in own_pgids]
        now = self.host.wall()
        seen = {app["pid"] for app in foreign}
        with self._state:      # /status and acquire both land here from different threads
            for pid in list(self._first_seen):
                if pid not in seen:
                    self._first_seen.pop(pid, None)
            return [{**app, "first_seen": self._first_seen.setdefault(app["pid"], now)}
                    for app in foreign]

    def _stop(self, name: str, record: dict) -> None:
        """Called with `_ops` held and `_state` free: kill our group, wait, then forget it."""
        if self._alive(name, record):
            pgid = record["pgid"]
            self.host.killpg(pgid, signal.SIGTERM)
            deadline = self.host.monotonic() + STOP_GRACE_SECONDS
            while self.host.group_alive(pgid) and self.host.monotonic() < deadline:
                self.host.sleep(1.0)
            if self.host.group_alive(pgid):
                self.host.killpg(pgid, signal.SIGKILL)
        self._update(lambda state: state["engines"].pop(name, None))

    def _release_lock_if_idle(self) -> None:
        """No live engine of ours left means nobody inherited the lock fd: let go of it."""
        if not self._own_records(self._snapshot()):
            self.lock.release()

    def _release_all(self) -> list[str]:
        state = self._snapshot()
        stopped = sorted(self._own_records(state))
        for name, record in state["engines"].items():
            self._stop(name, record)
        self.lock.release()
        return stopped

    # -- handles ----------------------------------------------------------------------------
    def status(self) -> dict:
        state = self._snapshot()
        own = {}
        for name, record in self._own_records(state).items():
            own[name] = {"pid": record["pid"], "variant": record.get("variant"),
                         "started_at": record["started_at"], "log": record["log"],
                         "ready": self.host.url_ok(self.specs[name].ready_url)}
        errors = []
        try:
            foreign = self._foreign(self._own_records(state))
        except HostError as exc:
            foreign = []
            errors.append(str(exc))
        try:
            gpu = self.host.gpu_stats()
        except HostError as exc:
            gpu = None
            errors.append(str(exc))
        answer = {"ok": True, "own": own, "foreign": foreign,
                  "qwen": {"running": self.host.url_ok(QWEN_HEALTH),
                           "unloaded_by_us": bool(state["qwen_was_running"])},
                  "lock": {"held_by_us": bool(own) or self.lock.held, "path": str(self.lock.path)},
                  "gpu": gpu, "server_outputs_bytes": self.host.dir_size(self.server_outputs)}
        if errors:
            answer["gpu_error"] = "; ".join(errors)
        return answer

    def acquire(self, engine: str) -> dict:
        with self._ops:
            spec = self.specs[engine]
            state = self._snapshot()
            record = state["engines"].get(engine)
            if record and not self._alive(engine, record):
                self._update(lambda st: st["engines"].pop(engine, None))
                self._release_lock_if_idle()
                if not record.get("ready"):
                    return {"ok": True, "state": "failed", "engine": engine, "log": record["log"],
                            "reason": "движок не поднялся, смотрите лог"}
                record = None
            if record:
                if self.host.url_ok(spec.ready_url):
                    self._update(lambda st: st["engines"][engine].__setitem__("ready", True))
                    return {"ok": True, "state": "ready", "engine": engine}
                if self.host.wall() - record["started_at"] > spec.start_timeout:
                    self._stop(engine, record)
                    self._release_lock_if_idle()
                    return {"ok": True, "state": "failed", "engine": engine, "log": record["log"],
                            "reason": f"движок не поднялся за {spec.start_timeout:g} с, "
                                      f"смотрите лог"}
                return {"ok": True, "state": "starting", "engine": engine, "log": record["log"]}
            own = self._own_records(state)
            try:
                foreign = self._foreign(own)
            except HostError as exc:
                return {"ok": True, "state": "wait", "engine": engine,
                        "reason": f"nvidia-smi недоступен: {exc}", "foreign": []}
            if self.host.url_ok(QWEN_HEALTH):
                return {"ok": True, "state": "wait_qwen", "engine": engine,
                        "reason": "Qwen держит карту", "foreign": foreign}
            if foreign:
                names = ", ".join(f"{a['name']} (pid {a['pid']}, {a['memory_mb']} МБ)"
                                  for a in foreign)
                return {"ok": True, "state": "wait", "engine": engine,
                        "reason": f"GPU занята: {names}", "foreign": foreign}
            if self.host.url_ok(spec.ready_url):
                return {"ok": True, "state": "wait", "engine": engine,
                        "reason": f"чужой {spec.label} на :{spec.port}", "foreign": []}
            for other, other_record in own.items():
                if other != engine:
                    self._stop(other, other_record)
            if not self.lock.try_acquire():
                return {"ok": True, "state": "wait", "engine": engine,
                        "reason": "generation.lock занят", "foreign": []}
            stamp = datetime.fromtimestamp(self.host.wall()).strftime("%Y%m%d-%H%M%S")
            log_path = spec.log_dir / f"{spec.log_prefix}-{stamp}.log"
            pid, pgid = self.host.spawn(spec, log_path, pass_fds=(self.lock.fd,))
            new = {"pid": pid, "pgid": pgid, "variant": spec.variant,
                   "started_at": self.host.wall(), "log": str(log_path), "ready": False}
            self._update(lambda st: st["engines"].__setitem__(engine, new))
            return {"ok": True, "state": "starting", "engine": engine, "log": str(log_path)}

    def release(self) -> dict:
        with self._ops:
            return {"ok": True, "stopped": self._release_all()}

    def qwen_unload(self) -> tuple[int, dict]:
        with self._ops:
            if not self.host.url_ok(QWEN_HEALTH):
                return 200, {"ok": True, "was_running": False}
            self._update(lambda st: st.__setitem__("qwen_was_running", True))
            code = self.host.run_qwen("stop")
            return 200, {"ok": code == 0, "was_running": True, "exit_code": code}

    def qwen_restore(self) -> tuple[int, dict]:
        with self._ops:
            if not self._snapshot()["qwen_was_running"]:
                return 409, {"ok": False, "error": {"code": "qwen_was_not_running",
                                                    "message": "Qwen не был запущен до выгрузки"}}
            if self.host.url_ok(QWEN_HEALTH):      # someone already brought it back
                self._update(lambda st: st.__setitem__("qwen_was_running", False))
                return 200, {"ok": True, "state": "already_running"}
            self._release_all()
            self.host.start_qwen()
            self._update(lambda st: st.__setitem__("qwen_was_running", False))
            return 200, {"ok": True, "state": "starting"}


def make_server(dispatcher: Dispatcher, host: str = "127.0.0.1", port: int = 8790):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status: int, body: dict) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            data = json.loads(self.rfile.read(length))
            return data if isinstance(data, dict) else {}

        def _forbidden(self) -> bool:
            # A browser page on this very machine can reach 127.0.0.1; ours never sends Origin.
            if self.headers.get("Origin") is None:
                return False
            self._send(403, {"ok": False, "error": {"code": "forbidden",
                                                    "message": "запросы из браузера не принимаются"}})
            return True

        def do_GET(self):
            if self._forbidden():
                return
            try:
                if self.path == "/status":
                    return self._send(200, dispatcher.status())
                self._send(404, {"ok": False, "error": {"code": "not_found", "message": self.path}})
            except Exception as exc:  # noqa: BLE001 -- answer JSON, do not drop the connection
                self._send(500, {"ok": False, "error": {"code": "internal", "message": str(exc)}})

        def do_POST(self):
            if self._forbidden():
                return
            try:
                self._post()
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"ok": False, "error": {"code": "internal", "message": str(exc)}})

        def _post(self):
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"ok": False, "error": {"code": "bad_json",
                                                               "message": "тело не JSON"}})
            if self.path == "/acquire":
                engine = body.get("engine")
                if engine not in dispatcher.specs:
                    return self._send(400, {"ok": False, "error": {
                        "code": "unknown_engine", "message": f"движок {engine!r}: h3 или ltx"}})
                return self._send(200, dispatcher.acquire(engine))
            if self.path == "/release":
                return self._send(200, dispatcher.release())
            if self.path == "/qwen/unload":
                return self._send(*dispatcher.qwen_unload())
            if self.path == "/qwen/restore":
                return self._send(*dispatcher.qwen_restore())
            self._send(404, {"ok": False, "error": {"code": "not_found", "message": self.path}})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args(argv)
    dispatcher = Dispatcher(host=Host(), specs=engine_specs(), state_path=STATE_PATH,
                            lock_path=GENERATION_LOCK, server_outputs=SERVER_OUTPUTS)
    server = make_server(dispatcher, args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
