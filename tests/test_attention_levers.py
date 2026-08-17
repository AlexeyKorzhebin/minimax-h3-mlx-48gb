"""Bit-parity gates for the attention/MLP memory levers.

Four edits to `upstream/minimax_h3_mlx/dit.py` (carried as
`patches/0002-attention-memory-levers.patch`) and one to `h3_48gb/dit.py` cut the peak of a DiT
forward without changing a single output value:

* **rope-lean** — `apply_rotary` without the split/concatenate of the pass-through channels.
* **qkv-split** — the fused `attn.qkv_proj` carved into `q_proj`/`k_proj`/`v_proj` at load time,
  so k and v can be staged before q exists.
* **q-chunk** — attention over query-row chunks, k/v full.
* **mlp-chunk** — the SwiGLU feed-forward over row chunks.

Every one of them is an arithmetic identity, so the gate is `atol=0, rtol=0` against a *frozen
copy of the stock code* kept in this file. Approximate equality would be the wrong test: these
levers are only allowed to exist because they change nothing, and "nothing" here means no bit.

The reference implementations below are the upstream originals verbatim. They are duplicated on
purpose — reading them out of the patched module would compare the change against itself.
"""

from __future__ import annotations

import mlx.core as mx
import mlx.nn as nn
import numpy as np
import pytest

from h3_48gb import _upstream  # noqa: F401  (puts `minimax_h3_mlx` on the path)

from minimax_h3_mlx import dit as updit
from minimax_h3_mlx.config import TAG_AUDIO, TAG_TEXT, TAG_VIDEO, DiTConfig


# ---------------------------------------------------------------- frozen stock code


def stock_apply_rotary(x: mx.array, cos: mx.array, sin: mx.array) -> mx.array:
    """`minimax_h3_mlx.dit.apply_rotary` as it stood before the rope-lean patch."""
    rotary_dim = cos.shape[-1]
    x_rot, x_pass = x[..., :rotary_dim], x[..., rotary_dim:]
    cos = cos.astype(x.dtype)[None, None, :, :]
    sin = sin.astype(x.dtype)[None, None, :, :]
    half = rotary_dim // 2
    x1, x2 = x_rot[..., :half], x_rot[..., half:]
    rotated = mx.concatenate([-x2, x1], axis=-1)
    out = x_rot * cos + rotated * sin
    if x_pass.shape[-1] == 0:
        return out
    return mx.concatenate([out, x_pass], axis=-1)


# ---------------------------------------------------------------- fixtures


def tiny_config(**overrides) -> DiTConfig:
    """The tiny DiT the upstream smoke test uses: 2 blocks, 4 heads of 16, rotary 12 of 16."""
    hidden = 64
    base = dict(
        hidden_size=hidden,
        num_layers=2,
        token_refiner_num_layers=2,
        num_attention_heads=4,
        attention_head_dim=16,
        ffn_hidden_size=32,
        latents_dim=4,
        audio_latents_dim=8,
        patch_size=(1, 2, 2),
        text_dim=32,
        timestep_input_dim=16,
        time_embed_hidden_size=hidden,
        time_embed_dim=32,
        adaln_out_features=6 * 3 * hidden,
        final_adaln_out_features=2 * hidden,
        rope_inv_freq_len=2,
    )
    base.update(overrides)
    return DiTConfig(**base)


def rotary_case(seq: int, heads: int, head_dim: int, rotary_dim: int, dtype: mx.Dtype):
    mx.random.seed(0)
    x = mx.random.normal((1, heads, seq, head_dim)).astype(dtype)
    angles = mx.random.normal((seq, rotary_dim))
    return x, mx.cos(angles), mx.sin(angles)


def exact(got: mx.array, want: mx.array, what: str) -> None:
    """`atol=0, rtol=0`, with the worst offender named when it fails."""
    mx.eval(got, want)
    assert got.shape == want.shape, f"{what}: shape {got.shape} != {want.shape}"
    assert got.dtype == want.dtype, f"{what}: dtype {got.dtype} != {want.dtype}"
    if bool(mx.all(got == want).item()):
        return
    delta = float(mx.abs(got.astype(mx.float32) - want.astype(mx.float32)).max().item())
    raise AssertionError(f"{what}: not bit-identical, max|delta| = {delta:.3e}")


# ---------------------------------------------------------------- Task 1: rope-lean


@pytest.mark.parametrize("dtype", [mx.bfloat16, mx.float32])
@pytest.mark.parametrize("seq", [1, 7, 64])
def test_rope_lean_is_bit_identical_with_pass_through_channels(dtype, seq):
    """head_dim 16, rotary 12: four channels per head ride through untouched.

    This is the shape the real model has (128 of which 96 rotate) and the case the padding trick
    is for: the pass-through channels now come out of `x * 1 + rotated * 0` rather than out of a
    slice that skipped the arithmetic entirely.
    """
    x, cos, sin = rotary_case(seq, heads=4, head_dim=16, rotary_dim=12, dtype=dtype)
    exact(updit.apply_rotary(x, cos, sin), stock_apply_rotary(x, cos, sin), f"rope-lean {dtype}")


@pytest.mark.parametrize("dtype", [mx.bfloat16, mx.float32])
def test_rope_lean_is_bit_identical_when_everything_rotates(dtype):
    """`rotary_dim == head_dim`: no padding, and the stock code took its early-return branch."""
    x, cos, sin = rotary_case(9, heads=3, head_dim=12, rotary_dim=12, dtype=dtype)
    exact(updit.apply_rotary(x, cos, sin), stock_apply_rotary(x, cos, sin), f"rope-lean full {dtype}")


def test_rope_lean_actually_rotates():
    """A guard against the whole gate passing because both sides became the identity.

    `x * cos + rotated * sin` with the pass region padded 1/0 is *exactly* the identity on the
    pass channels — so an implementation that padded with 1/0 everywhere would satisfy the parity
    test against a reference that had the same bug. Check the rotation happens at all.
    """
    x, cos, sin = rotary_case(5, heads=2, head_dim=16, rotary_dim=12, dtype=mx.float32)
    out = updit.apply_rotary(x, cos, sin)
    mx.eval(out)
    rotated = float(mx.abs(out[..., :12] - x[..., :12]).max().item())
    passed = float(mx.abs(out[..., 12:] - x[..., 12:]).max().item())
    assert rotated > 1e-3, "the leading channels were not rotated at all"
    assert passed == 0.0, f"the pass-through channels moved by {passed}"
