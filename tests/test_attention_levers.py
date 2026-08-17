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


def tiny_dit(quantize_qkv: bool = False, seed: int = 1234):
    """A doll-sized DiT with deterministic weights. Two calls give bit-identical models."""
    from minimax_h3_mlx.dit import MiniMaxH3DiT

    mx.random.seed(seed)
    dit = MiniMaxH3DiT(tiny_config())
    mx.eval(dit.parameters())
    if quantize_qkv:
        # Only the fused projection, and only because that is the layer the carve has to move
        # `scales`/`biases` for. The rest of the tree has dimensions a group size of 32 does not
        # divide, and quantizing them would prove nothing extra.
        nn.quantize(dit, group_size=32, bits=8,
                    class_predicate=lambda path, _m: path.endswith("attn.qkv_proj"))
        mx.eval(dit.parameters())
    return dit


def packed_layout(n_text: int = 5, n_video: int = 9, n_audio: int = 3):
    """Text rows, then video, then audio — the upstream smoke test's packing."""
    seq = n_text + n_video + n_audio
    tags = mx.concatenate([mx.full((n_text,), TAG_TEXT, dtype=mx.int32),
                           mx.full((n_video,), TAG_VIDEO, dtype=mx.int32),
                           mx.full((n_audio,), TAG_AUDIO, dtype=mx.int32)])
    timestep_indices = mx.concatenate([mx.zeros((n_text,), dtype=mx.int32),
                                       mx.ones((n_video,), dtype=mx.int32),
                                       mx.zeros((n_audio,), dtype=mx.int32)])
    position_ids = mx.stack([mx.arange(seq) % 3, mx.arange(seq) % 5, mx.arange(seq) % 7],
                            axis=-1).astype(mx.int32)
    return dict(text_indices=mx.arange(n_text),
                video_indices=mx.arange(n_text, n_text + n_video),
                audio_indices=mx.arange(n_text + n_video, seq),
                tags=tags, timestep_indices=timestep_indices, position_ids=position_ids,
                n_text=n_text, n_video=n_video, n_audio=n_audio)


def forward(dit, layout, seed: int = 7):
    """One forward over the packed layout, with the live AdaLN projection."""
    cfg = dit.config
    mx.random.seed(seed)
    video = mx.random.normal((1, layout["n_video"], cfg.video_patch_dim))
    audio = mx.random.normal((1, layout["n_audio"], cfg.audio_latents_dim))
    text = mx.random.normal((1, layout["n_text"], cfg.text_dim))
    out = dit(video, audio, text, mx.array([0.0, 0.7]), layout["timestep_indices"],
              layout["tags"], layout["position_ids"], layout["video_indices"],
              layout["audio_indices"], layout["text_indices"])
    mx.eval(out)
    return out


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


# ---------------------------------------------------------------- Task 2: qkv-split


def test_the_carved_rows_are_the_release_slab_order():
    """The claim the LoRA split rests on, checked against the conversion's own permutation.

    `convert_sawfwair.slabs_to_interleaved_index` is the index that turned the release's
    `[all-q; all-k; all-v]` slabs into this fork's per-head interleave. Gathering the carve's q
    rows out of the interleave has to land back on the q slab, in order — otherwise slicing a
    fused `lora_B` into thirds would attach each correction to the wrong head.
    """
    from convert_sawfwair import slabs_to_interleaved_index

    heads, head_dim = 4, 16
    attention = updit.Attention(tiny_config())
    rows = np.array(attention.qkv_rows())

    forward_index = slabs_to_interleaved_index(heads, head_dim)  # interleaved[i] <- slabs[idx[i]]
    for part in range(3):
        landed = forward_index[rows[part]]
        want = np.arange(part * heads * head_dim, (part + 1) * heads * head_dim)
        assert (landed == want).all(), (
            f"part {part}: carved rows map to slab rows {landed[:6]}…, expected {want[:6]}…")
    assert sorted(rows.reshape(-1).tolist()) == list(range(3 * heads * head_dim))


@pytest.mark.parametrize("quantized", [False, True])
def test_the_split_forward_is_bit_identical_to_the_fused_one(quantized):
    """The gate for Task 2, quantized and not.

    Quantized is the case that could go wrong invisibly: MLX groups affine quantization along the
    *input* axis, so a row owns its scale and bias and a row gather moves them intact. Were the
    grouping along the output axis instead, this test would fail and the carve would be a
    requantization pretending to be a copy.
    """
    layout = packed_layout()
    dit = tiny_dit(quantize_qkv=quantized)
    fused_v, fused_a = forward(dit, layout)

    from h3_48gb.dit import split_fused_attention

    assert split_fused_attention(dit) == len(dit.blocks) + len(dit.token_refiner.blocks)
    assert all(block.attn.is_split for block in dit.blocks)
    assert not any(hasattr(block.attn, "qkv_proj") for block in dit.blocks), (
        "the fused projection is still resident — that is ~6 GB of duplicated weights at full size")

    split_v, split_a = forward(dit, layout)
    exact(split_v, fused_v, f"qkv-split video (quantized={quantized})")
    exact(split_a, fused_a, f"qkv-split audio (quantized={quantized})")


def test_splitting_twice_is_a_no_op():
    """`split_fused_attention` runs at load; a resumed run must not be able to double-carve."""
    dit = tiny_dit()
    from h3_48gb.dit import split_fused_attention

    split_fused_attention(dit)
    weight = dit.blocks[0].attn.q_proj.weight
    split_fused_attention(dit)
    assert dit.blocks[0].attn.q_proj.weight is weight


