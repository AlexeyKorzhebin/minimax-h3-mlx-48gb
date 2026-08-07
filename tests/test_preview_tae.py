import sys
from pathlib import Path

import mlx.core as mx
import pytest

from PIL import Image

from h3_48gb.preview import emit_preview, preview_path
from h3_48gb.tae import SPATIAL_RATIO, TAE_WEIGHTS_PATH


class _ExplodingPipeline:
    """Any decoder that touches the VAE through this will fail; TAE must not need it."""

    class _Config:
        latent_channels = 24
        latents_mean = [0.0] * 24
        latents_std = [1.0] * 24
        clip_length = 17
        token_drop = 3

    @property
    def video_vae(self):
        raise AssertionError("a TAE preview must not load the video VAE")


def _rows(num_latent_frames=8, latent_h=8, latent_w=12, patch=(1, 2, 2)):
    per_frame = (latent_h // patch[1]) * (latent_w // patch[2])
    width = 24 * patch[0] * patch[1] * patch[2]
    return mx.zeros((num_latent_frames * per_frame, width))


@pytest.mark.skipif(not TAE_WEIGHTS_PATH.exists(),
                    reason=f"no TAE weights at {TAE_WEIGHTS_PATH}")
def test_a_tae_preview_never_touches_the_video_vae(tmp_path, monkeypatch):
    """The whole point: 9.8 MB instead of 5.21 GB, and no chunk floor.

    Checked by the frame's *size*, not by a file merely existing. Both decoders write a JPEG, so
    "a JPEG appeared" is satisfied by the latent fallback — which is what this test would silently
    become if TAE broke, or if the weights were absent, or if reading the VAE config ever started
    loading 5.21 GB. TAE upsamples the 8x12 latent by 16 to 192x128; the fallback draws the latent
    grid itself, at 12x8.
    """
    written = emit_preview(
        _ExplodingPipeline(), _rows(), 8, 8, 12, (1, 2, 2),
        stem=tmp_path / "run", step=3, decoder="tae",
    )
    assert written
    dest = preview_path(tmp_path / "run", 3)
    assert dest.exists()
    with Image.open(dest) as frame:
        assert frame.size == (12 * SPATIAL_RATIO, 8 * SPATIAL_RATIO), (
            f"expected a TAE frame at {12 * SPATIAL_RATIO}x{8 * SPATIAL_RATIO}, got {frame.size} "
            "— this is the latent fallback, so TAE did not run")


def test_a_missing_tae_file_falls_back_instead_of_raising(tmp_path, monkeypatch):
    from h3_48gb import tae

    monkeypatch.setattr(tae, "TAE_WEIGHTS_PATH", tmp_path / "absent.safetensors")
    written = emit_preview(
        _ExplodingPipeline(), _rows(), 8, 8, 12, (1, 2, 2),
        stem=tmp_path / "run", step=4, decoder="tae",
    )
    # The latent fallback needs no VAE weights either, so a frame still lands.
    assert written


def test_a_corrupt_tae_file_falls_back_instead_of_raising(tmp_path, monkeypatch):
    from h3_48gb import tae

    corrupt = tmp_path / "corrupt.safetensors"
    corrupt.write_bytes(b"not a safetensors file")
    monkeypatch.setattr(tae, "TAE_WEIGHTS_PATH", corrupt)
    written = emit_preview(
        _ExplodingPipeline(), _rows(), 8, 8, 12, (1, 2, 2),
        stem=tmp_path / "run", step=5, decoder="tae",
    )
    assert written


def test_an_unknown_decoder_name_is_refused_loudly(tmp_path):
    """A typo must not silently fall back to a different decoder than the caller asked for."""
    with pytest.raises(ValueError) as excinfo:
        emit_preview(_ExplodingPipeline(), _rows(), 8, 8, 12, (1, 2, 2),
                     stem=tmp_path / "run", step=1, decoder="taa")
    assert "taa" in str(excinfo.value)


def test_the_default_is_still_the_real_vae(tmp_path):
    """An experimental decoder must not become the default by being merged."""
    import inspect

    from h3_48gb.preview import emit_preview as fn

    assert inspect.signature(fn).parameters["decoder"].default == "vae"


def test_the_latent_decoder_is_reachable_and_needs_no_weights(tmp_path):
    """The third state ships too, and is the only one that works with nothing on disk at all."""
    written = emit_preview(
        _ExplodingPipeline(), _rows(), 8, 8, 12, (1, 2, 2),
        stem=tmp_path / "run", step=7, decoder="latent",
    )
    assert written
    dest = preview_path(tmp_path / "run", 7)
    with Image.open(dest) as frame:
        assert frame.size == (12, 8), (
            f"the latent heat map draws the latent grid itself; got {frame.size}")
