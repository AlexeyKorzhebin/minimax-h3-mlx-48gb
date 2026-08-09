"""The sigma grid a run samples on comes from the AdaLN cache, not from `linspace`.

`simple` spends its final 8-step forward jumping from sigma 0.667 straight to 0 — one Euler step
across two thirds of the remaining trajectory. Trying a grid that splits that jump used to be
impossible: upstream rebuilt a uniform grid inside `_build_schedules`, and the cache's own
`check_schedule` then rejected the run as a mismatch. Nothing was wrong with the baked table; the
pipeline simply refused to sample where the table said it had been baked for.
"""
from pathlib import Path

import mlx.core as mx
import numpy as np
import pytest

from h3_48gb.pipeline import LazyMiniMaxH3Pipeline


def write_cache(path: Path, video: list[float], audio: list[float]) -> Path:
    mx.save_safetensors(str(path), {"video_sigmas": mx.array(video, dtype=mx.float32),
                                    "audio_sigmas": mx.array(audio, dtype=mx.float32)})
    return path


class Bare(LazyMiniMaxH3Pipeline):
    """Only the schedule machinery — constructing the real pipeline needs 46 GB of weights."""

    def __init__(self, cache_path):
        self._adaln_cache_path = Path(cache_path) if cache_path else None
        self._schedules = None
        self._cached_grids = None

    @property
    def config(self):
        class _C:
            sigma_shift_video = 12.0
            sigma_shift_audio = 3.0
        return _C()


def uniform(steps: int, shift: float) -> list[float]:
    from h3_48gb import _upstream  # noqa: F401
    from minimax_h3_mlx.scheduler import MiniMaxH3Scheduler

    s = MiniMaxH3Scheduler(shift=shift)
    s.set_timesteps(steps)
    return [float(v) for v in s.sigmas.tolist()]


def test_simple_cache_grid_is_unchanged(tmp_path):
    """Adopting the stored grid must be a no-op for every cache baked the uniform way.

    The baker writes float32 straight from this scheduler, so the round trip is exact — if it
    were not, this change would silently move every existing run onto a slightly different
    trajectory, which is precisely the failure `check_schedule` exists to prevent.
    """
    steps = 8
    cache = write_cache(tmp_path / "simple.safetensors", uniform(steps, 12.0), uniform(steps, 3.0))
    video, audio = Bare(cache)._build_schedules(steps)

    assert [float(v) for v in video.sigmas.tolist()] == uniform(steps, 12.0)
    assert [float(v) for v in audio.sigmas.tolist()] == uniform(steps, 3.0)


def test_a_non_uniform_cache_grid_is_what_the_run_samples(tmp_path):
    """The whole point: a grid the uniform construction cannot produce still runs."""
    # float32 literals: the grid is stored as F32, and the point of the test is the shape of the
    # schedule, not a rounding step the storage format imposes on everyone equally.
    f32 = lambda xs: [float(v) for v in np.array(xs, np.float32)]
    dense_tail = f32([1.0, 0.9, 0.719, 0.421, 0.18, 0.0])
    audio_grid = f32([1.0, 0.8, 0.55, 0.3, 0.12, 0.0])
    cache = write_cache(tmp_path / "beta.safetensors", dense_tail, audio_grid)
    video, audio = Bare(cache)._build_schedules(len(dense_tail))

    assert [float(v) for v in video.sigmas.tolist()] == dense_tail
    assert [float(v) for v in audio.sigmas.tolist()] == audio_grid
    # `timesteps` is what the modulation rows are addressed by; it has to follow the new grid.
    assert [float(v) for v in video.timesteps.tolist()] == [1.0 - s for s in dense_tail[:-1]]
    assert video.num_inference_steps == len(dense_tail) - 1


def test_a_grid_of_the_wrong_length_is_left_for_check_schedule(tmp_path):
    """Silently adopting a 6-point grid for a 31-step request would denoise on the wrong rows.

    `check_schedule` already refuses this with the grid sizes and the N-vs-N-1 convention
    spelled out, so `_build_schedules` must not pre-empt it with a worse message — or, worse,
    accept it.
    """
    cache = write_cache(tmp_path / "short.safetensors", [1.0, 0.5, 0.0], [1.0, 0.4, 0.0])
    video, _ = Bare(cache)._build_schedules(8)

    assert len(video.sigmas.tolist()) == 8, "an unrelated grid must not be adopted"
    assert [float(v) for v in video.sigmas.tolist()] == uniform(8, 12.0)


def test_no_cache_falls_back_to_the_uniform_grid():
    """The unquantized path has no baked table and must keep working."""
    video, audio = Bare(None)._build_schedules(31)

    assert [float(v) for v in video.sigmas.tolist()] == uniform(31, 12.0)
    assert [float(v) for v in audio.sigmas.tolist()] == uniform(31, 3.0)


def test_the_sigma_tensors_are_read_once(tmp_path):
    """`_build_schedules` runs per call; re-reading a 3 GB table off Yandex.Disk for two small
    tensors would show up as latency on every one."""
    cache = write_cache(tmp_path / "simple.safetensors", uniform(8, 12.0), uniform(8, 3.0))
    pipe = Bare(cache)

    reads = []
    real_load = mx.load

    def counting_load(path, *a, **k):
        reads.append(path)
        return real_load(path, *a, **k)

    mx.load = counting_load
    try:
        pipe._build_schedules(8)
        pipe._build_schedules(8)
    finally:
        mx.load = real_load

    assert len(reads) == 1, f"the cache file was opened {len(reads)} times"