def _write_backbone_lora(path, dit, rank: int = 4):
    """A synthetic Turbo-shaped LoRA: fused `attn.qkv_proj` rows in release slab order."""
    from h3_48gb.turbo import BACKBONE_TARGETS

    cfg = dit.config
    widths = {"attn.qkv_proj": 3 * cfg.num_attention_heads * cfg.attention_head_dim,
              "attn.out_proj": cfg.hidden_size,
              "mlp.fc1": 2 * cfg.ffn_hidden_size,
              "mlp.fc2": cfg.hidden_size}
    inputs = {"attn.qkv_proj": cfg.hidden_size, "attn.out_proj": cfg.inner_dim,
              "mlp.fc1": cfg.hidden_size, "mlp.fc2": cfg.ffn_hidden_size}

    mx.random.seed(99)
    tensors = {}
    blocks = ([f"blocks.{i}" for i in range(len(dit.blocks))]
              + [f"token_refiner.blocks.{i}" for i in range(len(dit.token_refiner.blocks))])
    for block in blocks:
        for target in BACKBONE_TARGETS:
            tensors[f"{block}.{target}.lora_A.weight"] = mx.random.normal((rank, inputs[target]))
            tensors[f"{block}.{target}.lora_B.weight"] = mx.random.normal((widths[target], rank))
    mx.save_safetensors(str(path), tensors)
    return path


def test_the_turbo_lora_survives_the_split_bit_for_bit(tmp_path):
    """DELICATE: the fused adapter permutes `lora_B`; the split one must slice it instead.

    Both spellings have to produce the same numbers, and the failure mode if they do not is the
    quiet one — every correction still lands on *a* head, just not its own, which reads as a
    slightly weak LoRA rather than as a bug. So this compares whole forwards, not shapes.
    """
    from h3_48gb.dit import split_fused_attention
    from h3_48gb.turbo import apply_backbone_lora

    layout = packed_layout()
    heads, head_dim = 4, 16

    fused = tiny_dit()
    weights = _write_backbone_lora(tmp_path / "lora.safetensors", fused)
    report_fused = apply_backbone_lora(fused, weights, strength=1.0, num_heads=heads,
                                       head_dim=head_dim, verbose=False)
    want_v, want_a = forward(fused, layout)

    split = tiny_dit()
    split_fused_attention(split)
    report_split = apply_backbone_lora(split, weights, strength=1.0, num_heads=heads,
                                       head_dim=head_dim, verbose=False)
    got_v, got_a = forward(split, layout)

    exact(got_v, want_v, "turbo LoRA over split QKV, video")
    exact(got_a, want_a, "turbo LoRA over split QKV, audio")

    n_attn = len(fused.blocks) + len(fused.token_refiner.blocks)
    assert report_fused["permuted"] == n_attn and report_fused["split"] == 0
    assert report_split["split"] == n_attn and report_split["permuted"] == 0
    assert report_split["wrapped"] == report_fused["wrapped"] + 2 * n_attn
    assert report_split["added_gb"] == report_fused["added_gb"], (
        "splitting `lora_B` must not duplicate it")


def test_an_unpermuted_fused_lora_is_visibly_different(tmp_path):
    """Guard: the parity above must not pass because the permutation stopped mattering."""
    from h3_48gb.turbo import apply_backbone_lora

    layout = packed_layout()
    reference = tiny_dit()
    weights = _write_backbone_lora(tmp_path / "lora.safetensors", reference)
    apply_backbone_lora(reference, weights, strength=1.0, num_heads=4, head_dim=16, verbose=False)
    want_v, _ = forward(reference, layout)

    wrong = tiny_dit()
    apply_backbone_lora(wrong, weights, strength=1.0, num_heads=4, head_dim=16, verbose=False,
                        permute_qkv=False)
    got_v, _ = forward(wrong, layout)
    assert float(mx.abs(got_v - want_v).max().item()) > 1e-4, (
        "the QKV permutation made no difference — the layout claim is untested")


def test_the_lightx2v_adapter_also_survives_the_split(tmp_path):
    """LightX2V trains q/k/v separately, so on a split model it needs no conversion at all."""
    from h3_48gb.dit import split_fused_attention
    from h3_48gb.turbo import LoRALinear, SplitQKVLoRALinear, apply_lightx2v_lora

    cfg = tiny_config()
    rank = 4
    mx.random.seed(5)
    tensors = {}
    for source, count in (("transformer_blocks", cfg.num_layers),
                          ("token_refiner.refiner_blocks", cfg.token_refiner_num_layers)):
        for i in range(count):
            for part in ("q", "k", "v"):
                key = f"{source}.{i}.attn.to_{part}"
                tensors[f"{key}.lora_A.default.weight"] = mx.random.normal((rank, cfg.hidden_size))
                tensors[f"{key}.lora_B.default.weight"] = mx.random.normal((cfg.inner_dim, rank))
    path = tmp_path / "lightx2v.safetensors"
    mx.save_safetensors(str(path), tensors)

    layout = packed_layout()
    fused = tiny_dit()
    apply_lightx2v_lora(fused, path, num_heads=4, head_dim=16, verbose=False)
    assert isinstance(fused.blocks[0].attn.qkv_proj, SplitQKVLoRALinear)
    want_v, want_a = forward(fused, layout)

    split = tiny_dit()
    split_fused_attention(split)
    apply_lightx2v_lora(split, path, num_heads=4, head_dim=16, verbose=False)
    assert isinstance(split.blocks[0].attn.q_proj, LoRALinear)
    got_v, got_a = forward(split, layout)

    exact(got_v, want_v, "LightX2V over split QKV, video")
    exact(got_a, want_a, "LightX2V over split QKV, audio")
