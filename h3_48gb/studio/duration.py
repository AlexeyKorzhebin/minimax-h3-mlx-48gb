"""Scene length (spec §6.2): `requested_s` is kept as typed; `frames` is the nearest point of the
17n+5 grid at 24 fps (ties go up), clamped to [73, 345] = 3.042–14.375 s."""
from __future__ import annotations

from fractions import Fraction

from h3_48gb.engines.sglang_args import FPS, FRAME_REMAINDER, FRAMES_PER_CHUNK

MIN_FRAMES = 73
MAX_FRAMES = 345
MIN_SECONDS = 3.0
MAX_SECONDS = 15.0


def snap_frames(requested_s: float) -> int:
    # Fraction: 4.8125 s is exactly 115.5 frames, halfway between 107 and 124; float arithmetic
    # must not decide that tie.
    target = Fraction(requested_s) * FPS
    low = FRAME_REMAINDER + FRAMES_PER_CHUNK * int((target - FRAME_REMAINDER) // FRAMES_PER_CHUNK)
    high = low + FRAMES_PER_CHUNK
    frames = low if target - low < high - target else high
    return min(max(frames, MIN_FRAMES), MAX_FRAMES)


def seconds(frames: int) -> float:
    return frames / FPS
