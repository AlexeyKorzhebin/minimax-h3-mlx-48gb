# minimax-h3-mlx-48gb

A fork of [PipeNetwork/minimax-h3-mlx](https://github.com/PipeNetwork/minimax-h3-mlx) (Apache-2.0),
the MLX port of [MiniMax H3](https://huggingface.co/MiniMaxAI/MiniMax-H3) — a 33B diffusion
transformer that generates synchronized video and audio — that makes it run on a Mac with 48 GB of
unified memory. The upstream port and the reference Swift runtime both assume far more memory than
that is available; this fork changes nothing about the model, only when each of its parts is
resident, and adds the checkpointing, previews and CLI a multi-hour local render needs in practice.

## The memory problem, in numbers

The upstream pipeline loads every component at `from_pretrained` and keeps all of them resident for
the whole run: text encoder, DiT, video VAE, audio VAE — **55.5 GB**. This machine has **48 GB**.
mere.run, the Swift runtime whose model build this fork's converter targets, will not even attempt
it: it refuses outright with *"Requires at least 96 GB unified memory; detected 48 GB"*, and,
separately, its admission control demands 32 GB free before it will admit any job at all.

Both numbers describe the model's declared footprint, not a requirement of the computation itself.
The four components are needed in disjoint phases — the text encoder runs once, at the very start,
and is dead weight for the rest of the render — so loading them one phase at a time and discarding
each as its phase ends brings the measured peak down to about **11 GB**.

## The four patches

Everything lives in `h3_48gb/`; `upstream/` is a vendored, unmodified clone, so it can be
fast-forwarded against the original project at any time.

1. **`h3_48gb/adaln.py`** — the mere.run build ships no AdaLN modulation path at all (106 tensors
   absent: 50x `blocks.N.adaln_proj`, `final_layer.adaln_proj`, `time_embedder`); it precomputed
   them into `adaln_cache.safetensors` for one fixed schedule, so this module serves the port's
   `ModulationCache` interface from that file instead of computing it. Consequence: **only
   `num_inference_steps=31`** (31 grid points, 30 forwards, sigma shifts 12.0/3.0) is servable —
   any other schedule fails loudly, not silently.
2. **`h3_48gb/text_encoder.py`** — the port builds plain `nn.Linear` layers, so mere.run's 8-bit
   conditioner would load as packed uint32 into bf16 slots and silently produce garbage; this
   quantizes selectively per the converter's manifest (439 modules — `visual.blocks.N.mlp.linear_fc2`
   is deliberately left dense, since mere.run shipped it that way).
3. **`h3_48gb/pipeline.py`** — components load per phase instead of all at once at
   `from_pretrained`.
4. **`h3_48gb/pipeline.py`** (same module) — the text encoder is unloaded immediately after its one
   call, `encode()`. Its output is materialized first (`mx.eval`; MLX is lazy, and an unevaluated
   graph keeps the encoder's weights alive), then `gc.collect()` runs before `mx.clear_cache()`,
   because module trees form reference cycles that plain refcounting does not break.

Alongside the patches, **`convert_sawfwair.py`** turns a user's own Sawfwair (mere.run) download
into the directory layout the port reads — including undoing mere.run's QKV re-layout, from its
`[all-q; all-k; all-v]` slabs back to the port's per-head interleave, across 52 matrices.
Quantization survives the move unchanged: grouping runs along input features, so each row carries
its own scales and biases with it as it moves.

Beyond the original plan, this fork also adds crash-safe checkpoint/resume (`h3_48gb/checkpoint.py`),
in-flight frame previews so a multi-hour render is watchable instead of opaque (`h3_48gb/preview.py`),
and a CLI (`h3_48gb/cli.py`) with `generate`, `resume`, `list` and `doctor` subcommands, each able to
emit machine-readable JSON for scripting or an MCP wrapper.

The full memory-phase breakdown and the QKV/quantization mapping behind the patches above are in
[`docs/DESIGN.md`](docs/DESIGN.md).

## Quickstart

```bash
# 0. Dependencies: this needs an Apple Silicon Mac, Python 3.12, and ffmpeg on PATH.
#    upstream/ is a vendored, unmodified clone this fork patches from the outside — see "The
#    four patches" above — and is never committed here, so clone it yourself first.
git clone https://github.com/PipeNetwork/minimax-h3-mlx upstream
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/pip install -e .   # installs the `h3` console script

# 1. Get the weights. Use the mere.run app to download the MiniMax H3 (FL2VA) build — mere.run
#    will refuse to *run* it on a 48 GB machine, but downloading is unaffected. It lands at:
#    ~/Library/Application Support/MereRun/models/video-minimax-h3-fl2va-mlx/

# 2. Convert that download into the layout this port reads. --check cross-validates the written
#    key sets against the port's own module trees before you trust a multi-hour run to it.
./.venv/bin/python convert_sawfwair.py --out ~/models/h3-converted --check

# 3. Verify the converted checkpoint has everything this fork needs, including the AdaLN cache.
h3 doctor --checkpoint ~/models/h3-converted

# 4. Generate. --steps defaults to 31 — the only schedule the baked AdaLN table covers.
h3 generate "a jeweled hummingbird hovering beside a red orchid, cinematic natural light" \
    --checkpoint ~/models/h3-converted --outdir ~/models/video-out --width 1344 --height 768
```

`requirements.txt` covers both this fork and the vendored `upstream/` port: upstream's own
`requirements.txt` omits `mlx-vlm` (imported unconditionally by its text encoder) and `pillow`
(used by this fork's preview writer), so installing from it alone fails the first `h3 generate`
with `ModuleNotFoundError: No module named 'mlx_vlm'`.

`h3 resume` continues an interrupted run from its checkpoint rather than restarting it; `h3 list`
lists finished runs under `--outdir`. Every subcommand accepts `--json` for a machine-readable
report instead of human-readable text.

## Measured results

All runs at the one schedule the baked AdaLN table supports: 31 grid points (30 forwards), sigma
shifts 12.0/3.0. Full per-run detail, the scaling curve and the memory profile are in
[`docs/RESULTS.md`](docs/RESULTS.md).

| Resolution | Clip length | Per step | Total | Peak RSS | Swap |
|---|---|---|---|---|---|
| 512x512 | 2.4 s | 46 s | 24 min | 11.0 GB | 0 |
| 1344x768 (native) | 5 s | 586 s | 299 min | 10.0 GB | 0 |
| 1344x768 (native) | 10 s | 1881 s | ~15.7 h (extrapolated) | — | — |

The 10-second run was measured over 2 steps and then abandoned deliberately, not crashed — see
`docs/RESULTS.md` for why scaling is worse than linear (attention is dense; MiniMax has not
released the sparse-attention implementation, so this is an attention-FLOPs bottleneck, not a
memory one, and quantization does not help it).

## Sample frame

![A hummingbird feeding at a red orchid, sharp feather detail with motion-blurred wings](docs/media/native5-frame.jpg)

Frame 40 of the native 1344x768, 5-second run above. This is the proof the conversion is correct,
not just that it runs: a wrong QKV permutation or a mis-indexed AdaLN modulation table would produce
noise at this resolution, not a coherent bird.

## Licensing

This repository redistributes no model weights. `upstream/` is a vendored, unmodified clone of
[PipeNetwork/minimax-h3-mlx](https://github.com/PipeNetwork/minimax-h3-mlx), licensed
Apache-2.0. The code in `h3_48gb/` and the conversion script are original to this fork, under the
same license.

MiniMax H3 itself — the weights `convert_sawfwair.py` reads and rewrites — is governed by the
**MiniMax H3 Community License**, which excludes use, distribution and display of the model in the
**United States, the European Union, the United Kingdom and the Republic of Korea**, and imposes
notice obligations on downstream redistribution. This project does not obtain, host or distribute
those weights; you download and convert your own copy, and are responsible for confirming the
license permits your use before you do.

See [`LICENSE`](LICENSE) for the full Apache-2.0 text and [`NOTICE`](NOTICE) for the complete
attribution and licensing statement.
