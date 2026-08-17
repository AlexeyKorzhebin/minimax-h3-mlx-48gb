"""The windowed v2v engine: window planning, crossfade compositing, partial-table selection.

Everything in here except the last test runs on the CPU with no weights on disk. That is the
point: the parts of `facerefine` that are easy to get silently wrong -- where the windows land,
which frames the crossfade ramp covers, which table name a sigma maps to -- are pure arithmetic,
and pinning them costs milliseconds instead of the 2.1 GPU-minutes one window actually takes.

The one GPU test is marked `slow` and skipped unless `H3_GPU_SMOKE=1` is set, because collecting
it into an ordinary `pytest tests/ -q` run would put a 2-minute, 28 GB job in the middle of a
suite that otherwise finishes in seconds.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from h3_48gb import facerefine
from h3_48gb.facerefine import (
    CROSSFADE_FRAMES,
    SIGMA_CEILING,
    WINDOW_FRAMES,
    WINDOW_STEP,
    Window,
    composite_windows,
    ensure_partial_table,
    native_frame_count,
    partial_sigmas,
    partial_table_name,
    plan_windows,
    refine_clip,
)


# -- the native 17k+5 grid -----------------------------------------------------------------------

def test_native_frame_count_rounds_a_length_down_to_the_grid_never_up():
    # 17k + 5: 5, 22, 39, 56, 73 ... A count rounded *up* would need frames that do not exist.
    assert native_frame_count(56) == 56
    assert native_frame_count(55) == 39
    assert native_frame_count(40) == 39
    assert native_frame_count(39) == 39
    assert native_frame_count(38) == 22
    assert native_frame_count(5) == 5


def test_native_frame_count_rejects_a_clip_below_the_shortest_native_length():
    with pytest.raises(ValueError, match="5"):
        native_frame_count(4)


def test_the_default_window_is_the_one_the_experiments_ran():
    # 56 = 17*3 + 5 -> 17 latent frames. Documented in docs/FEASIBILITY-face-refine.md.
    assert (WINDOW_FRAMES, WINDOW_STEP, CROSSFADE_FRAMES) == (56, 42, 12)
    assert WINDOW_FRAMES % 17 == 5


# -- window planning -----------------------------------------------------------------------------

def test_plan_windows_on_exactly_one_window_is_one_window():
    assert plan_windows(56) == [Window(0, 56)]


def test_plan_windows_reproduces_the_round_three_overlap_experiment():
    """84 frames -> windows 0-55 and 28-83, the exact pair round 3 measured the seam on."""
    plan = plan_windows(84)
    assert plan == [Window(0, 56), Window(28, 56)]
    assert plan[0].end - plan[1].start == 28          # the 28-frame overlap of the experiment


def test_plan_windows_covers_every_frame_of_a_long_clip_and_pins_the_last_window_to_the_tail():
    plan = plan_windows(361)
    assert [w.start for w in plan] == [0, 42, 84, 126, 168, 210, 252, 294, 305]
    assert all(w.length == 56 for w in plan)
    assert plan[-1].end == 361                        # the tail window is pinned, not padded
    covered = np.zeros(361, bool)
    for w in plan:
        covered[w.start:w.end] = True
    assert covered.all()


def test_plan_windows_pinned_tail_overlaps_more_never_less():
    plan = plan_windows(361)
    overlaps = [plan[i].end - plan[i + 1].start for i in range(len(plan) - 1)]
    assert min(overlaps) >= CROSSFADE_FRAMES
    assert overlaps[-1] == 45                         # 294+56 - 305, the squeezed tail


@pytest.mark.parametrize("num_frames", [56, 57, 70, 84, 97, 98, 99, 145, 240, 243, 361, 720])
def test_plan_windows_always_covers_the_whole_clip_with_room_for_the_crossfade(num_frames):
    plan = plan_windows(num_frames)
    assert plan[0].start == 0 and plan[-1].end == num_frames
    starts = [w.start for w in plan]
    assert starts == sorted(set(starts))              # strictly increasing, no duplicate window
    for i in range(len(plan) - 1):
        assert plan[i].end - plan[i + 1].start >= CROSSFADE_FRAMES


def test_plan_windows_on_a_clip_shorter_than_the_window_truncates_down_to_the_grid():
    """40 frames -> one 39-frame window; the odd frame at the tail is left to the source."""
    assert plan_windows(40) == [Window(0, 39)]
    assert plan_windows(39) == [Window(0, 39)]
    assert plan_windows(22) == [Window(0, 22)]


def test_plan_windows_rejects_a_clip_shorter_than_the_shortest_native_window():
    with pytest.raises(ValueError):
        plan_windows(4)


def test_plan_windows_rejects_a_window_length_off_the_native_grid():
    with pytest.raises(ValueError, match="17"):
        plan_windows(200, window=55)


def test_plan_windows_rejects_a_step_that_leaves_no_overlap_for_the_crossfade():
    with pytest.raises(ValueError, match="crossfade"):
        plan_windows(200, window=56, step=50, crossfade=12)


def test_plan_windows_rejects_a_nonpositive_or_oversized_step():
    with pytest.raises(ValueError):
        plan_windows(200, step=0)
    with pytest.raises(ValueError):
        plan_windows(200, step=57)


# -- crossfade compositing -----------------------------------------------------------------------

def _ramp(fade: int) -> np.ndarray:
    """The reference's own weights (round2/overlap/crossfade.py): (i + 1) / (fade + 1)."""
    return np.array([(i + 1) / (fade + 1) for i in range(fade)], np.float32)


