"""How much a clip moves, and the LTX detailer strength that follows (spec §4.2.2), ported from
h3-bench/motion.py: mean absolute difference of neighbouring frames at 32x24 grey. Measured over
*all* parts of a clip together -- one strength for the whole clip, never per scene."""
from __future__ import annotations

import subprocess

import numpy as np

CALM, FAST = 3.0, 7.5
_W, _H = 32, 24


class MotionError(ValueError):
    """A part could not be measured. Silence would read as "no motion" and pick the strongest
    detailer strength (0.6) for a clip nobody looked at."""


def _part_totals(path, *, run) -> tuple[float, int]:
    result = run(["ffmpeg", "-v", "error", "-i", str(path), "-vf",
                  f"scale={_W}:{_H},format=gray", "-f", "rawvideo", "-"], capture_output=True)
    raw = result.stdout
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace") if isinstance(
            result.stderr, bytes) else str(result.stderr or "")
        raise MotionError(f"ffmpeg не прочитал {path}: {detail.strip()[:500]}")
    px = _W * _H
    frames = np.frombuffer(raw, dtype=np.uint8)[: len(raw) // px * px].reshape(-1, px)
    if len(frames) < 2:
        raise MotionError(f"в {path} меньше двух кадров ({len(frames)}), движение не измерить")
    diffs = np.abs(np.diff(frames.astype(np.int16), axis=0))
    return float(diffs.sum()), int(diffs.size)


def clip_motion(paths, *, run=subprocess.run) -> float:
    total, count = 0.0, 0
    for path in paths:
        part_total, part_count = _part_totals(path, run=run)
        total += part_total
        count += part_count
    return total / count if count else 0.0


def lora_for(m: float) -> float:
    if m < CALM:
        return 0.6
    if m < FAST:
        return 0.3
    return 0.15
