"""Crop window / paste back -- the CPU geometry half of the face-refine pipeline.

`crop_window` and `paste_back` never touch a real detector: every track here is built directly
with `facetrack._build_track` (the same seam `test_facetrack.py` uses to test track math without
`cv2`'s YuNet), so this module's own tests are about the crop/paste geometry and blending, not
about detection at all.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from h3_48gb import facepaste as fp
from h3_48gb import facetrack as ft


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------

def _dense_track(n_frames: int, box: tuple[float, float, float, float]) -> ft.FaceTrack:
    """A track holding the same `box` on *every* frame, with `every=1` so `detected()` is True
    everywhere -- the track shape the round-trip and clip tests want: fade never engages, so a
    disagreement between the crop and the paste is purely about the crop/resize/blend geometry
    itself, not about the fade_out ramp `_sparse_track` below is built to exercise.
    """
    samples = [(i, box) for i in range(n_frames)]
    track = ft._build_track(samples, n_frames=n_frames, every=1)
    assert track is not None
    return track


def _sparse_track(n_frames: int, box: tuple[float, float, float, float],
                   anchor: int, every: int) -> ft.FaceTrack:
    """A track with exactly one real detection, at frame `anchor` -- `detected(i)` is True only
    within `every` frames of `anchor` (see `facetrack.FaceTrack.detected`'s own docstring: the
    "near" window is `±every` around each anchor, not just the anchor frame itself) and False
    everywhere else. Callers that want a *single*-frame True island, to keep "distance from
    frame_idx to `anchor`" arithmetic simple in a test, pass `every=0`.
    """
    track = ft._build_track([(anchor, box)], n_frames=n_frames, every=every)
    assert track is not None
    return track


def _synthetic_frames(n_frames: int, height: int, width: int, seed: int = 20260817) -> np.ndarray:
    """`n_frames` RGB uint8 frames with real texture (a smooth gradient plus noise), not a flat
    color: a flat-color frame round-trips a Lanczos resize *exactly*, which would make the
    round-trip test's "close but not bit-exact inside the mask" claim vacuously true instead of
    actually exercising the resize.
    """
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width]
    gradient = (
        (xx.astype(np.float64) / max(width - 1, 1)) * 120.0
        + (yy.astype(np.float64) / max(height - 1, 1)) * 80.0
    )
    frames = np.empty((n_frames, height, width, 3), dtype=np.uint8)
    for i in range(n_frames):
        noise = rng.integers(0, 40, size=(height, width, 3))
        channel_shift = np.array([0, 20, 50]) + i  # a little per-frame, per-channel drift
        frame = gradient[:, :, None] + noise + channel_shift[None, None, :]
        frames[i] = np.clip(frame, 0, 255).astype(np.uint8)
    return frames


# --------------------------------------------------------------------------------------------
# `_round_rect` / `_clip_to_frame` -- private rectangle math
# --------------------------------------------------------------------------------------------

def test_round_rect_is_deterministic_and_matches_python_round():
    # x0=10.5 rounds to 10 (round-half-to-even: 10 is even), x0+w=30.5 rounds to 30 (even) too
    # -> width 20. y0=3.2 rounds to 3, y0+h=13.2 rounds to 13 -> height 10.
    assert fp._round_rect(10.5, 3.2, 20.0, 10.0) == (10, 3, 20, 10)


def test_round_rect_edges_never_cross_the_frame_bound_after_a_float_clip():
    # Regression check for the "each edge rounds independently" design: given a rectangle that
    # is already float-clipped to [0, 100), rounding x0 and x0+w separately must never push the
    # right edge past 100, even right at the boundary.
    ix0, iy0, iw, ih = fp._round_rect(0.0, 0.0, 99.6, 50.4)
    assert ix0 == 0 and iy0 == 0
    assert ix0 + iw <= 100
    assert iy0 + ih <= 100


def test_clip_to_frame_keeps_a_rect_that_already_fits_centered():
    x0, y0, w, h = fp._clip_to_frame(cx=50.0, cy=50.0, w=20.0, h=10.0,
                                      frame_w=200, frame_h=200)
    assert (x0, y0, w, h) == pytest.approx((40.0, 45.0, 20.0, 10.0))


def test_clip_to_frame_shifts_a_rect_that_would_spill_off_the_top_left():
    x0, y0, w, h = fp._clip_to_frame(cx=2.0, cy=2.0, w=20.0, h=20.0,
                                      frame_w=200, frame_h=200)
    assert x0 == pytest.approx(0.0)
    assert y0 == pytest.approx(0.0)
    assert (w, h) == pytest.approx((20.0, 20.0))


def test_clip_to_frame_shifts_a_rect_that_would_spill_off_the_bottom_right():
    x0, y0, w, h = fp._clip_to_frame(cx=198.0, cy=198.0, w=20.0, h=20.0,
                                      frame_w=200, frame_h=200)
    assert x0 + w == pytest.approx(200.0)
    assert y0 + h == pytest.approx(200.0)


def test_clip_to_frame_shrinks_a_rect_wider_than_the_whole_frame():
    x0, y0, w, h = fp._clip_to_frame(cx=50.0, cy=50.0, w=500.0, h=500.0,
                                      frame_w=100, frame_h=80)
    assert (w, h) == pytest.approx((100.0, 80.0))
    assert x0 == pytest.approx(0.0)
    assert y0 == pytest.approx(0.0)


# --------------------------------------------------------------------------------------------
# `_feather_ramp` / `_feather_mask`
# --------------------------------------------------------------------------------------------

def test_feather_ramp_is_flat_one_in_the_middle_and_near_zero_at_the_edges():
    ramp = fp._feather_ramp(100, feather=0.10)
    assert ramp[0] < 0.1
    assert ramp[-1] < 0.1
    assert ramp[50] == pytest.approx(1.0)
    # Monotonic rise from the left edge into the flat interior.
    assert np.all(np.diff(ramp[:10]) >= 0)


def test_feather_ramp_is_all_ones_when_feather_is_zero():
    ramp = fp._feather_ramp(50, feather=0.0)
    assert np.all(ramp == 1.0)


def test_feather_mask_is_the_outer_product_of_the_two_axis_ramps():
    mask = fp._feather_mask(20, 30, feather=0.10)
    assert mask.shape == (20, 30)
    expected = np.outer(fp._feather_ramp(20, 0.10), fp._feather_ramp(30, 0.10))
    assert np.allclose(mask, expected)


# --------------------------------------------------------------------------------------------
# `_fade_multipliers` -- the linear fade_out ramp over FADE_FRAMES
# --------------------------------------------------------------------------------------------

def test_fade_multipliers_is_one_at_and_near_the_one_real_detection():
    track = _sparse_track(n_frames=40, box=(0.0, 0.0, 10.0, 10.0), anchor=20, every=2)
    fade = fp._fade_multipliers(track)
    assert fade[20] == pytest.approx(1.0)
    assert fade[18] == pytest.approx(1.0)  # within `every` of the anchor: detected() is True
    assert fade[22] == pytest.approx(1.0)


def test_fade_multipliers_ramps_down_linearly_over_fade_frames_and_then_holds_zero():
    track = _sparse_track(n_frames=40, box=(0.0, 0.0, 10.0, 10.0), anchor=0, every=0)
    fade = fp._fade_multipliers(track)
    # detected() is True only at frame 0 here (every=0: a single-frame island); frame i>0 is
    # exactly i frames past the nearest True frame.
    for distance in range(1, fp.FADE_FRAMES + 1):
        frame_idx = distance  # frame 0 is the True anchor itself
        expected = max(0.0, 1.0 - distance / fp.FADE_FRAMES)
        assert fade[frame_idx] == pytest.approx(expected), f"frame {frame_idx}"
    assert fade[fp.FADE_FRAMES] == pytest.approx(0.0)
    assert fade[fp.FADE_FRAMES + 5] == pytest.approx(0.0)
    # Strictly decreasing across the ramp itself.
    ramp = fade[0:fp.FADE_FRAMES + 1]
    assert np.all(np.diff(ramp) <= 0)


# --------------------------------------------------------------------------------------------
# `crop_window`
# --------------------------------------------------------------------------------------------

def test_crop_window_produces_the_requested_out_size_for_every_frame():
    frames = _synthetic_frames(6, height=120, width=160)
    track = _dense_track(6, box=(60.0, 40.0, 30.0, 30.0))
    crops, geometry = fp.crop_window(frames, track, scale=2.0, out_size=(64, 48))
    assert crops.shape == (6, 48, 64, 3)
    assert crops.dtype == np.uint8
    assert geometry.n_frames == 6
    assert geometry.out_size == (64, 48)


def test_crop_window_clips_a_rect_that_would_spill_off_the_frame_edge():
    frames = _synthetic_frames(3, height=100, width=100)
    # Face box hard in the top-left corner: ideal scale=3 crop is far larger than fits there.
    track = _dense_track(3, box=(2.0, 2.0, 20.0, 20.0))
    crops, geometry = fp.crop_window(frames, track, scale=3.0, out_size=(64, 64))
    for frame_idx in range(3):
        x, y, w, h = geometry.rect(frame_idx)
        assert x >= 0 and y >= 0
        assert x + w <= 100
        assert y + h <= 100
        assert w > 0 and h > 0
    assert crops.shape == (3, 64, 64, 3)


def test_crop_window_rejects_too_few_frames():
    frames = _synthetic_frames(4, height=80, width=80)
    track = _dense_track(6, box=(30.0, 30.0, 10.0, 10.0))  # track expects 6 frames, only 4 given
    with pytest.raises(ValueError):
        fp.crop_window(frames, track)


def test_crop_window_rejects_too_many_frames():
    frames = _synthetic_frames(8, height=80, width=80)
    track = _dense_track(6, box=(30.0, 30.0, 10.0, 10.0))  # track expects 6 frames, 8 given
    with pytest.raises(ValueError):
        fp.crop_window(frames, track)


# --------------------------------------------------------------------------------------------
# `paste_back` -- round trip, edge clip, fade_out
# --------------------------------------------------------------------------------------------

def test_paste_back_round_trip_is_bit_exact_outside_the_crop_rect():
    frames = _synthetic_frames(4, height=120, width=160)
    track = _dense_track(4, box=(60.0, 40.0, 30.0, 30.0))
    crops, geometry = fp.crop_window(frames, track, scale=2.0, out_size=(64, 48))
    result = fp.paste_back(frames, crops, geometry, track, feather=0.10)
    assert result.shape == frames.shape
    for frame_idx in range(4):
        x, y, w, h = geometry.rect(frame_idx)
        outside = np.ones(frames[frame_idx].shape[:2], dtype=bool)
        outside[y:y + h, x:x + w] = False
        assert np.array_equal(result[frame_idx][outside], frames[frame_idx][outside])


def test_paste_back_round_trip_is_close_inside_the_crop_rect():
    frames = _synthetic_frames(4, height=120, width=160)
    track = _dense_track(4, box=(60.0, 40.0, 30.0, 30.0))
    crops, geometry = fp.crop_window(frames, track, scale=2.0, out_size=(64, 48))
    result = fp.paste_back(frames, crops, geometry, track, feather=0.10)
    for frame_idx in range(4):
        x, y, w, h = geometry.rect(frame_idx)
        # The center of the rect is fully inside the feather mask's flat interior (mask == 1),
        # so it went through a full-strength Lanczos up/down round trip: close, not bit-exact.
        cy, cx = y + h // 2, x + w // 2
        original = frames[frame_idx][cy, cx].astype(np.int16)
        pasted = result[frame_idx][cy, cx].astype(np.int16)
        assert np.abs(original - pasted).max() < 20


def test_paste_back_rejects_length_mismatch():
    frames = _synthetic_frames(4, height=80, width=80)
    track = _dense_track(4, box=(30.0, 30.0, 10.0, 10.0))
    crops, geometry = fp.crop_window(frames, track, scale=2.0, out_size=(32, 32))
    with pytest.raises(ValueError):
        fp.paste_back(frames[:3], crops, geometry, track)


def test_paste_back_fade_out_shrinks_the_blend_and_then_stops_pasting_entirely():
    height, width = 100, 100
    n_frames = 40
    # The *same* source content on every frame (not `_synthetic_frames(n_frames, ...)`, which
    # deliberately drifts color per frame): isolates the measured deviation to the fade weight
    # itself, since the box/geometry are also constant (`_sparse_track` extrapolates one box
    # everywhere) and only `_fade_multipliers` changes across frames.
    one_frame = _synthetic_frames(1, height=height, width=width)[0]
    frames = np.tile(one_frame[None], (n_frames, 1, 1, 1))
    track = _sparse_track(n_frames, box=(30.0, 30.0, 20.0, 20.0), anchor=0, every=0)
    crops, geometry = fp.crop_window(frames, track, scale=1.5, out_size=(48, 48))
    # A "refined" result that is as different from the source as possible (pure white), so any
    # surviving blend strength is visible as a jump toward 255 at the rect's center pixel.
    refined = np.full_like(crops, 255)
    result = fp.paste_back(frames, refined, geometry, track, feather=0.10)

    x, y, w, h = geometry.rect(0)
    cy, cx = y + h // 2, x + w // 2
    deviations = [
        int(result[i][cy, cx].astype(np.int16).sum())
        - int(frames[i][cy, cx].astype(np.int16).sum())
        for i in range(n_frames)
    ]
    # Frame 0 (the real detection) blends at full strength: a large deviation toward white.
    assert deviations[0] > 100
    # Deviation shrinks monotonically across the fade window...
    ramp = deviations[0:fp.FADE_FRAMES + 1]
    assert all(a >= b for a, b in zip(ramp, ramp[1:]))
    # ...and every frame FADE_FRAMES or more past the anchor is untouched: bit-exact original.
    for i in range(fp.FADE_FRAMES, n_frames):
        assert np.array_equal(result[i], frames[i]), f"frame {i} should be untouched"


# --------------------------------------------------------------------------------------------
# Module hygiene
# --------------------------------------------------------------------------------------------

def test_facepaste_module_does_not_import_mlx():
    """`h3_48gb.facepaste` is a CPU-only module, same reasoning and same test shape as
    `facetrack.py`'s `test_facetrack_module_does_not_import_mlx` -- see that module's docstring.
    """
    code = "import sys; import h3_48gb.facepaste; print('mlx' in sys.modules)"
    project_root = Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            cwd=str(project_root))
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    assert result.stdout.strip() == "False", "importing h3_48gb.facepaste must not import mlx"