def _flat(num_frames: int, value: int) -> np.ndarray:
    return np.full((num_frames, 6, 8, 3), value, np.uint8)


def test_composite_of_a_single_window_is_that_window_verbatim():
    source = _flat(56, 7)
    refined = [_flat(56, 200)]
    out = composite_windows(source, refined, plan_windows(56))
    assert out.dtype == np.uint8 and out.shape == source.shape
    assert (out == 200).all()


def test_composite_passes_frames_no_window_covers_through_byte_for_byte():
    rng = np.random.default_rng(0)
    source = rng.integers(0, 256, (40, 6, 8, 3), dtype=np.uint8)
    refined = [_flat(39, 200)]
    out = composite_windows(source, refined, plan_windows(40))
    assert (out[39] == source[39]).all()               # the un-refined tail frame is untouched
    assert (out[:39] == 200).all()


def test_composite_hands_over_through_the_reference_ramp_centred_in_the_overlap():
    source = _flat(84, 0)
    a, b = _flat(56, 10), _flat(56, 200)
    plan = plan_windows(84)
    out = composite_windows(source, [a, b], plan, crossfade=12)

    # overlap 28..55, fade 12 frames -> starts at 28 + (28 - 12)//2 = 36, exactly as the
    # experiment's FADE_START.
    assert (out[:36] == 10).all()
    assert (out[48:] == 200).all()
    weights = _ramp(12)
    expected = np.floor((1 - weights) * 10 + weights * 200 + 0.5).astype(np.uint8)
    assert (out[36:48, 0, 0, 0] == expected).all()


def test_composite_never_leaves_a_hard_cut_when_the_overlap_is_narrower_than_the_crossfade():
    """A pinned tail can overlap by less than `crossfade` only if a caller shrinks it; the fade
    then shrinks with the overlap rather than reading outside the window."""
    source = _flat(70, 0)
    plan = [Window(0, 56), Window(52, 18)]
    out = composite_windows(source, [_flat(56, 10), _flat(18, 200)], plan, crossfade=12)
    assert (out[:52] == 10).all()
    assert (out[56:] == 200).all()
    middle = out[52:56, 0, 0, 0].astype(int)
    assert 10 < middle.min() and middle.max() < 200     # a real ramp, not a cut
    assert list(middle) == sorted(middle)


def test_composite_rejects_a_window_whose_frames_do_not_match_the_plan():
    with pytest.raises(ValueError):
        composite_windows(_flat(84, 0), [_flat(56, 1), _flat(55, 2)], plan_windows(84))


# -- the partial AdaLN table ---------------------------------------------------------------------

def test_partial_table_name_is_the_two_decimal_sigma_the_experiments_baked():
    assert partial_table_name(0.25) == "adaln_face_s025_4pt_turbo.safetensors"
    assert partial_table_name(0.15) == "adaln_face_s015_4pt_turbo.safetensors"
    assert partial_table_name(0.2) == "adaln_face_s020_4pt_turbo.safetensors"


def test_partial_sigma_grid_matches_the_table_baked_for_sigma_025():
    """The s0.25 grid documented in the plan: [0.250, 0.181, 0.098, 0]."""
    grid = partial_sigmas(0.25, 4, 12.0)
    assert grid[0] == pytest.approx(0.25, abs=1e-6)
    assert [round(float(v), 3) for v in grid] == [0.250, 0.180, 0.098, 0.0]
    assert grid[-1] == 0.0
    assert list(grid) == sorted(grid, reverse=True)


