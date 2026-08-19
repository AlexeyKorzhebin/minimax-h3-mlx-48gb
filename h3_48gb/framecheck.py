"""Detect VAE-decode corruption in a frame: zero-filled tiles and tile-seam blocking artifacts.

Боевые ворота 2026-08-19's root cause (chunk-recon investigation, `~/Research/TestVideo/chunk-
recon/`): `VideoVAE.decode` (`upstream/minimax_h3_mlx/video_vae.py`) chains 13 chunks x 15 tiles —
up to 195 ViT-decoder forwards — into a single lazy MLX graph and materializes it once, at the very
end, with one `np.array()` call. Under allocator/memory pressure, a fraction of that graph's
intermediate buffers could come back as an unwritten (zero) or garbled buffer instead of the tile
actually computed — non-deterministically, invisible to any shape/dtype check, because the corrupt
buffer is still a well-formed array. Two visible symptoms: a tile (or a whole frame) decodes to a
flat fill color, or a tile lands correctly but disagrees with its neighbour hard enough at the seam
to be visible as blocking.

`patches/0003-vae-decode-eval.patch` (`h3_48gb.pipeline.vae_decode_eval_patch_applied`) bounds the
graph at the source, which should make both symptoms unreachable. This module is defense in depth
on top of that, not a redundant check to delete once the patch lands — a decode is expensive
(minutes) and feeds a scene chain (`h3_48gb.assemble`) a silently-corrupt keyframe would poison, so
every frame that reaches a caller is verified rather than trusted.

Both thresholds and the seam-score formula come from the investigation's own scripts, merged: the
five-column seam list is `check_mp4.py`'s (the investigation's "working detector for both modes");
the fixed `2.5` threshold and the zero-fill fraction floor are `decode_after_unload.py`'s (the
investigation's final, most-refined pass, run against the real allocator state a production decode
sees) — see each constant's own docstring for its exact provenance.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: RGB a zero (unwritten) decode buffer resolves to once `PIXEL_MEAN`/`PIXEL_STD` unnormalize it
#: and the pipeline's `* 255.0 + 0.5` round-to-uint8 runs — measured directly off a corrupt clip's
#: flat-filled frames (chunk-recon investigation, `decode_after_unload.py`'s `FILL`), not derived
#: symbolically here, so this matches what was actually observed rather than what algebra predicts.
FILL_COLOR = (124, 116, 104)

#: How close an RGB pixel must be to `FILL_COLOR`, per channel, to count as "filled" — matches
#: `decode_after_unload.py`'s own `<= 2` (uint8 rounding slop, not a loose match).
FILL_TOLERANCE = 2

#: Fraction of a frame's pixels that must read as `FILL_COLOR`-filled before the frame counts as
#: zero-fill corrupted. `decode_after_unload.py`'s own "partial" floor (`0.005`, ~0.5% of an
#: 896x512 frame is ~2300 pixels — well under one VAE tile, which covers several percent) — low
#: enough to catch a single unwritten tile without needing the whole frame to be flat, but high
#: enough that no legitimate frame accidentally lands enough pixels on this one exact RGB triple.
ZERO_FILL_FRACTION_THRESHOLD = 0.005

#: Column x-positions where two decode tiles seam together horizontally, at the project's
#: `DEFAULT_SCENE_CANVAS` (896x512, `h3_48gb.assemble`) — `check_mp4.py`'s own five columns, the
#: investigation's most complete list of the VAE's tile boundaries at that canvas. A column outside
#: a given frame's width is skipped, not clamped (see `tile_seam_score`), so this degrades to
#: "no seam evidence at this column" rather than raising on a differently-sized frame.
TILE_SEAM_COLUMNS = (160, 320, 480, 640, 736)

#: Row y-positions where two decode tiles seam together vertically, same canvas and source.
TILE_SEAM_ROWS = (128, 256)

#: Above this, `tile_seam_score`'s ratio of seam-adjacent pixel deltas to a same-tile baseline a
#: few pixels over reads as a real discontinuity rather than picture detail. Chosen empirically by
#: the investigation (`decode_after_unload.py`): clean frames scored <= 1.86, corrupted
#: (tile-boundary garbage) frames scored >= 2.80 across its sample; `2.5` sits in the gap with
#: margin on both sides and produced 0 false positives across 365 clean frames.
TILE_SEAM_SCORE_THRESHOLD = 2.5


def zero_fill_fraction(frame: np.ndarray) -> float:
    """Fraction of `frame`'s (H, W, 3) pixels within `FILL_TOLERANCE` of `FILL_COLOR`, per channel."""
    diff = np.abs(frame.astype(np.int16) - np.array(FILL_COLOR, dtype=np.int16))
    return float((diff <= FILL_TOLERANCE).all(axis=-1).mean())


