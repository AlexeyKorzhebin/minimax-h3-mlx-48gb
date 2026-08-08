"""Turbo LoRA as a runtime side path over 4-bit weights.

The Turbo LoRA (`larryvrh/MiniMax-H3-Turbo-Lora`) trains MiniMax-H3 to sample in 4-8 steps instead
of 30. It arrives in two halves, and they must be applied in completely different ways:

* **The AdaLN half** (102 of 518 tensors) lives in the 2688-dim ``silu(t_emb)`` space that this
  build's pruned base folded away. It is baked into the modulation table instead — see
  ``scripts/bake_adaln.py``. That table is bf16, so folding costs nothing at run time.
* **The backbone half** (416 tensors: ``attn.qkv_proj``, ``attn.out_proj``, ``mlp.fc1``,
  ``mlp.fc2``) is what this module applies, and it **must not be merged into the weights**. Those
  weights are 4-bit: the update's rms is 3.37e-05 against a quantization step of 1.19e-02, so it is
  0.3% of one step and requantization would discard 99.7% of the training. It is added at run time
  as ``y = quantized(x) + strength * (x @ A.T) @ B.T`` — about 1.5% more work in the linear layers
  and none at all in attention, which is where the time actually goes.

**The QKV rows have to be permuted.** This fork's ``attn.qkv_proj`` is in the release's per-head
interleave (``[h0:q,k,v][h1:q,k,v]...``), restored by ``convert_sawfwair.py`` from the global slabs
mere.run had rewritten it into. The LoRA is in slabs. Measured rather than assumed: grouping
``lora_B``'s row norms by slab separates q/k/v where grouping by head does not — in block 49 the v
slab is half the magnitude of q and k (0.0051 against 0.0102 and 0.0108), and that structure
vanishes entirely under the per-head grouping (0.0086/0.0087/0.0088). Applying an unpermuted
``lora_B`` raises nothing; it just scrambles which head each correction lands on.
"""
from __future__ import annotations

from pathlib import Path

import mlx.core as mx
import mlx.nn as nn

#: The four projections the backbone half touches, as they are named in both trees.
BACKBONE_TARGETS = ("attn.qkv_proj", "attn.out_proj", "mlp.fc1", "mlp.fc2")

#: Only this one needs its rows permuted; the other three are 1:1 between the two layouts.
PERMUTED_TARGET = "attn.qkv_proj"


def slabs_to_interleaved(num_heads: int, head_dim: int) -> mx.array:
    """Row index mapping ``[all-q; all-k; all-v]`` -> ``[h0:q,k,v][h1:q,k,v]...``.

    The same permutation `convert_sawfwair.py` applies to the checkpoint itself, expressed here
    over row indices so it can be applied to a LoRA factor.
    """
    index = mx.arange(3 * num_heads * head_dim).reshape(3, num_heads, head_dim)
    return index.transpose(1, 0, 2).reshape(-1)


class LoRALinear(nn.Module):
    """A quantized linear with a full-precision low-rank correction added to its output.

    Deliberately not a merge. See the module docstring for the arithmetic that rules merging out.
    """

    def __init__(self, base: nn.Module, lora_a: mx.array, lora_b: mx.array, strength: float = 1.0):
        super().__init__()
        self.base = base
        self.lora_a = lora_a
        self.lora_b = lora_b
        self.strength = strength

    def __call__(self, x: mx.array) -> mx.array:
        out = self.base(x)
        delta = (x @ self.lora_a.T) @ self.lora_b.T
        return out + self.strength * delta.astype(out.dtype)


def _resolve(root: nn.Module, path: str):
    """``blocks.0.attn.qkv_proj`` -> (owning module, attribute name)."""
    parts = path.split(".")
    node = root
    for part in parts[:-1]:
        node = node[int(part)] if part.isdigit() else getattr(node, part)
    return node, parts[-1]


def apply_backbone_lora(dit, weights_path: Path | str, strength: float = 1.0,
                        num_heads: int = 56, head_dim: int = 128, verbose: bool = True,
                        permute_qkv: bool = True) -> dict:
    """Wrap every backbone projection of `dit` with its LoRA correction.

    Returns a report: how many layers were wrapped, how many were permuted, and the added bytes.
    Raises if the LoRA names a layer this model does not have, or if a shape disagrees — a LoRA
    silently applied to three quarters of the blocks would look like a weak LoRA, not like a bug.
    """
    lora = mx.load(str(weights_path))
    permutation = slabs_to_interleaved(num_heads, head_dim)

    wrapped = permuted = 0
    added_bytes = 0
    missing: list[str] = []

    # `token_refiner` is part of the backbone too — the author's node says so explicitly
    # ("attn/mlp/refiner"). Missing it leaves the text conditioning un-adapted while all 50
    # blocks downstream expect the adapted form, and nothing raises: the run just gets worse.
    paths = [f"blocks.{b}.{t}" for b in range(len(dit.blocks)) for t in BACKBONE_TARGETS]
    refiner = getattr(dit, "token_refiner", None)
    if refiner is not None:
        paths += [f"token_refiner.blocks.{b}.{t}"
                  for b in range(len(refiner.blocks)) for t in BACKBONE_TARGETS]

    for path in paths:
        if True:
            target = path.rsplit(".", 2)[-2] + "." + path.rsplit(".", 1)[-1]
            key_a, key_b = f"{path}.lora_A.weight", f"{path}.lora_B.weight"
            if key_a not in lora or key_b not in lora:
                missing.append(path)
                continue

            a, b = lora[key_a], lora[key_b]
            if target == PERMUTED_TARGET and permute_qkv:
                if b.shape[0] != 3 * num_heads * head_dim:
                    raise ValueError(
                        f"{key_b} has {b.shape[0]} rows, expected 3 * {num_heads} * {head_dim} = "
                        f"{3 * num_heads * head_dim}; the QKV permutation would be wrong.")
                b = b[permutation]
                permuted += 1

            owner, attribute = _resolve(dit, path)
            base = getattr(owner, attribute)
            setattr(owner, attribute, LoRALinear(base, a, b, strength))
            wrapped += 1
            added_bytes += a.nbytes + b.nbytes

    if missing:
        raise KeyError(
            f"the LoRA is missing {len(missing)} backbone projections this model has, e.g. "
            f"{missing[:3]} — a partial application looks like a weak LoRA, not like a failure.")

    # The check above is one-directional and that is how `token_refiner` was silently skipped for
    # a whole afternoon: every projection the loop visited was present in the LoRA, so nothing
    # complained, while eight of the LoRA's own targets were never applied. Check both ways.
    offered = {k.rsplit(".lora_", 1)[0] for k in lora}
    unapplied = sorted(offered - set(paths) - {n for n in offered if "adaln_proj" in n})
    if unapplied:
        raise KeyError(
            f"the LoRA carries {len(unapplied)} backbone targets this run never applied: "
            f"{unapplied[:4]} — a partial application is indistinguishable from a weak LoRA.")

    report = {"wrapped": wrapped, "permuted": permuted, "added_gb": added_bytes / 1e9,
              "strength": strength}
    if verbose:
        print(f"  turbo: {wrapped} projections wrapped ({permuted} QKV permuted), "
              f"+{report['added_gb']:.2f} GB, strength {strength}")
    return report
