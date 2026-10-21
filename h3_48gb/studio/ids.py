"""Opaque ids and timestamps. Prefixes make a stray id in a log readable: p_ project, s_ scene,
c_ cast member, a_ asset, r_ proposal."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
