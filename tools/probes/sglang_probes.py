#!/usr/bin/env python3
"""Live probes for spec §6 against the panel's own sglang client. Run inside the panel container
(`docker compose run --rm --entrypoint python h3-panel tools/probes/sglang_probes.py ...`), only
with a free GPU: it asks the dispatcher for H3 and gives up -- never waits -- if anything
foreign holds the card. Results go to /home/alex/Outputs/h3-panel/probes/*.jsonl.

Names: picture_numbering | keyframe_without_reference | eight_references | references:N | beach.
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
from h3_48gb.engines.dispatcher_client import DispatcherClient

OUT_DIR = Path("/home/alex/Outputs/h3-panel/probes")
BEACH_JOBS = Path("/home/alex/Projects/h3-bench/beach-jobs.json")
_COMMON = {"model": sg.MODEL, "num_outputs_per_prompt": 1, "flow_shift": 12.0,
           "audio_flow_shift": 3.0, "seed": 42, "quality": "lossless"}
_PROBE_TARGET = {"short_edge": 512, "aspect_ratio": "16:9", "duration_seconds": 3.0}
_PROBE_STEPS = 8


def beach_payload(job: dict) -> dict:
    """chain_beach.py:render's head-scene payload (keyframe + refs, 16:9), steps cut to 1."""
    conditions = [{"type": "image", "uri": job["keyframe"], "role": "keyframe", "frame_index": 0}]
    conditions += [{"type": "image", "uri": ref, "role": "reference"} for ref in job.get("refs", [])]
    return {"model": sg.MODEL, "prompt": job["prompt"], "task": "ref2va",
            "conditions": sg.normalize_h3_conditions(conditions),
            "target": {"short_edge": job["short_edge"], "aspect_ratio": "16:9",
                       "duration_seconds": float(job["duration"])},
            "num_outputs_per_prompt": 1, "num_inference_steps": 1, "flow_shift": 12.0,
            "audio_flow_shift": 3.0, "seed": int(job["seed"]), "quality": "lossless"}


def _card(path: Path, colour) -> str:
    Image.new("RGB", (512, 512), colour).save(path)
    return str(path)


