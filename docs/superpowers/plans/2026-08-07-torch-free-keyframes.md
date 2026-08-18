# Torch-free Keyframe Preprocessing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `--image` actually reach the model, by replacing the composite `AutoProcessor` — which drags in `torch` via its unused video half — with the torch-free `Qwen3VLImageProcessor` already installed as part of `mlx_vlm`.

**Architecture:** A small `h3_48gb/image_processor.py` builds the mlx-vlm processor from the checkpoint's own `preprocessor_config.json` and wraps it in an object exposing the single `image_processor` attribute upstream consumes. `QuantizedTextEncoder` overrides the `processor` property to return it, so `AutoProcessor` is never constructed. Equivalence with `transformers` is pinned by fixtures generated once in a throwaway venv and committed, so the check survives without torch.

**Tech Stack:** Python 3.12, mlx-vlm 0.6.10 (already a dependency), Pillow, numpy, pytest.

## Global Constraints

- `upstream/` is never modified. All behaviour changes live in `h3_48gb/`.
- English throughout — code, comments, docstrings, user-facing strings. This ships publicly.
- `torch` and `torchvision` must never appear in `requirements.txt`, `pyproject.toml`, or the project venv. They are used once, in a throwaway venv outside the repository, to generate fixtures.
- `import h3_48gb` must not load `mlx.core`; `tests/test_cli.py::test_import_h3_48gb_does_not_load_mlx_core` pins it. Keep heavy imports inside functions.
- Tests must never run a real generation: one native step costs 586 seconds. Task 4 is the sole exception and is explicitly a measurement task.
- Fixture tolerance: `pixel_values` within `1e-5`, `image_grid_thw` exact. If the comparison fails, the finding is the deliverable — do not widen the tolerance.
- Established by experiment, use verbatim: `Qwen3VLImageProcessor` lives at `mlx_vlm.models.qwen3_vl.processing_qwen3_vl`; constructed from our config it reports `merge_size=2`, `patch_size=16`, and returns `pixel_values` float32 plus `image_grid_thw` int64.
- Upstream consumes exactly two things: `self.processor.image_processor(images=…, return_tensors="np")` (`upstream/minimax_h3_mlx/text_encoder.py:193`) and `self.processor.image_processor.merge_size` (line 196).

---

### Task 1: Build the torch-free processor

**Files:**
- Create: `h3_48gb/image_processor.py`
- Create: `tests/test_image_processor.py`

**Interfaces:**
- Consumes: `~/models/h3-converted/processor/preprocessor_config.json`.
- Produces: `load_image_processor(processor_dir: Path) -> Qwen3VLImageProcessor` and `TorchFreeProcessor(processor_dir: Path)` with an `.image_processor` attribute. Task 2 wires these into the encoder; Task 3 compares their output against fixtures.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_image_processor.py
import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from h3_48gb.image_processor import TorchFreeProcessor, load_image_processor

PROCESSOR_DIR = Path.home() / "models/h3-converted/processor"
pytestmark = pytest.mark.skipif(not PROCESSOR_DIR.exists(),
                                reason="converted checkpoint not present")


def test_processor_loads_without_torch():
    """The whole point: the composite AutoProcessor pulls torchvision, this must not."""
    ip = load_image_processor(PROCESSOR_DIR)
    assert ip.merge_size == 2
    assert ip.patch_size == 16
    assert "torch" not in sys.modules
    assert "torchvision" not in sys.modules


def test_it_produces_what_upstream_consumes():
    """upstream reads exactly `pixel_values` and `image_grid_thw`, nothing else."""
    ip = load_image_processor(PROCESSOR_DIR)
    out = ip(images=[Image.new("RGB", (640, 384), (200, 40, 40))], return_tensors="np")
    assert out["pixel_values"].ndim == 2
    assert out["pixel_values"].dtype == np.float32
    assert out["image_grid_thw"].shape == (1, 3)


def test_wrapper_exposes_the_single_attribute_upstream_uses():
    proc = TorchFreeProcessor(PROCESSOR_DIR)
    assert proc.image_processor.merge_size == 2


