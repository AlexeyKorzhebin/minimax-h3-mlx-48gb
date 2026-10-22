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
    args = parser.parse_args(argv)

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


if __name__ == "__main__":
    sys.exit(main())
