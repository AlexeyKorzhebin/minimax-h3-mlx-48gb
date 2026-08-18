# TAE preview decoder — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decode in-flight previews with a 9.8 MB 2D decoder instead of the 5.21 GB causal video VAE, so a preview costs a fraction of a second instead of 49.3 s and stops forcing a 7-latent-frame floor.

**Architecture:** A new `h3_48gb/tae.py` holds a flat 23-slot `Sequential` port of the TAESD decoder plus its loader. `emit_preview` gains a decoder choice with three states (real VAE, TAE, latent heat map) and keeps its existing fallback chain and never-raises guarantee. Nothing in `preview.py`'s existing VAE path changes, and TAE does not become the default by being merged.

**Tech Stack:** MLX (`mlx.core`, `mlx.nn`), safetensors, NumPy, PIL. Weights from `Kijai/MiniMax-H3-TAE` at `~/models/tae/taeh3.safetensors` — third-party, outside the repository.

**Spec:** `docs/superpowers/specs/2026-08-07-tae-preview-design.md`

## Global Constraints

- `upstream/` is a vendored clone carrying exactly one patch (`patches/0001-keyframe-masked-scatter.patch`). Do not add a second. Everything else is applied from the outside.
- `import h3_48gb` must not import `mlx.core`. There is a subprocess test; keep MLX imports inside functions or inside modules the package does not import at top level.
- `emit_preview` must never raise. A generation runs for hours; no preview failure is worth it. This is already covered by a test and must stay covered.
- The weights live **outside** the repository at `~/models/tae/taeh3.safetensors`. Never commit them. A missing file is a clean skip with a log line, not an error.
- The real video VAE stays the default decoder. TAE ships selectable and off.
- Run tests as `pytest` with **no arguments** from the repo root: some test files live at the root, some in `tests/`. `pytest tests/` silently collects only a subset.
- Never `astype` packed quantized storage. Not expected to arise here (TAE is all F32), but the rule holds.

## Established facts (do not re-derive)

Read from the safetensors header, not from documentation. 81 tensors, all F32, 9.78 MB.

| Slot | Contents | Shapes |
|---|---|---|
| 1 | `Conv2d(24 -> 96, 3x3, bias)` | `weight [96, 24, 3, 3]`, `bias [96]` |
| 3, 4, 5 | `Block(96 -> 96)` | `conv.{0,2,4}.weight [96, 96, 3, 3]` + biases |
| 7 | `Conv2d(96 -> 96, 3x3, no bias)` | `weight [96, 96, 3, 3]` |
| 8, 9, 10 | `Block(96 -> 96)` | as above |
| 12 | `Conv2d(96 -> 96, 3x3, no bias)` | `weight [96, 96, 3, 3]` |
| 13 | `Block(96 -> 64)` **with skip** | `conv.0.weight [64, 96, 3, 3]`, `conv.{2,4}.weight [64, 64, 3, 3]`, `skip.weight [64, 96, 1, 1]` |
| 14, 15 | `Block(64 -> 64)` | `conv.{0,2,4}.weight [64, 64, 3, 3]` |
| 17 | `Conv2d(64 -> 64, 3x3, no bias)` | `weight [64, 64, 3, 3]` |
| 18, 19 | `Block(64 -> 64)` | as above |
| 21 | `Conv2d(64 -> 64, 3x3, no bias)` | `weight [64, 64, 3, 3]` |
| 22 | `Block(64 -> 64)` | as above |
| 23 | `Conv2d(64 -> 3, 3x3, bias)` | `weight [3, 64, 3, 3]`, `bias [3]` |

Slots **0, 2, 6, 11, 16, 20** carry no tensors, which is what identifies them: 0 is the input clamp, 2 is a ReLU, and 6/11/16/20 are the four nearest-neighbour ×2 upsamples. Four upsamples give **16x spatial**, exactly the video VAE's `spatial_compression_ratio`. Input is **24 channels**, exactly the VAE's `latent_channels`.

`Block` is TAESD's: `conv3x3 -> ReLU -> conv3x3 -> ReLU -> conv3x3`, added to a skip (identity when the widths match, a 1x1 convolution when they do not — only slot 13), then a final ReLU.

Two facts about the surrounding code:

- `h3_48gb/preview.py:121` denormalizes before the real VAE: `latents * std + mean`, using `latents_mean`/`latents_std` from the VAE config. Whether TAE wants that form or the normalized one is **the open question Task 2 settles by measurement**.
- `render_preview_frame` decodes only `latents[:, :, :needed]` where `needed` is the VAE's 7-frame chunk floor, then returns the middle frame. TAE has no such floor.

## File structure

| File | Responsibility |
|---|---|
| `h3_48gb/tae.py` (new) | The decoder module, its loader, and the strict key audit. Knows nothing about previews. |
| `tests/test_tae.py` (new) | Shape, layout, strict-loading and normalization-contract tests. Skips cleanly without the weights file. |
| `scripts/measure_tae.py` (new) | One-off measurement: normalization decision, PSNR against the real VAE, time and peak memory. Not imported by the package. |
| `h3_48gb/preview.py` (modify) | `emit_preview` gains a `decoder` choice; the fallback chain and the never-raises guarantee stay as they are. |
| `h3_48gb/cli.py` (modify) | `--preview-decoder {vae,tae,latent}`, defaulting to `vae`. |
| `docs/RESULTS.md` (modify) | The measured numbers, whatever they turn out to be. |

