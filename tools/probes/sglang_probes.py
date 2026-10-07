#!/usr/bin/env python3
"""Live probes for spec §6 against the panel's own sglang client. Run inside the panel container
(`docker compose run --rm --entrypoint python h3-panel tools/probes/sglang_probes.py ...`), only
with a free GPU: it asks the dispatcher for H3 and gives up -- never waits -- if anything
foreign holds the card. Results go to /home/alex/Outputs/h3-panel/probes/*.jsonl.

Names: picture_numbering | picture_numbering_mirrored | keyframe_without_reference | eight_references | references:N | beach.
`--together` posts every named probe back to back before polling any, so every POST after the
first is timed while the server is already rendering (the adapter's 60 s timeout question)."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

from h3_48gb.engines import sglang as sg
from h3_48gb.engines.dispatcher_client import PROBES, DispatcherClient

OUT_DIR = Path("/home/alex/Outputs/h3-panel/probes")
BEACH_JOBS = Path("/home/alex/Projects/h3-bench/beach-jobs.json")
_COMMON = {"model": sg.MODEL, "num_outputs_per_prompt": 1, "flow_shift": 12.0,
           "audio_flow_shift": 3.0, "seed": 42, "quality": "lossless"}
_PROBE_TARGET = {"short_edge": 512, "aspect_ratio": "16:9", "duration_seconds": 3.0}
_PROBE_STEPS = 8


def beach_payload(job: dict) -> dict:
    """chain_beach.py:render's head-scene payload (keyframe + refs, 16:9), steps cut to 2 (the
    server's floor for H3: both sigma endpoints are in the schedule)."""
    conditions = [{"type": "image", "uri": job["keyframe"], "role": "keyframe", "frame_index": 0}]
    conditions += [{"type": "image", "uri": ref, "role": "reference"} for ref in job.get("refs", [])]
    return {"model": sg.MODEL, "prompt": job["prompt"], "task": "ref2va",
            "conditions": sg.normalize_h3_conditions(conditions),
            "target": {"short_edge": job["short_edge"], "aspect_ratio": "16:9",
                       "duration_seconds": float(job["duration"])},
            "num_outputs_per_prompt": 1, "num_inference_steps": 2, "flow_shift": 12.0,
            "audio_flow_shift": 3.0, "seed": int(job["seed"]), "quality": "lossless"}


def _card(path: Path, colour, size=(512, 512)) -> str:
    Image.new("RGB", tuple(size), colour).save(path)
    return str(path)


