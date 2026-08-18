# Torch-free keyframe preprocessing — design

**Status:** approved 2026-08-07. Blocks `--image` from working at all.

## The problem

`--image` reaches the model and dies. `upstream/minimax_h3_mlx/text_encoder.py:170-175` resolves its
image processor through `transformers.AutoProcessor.from_pretrained`, which builds the *composite*
`Qwen3VLProcessor` — image processor, video processor and tokenizer together. The video half needs
`torchvision`, which needs `torch`, neither of which this fork installs. The run loads the 28.22 GB
text encoder, then raises.

Upstream knows: their issue #2, "Fix keyframe generation without PyTorch", is open. We are pinned at
`fcd9e9b`, which is the tip of their `main` — there is nothing newer to pull.

Nothing here was caught by 130 passing tests, because a text-only prompt never touches
`self.processor`, and every unit test that exercises `--image` substitutes the pipeline wholesale.
The flags, the anchors and the checkpoint identity are all correct and all tested; the model simply
cannot receive the image.

## What makes this cheap

`upstream`'s consumer only ever uses one attribute:

```python
vision = self.processor.image_processor(images=images, return_tensors="np")   # line 193
merge  = self.processor.image_processor.merge_size**2                          # line 196
```

So the composite processor is not needed — only its `image_processor` member.

And a torch-free implementation is already installed: **`mlx_vlm.models.qwen3_vl.processing_qwen3_vl.Qwen3VLImageProcessor`**.
Verified by experiment before writing this spec — constructed from our own
`~/models/h3-converted/processor/preprocessor_config.json`, it processed a 640×384 image and returned
`pixel_values (960, 1536) float32` and `image_grid_thw (1, 3) int64`, with neither `torch` nor
`torchvision` entering `sys.modules`. `merge_size=2`, `patch_size=16`.

## Design

**A small module, `h3_48gb/image_processor.py`**, exposing:

- `load_image_processor(processor_dir: Path) -> Qwen3VLImageProcessor` — reads
  `preprocessor_config.json`, drops the keys that describe the composite (`processor_class`,
  `image_processor_type`, `auto_map`) and constructs the mlx-vlm processor from the rest.
- `TorchFreeProcessor` — an object with a single `image_processor` attribute, shaped to satisfy
  upstream's two call sites and nothing more.

**`h3_48gb/text_encoder.py`'s `QuantizedTextEncoder` overrides the `processor` property** to return
`TorchFreeProcessor`, so `AutoProcessor` is never reached. `upstream/` stays untouched, as everywhere
else in this fork.

**Failure is loud.** If `mlx_vlm` is missing or the config cannot be read, raise with a message naming
what is wrong. A silent fallback to `AutoProcessor` would reintroduce the torch dependency at the
worst moment — 28 GB into a run.

## The risk that decides whether this is correct

Two independent implementations of the same preprocessing can disagree in resize rounding,
normalisation or patch ordering. A disagreement does not raise — it conditions the clip on a subtly
different image, and the only symptom is worse output hours later. Reading both sources and finding
them plausible is not enough.

**So the equivalence is established numerically, once, against `transformers` itself.**

- Build a throwaway virtualenv **outside the repository** (`/tmp`, not the synced project folder),
  install `torch` + `transformers` there, and run the real `Qwen3VLImageProcessor` over a fixed set of
  test images.
- Save its `pixel_values` and `image_grid_thw` as `.npz` fixtures committed to the repo.
- Delete the throwaway venv. `torch` never enters `requirements.txt`, `pyproject.toml`, or the
  project venv.
- A permanent test compares our processor's output against those fixtures — so the equivalence keeps
  being checked on every run of the suite, forever, without torch.

Fixture images must cover what actually varies: a landscape and a portrait aspect (resize rounding
differs), a size below `min_pixels` and one above `max_pixels` (both bounds are exercised), and a
size that is not a multiple of `patch_size * merge_size` (the padding path). Four images, four
fixtures.

**Tolerance:** `pixel_values` must match within `1e-5` and `image_grid_thw` exactly. If they do not,
this design is wrong and the finding is the deliverable — record it, do not tune the tolerance until
it passes.

## Verification

1. The fixture comparison above, as a permanent test.
2. `import h3_48gb` still does not load `mlx.core` — the existing subprocess test must stay green,
   and a new one asserts `torch` is absent from `sys.modules` after a keyframe is processed.
3. The end-to-end run that Task 4 of the previous plan could not complete: one conditioned generation
   at 512×512 with its unconditioned control, comparing frame 0 against the input image. This is what
   proves the whole chain, and it is the reason this work exists.

## Out of scope

- The composite processor's video half. We never generate from video references; that is Ref2VA,
  a different partition we have not converted.
- Contributing the fix upstream. Worth doing later — their issue #2 is open and this is a real
  answer to it — but it is not a precondition for our own `--image` working.
