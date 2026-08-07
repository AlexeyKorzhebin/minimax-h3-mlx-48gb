import sys
from pathlib import Path

import mlx.core as mx
import pytest

from h3_48gb.preview import emit_preview, preview_path


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


def test_a_tae_preview_never_touches_the_video_vae(tmp_path, monkeypatch):
    """The whole point: 9.8 MB instead of 5.21 GB, and no chunk floor."""
    written = emit_preview(
        _ExplodingPipeline(), _rows(), 8, 8, 12, (1, 2, 2),
        stem=tmp_path / "run", step=3, decoder="tae",
    )
    assert written
    assert preview_path(tmp_path / "run", 3).exists()


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


def test_an_unknown_decoder_name_is_refused_loudly():
    """A typo must not silently fall back to a different decoder than the caller asked for."""
    with pytest.raises(ValueError) as excinfo:
        emit_preview(_ExplodingPipeline(), _rows(), 8, 8, 12, (1, 2, 2),
                     stem="/tmp/x", step=1, decoder="taa")
    assert "taa" in str(excinfo.value)


def test_the_default_is_still_the_real_vae(tmp_path):
    """An experimental decoder must not become the default by being merged."""
    import inspect

    from h3_48gb.preview import emit_preview as fn

    assert inspect.signature(fn).parameters["decoder"].default == "vae"
