from pathlib import Path

import numpy as np
import pytest

from h3_48gb.tae import TAE_WEIGHTS_PATH, to_mlx_conv2d_layout

pytestmark = pytest.mark.skipif(not TAE_WEIGHTS_PATH.exists(),
                                reason=f"no TAE weights at {TAE_WEIGHTS_PATH}")


def test_conv_layout_is_channels_last_by_value_not_by_shape():
    import mlx.core as mx

    source = mx.random.normal((5, 4, 3, 3))          # (out, in, kH, kW)
    out = to_mlx_conv2d_layout(source)
    assert out.shape == (5, 3, 3, 4)

    reference = np.zeros((5, 3, 3, 4), dtype=np.float32)
    src = np.array(source)
    for o in range(5):
        for i in range(4):
            for h in range(3):
                for w in range(3):
                    reference[o, h, w, i] = src[o, i, h, w]
    assert np.array_equal(np.array(out), reference)


def test_a_wrong_permutation_would_have_the_same_shape():
    """Without this, the test above could be satisfied by a transpose that swaps kH and kW."""
    import mlx.core as mx

    source = mx.random.normal((5, 4, 3, 3))
    right = to_mlx_conv2d_layout(source)
    wrong = source.transpose(0, 3, 2, 1)              # kW and kH swapped
    assert right.shape == wrong.shape, "the shapes must collide, or this test proves nothing"
    assert float(mx.abs(right - wrong).max()) > 0.0, "and the values must differ"


SLOT_TO_PARAM_SAMPLES = [
    ("1.weight", "conv_in.weight", (96, 3, 3, 24)),
    ("13.skip.weight", "stage3.0.skip.weight", (64, 1, 1, 96)),
    ("23.weight", "conv_out.weight", (3, 3, 3, 64)),
]


def test_every_tensor_lands_and_nothing_is_invented():
    from h3_48gb.tae import load_tae

    decoder, report = load_tae(TAE_WEIGHTS_PATH, report=True)
    assert report["missing"] == [], f"parameters no tensor filled: {report['missing']}"
    assert report["unused"] == [], f"tensors that matched no parameter: {report['unused']}"
    assert report["loaded"] == 81, f"expected all 81 tensors, got {report['loaded']}"


@pytest.mark.parametrize("checkpoint_key,param_path,expected_shape", SLOT_TO_PARAM_SAMPLES)
def test_named_slots_reach_their_parameters_with_the_right_shape(checkpoint_key, param_path,
                                                                 expected_shape):
    from mlx.utils import tree_flatten

    from h3_48gb.tae import load_tae

    decoder = load_tae(TAE_WEIGHTS_PATH)
    params = dict(tree_flatten(decoder.parameters()))
    assert param_path in params, f"{param_path} is not a parameter of the module tree"
    assert params[param_path].shape == expected_shape


def test_a_missing_weights_file_says_so_plainly(tmp_path):
    from h3_48gb.tae import load_tae

    with pytest.raises(FileNotFoundError) as excinfo:
        load_tae(tmp_path / "absent.safetensors")
    assert "absent.safetensors" in str(excinfo.value)


def test_a_truncated_checkpoint_is_refused_rather_than_half_loaded(tmp_path):
    """A decoder with three blocks silently left at their init values produces plausible noise."""
    import mlx.core as mx

    full = mx.load(str(TAE_WEIGHTS_PATH))
    partial = {k: v for k, v in full.items() if not k.startswith("22.")}
    path = tmp_path / "partial.safetensors"
    mx.save_safetensors(str(path), partial)

    from h3_48gb.tae import load_tae

    with pytest.raises(KeyError) as excinfo:
        load_tae(path)
    assert "22" in str(excinfo.value), "the message must name what is missing"


def test_the_decoder_upsamples_by_exactly_sixteen():
    import mlx.core as mx

    from h3_48gb.tae import SPATIAL_RATIO, load_tae

    decoder = load_tae(TAE_WEIGHTS_PATH)
    out = decoder(mx.zeros((1, 8, 12, 24)))
    assert out.shape == (1, 8 * SPATIAL_RATIO, 12 * SPATIAL_RATIO, 3)


@pytest.mark.skipif(
    not (Path.home() / "models/h3-converted/video_vae").exists(),
    reason="no converted video_vae checkpoint",
)
def test_the_ratio_matches_the_video_vae():
    """If these ever diverge, a TAE preview would be a different size from a VAE one."""
    from h3_48gb.pipeline import video_vae_config
    from h3_48gb.tae import LATENT_CHANNELS, SPATIAL_RATIO

    cfg = video_vae_config(Path.home() / "models/h3-converted/video_vae")
    assert SPATIAL_RATIO == cfg.spatial_compression_ratio
    assert LATENT_CHANNELS == cfg.latent_channels