def _shape(path: Path, colour, *, ball: bool) -> str:
    image = Image.new("RGB", (512, 512), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    (draw.ellipse if ball else draw.rectangle)((128, 128, 384, 384), fill=colour)
    image.save(path)
    return str(path)


def references_payload(workdir: Path, n: int, size=(512, 512)) -> dict:
    """n plain colour cards as references, each named in order -- the memory probe (spec §6 b)."""
    workdir.mkdir(parents=True, exist_ok=True)
    refs = [_card(workdir / f"r{i}.png", ((i * 30) % 255, 80, 160), size) for i in range(n)]
    return {**_COMMON, "num_inference_steps": _PROBE_STEPS, "task": "ref2va",
            "prompt": "subject_definitions:\n" + "\n".join(
                f"<Subject {i + 1}> is the colour card in <Picture {i + 1}>." for i in range(n))
            + "\n\nThe cards lie on a table.",
            "conditions": [{"type": "image", "uri": ref, "role": "reference"} for ref in refs],
            "target": dict(_PROBE_TARGET)}


_NUMBERING_SIDES = ("<Subject 1> stands at the left edge and <Subject 2> at the right edge",
                    "<Subject 1> stands at the right edge and <Subject 2> at the left edge")


def _numbering_payload(workdir: Path, *, mirrored: bool = False) -> dict:
    """spec §6 (a): confirm by a render that, with a keyframe present, <Picture 1> is the first
    *reference* picture (the server code says the keyframe is not numbered). Red on the left =>
    confirmed; grey/table things on the left => not. The mirrored control swaps the sides only."""
    workdir.mkdir(parents=True, exist_ok=True)
    table = _card(workdir / "table.png", (150, 150, 150))
    red = _shape(workdir / "red-cube.png", (220, 20, 20), ball=False)
    blue = _shape(workdir / "blue-ball.png", (20, 40, 220), ball=True)
    sides = _NUMBERING_SIDES[1] if mirrored else _NUMBERING_SIDES[0]
    return {**_COMMON, "num_inference_steps": _PROBE_STEPS, "task": "ref2va",
            "prompt": ("subject_definitions:\n"
                       "<Subject 1> is the object shown in <Picture 1>.\n"
                       "<Subject 2> is the object shown in <Picture 2>.\n\n"
                       f"On the empty table, {sides}; the camera does not move."),
            "conditions": [{"type": "image", "uri": table, "role": "keyframe", "frame_index": 0},
                           {"type": "image", "uri": red, "role": "reference"},
                           {"type": "image", "uri": blue, "role": "reference"}],
            "target": {**_PROBE_TARGET, "aspect_ratio": "auto"}}


def _keyframe_only_payload(workdir: Path) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    keyframe = _card(workdir / "kf.png", (200, 120, 40))
    return {**_COMMON, "num_inference_steps": _PROBE_STEPS, "task": "ref2va",
            "prompt": "The scene continues.",
            "conditions": [{"type": "image", "uri": keyframe, "role": "keyframe",
                            "frame_index": 0}],
            "target": {**_PROBE_TARGET, "aspect_ratio": "auto"}}


def probe_payloads(workdir: Path) -> dict[str, dict]:
    """Each probe writes its pictures into its own subfolder: the server reads them by path at
    render time, so one probe must never overwrite another's inputs in the same run."""
    return {"picture_numbering": _numbering_payload(workdir / "picture_numbering"),
            "keyframe_without_reference": _keyframe_only_payload(
                workdir / "keyframe_without_reference"),
            "eight_references": references_payload(workdir / "eight_references", 8)}


PROBE_NAMES = ("picture_numbering", "picture_numbering_mirrored", "keyframe_without_reference",
               "eight_references", "references:N", "beach")


def named_payload(name: str, workdir: Path, *, beach_jobs, steps, duration=None,
                  ref_size=(512, 512)) -> dict:
    if name == "beach":
        jobs = json.loads(Path(beach_jobs).read_text(encoding="utf-8"))["jobs"]
        payload = beach_payload(next(j for j in jobs if j["name"] == "beach-01"))
    elif name.startswith("references:"):
        n = int(name.split(":", 1)[1])
        width, height = ref_size
        payload = references_payload(workdir / f"references-{n}-{width}x{height}", n, ref_size)
    elif name == "picture_numbering":
        payload = _numbering_payload(workdir / name)
    elif name == "picture_numbering_mirrored":
        payload = _numbering_payload(workdir / name, mirrored=True)
    elif name == "keyframe_without_reference":
        payload = _keyframe_only_payload(workdir / name)
    elif name == "eight_references":
        payload = references_payload(workdir / name, 8)
    else:
        raise ValueError(f"unknown probe {name!r}; known: {', '.join(PROBE_NAMES)}")
    if duration is not None:
        payload = {**payload, "target": {**payload["target"], "duration_seconds": float(duration)}}
    return payload if steps is None else {**payload, "num_inference_steps": int(steps)}


def _post(name: str, payload: dict, client, clock) -> dict:
    started = clock()
    try:
        video_id = client.create(payload)["id"]
    except sg.SglangHTTPError as exc:
        return {"probe": name, "result": "http_error", "status": exc.status, "detail": exc.detail,
                "create_s": round(clock() - started, 3)}
    return {"probe": name, "id": video_id, "create_s": round(clock() - started, 3),
            "_started": started}


#: A probe that is still `queued`/`in_progress` after this long is given up on, so a server that
#: never finishes cannot keep H3 (~45 GB) up for ever; the slowest probe measured took 680 s.
MAX_WAIT_SECONDS = 3600.0
#: The dispatcher's own start timeout ends /acquire earlier; this only bounds a dispatcher that
#: keeps answering `starting`.
MAX_ACQUIRE_SECONDS = 1200.0


def _wait(posted: dict, client, *, poll, sleep, clock, max_wait: float = MAX_WAIT_SECONDS) -> dict:
    if "_started" not in posted:
        return posted
    slowest = 0.0
    while True:
        asked = clock()
        status = client.get(posted["id"])
        slowest = max(slowest, clock() - asked)
        if status.get("status") in ("completed", "failed"):
            break
        if clock() - posted["_started"] >= max_wait:
            status = {**status, "status": "timeout",
                      "error": f"still {status.get('status')!r} after {max_wait:.0f} s"}
            break
        sleep(poll)
    return {"probe": posted["probe"], "result": status["status"], "id": posted["id"],
            "create_s": posted["create_s"], "max_get_s": round(slowest, 3),
            "wall_s": round(clock() - posted["_started"], 1),
            "peak_memory_mb": status.get("peak_memory_mb"),
            "inference_time_s": status.get("inference_time_s"), "error": status.get("error")}


def run_probe(name: str, payload: dict, client, *, poll: float = 10.0, sleep=time.sleep,
              clock=time.monotonic, max_wait: float = MAX_WAIT_SECONDS) -> dict:
    """POST once (timed), poll to the end; the server's own peak and inference time are kept."""
    return _wait(_post(name, payload, client, clock), client, poll=poll, sleep=sleep, clock=clock,
                 max_wait=max_wait)


def _acquire_h3(dispatcher, *, sleep=time.sleep, clock=time.monotonic,
                max_wait: float = MAX_ACQUIRE_SECONDS) -> bool:
    started = clock()
    while True:
        answer = dispatcher.acquire("h3")
        state = answer.get("state")
        if state == "ready":
            return True
        if state in ("wait", "wait_qwen", "failed"):
            print(f"пробы отложены: {state}: {answer.get('reason')} {answer.get('log') or ''}",
                  file=sys.stderr)
            return False
        if clock() - started >= max_wait:
            print(f"пробы отложены: H3 не поднялся за {max_wait:.0f} с ({state})", file=sys.stderr)
            return False
        sleep(10)


def _frames(client, result: dict, stem: str) -> None:
    video = OUT_DIR / f"{stem}-{result['probe'].replace(':', '-')}.mp4"
    client.download(result["id"], video)
    result["video"] = str(video)
    frames = []
    for at in ("0", "1.5", "2.5"):
        frame = video.with_name(f"{video.stem}-t{at}.png")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", at, "-i", str(video),
                        "-frames:v", "1", str(frame)], check=True)
        frames.append(str(frame))
    result["frames"] = frames          # the owner looks: red square on the left?


