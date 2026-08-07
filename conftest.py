"""Make the checkpoint-dependent skips loud.

Twelve tests — the whole of `tests/test_image_processor.py`, including the numerical equivalence
against real `transformers` that the committed fixtures exist for — are skipped when
`~/models/h3-converted` is absent. `pytest -q` reports that as twelve `s` characters among the
dots, which reads as "all green" on a machine that has never held the weights: every torch-free
guarantee in this fork would be unverified and nothing would say so.
"""
from pathlib import Path

CHECKPOINT = Path.home() / "models/h3-converted"


def pytest_terminal_summary(terminalreporter, exitstatus, config):
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
