# Image-to-video (fl2va) in the CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `h3 generate` condition a clip on a starting frame, and optionally an ending frame, using the `fl2va` capability the converted checkpoint already has.

**Architecture:** Two optional path flags on `generate` and `resume`. `RunSpec` carries them, validates in `__post_init__` before any weight loads, and `run_generate` turns them into the `images` / `keyframe_anchors` pair the upstream pipeline already consumes. No pipeline changes — `_encode_keyframes` and the vision tower are already in place and already loaded on every run.

**Tech Stack:** Python 3.12, MLX 0.32, Pillow (already a dependency, used by the preview path), pytest.

## Global Constraints

- `upstream/` is never modified. All changes live in `h3_48gb/`.
- English throughout — code, comments, docstrings, user-facing strings. This ships publicly.
- Tests must never run a real generation: one native step costs 586 seconds. Use stubs; `tests/test_cli.py` already has the pattern.
- `import h3_48gb` must not load `mlx.core` — `tests/test_cli.py::test_import_h3_48gb_does_not_load_mlx_core` pins it. Keep heavy imports inside functions.
- Every failure carries a code from `ERROR_CODES`; `CliError.__init__` asserts membership, so no code may be coined inline.
- Only two conditioning modes are supported: one image → anchors `("first",)`, two images → `("first", "last")`. Anything else must be inexpressible, not merely rejected.
- Existing `ERROR_CODES` keys, for reference: `geometry_not_multiple_of_32`, `schedule_not_baked`, `checkpoint_not_found`, `checkpoint_mismatch`, `checkpoint_corrupt`, `preview_interval_negative`, `internal_error`.
- `RunSpec` field order today: `prompt, width, height, duration, steps, seed, checkpoint, outdir, tag`, then defaulted `checkpoint_dir=None`, `no_checkpoint=False`. New fields must be defaulted and appended, so positional construction in existing tests keeps working.

---

### Task 1: `RunSpec` carries and validates the keyframes