def test_partial_sigma_grid_is_shifted_per_modality_off_one_base_grid():
    """Video (shift 12) and audio (shift 3) must sit on the same unshifted base grid, which is
    what keeps the pair on the (video_t, audio_t) diagonal every training step sat on."""
    video, audio = partial_sigmas(0.25, 4, 12.0), partial_sigmas(0.25, 4, 3.0)

    def unshift(s, shift):
        return s / (shift - (shift - 1.0) * s)

    assert [unshift(float(v), 12.0) for v in video] == pytest.approx(
        [unshift(float(a), 3.0) for a in audio], abs=1e-7)


def test_ensure_partial_table_returns_an_existing_table_and_bakes_nothing(tmp_path, monkeypatch):
    table = tmp_path / partial_table_name(0.25)
    table.write_bytes(b"not really a table")
    monkeypatch.setattr(facerefine, "_bake_partial_table",
                        lambda *a, **k: pytest.fail("baked over an existing table"))
    assert ensure_partial_table(0.25, None, tmp_path) == table


def test_ensure_partial_table_bakes_the_four_point_grid_when_the_table_is_missing(tmp_path,
                                                                                 monkeypatch):
    calls = []

    def fake_bake(sigma, dest, points, lora, strength, verbose):
        calls.append((sigma, dest, points, lora, strength))
        Path(dest).write_bytes(b"baked")

    monkeypatch.setattr(facerefine, "_bake_partial_table", fake_bake)
    lora = tmp_path / facerefine.TURBO_LORA_NAME
    lora.write_bytes(b"lora")
    monkeypatch.setattr(facerefine, "_bake_inputs_present", lambda adaln_dir, lora: None)

    out = ensure_partial_table(0.15, None, tmp_path)
    assert out == tmp_path / "adaln_face_s015_4pt_turbo.safetensors"
    assert calls == [(0.15, out, 4, lora, 1.0)]


def test_ensure_partial_table_says_what_is_missing_instead_of_failing_inside_the_baker(tmp_path):
    with pytest.raises(RuntimeError) as excinfo:
        ensure_partial_table(0.25, None, tmp_path)
    assert "adaln_curve.safetensors" in str(excinfo.value) or \
           facerefine.TURBO_LORA_NAME in str(excinfo.value)


# -- refine_clip's guard rails (no GPU, no weights) ----------------------------------------------

def _gray_crops(num_frames: int = 56) -> np.ndarray:
    return np.full((num_frames, 288, 448, 3), 128, np.uint8)


def test_refine_clip_refuses_a_sigma_above_the_measured_ceiling():
    with pytest.raises(ValueError, match="0.25"):
        refine_clip(_gray_crops(), sigma=0.4, checkpoint=Path("/nonexistent"))


def test_refine_clip_refuses_a_sigma_at_or_below_zero():
    with pytest.raises(ValueError):
        refine_clip(_gray_crops(), sigma=0.0, checkpoint=Path("/nonexistent"))


def test_the_sigma_ceiling_is_the_round_two_measurement():
    assert SIGMA_CEILING == 0.25


def test_refine_clip_refuses_crops_that_are_not_a_uint8_rgb_stack():
    with pytest.raises(ValueError):
        refine_clip(np.zeros((56, 288, 448), np.uint8), checkpoint=Path("/nonexistent"))
    with pytest.raises(ValueError):
        refine_clip(np.zeros((56, 288, 448, 3), np.float32), checkpoint=Path("/nonexistent"))


def test_refine_clip_refuses_a_crop_whose_sides_are_not_multiples_of_the_vae_ratio():
    with pytest.raises(ValueError, match="16"):
        refine_clip(np.zeros((56, 100, 448, 3), np.uint8), checkpoint=Path("/nonexistent"))


# -- the one GPU test ----------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("H3_GPU_SMOKE") != "1",
                    reason="2 GPU-minutes and 28 GB; run with H3_GPU_SMOKE=1")
def test_refine_clip_smoke_on_one_window_of_grey_frames():
    checkpoint = Path.home() / "models/h3-8bit-full"
    if not checkpoint.is_dir():
        pytest.skip(f"no checkpoint at {checkpoint}")
    crops = _gray_crops(56)
    out = refine_clip(crops, sigma=0.25, checkpoint=checkpoint)
    assert out.shape == crops.shape
    assert out.dtype == np.uint8
