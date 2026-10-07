"""`h3 generate` argv as the sglang engine understands it (spec §3.3.2, §4.1.4, §4.1.5).

`cli.build_parser` is never used on this path: it fills in MLX defaults (`--turbo-lora`,
`--adaln-cache`, `--steps 8`, `cli.py:52-58`, `542-545`) and `RunSpec` refuses a missing LoRA file
(`cli.py:451`), none of which exist in the panel image. Any flag outside `_VALUE_FLAGS` is refused
by name. The refusals mirror what the sglang server itself rejects with a bare 400
(`request_validation.py`), so a scene fails in the form before it costs a queue slot.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

FPS = 24
FRAMES_PER_CHUNK = 17
FRAME_REMAINDER = 5
MIN_SECONDS = 3.0
MAX_SECONDS = 15.0
DEFAULT_STEPS = 50
MIN_STEPS = 2      # the server refuses fewer (MiniMaxH3Scheduler needs >= 2)
MAX_STEPS = 100    # the panel's own cap: nothing above 50 was ever measured, 100 is a typo guard
DEFAULT_SEED = 42
DEFAULT_MAX_REF_IMAGES = 5   # probe 2026-10-07: 6 portraits at 10 s = 63.6 of ~64.9 GB

#: Panel canvas -> (short_edge, aspect_ratio) (spec §4.1.4). The delivered frame size is read from
#: the mp4, not from this table: sglang picks its own frame for a short edge and an aspect.
CANVAS_TABLE = {(896, 512): (512, "16:9"), (512, 896): (512, "9:16"), (768, 768): (768, "1:1")}
ASPECT_RATIOS = ("auto", "21:9", "16:9", "4:3", "3:2", "1:1", "2:3", "3:4", "9:16")
#: spec §4.1.3, probe 2026-10-07: the server runs with VARIANT=ref2va and serves only ref2va
#: (t2va is accepted with HTTP 200 and then fails "not served by MiniMax H3 partition 'ref2va'").
TASKS = ("ref2va",)

#: Path policy for `web.check_path_flags` on this engine (the MLX table `web.PATH_FLAGS` is pinned
#: to the MLX parser's flags by test_web.py, so the two tables stay separate).
PATH_FLAGS = {"--image": "read", "--ref": "read", "--audio": "read", "--outdir": "write"}

_VALUE_FLAGS = ("--width", "--height", "--duration", "--steps", "--seed", "--tag", "--outdir",
                "--image", "--ref", "--audio", "--task", "--aspect")
_REPEATABLE = ("--ref", "--audio")

ERROR_CODES = {
    "unsupported_on_sglang": "a generate flag the sglang engine does not implement",
    "sglang_args_invalid": "a sglang generate argument is missing, repeated or malformed",
    "canvas_unsupported_on_sglang": "--width/--height is not a canvas the sglang format table maps",
    "duration_out_of_range": "--duration is outside sglang's 3..15 s",
    "duration_off_grid": "--duration does not land on sglang's 17n+5 frame grid at 24 fps",
    "ref2va_needs_reference": "a scene needs at least one reference (an @tag picture, or a clip's track piece)",
    "too_many_reference_images": "more reference pictures than H3_MAX_REF_IMAGES allows per scene",
    "condition_file_missing": "a --image/--ref/--audio path does not exist",
}


class SglangArgsError(ValueError):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = dict(detail or {})


@dataclass(frozen=True)
class SglangSpec:
    prompt: str
    width: int
    height: int
    duration: float
    frames: int
    steps: int
    seed: int
    tag: str
    outdir: str
    image: str | None
    refs: tuple[str, ...]
    audio: tuple[str, ...]
    task: str
    short_edge: int
    aspect_ratio: str


def grid_frames_up(frames: int) -> int:
    frames = max(int(frames), FRAME_REMAINDER)
    return frames + (FRAME_REMAINDER - frames) % FRAMES_PER_CHUNK


def max_ref_images(environ=None) -> int:
    raw = (os.environ if environ is None else environ).get("H3_MAX_REF_IMAGES", "").strip()
    if not raw:
        return DEFAULT_MAX_REF_IMAGES
    try:
        value = int(raw)
    except ValueError:
        raise SglangArgsError("sglang_args_invalid",
                              f"H3_MAX_REF_IMAGES={raw!r} — не целое число", {"value": raw}) from None
    if value < 1:
        raise SglangArgsError("sglang_args_invalid", "H3_MAX_REF_IMAGES должен быть ≥ 1",
                              {"value": value})
    return value


def _collect(args: list[str]) -> tuple[str | None, dict[str, list[str]]]:
    prompt = None
    values: dict[str, list[str]] = {}
    index = 1
    while index < len(args):
        token = args[index]
        if token.startswith("--"):
            flag, equals, inline = token.partition("=")
            if flag not in _VALUE_FLAGS:
                raise SglangArgsError("unsupported_on_sglang", f"unsupported_on_sglang: {flag}",
                                      {"flag": flag})
            if equals:
                value = inline
            elif index + 1 < len(args):
                index += 1
                value = args[index]
            else:
                raise SglangArgsError("sglang_args_invalid", f"{flag} без значения", {"flag": flag})
            if flag in values and flag not in _REPEATABLE:
                raise SglangArgsError("sglang_args_invalid", f"{flag} указан дважды", {"flag": flag})
            values.setdefault(flag, []).append(value)
        elif prompt is None:
            prompt = token
        else:
            raise SglangArgsError("sglang_args_invalid", "больше одного промпта в аргументах",
                                  {"extra": token})
        index += 1
    return prompt, values


def _one(values, flag, default=None, *, required=False):
    if flag in values:
        return values[flag][0]
    if required:
        raise SglangArgsError("sglang_args_invalid", f"не хватает {flag}", {"flag": flag})
    return default


def _number(values, flag, kind, default=None, *, required=False):
    raw = _one(values, flag, default, required=required)
    try:
        return kind(raw)
    except (TypeError, ValueError):
        raise SglangArgsError("sglang_args_invalid", f"{flag}={raw!r} — не число",
                              {"flag": flag, "value": raw}) from None


def parse(argv, *, environ=None, check_files: bool = True) -> SglangSpec:
    args = [str(item) for item in argv]
    if not args or args[0] != "generate":
        raise SglangArgsError("sglang_args_invalid", "ожидалась команда generate", {"args": args[:1]})
    prompt, values = _collect(args)
    if prompt is None or not prompt.strip():
        raise SglangArgsError("prompt_missing", "нет промпта сцены", {})

    width = _number(values, "--width", int, required=True)
    height = _number(values, "--height", int, required=True)
    if (width, height) not in CANVAS_TABLE:
        raise SglangArgsError("canvas_unsupported_on_sglang",
                              f"холст {width}x{height} не поддержан на sglang; можно: "
                              f"{', '.join(f'{w}x{h}' for w, h in CANVAS_TABLE)}",
                              {"width": width, "height": height})
    short_edge, table_aspect = CANVAS_TABLE[(width, height)]

    duration = _number(values, "--duration", float, required=True)
    if not MIN_SECONDS <= duration <= MAX_SECONDS:
        raise SglangArgsError("duration_out_of_range",
                              f"длительность {duration:g} с вне {MIN_SECONDS:g}–{MAX_SECONDS:g} с",
                              {"duration": duration})
    frames = round(duration * FPS)
    if grid_frames_up(frames) != frames:
        nxt = grid_frames_up(frames)
        raise SglangArgsError("duration_off_grid",
                              f"длительность {duration:g} с = {frames} кадров, не на сетке 17n+5; "
                              f"ближайшая вверх — {nxt / FPS:.3f} с",
                              {"frames": frames, "next_grid_frames": nxt,
                               "next_grid_seconds": nxt / FPS})

    # The spec carries the grid's own duration, not the typed one: 7.31 s snaps to 175 frames, and
    # 175 frames are 175/24 s -- that exact value is what downstream cuts the track and drops head
    # frames by, so it has to equal the mp4's length, not the user's rounding.
    duration = frames / FPS

    steps = _number(values, "--steps", int, DEFAULT_STEPS)
    seed = _number(values, "--seed", int, DEFAULT_SEED)
    if not MIN_STEPS <= steps <= MAX_STEPS or seed < 0:
        raise SglangArgsError("sglang_args_invalid", f"--steps {MIN_STEPS}..{MAX_STEPS} и --seed ≥ 0",
                              {"steps": steps, "seed": seed})
    tag = _one(values, "--tag", required=True)
    outdir = _one(values, "--outdir", required=True)
    image = _one(values, "--image")
    refs = tuple(values.get("--ref", []))
    audio = tuple(values.get("--audio", []))
    has_reference = bool(refs or audio)

    task = _one(values, "--task", "ref2va")
    if task not in TASKS:
        raise SglangArgsError("sglang_args_invalid",
                              f"--task {task!r}: сервер H3 поднят как ref2va и обслуживает только его",
                              {"task": task})
    if not has_reference:
        raise SglangArgsError("ref2va_needs_reference", "нужен хотя бы один референс (@тег) в сцене",
                              {"keyframe": image})
    limit = max_ref_images(environ)
    if len(refs) > limit:
        raise SglangArgsError("too_many_reference_images",
                              f"картинок-референсов {len(refs)}, а можно не больше {limit}",
                              {"count": len(refs), "limit": limit})

    aspect = _one(values, "--aspect", table_aspect)
    if aspect not in ("auto", table_aspect):
        raise SglangArgsError("sglang_args_invalid",
                              f"--aspect {aspect!r}: для {width}x{height} можно auto или {table_aspect}",
                              {"aspect": aspect})

    if check_files:
        for path in [image, *refs, *audio]:
            if path is not None and not Path(path).is_file():
                raise SglangArgsError("condition_file_missing", f"нет файла {path}", {"path": path})

    return SglangSpec(prompt=prompt, width=width, height=height, duration=duration, frames=frames,
                      steps=steps, seed=seed, tag=tag, outdir=outdir, image=image, refs=refs,
                      audio=audio, task=task, short_edge=short_edge, aspect_ratio=aspect)


def output_stem(spec: SglangSpec) -> str:
    return f"{spec.outdir}/h3-{spec.tag}-{spec.width}x{spec.height}"


def dry_run_report(spec: SglangSpec) -> dict:
    return {"dry_run": True, "engine": "sglang", "output_stem": output_stem(spec),
            "canvas": f"{spec.width}x{spec.height}", "duration_seconds": spec.duration,
            "frames": spec.frames, "grid_points": spec.steps, "task": spec.task}
