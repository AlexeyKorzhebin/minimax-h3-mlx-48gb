# Image-to-video (fl2va) in the CLI — design

**Status:** approved 2026-08-07.

## Why

The converted checkpoint declares `tasks: ["t2va", "fl2va"]` and `partition: fl2va`. The
pipeline already accepts `images` and `keyframe_anchors`, `_encode_keyframes` is implemented,
and our own `LazyMiniMaxH3Pipeline` sets `load_vision=True` by default precisely so the vision
tower comes along with the encoder. The one thing missing is a way to reach any of it: `h3
generate` has no image flag at all.

So this is not an experiment. It is exposing a capability that is already paid for — the vision
weights are loaded on every run today whether or not anyone can use them.

## Scope

Two modes, both native to the `fl2va` partition:

| Invocation | Anchors | Meaning |
|---|---|---|
| `--image first.png` | `("first",)` | conventional image-to-video |
| `--image first.png --end-image last.png` | `("first", "last")` | interpolate between a given start and end |

Anything else is out of scope. The checkpoint was trained on first/last conditioning; arbitrary
counts and orderings are off-distribution, and the pipeline would accept them silently.

## Interface

`--image` and `--end-image`, both optional paths, on `generate` and `resume`.

Rejected alternative: repeatable `--image` plus repeatable `--anchor`, as in
`upstream/scripts/generate.py`. It is more flexible and that is the problem — the two lists can
disagree in length, `last` can be passed without `first`, and a third image can be supplied. The
chosen form makes the unsupported cases *inexpressible* rather than validated-against. It also
maps cleanly onto an MCP tool schema: two optional strings, no coupled arrays.

## Implementation

**`RunSpec`** gains `image: Path | None` and `end_image: Path | None`.

**Validation in `RunSpec.__post_init__`**, beside the existing geometry and schedule checks, so
it fires before any weight is touched:
- `--end-image` without `--image` → `CliError("end_image_without_image")`
- either path missing on disk → `CliError("image_not_found")` naming the path

Both codes join `ERROR_CODES`; the module's existing rule holds — `CliError.__init__` asserts the
code is listed, so neither can be coined inline.

**`run_generate`** loads the images with PIL and applies `ImageOps.exif_transpose` before
handing them over. Without it a phone photo arrives rotated and the conditioning silently
describes a different frame than the user saw. Then it passes `images=[...]` and
`keyframe_anchors=("first",)` or `("first", "last")` to the pipeline, which already handles the
rest.

**Checkpoint identity must include the images.** `request_identity` already hashes them through
`_image_digest`. Verified while writing this spec: the digest is taken over content in both
forms it can arrive in — `blake2b` over the file's bytes for an existing path, over the pixel
array otherwise. So renaming a file does not read as a different keyframe and editing one does.
No work needed here; the tests below pin the behaviour so it stays true.

## Verification

Unit tests, no generation:
- `--end-image` alone is refused, with the listed code
- a missing path is refused, naming it
- one image produces `("first",)`; two produce `("first", "last")`
- EXIF orientation is applied — a rotated fixture arrives upright
- checkpoint identity changes when the keyframe changes, and does not change when the file is
  merely renamed

One real run, 512×512, about 25 minutes:
- generate with a synthetic keyframe of recognisable structure
- compare frame 0 of the output against the input numerically (PSNR and correlation)
- **control:** the same prompt and seed with no keyframe. If its frame 0 scores as close to the
  image as the conditioned run's does, the conditioning is not working and we are measuring
  prompt agreement instead. The control is what makes the measurement mean anything.

## Out of scope

- Ref2VA (omni-reference: multiple images, video and audio references). Different partition, not
  converted, separate work.
- Turbo LoRA and TAE. Tracked separately; TAE runs in its own branch.
