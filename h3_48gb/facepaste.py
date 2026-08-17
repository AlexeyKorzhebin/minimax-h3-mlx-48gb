"""Crop a window around a tracked face and paste a refined result back -- the CPU-only geometry
half of the face-refine pipeline, sitting between `facetrack.FaceTrack` (Task 1) and the GPU v2v
engine that actually improves the face (Task 3, a later module).

**Why the crop rectangle needs its own record, separate from `FaceTrack.box`.** `FaceTrack.box`
is a *smooth float function of frame index* by design (see `facetrack.py`'s module docstring) --
that smoothness is the whole point of the track, and this module must not break it by rounding
inconsistently from one caller to the next. But the pixels `crop_window` actually reads are never
that exact float rectangle: they are `scale` times it, shifted and possibly shrunk to fit inside
the frame (`_clip_to_frame`), and then rounded to integers to become real array slice indices
(`_round_rect`). `CropGeometry` is the record of that *real* rectangle, per frame -- the one
`paste_back` must use to land refined pixels back where they were actually cut from, not the
geometric ideal `FaceTrack.box` still reports for the same frame.

**Why the rounding is a function, not "just round it".** A crop rectangle whose width silently
drifts by a pixel between two adjacent frames (e.g. `round(x0)` and `round(x0+w)` computed from
two *different* roundings of `w`) would show up as visible flicker in the pasted mask edge, on a
track whose underlying float geometry is otherwise perfectly smooth. `_round_rect` rounds the two
*edges* (`x0` and `x0 + w`) independently and takes their difference for the integer width --
edges of a smoothly moving rectangle change by at most one integer step per frame this way, and
(see `_round_rect`'s own docstring) can never cross a bound a float-clipped rectangle already
respects.

**Why the paste is masked and fades, not a flat rectangle swap.** A hard-edged rectangle paste
would show a visible seam at the crop boundary the instant the refined pixels differ from the
source even slightly -- the entire reason ComfyUI-style face-restoration nodes paste through a
feathered mask instead of a bare crop (see the design spec's ComfyUI-FaceRefine reference).
`_feather_mask` softens the outer `feather` fraction of each side into a linear ramp; `_fade_multipliers`
additionally collapses that mask toward zero, over `FADE_FRAMES` frames, on any stretch where
`FaceTrack.detected` says there is no real evidence nearby -- an extrapolated box far from the
last real detection is a guess, and guessing at where to paste face-refined pixels is worse than
leaving the original frame alone.

Deliberately no dependency on `mlx`, same reasoning as `facetrack.py` and `queue.py`: this is a
CPU-only geometry/blending step, and importing it must never pull in the 33B-parameter transformer
stack. See `test_facepaste_module_does_not_import_mlx`.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

#: How many frames `paste_back`'s fade_out ramps the blend down over, once a frame leaves the
#: neighborhood of a real detection (`FaceTrack.detected` false). Fixed, not a parameter: the
#: design spec's parameter table gives this as a measured constant ("линейный fade_out вклейки за
#: 6 кадров"), the same status as `facetrack`'s `_SCORE_THRESHOLD` -- a number from experiments,
#: not something a caller is expected to retune per clip.
FADE_FRAMES = 6


@dataclass(frozen=True)
class CropGeometry:
    """The pixel-exact crop rectangle `crop_window` actually used on every frame -- see the
    module docstring's "why the crop rectangle needs its own record" for why this is not simply
    recomputed from `FaceTrack.box` at paste time.

    `rects` is `(n_frames, 4)` int32, one `(x, y, w, h)` row per frame index -- top-left corner
    and extent, in the source frame's own pixel coordinates, the same convention `FaceTrack.box`
    uses. `out_size` is `(width, height)` the crop was resized to (`crop_window`'s own `out_size`
    argument, echoed back so `paste_back` knows what shape `refined` frames must be without
    threading a second parameter through every caller).
    """

    rects: np.ndarray
    out_size: tuple[int, int]

    @property
    def n_frames(self) -> int:
        return int(self.rects.shape[0])

    def _check_index(self, frame_idx: int) -> None:
        if not 0 <= frame_idx < self.n_frames:
            raise IndexError(f"frame_idx {frame_idx} out of range [0, {self.n_frames})")

    def rect(self, frame_idx: int) -> tuple[int, int, int, int]:
        """`(x, y, w, h)`, the real integer pixel rectangle `crop_window` read frame `frame_idx`
        from -- always inside the source frame's bounds (see `_clip_to_frame`).
        """
        self._check_index(frame_idx)
        x, y, w, h = self.rects[frame_idx]
        return (int(x), int(y), int(w), int(h))


def _clip_to_frame(cx: float, cy: float, w: float, h: float,
                    frame_w: int, frame_h: int) -> tuple[float, float, float, float]:
    """The `w x h` rectangle centered on `(cx, cy)`, shrunk to fit inside a `frame_w x frame_h`
    frame if it is larger than the frame in either dimension, then shifted (never shrunk further)
    so both edges land inside `[0, frame_w]` / `[0, frame_h]`.

    Shrinking happens before shifting on purpose: a `scale`-times-the-face-box rectangle can
    legitimately be bigger than the whole frame (a face that nearly fills the shot, scaled up by
    2.75x), and there is no shift that fits an oversized rectangle inside a frame it cannot fit
    in -- the only honest answer there is the whole frame. A rectangle that already fits just
    gets pushed back inside the bounds, keeping its own size, which is the "клип у краёв кадра"
    (clip at the frame edges) the brief asks for: the crop follows the face right up to the edge
    of frame instead of the box wandering off it.
    """
    w = min(w, float(frame_w))
    h = min(h, float(frame_h))
    x0 = cx - w / 2.0
    y0 = cy - h / 2.0
    x0 = min(max(x0, 0.0), frame_w - w)
    y0 = min(max(y0, 0.0), frame_h - h)
    return x0, y0, w, h


def _round_rect(x0: float, y0: float, w: float, h: float) -> tuple[int, int, int, int]:
    """`(x0, y0, w, h)` rounded to integer pixel indices, deterministically: `round()` (Python's
    own round-half-to-even) applied to each of the four *edges* -- `x0`, `y0`, `x0 + w`, `y0 + h`
    -- independently, with width/height recovered as the difference between the rounded edges.

    Rounding the edges instead of rounding `w`/`h` directly and adding them to a separately
    rounded `x0` matters for two reasons, both explained in the module docstring's "why the
    rounding is a function" note: it keeps a smoothly moving rectangle's *width* from jittering by
    a pixel frame-to-frame independent of its position, and -- the property this module's own
    tests rely on -- if `(x0, y0, w, h)` is already float-clipped to fit inside a `frame_w x
    frame_h` frame (`_clip_to_frame`'s job), rounding the edges this way can never push the result
    outside those integer bounds: `round(t) <= frame_w` whenever `t <= frame_w` and `frame_w` is
    itself an integer, because `round(t) < t + 0.5 <= frame_w + 0.5` and the left side is an
    integer strictly below `frame_w + 0.5`. `crop_window`/`paste_back` therefore never need a
    second clamp after this call.

    `round()`'s ties-to-even policy (not "always round half up") is deliberate too: a track's box
    drifts continuously across many frames and will cross a `.5` boundary many times over a long
    clip, and "always round up" would accumulate a directional bias in the crop's average position
    that ties-to-even does not.
    """
    ix0 = round(x0)
    iy0 = round(y0)
    ix1 = round(x0 + w)
    iy1 = round(y0 + h)
    return ix0, iy0, ix1 - ix0, iy1 - iy0


def crop_window(frames, track, scale: float = 2.75,
                 out_size: tuple[int, int] = (448, 288)) -> tuple[np.ndarray, CropGeometry]:
    """Crop a `scale`-times-the-face-box window around `track`'s smoothed box on every frame of
    `frames`, Lanczos-resize each crop to `out_size`, and return the stacked crops alongside the
    `CropGeometry` `paste_back` will need to undo this.

    `frames` is consumed one frame at a time via plain iteration -- a list, a NumPy `(n, H, W, 3)`
    array, or any other single-pass iterable -- since nothing here needs random access into it:
    `track.box(i)` already carries everything about frame `i`'s geometry, computed once up front
    in Task 1. `out_size` is `(width, height)`, matching this fork's own `WxH` convention (e.g.
    `h3-<tag>-448x288.mp4`) and `cv2.resize`'s own `dsize` argument, which it is passed to
    directly.

    The crop window is `scale * (w, h)` centered on `track.box(i)`'s own center -- not `scale *
    out_size` or any other independent size -- because the window must track the face's actual
    size in the source frame; `out_size` only decides what resolution the refine model sees, a
    completely separate concern from how much of the source frame is included.

    Raises `ValueError` if `frames` yields a different number of frames than `track.n_frames`:
    `track.box`/`track.detected` are only meaningful over `[0, track.n_frames)`, and a caller that
    hands this function frames from a different pass over the source video (a re-decode with a
    dropped frame, say) needs to hear about the mismatch here, not get a silently truncated or
    out-of-range result.
    """
    out_w, out_h = out_size
    n_frames = track.n_frames
    rects = np.empty((n_frames, 4), dtype=np.int32)
    crops = np.empty((n_frames, out_h, out_w, 3), dtype=np.uint8)

    consumed = 0
    for frame_idx, frame in enumerate(frames):
        if frame_idx >= n_frames:
            raise ValueError(
                f"frames has more frames than track.n_frames={n_frames}"
            )
        frame_h, frame_w = frame.shape[:2]
        x, y, w, h = track.box(frame_idx)
        cx, cy = x + w / 2.0, y + h / 2.0
        cx0, cy0, cw, ch = _clip_to_frame(cx, cy, scale * w, scale * h, frame_w, frame_h)
        ix, iy, iw, ih = _round_rect(cx0, cy0, cw, ch)
        if iw <= 0 or ih <= 0:
            raise ValueError(
                f"frame {frame_idx}: crop rect collapsed to {iw}x{ih}, nothing to crop"
            )
        rects[frame_idx] = (ix, iy, iw, ih)
        patch = frame[iy:iy + ih, ix:ix + iw]
        crops[frame_idx] = cv2.resize(patch, (out_w, out_h), interpolation=cv2.INTER_LANCZOS4)
        consumed = frame_idx + 1

    if consumed != n_frames:
        raise ValueError(f"frames has {consumed} frames but track.n_frames={n_frames}")

    return crops, CropGeometry(rects=rects, out_size=(out_w, out_h))


def _feather_ramp(length: int, feather: float) -> np.ndarray:
    """A 1-D alpha ramp of `length` samples: 0 at both ends, rising linearly to a flat 1.0 over
    the outer `feather` fraction of `length` on each side.

    Sampled at pixel *centers* (`i + 0.5`), not integer pixel indices, so the ramp is symmetric
    around the rectangle's true center regardless of `length`'s parity -- an integer-index ramp
    would be off-center by half a pixel on an even-length axis. `feather <= 0` (or a `length` too
    short for any band) returns a flat 1.0 everywhere: a zero-width soft border is just a hard
    rectangle, which is a valid, if unrequested, choice for a caller rather than a special case
    this function needs to refuse.
    """
    if length <= 0:
        return np.zeros(0, dtype=np.float64)
    band = feather * length
    if band < 1e-9:
        return np.ones(length, dtype=np.float64)
    idx = np.arange(length, dtype=np.float64) + 0.5
    return np.clip(np.minimum(idx, length - idx) / band, 0.0, 1.0)


def _feather_mask(height: int, width: int, feather: float) -> np.ndarray:
    """A `(height, width)` 2-D alpha mask: the outer product of the two axes' `_feather_ramp`s.

    The outer product (rather than, say, `min` of the two axis ramps) gives the corners a smooth
    radial-looking falloff instead of a sharp diagonal crease -- both are "a feathered rectangle",
    but the product keeps the corner alpha continuous in both partial derivatives, which reads as
    softer over a real face crop's rounded features.
    """
    return np.outer(_feather_ramp(height, feather), _feather_ramp(width, feather))


def _fade_multipliers(track) -> np.ndarray:
    """One multiplier per frame, in `[0, 1]`: `1.0` wherever `track.detected(i)` is true, ramping
    linearly down to `0.0` over `FADE_FRAMES` frames of distance from the nearest frame where it
    is, and staying `0.0` beyond that.

    "Distance" is to the nearest true frame in *either* direction, computed with two linear
    sweeps (forward, then backward, each one only ever lowering a running distance) rather than
    a single-direction "frames since the last detection": `FaceTrack.detected`'s own true regions
    already sit on both sides of every real YuNet anchor (`facetrack.py`'s `_build_track`, `±every`
    around each anchor), so a stretch with no real evidence can end at a true region approached
    from *either* side -- the tail past the last detection, but also, symmetrically, the run-up to
    a detection that comes later. Fading symmetrically means the paste eases in on approach to a
    detection and eases out on departure from one, rather than snapping to full strength the
    instant `detected` flips true.

    `1.0 - distance / FADE_FRAMES` is the requested "linear fade_out over 6 frames": at
    `distance=0` (a true frame itself) the multiplier is the same `1.0` the mask would already
    give it, at `distance=FADE_FRAMES` it reaches exactly `0.0`, and it is clamped there for any
    larger distance so a long stretch with no nearby evidence at all is left completely untouched
    rather than asymptotically approaching zero without ever reaching it.
    """
    n = track.n_frames
    detected = np.array([track.detected(i) for i in range(n)], dtype=bool)

    far = n + FADE_FRAMES + 1  # larger than any real distance this loop can produce
    distance = np.where(detected, 0, far).astype(np.int64)
    for i in range(1, n):
        if distance[i] > distance[i - 1] + 1:
            distance[i] = distance[i - 1] + 1
    for i in range(n - 2, -1, -1):
        if distance[i] > distance[i + 1] + 1:
            distance[i] = distance[i + 1] + 1

    return np.clip(1.0 - distance / FADE_FRAMES, 0.0, 1.0)


def paste_back(frames, refined, geometry: CropGeometry, track, feather: float = 0.10) -> np.ndarray:
    """Paste `refined` (frames shaped like `crop_window`'s own `out_size` output, one per frame
    of `frames`) back into `frames`, through a feathered mask confined to each frame's
    `geometry.rect`, faded by `_fade_multipliers` wherever `track` has no nearby real detection.

    Every returned frame is a *new* array, and every pixel strictly outside `geometry.rect(i)` is
    copied byte-for-byte from `frames[i]` -- the module docstring's "outside the mask, bit-exact
    source" guarantee holds unconditionally, not just approximately, because those pixels are
    literally never written to. Inside the rect, `refined[i]` is first downscaled (`cv2.resize`,
    `INTER_AREA` -- the shrinking counterpart to `crop_window`'s Lanczos upscale; `cv2`'s own docs
    recommend area-averaging over Lanczos when the target is smaller, since Lanczos can ring on a
    strong downscale) back to the rect's own `(w, h)`, then alpha-blended against the source pixels
    with `_feather_mask(h, w, feather) * _fade_multipliers(track)[i]` as the per-pixel weight.

    A frame whose fade multiplier is exactly `0.0` skips the resize/blend entirely and is a
    straight copy -- not merely "blended with weight zero", which would still visit every pixel of
    the rect and could still be off by a rounding fraction from the source through the float
    arithmetic. This is what makes the fade_out tail genuinely, bit-exactly untouched rather than
    just very close to it.

    Raises `ValueError` if `frames`, `refined` or `geometry` disagree with `track.n_frames` on how
    many frames there are -- the same "surface a mismatch here, not as a confusing index error
    three lines down" reasoning as `crop_window`.
    """
    n_frames = track.n_frames
    if len(frames) != n_frames:
        raise ValueError(f"frames has {len(frames)} frames but track.n_frames={n_frames}")
    if len(refined) != n_frames:
        raise ValueError(f"refined has {len(refined)} frames but track.n_frames={n_frames}")
    if geometry.n_frames != n_frames:
        raise ValueError(
            f"geometry has {geometry.n_frames} frames but track.n_frames={n_frames}"
        )

    fade = _fade_multipliers(track)
    out = np.empty((n_frames, *np.asarray(frames[0]).shape), dtype=np.uint8)

    for frame_idx in range(n_frames):
        source = np.asarray(frames[frame_idx])
        out_frame = source.copy()
        weight = fade[frame_idx]
        if weight > 0.0:
            x, y, w, h = geometry.rect(frame_idx)
            patch = cv2.resize(np.asarray(refined[frame_idx]), (w, h),
                               interpolation=cv2.INTER_AREA)
            mask = (_feather_mask(h, w, feather) * weight)[:, :, None]
            region = out_frame[y:y + h, x:x + w].astype(np.float64)
            blended = region * (1.0 - mask) + patch.astype(np.float64) * mask
            out_frame[y:y + h, x:x + w] = np.clip(blended + 0.5, 0, 255).astype(np.uint8)
        out[frame_idx] = out_frame

    return out
