"""The executor contract (spec §8.1). An executor never reads the database and knows nothing of
scenes: it gets a `TaskSpec` (template, values, files to upload, where results go) and answers in
the terms below. The contract test checks every capability an adapter declares, and only those."""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict


class Capabilities(TypedDict):
    find: Literal["exact", "none"]           # can find our task without engine_ref
    restart_evidence: bool                   # can tell the engine restarted after a moment t
    progress: bool                           # (step, total) while running
    cancel: Literal["hard", "soft", "none"]  # hard stops the computation; soft only drops it
    async_submit: bool                       # submit returns a ref quickly, the work goes on


class Probe(TypedDict):
    alive: bool
    ready: bool          # the spec's template and models are there and no foreign task runs
    detail: str


class EngineStatus(TypedDict):
    state: Literal["queued", "running", "done", "failed", "cancelled", "lost"]
    progress: tuple[int, int] | None
    error: str | None


class TaskSpec(BaseModel):
    """What the worker hands an executor: built from the task's payload (an image task) or the
    take's materialized `inputs_json` (a render), plus the task id and the result folder.
    `values` are the template's scalar inputs (prompt, seed, steps, width, height, length…);
    `images` maps an image input of the template to the files to upload, in order."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    template: str
    #: the template version the task was planned with; the executor refuses another one
    template_sha256: str | None = None
    values: dict
    images: dict[str, list[Path]] = {}
    dest_dir: Path


class ExecutorError(Exception):
    """The engine refused or failed in a way the worker reports as the task's error."""


class SubmitUncertain(ExecutorError):
    """The request may or may not have reached the engine (timeout, broken connection after the
    body was sent): the task becomes `submit_unknown` (§8.5; resolved in slice S5)."""


class Executor(Protocol):
    name: str
    capabilities: Capabilities

    def gpu_scope(self, spec: TaskSpec) -> str: ...
    def probe(self, spec: TaskSpec) -> Probe: ...
    def estimate_s(self, spec: TaskSpec) -> float: ...
    def submit(self, spec: TaskSpec) -> str: ...
    def engine_version(self) -> str | None: ...              # recorded with the task at submit
    def find(self, spec: TaskSpec) -> str | None: ...
    def restarted_since(self, t: float) -> bool | None: ...
    def status(self, ref: str) -> EngineStatus: ...
    def result(self, ref: str, dest: Path) -> list[Path]: ...
    def cancel(self, ref: str) -> None: ...
