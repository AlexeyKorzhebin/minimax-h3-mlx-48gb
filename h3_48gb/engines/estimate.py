"""Scene wall-time estimates on sglang (spec §3.3.15): the median `wall_s` of the last ten scenes
of the same canvas and frame count; until there is history, the h3-bench table (medians of
`results-*.jsonl` on alex-neuro, 2026-10-07, completed runs only), marked `source: "table"`."""
from __future__ import annotations

import json
import statistics
from pathlib import Path

HISTORY_NAME = "sglang-history.jsonl"
HISTORY_WINDOW = 10
#: the step count the bench table and every history row written before `steps` was recorded ran at
DEFAULT_STEPS = 50

#: short edge -> {frames: median wall_s}. Counts behind each median: 512/124 ×21, 512/175 ×4,
#: 512/192 ×4, 512/243 ×6, 512/277 ×1, 768/124 ×1, 768/192 ×2, 768/209 ×10. The 512/175 and
#: 512/192 rows are the beach ref2va runs (references scaled to 2048 px) -- kept on purpose: on
#: this server every scene is ref2va now (spec §4.1.3), so they are the closest to what runs.
#: 512/90 is 896x512 at 50 steps from two scenes of the 2026-10-08 acceptance run (1022.8 s,
#: 1021.9 s). sglang gives no step progress (video_api.py: progress 0, then 100), so the panel's
#: share is time over this estimate -- a 3.75 s scene must not be estimated as the 124-frame 345 s.
FALLBACK_SECONDS = {512: {90: 1022.0, 124: 345.0, 175: 2810.0, 192: 2980.0, 243: 580.0,
                          277: 720.0},
                    768: {124: 1140.0, 192: 1793.0, 209: 2820.0}}


def record(outdir, *, width: int, height: int, frames: int, wall_s: float,
           steps: int = DEFAULT_STEPS) -> None:
    path = Path(outdir) / HISTORY_NAME
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"width": width, "height": height, "frames": frames,
                                 "steps": steps, "wall_s": float(wall_s)}) + "\n")


def _history(outdir) -> list[dict]:
    path = Path(outdir) / HISTORY_NAME
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("wall_s"), (int, float)):
            rows.append(row)
    return rows


def estimate_seconds(outdir, *, width: int, height: int, frames: int,
                     steps: int = DEFAULT_STEPS) -> dict:
    """History is keyed by canvas, frames *and steps* (a row without `steps` ran at 50); the
    table fallback is a 50-step measurement and is scaled by `steps / 50`."""
    same = [row["wall_s"] for row in _history(outdir)
            if (row.get("width"), row.get("height"), row.get("frames"),
                row.get("steps", DEFAULT_STEPS)) == (width, height, frames, steps)]
    same = same[-HISTORY_WINDOW:]
    if same:
        return {"seconds": float(statistics.median(same)), "source": "history", "samples": len(same)}
    short = min(width, height)
    edge = short if short in FALLBACK_SECONDS else min(FALLBACK_SECONDS, key=lambda e: abs(e - short))
    table = FALLBACK_SECONDS[edge]
    nearest = min(table, key=lambda n: (abs(n - frames), n))
    return {"seconds": table[nearest] * steps / DEFAULT_STEPS, "source": "table", "samples": 0}
