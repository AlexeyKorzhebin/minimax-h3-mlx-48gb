"""LTX-2.5 upscale through ComfyUI's HTTP API (spec §4.2), no host scripts: the workflow
template ships in this package (`ltx_workflow.json`, the graph of comfy/bin/build_ltx_workflow.py),
each part is padded to 8k+1 frames by cloning its last frame, uploaded, run, and its PNG frames
muxed back with the part's own audio and cut to the original frame count -> `<stem>-ltx.mp4`."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from h3_48gb import project as project_module
from h3_48gb.engines import motion

DEFAULT_COMFY_URL = "http://127.0.0.1:8188"
DEFAULT_COMFY_OUTPUT = "/home/alex/Outputs/comfy/output"
DENOISE = 0.10
SEED = 42
STEPS = 4
FPS = 24
PREFIX_ROOT = "h3panel"
#: ComfyUI writes a /history entry only once a prompt has finished, with a `status`. An entry
#: that is there but is neither "success" nor "error" is not "still running" (that is *no* entry);
#: tolerate a few polls in case of a half-written read, then fail instead of polling forever.
MAX_ODD_HISTORY_POLLS = 12
FALLBACK_PROMPT = ("Amateur handheld phone video in natural daylight, sharp and highly detailed, "
                   "real skin texture, real fabric texture, natural light.")
_TEMPLATE_PATH = Path(__file__).with_name("ltx_workflow.json")


class UpscaleError(Exception):
    pass


def pad_frames(n: int) -> int:
    return ((n + 6) // 8) * 8 + 1


def build_workflow(*, input_name: str, prompt: str, strength: float, prefix: str) -> dict:
    workflow = copy.deepcopy(json.loads(_TEMPLATE_PATH.read_text(encoding="utf-8")))
    workflow["10"]["inputs"]["file"] = input_name
    workflow["20"]["inputs"]["text"] = prompt
    workflow["2"]["inputs"]["strength_model"] = strength
    workflow["33"]["inputs"]["noise_seed"] = SEED
    workflow["42"]["inputs"]["filename_prefix"] = f"{prefix}/f"
    return workflow


class ComfyClient:
    def __init__(self, base_url: str, timeout: float = 60.0, boundary: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.boundary = boundary

    def _open(self, method, path, data=None, headers=None):
        request = urllib.request.Request(self.base_url + path, data=data, method=method,
                                         headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise UpscaleError(f"ComfyUI {method} {path}: HTTP {exc.code}: "
                               f"{exc.read().decode('utf-8', 'replace')[:2000]}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise UpscaleError(f"ComfyUI {method} {path} недоступен: {exc}") from None

    def upload(self, path: Path, name: str) -> str:
        boundary = self.boundary or uuid.uuid4().hex
        b = boundary.encode()
        body = (b"--" + b + b"\r\nContent-Disposition: form-data; name=\"image\"; filename=\""
                + name.encode() + b"\"\r\nContent-Type: video/mp4\r\n\r\n" + Path(path).read_bytes()
                + b"\r\n--" + b + b"\r\nContent-Disposition: form-data; name=\"type\"\r\n\r\ninput"
                + b"\r\n--" + b + b"\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\n"
                + b"true\r\n--" + b + b"--\r\n")
        answer = self._open("POST", "/upload/image", body,
                            {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return answer["name"]

    def prompt(self, workflow: dict) -> str:
        answer = self._open("POST", "/prompt",
                            json.dumps({"prompt": workflow, "client_id": "h3-panel"}).encode(),
                            {"Content-Type": "application/json"})
        if "prompt_id" not in answer:
            raise UpscaleError(f"ComfyUI отклонил воркфлоу: {json.dumps(answer)[:2000]}")
        return answer["prompt_id"]

    def history(self, prompt_id: str) -> dict | None:
        return self._open("GET", f"/history/{prompt_id}").get(prompt_id)

    def interrupt(self) -> None:
        """Stop the prompt ComfyUI is executing now (triage of task 10/11: a cancelled part
        otherwise runs its 3.5 min to the end and the next job queues behind it)."""
        self._open("POST", "/interrupt", b"{}", {"Content-Type": "application/json"})


def _count_frames(path, *, run) -> int:
    out = run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v",
               "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
              capture_output=True, text=True)
    try:
        return int(out.stdout.strip())
    except ValueError:
        raise UpscaleError(f"ffprobe не посчитал кадры {path}: {out.stderr}") from None


def _ffmpeg(cmd, *, run, what) -> None:
    result = run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise UpscaleError(f"{what}: {(result.stderr or '').strip()[:2000]}")


def upscale_part(clip, *, prompt, strength, prefix, client, comfy_output, run,
                 sleep=time.sleep, cancelled=lambda: None, poll_seconds: float = 5.0,
                 log=lambda line: None, clock=time.time, timings: dict | None = None) -> Path:
    """`timings` (final review 2026-10-07, I7), when given, is filled with this part's frame
    counts and how long each step took: pad, upload, ComfyUI (from the queued prompt to the
    finished history entry, as fine as `poll_seconds`), mux, and the whole part."""
    timings = {} if timings is None else timings
    began = clock()
    clip = Path(clip)
    src_n = _count_frames(clip, run=run)
    pad_n = pad_frames(src_n)
    work = clip.parent / "ltx-work"
    work.mkdir(parents=True, exist_ok=True)
    attempt_name = Path(prefix).name
    padded = work / f"{attempt_name}-pad.mp4"
    vf = ["-vf", f"tpad=stop_mode=clone:stop={pad_n - src_n}"] if pad_n > src_n else []
    _ffmpeg(["ffmpeg", "-v", "error", "-y", "-i", str(clip), *vf, "-frames:v", str(pad_n),
             "-c:v", "libx264", "-qp", "0", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             "-c:a", "copy", str(padded)], run=run, what="ffmpeg pad")
    if _count_frames(padded, run=run) != pad_n:
        raise UpscaleError(f"дополнение до {pad_n} кадров не удалось ({padded})")
    padded_at = clock()
    timings.update(src_frames=src_n, pad_frames=pad_n, pad_s=round(padded_at - began, 1))
    name = client.upload(padded, padded.name)
    uploaded_at = clock()
    timings["upload_s"] = round(uploaded_at - padded_at, 1)
    prompt_id = client.prompt(build_workflow(input_name=name, prompt=prompt, strength=strength,
                                             prefix=prefix))
    odd_polls = 0
    while True:
        reason = cancelled()
        if reason:
            try:
                client.interrupt()
            except UpscaleError as exc:
                log(f"ltx: ComfyUI не прервал часть: {exc}\n")
            raise UpscaleError(reason)
        entry = client.history(prompt_id)
        status = (entry or {}).get("status") or {}
        if status.get("status_str") == "success" and status.get("completed"):
            break
        if status.get("status_str") == "error":
            message = next((m[1].get("exception_message") for m in status.get("messages", [])
                            if m and m[0] == "execution_error"), "ошибка исполнения")
            raise UpscaleError(f"ComfyUI: {message}")
        if entry is not None:
            odd_polls += 1
            if odd_polls >= MAX_ODD_HISTORY_POLLS:
                raise UpscaleError(
                    f"ComfyUI: запись /history без итогового статуса после {odd_polls} опросов "
                    f"({json.dumps(status)[:200]})")
        sleep(poll_seconds)
    comfy_done_at = clock()
    timings["comfy_s"] = round(comfy_done_at - uploaded_at, 1)
    frames_dir = Path(comfy_output) / prefix
    frames = sorted(frames_dir.glob("f_*.png"))
    if len(frames) != pad_n:
        raise UpscaleError(f"ComfyUI вернул {len(frames)} кадров вместо {pad_n} ({prefix})")
    out = clip.with_name(f"{clip.stem}-ltx.mp4")
    part = out.with_name(out.stem + ".part.mp4")
    _ffmpeg(["ffmpeg", "-v", "error", "-y", "-framerate", str(FPS), "-pattern_type", "glob",
             "-i", str(frames_dir / "f_*.png"), "-i", str(clip), "-frames:v", str(src_n),
             "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-crf", "12", "-preset", "slow",
             "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(part)],
            run=run, what="ffmpeg mux")
    os.replace(part, out)
    muxed_at = clock()
    timings.update(mux_s=round(muxed_at - comfy_done_at, 1), wall_s=round(muxed_at - began, 1))
    # The frames were only an intermediate; the -ltx part now holds them. They live in our own
    # rw subdirectory of ComfyUI's output (compose.yaml mounts <output>/h3panel rw, the rest ro).
    try:
        shutil.rmtree(frames_dir)
    except OSError as exc:
        # the -ltx part is already good; a leftover directory is a disk-space problem to be told
        # about, not a reason to fail the upscale
        log(f"ltx: не удалось удалить кадры {frames_dir}: {exc}\n")
    padded.unlink(missing_ok=True)
    return out


def _scene_clips(scenes) -> list[Path]:
    clips = []
    for scene in scenes:
        if scene.get("status") != "done" or not scene.get("clip_path"):
            raise UpscaleError(f"сцена {scene.get('idx')} не готова к апскейлу "
                               f"(статус {scene.get('status')!r}, clip_path {scene.get('clip_path')!r})")
        clips.append(Path(scene["clip_path"]))
    return clips


def _write_report(proj, report: dict) -> None:
    """`<project>/upscale/report.json` (final review 2026-10-07, I7): what the battle report reads
    for the upscale -- written on every way out of an attempt that got as far as the parts."""
    path = proj.path.parent / "upscale" / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    project_module.write_json_durably(path, report)


def run_upscale(project_path, *, client, comfy_output, run, attempt: str, sleep=time.sleep,
                cancelled=lambda: None, clock=time.time) -> tuple[int, str]:
    proj = project_module.load_project(project_path)
    log: list[str] = []
    # `_submit_upscale` set the stage to `running`. If it is anything else now, the stage was
    # invalidated after this job was queued (a scene re-shot while the job waited at the GPU gate):
    # write nothing, `advance_project` queues a fresh upscale once the scenes are done again.
    if not proj.upscale_still_wanted():
        return 0, "ltx: этап апскейла сброшен после постановки задачи (сцена переснята), " \
                  "задача пропущена\n"
    proj = project_module.load_project(project_path)
    scenes = sorted(proj.scenes, key=lambda scene: scene["idx"])
    # what this attempt works on, for the locked "still current?" check on every way out
    upscaled = {scene["idx"]: scene.get("clip_path") for scene in scenes}
    report = {"attempt": attempt, "started_at": round(clock(), 3), "parts": []}
    try:
        if not scenes:
            raise UpscaleError("в проекте нет сцен")
        clips = _scene_clips(scenes)
        measured = clock()
        clip_motion = motion.clip_motion(clips, run=run)
        strength = motion.lora_for(clip_motion)
        report.update(motion=clip_motion, strength=strength,
                      motion_s=round(clock() - measured, 1))
        log.append(f"ltx: сила {strength:g} на весь клип ({len(clips)} частей)\n")
        for scene, clip in zip(scenes, clips):
            timings = {"idx": scene["idx"], "clip": str(clip)}
            report["parts"].append(timings)
            out = upscale_part(clip, prompt=scene.get("prompt") or FALLBACK_PROMPT,
                               strength=strength,
                               prefix=f"{PREFIX_ROOT}/{proj.id}/{clip.stem}-{attempt}",
                               client=client, comfy_output=comfy_output, run=run, sleep=sleep,
                               cancelled=cancelled, log=log.append, clock=clock,
                               timings=timings)
            # the scene may have been re-shot while this part was being upscaled: the check is
            # under the project lock, a stale -ltx part is never attached to the new take
            if not proj.set_scene_ltx_if_current(scene["idx"], str(clip), str(out)):
                log.append(f"ltx: сцена {scene['idx']} переснята во время апскейла, "
                           f"результат отброшен\n")
                break
            log.append(f"ltx: сцена {scene['idx']} -> {out.name} (ComfyUI "
                       f"{timings['comfy_s']:.1f} с, часть {timings['wall_s']:.1f} с)\n")
    except (UpscaleError, motion.MotionError) as exc:
        proj.finish_upscale(upscaled, ok=False)
        log.append(f"ltx: {exc}\n")
        _write_report(proj, {**report, "status": "failed", "error": str(exc),
                             "finished_at": round(clock(), 3)})
        return 1, "".join(log)
    except Exception as exc:  # noqa: BLE001 -- a bug here must fail the job, not kill the worker
        proj.finish_upscale(upscaled, ok=False)
        log.append(f"ltx crashed: {type(exc).__name__}: {exc}\n")
        _write_report(proj, {**report, "status": "failed", "error": f"{type(exc).__name__}: {exc}",
                             "finished_at": round(clock(), 3)})
        return 1, "".join(log)
    _write_report(proj, {**report, "status": "done", "finished_at": round(clock(), 3)})
    if not proj.finish_upscale(upscaled):
        log.append("ltx: сцены изменились во время апскейла, этап вернулся в draft и "
                   "поставится заново\n")
    return 0, "".join(log)
