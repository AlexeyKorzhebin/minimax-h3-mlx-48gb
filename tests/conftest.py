"""Project-wide test fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _frame_is_corrupt_default(monkeypatch):
    """P0 fix (боевые ворота 2026-08-19): `h3_48gb.assemble._extract_keyframe` and `_extract_valid_
    last_frame` both call `assemble._frame_is_corrupt`, which shells out to real `ffmpeg` to decode
    actual pixel bytes off disk (`_read_frame_rgb`). None of the scripted `run`/`spawn` fakes across
    `test_assemble.py`, `test_worker.py` and `test_web_projects.py` touch the filesystem -- they
    answer out of a script -- so there is no real image for that decode to read. Left unpatched,
    every test that reaches either function through a fake would fail on a missing file, regardless
    of what it is actually testing.

    Autouse and project-wide (rather than duplicated per test module) because `_extract_keyframe`
    is reachable from `h3_48gb.worker`'s post-job hook (`test_worker.py`) and the project web layer
    (`test_web_projects.py`), not just `h3_48gb.assemble`'s own tests. Defaulting to "never corrupt"
    keeps every test that predates this fix exercising exactly what it always did. Tests that
    actually want to exercise real seam-check behaviour override this within their own body via a
    fresh `monkeypatch.setattr(assemble, "_frame_is_corrupt", ...)` call -- a later `setattr`
    through the same `monkeypatch` fixture always wins over this one's earlier call, whether the
    override is a scripted fake or (`test_assemble.py`'s own `_REAL_FRAME_IS_CORRUPT`) the genuine
    implementation restored for a real-`ffmpeg` end-to-end test.
    """
    from h3_48gb import assemble

    monkeypatch.setattr(assemble, "_frame_is_corrupt", lambda *a, **k: False)