---

### Task 1: Port the decoder

**Files:**
- Create: `h3_48gb/tae.py`
- Test: `tests/test_tae.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `TAEDecoder` (an `mlx.nn.Module`, `__call__(latents: mx.array) -> mx.array`, taking `(N, H, W, 24)` channels-last and returning `(N, 16H, 16W, 3)` in `[0, 1]`); `load_tae(path: Path) -> TAEDecoder`; `TAE_WEIGHTS_PATH: Path`; `to_mlx_conv2d_layout(t: mx.array) -> mx.array`.

- [ ] **Step 1: Write the failing layout test**

The conv weights are stored PyTorch-style, `(out, in, kH, kW)`; `mlx.nn.Conv2d` wants `(out, kH, kW, in)`. **Every kernel here is 3x3 or 1x1, so kH == kW and a permutation that swaps them produces the identical shape** — the same trap that cost a full run on the vision tower's Conv3d. Pin values, not shape.

```python
# tests/test_tae.py
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
```

- [ ] **Step 2: Run it and watch it fail**

Run: `./.venv/bin/python -m pytest tests/test_tae.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'h3_48gb.tae'`.

- [ ] **Step 3: Write the module**

```python
# h3_48gb/tae.py
"""TAE: a 9.8 MB 2D decoder for in-flight previews.

The real video VAE cannot decode fewer than 7 latent frames — its temporal chunking is causal —
and tiles 28 times at 1344x768 regardless of how little is asked for. A preview therefore costs
49.3 s and 8.46 GB to show one frame. TAE is a plain 2D convolutional decoder: no temporal state,
no chunk floor, no tiling.

The weights are third party (`Kijai/MiniMax-H3-TAE`) and live outside the repository. This module
ships the loader, not the file.

Architecture is read from the checkpoint's own slot layout rather than guessed: 81 tensors in a
flat `Sequential` numbered 1..23, where the gaps (0, 2, 6, 11, 16, 20) are the parameterless
layers — an input clamp, a ReLU, and four nearest-neighbour x2 upsamples. Four upsamples make 16x,
which is exactly this VAE's `spatial_compression_ratio`, and the input is 24 channels, exactly its
`latent_channels`.
"""
from __future__ import annotations

from pathlib import Path

import mlx.core as mx
import mlx.nn as nn

#: Third-party weights, deliberately outside the repo. Override for tests or another location.
TAE_WEIGHTS_PATH = Path.home() / "models/tae/taeh3.safetensors"

#: The video VAE's latent channel count, which the decoder's input convolution must match.
LATENT_CHANNELS = 24

#: Four x2 upsamples. Must equal the video VAE's `spatial_compression_ratio`.
SPATIAL_RATIO = 16


def to_mlx_conv2d_layout(tensor: mx.array) -> mx.array:
    """PyTorch's ``(out, in, kH, kW)`` -> MLX's channels-last ``(out, kH, kW, in)``.

    Every kernel in this checkpoint is square (3x3 or 1x1), so a permutation that swaps kH and kW
    yields the *same shape* as the correct one and raises nothing — it just scrambles the filter.
    `tests/test_tae.py` therefore pins values, not shape.
    """
    return tensor.transpose(0, 2, 3, 1)


class Block(nn.Module):
    """TAESD's residual block: three 3x3 convolutions with ReLUs, plus a skip, then a ReLU.

    The skip is the identity when the widths match and a 1x1 convolution when they do not — which
    happens exactly once in this checkpoint, at slot 13 (96 -> 64), the only slot carrying a
    seventh tensor.
    """

    def __init__(self, in_channels: int, out_channels: int):
        super().__init__()
        self.conv = [
            nn.Conv2d(in_channels, out_channels, 3, padding=1),
            nn.Conv2d(out_channels, out_channels, 3, padding=1),
            nn.Conv2d(out_channels, out_channels, 3, padding=1),
        ]
        self.skip = (nn.Conv2d(in_channels, out_channels, 1, bias=False)
                     if in_channels != out_channels else None)

    def __call__(self, x: mx.array) -> mx.array:
        h = self.conv[0](x)
        h = self.conv[1](nn.relu(h))
        h = self.conv[2](nn.relu(h))
        residual = x if self.skip is None else self.skip(x)
        return nn.relu(h + residual)


def _upsample(x: mx.array) -> mx.array:
    """Nearest-neighbour x2 on a channels-last ``(N, H, W, C)`` array."""
    n, h, w, c = x.shape
    x = mx.repeat(x, 2, axis=1)
    return mx.repeat(x, 2, axis=2)


