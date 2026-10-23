"""`studio-worker` (spec §4.2, §8.5): take a queued task, wait for the card, wait until the engine
is ready, submit, follow it, fetch and verify the result, record it — one GPU task at a time.
Every state is written to the task row before the next step, so a crash leaves a state the
reconcile table of slice S5 knows. `submitting` is written before the request, `engine_ref`
right after the answer and before the first poll (И9).

Task kinds plug in (`kinds`): slice S2 has `image` (Krea 2 keyframe), slice S3 adds `render`."""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from pathlib import Path

from h3_48gb.studio import db, events, ids
from h3_48gb.studio.errors import StudioError
from h3_48gb.studio.executors.base import ExecutorError, SubmitUncertain
from h3_48gb.studio.gpu import GpuEngineFailed
from h3_48gb.studio.images import VerifyError
from h3_48gb.studio.plans import last_engine_template
from h3_48gb.studio.tasks import finish_cancelled, queued_in_order

POLL_SECONDS = 2.0
PROBE_SECONDS = 5.0
READY_TIMEOUT_SECONDS = 600.0      # the dispatcher's start_timeout for ComfyUI (§8.1)
CANCEL_WAIT_SECONDS = 30.0         # §8.5: then cancelled with engine_stop=unconfirmed
MAX_STATUS_ERRORS = 30             # consecutive failed status reads before submit_unknown
MAX_ATTEMPTS = 2                   # §8.5: lost -> one automatic retry


