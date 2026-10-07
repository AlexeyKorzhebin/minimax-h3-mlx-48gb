"""One sglang generate job, run inside the worker process (spec §3.3.3, §4.1, §5).

POST /v1/videos once; write sglang's id into the job (`engine_ref`) at once; poll every 20 s;
download `/content` atomically on `completed`. A worker restart resumes polling the same id (the
queue hands such jobs back as `resumable`, never as pending), so a scene is posted exactly once.
`DELETE` is sent on cancel but does not stop the GPU (video_api.py:821-827) -- the message says so.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

import numpy as np

from h3_48gb import framecheck
from h3_48gb import queue as q
from h3_48gb.engines import estimate as sglang_estimate
from h3_48gb.engines import sglang_args

MODEL = "MiniMaxAI/MiniMax-H3"
DEFAULT_URL = "http://127.0.0.1:30020"
POLL_SECONDS = 20.0
LOST_RETRIES = 5
LOST_RETRY_SECONDS = 30.0
_CANCEL_SLICE_SECONDS = 1.0

# -- ported from h3-bench/video_paths.py (no import from h3-bench, spec §4.1.3) ----------------
_PROJECTS_ROOT = Path(os.environ.get("ALEX_PROJECTS_ROOT", "/home/alex/Projects"))
_OUTPUTS_ROOT = Path(os.environ.get("ALEX_OUTPUTS_ROOT", "/home/alex/Outputs"))
_OUTPUT_DIR = Path(os.environ.get("H3_BENCH_OUTPUT_DIR", str(_OUTPUTS_ROOT / "h3-bench")))
_H3_BENCH_DIR = Path(os.environ.get("H3_BENCH_DIR", str(_PROJECTS_ROOT / "h3-bench")))
_PUBPORT_IMAGES_DIR = Path(os.environ.get("PUBPORT_IMAGES_DIR",
                                          str(_H3_BENCH_DIR / "inputs/pubport-images")))


def normalize_h3_uri(value):
    """Map only known legacy path roots, keeping file:// and unrelated URIs untouched."""
    if not isinstance(value, str):
        return value
    scheme = "file://" if value.startswith("file://") else ""
    path = value[len(scheme):]
    roots = (("/home/alex/PubPort/h3-bench/outputs", _OUTPUT_DIR),
             ("/home/alex/h3-bench/outputs", _OUTPUT_DIR),
             ("/home/alex/Outputs/h3-bench", _OUTPUT_DIR),
             ("/home/alex/PubPort/images", _PUBPORT_IMAGES_DIR),
             ("/home/alex/Projects/h3-bench", _H3_BENCH_DIR),
             ("/home/alex/h3-bench", _H3_BENCH_DIR))
    for old, new in roots:
        if path == old or path.startswith(old + "/"):
            return scheme + str(new) + path[len(old):]
    return value


def normalize_h3_conditions(conditions):
    return [dict(condition, uri=normalize_h3_uri(condition["uri"]))
            if "uri" in condition else dict(condition) for condition in conditions]