class TAEDecoder(nn.Module):
    """The flat 23-slot decoder. Input ``(N, H, W, 24)``, output ``(N, 16H, 16W, 3)`` in [0, 1]."""

    def __init__(self):
        super().__init__()
        self.conv_in = nn.Conv2d(LATENT_CHANNELS, 96, 3, padding=1)          # slot 1
        self.stage1 = [Block(96, 96) for _ in range(3)]                      # slots 3, 4, 5
        self.up1 = nn.Conv2d(96, 96, 3, padding=1, bias=False)               # slot 7
        self.stage2 = [Block(96, 96) for _ in range(3)]                      # slots 8, 9, 10
        self.up2 = nn.Conv2d(96, 96, 3, padding=1, bias=False)               # slot 12
        self.stage3 = [Block(96, 64), Block(64, 64), Block(64, 64)]          # slots 13, 14, 15
        self.up3 = nn.Conv2d(64, 64, 3, padding=1, bias=False)               # slot 17
        self.stage4 = [Block(64, 64) for _ in range(2)]                      # slots 18, 19
        self.up4 = nn.Conv2d(64, 64, 3, padding=1, bias=False)               # slot 21
        self.stage5 = [Block(64, 64)]                                        # slot 22
        self.conv_out = nn.Conv2d(64, 3, 3, padding=1)                       # slot 23

    def __call__(self, latents: mx.array) -> mx.array:
        # Slot 0: TAESD clamps its input rather than trusting the sampler's tails.
        x = mx.tanh(latents / 3.0) * 3.0
        x = nn.relu(self.conv_in(x))                                         # slots 1, 2
        for block in self.stage1:
            x = block(x)
        x = self.up1(_upsample(x))                                           # slots 6, 7
        for block in self.stage2:
            x = block(x)
        x = self.up2(_upsample(x))                                           # slots 11, 12
        for block in self.stage3:
            x = block(x)
        x = self.up3(_upsample(x))                                           # slots 16, 17
        for block in self.stage4:
            x = block(x)
        x = self.up4(_upsample(x))                                           # slots 20, 21
        for block in self.stage5:
            x = block(x)
        return self.conv_out(x) + 0.5
```

`+ 0.5` at the end is TAESD's convention: the decoder predicts an offset from mid-grey, so the output lands in `[0, 1]` rather than `[-0.5, 0.5]`. Task 2's measurement is what confirms it; if the frames come out uniformly dark or blown out, this constant is the first suspect.

- [ ] **Step 4: Run the layout tests**

Run: `./.venv/bin/python -m pytest tests/test_tae.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Write the failing loader test**

Every one of the 81 tensors must land on a parameter, with nothing missing and nothing left over — the same `strict` discipline `h3_48gb/text_encoder.py` uses, for the same reason: a silently-unloaded block decodes noise.

```python
# tests/test_tae.py (append)
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
```

- [ ] **Step 6: Run it and watch it fail**

Run: `./.venv/bin/python -m pytest tests/test_tae.py -q`
Expected: FAIL with `ImportError: cannot import name 'load_tae'`.

- [ ] **Step 7: Write the loader**

```python
# h3_48gb/tae.py (append)

#: Checkpoint slot -> module attribute. The gaps (0, 2, 6, 11, 16, 20) are parameterless layers.
SLOT_MAP = {
    1: "conv_in",
    3: "stage1.0", 4: "stage1.1", 5: "stage1.2",
    7: "up1",
    8: "stage2.0", 9: "stage2.1", 10: "stage2.2",
    12: "up2",
    13: "stage3.0", 14: "stage3.1", 15: "stage3.2",
    17: "up3",
    18: "stage4.0", 19: "stage4.1",
    21: "up4",
    22: "stage5.0",
    23: "conv_out",
}


def _parameter_path(key: str) -> str:
    """``13.conv.0.weight`` -> ``stage3.0.conv.0.weight``; ``1.weight`` -> ``conv_in.weight``."""
    slot, _, rest = key.partition(".")
    try:
        prefix = SLOT_MAP[int(slot)]
    except (ValueError, KeyError):
        raise KeyError(f"{key!r} does not belong to a known decoder slot") from None
    return f"{prefix}.{rest}"


def load_tae(path: Path | str = TAE_WEIGHTS_PATH, report: bool = False):
    """Build a `TAEDecoder` and fill every one of its parameters from `path`.

    Refuses a checkpoint that does not account for the module tree exactly. A decoder with a few
    blocks left at their initialization values does not raise and does not look obviously wrong —
    it produces plausible noise, which is the worst failure mode a preview can have.
    """
    from mlx.utils import tree_flatten, tree_unflatten

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No TAE weights at {path}")

    raw = mx.load(str(path))
    decoder = TAEDecoder()
    expected = {name for name, _ in tree_flatten(decoder.parameters())}

    resolved: dict[str, mx.array] = {}
    unused: list[str] = []
    for key, tensor in raw.items():
        try:
            name = _parameter_path(key)
        except KeyError:
            unused.append(key)
            continue
        if name not in expected:
            unused.append(key)
            continue
        resolved[name] = to_mlx_conv2d_layout(tensor) if tensor.ndim == 4 else tensor

    missing = sorted(expected - resolved.keys())
    if missing or unused:
        raise KeyError(
            f"{path.name} does not match the decoder: {len(missing)} parameters unfilled "
            f"(e.g. {missing[:3]}), {len(unused)} tensors unused (e.g. {sorted(unused)[:3]})."
        )

    decoder.update(tree_unflatten(list(resolved.items())))
    mx.eval(decoder.parameters())
    if report:
        return decoder, {"loaded": len(resolved), "missing": missing, "unused": unused}
    return decoder
```

- [ ] **Step 8: Run the loader tests**