def _request_summary(payload: dict) -> dict:
    """What the server was asked, written next to each result: without it three `references:6`
    lines (3 s / 10 s / portrait cards) in the jsonl cannot be told apart."""
    sizes = []
    for condition in payload["conditions"]:
        if condition.get("role") == "reference" and condition.get("type") == "image":
            try:
                with Image.open(condition["uri"]) as image:
                    sizes.append("x".join(map(str, image.size)))
            except OSError:
                sizes.append(None)
    return {"num_inference_steps": payload["num_inference_steps"],
            "target": dict(payload["target"]),
            "roles": [c.get("role") for c in payload["conditions"]],
            "reference_sizes": sizes}


def _ref_size(text: str) -> tuple[int, int]:
    try:
        width, height = (int(v) for v in text.lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"--ref-size wants WxH, got {text!r}") from None
    if width < 1 or height < 1:
        raise argparse.ArgumentTypeError(f"--ref-size wants positive WxH, got {text!r}")
    return width, height


def main(argv=None, *, dispatcher=None, client=None, sleep=time.sleep, clock=time.monotonic,
         max_wait: float = MAX_WAIT_SECONDS) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="+")
    parser.add_argument("--keep-h3", action="store_true", help="do not release the card after")
    parser.add_argument("--together", action="store_true",
                        help="post all probes first, then poll (POST timed under load)")
    parser.add_argument("--steps", type=int, default=None, help="override num_inference_steps")
    parser.add_argument("--duration", type=float, default=None, help="override seconds")
    parser.add_argument("--ref-size", type=_ref_size, default=(512, 512),
                        help="references:N card size, WxH")
    parser.add_argument("--beach-jobs", type=Path, default=BEACH_JOBS)
    args = parser.parse_args(argv)
    stem = f"{datetime.now():%Y%m%d-%H%M%S}"
    # Every input is built and checked before the card is asked for: a typo in a probe name or a
    # missing beach-jobs.json must not leave H3 (~45 GB) up with nothing to render.
    payloads = {name: named_payload(name, OUT_DIR / "inputs" / stem, beach_jobs=args.beach_jobs,
                                    steps=args.steps, duration=args.duration,
                                    ref_size=args.ref_size)
                for name in args.names}
    run_args = {"steps": args.steps, "duration": args.duration,
                "ref_size": "x".join(map(str, args.ref_size)), "together": args.together}
    # "probes" owns what it raises: its `finally: release()` stops only that, never the panel's.
    dispatcher = dispatcher if dispatcher is not None else DispatcherClient(client=PROBES)
    client = client if client is not None else sg.SglangClient(sg.DEFAULT_URL)
    out_path = OUT_DIR / f"{stem}.jsonl"

    def finish(result: dict) -> None:
        if result.get("result") == "completed" and result["probe"].startswith("picture_numbering"):
            _frames(client, result, stem)
        result = {**result, "args": run_args, "request": _request_summary(payloads[result["probe"]])}
        print(json.dumps(result, ensure_ascii=False), flush=True)
        with out_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")

    # From the first /acquire on, any way out -- an error, a timeout, Ctrl-C while H3 is still
    # starting -- gives the card back (unless --keep-h3 asked otherwise).
    try:
        if not _acquire_h3(dispatcher, sleep=sleep, clock=clock):
            return 2
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        if args.together:
            posted = [_post(name, payloads[name], client, clock) for name in args.names]
            for item in posted:
                finish(_wait(item, client, poll=10.0, sleep=sleep, clock=clock, max_wait=max_wait))
        else:
            for name in args.names:
                finish(run_probe(name, payloads[name], client, sleep=sleep, clock=clock,
                                 max_wait=max_wait))
    finally:
        if not args.keep_h3:
            dispatcher.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