def tile_seam_score(frame: np.ndarray) -> float:
    """Energy at the VAE's own tile seams, relative to a same-tile baseline a few pixels away.

    ``1.0`` means the seam pixels differ exactly as much as any other pixels a few columns/rows
    over — a clean image, where nothing marks a tile boundary as special. Values well above 1.0
    mean the discontinuity is concentrated exactly at the seam, which only tile-level decode damage
    produces (legitimate picture content has no reason to break precisely on a VAE tile boundary).
    Ported from chunk-recon's `check_mp4.py` / `decode_after_unload.py` `seam_score`.

    Returns ``1.0`` (the "clean" value) if `frame` is too small for any configured seam to fall
    inside it — nothing to measure is not evidence of corruption.
    """
    g = frame.astype(np.float32).mean(axis=-1)
    h, w = g.shape
    seam, base = [], []
    for x in TILE_SEAM_COLUMNS:
        if x - 4 >= 0 and x < w:
            seam.append(np.abs(g[:, x] - g[:, x - 1]).mean())
            base.append(np.abs(g[:, x - 3] - g[:, x - 4]).mean())
    for y in TILE_SEAM_ROWS:
        if y - 4 >= 0 and y < h:
            seam.append(np.abs(g[y] - g[y - 1]).mean())
            base.append(np.abs(g[y - 3] - g[y - 4]).mean())
    if not seam:
        return 1.0
    baseline = float(np.mean(base)) or 1e-6
    return float(np.mean(seam)) / baseline


def is_frame_corrupt(frame: np.ndarray) -> bool:
    """Whether a single (H, W, 3) uint8 frame trips zero-fill or tile-seam detection.

    The check `h3_48gb.assemble`'s keyframe/freeze-frame extraction runs on one still image at a
    time (there is no clip of neighbouring frames to compare against there).
    """
    return (zero_fill_fraction(frame) > ZERO_FILL_FRACTION_THRESHOLD
            or tile_seam_score(frame) > TILE_SEAM_SCORE_THRESHOLD)


@dataclass(frozen=True)
class FrameCorruption:
    """One corrupt frame `find_corrupt_frames` found, and which detector(s) tripped."""

    index: int
    zero_fill: bool
    seam_score: float

    @property
    def seam(self) -> bool:
        return self.seam_score > TILE_SEAM_SCORE_THRESHOLD


def find_corrupt_frames(frames: np.ndarray) -> list[FrameCorruption]:
    """Every frame in `frames` (N, H, W, 3) uint8 that trips zero-fill or tile-seam detection.

    Used on a decoded clip's full frame stack (`h3_48gb.pipeline._validate_decoded_frames`), where
    there are many frames to report on at once.
    """
    bad = []
    for i in range(frames.shape[0]):
        frame = frames[i]
        zf = zero_fill_fraction(frame) > ZERO_FILL_FRACTION_THRESHOLD
        seam_score = tile_seam_score(frame)
        if zf or seam_score > TILE_SEAM_SCORE_THRESHOLD:
            bad.append(FrameCorruption(index=i, zero_fill=zf, seam_score=seam_score))
    return bad


class CorruptFramesError(RuntimeError):
    """Raised when one or more frames fail `is_frame_corrupt`/`find_corrupt_frames` validation."""