Run: `./.venv/bin/python -m pytest tests/test_tae.py -q`
Expected: PASS. If `test_every_tensor_lands_and_nothing_is_invented` fails on `unused` or `missing`, the slot map is wrong — fix `SLOT_MAP`, not the test.

- [ ] **Step 9: Add the forward-shape test**

```python
# tests/test_tae.py (append)
def test_the_decoder_upsamples_by_exactly_sixteen():
    import mlx.core as mx

    from h3_48gb.tae import SPATIAL_RATIO, load_tae

    decoder = load_tae(TAE_WEIGHTS_PATH)
    out = decoder(mx.zeros((1, 8, 12, 24)))
    assert out.shape == (1, 8 * SPATIAL_RATIO, 12 * SPATIAL_RATIO, 3)


def test_the_ratio_matches_the_video_vae():
    """If these ever diverge, a TAE preview would be a different size from a VAE one."""
    from h3_48gb.pipeline import video_vae_config
    from h3_48gb.tae import LATENT_CHANNELS, SPATIAL_RATIO

    cfg = video_vae_config(Path.home() / "models/h3-converted/video_vae")
    assert SPATIAL_RATIO == cfg.spatial_compression_ratio
    assert LATENT_CHANNELS == cfg.latent_channels
```

Note: `test_the_ratio_matches_the_video_vae` also needs the converted checkpoint. Guard it with its own `skipif` on `~/models/h3-converted/video_vae`, since the module-level mark only covers the TAE weights.

- [ ] **Step 10: Run and commit**

Run: `./.venv/bin/python -m pytest -q` (no arguments — the root-level test files must run too)
Expected: all pass, previous count + 8.

```bash
git add h3_48gb/tae.py tests/test_tae.py
git commit -m "feat: port the TAE decoder to MLX

Architecture read from the checkpoint's slot layout rather than guessed: 81 tensors
in a flat Sequential 1..23, whose gaps are the parameterless layers — an input clamp,
a ReLU, and four x2 upsamples making the 16x that matches the video VAE.

Loading is strict in both directions. A decoder with a few blocks left at their
initialization values raises nothing and produces plausible noise, which is the worst
failure a preview can have.

Conv weights are transposed to channels-last, and the test pins values rather than
shape: every kernel here is square, so a permutation swapping kH and kW would pass a
shape check unchanged."
```

---

### Task 2: Settle the normalization by measurement — DONE 2026-08-07

**Outcome: `TAE_EXPECTS_NORMALIZED = True`, and the `+ 0.5` in Task 1's decoder was wrong.**

Two lessons worth carrying, because this plan as written would have chosen the wrong answer:

1. **PSNR against the real VAE picks the loser.** The denormalized form scores 17.50 dB against
   the VAE's own decode where normalized scores 15.11 — it wins on a colour shift that lands near
   the reference's tone, while losing on structure (gradient correlation 0.457 vs 0.508) and on
   plain correlation with that same reference (0.883 vs 0.927). A single metric, and this plan's
   3 dB threshold on it, would have settled the question backwards.
2. **Use a second reference.** Produce the latent by *encoding a known image*, so that image is
   ground truth independent of the VAE. Against it, normalized scores 21.78 dB / 0.939 where the
   real VAE manages 14.72 / 0.941 — the small decoder is closer to the original than the VAE,
   having skipped the encoder round trip and the tiling. That comparison is unambiguous where the
   VAE-only one was not.

Also: the plan told the implementer to source a latent from an interrupted run's checkpoint. Those
are cleaned up after a run completes, so none existed. Encoding an image with `video_vae._encode_clip`
is both cheaper and better — it gives the ground-truth reference above.

Measured cost at 576x384: **0.027 s and 0.52 GB** against the real VAE's 10.5 s and 7.18 GB.

The original task text follows, kept for the record.

### Task 2 (original text): Settle the normalization by measurement

**Files:**
- Create: `scripts/measure_tae.py`
- Modify: `h3_48gb/tae.py` (record the answer as a named constant and a docstring)
- Test: `tests/test_tae.py` (pin the decision)

**Interfaces:**
- Consumes: `load_tae`, `TAEDecoder` from Task 1.
- Produces: `TAE_EXPECTS_NORMALIZED: bool` in `h3_48gb/tae.py`, and `decode_latent_frame(decoder, latents, cfg, frame_index) -> np.ndarray` returning `(H, W, 3)` uint8.

**This is the task the spec says decides whether the port is trustworthy at all.** `preview.py:121` denormalizes before the real VAE (`latents * std + mean`). TAE was trained by a third party and may expect either form. Decide by measuring, not by reading.

- [ ] **Step 1: Write the measurement script**

