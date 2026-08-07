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
    """The whole point: the composite AutoProcessor pulls torchvision, this must not.

    Checks both construction and processing paths, since torch is most likely to sneak in
    during image processing, not during initialization.
    """
    ip = load_image_processor(PROCESSOR_DIR)
    assert ip.merge_size == 2
    assert ip.patch_size == 16

    # Process an image to test the full path, not just construction
    ip(images=[Image.new("RGB", (640, 384), (200, 40, 40))], return_tensors="np")

    # Most critically: torch never entered during construction or processing
    assert "torch" not in sys.modules
    assert "torchvision" not in sys.modules


def test_torch_free_assertion_has_teeth():
    """Demonstrate that the torch-free check would catch a regression.

    If a future change accidentally imports torch during processing, this proves
    the assertion would catch it. We simulate the regression by manually injecting
    a sentinel into sys.modules, then confirm the assertion fires.
    """
    ip = load_image_processor(PROCESSOR_DIR)
    ip(images=[Image.new("RGB", (640, 384), (200, 40, 40))], return_tensors="np")

    # Simulate a regression: torch gets imported (e.g., in a future change to the processor)
    sys.modules["torch"] = object()  # Sentinel
    try:
        with pytest.raises(AssertionError):
            assert "torch" not in sys.modules
    finally:
        # Cleanup to avoid affecting other tests
        del sys.modules["torch"]


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
