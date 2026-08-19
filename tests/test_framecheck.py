"""Unit tests for `h3_48gb.framecheck`'s two corruption detectors, on synthetic frames.

No `ffmpeg`/`ffprobe`, no MLX -- pure numpy, matching the module's own "no `mlx` import, ever"
input contract, since it has to be importable from `h3_48gb.assemble` (worker-process, no GPU).
"""
from __future__ import annotations

import numpy as np
import pytest

from h3_48gb import framecheck

CANVAS = (512, 896)  # (H, W) -- DEFAULT_SCENE_CANVAS, so every configured seam falls in range


def _clean_frame(seed: int = 0) -> np.ndarray:
    """Smooth, low-variance noise around mid-grey -- far from `FILL_COLOR` and with no artificial
    discontinuity at any of `framecheck`'s configured tile seams.
    """
    rng = np.random.default_rng(seed)
    h, w = CANVAS
    frame = 128.0 + rng.normal(0.0, 5.0, size=(h, w, 3))
    return np.clip(frame, 0, 255).astype(np.uint8)


# -- zero-fill --------------------------------------------------------------------------------


def test_zero_fill_fraction_is_zero_on_a_clean_frame():
    frame = _clean_frame()
    assert framecheck.zero_fill_fraction(frame) < framecheck.ZERO_FILL_FRACTION_THRESHOLD


def test_zero_fill_rectangle_trips_the_threshold_and_is_frame_corrupt():
    """A single unwritten VAE tile's worth of `FILL_COLOR`, not the whole frame -- the "or a large
    rectangle" half of the task's own criterion, not just the all-flat-frame case.
    """
    frame = _clean_frame()
    frame[100:300, 100:300] = framecheck.FILL_COLOR  # 200x200 = 40,000 px >> 0.5% of 458,752

    fraction = framecheck.zero_fill_fraction(frame)

    assert fraction > framecheck.ZERO_FILL_FRACTION_THRESHOLD
    assert framecheck.is_frame_corrupt(frame)


def test_zero_fill_fraction_tolerates_the_documented_slop():
    """`FILL_TOLERANCE` (2) is uint8 rounding slop, not a loose match -- a fill color nudged by
    exactly the tolerance still counts; nudged one past it does not.
    """
    within = np.full((64, 64, 3), 0, dtype=np.uint8)
    within[:] = np.array(framecheck.FILL_COLOR) + framecheck.FILL_TOLERANCE
    assert framecheck.zero_fill_fraction(within) == pytest.approx(1.0)

    outside = np.full((64, 64, 3), 0, dtype=np.uint8)
    outside[:] = np.array(framecheck.FILL_COLOR) + framecheck.FILL_TOLERANCE + 1
    assert framecheck.zero_fill_fraction(outside) == pytest.approx(0.0)


# -- tile-seam score ----------------------------------------------------------------------------


def test_tile_seam_score_is_near_one_on_a_clean_frame():
    frame = _clean_frame()
    assert framecheck.tile_seam_score(frame) <= framecheck.TILE_SEAM_SCORE_THRESHOLD
    assert not framecheck.is_frame_corrupt(frame)


def test_tile_seam_score_flags_an_artificial_seam_at_a_configured_column():
    """A hard step exactly at one of `TILE_SEAM_COLUMNS` (320) -- everything from that column on is
    brightened by 60 levels -- must read as far more discontinuous at the seam than a few pixels
    either side of it.
    """
    frame = _clean_frame()
    x = 320
    assert x in framecheck.TILE_SEAM_COLUMNS
    shifted = frame.astype(np.int16)
    shifted[:, x:, :] = np.clip(shifted[:, x:, :] + 60, 0, 255)
    frame = shifted.astype(np.uint8)

    score = framecheck.tile_seam_score(frame)

    assert score > framecheck.TILE_SEAM_SCORE_THRESHOLD
    assert framecheck.is_frame_corrupt(frame)


def test_tile_seam_score_flags_an_artificial_seam_at_a_configured_row():
    frame = _clean_frame()
    y = 256
    assert y in framecheck.TILE_SEAM_ROWS
    shifted = frame.astype(np.int16)
    shifted[y:, :, :] = np.clip(shifted[y:, :, :] + 60, 0, 255)
    frame = shifted.astype(np.uint8)

    assert framecheck.tile_seam_score(frame) > framecheck.TILE_SEAM_SCORE_THRESHOLD


def test_tile_seam_score_returns_the_clean_value_when_no_configured_seam_fits():
    """A frame smaller than every configured seam position: nothing to measure is not evidence of
    corruption -- must degrade to the clean value (1.0), not raise or return a spurious score.
    """
    tiny = np.zeros((8, 8, 3), dtype=np.uint8)
    assert framecheck.tile_seam_score(tiny) == 1.0
    assert not framecheck.is_frame_corrupt(tiny)


# -- find_corrupt_frames / batch API --------------------------------------------------------------


def test_find_corrupt_frames_reports_only_the_bad_ones_with_their_reason():
    clean = _clean_frame(seed=1)
    zero_filled = _clean_frame(seed=2)
    zero_filled[:, :] = framecheck.FILL_COLOR
    seam = _clean_frame(seed=3)
    seam_i16 = seam.astype(np.int16)
    seam_i16[:, 640:, :] = np.clip(seam_i16[:, 640:, :] + 80, 0, 255)
    seam = seam_i16.astype(np.uint8)

    frames = np.stack([clean, zero_filled, seam, clean])
    bad = framecheck.find_corrupt_frames(frames)

    assert {b.index for b in bad} == {1, 2}
    by_index = {b.index: b for b in bad}
    assert by_index[1].zero_fill
    assert by_index[2].seam
    assert not by_index[2].zero_fill


def test_find_corrupt_frames_is_empty_for_an_all_clean_batch():
    frames = np.stack([_clean_frame(seed=i) for i in range(5)])
    assert framecheck.find_corrupt_frames(frames) == []