def _shape(path: Path, colour, *, ball: bool) -> str:
    image = Image.new("RGB", (512, 512), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    (draw.ellipse if ball else draw.rectangle)((128, 128, 384, 384), fill=colour)
    image.save(path)
    return str(path)


def references_payload(workdir: Path, n: int) -> dict:
    """n plain colour cards as references, each named in order -- the memory probe (spec §6 b)."""
    workdir.mkdir(parents=True, exist_ok=True)
    refs = [_card(workdir / f"r{i}.png", ((i * 30) % 255, 80, 160)) for i in range(n)]
    return {**_COMMON, "num_inference_steps": _PROBE_STEPS, "task": "ref2va",
            "prompt": "subject_definitions:\n" + "\n".join(
                f"<Subject {i + 1}> is the colour card in <Picture {i + 1}>." for i in range(n))
            + "\n\nThe cards lie on a table.",
            "conditions": [{"type": "image", "uri": ref, "role": "reference"} for ref in refs],
            "target": dict(_PROBE_TARGET)}


def probe_payloads(workdir: Path) -> dict[str, dict]:
    workdir.mkdir(parents=True, exist_ok=True)
    keyframe = _card(workdir / "kf.png", (200, 120, 40))
    base = {**_COMMON, "num_inference_steps": _PROBE_STEPS}
    table = _card(workdir / "table.png", (150, 150, 150))
    red = _shape(workdir / "red-cube.png", (220, 20, 20), ball=False)
    blue = _shape(workdir / "blue-ball.png", (20, 40, 220), ball=True)
    return {
        # spec §6 (a): confirm by a render that, with a keyframe present, <Picture 1> is the
        # first *reference* picture (the server code says the keyframe is not numbered).
        # Red on the left => confirmed; grey/table things on the left => not.
        "picture_numbering": {
            **base, "task": "ref2va",
            "prompt": ("subject_definitions:\n"
                       "<Subject 1> is the object shown in <Picture 1>.\n"
                       "<Subject 2> is the object shown in <Picture 2>.\n\n"
                       "On the empty table, <Subject 1> stands at the left edge and <Subject 2> "
                       "at the right edge; the camera does not move."),
            "conditions": [{"type": "image", "uri": table, "role": "keyframe", "frame_index": 0},
                           {"type": "image", "uri": red, "role": "reference"},
                           {"type": "image", "uri": blue, "role": "reference"}],
            "target": {**_PROBE_TARGET, "aspect_ratio": "auto"}},
        "keyframe_without_reference": {
            **base, "prompt": "The scene continues.", "task": "ref2va",
            "conditions": [{"type": "image", "uri": keyframe, "role": "keyframe",
                            "frame_index": 0}],
            "target": {**_PROBE_TARGET, "aspect_ratio": "auto"}},
        "eight_references": references_payload(workdir, 8),
    }


def named_payload(name: str, workdir: Path, *, beach_jobs, steps) -> dict:
    if name == "beach":
        jobs = json.loads(Path(beach_jobs).read_text(encoding="utf-8"))["jobs"]
        payload = beach_payload(next(j for j in jobs if j["name"] == "beach-01"))
    elif name.startswith("references:"):
        payload = references_payload(workdir, int(name.split(":", 1)[1]))
    else:
        payload = probe_payloads(workdir)[name]
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


def _wait(posted: dict, client, *, poll, sleep, clock) -> dict:
    if "_started" not in posted:
        return posted
    slowest = 0.0
    while True:
        asked = clock()
        status = client.get(posted["id"])
        slowest = max(slowest, clock() - asked)
        if status.get("status") in ("completed", "failed"):
            break
        sleep(poll)
    return {"probe": posted["probe"], "result": status["status"], "id": posted["id"],
            "create_s": posted["create_s"], "max_get_s": round(slowest, 3),
            "wall_s": round(clock() - posted["_started"], 1),
            "peak_memory_mb": status.get("peak_memory_mb"),
            "inference_time_s": status.get("inference_time_s"), "error": status.get("error")}


def run_probe(name: str, payload: dict, client, *, poll: float = 10.0, sleep=time.sleep,
              clock=time.monotonic) -> dict:
    """POST once (timed), poll to the end; the server's own peak and inference time are kept."""
    return _wait(_post(name, payload, client, clock), client, poll=poll, sleep=sleep, clock=clock)


def _acquire_h3(dispatcher: DispatcherClient) -> bool:
    while True:
        answer = dispatcher.acquire("h3")
        state = answer.get("state")
        if state == "ready":
            return True
        if state in ("wait", "wait_qwen", "failed"):
            print(f"пробы отложены: {state}: {answer.get('reason')} {answer.get('log') or ''}",
                  file=sys.stderr)
            return False
        time.sleep(10)


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="+")
    parser.add_argument("--keep-h3", action="store_true", help="do not release the card after")
    parser.add_argument("--together", action="store_true",
                        help="post all probes first, then poll (POST timed under load)")
    parser.add_argument("--steps", type=int, default=None, help="override num_inference_steps")
    parser.add_argument("--beach-jobs", type=Path, default=BEACH_JOBS)
    args = parser.parse_args(argv)
    dispatcher = DispatcherClient()
    if not _acquire_h3(dispatcher):
        return 2
    client = sg.SglangClient(sg.DEFAULT_URL)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now():%Y%m%d-%H%M%S}"
    out_path = OUT_DIR / f"{stem}.jsonl"
    payloads = {name: named_payload(name, OUT_DIR / "inputs", beach_jobs=args.beach_jobs,
                                    steps=args.steps) for name in args.names}

    def finish(result: dict) -> None:
        if result.get("result") == "completed" and result["probe"] == "picture_numbering":
            _frames(client, result, stem)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        with out_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")

    try:
        if args.together:
            posted = [_post(name, payloads[name], client, time.monotonic) for name in args.names]
            for item in posted:
                finish(_wait(item, client, poll=10.0, sleep=time.sleep, clock=time.monotonic))
        else:
            for name in args.names:
                finish(run_probe(name, payloads[name], client))
    finally:
        if not args.keep_h3:
            dispatcher.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
