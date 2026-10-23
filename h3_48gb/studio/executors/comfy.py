"""The ComfyUI adapter (spec §8.2) — the only executor of part 1: H3 fl2va/ref2va and Krea 2 on
the studio's own ComfyUI (`comfy-studio`, :8189 on alex-neuro).

Identity: the task id travels as `client_id` and `extra_data.studio_task_id` and names the output
folder `studio/<task_id>/` (`filename_prefix`), so the task is found without its `prompt_id`: in
`/queue` and `/history` while the process lives, by its files after a restart (GPU-DAY-20261021
п.5–6). Progress comes over the WebSocket by `prompt_id`; the state itself always from `/history`
and `/queue`, so a dropped socket costs the progress bar, never the result."""
from __future__ import annotations

import json
import re
import shutil
import threading
from pathlib import Path

from h3_48gb.engines.comfy_api import ComfyApi, ComfyError
from h3_48gb.engines.dispatcher_client import DispatcherUnavailable
from h3_48gb.studio import templates
from h3_48gb.studio.executors.base import (Capabilities, EngineStatus, ExecutorError, Probe,
                                           SubmitUncertain, TaskSpec)
from h3_48gb.studio.executors.comfy_template import TemplateError

#: `ltx.py:31`: a /history entry without a final status is tolerated this many reads, then failed.
MAX_ODD_HISTORY_POLLS = 12
FILE_REF = "file:"
#: The socket thread wakes this often to see whether anyone still waits for its prompt.
WS_IDLE_S = 5.0


#: `TaskSpec.task_id` of a probe that is not for any task (O22): every studio task in the queue is
#: then the studio's own, and only entries without a studio task id are foreign.
PROBE_TASK_ID = "probe"


