"""Make the checkpoint-dependent skips loud.

Twelve tests — the whole of `tests/test_image_processor.py`, including the numerical equivalence
against real `transformers` that the committed fixtures exist for — are skipped when
`~/models/h3-converted` is absent. `pytest -q` reports that as twelve `s` characters among the
dots, which reads as "all green" on a machine that has never held the weights: every torch-free
guarantee in this fork would be unverified and nothing would say so.
"""
import importlib.util
from pathlib import Path

import pytest

CHECKPOINT = Path.home() / "models/h3-converted"

#: marker name -> (modules that must all be importable, the skip reason). The reason text is what
#: `pytest_terminal_summary` below counts, so it doubles as a stable tag.
_MODULE_MARKERS = {
    "mlx": (("mlx",), "mlx: needs the MLX stack, absent here"),
    "cv": (("cv2", "scipy"), "cv: needs opencv-python and scipy, absent here"),
}


def _missing(modules) -> bool:
    return any(importlib.util.find_spec(name) is None for name in modules)


def pytest_runtest_setup(item):
    for marker, (modules, reason) in _MODULE_MARKERS.items():
        if item.get_closest_marker(marker) is not None and _missing(modules):
            pytest.skip(reason)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    skipped_reports = terminalreporter.stats.get("skipped", [])
    for marker, (_modules, reason) in _MODULE_MARKERS.items():
        count = sum(1 for report in skipped_reports
                    if reason in str(getattr(report, "longrepr", "")))
        if count:
            terminalreporter.write_sep("=", f"{count} skipped: {reason}", yellow=True)
    if CHECKPOINT.exists():
        return
    skipped = len(terminalreporter.stats.get("skipped", []))
    if not skipped:
        return
    terminalreporter.write_sep("=", f"{skipped} tests skipped: no converted checkpoint",
                               yellow=True, bold=True)
    terminalreporter.write_line(
        f"  {CHECKPOINT} is absent, so the tests that read real weights did not run — including "
        "the processor's equivalence with transformers. A green run here is not a full run.")
