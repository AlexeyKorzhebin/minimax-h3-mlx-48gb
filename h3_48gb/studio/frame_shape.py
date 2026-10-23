"""L7d (spec §10, revision 3.1): a picture is a keyframe of a project only if its aspect ratio is
the project's `frame_format` within 2 % — 1344×768 (1.6 %), 1920×1088, 1664×928 pass; a square,
4:3, 3:4 do not. One function for every place that decides it: the plan of a render (slice S3)
and, in slice S2b, O8's refusal `frame_shape_mismatch` and the image mode's «В сцену…» — so the
three never disagree (plan S2–S3 revision 4)."""
from __future__ import annotations

TOLERANCE = 0.02
RATIO = {"16:9": 16 / 9, "9:16": 9 / 16}
#: Names for the message «кадр 4:3, а проект 16:9»: the nearest of these.
NAMED = (("16:9", 16 / 9), ("9:16", 9 / 16), ("1:1", 1.0), ("4:3", 4 / 3), ("3:4", 3 / 4))


def deviation(width: int, height: int, frame_format: str) -> float:
    return abs((width / height) / RATIO[frame_format] - 1)


def matches(width: int, height: int, frame_format: str) -> bool:
    return deviation(width, height, frame_format) <= TOLERANCE


def shape_name(width: int, height: int) -> str:
    return min(NAMED, key=lambda named: abs((width / height) / named[1] - 1))[0]