class Worker:
    def __init__(self, db_path: Path, *, executor, gate, kinds: dict, idle=None,
                 poll_s: float = POLL_SECONDS, probe_s: float = PROBE_SECONDS,
                 ready_timeout_s: float = READY_TIMEOUT_SECONDS,
                 cancel_wait_s: float = CANCEL_WAIT_SECONDS, clock=time.time, sleep=time.sleep):
        self.conn = db.connect(db_path)
        self.executor = executor
        self.gate = gate
        self.kinds = kinds
        self.idle = idle
        self.poll_s = poll_s
        self.probe_s = probe_s
        self.ready_timeout_s = ready_timeout_s
        self.cancel_wait_s = cancel_wait_s
        self.clock = clock
        self.sleep = sleep
        # the model the engine holds as far as the studio knows — the plan counts from it too
        # (revision 4.1: after a restart the worker and the plan start from the same model)
        self.last_template: str | None = last_engine_template(self.conn)
        self._recovered = False

    # -- loop -------------------------------------------------------------------------------
    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            if not self.run_once():
                stop.wait(self.poll_s)

    def run_once(self) -> bool:
        """Prepare waiting work, take one queued task and run it to its end. False = idle."""
        if not self._recovered:
            self._recovered = True
            if self.recover():
                return True
        task = self._claim()
        if self.idle is not None:
            self.idle.tick(busy=task is not None)
        if task is None:
            return False
        self._execute(task)
        return True

    def _claim(self) -> sqlite3.Row | None:
        with db.tx(self.conn):
            for kind in self.kinds.values():
                kind.prepare(self.conn)
            # §8.4 `queue_order` (revision 4): the same order the plan counts switches by
            rows = queued_in_order(self.conn, kinds=tuple(self.kinds),
                                   loaded_template=self.last_template, limit=1)
            if not rows:
                return None
            row = rows[0]
            self.conn.execute("UPDATE task SET status = 'waiting_gpu', updated_at = ? "
                              "WHERE id = ? AND status = 'queued'", (ids.now_iso(), row["id"]))
            return self._row(row["id"])

    # -- after a restart -------------------------------------------------------------------
    def recover(self) -> int:
        """At start — the worker or its container was restarted mid-task. The rows of the §8.5
        reconcile table that need no guessing: a task waiting for the card or the engine goes
        back to the queue; one cut off while submitting becomes `submit_unknown` (never a blind
        resubmit, Codex-4); one the engine had accepted (`engine_ref` written, И9) is followed
        on from where it was. `find`/`restarted_since` for the rest is slice S5. Returns how
        many tasks were resumed."""
        with db.tx(self.conn):
            for row in self.conn.execute("SELECT * FROM task WHERE status IN "
                                         "('waiting_gpu', 'waiting_ready')").fetchall():
                self._update(row["id"], status="queued", wait_reason=None)
            for row in self.conn.execute("SELECT * FROM task WHERE status = 'submitting'"
                                         ).fetchall():
                self._update(row["id"], status="submit_unknown",
                             wait_reason="Воркер перезапущен во время постановки: не знаю, "
                                         "принял ли ComfyUI задачу")
                events.emit(self.conn, "task_submit_unknown", project_id=row["project_id"],
                            task_id=row["id"], payload={"error": "worker restarted"})
        rows = self.conn.execute(
            "SELECT * FROM task WHERE status IN ('running', 'cancelling', 'downloading', "
            "'verifying') AND engine_ref IS NOT NULL ORDER BY rowid").fetchall()
        for row in rows:
            kind = self.kinds[row["kind"]]
            spec = kind.spec(self.conn, row)
            self.last_template = spec.template
            if row["status"] == "cancelling" or row["cancel_requested_at"] is not None:
                self._stop_engine(row["id"], row["engine_ref"])
                continue
            ref = self._follow(row["id"], kind, spec, row["engine_ref"])
            if ref is not None:
                self._collect(row["id"], kind, spec, ref, row["engine_submitted_at"])
        return len(rows)

    # -- one task ---------------------------------------------------------------------------
    def _execute(self, task: sqlite3.Row) -> None:
        kind = self.kinds[task["kind"]]
        try:
            spec = kind.spec(self.conn, task)
        except (StudioError, ExecutorError, KeyError, ValueError) as err:
            self._fail(task["id"], f"входы задачи не собрались: {err}")
            return
        try:
            cancelled = self.gate.acquire(lambda reason, s: self._wait(task["id"], reason, s))
        except GpuEngineFailed as err:
            self._fail(task["id"], f"движок не поднялся: {err.reason}"
                       + (f" (лог {err.log})" if err.log else ""))
            return
        if cancelled:
            self._cancelled(task["id"], None)
            return
        try:
            self._run_on_engine(task["id"], kind, spec)
        finally:
            self.gate.done()

    def _run_on_engine(self, task_id: str, kind, spec) -> None:
        self._set(task_id, status="waiting_ready", wait_reason=None)
        deadline = self.clock() + self.ready_timeout_s
        while True:
            probe = self.executor.probe(spec)
            if probe["ready"]:
                break
            if self.clock() > deadline:
                self._fail(task_id, f"ComfyUI не готов за {self.ready_timeout_s:g} с: "
                           f"{probe['detail']}")
                return
            if self._wait(task_id, f"ждём ComfyUI: {probe['detail']}", self.probe_s):
                self._cancelled(task_id, None)
                return
        self._set(task_id, status="submitting", wait_reason=None,
                  submit_attempt_at=self.clock())
        version = self.executor.engine_version()
        try:
            ref = self.executor.submit(spec)
        except SubmitUncertain as err:
            self._event_set(task_id, "task_submit_unknown", {"error": str(err)},
                            status="submit_unknown",
                            wait_reason=f"Не знаю, принял ли ComfyUI задачу: {err}")
            return
        except ExecutorError as err:
            self._fail(task_id, str(err))
            return
        submitted = self.clock()
        with db.tx(self.conn):
            self._update(task_id, engine_ref=ref, engine_submitted_at=submitted, status="running",
                         engine_version=version, template_sha256=spec.template_sha256)
            task = self._row(task_id)
            kind.on_running(self.conn, task)
            events.emit(self.conn, "task_running", project_id=task["project_id"],
                        task_id=task_id, payload={"engine_ref": ref})
        self.last_template = spec.template
        ref = self._follow(task_id, kind, spec, ref)
        if ref is None:
            return
        self._collect(task_id, kind, spec, ref, submitted)

    def _collect(self, task_id: str, kind, spec, ref: str, submitted: float) -> None:
        """§8.5 downloading → verifying → succeeded (or failed / cancelled)."""
        self._set(task_id, status="downloading")
        try:
            files = self.executor.result(ref, spec.dest_dir)
        except ExecutorError as err:
            self._fail(task_id, str(err))
            return
        self._set(task_id, status="verifying")
        try:
            with db.tx(self.conn):
                task = self._row(task_id)
                if task["cancel_requested_at"] is not None:      # logical cancel: never attach
                    finish_cancelled(self.conn, task_id, project_id=task["project_id"],
                                     error=None, engine_stop="finished")
                    return
                result = kind.finish(self.conn, task, files, spec, self.clock() - submitted)
                self._update(task_id, status="succeeded", wait_reason=None)
                events.emit(self.conn, "task_succeeded", project_id=task["project_id"],
                            task_id=task_id, payload=result)
        except VerifyError as err:
            self._fail(task_id, f"результат не прошёл проверку: {err}")

    def _follow(self, task_id: str, kind, spec, ref: str) -> str | None:
        """Poll until done; returns the ref to fetch from, or None if the task ended otherwise."""
        errors = 0
        while True:
            task = self._row(task_id)
            if task["cancel_requested_at"] is not None:
                self._stop_engine(task_id, ref)
                return None
            try:
                status = self.executor.status(ref)
                errors = 0
            except ExecutorError as err:
                errors += 1
                if errors < MAX_STATUS_ERRORS:
                    self.sleep(self.poll_s)
                    continue
                # «could not check» is not «gone» (§8.5, Codex-4): keep the ref, ask a human
                self._unknown(task_id, f"ComfyUI не отвечает на проверку задачи: {err}")
                return None
            if status["progress"] is not None:
                step, total = status["progress"]
                if (task["progress_step"], task["progress_total"]) != (step, total):
                    self._event_set(task_id, "task_progress", {"step": step, "total": total},
                                    progress_step=step, progress_total=total)
            state = status["state"]
            if state == "done":
                return ref
            if state == "failed":
                self._fail(task_id, status["error"] or "ошибка исполнителя")
                return None
            if state == "cancelled":
                self._cancelled(task_id, "ComfyUI прервал задачу не по нашей просьбе",
                                engine_stop="confirmed")
                return None
            if state == "lost":
                try:
                    found = self.executor.find(spec) \
                        if self.executor.capabilities["find"] == "exact" else None
                except ExecutorError as err:
                    self._unknown(task_id, f"ComfyUI не знает задачу, а проверить не вышло: {err}")
                    return None
                if found is not None and found != ref:
                    ref = found
                    self._set(task_id, engine_ref=found)
                    continue
                if found is None:
                    self._lost(task_id, kind)
                    return None
            self.sleep(self.poll_s)

    def _stop_engine(self, task_id: str, ref: str) -> None:
        """§8.5 actual cancel: ask the engine, wait up to 30 s for it to confirm."""
        self._set(task_id, status="cancelling")
        try:
            self.executor.cancel(ref)
        except ExecutorError:
            pass
        stop = "unconfirmed"
        # the 30 s run from the first press (`cancel_requested_at`, COALESCE in O14), not from the
        # moment this worker got to it: a repeated press or a restart does not extend them
        asked = self._row(task_id)["cancel_requested_at"]
        deadline = (asked if asked is not None else self.clock()) + self.cancel_wait_s
        while True:
            try:
                state = self.executor.status(ref)["state"]
            except ExecutorError:
                state = None
            # a task cancelled while still queued in the engine is gone from /queue and /history:
            # `lost` is the engine's answer to a cancel, not a loss (coordinator, review of task 5)
            if state in ("cancelled", "lost"):
                stop = "confirmed"
                break
            if state in ("done", "failed"):
                stop = "finished"
                break
            if self.clock() >= deadline:
                break
            self.sleep(self.poll_s)
        self._cancelled(task_id, None, engine_stop=stop)

    # -- endings ----------------------------------------------------------------------------
    def _lost(self, task_id: str, kind) -> None:
        """Nothing in the queue, the history or the files. A retry only with proof that the
        engine was restarted after we submitted (§8.5: the task cannot have survived a process
        that is gone); without proof — `submit_unknown`, the ref kept, a human decides (S5)."""
        task = self._row(task_id)
        restarted = self.executor.restarted_since(task["engine_submitted_at"] or 0.0) \
            if self.executor.capabilities["restart_evidence"] else None
        if restarted is not True:
            self._unknown(task_id, "ComfyUI не знает задачу, а перезапуска движка не было — "
                                   "не повторяю вслепую")
            return
        with db.tx(self.conn):
            task = self._row(task_id)
            if task["attempts"] + 1 < MAX_ATTEMPTS:
                self._update(task_id, status="queued", attempts=task["attempts"] + 1,
                             engine_ref=None, submit_attempt_at=None, engine_submitted_at=None,
                             progress_step=None, progress_total=None, wait_reason=None)
                kind.on_requeued(self.conn, task)
                events.emit(self.conn, "task_requeued", project_id=task["project_id"],
                            task_id=task_id, payload={"reason": "lost"})
                return
        self._fail(task_id, "ComfyUI потерял задачу дважды")

    def _unknown(self, task_id: str, reason: str) -> None:
        """`submit_unknown` with the ref kept (resolution: O27, slice S5)."""
        self._event_set(task_id, "task_submit_unknown", {"error": reason},
                        status="submit_unknown", wait_reason=reason)

    def _fail(self, task_id: str, error: str) -> None:
        with db.tx(self.conn):
            self._update(task_id, status="failed", error=error, wait_reason=None)
            task = self._row(task_id)
            self.kinds[task["kind"]].on_failed(self.conn, task)
            events.emit(self.conn, "task_failed", project_id=task["project_id"],
                        task_id=task_id, payload={"error": error})

    def _cancelled(self, task_id: str, error: str | None, engine_stop: str | None = None):
        with db.tx(self.conn):
            task = self._row(task_id)
            finish_cancelled(self.conn, task_id, project_id=task["project_id"], error=error,
                             engine_stop=engine_stop)

    # -- waiting ----------------------------------------------------------------------------
    def _wait(self, task_id: str, reason: str, seconds: float) -> bool:
        """Show why the task waits, then sleep `seconds` in slices; True if cancelled."""
        task = self._row(task_id)
        if task["wait_reason"] != reason:
            self._event_set(task_id, "task_waiting", {"wait_reason": reason}, wait_reason=reason)
        piece = min(1.0, seconds) or seconds
        for _ in range(max(1, math.ceil(seconds / piece))):
            if self._row(task_id)["cancel_requested_at"] is not None:
                return True
            self.sleep(piece)
        return self._row(task_id)["cancel_requested_at"] is not None

    # -- rows -------------------------------------------------------------------------------
    def _row(self, task_id: str) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()

    def _update(self, task_id: str, **fields) -> None:
        sets = ", ".join(f"{name} = ?" for name in fields)
        self.conn.execute(f"UPDATE task SET {sets}, updated_at = ? WHERE id = ?",
                          (*fields.values(), ids.now_iso(), task_id))

    def _set(self, task_id: str, **fields) -> None:
        with db.tx(self.conn):
            self._update(task_id, **fields)

    def _event_set(self, task_id: str, kind: str, payload: dict, **fields) -> None:
        with db.tx(self.conn):
            self._update(task_id, **fields)
            task = self._row(task_id)
            events.emit(self.conn, kind, project_id=task["project_id"], task_id=task_id,
                        payload=payload)