class SglangHTTPError(Exception):
    def __init__(self, status: int, detail: str, body: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail
        self.body = body


class SglangUnavailable(Exception):
    """No HTTP answer at all: refused, reset, timed out, or a body cut short."""


def _detail(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    try:
        detail = json.loads(text).get("detail")
    except (ValueError, AttributeError):
        return text[:2000]
    return detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)[:2000]


class SglangClient:
    def __init__(self, base_url: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _open(self, method: str, path: str, payload=None):
        """One request, answered by `(connection, response)` with the response still open; the
        caller closes both. http.client and not urllib: a body cut short must surface as a short
        read at once, and urllib's wrapper left the reader waiting out its timeout on a closed
        connection."""
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        parts = urllib.parse.urlsplit(self.base_url)
        connection = http.client.HTTPConnection(parts.hostname, parts.port or 80,
                                                timeout=self.timeout)
        try:
            connection.request(method, parts.path + path, body=data, headers=headers)
            response = connection.getresponse()
        except (http.client.HTTPException, OSError) as exc:
            connection.close()
            raise SglangUnavailable(f"{method} {path}: {exc}") from None
        if response.status >= 400:
            try:
                raw = response.read()
            except (http.client.HTTPException, OSError):
                raw = b""
            finally:
                connection.close()
            raise SglangHTTPError(response.status, _detail(raw), raw.decode("utf-8", "replace"))
        return connection, response

    def _json(self, method: str, path: str, payload=None) -> dict:
        connection, response = self._open(method, path, payload)
        try:
            with response:
                return json.loads(response.read())
        except (http.client.HTTPException, OSError, ValueError) as exc:
            raise SglangUnavailable(f"{method} {path}: {exc}") from None
        finally:
            connection.close()

    def create(self, payload: dict) -> dict:
        return self._json("POST", "/v1/videos", payload)

    def get(self, video_id: str) -> dict:
        return self._json("GET", f"/v1/videos/{video_id}")

    def delete(self, video_id: str) -> None:
        self._json("DELETE", f"/v1/videos/{video_id}")

    def download(self, video_id: str, dest: Path) -> None:
        dest = Path(dest)
        part = dest.with_name(dest.name + ".part")
        connection, response = self._open("GET", f"/v1/videos/{video_id}/content")
        # A queued job's stem may live in a per-job subdirectory that nothing has created yet.
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with response, open(part, "wb") as out:
                expected = response.headers.get("Content-Length")
                shutil.copyfileobj(response, out, 1 << 20)
            if expected is not None and part.stat().st_size != int(expected):
                raise SglangUnavailable(
                    f"download {video_id}: {part.stat().st_size} of {expected} bytes")
        except (http.client.HTTPException, OSError, SglangUnavailable) as exc:
            part.unlink(missing_ok=True)
            if isinstance(exc, SglangUnavailable):
                raise
            raise SglangUnavailable(f"download {video_id}: {exc}") from None
        finally:
            connection.close()
        os.replace(part, dest)


def build_payload(spec) -> dict:
    """spec §4.1.3, after chain_beach.py/runner_clip.py: keyframe first (frame 0), then reference
    pictures in order (they become <Picture 1..>), then audio references as file:// URIs."""
    conditions = []
    if spec.image:
        conditions.append({"type": "image", "uri": spec.image, "role": "keyframe",
                           "frame_index": 0})
    for ref in spec.refs:
        conditions.append({"type": "image", "uri": ref, "role": "reference"})
    for audio in spec.audio:
        conditions.append({"type": "audio", "uri": f"file://{audio}", "role": "reference"})
    return {"model": MODEL, "prompt": spec.prompt, "task": spec.task,
            "conditions": normalize_h3_conditions(conditions),
            "target": {"short_edge": spec.short_edge, "aspect_ratio": spec.aspect_ratio,
                       "duration_seconds": spec.duration},
            "num_outputs_per_prompt": 1, "num_inference_steps": spec.steps,
            "flow_shift": 12.0, "audio_flow_shift": 3.0, "seed": spec.seed,
            "quality": "lossless"}


def _sleep_unless_cancelled(root, job_id: str, seconds: float, sleep) -> str | None:
    """Sleep `seconds` in one-second slices; return the cancel reason as soon as one appears."""
    remaining = seconds
    while True:
        reason = q.cancel_reason(root, job_id)
        if reason or remaining <= 0:
            return reason
        step = min(_CANCEL_SLICE_SECONDS, remaining)
        sleep(step)
        remaining -= step


def _flat_frames(mp4: Path, expected_frames: int) -> list[int]:
    """spec §4.1.7 on sglang (final review C1, coordinator's ruling): the mp4 decodes, holds
    exactly `expected_frames` frames (what was requested, 17n+5), and no frame is filled with one
    colour (`framecheck.is_flat_frame`). Returns the indices of the flat frames. Raises `OSError`
    when the file cannot be decoded or the frame count is off -- either is a failed scene. The MLX
    zero-fill/tile-seam detectors are not run: both are calibrations of the MLX VAE.

    ffmpeg's stderr goes to a file, not a pipe: a pipe nobody reads while stdout is drained fills
    up and stalls the decoder for good."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0:s=x", str(mp4)],
            capture_output=True, text=True, check=True)
        width, height = (int(x) for x in probe.stdout.strip().split("x"))
        size = width * height * 3
        flat: list[int] = []
        index = 0
        with tempfile.TemporaryFile() as errors:
            with subprocess.Popen(
                    ["ffmpeg", "-v", "error", "-i", str(mp4), "-fps_mode", "passthrough", "-f",
                     "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE, stderr=errors) as proc:
                while True:
                    raw = proc.stdout.read(size)
                    if not raw:
                        break
                    if len(raw) != size:
                        raise OSError(f"обрезанный кадр {index}: {len(raw)} из {size} байт")
                    frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
                    if framecheck.is_flat_frame(frame):
                        flat.append(index)
                    index += 1
            errors.seek(0)
            err = errors.read().decode("utf-8", "replace").strip()
        if proc.returncode != 0 or index == 0:
            raise OSError(err or "ни одного кадра")
        if index != expected_frames:
            raise OSError(f"в mp4 {index} кадров, запрошено {expected_frames}")
        return flat
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError) as exc:
        raise OSError(f"не удалось декодировать {mp4}: {exc}") from exc


def _write_report(job, report: dict) -> None:
    q.write_json_durably(Path(job.output_stem + ".json"), report)


def run_generate(job, *, root, outdir, client, gate=None, sleep=time.sleep,
                 clock=time.time) -> tuple[int, str]:
    spec = sglang_args.parse(job.args, check_files=False)
    payload = build_payload(spec)
    log: list[str] = []
    video_id = job.engine_ref

    def done(code: int, line: str, report: dict | None = None) -> tuple[int, str]:
        log.append(line)
        if report is not None:
            _write_report(job, {"engine": "sglang", **report, "payload": payload})
        return code, "".join(log)

    if video_id is None:
        reason = q.cancel_reason(root, job.id)
        if reason:
            return done(1, f"sglang: {reason} — отменена до начала\n",
                        {"status": "cancelled", "id": None, "reason": reason})
        if gate is not None:
            reason = gate(job)
            if reason:
                return done(1, f"sglang: {reason} — сцена не начиналась\n",
                            {"status": "cancelled", "id": None, "reason": reason})
        started = clock()
        try:
            video_id = client.create(payload)["id"]
        except SglangHTTPError as exc:
            return done(1, f"sglang отказал ({exc.status}): {exc.detail}\n",
                        {"status": "rejected", "id": None, "http_status": exc.status,
                         "detail": exc.detail})
        except SglangUnavailable as exc:
            return done(1, f"sglang: H3 недоступен при постановке: {exc}\n",
                        {"status": "unavailable", "id": None, "error": str(exc)})
        q.set_running_fields(root, job.id, engine_ref=video_id, engine_submitted_at=started,
                             wait_reason=None)
        log.append(f"sglang: id={video_id}\n")
    else:
        started = job.engine_submitted_at if job.engine_submitted_at is not None else clock()
        log.append(f"sglang: продолжаю опрос id={video_id} после рестарта\n")

    misses = 0
    completed_at = downloaded_at = None
    while True:
        reason = q.cancel_reason(root, job.id)
        if reason:
            try:
                client.delete(video_id)
            except (SglangHTTPError, SglangUnavailable):
                pass
            return done(1, f"sglang: {reason} — H3 досчитает сцену впустую, следующая задача "
                           f"начнётся после\n",
                        {"status": "cancelled", "id": video_id, "reason": reason})
        try:
            status = client.get(video_id)
            if status.get("status") == "completed":
                completed_at = clock()
                client.download(video_id, Path(job.output_stem + ".mp4"))
                downloaded_at = clock()
            misses = 0
        except SglangHTTPError as exc:
            if exc.status == 404:
                return done(1, f"sglang: задача потеряна при рестарте H3 (id={video_id} "
                               f"неизвестен серверу)\n",
                            {"status": "lost", "id": video_id,
                             "error": "задача потеряна при рестарте H3"})
            if exc.status < 500:
                return done(1, f"sglang отказал ({exc.status}): {exc.detail}\n",
                            {"status": "rejected", "id": video_id, "http_status": exc.status,
                             "detail": exc.detail})
            status = None
        except SglangUnavailable:
            status = None
        if status is None:
            misses += 1
            if misses >= LOST_RETRIES:
                return done(1, f"sglang: H3 пропал — {LOST_RETRIES} попыток через "
                               f"{LOST_RETRY_SECONDS:g} с без ответа\n",
                            {"status": "unavailable", "id": video_id, "error": "H3 пропал"})
            _sleep_unless_cancelled(root, job.id, LOST_RETRY_SECONDS, sleep)
            continue
        if status.get("status") == "completed":
            try:
                bad_frames = _flat_frames(Path(job.output_stem + ".mp4"), spec.frames)
            except OSError as exc:
                return done(1, f"sglang: mp4 не прошёл проверку кадров: {exc}\n",
                            {"status": "corrupt", "id": video_id, "error": str(exc)})
            if bad_frames:
                shown = ", ".join(str(i) for i in bad_frames[:20])
                return done(1, f"sglang: кадры залиты одним цветом: {shown}\n",
                            {"status": "corrupt", "id": video_id, "frames": bad_frames})
            checked_at = clock()
            wall = round(checked_at - started, 1)
            sglang_estimate.record(outdir, width=spec.width, height=spec.height,
                                   frames=spec.frames, wall_s=wall)
            # I7: `wall_s` runs from the POST to a checked mp4; its parts are split out so the
            # battle report can tell the server's time from the download and the frame check.
            # `server_s` is as fine as the poll (20 s): the first GET that saw `completed`.
            return done(0, f"sglang: готово, {wall:.1f} с\n",
                        {"status": "completed", "id": video_id, "wall_s": wall,
                         "inference_time_s": status.get("inference_time_s"),
                         "peak_memory_mb": status.get("peak_memory_mb"),
                         "post_at": round(started, 3), "completed_at": round(completed_at, 3),
                         "server_s": round(completed_at - started, 1),
                         "download_s": round(downloaded_at - completed_at, 1),
                         "framecheck_s": round(checked_at - downloaded_at, 1)})
        if status.get("status") == "failed":
            message = (status.get("error") or {}).get("message") or "без текста"
            return done(1, f"sglang: сцена упала: {message}\n",
                        {"status": "failed", "id": video_id, "error": message})
        _sleep_unless_cancelled(root, job.id, POLL_SECONDS, sleep)
