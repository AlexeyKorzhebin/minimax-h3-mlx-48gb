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
                 sleep=time.sleep, cancelled=lambda: None, poll_seconds: float = 5.0) -> Path:
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
    name = client.upload(padded, padded.name)
    prompt_id = client.prompt(build_workflow(input_name=name, prompt=prompt, strength=strength,
                                             prefix=prefix))
    while True:
        reason = cancelled()
        if reason:
            raise UpscaleError(reason)
        entry = client.history(prompt_id)
        status = (entry or {}).get("status") or {}
        if status.get("status_str") == "success" and status.get("completed"):
            break
        if status.get("status_str") == "error":
            message = next((m[1].get("exception_message") for m in status.get("messages", [])
                            if m and m[0] == "execution_error"), "ошибка исполнения")
            raise UpscaleError(f"ComfyUI: {message}")
        sleep(poll_seconds)
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
    # The frames were only an intermediate; the -ltx part now holds them. They live in our own
    # rw subdirectory of ComfyUI's output (compose.yaml mounts <output>/h3panel rw, the rest ro).
    shutil.rmtree(frames_dir, ignore_errors=True)
    padded.unlink(missing_ok=True)
    return out


def run_upscale(project_path, *, client, comfy_output, run, attempt: str, sleep=time.sleep,
                cancelled=lambda: None) -> tuple[int, str]:
    proj = project_module.load_project(project_path)
    scenes = sorted(proj.scenes, key=lambda scene: scene["idx"])
    clips = [Path(scene["clip_path"]) for scene in scenes]
    proj.set_stage_status("upscale", "running")
    strength = motion.lora_for(motion.clip_motion(clips, run=run))
    log = [f"ltx: сила {strength:g} на весь клип ({len(clips)} частей)\n"]
    try:
        for scene, clip in zip(scenes, clips):
            out = upscale_part(clip, prompt=scene.get("prompt") or FALLBACK_PROMPT,
                               strength=strength,
                               prefix=f"{PREFIX_ROOT}/{proj.id}/{clip.stem}-{attempt}",
                               client=client, comfy_output=comfy_output, run=run, sleep=sleep,
                               cancelled=cancelled)
            proj.set_scene_fields(scene["idx"], ltx_path=str(out))
            log.append(f"ltx: сцена {scene['idx']} -> {out.name}\n")
    except UpscaleError as exc:
        proj.set_stage_status("upscale", "failed")
        log.append(f"ltx: {exc}\n")
        return 1, "".join(log)
    proj.set_stage_status("upscale", "done")
    return 0, "".join(log)
