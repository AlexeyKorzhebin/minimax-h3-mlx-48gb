"""Rolling the dispatcher back (plan S2–S3 revisions 2–3, Codex blocker 1) on stubs: `docker` and
`sudo` on PATH, and a dispatcher stub that behaves like the NEW version until the unit restart
and like the OLD one after it — the old one fails every /status and /acquire with KeyError while
state.json still holds `comfy-studio` (reproduced on the real old dispatcher.py in scratch).
The studio's engine is a real child process listening on a real port: the rollback must see it
gone (revision 3), not just its record."""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "studio_dispatcher_rollback.py"

#: A stand-in for the studio's ComfyUI: a process that listens on a port until it is killed.
ENGINE = ("import socket, sys, time\n"
          "s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
          "s.bind(('127.0.0.1', 0)); s.listen(); print(s.getsockname()[1], flush=True)\n"
          "time.sleep(600)\n")


class TwoDispatchers:
    def __init__(self, tmp: Path, *, kill_engine: bool = True, drop_record: bool = True,
                 acquire_state: str = "starting", panel_holds: str | None = None,
                 status_record: bool = True):
        self.engine = subprocess.Popen([sys.executable, "-c", ENGINE], stdout=subprocess.PIPE,
                                       text=True)
        self.port = int(self.engine.stdout.readline())
        self.state = tmp / "state.json"
        engines = {"comfy-studio": {"pid": self.engine.pid, "pgid": self.engine.pid,
                                    "owner": "studio-worker"}}
        if panel_holds:
            engines[panel_holds] = {"pid": 8, "pgid": 8, "owner": "panel-worker"}
        self.state.write_text(json.dumps({"engines": engines}))
        self.restarted = tmp / "restarted"          # written by the `sudo systemctl restart` stub
        self.kill_engine, self.drop_record = kill_engine, drop_record
        # revision 4: False — /status forgets a dead engine while state.json still holds it
        self.status_record = status_record
        self.acquire_state = acquire_state
        self.calls: list[str] = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def engines(self) -> dict:
        return json.loads(self.state.read_text())["engines"]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.engine.poll() is None:
            self.engine.kill()
        self.engine.wait()

    def _handler(fake):  # noqa: N805
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _old_and_broken(self) -> bool:
                return fake.restarted.exists() and "comfy-studio" in fake.engines()

            def do_GET(self):
                version = "old" if fake.restarted.exists() else "new"
                fake.calls.append(f"{version} GET {self.path}")
                if self._old_and_broken():
                    return self._send(500, {"ok": False, "error": {
                        "code": "internal", "message": "'comfy-studio'"}})
                self._send(200, {"ok": True, "own": {
                    k: {"owner": v["owner"]} for k, v in fake.engines().items()
                    if fake.status_record or k != "comfy-studio"}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                version = "old" if fake.restarted.exists() else "new"
                fake.calls.append(f"{version} POST {self.path} {body.get('engine', '')}"
                                  f"{' ' if body.get('engine') else ''}{body['client']}")
                if self._old_and_broken():
                    return self._send(500, {"ok": False, "error": {
                        "code": "internal", "message": "'comfy-studio'"}})
                if self.path == "/release":
                    engines = fake.engines()
                    mine = [k for k, v in engines.items() if v["owner"] == body["client"]]
                    if "comfy-studio" in mine and fake.kill_engine:
                        fake.engine.kill()
                        fake.engine.wait()
                    if fake.drop_record:
                        fake.state.write_text(json.dumps({"engines": {
                            k: v for k, v in engines.items() if k not in mine}}))
                    return self._send(200, {"ok": True, "stopped": mine})
                return self._send(200, {"ok": True, "state": fake.acquire_state,
                                        "engine": body["engine"],
                                        **({"reason": "GPU занята: qwen"}
                                           if fake.acquire_state == "wait" else {})})
        return Handler


def _stubs(tmp: Path, fake: TwoDispatchers) -> dict:
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    log = tmp / "commands.log"
    for name, extra in (("docker", ""), ("sudo", f'''
case "$*" in *"systemctl restart"*) touch {fake.restarted} ;; esac''')):
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\necho \"{name} $*\" >> {log}{extra}\nexit 0\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "LOG": str(log)}


def _run(fake: TwoDispatchers, env: dict, script: Path = SCRIPT):
    return subprocess.run([sys.executable, str(script), "--dispatcher", fake.url, "--state",
                           str(fake.state), "--compose", "compose.studio.yaml", "--override",
                           "/etc/x/override.conf", "--wait", "2", "--studio-port",
                           str(fake.port)],
                          env={**os.environ, "PATH": env["PATH"]}, capture_output=True, text=True)


@pytest.fixture
def rollback(tmp_path):
    made = []

    def make(**kwargs):
        fake = TwoDispatchers(tmp_path, **kwargs)
        made.append(fake)
        return fake, _stubs(tmp_path, fake)
    yield make
    for fake in made:
        fake.close()


def test_the_rollback_releases_the_studio_engine_before_the_old_dispatcher_returns(rollback):
    fake, env = rollback()
    done = _run(fake, env)
    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout == ("ok: worker stopped\nok: released ['comfy-studio']\n"
                           "ok: comfy-studio gone\nok: state.json: no comfy-studio record\n"
                           "ok: unit restored\n"
                           "ok: old dispatcher grants ltx to the panel (starting)\n")
    assert Path(env["LOG"]).read_text().splitlines() == [
        "docker compose -f compose.studio.yaml stop studio-worker",
        "sudo systemctl stop h3-gpu-dispatcher",
        "sudo rm -f /etc/x/override.conf", "sudo systemctl daemon-reload",
        "sudo systemctl restart h3-gpu-dispatcher"]
    assert fake.calls == ["new POST /release studio-worker", "new GET /status",
                          "old GET /status", "old POST /acquire ltx panel-worker",
                          "old POST /release panel-worker"]
    assert fake.engines() == {}
    assert fake.engine.poll() is not None              # the process itself is gone


def test_the_panels_own_engine_is_asked_for_and_left_running(rollback):
    fake, env = rollback(acquire_state="ready", panel_holds="h3")
    done = _run(fake, env)
    assert (done.returncode, done.stdout.splitlines()[-1]) == (
        0, "ok: old dispatcher grants h3 to the panel (ready)")
    assert fake.calls[-1] == "old POST /acquire h3 panel-worker"     # no release after it


def test_the_old_order_breaks_the_old_dispatcher_and_the_script_says_so(rollback, tmp_path):
    """The order Codex flagged — restore the unit first: the old dispatcher answers 500."""
    old_order = tmp_path / "old_order.py"
    text = SCRIPT.read_text()
    a = text.index('    status, body = http(base, "POST", "/release", {"client": STUDIO_CLIENT')
    b = text.index('    run(["sudo", "rm", "-f", override])')
    c = text.index('    done.append("unit restored")')
    old_order.write_text(text[:a] + text[b:c] + text[a:b] + text[c:])
    fake, env = rollback()
    done = _run(fake, env, old_order)
    assert done.returncode == 1
    assert done.stderr.startswith("ОТКАТ ОСТАНОВЛЕН: новый диспетчер: /release studio-worker "
                                  "ответил 500")


def test_a_release_that_leaves_the_record_stops_before_touching_the_unit(rollback):
    fake, env = rollback(drop_record=False)
    done = _run(fake, env)
    assert done.returncode == 1
    assert done.stderr == "ОТКАТ ОСТАНОВЛЕН: comfy-studio не ушёл за 2 с: запись в /status\n"
    assert Path(env["LOG"]).read_text().splitlines() == [
        "docker compose -f compose.studio.yaml stop studio-worker"]


def test_a_record_left_only_in_state_json_is_removed_while_the_unit_is_stopped(rollback):
    """Revision 4 (coordinator 23.10, review of task 1): the old dispatcher fails every path with
    KeyError on a `comfy-studio` record in state.json — so the script removes it itself, with
    the unit stopped (the new dispatcher cannot write it back), and keeps the panel's record."""
    fake, env = rollback(drop_record=False, status_record=False, panel_holds="h3",
                         acquire_state="ready")
    done = _run(fake, env)
    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout == ("ok: worker stopped\nok: released ['comfy-studio']\n"
                           "ok: comfy-studio gone\nok: state.json: comfy-studio removed\n"
                           "ok: unit restored\n"
                           "ok: old dispatcher grants h3 to the panel (ready)\n")
    assert fake.engines() == {"h3": {"pid": 8, "pgid": 8, "owner": "panel-worker"}}
    assert fake.calls[-2:] == ["old GET /status", "old POST /acquire h3 panel-worker"]


def test_a_record_gone_while_the_process_lives_stops_before_touching_the_unit(rollback):
    """Revision 3 (Codex r2, finding 1): the record can vanish while the engine's group lives on
    and keeps the card — the rollback checks the process and the port, not just JSON."""
    fake, env = rollback(kill_engine=False)
    done = _run(fake, env)
    assert done.returncode == 1
    assert done.stderr == (f"ОТКАТ ОСТАНОВЛЕН: comfy-studio не ушёл за 2 с: процесс pid "
                           f"{fake.engine.pid}\n")
    assert Path(env["LOG"]).read_text().splitlines() == [
        "docker compose -f compose.studio.yaml stop studio-worker"]


def test_a_port_still_taken_stops_before_touching_the_unit(rollback, tmp_path):
    fake, env = rollback()
    other = socket.socket()
    other.bind(("127.0.0.1", 0))
    other.listen()
    fake.port = other.getsockname()[1]           # someone else still answers on the port
    try:
        done = _run(fake, env)
    finally:
        other.close()
    assert (done.returncode, done.stderr) == (
        1, f"ОТКАТ ОСТАНОВЛЕН: comfy-studio не ушёл за 2 с: порт {fake.port} занят\n")


def test_an_old_dispatcher_that_only_says_wait_fails_the_rollback(rollback):
    """Revision 3: `wait` does not prove the panel can get the card back — only a grant does."""
    fake, env = rollback(acquire_state="wait")
    done = _run(fake, env)
    assert done.returncode == 1
    assert done.stderr == ("ОТКАТ ОСТАНОВЛЕН: старый диспетчер не выдал карту панели: /acquire "
                           "ltx ответил 200: {'ok': True, 'state': 'wait', 'engine': 'ltx', "
                           "'reason': 'GPU занята: qwen'}\n")
