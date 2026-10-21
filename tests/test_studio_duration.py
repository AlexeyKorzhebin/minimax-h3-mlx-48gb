"""§6.2: nearest 17n+5 grid point at 24 fps, ties up, clamped to [73, 345]."""
from __future__ import annotations

import pytest

from h3_48gb.studio.duration import snap_frames


@pytest.mark.parametrize("requested_s, frames", [
    (8.0, 192),      # already on the grid (Codex-13: not 197)
    (3.0, 73),       # 72 -> 73
    (10.0, 243),     # 240: 226 is 14 away, 243 is 3 away
    (4.8125, 124),   # 115.5 frames: exactly between 107 and 124 -> up
    (14.375, 345),   # the upper bound itself
    (15.0, 345),     # 360 -> 362 is above the bound -> clamped
    (0.5, 73),       # below the bound -> clamped
    (60.0, 345),
])
def test_snap(requested_s, frames):
    assert snap_frames(requested_s) == frames
