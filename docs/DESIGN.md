# Design notes

Facts established by reading the code and the safetensors headers directly, not from
documentation.

## What blocks a run

`pipeline.py::from_pretrained` (lines 92-113 of the upstream port) loads the text encoder, the
DiT and both VAEs one after another and keeps all of them resident until generation finishes —
**55.5 GB** on a machine with **48 GB** physical. The mere.run runtime refuses this twice: a
declared-support threshold of 96 GB (liftable with `--allow-unsupported`), and, separately,
admission control that requires 32 GB free before it will admit any job at all.

The four components are needed strictly by phase, though:

| Phase | Component | Where in `__call__` |
|---|---|---|
| 1. Text encoding | `text_encoder` | line 238, called once |
| 2. Keyframes | `video_vae.encode` | line 270, only with `--image` |
| 3. Diffusion | `dit` | lines 306-318 |
| 4. Decode | `video_vae`, `audio_vae` | lines 368, 382 |

## Sawfwair weight layout vs. what the port expects

| | Sawfwair (as downloaded) | Port expects |
|---|---|---|
| Structure | monolithic `<component>.safetensors` files | `FL2VA/<component>/` directories, sharded |
| Quantization | MLX affine: `weight` U32 + `scales`/`biases` BF16 | same ✅ |
| Tensor names | `blocks.N.attn.qkv_proj.*`, `model.layers.N.mlp.*` | same ✅ |
| QKV layout | `[all-q; all-k; all-v]` slabs | **per-head interleave** `[h0:q,k,v][h1:q,k,v]...` ❌ |
| Quantization recipe | a field inside `config.json` | separate `quant_config.json` ❌ |

The QKV permutation is cheap: `scales [21504, 84]` means grouping runs along input features
(5376 / 64 = 84 groups), not along rows. Rows move together with their own `scales` and `biases`,
so no dequantization is required. 56 heads x 128 = 7168 = `inner_dim`.

## The work

1. **Converter** (`convert_sawfwair.py`): unpack the monoliths into the port's directory
   structure, permute QKV into the per-head interleave, and generate `quant_config.json`. Avoid
   copying weights that need no change — tensors that don't require permutation are written
   as-is.
2. **Lazy loading** in `pipeline.py`: `from_pretrained` reads only configs; weights load on
   demand. Configs are needed early (`dit.config.patch_size`, `video_vae.config`), so they are
   split from the weights.
3. **Encoder eviction** after line 238. Critical detail: `mx.eval(prompt_embeds)` must run before
   eviction — MLX is lazy, and without materializing the output the graph keeps the encoder's
   weights alive, so nothing is actually freed.
4. **Verification**: 512x512, 56 frames — the geometry with the largest memory headroom. Only
   attempt native 1344x768 after that succeeds.

## Expected memory use after the patches

| Phase | Resident | Peak |
|---|---|---|
| Encoding | encoder Q8, 28.2 GB | ~29 GB |
| Diffusion, 512x512 | DiT 11.3 + VAE 5.8 + cache 0.9 + activations ~2 | ~20 GB |
| Diffusion, 5 s at 1344x768 | same + activations 9.3 | ~27 GB |
| Diffusion, 15 s at 1344x768 | same + activations 24.4 | ~42 GB |

GPU limit raised to 44 GB (`iogpu.wired_limit_mb=45056`; resets on reboot).

## What this fork does not solve

Speed. The bottleneck is dense-attention FLOPs — MiniMax has not released a sparse-attention
implementation. The Spectrum accelerator (ComfyUI-Spectrum-MiniMax-H3) gets 30-35% by skipping
steps, but needs 2.2-6.1 GB for prediction history, degrades fast motion, and is written for
ComfyUI/PyTorch. Worth revisiting once the pipeline itself works, not before.

See `docs/RESULTS.md` for the measured numbers this plan predicted, and the README's "The four
patches" section for how the patches above map onto `h3_48gb/`.
