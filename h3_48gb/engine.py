"""Which render engine this process drives (spec §3.3.1): `mlx` (the Mac, the default) or
`sglang` (alex-neuro). Read from `H3_ENGINE` on every call rather than cached at import, so a
test can flip it with monkeypatch and the web server and the worker can never disagree about a
value one of them read earlier.

No import from the rest of the package: `web`, `worker`, `assemble` and `cli` all read this.
"""
from __future__ import annotations

import os

ENGINES = ("mlx", "sglang")
DEFAULT_ENGINE = "mlx"


class UnknownEngine(ValueError):
    """`H3_ENGINE` names something outside `ENGINES`."""


def current(environ=None) -> str:
    raw = (os.environ if environ is None else environ).get("H3_ENGINE", "")
    value = raw.strip().lower() or DEFAULT_ENGINE
    if value not in ENGINES:
        raise UnknownEngine(f"H3_ENGINE={raw!r}: expected one of {ENGINES}")
    return value


def is_sglang(environ=None) -> bool:
    return current(environ) == "sglang"
