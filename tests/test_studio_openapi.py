"""The master's request types are generated from `web/openapi.json`; it must be the app's own."""
from __future__ import annotations

import json
from pathlib import Path

from h3_48gb.studio.app import create_app

COMMITTED = Path(__file__).resolve().parent.parent / "h3_48gb" / "studio" / "web" / "openapi.json"


def test_committed_openapi_matches_the_app(tmp_path):
    assert json.loads(COMMITTED.read_text(encoding="utf-8")) == create_app(tmp_path).openapi(), (
        "regenerate: python -m h3_48gb.studio openapi --out h3_48gb/studio/web/openapi.json")
