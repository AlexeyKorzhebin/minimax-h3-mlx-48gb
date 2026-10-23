#!/usr/bin/env python3
"""Roll the GPU dispatcher back from the studio's version to the old panel's one (plan S2–S3,
revision 2, Codex blocker 1). Order matters: the old dispatcher has no EngineSpec `comfy-studio`
and answers every /status and /acquire with KeyError while state.json still holds that engine
(`KillMode=process` keeps the engine alive across the restart, state.json keeps its record).

1. stop the studio worker — no new submit;
2. through the NEW dispatcher: release the client `studio-worker` — stops the studio's ComfyUI
   (and with it any running studio prompt) and drops its record from state.json;
3. wait until /status no longer knows `comfy-studio`, its recorded process is gone and nothing
   listens on its port (8189) — a record can vanish while the group lives on;
4. stop the unit and remove a `comfy-studio` record left in state.json ourselves (revision 4:
   the old dispatcher fails EVERY path with KeyError on it; with the unit stopped the new one
   cannot write it back; the other engines' records stay byte-equal in value);
5. remove the systemd override and restart the unit (the old panel's dispatcher.py, untouched);
6. check the old dispatcher: /status answers JSON, and /acquire as the panel's client is a GRANT
   (`ready` / `starting`); `wait` or anything else fails the rollback. The engine asked for is the
   one the panel already holds (then `ready`, nothing moves), or `ltx` on a free card (then
   `starting`, released again right away).

Exit 0 — rolled back and checked; 1 — a step failed (the message says which; nothing after it ran).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

STUDIO_CLIENT = "studio-worker"
PANEL_CLIENT = "panel-worker"
GRANT = ("ready", "starting")


class Failed(Exception):
    pass


def http(base: str, method: str, path: str, body: dict | None = None, timeout: float = 130.0):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, {"raw": exc.read().decode("utf-8", "replace")[:300]}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, {"raw": str(exc)}


def run(cmd: list[str]) -> None:
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        raise Failed(f"{' '.join(cmd)}: код {done.returncode}: {done.stderr.strip()[:300]}")


def _engines(state: Path) -> dict:
    try:
        return json.loads(state.read_text(encoding="utf-8")).get("engines", {})
    except FileNotFoundError:
        return {}


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def drop_studio_record(state: Path) -> bool:
    """Remove `comfy-studio` from state.json (the unit is stopped); True if it was there. Written
    through a temporary file and `os.replace`, so a crash leaves the old file or the new one."""
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    if "comfy-studio" not in data.get("engines", {}):
        return False
    del data["engines"]["comfy-studio"]
    tmp = state.with_name(state.name + ".rollback")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, state)
    return True


def studio_engine_gone(base: str, state: Path, pid: int | None, port: int) -> str | None:
    """None when the studio's engine is gone everywhere; else what still holds it."""
    status, body = http(base, "GET", "/status", timeout=10)
    if status != 200:
        raise Failed(f"новый диспетчер: /status ответил {status}: {body}")
    if "comfy-studio" in (body.get("own") or {}):
        return "запись в /status"
    if pid_alive(pid):
        return f"процесс pid {pid}"
    if port_open(port):
        return f"порт {port} занят"
    return None


def rollback(*, base: str, state: Path, compose: str, override: str, unit: str,
             wait_s: float, studio_port: int = 8189, sleep=time.sleep) -> list[str]:
    done = []
    run(["docker", "compose", "-f", compose, "stop", "studio-worker"])
    done.append("worker stopped")
    pid = (_engines(state).get("comfy-studio") or {}).get("pid")
    status, body = http(base, "POST", "/release", {"client": STUDIO_CLIENT, "all": False})
    if status != 200:
        raise Failed(f"новый диспетчер: /release {STUDIO_CLIENT} ответил {status}: {body}")
    done.append(f"released {body.get('stopped')}")
    deadline = time.monotonic() + wait_s
    while (left := studio_engine_gone(base, state, pid, studio_port)) is not None:
        if time.monotonic() > deadline:
            raise Failed(f"comfy-studio не ушёл за {wait_s:g} с: {left}")
        sleep(1.0)
    done.append("comfy-studio gone")
    run(["sudo", "systemctl", "stop", unit])
    done.append("state.json: comfy-studio removed" if drop_studio_record(state)
                else "state.json: no comfy-studio record")
    run(["sudo", "rm", "-f", override])
    run(["sudo", "systemctl", "daemon-reload"])
    run(["sudo", "systemctl", "restart", unit])
    done.append("unit restored")
    deadline = time.monotonic() + wait_s
    while True:
        status, body = http(base, "GET", "/status", timeout=10)
        if status == 200 and isinstance(body.get("own"), dict):
            break
        if time.monotonic() > deadline:
            raise Failed(f"старый диспетчер: /status ответил {status}: {body}")
        sleep(1.0)
    held = [name for name, rec in (body.get("own") or {}).items()
            if rec.get("owner") == PANEL_CLIENT]
    engine = held[0] if held else "ltx"
    status, body = http(base, "POST", "/acquire", {"engine": engine, "client": PANEL_CLIENT})
    if status != 200 or body.get("state") not in GRANT:
        raise Failed(f"старый диспетчер не выдал карту панели: /acquire {engine} ответил "
                     f"{status}: {body}")
    if not held:                  # we started it on a free card: give the card back
        http(base, "POST", "/release", {"client": PANEL_CLIENT, "all": False})
    done.append(f"old dispatcher grants {engine} to the panel ({body.get('state')})")
    return done


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dispatcher", default="http://127.0.0.1:8790")
    parser.add_argument("--state", type=Path,
                        default=Path.home() / ".local/state/h3-gpu-dispatcher/state.json")
    parser.add_argument("--compose", default="compose.studio.yaml")
    parser.add_argument("--override",
                        default="/etc/systemd/system/h3-gpu-dispatcher.service.d/override.conf")
    parser.add_argument("--unit", default="h3-gpu-dispatcher")
    parser.add_argument("--wait", type=float, default=150.0)
    parser.add_argument("--studio-port", type=int, default=8189)
    args = parser.parse_args(argv)
    try:
        for step in rollback(base=args.dispatcher.rstrip("/"), state=args.state,
                             compose=args.compose, override=args.override, unit=args.unit,
                             wait_s=args.wait, studio_port=args.studio_port):
            print(f"ok: {step}")
    except Failed as err:
        print(f"ОТКАТ ОСТАНОВЛЕН: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
