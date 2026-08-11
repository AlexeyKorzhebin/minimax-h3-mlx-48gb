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
from datetime import datetime
from pathlib import Path

#: safetensors headers here are JSON and small. A junk file read as one yields an absurd length
#: prefix, and reading that many bytes raises MemoryError rather than returning garbage -- which
#: no reasonable `except` clause on a parser would list. Bounding the read turns a crash into the
#: `unreadable` state every caller already handles. Mirrors `_MAX_HEADER_BYTES` in cli.py.
_MAX_HEADER_BYTES = 8 << 20

_META_KEY = "h3_checkpoint"

#: Three forwards absorbs one slow step; 120 s absorbs loading and decoding, during which the
#: writer is silent. Below this a run has started and stopped.
_STALE_GRACE_SECONDS = 120
_STALE_FORWARD_MULTIPLE = 3

#: Absolute ceiling for `_state_of`'s `rate is None` branch, where `started_at` is missing and so
#: there is no measured rate to build a `_STALE_FORWARD_MULTIPLE * rate` window from. A forward at
#: native resolution runs up to ~30 minutes (see the timing figures in `checkpoint.py`'s module
#: docstring), and a checkpoint can go quiet for a VAE decode on top of that before the next write.
#: Four hours clears both several times over, so a live run sitting on a slow step is never
#: declared dead just because its checkpoint predates `started_at` -- but it is still finite, so
#: `h3 watch` on a checkpoint that is actually abandoned terminates instead of polling it forever.
#: (Without this, "unknown" from a missing `started_at` was itself an unbounded window: every
#: checkpoint on the machine at the time this was written lacks `started_at`, which made `watch`
#: unable to end on any of them, ever.)
_UNKNOWN_MAX_AGE_SECONDS = 4 * 3600


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def _now() -> datetime:
    """Indirected so tests can pin it."""
    return datetime.now()


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
    started_at: str | None = None
    completed_at_start: int = 0
    written_at: str | None = None

    @property
    def fraction(self) -> float | None:
        if not self.total:
            return None
        return (self.completed or 0) / self.total

    @property
    def seconds_this_session(self) -> float | None:
        if not (self.started_at and self.written_at):
            return None
        return (_parse(self.written_at) - _parse(self.started_at)).total_seconds()

    @property
    def forwards_this_session(self) -> int:
        return (self.completed or 0) - self.completed_at_start

    @property
    def seconds_per_forward(self) -> float | None:
        """None, not a crash, in the window between a resume and its first write."""
        elapsed, done = self.seconds_this_session, self.forwards_this_session
        if elapsed is None or done <= 0:
            return None
        return elapsed / done

    @property
    def eta_seconds(self) -> float | None:
        rate = self.seconds_per_forward
        if rate is None or self.total is None:
            return None
        return rate * (self.total - (self.completed or 0))

    @property
    def age_seconds(self) -> float | None:
        if not self.written_at:
            return None
        return (_now() - _parse(self.written_at)).total_seconds()

    def as_dict(self) -> dict:
        """Flat JSON, absolute paths, computed fields included so a caller need not recompute."""
        return {
            "outdir": str(self.outdir), "tag": self.tag, "state": self.state,
            "completed": self.completed, "total": self.total, "fraction": self.fraction,
            "started_at": self.started_at, "written_at": self.written_at,
            "age_seconds": self.age_seconds,
            "seconds_per_forward": self.seconds_per_forward,
            "eta_seconds": self.eta_seconds,
            "identity_digest": self.identity_digest, "error": self.error,
        }


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


def _state_of(run: Run) -> str:
    """"unreadable" for a Run this can't be reached on -- construction already raised for that.

    An unknown rate must not become a guessed window: `rate or 0` used to fold "I don't know the
    speed" into "assume it is instant", which shrinks the window to the bare 120 s grace period --
    a quarter of a real ~600 s/forward silence -- and calls a live run `stale`. Killing a run that
    is actually in flight costs hours on a machine where one forward is ten minutes; reporting
    `unknown` for one extra poll costs nothing. "can't tell" and "confirmed dead" must not look the
    same, mirroring the same principle this project already applies to a checkpoint field it can't
    parse: `None`, not a guess.
    """
    age, rate = run.age_seconds, run.seconds_per_forward
    if age is None:
        # No `written_at` at all -- unlike `started_at` (added later for the session-rate
        # feature, and `None` on every checkpoint written before it), `written_at` has been set
        # on *every* call to `_write` since the checkpoint format was created; there is no path
        # in this fork that produces a checkpoint without one. So this is not "can't tell": a
        # file missing it is not a live checkpoint from current code, and calling it anything
        # that keeps `watch` polling would wait forever for a field nothing will ever supply.
        return "stale"
    if rate is None:
        # No rate -- only `started_at` is missing, which by itself might still mean "alive" (see
        # the docstring above). Without the ceiling below, though, that "might" never resolves:
        # `watch` would poll such a checkpoint forever. See `_UNKNOWN_MAX_AGE_SECONDS` for why
        # four hours is the cutoff.
        return "unknown" if age < _UNKNOWN_MAX_AGE_SECONDS else "stale"
    window = _STALE_FORWARD_MULTIPLE * rate + _STALE_GRACE_SECONDS
    return "in_flight" if age < window else "stale"


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
                completed=int(meta.get("completed_steps", 0)),
                total=int(meta.get("total_forwards", 0)) or None,
                identity_digest=meta.get("identity_digest"),
                identity=meta.get("identity", {}),
                started_at=meta.get("started_at"),
                completed_at_start=int(meta.get("completed_at_start", 0)),
                written_at=meta.get("written_at"),
            )
            # Computed here, inside the same protected try that builds `run`: `_state_of` reads
            # `age_seconds` and `seconds_per_forward`, both of which call `_parse` on caller-
            # controlled timestamp strings. A checkpoint with well-formed JSON but an unparsable
            # `started_at` must still cost one run, not the whole scan -- so this must not move
            # outside the `try` below, where it would defeat the very isolation this function
            # exists to provide.
            run.state = _state_of(run)
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