**Files:**
- Modify: `h3_48gb/cli.py` (`ERROR_CODES`, `RunSpec`, `build_parser`, `spec_from_args`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `CliError(code, message, detail=None)`, `RunSpec` as listed in Global Constraints.
- Produces: `RunSpec.image: Path | None`, `RunSpec.end_image: Path | None`, both defaulted to `None`; error codes `end_image_without_image` and `image_not_found`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli.py
import pytest

from h3_48gb.cli import CliError, RunSpec, build_parser, spec_from_args


def _spec(tmp_path, **overrides):
    base = dict(prompt="a cat", width=64, height=64, duration=1.0, steps=31, seed=0,
                checkpoint=tmp_path, outdir=tmp_path, tag="t")
    base.update(overrides)
    return RunSpec(**base)


def test_end_image_without_image_is_refused(tmp_path):
    last = tmp_path / "last.png"
    last.write_bytes(b"not really a png, never opened")
    with pytest.raises(CliError) as excinfo:
        _spec(tmp_path, end_image=last)
    assert excinfo.value.code == "end_image_without_image"


def test_a_missing_keyframe_is_refused_by_path(tmp_path):
    with pytest.raises(CliError) as excinfo:
        _spec(tmp_path, image=tmp_path / "absent.png")
    assert excinfo.value.code == "image_not_found"
    assert "absent.png" in excinfo.value.message


def test_both_keyframes_present_is_accepted(tmp_path):
    first, last = tmp_path / "a.png", tmp_path / "b.png"
    first.write_bytes(b"x")
    last.write_bytes(b"y")
    spec = _spec(tmp_path, image=first, end_image=last)
    assert (spec.image, spec.end_image) == (first, last)


def test_parser_accepts_the_two_flags(tmp_path):
    args = build_parser().parse_args(
        ["generate", "a cat", "--image", str(tmp_path / "a.png"),
         "--end-image", str(tmp_path / "b.png")]
    )
    assert args.image == tmp_path / "a.png"
    assert args.end_image == tmp_path / "b.png"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_cli.py -k "keyframe or end_image or two_flags" -v`
Expected: FAIL — `TypeError: RunSpec.__init__() got an unexpected keyword argument 'end_image'`

- [ ] **Step 3: Write the implementation**

Add to `ERROR_CODES`:

```python
    "end_image_without_image": "--end-image was given without --image; the end frame anchors a run that must also have a start frame",
    "image_not_found": "a keyframe path does not exist",
```

Append to `RunSpec` (after `no_checkpoint`):

```python
    #: Conditioning keyframes. One image anchors the clip's first frame; adding `end_image`
    #: makes it interpolate to a given last frame. The checkpoint was trained on exactly these
    #: two arrangements, so the flags deliberately cannot express a third.
    image: Path | None = None
    end_image: Path | None = None
```

Add to `RunSpec.__post_init__`, beside the geometry and schedule checks:

```python
        if self.end_image is not None and self.image is None:
            raise CliError("end_image_without_image",
                           "--end-image needs --image: the end frame is the far anchor of a "
                           "run whose near anchor is the start frame.")
        for label, path in (("--image", self.image), ("--end-image", self.end_image)):
            if path is not None and not Path(path).exists():
                raise CliError("image_not_found", f"{label} does not exist: {path}",
                               {"flag": label, "path": str(path)})
```

Add to both the `generate` and `resume` subparsers in `build_parser`:

```python
    gen.add_argument("--image", type=Path, default=None,
                     help="condition the first frame on this image")
    gen.add_argument("--end-image", type=Path, default=None,
                     help="also condition the last frame; requires --image")
```

Pass them through in `spec_from_args`:

```python
        image=args.image, end_image=args.end_image,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_cli.py -v`
Expected: all pass, including the pre-existing tests

- [ ] **Step 5: Commit**

```bash
git add h3_48gb/cli.py tests/test_cli.py
git commit -m "feat(cli): accept --image and --end-image, validated before any weight loads"
```

---

### Task 2: Turn the keyframes into anchors the pipeline understands

**Files:**
- Modify: `h3_48gb/cli.py` (`run_generate`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `RunSpec.image`, `RunSpec.end_image` from Task 1.
- Produces: `load_keyframes(spec: RunSpec) -> tuple[list, tuple[str, ...]]` returning the `images` list and the matching `keyframe_anchors`; `run_generate` passes both into the pipeline call.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
import numpy as np
from PIL import Image

from h3_48gb.cli import load_keyframes


def _png(path, size=(64, 64), colour=(200, 30, 30)):
    Image.new("RGB", size, colour).save(path)
    return path


def test_one_image_anchors_the_first_frame(tmp_path):
    images, anchors = load_keyframes(_spec(tmp_path, image=_png(tmp_path / "a.png")))
    assert anchors == ("first",)
    assert len(images) == 1


def test_two_images_anchor_both_ends(tmp_path):
    spec = _spec(tmp_path, image=_png(tmp_path / "a.png"),
                 end_image=_png(tmp_path / "b.png", colour=(30, 30, 200)))
    images, anchors = load_keyframes(spec)
    assert anchors == ("first", "last")
    assert len(images) == 2


def test_no_image_means_no_conditioning(tmp_path):
    assert load_keyframes(_spec(tmp_path)) == ([], ())


def test_exif_rotation_is_applied(tmp_path):
    """A phone photo carries its rotation in EXIF. Ignoring it conditions the run on a
    differently-oriented frame than the user saw, silently."""
    path = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[274] = 6  # Orientation: rotate 90 degrees clockwise
    Image.new("RGB", (64, 32), (10, 200, 10)).save(path, exif=exif)
    images, _ = load_keyframes(_spec(tmp_path, image=path))
    assert images[0].size == (32, 64), "EXIF orientation 6 rotates 90 degrees"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_cli.py -k keyframe -v`
Expected: FAIL — `ImportError: cannot import name 'load_keyframes'`

- [ ] **Step 3: Write the implementation**

```python
def load_keyframes(spec: RunSpec) -> tuple[list, tuple[str, ...]]:
    """Load the conditioning frames and the anchors that place them on the timeline.

    `exif_transpose` is not cosmetic: a camera stores orientation as a tag rather than by
    rotating the pixels, so without it a portrait photo conditions the run on a landscape
    frame — and nothing downstream can tell that happened.
    """
    from PIL import Image, ImageOps

    images, anchors = [], []
    for path, anchor in ((spec.image, "first"), (spec.end_image, "last")):
        if path is None:
            continue
        images.append(ImageOps.exif_transpose(Image.open(path).convert("RGB")))
        anchors.append(anchor)
    return images, tuple(anchors)
```

In `run_generate`, before the pipeline call:

```python
    images, keyframe_anchors = load_keyframes(spec)
```

and add to the `pipe(...)` call:

```python
        images=images or None, keyframe_anchors=keyframe_anchors,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/ test_preview.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add h3_48gb/cli.py tests/test_cli.py
git commit -m "feat(cli): load keyframes with EXIF orientation and anchor them"
```

---

### Task 3: Keyframes must change the checkpoint identity

**Files:**
- Modify: `h3_48gb/cli.py` (`_checkpoint_path_for`, if it does not already forward images)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `load_keyframes` from Task 2; `request_identity(arguments, extra=None, tag=None)` and `_image_digest` from `h3_48gb/checkpoint.py`.
- Produces: no new API; the guarantee that a conditioned run and an unconditioned one never share a checkpoint file.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
from h3_48gb.cli import _checkpoint_path_for


def test_a_keyframe_changes_the_checkpoint_identity(tmp_path):
    """Resuming a conditioned run from an unconditioned checkpoint would restart the clip
    from different latents than the ones it was written for."""
    plain = _spec(tmp_path)
    conditioned = _spec(tmp_path, image=_png(tmp_path / "a.png"))
    assert _checkpoint_path_for(plain) != _checkpoint_path_for(conditioned)


def test_different_keyframes_give_different_checkpoints(tmp_path):
    red = _spec(tmp_path, image=_png(tmp_path / "red.png", colour=(200, 30, 30)))
    blue = _spec(tmp_path, image=_png(tmp_path / "blue.png", colour=(30, 30, 200)))
    assert _checkpoint_path_for(red) != _checkpoint_path_for(blue)


def test_renaming_a_keyframe_keeps_the_same_checkpoint(tmp_path):
    """The digest is over content, not path — a renamed file is the same keyframe."""
    original = _png(tmp_path / "a.png")
    spec_a = _spec(tmp_path, image=original)
    renamed = tmp_path / "b.png"
    original.rename(renamed)
    spec_b = _spec(tmp_path, image=renamed)
    assert _checkpoint_path_for(spec_a) == _checkpoint_path_for(spec_b)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_cli.py -k checkpoint_identity -v`
Expected: FAIL — the first two assert paths differ but they are equal, because images never reach `request_identity`

- [ ] **Step 3: Write the implementation**

In `_checkpoint_path_for`, load the keyframes and include them in the arguments handed to `request_identity`, mirroring what `run_generate` passes to the pipeline:

```python
    images, keyframe_anchors = load_keyframes(spec)
    arguments = {
        ...  # existing fields unchanged
        "images": images or None,
        "keyframe_anchors": keyframe_anchors,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/ test_preview.py -q`
Expected: all pass, including `test_cli_and_checkpoint_module_agree_on_the_file_name`

- [ ] **Step 5: Commit**

```bash
git add h3_48gb/cli.py tests/test_cli.py
git commit -m "fix(cli): bind keyframes into the checkpoint identity"
```

---

### Task 4: Prove it on a real run, with a control

**Files:**
- Create: `scripts/verify_i2v.py`
- Modify: `docs/RESULTS.md`

**Interfaces:**
- Consumes: the CLI from Tasks 1-3.
- Produces: `scripts/verify_i2v.py`, runnable as `./.venv/bin/python scripts/verify_i2v.py`, writing its verdict to stdout and a JSON report beside the clips.

- [ ] **Step 1: Write the verification script**

```python
#!/usr/bin/env python3
"""Does a keyframe actually condition the clip, or is it silently ignored?

Runs the same prompt and seed twice at 512x512 — once with a keyframe, once without — and
compares each clip's first frame against the image. The control run is the point: if an
unconditioned clip scores as close to the image as the conditioned one, then what we measured
was agreement with the prompt, not conditioning.

About 50 minutes for the pair. Nothing here is imported by the package.
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

OUT = Path.home() / "models/video-out/i2v-check"
PROMPT = "a red vintage car parked on a wet street at night, neon reflections"


def make_keyframe(path: Path) -> None:
    """A frame with structure a generator would not produce by chance."""
    img = Image.new("RGB", (512, 512), (18, 18, 28))
    px = img.load()
    for y in range(512):
        for x in range(512):
            if (x // 64 + y // 64) % 2 == 0:
                px[x, y] = (200, 40, 40)
    img.save(path)


def run(tag: str, image: Path | None) -> Path:
    cmd = ["./.venv/bin/python", "-m", "h3_48gb", "generate", PROMPT,
           "--width", "512", "--height", "512", "--duration", "2.4",
           "--steps", "31", "--seed", "20260807", "--tag", tag, "--outdir", str(OUT)]
    if image is not None:
        cmd += ["--image", str(image)]
    subprocess.run(cmd, check=True)
    return OUT / f"h3-{tag}-512x512.mp4"


def first_frame(clip: Path) -> np.ndarray:
    out = clip.with_suffix(".frame0.png")
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(clip), "-vframes", "1",
                    "-y", str(out)], check=True)
    return np.asarray(Image.open(out).convert("RGB"), dtype=np.float64)


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(((a - b) ** 2).mean())
    return float("inf") if mse == 0 else 20 * np.log10(255.0) - 10 * np.log10(mse)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    key = OUT / "keyframe.png"
    make_keyframe(key)
    reference = np.asarray(Image.open(key).convert("RGB"), dtype=np.float64)

    conditioned = first_frame(run("i2v", key))
    control = first_frame(run("control", None))

    report = {
        "conditioned_psnr": round(psnr(reference, conditioned), 2),
        "control_psnr": round(psnr(reference, control), 2),
        "conditioned_corr": round(float(np.corrcoef(reference.ravel(), conditioned.ravel())[0, 1]), 4),
        "control_corr": round(float(np.corrcoef(reference.ravel(), control.ravel())[0, 1]), 4),
    }
    report["verdict"] = (
        "conditioning works"
        if report["conditioned_psnr"] > report["control_psnr"] + 3
        else "INCONCLUSIVE: the keyframe did not move the first frame measurably"
    )
    (OUT / "verdict.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0 if report["verdict"] == "conditioning works" else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Check the script without generating**

Run: `./.venv/bin/python -c "import ast; ast.parse(open('scripts/verify_i2v.py').read()); print('parses')"`
Expected: `parses`

- [ ] **Step 3: Run the pair**

Run: `./.venv/bin/python scripts/verify_i2v.py`
Expected: about 50 minutes, then a JSON report. A verdict of `conditioning works` requires the conditioned clip to beat the control by more than 3 dB PSNR.

- [ ] **Step 4: Record the measurement**

Add a short section to `docs/RESULTS.md` with the two PSNR figures, the two correlations, and the verdict — stating that the control run is what makes the comparison meaningful. If the verdict is INCONCLUSIVE, record that instead and do not claim the feature works.

- [ ] **Step 5: Commit**

```bash
git add scripts/verify_i2v.py docs/RESULTS.md
git commit -m "test: prove keyframe conditioning against an unconditioned control"
```

---

## Self-Review

**Spec coverage:** flags and their rejected alternative → Task 1; EXIF and anchors → Task 2; identity over image content, including the rename case → Task 3; the real run with its control → Task 4. The spec's note that `_image_digest` already hashes content is pinned by Task 3's third test rather than reimplemented.

**Placeholders:** none — every step carries real code and a real command with its expected output.

**Type consistency:** `RunSpec.image` / `RunSpec.end_image` are named identically in Tasks 1, 2 and 3. `load_keyframes` returns `(list, tuple[str, ...])` in Task 2 and is consumed with that shape in Task 3. The `_spec` and `_png` helpers are defined once in Task 1 and Task 2 respectively and reused by later tasks in the same file.