def test_a_broken_config_fails_loudly(tmp_path):
    """A silent fallback to AutoProcessor would reintroduce torch 28 GB into a run."""
    (tmp_path / "preprocessor_config.json").write_text("{ not json")
    with pytest.raises(Exception) as excinfo:
        load_image_processor(tmp_path)
    assert "preprocessor_config.json" in str(excinfo.value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_image_processor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'h3_48gb.image_processor'`

- [ ] **Step 3: Write the implementation**

```python
# h3_48gb/image_processor.py
"""Keyframe preprocessing without torch.

`upstream`'s text encoder resolves its image processor through
`transformers.AutoProcessor.from_pretrained`, which constructs the *composite* Qwen3VL processor —
image, video and tokenizer together. The video half imports torchvision, which imports torch, and
this fork installs neither. The failure lands after the 28.22 GB text encoder has loaded.

Only two things are ever read off that processor (`upstream/minimax_h3_mlx/text_encoder.py:193,196`):
the image processor's `__call__` and its `merge_size`. So the composite is unnecessary, and
`mlx_vlm` already ships a torch-free `Qwen3VLImageProcessor` that satisfies both.
"""
from __future__ import annotations

import json
from pathlib import Path

#: Keys describing the composite processor, not the image processor's own parameters.
_COMPOSITE_KEYS = ("processor_class", "image_processor_type", "auto_map")


def load_image_processor(processor_dir: str | Path):
    """Build the image processor from the checkpoint's own preprocessor config."""
    from mlx_vlm.models.qwen3_vl.processing_qwen3_vl import Qwen3VLImageProcessor

    config_path = Path(processor_dir) / "preprocessor_config.json"
    try:
        config = json.loads(config_path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"could not read {config_path}: {exc}") from exc

    kwargs = {k: v for k, v in config.items() if k not in _COMPOSITE_KEYS}
    return Qwen3VLImageProcessor(**kwargs)


class TorchFreeProcessor:
    """Stands in for the composite processor, exposing only what upstream reads.

    Deliberately not a subclass of anything: the surface is two call sites, and a wider stand-in
    would invite code to depend on parts that are not actually torch-free.
    """

    def __init__(self, processor_dir: str | Path):
        self.image_processor = load_image_processor(processor_dir)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_image_processor.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add h3_48gb/image_processor.py tests/test_image_processor.py
git commit -m "feat: build keyframe preprocessing without torch"
```

---

### Task 2: Wire it into the encoder

**Files:**
- Modify: `h3_48gb/text_encoder.py` (`QuantizedTextEncoder`)
- Test: `tests/test_image_processor.py`

**Interfaces:**
- Consumes: `TorchFreeProcessor(processor_dir)` from Task 1.
- Produces: `QuantizedTextEncoder.processor` returning a `TorchFreeProcessor`, so upstream's `encode()` never reaches `AutoProcessor`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_image_processor.py
from h3_48gb.image_processor import TorchFreeProcessor


def test_encoder_serves_the_torch_free_processor(monkeypatch):
    """upstream's property would build AutoProcessor; ours must shadow it entirely."""
    import h3_48gb.text_encoder as te

    built = {}

    class _Spy(TorchFreeProcessor):
        def __init__(self, processor_dir):
            built["dir"] = Path(processor_dir)
            super().__init__(processor_dir)

    monkeypatch.setattr(te, "TorchFreeProcessor", _Spy)

    encoder = te.QuantizedTextEncoder.__new__(te.QuantizedTextEncoder)
    encoder._model_dir = Path.home() / "models/h3-converted/text_encoder"
    encoder._processor = None

    proc = encoder.processor
    assert proc.image_processor.merge_size == 2
    assert built["dir"].name == "processor", "must read the sibling processor directory"
    assert "torch" not in sys.modules
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_image_processor.py::test_encoder_serves_the_torch_free_processor -v`
Expected: FAIL — `AttributeError: module 'h3_48gb.text_encoder' has no attribute 'TorchFreeProcessor'`

- [ ] **Step 3: Write the implementation**

Add the import at the top of `h3_48gb/text_encoder.py` (it is light — stdlib plus a deferred mlx-vlm import inside the function):

```python
from .image_processor import TorchFreeProcessor
```

Add to `QuantizedTextEncoder`:

```python
    @property
    def processor(self):
        """Serve a torch-free processor instead of upstream's `AutoProcessor`.

        Upstream builds the composite Qwen3VL processor, whose video half needs torchvision. It is
        never used — `encode()` reads only `image_processor` — so constructing it costs a hard
        dependency for nothing, and the failure surfaces only once an image is passed, after the
        encoder has loaded.
        """
        if self._processor is None:
            self._processor = TorchFreeProcessor(self._model_dir.parent / "processor")
        return self._processor
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest -q`
Expected: all pass, including `test_import_h3_48gb_does_not_load_mlx_core`

- [ ] **Step 5: Commit**

```bash
git add h3_48gb/text_encoder.py tests/test_image_processor.py
git commit -m "feat: serve keyframes through the torch-free processor"
```

---

### Task 3: Pin equivalence with transformers, permanently

**Files:**
- Create: `scripts/generate_processor_fixtures.py`
- Create: `tests/fixtures/processor/` (four `.npz` files plus `manifest.json`)
- Test: `tests/test_image_processor.py`

**Interfaces:**
- Consumes: `load_image_processor` from Task 1.
- Produces: committed fixtures and a permanent comparison test that needs no torch.

- [ ] **Step 1: Write the fixture generator**

```python
#!/usr/bin/env python3
"""Generate reference outputs from the real transformers processor, once.

Run this in a THROWAWAY venv outside the repository — torch must never enter the project
environment. The fixtures it writes are committed, so the equivalence keeps being checked on every
test run afterwards without torch:

    python3 -m venv /tmp/h3-fixture-venv
    /tmp/h3-fixture-venv/bin/pip install -q torch transformers pillow numpy
    /tmp/h3-fixture-venv/bin/python scripts/generate_processor_fixtures.py
    rm -rf /tmp/h3-fixture-venv
"""
import json
from pathlib import Path

import numpy as np
from PIL import Image
from transformers import AutoProcessor

PROCESSOR_DIR = Path.home() / "models/h3-converted/processor"
OUT = Path(__file__).resolve().parent.parent / "tests/fixtures/processor"

#: Sizes chosen for what varies in preprocessing, not for variety's sake: a landscape and a
#: portrait (resize rounding differs), one below `min_pixels` and one above `max_pixels` (both
#: bounds), and one not a multiple of patch_size*merge_size (the padding path).
CASES = {
    "landscape": (640, 384),
    "portrait": (384, 640),
    "tiny": (64, 48),
    "huge": (2048, 1536),
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    processor = AutoProcessor.from_pretrained(str(PROCESSOR_DIR))
    manifest = {}
    for name, (w, h) in CASES.items():
        # A deterministic gradient — content must be reproducible from the manifest alone.
        arr = np.zeros((h, w, 3), dtype=np.uint8)
        arr[..., 0] = np.linspace(0, 255, w, dtype=np.uint8)[None, :]
        arr[..., 1] = np.linspace(0, 255, h, dtype=np.uint8)[:, None]
        image = Image.fromarray(arr)

        out = processor.image_processor(images=[image], return_tensors="np")
        np.savez_compressed(OUT / f"{name}.npz",
                            pixel_values=out["pixel_values"],
                            image_grid_thw=out["image_grid_thw"])
        manifest[name] = {"size": [w, h],
                          "pixel_values_shape": list(out["pixel_values"].shape),
                          "image_grid_thw": out["image_grid_thw"].tolist()}
        print(f"{name}: {out['pixel_values'].shape}")

    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Generate the fixtures in a throwaway venv**

```bash
python3 -m venv /tmp/h3-fixture-venv
/tmp/h3-fixture-venv/bin/pip install -q torch transformers pillow numpy
/tmp/h3-fixture-venv/bin/python scripts/generate_processor_fixtures.py
rm -rf /tmp/h3-fixture-venv
```
Expected: four shapes printed, four `.npz` files plus `manifest.json` in `tests/fixtures/processor/`.
Then confirm the project venv is untouched: `./.venv/bin/python -c "import importlib.util; print(importlib.util.find_spec('torch'))"` must print `None`.

- [ ] **Step 3: Write the comparison test**

```python
# tests/test_image_processor.py
FIXTURES = Path(__file__).parent / "fixtures/processor"


def _case_image(w: int, h: int) -> Image.Image:
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[..., 0] = np.linspace(0, 255, w, dtype=np.uint8)[None, :]
    arr[..., 1] = np.linspace(0, 255, h, dtype=np.uint8)[:, None]
    return Image.fromarray(arr)


@pytest.mark.parametrize("name", ["landscape", "portrait", "tiny", "huge"])
def test_matches_transformers_reference(name):
    """Two independent implementations can disagree in resize rounding or normalisation without
    raising — the clip is then conditioned on a subtly different image, and the only symptom is
    worse output hours later. These fixtures came from transformers itself."""
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    w, h = manifest[name]["size"]
    reference = np.load(FIXTURES / f"{name}.npz")

    out = load_image_processor(PROCESSOR_DIR)(images=[_case_image(w, h)], return_tensors="np")

    np.testing.assert_array_equal(out["image_grid_thw"], reference["image_grid_thw"])
    np.testing.assert_allclose(out["pixel_values"], reference["pixel_values"], atol=1e-5)
```

- [ ] **Step 4: Run the comparison**

Run: `./.venv/bin/python -m pytest tests/test_image_processor.py -v`
Expected: 4 parametrised cases pass. If any fails, stop — record the divergence with its magnitude in the report. Do not widen `atol`.

- [ ] **Step 5: Commit**

```bash
git add scripts/generate_processor_fixtures.py tests/fixtures/processor tests/test_image_processor.py
git commit -m "test: pin processor equivalence with transformers via committed fixtures"
```

---

### Task 4: The end-to-end run the previous plan could not finish

**Files:**
- Modify: `docs/RESULTS.md`

**Interfaces:**
- Consumes: everything above, plus `scripts/verify_i2v.py`, which already exists from the previous plan and needs no changes.
- Produces: the four measured numbers and a verdict.

- [ ] **Step 1: Confirm a keyframe now reaches the model**

Run a single conditioned generation and watch that it gets past text encoding:
```bash
./.venv/bin/python -m h3_48gb generate "a red vintage car on a wet street at night" \
  --width 512 --height 512 --duration 2.4 --steps 31 --seed 20260807 \
  --tag i2v-smoke --image tests/fixtures/processor/../../../docs/media/native5-frame.jpg \
  --outdir ~/models/video-out/i2v-check
```
Expected: the run proceeds past `unloaded text encoder` into `step 1/30`. If it raises, stop and report — the rest of this task is meaningless until it does.

- [ ] **Step 2: Run the conditioned/control pair**

Run: `./.venv/bin/python scripts/verify_i2v.py`
Expected: about 50 minutes, then a JSON verdict. `conditioning works` requires the conditioned clip to beat the control by more than 3 dB PSNR.

- [ ] **Step 3: Record the outcome honestly**

Add to `docs/RESULTS.md`: the four numbers (conditioned PSNR, control PSNR, both correlations), the verdict, and one sentence on why the control run is what makes the comparison meaningful. If the verdict is INCONCLUSIVE, record that — a negative result is a real finding about this fork and must not be tuned away.

- [ ] **Step 4: Commit**

```bash
git add docs/RESULTS.md
git commit -m "docs: record measured keyframe conditioning against its control"
```

---

## Self-Review

**Spec coverage:** the torch-free processor and its loud failure → Task 1; the encoder override → Task 2; numerical equivalence with transformers via throwaway-venv fixtures → Task 3; the end-to-end proof with control → Task 4. The spec's constraint that torch never enters the project environment is enforced by Task 3's Step 2 verification.

**Placeholders:** none — every step carries real code or a real command with its expected output.

**Type consistency:** `load_image_processor(processor_dir)` and `TorchFreeProcessor(processor_dir)` keep the same names and signatures across Tasks 1, 2 and 3. `PROCESSOR_DIR` and the four case names are defined once and reused.
