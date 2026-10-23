"""`python -m h3_48gb.studio api|openapi` (spec §4.2)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from h3_48gb.studio import config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m h3_48gb.studio")
    sub = parser.add_subparsers(dest="command", required=True)
    api = sub.add_parser("api", help="FastAPI + master + SSE")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8766)
    api.add_argument("--outdir", default=os.environ.get("H3_OUTDIR"))
    spec = sub.add_parser("openapi", help="write the OpenAPI document the master's types use")
    spec.add_argument("--out", type=Path, required=True)
    work = sub.add_parser("worker", help="the task loop: GPU gate, executor, results (§4.2)")
    work.add_argument("--outdir", default=os.environ.get("H3_OUTDIR"))
    setting = sub.add_parser("setting", help="show or set a studio setting (comfy.url, …)")
    setting.add_argument("--outdir", default=os.environ.get("H3_OUTDIR"))
    setting.add_argument("key", nargs="?")
    setting.add_argument("value", nargs="?")
    args = parser.parse_args(argv)
    if args.command in ("worker", "setting"):
        try:
            outdir = config.resolve_outdir(args.outdir)
        except config.ConfigError as err:
            print(f"studio: {err}", file=sys.stderr)
            return 2
        return run_worker(outdir) if args.command == "worker" else \
            run_setting(outdir, args.key, args.value)

    from h3_48gb.studio.app import create_app
    if args.command == "openapi":
        with tempfile.TemporaryDirectory() as tmp:
            document = create_app(Path(tmp)).openapi()
        args.out.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
                            + "\n", encoding="utf-8")
        return 0
    try:
        outdir = config.resolve_outdir(args.outdir)
    except config.ConfigError as err:
        print(f"studio: {err}", file=sys.stderr)
        return 2
    import uvicorn
    uvicorn.run(create_app(outdir), host=args.host, port=args.port, log_level="info")
    return 0


def run_worker(outdir: Path) -> int:
    """One worker per outdir (`flock worker.lock`, like `worker.py:105` of the old panel)."""
    import fcntl
    import signal
    import threading

    from h3_48gb.studio.worker import build_worker

    home = config.studio_dir(outdir)
    home.mkdir(parents=True, exist_ok=True)
    lock = open(home / "worker.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("studio: второй воркер не нужен — worker.lock занят", file=sys.stderr)
        return 2
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        build_worker(outdir).run_forever(stop)
    except KeyboardInterrupt:
        pass
    return 0


def run_setting(outdir: Path, key: str | None, value: str | None) -> int:
    from h3_48gb.studio import db, settings

    home = config.studio_dir(outdir)
    home.mkdir(parents=True, exist_ok=True)
    conn = db.connect(home / "studio.sqlite")
    try:
        db.migrate(conn)
        if key is None:
            for name, current in settings.all_settings(conn).items():
                print(f"{name} = {current}")
            return 0
        if key not in settings.DEFAULTS:
            print(f"studio: нет настройки {key!r}; есть: {', '.join(settings.DEFAULTS)}",
                  file=sys.stderr)
            return 2
        if value is not None:
            settings.put(conn, key, value)
        print(f"{key} = {settings.get(conn, key)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
