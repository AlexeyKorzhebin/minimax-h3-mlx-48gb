"""Read runs off disk without loading a model.

A run leaves two kinds of artifact under `--outdir`: a resume checkpoint while it is in flight,
and a `<stem>.json` report once it finishes. Both are readable without MLX -- a safetensors file
is an 8-byte length, a JSON header, then tensor bytes this module never touches -- so a caller can
ask "how far along is it" for the price of a few hundred bytes, on a run this process did not
launch and does not own the terminal of.

Deliberately no dependency on MLX, the minimax-h3-mlx package, or h3_48gb's own pipeline module:
the point is that `h3 status` starts instantly and that these tests need no weights.
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path

#: safetensors headers here are JSON and small. A junk file read as one yields an absurd length
#: prefix, and reading that many bytes raises MemoryError rather than returning garbage -- which
#: no reasonable `except` clause on a parser would list. Bounding the read turns a crash into the
#: `unreadable` state every caller already handles. Mirrors `_MAX_HEADER_BYTES` in cli.py.
_MAX_HEADER_BYTES = 8 << 20

_META_KEY = "h3_checkpoint"


@dataclass
class Run:
    """One run, as far as the files on disk can describe it."""

    outdir: Path
    tag: str | None = None
    stem: str | None = None
    state: str = "unreadable"
    completed: int | None = None
    total: int | None = None
    identity_digest: str | None = None
    identity: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def fraction(self) -> float | None:
        if not self.total:
            return None
        return (self.completed or 0) / self.total


def read_checkpoint_meta(path: Path) -> dict:
    """The `h3_checkpoint` metadata block, or raise ValueError describing why not."""
    size = path.stat().st_size
    with open(path, "rb") as fh:
        length = struct.unpack("<Q", fh.read(8))[0]
        if length > min(_MAX_HEADER_BYTES, size):
            raise ValueError(f"header length {length} exceeds the file")
        header = json.loads(fh.read(length))
    raw = header.get("__metadata__", {})
    if _META_KEY not in raw:
        raise ValueError(f"no {_META_KEY!r} metadata")
    return json.loads(raw[_META_KEY])


def scan(root: Path) -> list[Run]:
    """Every run under `root`, found recursively. Never raises for a file it finds."""
    root = Path(root)
    runs: list[Run] = []
    for checkpoint in sorted(root.rglob("checkpoints/h3-*.safetensors")):
        outdir = checkpoint.parent.parent
        try:
            meta = read_checkpoint_meta(checkpoint)
            run = Run(
                outdir=outdir,
                state="in_flight",
                completed=int(meta.get("completed_steps", 0)),
                total=int(meta.get("total_forwards", 0)) or None,
                identity_digest=meta.get("identity_digest"),
                identity=meta.get("identity", {}),
            )
        except (OSError, ValueError, TypeError, AttributeError, KeyError, struct.error,
                MemoryError) as exc:
            # The bytes on disk are untrusted: a queue writes checkpoints by rename, so a reader
            # can see a truncated file (OSError/struct.error) or a header cut off mid-line
            # (ValueError from json.loads). But well-formed-JSON-with-a-surprising-shape is just
            # as real -- a review of this function found three cases live JSON never produces
            # from a healthy writer but a hand-edited or future-format file can: `h3_checkpoint`
            # nested as an object instead of the JSON string mx.save_safetensors always writes
            # (TypeError from the inner json.loads), `completed_steps: null` (TypeError from
            # int(None)), and a top-level header that parses to a list, not an object
            # (AttributeError from header.get). All of those are shape problems in one file, not
            # bugs in this loop, so they get folded into the same "unreadable" outcome. The catch
            # stays enumerated rather than `except Exception` on purpose: a typo in the Run(...)
            # call above -- a bad keyword, a NameError from a future edit -- must still crash
            # loudly instead of being misreported as a corrupt checkpoint.
            runs.append(Run(outdir=outdir, error=f"{type(exc).__name__}: {exc}"))
            continue
        runs.append(run)
    return runs
