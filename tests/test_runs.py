"""`runs.scan` reads what is on disk without importing the pipeline.

The fixtures write safetensors files by hand -- an 8-byte little-endian header length, then the
JSON header, then nothing. That is a valid safetensors file with no tensors, and it is all `scan`
reads, which is the point: a run's progress must be knowable without loading 21 GB of weights.
"""
import json
import struct
from pathlib import Path

from h3_48gb.runs import Run, scan


def write_checkpoint(path: Path, *, completed: int, total: int, written_at: str,
                     identity: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "format": 1,
        "identity": identity or {"request": {"prompt": "a cat", "seed": 7}},
        "identity_digest": "deadbeef",
        "completed_steps": completed,
        "total_forwards": total,
        "written_at": written_at,
    }
    header = json.dumps({"__metadata__": {"h3_checkpoint": json.dumps(meta)}}).encode()
    path.write_bytes(struct.pack("<Q", len(header)) + header)
    return path


def test_a_checkpoint_is_read_into_a_run(tmp_path):
    write_checkpoint(tmp_path / "run-a" / "checkpoints" / "h3-abc.safetensors",
                     completed=3, total=7, written_at="2026-08-10T21:00:00")

    runs = scan(tmp_path)

    assert len(runs) == 1
    run = runs[0]
    assert run.completed == 3
    assert run.total == 7
    assert run.fraction == 3 / 7
    assert run.identity_digest == "deadbeef"


def test_a_corrupt_checkpoint_does_not_hide_its_neighbours(tmp_path):
    """One bad file must cost one run, not the whole scan.

    The failure this pins is not hypothetical: a checkpoint is written by rename, so a file caught
    mid-write is a real possibility on a machine running a queue.
    """
    (tmp_path / "run-bad" / "checkpoints").mkdir(parents=True)
    (tmp_path / "run-bad" / "checkpoints" / "h3-bad.safetensors").write_bytes(b"\x01\x02\x03")
    write_checkpoint(tmp_path / "run-good" / "checkpoints" / "h3-ok.safetensors",
                     completed=1, total=7, written_at="2026-08-10T21:00:00")

    runs = {r.outdir.name: r for r in scan(tmp_path)}

    assert runs["run-bad"].state == "unreadable"
    assert runs["run-bad"].error
    assert runs["run-good"].completed == 1, "a corrupt neighbour swallowed a good run"


def test_subdirectories_are_found(tmp_path):
    """The reason `scan` recurses at all: runs land in one directory per experiment."""
    write_checkpoint(tmp_path / "13-ladder" / "checkpoints" / "h3-a.safetensors",
                     completed=2, total=7, written_at="2026-08-10T21:00:00")
    write_checkpoint(tmp_path / "18-baseline" / "checkpoints" / "h3-b.safetensors",
                     completed=5, total=7, written_at="2026-08-10T21:00:00")

    assert {r.outdir.name for r in scan(tmp_path)} == {"13-ladder", "18-baseline"}


def test_runs_does_not_import_mlx():
    """The module's whole value is starting instantly and testing without weights.

    Checked by reading the source rather than by inspecting `sys.modules`, because another test in
    the same session will already have imported mlx and the check would pass vacuously.
    """
    import h3_48gb.runs

    source = Path(h3_48gb.runs.__file__).read_text()
    for forbidden in ("import mlx", "minimax_h3_mlx", "h3_48gb.pipeline"):
        assert forbidden not in source, f"runs.py must not depend on {forbidden}"