# -- wiring --------------------------------------------------------------------------------------

STUDIO_WORKER = "studio-worker"


def build_worker(outdir: Path, **overrides) -> Worker:
    """The real worker from the settings (`settings.DEFAULTS` + rows of `setting`)."""
    from h3_48gb.engines.dispatcher_client import DispatcherClient
    from h3_48gb.studio import config, settings
    from h3_48gb.studio.executors.comfy import ComfyExecutor
    from h3_48gb.studio.gpu import DispatcherGate, IdleRelease, LocalGate
    from h3_48gb.studio.images import ImageKind

    home = config.studio_dir(outdir)
    home.mkdir(parents=True, exist_ok=True)
    db_path = home / "studio.sqlite"
    boot = db.connect(db_path)
    try:
        db.migrate(boot)
        conf = settings.all_settings(boot)
    finally:
        boot.close()
    scope = conf["comfy.scope"]
    dispatcher = None
    if scope.startswith("dispatcher:"):
        dispatcher = DispatcherClient(conf["dispatcher.url"], client=STUDIO_WORKER)
        gate = DispatcherGate(dispatcher, scope.split(":", 1)[1])
        idle = IdleRelease(dispatcher, gate)
    elif scope == "local-mac":
        gate, idle = LocalGate(home / "gpu-mac.lock"), None
    else:
        raise ValueError(f"comfy.scope {scope!r}: dispatcher:<engine> или local-mac")
    executor = ComfyExecutor(url=conf["comfy.url"], output_dir=Path(conf["comfy.output_dir"]),
                             scope=scope, dispatcher=dispatcher,
                             engine=scope.split(":", 1)[1] if dispatcher else "local")
    kinds = {"image": ImageKind(home, machine=conf["machine"])}
    return Worker(db_path, executor=executor, gate=gate, kinds=kinds, idle=idle, **overrides)
