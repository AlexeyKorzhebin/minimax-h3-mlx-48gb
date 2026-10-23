"""Time estimates (spec §3.1 `estimate.py` → table `estimate_sample`): the median wall time of the
last ten runs with the same (executor, template, machine, width, height, frames, steps); until
there is history, the template card's `estimate_s` — measured on 3 s / 20 steps for H3 (card,
22.10), so a video estimate is scaled by steps / 20 and frames / 73 (GPU-DAY-20261021 п.1:
8 s / 50 steps took 693–722 s; the scaled card gives 592–658 s)."""
from __future__ import annotations

import sqlite3
import statistics

from h3_48gb.studio import templates

WINDOW = 10
BASE_STEPS = 20
BASE_FRAMES = 73
#: Measured price of loading a template's model into the running ComfyUI (GPU-DAY-20261020 п.1,
#: GPU-DAY-20261021 п.4, IMG-22): seconds added to the first task after the switch. The price
#: belongs to the TARGET template (spec §8.4, revision 3: no dependence on the model before was
#: seen). A card's `requires_json.limits.switch_in_s` wins — S2b's cards carry it; this table
#: covers the cards of 22.10, which do not (plan revision 4, spec §14 seam 1).
MEASURED_SWITCH_IN_S = {"krea2-t2i": 17.0, "h3-fl2va": 38.0, "h3-ref2va": 38.0}
#: fl2va <-> ref2va is a checkpoint swap inside H3 (GPU-DAY-20261020 п.1) — a pair, not a load.
H3_PAIR = frozenset({"h3-fl2va", "h3-ref2va"})
H3_CHECKPOINT_SWAP_S = 16.0
#: A template measured nowhere: the dearest known load, so a plan never under-promises — and
#: never a KeyError for a model this table has not heard of.
UNMEASURED_SWITCH_S = 38.0


def switch_in_s(template: str) -> float:
    try:
        limits = templates.load(template).requires.get("limits") or {}
    except templates.UnknownTemplate:
        limits = {}
    if limits.get("switch_in_s") is not None:
        return float(limits["switch_in_s"])
    return MEASURED_SWITCH_IN_S.get(template, UNMEASURED_SWITCH_S)


def switch_cost(before: str | None, after: str) -> float:
    if before is None or before == after:
        return 0.0
    if {before, after} == H3_PAIR:
        return H3_CHECKPOINT_SWAP_S
    return switch_in_s(after)


def estimate(conn: sqlite3.Connection, *, executor: str, template: str, machine: str,
             width: int, height: int, frames: int, steps: int) -> dict:
    rows = conn.execute(
        "SELECT wall_s FROM estimate_sample WHERE executor = ? AND template = ? AND machine = ? "
        "AND width = ? AND height = ? AND frames = ? AND steps = ? ORDER BY id DESC LIMIT ?",
        (executor, template, machine, width, height, frames, steps, WINDOW)).fetchall()
    if rows:
        return {"seconds": float(statistics.median(r["wall_s"] for r in rows)),
                "source": "history", "samples": len(rows)}
    card = templates.load(template)
    seconds = card.estimate_s
    if card.kind == "video":
        seconds = seconds * steps / BASE_STEPS * frames / BASE_FRAMES
    return {"seconds": round(seconds, 1), "source": "card", "samples": 0}


def record(conn: sqlite3.Connection, *, executor: str, template: str, machine: str, width: int,
           height: int, frames: int, steps: int, wall_s: float) -> None:
    """Called inside the transaction that finishes the task."""
    conn.execute("INSERT INTO estimate_sample (executor, template, machine, width, height, "
                 "frames, steps, wall_s) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (executor, template, machine, width, height, frames, steps, float(wall_s)))