```python
#!/usr/bin/env python3
"""Which normalization does TAE expect, and what does a preview cost?

Decodes one real latent three ways — the real VAE (the reference), TAE on normalized input, TAE
on denormalized input — and reports PSNR of each TAE variant against the reference. The loser's
score is reported too, because a small margin means something else is wrong: if both forms look
plausible, the port is not trustworthy yet and that is the finding.

Needs a real latent. `scripts/canary_i2v.py` can produce one, or point --latents at any
`*-raw.npz` from a completed run.
"""
import argparse
import time
from pathlib import Path

import numpy as np


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(((a.astype(np.float64) - b.astype(np.float64)) ** 2).mean())
    return float("inf") if mse == 0 else 20 * np.log10(255.0) - 10 * np.log10(mse)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--latents", type=Path, required=True,
                    help="npz holding a `latents` array shaped (1, 24, F, H, W), normalized")
    ap.add_argument("--checkpoint", type=Path, default=Path.home() / "models/h3-converted")
    ap.add_argument("--outdir", type=Path, default=Path.home() / "models/video-out/tae-check")
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)

    import mlx.core as mx
    from PIL import Image

    from h3_48gb.pipeline import video_vae_config
    from h3_48gb.tae import load_tae

    latents = mx.array(np.load(args.latents)["latents"])
    cfg = video_vae_config(args.checkpoint / "video_vae")
    mean = mx.array(np.array(cfg.latents_mean, np.float32)).reshape(1, -1, 1, 1, 1)
    std = mx.array(np.array(cfg.latents_std, np.float32)).reshape(1, -1, 1, 1, 1)

    # 1. The reference: the real VAE, on the denormalized latent, exactly as preview.py does.
    from minimax_h3_mlx.load import load_video_vae
    from minimax_h3_mlx.packing import PIXEL_MEAN, PIXEL_STD

    vae = load_video_vae(args.checkpoint / "video_vae")
    needed = 2 * cfg.clip_length // 2 - cfg.token_drop if False else 7   # see min_preview_latent_frames
    started = time.perf_counter()
    mx.reset_peak_memory()
    frames = np.array(vae.decode((latents[:, :, :needed] * std + mean).astype(mx.float32)))
    vae_seconds, vae_peak = time.perf_counter() - started, mx.get_peak_memory() / 1e9
    pixel_mean = np.array(PIXEL_MEAN, np.float32).reshape(1, 3, 1, 1, 1)
    pixel_std = np.array(PIXEL_STD, np.float32).reshape(1, 3, 1, 1, 1)
    reference = np.clip(frames * pixel_std + pixel_mean, 0, 1)[0].transpose(1, 2, 3, 0)
    reference = (reference[reference.shape[0] // 2] * 255 + 0.5).astype(np.uint8)
    del vae

    # 2. TAE, both ways, on the same latent frame the reference came from.
    decoder = load_tae()
    frame_index = needed // 2
    results = {}
    for label, tensor in (("normalized", latents),
                          ("denormalized", latents * std + mean)):
        chw = tensor[0, :, frame_index]                       # (24, H, W)
        hwc = mx.transpose(chw, (1, 2, 0))[None]              # (1, H, W, 24)
        mx.reset_peak_memory()
        started = time.perf_counter()
        out = decoder(hwc.astype(mx.float32))
        mx.eval(out)
        seconds, peak = time.perf_counter() - started, mx.get_peak_memory() / 1e9
        rgb = (np.clip(np.array(out)[0], 0, 1) * 255 + 0.5).astype(np.uint8)
        Image.fromarray(rgb).save(args.outdir / f"tae-{label}.png")
        results[label] = {"psnr": round(psnr(reference, rgb), 2),
                          "seconds": round(seconds, 3), "peak_gb": round(peak, 2)}

    Image.fromarray(reference).save(args.outdir / "vae-reference.png")
    winner = max(results, key=lambda k: results[k]["psnr"])
    margin = results[winner]["psnr"] - results[min(results, key=lambda k: results[k]["psnr"])]["psnr"]

    print(f"reference (real VAE): {vae_seconds:.1f}s, peak {vae_peak:.2f} GB")
    for label, r in results.items():
        print(f"  TAE {label:13s}: PSNR {r['psnr']:6.2f} dB, {r['seconds']:.3f}s, "
              f"peak {r['peak_gb']:.2f} GB")
    print(f"\nwinner: {winner} (margin {margin:.2f} dB)")
    if margin < 3.0:
        print("MARGIN TOO SMALL — the spec says both forms looking plausible means the port is "
              "wrong, not that either will do. Do not pick a winner on this evidence.")
        return 1
    # NOTE (added after running this): a margin on PSNR-against-the-VAE alone is not sufficient
    # evidence either. It chose the wrong form. Compare against the source image too, and look at
    # structure (gradient correlation), not only intensity.
    print(f"speedup vs the real VAE: {vae_seconds / results[winner]['seconds']:.0f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Produce a latent to measure on**

The npz from any completed run holds decoded pixels, not latents, so extract latents from a checkpoint file instead — the resume checkpoints written under `~/models/video-out/*/checkpoints/` hold exactly the packed rows:

```bash
ls ~/models/video-out/i2v-check/checkpoints/*.safetensors
```

Write a five-line helper inline (not a committed script) that loads one, calls `unpatchify_video_tokens` the way `render_preview_frame` does, and saves `latents` into an npz. If no checkpoint exists, run `./.venv/bin/python scripts/canary_i2v.py --no-image --width 512 --height 512 --canary-steps 2` first — it takes about 4 minutes and writes checkpoints along the way.

- [ ] **Step 3: Run the measurement**

Run: `./.venv/bin/python scripts/measure_tae.py --latents /tmp/latents.npz`
Expected: a winner with a margin of at least 3 dB. **If the margin is under 3 dB, stop and report it — that is the spec's stated failure condition, and it means the port is wrong, not that the measurement is inconclusive.**

- [ ] **Step 4: Look at the images**

Open `~/models/video-out/tae-check/vae-reference.png`, `tae-normalized.png` and `tae-denormalized.png`. The winner must be recognisably the same scene as the reference. The loser must look visibly wrong. If both look fine, the numbers are lying and Step 3's guard did not fire — report it.

- [ ] **Step 5: Record the answer in code**

```python
# h3_48gb/tae.py (append near the top constants)

#: Whether TAE wants the latent as the sampler holds it (normalized) or after `latents * std + mean`.
#: Measured, not assumed: see `scripts/measure_tae.py` and docs/RESULTS.md. The losing form scored
#: <LOSER> dB against the real VAE's decode where the winner scored <WINNER> dB.
TAE_EXPECTS_NORMALIZED = <True or False>
```

Replace `<LOSER>`, `<WINNER>` and the boolean with the measured values. Do not leave placeholders.

- [ ] **Step 6: Add the decode helper**

```python
# h3_48gb/tae.py (append)

def decode_latent_frame(decoder, latents: mx.array, latents_mean, latents_std,
                        frame_index: int) -> "np.ndarray":
    """One latent frame -> one ``(H, W, 3)`` uint8 RGB frame.

    ``latents`` is ``(1, 24, F, H, W)`` as the sampler holds it — normalized. Whether TAE is fed
    that directly or the denormalized form is `TAE_EXPECTS_NORMALIZED`, settled by measurement.
    """
    import numpy as np

    if not TAE_EXPECTS_NORMALIZED:
        latents = latents * latents_std + latents_mean
    chw = latents[0, :, frame_index]
    hwc = mx.transpose(chw, (1, 2, 0))[None].astype(mx.float32)
    out = decoder(hwc)
    mx.eval(out)
    return (np.clip(np.array(out)[0], 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
```

- [ ] **Step 7: Pin the decision with a test**

```python
# tests/test_tae.py (append)
def test_the_normalization_choice_is_recorded_not_left_open():
    from h3_48gb import tae

    assert isinstance(tae.TAE_EXPECTS_NORMALIZED, bool)
    doc = tae.__doc__ or ""
    source = Path(tae.__file__).read_text()
    assert "measure_tae" in source, (
        "the constant must point at the measurement that produced it")


def test_decoding_one_frame_returns_a_native_resolution_rgb_frame():
    import mlx.core as mx
    import numpy as np

    from h3_48gb.tae import SPATIAL_RATIO, decode_latent_frame, load_tae

    decoder = load_tae(TAE_WEIGHTS_PATH)
    latents = mx.zeros((1, 24, 3, 8, 12))
    mean = mx.zeros((1, 24, 1, 1, 1))
    std = mx.ones((1, 24, 1, 1, 1))
    frame = decode_latent_frame(decoder, latents, mean, std, frame_index=1)
    assert frame.shape == (8 * SPATIAL_RATIO, 12 * SPATIAL_RATIO, 3)
    assert frame.dtype == np.uint8
```

- [ ] **Step 8: Run and commit**

Run: `./.venv/bin/python -m pytest -q`

```bash
git add h3_48gb/tae.py tests/test_tae.py scripts/measure_tae.py
git commit -m "feat: settle TAE's input normalization by measurement

Decoded one real latent three ways — the real VAE as reference, TAE normalized, TAE
denormalized — and compared by PSNR. <WINNER> dB against <LOSER> dB. The margin
matters as much as the winner: the spec calls a small one evidence that the port is
wrong rather than that either form will do, so the script refuses to pick below 3 dB."
```

---

### Task 3: Offer TAE at the call site, without weakening the fallback

**Files:**
- Modify: `h3_48gb/preview.py`
- Test: `tests/test_preview_tae.py` (new — `test_preview.py` at the repo root is already large)

**Interfaces:**
- Consumes: `load_tae`, `decode_latent_frame`, `TAE_WEIGHTS_PATH` from Tasks 1-2.
- Produces: `render_tae_frame(...)`; `emit_preview(..., decoder: str = "vae")` accepting `"vae"`, `"tae"`, `"latent"`.

The existing guarantee is the constraint: **`emit_preview` never raises**, and that is already covered by a test which must stay green. The fallback order when TAE is selected is TAE → latent heat map → skip, logging each step.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_preview_tae.py
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
```

Note `test_an_unknown_decoder_name_is_refused_loudly` is the one deliberate exception to never-raises: it fires on a programming error before any work starts, not on a decode failure mid-run. State that in the docstring of `emit_preview`.

- [ ] **Step 2: Run and watch them fail**

Run: `./.venv/bin/python -m pytest tests/test_preview_tae.py -q`
Expected: FAIL — `emit_preview() got an unexpected keyword argument 'decoder'`.

- [ ] **Step 3: Add the TAE render path**

```python
# h3_48gb/preview.py (append near render_preview_frame)

def render_tae_frame(
    pipeline,
    generated_video_rows: mx.array,
    num_latent_frames: int,
    latent_height: int,
    latent_width: int,
    patch_size,
) -> np.ndarray:
    """Decode one frame with TAE: no VAE, no chunk floor, no tiling.

    Unlike `render_preview_frame` this can decode *any* single frame, because TAE has no temporal
    state — so it takes the middle one, where the clip is most representative, rather than being
    forced to start at frame 0 by the real VAE's causal padding.
    """
    from minimax_h3_mlx.packing import unpatchify_video_tokens

    from h3_48gb.tae import TAE_WEIGHTS_PATH, decode_latent_frame, load_tae

    cfg = pipeline.video_vae.config if hasattr(pipeline, "video_vae") else None
    latent_channels = 24 if cfg is None else cfg.latent_channels
    latents = unpatchify_video_tokens(
        generated_video_rows, num_latent_frames, latent_height, latent_width,
        latent_channels, patch_size,
    )
    mean = mx.zeros((1, latent_channels, 1, 1, 1))
    std = mx.ones((1, latent_channels, 1, 1, 1))
    if cfg is not None:
        mean = mx.array(np.array(cfg.latents_mean, np.float32)).reshape(1, -1, 1, 1, 1)
        std = mx.array(np.array(cfg.latents_std, np.float32)).reshape(1, -1, 1, 1, 1)

    decoder = load_tae(TAE_WEIGHTS_PATH)
    return decode_latent_frame(decoder, latents, mean, std, num_latent_frames // 2)
```

The `hasattr` dance exists because a TAE preview must work without the VAE ever being loaded, which is the point of the decoder — but the normalization constants live in the VAE's config. Read them if the config is reachable for free (it is: `LazyComponent.config` does not trigger a load), and fall back to identity otherwise.

- [ ] **Step 4: Wire the choice into `emit_preview`**

Replace the body's first `try` with a dispatch, keeping the fallback chain and the `never raises` contract:

```python
# h3_48gb/preview.py — inside emit_preview, replacing the first try block

    DECODERS = {"vae": render_preview_frame, "tae": render_tae_frame, "latent": None}
    if decoder not in DECODERS:
        # A typo must not quietly decode with something else. This fires before any work, on a
        # programming error — it is not the mid-run decode failure the rest of this function
        # promises to swallow.
        raise ValueError(f"Unknown preview decoder {decoder!r}; expected one of {sorted(DECODERS)}.")

    render = DECODERS[decoder]
    if render is not None:
        try:
            frame = render(
                pipeline, generated_video_rows, num_latent_frames, latent_height, latent_width,
                patch_size,
            )
            save_preview_jpeg(frame, dest)
            if verbose:
                print(f"  [preview] step {step}: wrote {dest.name} via {decoder} "
                      f"({time.perf_counter() - started:.1f}s)", flush=True)
            return True
        except Exception as exc:  # noqa: BLE001 - a broken preview must never break the real run
            print(f"  [preview] step {step}: {decoder} decode failed ({exc!r}), "
                  f"falling back to a latent-only preview", file=sys.stderr, flush=True)
```

Add `decoder: str = "vae"` to the signature, after `verbose`, and document it in the docstring alongside the never-raises paragraph.

- [ ] **Step 5: Run the new tests**

Run: `./.venv/bin/python -m pytest tests/test_preview_tae.py -q`
Expected: PASS (5 tests).

- [ ] **Step 6: Run the existing preview tests, unchanged**

Run: `./.venv/bin/python -m pytest test_preview.py -q`
Expected: PASS, same count as before this task. If any fails, the VAE path was disturbed — that is a defect in this task, not a test to update.

- [ ] **Step 7: Commit**

```bash
git add h3_48gb/preview.py tests/test_preview_tae.py
git commit -m "feat: let emit_preview decode with TAE instead of the video VAE

Three states — vae (unchanged default), tae, latent — with the fallback chain and the
never-raises guarantee intact: a TAE failure drops to the latent heat map exactly as a
VAE failure does. A missing or corrupt weights file is a clean fallback, not an error.

The one deliberate exception: an unknown decoder name raises. It is a programming error
caught before any work starts, not the mid-run failure this function exists to swallow,
and silently decoding with something other than what the caller asked for is worse."
```

---

### Task 4: Expose it, measure the cost, and write down what it is worth

**Files:**
- Modify: `h3_48gb/cli.py`
- Modify: `docs/RESULTS.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `emit_preview(..., decoder=...)` from Task 3.
- Produces: `--preview-decoder {vae,tae,latent}` on `generate` and `resume`; `RunSpec.preview_decoder: str = "vae"`.

- [ ] **Step 1: Write the failing CLI tests**

```python
# tests/test_cli.py (append)
def test_the_preview_decoder_defaults_to_the_real_vae(tmp_path):
    spec = spec_from_args(build_parser().parse_args(
        ["generate", "a cat", "--outdir", str(tmp_path)]))
    assert spec.preview_decoder == "vae"


def test_the_preview_decoder_can_be_chosen(tmp_path):
    spec = spec_from_args(build_parser().parse_args(
        ["generate", "a cat", "--preview-decoder", "tae", "--outdir", str(tmp_path)]))
    assert spec.preview_decoder == "tae"


def test_an_unknown_preview_decoder_is_refused_by_the_parser(tmp_path):
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["generate", "a cat", "--preview-decoder", "taa", "--outdir", str(tmp_path)])


def test_the_preview_decoder_reaches_the_pipeline(tmp_path):
    """The flag is worthless if it stops at the RunSpec."""
    seen = {}

    def factory(_checkpoint):
        def pipe(**kwargs):
            seen.update(kwargs)
            return _StubResult()
        return pipe

    spec = _spec(tmp_path, preview_every=2, preview_decoder="tae")
    run_generate(spec, pipeline_factory=factory)
    assert seen.get("preview_decoder") == "tae"
```

- [ ] **Step 2: Run and watch them fail**

Run: `./.venv/bin/python -m pytest tests/test_cli.py -q`
Expected: FAIL — `RunSpec` has no `preview_decoder`.

- [ ] **Step 3: Add the field, the flag, and the wiring**

In `h3_48gb/cli.py`:

```python
# In RunSpec, after preview_stem:
    #: Which decoder in-flight previews use. The real VAE is correct but costs 49.3 s and 5.21 GB
    #: per preview; `tae` is an approximation for watching progress and never for the delivered
    #: clip; `latent` is the VAE-free heat map.
    preview_decoder: str = "vae"

# In _add_run_flags, beside the other preview flags:
    sub.add_argument("--preview-decoder", choices=("vae", "tae", "latent"), default="vae",
                     help="decoder for in-flight previews (default: vae, the real one)")

# In spec_from_args, in the RunSpec(...) call:
        preview_decoder=args.preview_decoder,
```

Then thread it through `h3_48gb/preview.py`'s `pop_preview_kwargs` and `PreviewInterceptor` the same way `preview_stem` is threaded, and pass it to `emit_preview`. Follow the existing `preview_stem` path exactly — every place that names `preview_stem` needs the same treatment for `preview_decoder`.

- [ ] **Step 4: Run the tests**

Run: `./.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Measure the real cost at native resolution**

The spec asks for time and peak memory measured the same way the 49.3 s / 8.46 GB figure was — at 1344x768.

Run:
```bash
./.venv/bin/python scripts/canary_i2v.py --no-image --width 1344 --height 768 \
  --duration 3.0 --canary-steps 2
```
with `--preview-every 1 --preview-decoder tae` threaded through (the canary does not expose preview flags; add them, or drive one preview directly from a Python one-liner using a checkpoint's rows). Record seconds per preview and peak GB.

- [ ] **Step 6: Judge the frames by eye**

Save a TAE preview and the real VAE's decode of the same latent side by side. The spec's bar, set by TAE's own author, is "beats latent2rgb". If the TAE frame is not recognisably the same scene as the VAE's, **do not merge** — record the finding and keep the branch.

- [ ] **Step 7: Write the numbers into RESULTS.md**

Add a `## TAE previews` section to `docs/RESULTS.md` with: the normalization decision and both PSNR figures from Task 2, seconds and peak GB per preview against the VAE's 49.3 s / 8.46 GB, the speedup, and one sentence on visual quality. If any of the spec's three stated no-merge conditions were hit — frames not recognisably the same scene, speedup under about 10x, or changes needed to `upstream/` or the real preview path — say so plainly instead.

- [ ] **Step 8: Commit**

```bash
git add h3_48gb/cli.py h3_48gb/preview.py tests/test_cli.py docs/RESULTS.md
git commit -m "feat: --preview-decoder, and the measured cost of a TAE preview

<N>x faster than the real VAE (<X>s against 49.3s) at <Y> GB against 8.46 GB, and
without the 7-latent-frame floor, so a preview can show any frame rather than only the
first decodable second.

The default stays the real VAE: an experimental decoder must not become the default by
being merged."
```

---

## Self-review

**Spec coverage.** Verification item 1 (all 81 tensors map, strict) → Task 1 Step 5. Item 2 (normalization decision with both PSNRs reported) → Task 2 Steps 3-5. Item 3 (time and peak memory at 1344x768) → Task 4 Step 5. Item 4 (failure path holds with the file absent and corrupt) → Task 3 Step 1. Item 5 (a frame a human can judge) → Task 4 Step 6. The "what would make this not worth merging" conditions are carried into Task 4 Step 7 rather than left in the spec.

**Placeholders.** Two intentional ones remain, both in commit messages and one constant, marked `<WINNER>`/`<LOSER>`/`<N>`: they are measurement results that do not exist until the task runs, and each is accompanied by an explicit instruction not to leave them unreplaced.

**Type consistency.** `load_tae` returns `TAEDecoder`, or `(TAEDecoder, dict)` when `report=True` — used both ways in Task 1's tests. `decode_latent_frame` takes `(decoder, latents, latents_mean, latents_std, frame_index)` and is called with exactly that in Task 3. `emit_preview`'s new parameter is `decoder`, matching `DECODERS` and the CLI's `preview_decoder` field, which are deliberately different names: one is the function's parameter, the other the request field, and Task 4 Step 3 maps between them.

**One risk worth naming.** Task 3's `render_tae_frame` reads `pipeline.video_vae.config` for the normalization constants. `LazyComponent.config` resolves without loading weights, so this is free — but if that ever changes, a TAE preview would start loading 5.21 GB, silently undoing the point. `test_a_tae_preview_never_touches_the_video_vae` is what catches that, which is why its stub raises on the attribute rather than returning a mock.