class ComfyExecutor:
    name = "comfy"
    capabilities: Capabilities = {"find": "exact", "restart_evidence": True, "progress": True,
                                  "cancel": "hard", "async_submit": True}

    def __init__(self, *, url: str, output_dir: Path, scope: str, dispatcher=None,
                 engine: str = "comfy-studio", api: ComfyApi | None = None, ws_connect=None):
        self.url = url.rstrip("/")
        self.output_dir = Path(output_dir)
        self.scope = scope
        self.dispatcher = dispatcher          # DispatcherClient, or None on the Mac (local-mac)
        self.engine = engine
        self.api = api or ComfyApi(self.url, timeout=60.0)
        self._ws_connect = ws_connect
        self._progress: dict[str, tuple[int, int]] = {}
        self._odd: dict[str, int] = {}
        self._sockets: dict[str, object] = {}       # task id -> its open progress socket
        self._refs: dict[str, str] = {}             # task id -> prompt_id once ComfyUI accepted it
        self._lock = threading.Lock()

    # -- contract ---------------------------------------------------------------------------
    def gpu_scope(self, spec: TaskSpec) -> str:
        return self.scope

    def estimate_s(self, spec: TaskSpec) -> float:
        return templates.load(spec.template).estimate_s

    def probe(self, spec: TaskSpec) -> Probe:
        try:
            self.api.system_stats()
        except ComfyError as err:
            return {"alive": False, "ready": False, "detail": str(err)}
        template = templates.load(spec.template)
        try:
            known = self.api.object_info()
            missing = [c for c in template.requires["class_types"] if c not in known]
            if missing:
                return {"alive": True, "ready": False,
                        "detail": f"в ComfyUI нет узлов: {', '.join(missing)}"}
            for folder, names in template.requires["models"].items():
                have = set(self.api.models(folder))
                absent = [n for n in names if n not in have]
                if absent:
                    return {"alive": True, "ready": False,
                            "detail": f"в ComfyUI нет моделей {folder}: {', '.join(absent)}"}
            queue = self.api.queue()
        except ComfyError as err:
            return {"alive": True, "ready": False, "detail": str(err)}
        entries = (*queue.get("queue_running", []), *queue.get("queue_pending", []))
        if spec.task_id == PROBE_TASK_ID:
            foreign = [e for e in entries if _studio_task_id(e) is None]
        else:
            foreign = [e for e in entries if _studio_task_id(e) != spec.task_id]
        if foreign:
            return {"alive": True, "ready": False,
                    "detail": f"в очереди ComfyUI чужие задачи: {len(foreign)}"}
        return {"alive": True, "ready": True, "detail": "готов"}

    def submit(self, spec: TaskSpec) -> str:
        template = templates.load(spec.template)
        if spec.template_sha256 is not None and spec.template_sha256 != template.sha256:
            # the graph that would run is not the one recorded in the take (Codex-10)
            raise ExecutorError(f"шаблон {spec.template} изменился после постановки "
                                f"({spec.template_sha256[:12]} → {template.sha256[:12]}) — "
                                f"постройте план заново")
        values = {**spec.values, "task_id": spec.task_id}
        n = 0
        try:
            for name, paths in spec.images.items():
                uploaded = []
                for path in paths:
                    n += 1
                    uploaded.append(self.api.upload_image(
                        Path(path).read_bytes(), f"{spec.task_id}-{n}{Path(path).suffix.lower()}"))
                values[name] = uploaded if template.card["inputs"][name]["type"] == "image_list" \
                    else uploaded[0]
            payload = template.build_payload(values)
        except TemplateError as err:
            raise ExecutorError(f"шаблон {spec.template}: {err}") from None
        except ComfyError as err:
            raise ExecutorError(str(err)) from None
        self._watch(spec.task_id)
        try:
            ref = self.api.submit_prompt(payload)
        except ComfyError as err:
            self._unwatch(spec.task_id)
            status = err.status
            if (status is not None and 400 <= status < 500) or "отклонил" in str(err):
                raise ExecutorError(str(err)) from None        # refused: it never ran
            raise SubmitUncertain(str(err)) from None          # may have reached the engine
        self._refs[spec.task_id] = ref
        return ref

    def engine_version(self) -> str | None:
        """«ComfyUI <ver> · torch <ver> · <GPU>» as `/system_stats` reports it now (spec rev 3.2,
        И15) — recorded with the task at submit. A part the engine does not name is left out;
        None when it does not name its own version or cannot be asked."""
        try:
            stats = self.api.system_stats()
        except ComfyError:
            return None
        system = stats.get("system") or {}
        version = system.get("comfyui_version")
        if not version:
            return None
        parts = [f"ComfyUI {version}"]
        if system.get("pytorch_version"):
            parts.append(f"torch {system['pytorch_version']}")
        devices = stats.get("devices") or []
        if devices and isinstance(devices[0], dict) and devices[0].get("name"):
            parts.append(str(devices[0]["name"]))
        return " · ".join(parts)

    def find(self, spec: TaskSpec) -> str | None:
        """The task's ref, or None when it is provably nowhere. Raises ExecutorError when the
        engine could not be asked and no files say «done» — «could not check» is not «not
        found» (a retry on it would run the task twice, Codex-4)."""
        try:
            queue = self.api.queue()
            for entry in (*queue.get("queue_running", []), *queue.get("queue_pending", [])):
                if _studio_task_id(entry) == spec.task_id:
                    return entry[1]
            for prompt_id, item in self.api.all_history().items():
                if _studio_task_id(item.get("prompt") or []) == spec.task_id:
                    return prompt_id
            unreachable = None
        except ComfyError as err:
            unreachable = err       # the engine is down: the files below may still be there
        if self._files(spec.task_id):
            return FILE_REF + spec.task_id
        if unreachable is not None:
            raise ExecutorError(f"очередь и история ComfyUI недоступны: {unreachable}")
        return None

    def restarted_since(self, t: float) -> bool | None:
        """True when the dispatcher's record of our engine started after `t`, or there is no
        live record at all (then ComfyUI's in-memory queue is gone); None without a dispatcher
        or when it does not answer — never a guess."""
        if self.dispatcher is None:
            return None
        try:
            record = (self.dispatcher.status().get("own") or {}).get(self.engine)
        except DispatcherUnavailable:
            return None
        if record is None:
            return True
        return float(record["started_at"]) > t

    def status(self, ref: str) -> EngineStatus:
        if ref.startswith(FILE_REF):
            return {"state": "done", "progress": None, "error": None}
        try:
            entry = self.api.history(ref)
            if entry is None:
                queue = self.api.queue()
                if any(e[1] == ref for e in queue.get("queue_running", [])):
                    return {"state": "running", "progress": self._progress.get(ref),
                            "error": None}
                if any(e[1] == ref for e in queue.get("queue_pending", [])):
                    return {"state": "queued", "progress": None, "error": None}
                entry = self.api.history(ref)           # it may have finished between the reads
                if entry is None:
                    self._forget(ref)
                    return {"state": "lost", "progress": None, "error": None}
        except ComfyError as err:
            raise ExecutorError(str(err)) from None
        status = entry.get("status") or {}
        messages = status.get("messages") or []
        if status.get("status_str") == "success" and status.get("completed"):
            return {"state": "done", "progress": self._progress.get(ref), "error": None}
        if status.get("status_str") == "error":
            if any(m and m[0] == "execution_interrupted" for m in messages):
                self._forget(ref)
                return {"state": "cancelled", "progress": None, "error": None}
            message = next((m[1].get("exception_message") for m in messages
                            if m and m[0] == "execution_error"), "ошибка исполнения")
            self._forget(ref)
            return {"state": "failed", "progress": None, "error": f"ComfyUI: {message}"}
        with self._lock:
            self._odd[ref] = self._odd.get(ref, 0) + 1
            odd = self._odd[ref]
        if odd >= MAX_ODD_HISTORY_POLLS:
            self._forget(ref)
            return {"state": "failed", "progress": None,
                    "error": f"ComfyUI: запись /history без итогового статуса после {odd} опросов"}
        return {"state": "running", "progress": self._progress.get(ref), "error": None}

    def result(self, ref: str, dest: Path) -> list[Path]:
        if ref.startswith(FILE_REF):
            task_id = ref[len(FILE_REF):]
            sources = self._files(task_id)
        else:
            try:
                entry = self.api.history(ref) or {}
            except ComfyError as err:
                raise ExecutorError(str(err)) from None
            task_id = _studio_task_id(entry.get("prompt") or [])
            if not task_id:
                raise ExecutorError("в записи ComfyUI нет studio_task_id — чужой файл не отдаём")
            sources = []
            for output in (entry.get("outputs") or {}).values():
                for items in output.values():
                    for item in items if isinstance(items, list) else []:
                        if isinstance(item, dict) and "filename" in item:
                            sources.append(self.output_dir / item.get("subfolder", "")
                                           / item["filename"])
        # Only what ComfyUI wrote into this task's own folder: a name from /history is data from
        # outside and may point anywhere (absolute path, `..`, another task's folder).
        home = (self.output_dir / "studio" / task_id).resolve()
        if home.parent != (self.output_dir / "studio").resolve():       # "", ".", "..", "a/b"
            raise ExecutorError(f"файл вне папки задачи {task_id}: недопустимый id задачи")
        for src in sources:
            if not src.resolve().is_relative_to(home):
                raise ExecutorError(f"файл вне папки задачи {task_id}: {src}")
        dest = Path(dest)
        for src in sources:
            if not src.is_file():
                raise ExecutorError(f"ComfyUI назвал файл, которого нет: {src}")
        if not sources:
            raise ExecutorError("ComfyUI не отдал ни одного файла")
        dest.mkdir(parents=True, exist_ok=True)
        copied = []
        for src in sources:
            target = dest / src.name
            shutil.copy2(src, target)
            copied.append(target)
        self._forget(ref)
        return copied

    def cancel(self, ref: str) -> None:
        if ref.startswith(FILE_REF):
            return
        try:
            queue = self.api.queue()
            if any(e[1] == ref for e in queue.get("queue_running", [])):
                self.api.interrupt(ref)          # revision 4: only our prompt (35cd037c)
            elif any(e[1] == ref for e in queue.get("queue_pending", [])):
                self.api.delete_queued([ref])
                self._unwatch_ref(ref)
        except ComfyError as err:
            raise ExecutorError(str(err)) from None

    # -- helpers ----------------------------------------------------------------------------
    def progress(self, ref: str) -> tuple[int, int] | None:
        return self._progress.get(ref)

    def _files(self, task_id: str) -> list[Path]:
        """What this task's folder holds — and, after a retry, only the newest run: ComfyUI counts
        `<prefix>_00001_`, `_00002_`… and the first attempt's files stay beside the second's."""
        folder = self.output_dir / "studio" / task_id
        if not folder.is_dir():
            return []
        newest: dict[tuple, tuple[int, Path]] = {}
        loose = []
        for path in sorted(p for p in folder.glob("*") if p.is_file()):
            m = _COUNTED.match(path.name)
            if m is None:
                loose.append(path)
                continue
            key = (m["prefix"], m["rest"])
            if key not in newest or int(m["n"]) > newest[key][0]:
                newest[key] = (int(m["n"]), path)
        return sorted([*loose, *(p for _, p in newest.values())])

    def _forget(self, ref: str) -> None:
        with self._lock:
            self._progress.pop(ref, None)
            self._odd.pop(ref, None)

    def _unwatch(self, task_id: str) -> None:
        """Close the progress socket of a task that will not run (refused, taken off the queue)."""
        with self._lock:
            socket = self._sockets.pop(task_id, None)
            self._refs.pop(task_id, None)
        if socket is not None:
            try:
                socket.close()
            except Exception:                 # noqa: BLE001
                pass

    def _unwatch_ref(self, ref: str) -> None:
        for task_id, known in list(self._refs.items()):
            if known == ref:
                self._unwatch(task_id)

    def _alive(self, task_id: str) -> bool:
        """Does anyone still wait for this task's prompt: it is queued or in the history. When
        the engine cannot be asked, assume yes — the socket is never dropped on a guess."""
        ref = self._refs.get(task_id)
        if ref is None:
            return task_id in self._sockets
        try:
            queue = self.api.queue()
            if any(e[1] == ref for e in (*queue.get("queue_running", []),
                                         *queue.get("queue_pending", []))):
                return True
            return self.api.history(ref) is not None
        except ComfyError:
            return True

    def _watch(self, task_id: str) -> None:
        """Follow `progress` over the socket for this client id; best effort. The socket is
        kept in `self._sockets` so a task that will not run can close it, and the thread leaves
        by itself when its prompt is nowhere any more."""
        connect = self._ws_connect
        if connect is None:
            from websockets.sync.client import connect
        ws_url = "ws" + self.url[len("http"):] + f"/ws?clientId={task_id}"
        try:
            # proxy=None: ComfyUI is on the loopback/LAN; websockets would otherwise follow a
            # SOCKS/HTTP proxy from the environment and fail to connect at all.
            socket = connect(ws_url, open_timeout=5, proxy=None)
        except Exception:                     # noqa: BLE001 -- no socket, no progress bar
            return
        with self._lock:
            self._sockets[task_id] = socket

        def follow():
            try:
                while True:
                    try:
                        raw = socket.recv(timeout=WS_IDLE_S)
                    except TimeoutError:
                        if self._sockets.get(task_id) is not socket or not self._alive(task_id):
                            break
                        continue
                    if not isinstance(raw, str):
                        continue
                    message = json.loads(raw)
                    data = message.get("data") or {}
                    if message.get("type") == "progress" and data.get("prompt_id"):
                        with self._lock:
                            self._progress[data["prompt_id"]] = (int(data["value"]),
                                                                 int(data["max"]))
                    if message.get("type") == "executing" and data.get("node") is None \
                            and data.get("prompt_id"):
                        break
                    if message.get("type") in ("execution_error", "execution_interrupted"):
                        break
            except Exception:                 # noqa: BLE001 -- the socket dropped or was closed
                pass
            finally:
                with self._lock:
                    if self._sockets.get(task_id) is socket:
                        del self._sockets[task_id]
                try:
                    socket.close()
                except Exception:             # noqa: BLE001
                    pass

        threading.Thread(target=follow, daemon=True, name=f"comfy-ws-{task_id}").start()


#: ComfyUI's SaveImage/SaveVideo names: `<prefix>_<counter>_<rest>`
_COUNTED = re.compile(r"^(?P<prefix>.*?)_(?P<n>\d{5})_?(?P<rest>.*)$")


def _studio_task_id(entry) -> str | None:
    """`/queue` entries and `/history[*].prompt` are `[number, prompt_id, graph, extra_data, …]`."""
    try:
        return (entry[3] or {}).get("studio_task_id")
    except (IndexError, TypeError, AttributeError):
        return None
