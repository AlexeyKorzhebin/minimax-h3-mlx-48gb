"""Command line entry point.

Every subcommand takes explicit flags and can emit JSON, so an MCP server can
drive this without parsing human-readable output.

Failures are machine-readable everywhere, not just in the happy path's JSON report. Every
refusal this module raises is a :class:`CliError`: a stable ``code`` plus a human sentence plus
optional structured ``detail``. Under ``--json`` it is rendered as
``{"ok": false, "error": {"code", "message", "detail"}}`` on stdout with a non-zero exit; without
``--json`` only the sentence goes to stderr, exactly as a plain ``SystemExit`` always has —
:class:`CliError` *is* a ``SystemExit`` subclass, so it costs existing call sites nothing and
degrades gracefully anywhere it is not specifically caught. An MCP wrapper matches on
``error.code``, which is why every code this CLI can produce is listed once, in :data:`ERROR_CODES`,
rather than being coined ad hoc at each call site.

This intentionally does not extend to *every* exception a subcommand might raise — an OOM in the
VAE decode or a missing ``ffmpeg`` binary is a bug or an environment problem, not a validation
refusal, and turning it into a tidy code would hide it. ``main`` still guarantees valid JSON on
stdout for those too (as ``internal_error``), because a broken machine-readable contract is worse
than an ungrouped code, but it does not attempt to classify them further.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

DEFAULT_CHECKPOINT = Path.home() / "models/h3-converted"
#: Where clips land. Deliberately not under the weights directory: output is disposable and
#: models are 46 GB you do not want to delete by accident while clearing space. `H3_OUTDIR`
#: overrides it, so a permanent choice does not need a flag on every command.
DEFAULT_OUTDIR = Path(os.environ.get("H3_OUTDIR") or Path.home() / "video-out")
#: The grid this fork shipped against. Only a fallback now: the real number is read from the
#: checkpoint's own AdaLN cache, since a cache can be baked for any grid (see `baked_grid_points`).
BAKED_GRID_POINTS = 31


#: safetensors headers are JSON and small — the largest table here is under 64 KB of header.
#: A junk file read as one yields an absurd length prefix, and `fh.read(that)` raises MemoryError
#: rather than returning garbage, which no reasonable `except` clause on a parser would list.
#: Bounding the read turns a crash into the `None` every caller already handles.
_MAX_HEADER_BYTES = 8 << 20


def _grid_points_from_header(path: Path) -> int | None:
    """Grid points in a safetensors AdaLN table, or None if it cannot be read as one."""
    import json
    import struct

    try:
        size = Path(path).stat().st_size
        with open(path, "rb") as fh:
            length = struct.unpack("<Q", fh.read(8))[0]
            if length > min(_MAX_HEADER_BYTES, size):
                return None
            header = json.loads(fh.read(length))
        return int(header["video_sigmas"]["shape"][0])
    except (OSError, ValueError, KeyError, struct.error):
        return None


def _grid_points_of(cache: Path) -> int | None:
    """Grid points in a standalone AdaLN table, read from its safetensors header."""
    import json
    import struct

    return _grid_points_from_header(cache)


def baked_grid_points(checkpoint: Path) -> int | None:
    """How many grid points this checkpoint's AdaLN cache covers, or None if it has none.

    Reads the safetensors header directly — a few hundred bytes, no MLX — because this runs inside
    `RunSpec.__post_init__`, which must stay importable without pulling in the MLX stack.

    The count used to be the hardcoded 31 that mere.run's cache happened to carry. That was wrong
    the moment a cache could be baked for another grid: the refusal quoted a number belonging to a
    different checkpoint than the one the user passed.
    """
    import json
    import struct

    return _grid_points_from_header(Path(checkpoint) / "transformer/adaln_cache.safetensors")

#: Every machine-readable failure code this CLI can raise under `--json`. The whole contract an
#: MCP wrapper needs is here: match on `error.code`, never on `error.message` — the sentence can
#: be reworded for clarity at any time; the code cannot, without a deliberate, documented break.
ERROR_CODES = {
    "geometry_not_multiple_of_32": "--width or --height is not a multiple of 32; the port cannot pack it",
    "schedule_not_baked": "--steps does not equal the one grid size the baked AdaLN table covers",
    "checkpoint_not_found": "`resume` was asked for, but no checkpoint matches this run's identity",
    "checkpoint_mismatch": "a checkpoint exists but was written for a different request or model",
    "checkpoint_corrupt": "a checkpoint exists but could not be read",
    "checkpoint_locked": "another process already holds the lock for this checkpoint",
    "preview_interval_negative": "--preview-every is negative; 0 disables previews, N > 0 sets a cadence",
    "end_image_without_image": "--end-image was given without --image; the end frame anchors a run that must also have a start frame",
    "image_not_found": "a keyframe path does not exist",
    "image_unreadable": "a keyframe exists but could not be decoded as an image",
    "image_aspect_unsupported": "a keyframe's aspect ratio is outside the 1:4..4:1 the model supports",
    "partial_canvas_with_image": "--image was given with only one of --width/--height",
    "lora_not_found": "--turbo-lora points at a file that does not exist",
    "turbo_strength_invalid": "--turbo-strength is not a finite number",
    "adaln_cache_unreadable": "--adaln-cache exists but is not a readable AdaLN table",
    "checkpoint_without_adaln": "the checkpoint has no readable transformer/adaln_cache.safetensors and no --adaln-cache was given; this build cannot build the table itself",
    "upstream_patch_missing": "a keyframe was given but the vendored upstream/ checkout is unpatched",
    "outdir_not_found": "--outdir does not exist or cannot be read",
    "prompt_file_not_found": "--prompt-file points at a file that does not exist",
    "prompt_file_unreadable": "--prompt-file exists but could not be read as UTF-8",
    "prompt_file_empty": "--prompt-file is empty once trailing newlines are stripped",
    "prompt_both_given": "both a positional prompt and --prompt-file were given; pass exactly one",
    "prompt_missing": "no prompt: pass one positionally or with --prompt-file",
    "mode_mismatch": "--mode contradicts the --image/--end-image flags given",
    "worker_already_running": "another worker already holds queue/worker.lock on this machine",
    # Raised by `h3_48gb.web`, listed here because the contract is one contract: the CLI, the
    # worker and the server all answer with the same `{"ok": false, "error": {"code", ...}}`
    # envelope, and a caller matching on `error.code` should not have to know which process
    # produced it. See `test_error_codes_are_documented_in_one_place`, which scans both modules.
    "args_invalid": "`h3 generate` will not accept this argument list; `detail.stderr` carries argparse's own sentence",
    "command_not_allowed": "only `h3 generate` may be queued, and never with --no-checkpoint",
    "output_stem_conflict": "another pending or running job, or a file already on disk, claims that output name",
    "job_not_pending": "the job is no longer in pending/: the worker claimed it, or it was already removed",
    "path_outside_root": "a path names something outside every root the server may touch, or writes into the read-only models root",
    "prompt_name_invalid": "a prompt name is not a bare `[A-Za-z0-9_-]+.txt` -- it names a directory or another suffix",
    "queue_unwritable": "the queue directory could not be read or written; see `detail.path`",
    "media_type_not_allowed": "/media serves only finished clips and preview frames, and that is not one",
    "range_not_satisfiable": "the request's Range header names bytes outside the file `/media` resolved; `detail.total` is the file's real length",
    # The chat prompt editor. The first three are the server's own refusals; the last three are
    # raised inside `h3_48gb.provider` and reach the wire unchanged, because a failure of the model
    # is not a failure of this server and a page that has to tell them apart matches on the code.
    "chat_not_found": "there is no chat session with that id under `<outdir>/chat/`",
    "chat_busy": "a turn of this session is already in flight; one at a time, so neither erases the other",
    "chat_corrupt": "the session file under `<outdir>/chat/` is not a session -- hand-edited, or truncated; `detail.path` names it",
    "bad_image": "the keyframe is not one of the image types a chat turn may attach, or is over the size limit",
    "provider_unavailable": "the chat provider is missing or unusable; `detail.provider` names it and the message says why",
    "gpu_busy": "a generation is running, so the local chat model is not raised: it would want the same 31 GB",
    "llama_did_not_start": "llama-server was spawned but never answered /health; the log tail is in the message",
    "chat_unreachable": "the chat provider did not answer at all -- not started, crashed, or the wrong address",
    "bad_model_json": "the chat model would not hold the answer schema, twice in a row",
    "bad_provider_reply": "the chat provider answered 200 with something that is not a completion -- OpenRouter's own `{\"error\": ...}` body, or a proxy's page; the body's first 400 characters are in the message",
    # Router-level refusals: produced by `web._router_code` (and by the standard library's own
    # error path behind it) rather than by a `raise CliError(...)`, which is why the contract test
    # reads `web.ROUTER_CODES` instead of only scanning source for raise sites. They belong in this
    # dict all the same: the contract the design spec fixes is the one on the wire, and a page
    # turning a `code` into a Russian sentence meets these two more often than any other.
    "host_not_allowed": "the request's Host header is not this server's own address (DNS rebinding)",
    "origin_not_allowed": "a write did not come from this server's own page: its Origin or Sec-Fetch-Site names another site (cross-site request forgery)",
    "not_found": "no route, or no file, at that URL",
    "method_not_implemented": "this server has no handler for that HTTP method",
    "bad_request": "the request itself could not be served -- malformed, or too large to accept",
    "internal_error": "an unexpected exception reached the CLI boundary; see `detail` for its type",
}


class CliError(SystemExit):
    """A refusal with a stable code, alongside the human sentence `SystemExit` already carried.

    Subclassing `SystemExit` rather than `Exception` keeps every existing call site's contract
    unchanged: uncaught, it still prints its message to stderr and exits non-zero, exactly like
    the plain `raise SystemExit(...)` this replaces. `main` additionally catches it *by type* so
    `--json` mode can render `to_dict()` instead of relying on a caller to string-match the
    message — which is the whole problem this class exists to retire.
    """

    def __init__(self, code: str, message: str, detail: dict | None = None):
        assert code in ERROR_CODES, f"{code!r} is not listed in ERROR_CODES"
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = dict(detail) if detail else {}

    def to_dict(self) -> dict:
        return {"ok": False, "error": {"code": self.code, "message": self.message, "detail": self.detail}}


#: Directories a converted checkpoint must have, and the one file inside `transformer/` this
#: build cannot do without — see `h3_48gb.adaln`, which reimplements nothing without it.
REQUIRED_COMPONENTS = ("transformer", "text_encoder", "video_vae", "audio_vae")


@dataclass(frozen=True)
class RunSpec:
    prompt: str
    width: int
    height: int
    duration: float
    steps: int
    seed: int
    checkpoint: Path
    outdir: Path
    tag: str
    #: Where the *resume* checkpoint lives (`checkpoint` above is the converted **weights**).
    #: `None` means the default, `<outdir>/checkpoints`, resolved by `resume_checkpoint_dir`
    #: rather than here so the dataclass keeps a static default and stays comparable.
    checkpoint_dir: Path | None = None
    #: Disable checkpointing entirely. A crash then costs the whole run, so this exists for
    #: read-only filesystems and throwaway runs, not as an everyday flag.
    no_checkpoint: bool = False
    #: Conditioning keyframes. One image anchors the clip's first frame; adding `end_image`
    #: makes it interpolate to a given last frame. The checkpoint was trained on exactly these
    #: two arrangements, so the flags deliberately cannot express a third.
    image: Path | None = None
    end_image: Path | None = None
    #: Decode a preview JPEG every N steps; 0 disables previews. On by default since TAE made a
    #: preview cost 0.125 s instead of 49.3 — see `preview_decoder`.
    preview_every: int = 5
    #: Prefix for `<stem>-preview-stepNN.jpg`; `None` means the run's own output stem.
    preview_stem: Path | None = None
    #: Which decoder in-flight previews use. The real VAE is correct but costs 49.3 s and 5.21 GB
    #: per preview; `tae` is an approximation for watching progress and never for the delivered
    #: clip; `latent` is the VAE-free heat map.
    preview_decoder: str = "tae"
    #: A Turbo LoRA to apply at run time, restoring the motion that few-step sampling loses.
    #: 1.0 is the author's own recommendation and, at the default canvas, the measured optimum on
    #: both axes: motion 113% of the 31-step reference (8 steps alone manage 52%), and detail equal
    #: to a 16-step run for the price of an 8-step one.
    #:
    #: This used to default to 0.45, because at 512x512 strength 1.0 measured 213% motion — a clear
    #: overshoot. That number was real and is now irrelevant: it belongs to a canvas this fork no
    #: longer defaults to, and it did not survive the move. Retiring a measurement when its
    #: conditions change is the point; carrying 0.45 forward would have cost half the motion.
    turbo_lora: Path | None = None
    turbo_strength: float = 1.0
    #: An AdaLN table baked for a grid other than the checkpoint's own — this is what makes
    #: `--steps 8` possible. `scripts/bake_adaln.py` produces one.
    adaln_cache: Path | None = None
    #: The file `prompt` was read from, when it came from `--prompt-file` rather than the
    #: positional argument; `None` otherwise. Record-only, like `preview_every`: it is never
    #: passed to `pipe(...)` (only the resolved `prompt` text is), so it never reaches
    #: `request_identity` and does not enter `identity_digest` — resuming a run started with
    #: `--prompt-file prompts/x.txt` and one started with the positional argument for the same
    #: text must land on the same checkpoint.
    prompt_file: str | None = None

    def __post_init__(self) -> None:
        """Every refusal that depends only on the request, checked once, at construction.

        `--steps` used to be validated inside `run_generate`, which `resume` reaches only *after*
        computing the checkpoint path — so `h3 resume "..." --steps 20` reported
        `checkpoint_not_found` and blamed a missing file for what is a schedule error. Validating
        here instead means no `RunSpec` can exist that names a run this build cannot serve, and
        both subcommands report the same first cause in the same order.
        """
        for name in ("width", "height"):
            value = getattr(self, name)
            if value % 32:
                raise CliError(
                    "geometry_not_multiple_of_32",
                    f"--{name} must be a multiple of 32, got {value}",
                    {name: value},
                )
        # Which grid this run is allowed to ask for. `--adaln-cache` wins when it is given (its own
        # readability is checked below, with a refusal that names the flag); otherwise the
        # checkpoint's own baked table answers.
        #
        # **A checkpoint with no readable table is a refusal, not a fallback to 31.** That fallback
        # cost a run: a broken symlink at `transformer/adaln_cache.safetensors` pointed at a file
        # that was never baked, `baked_grid_points` returned `None` exactly as it does for "no cache
        # here", `BAKED_GRID_POINTS` accepted the default `--steps 31`, and the failure arrived
        # after 21 GB of weights had been loaded -- `ModulationCache.build` reads `time_embedder`
        # and every block's `adaln_proj`, and this build ships neither, so there is no path where
        # such a run can finish. The number 31 is only ever *right* for a checkpoint that has the
        # table; when there is none, quoting it invents a grid nobody baked and turns a five-second
        # refusal into a five-minute one.
        #
        # `--adaln-cache` is the way out, and the message says so: a table baked for another grid
        # (`scripts/bake_adaln.py`) makes the checkpoint's own missing one irrelevant.
        if self.adaln_cache:
            required = _grid_points_of(self.adaln_cache) or BAKED_GRID_POINTS
        else:
            required = baked_grid_points(self.checkpoint)
            if required is None:
                raise CliError(
                    "checkpoint_without_adaln",
                    f"у чекпойнта нет читаемой таблицы AdaLN — передай --adaln-cache или почини "
                    f"{Path(self.checkpoint) / 'transformer/adaln_cache.safetensors'}",
                    {"checkpoint": str(self.checkpoint),
                     "cache": str(Path(self.checkpoint) / "transformer/adaln_cache.safetensors")},
                )
        if self.steps != required:
            raise CliError(
                "schedule_not_baked",
                f"steps must be {required} (this checkpoint's AdaLN cache covers only that grid), "
                f"got {self.steps}",
                {"steps": self.steps, "required": required},
            )
        if self.preview_every < 0:
            raise CliError(
                "preview_interval_negative",
                f"--preview-every must be >= 0 (0 disables previews), got {self.preview_every}",
                {"preview_every": self.preview_every},
            )
        if self.end_image is not None and self.image is None:
            raise CliError("end_image_without_image",
                           "--end-image needs --image: the end frame is the far anchor of a "
                           "run whose near anchor is the start frame.")
        import math

        if self.turbo_lora is not None and not Path(self.turbo_lora).exists():
            raise CliError("lora_not_found", f"--turbo-lora does not exist: {self.turbo_lora}",
                           {"path": str(self.turbo_lora)})
        if not math.isfinite(self.turbo_strength):
            raise CliError("turbo_strength_invalid",
                           f"--turbo-strength must be a finite number, got {self.turbo_strength}",
                           {"turbo_strength": self.turbo_strength})
        if self.adaln_cache is not None and _grid_points_of(self.adaln_cache) is None:
            # Falling back to the checkpoint's grid here would accept `--steps 31` and then hand
            # the pipeline an unreadable table — a failure 28 GB into the run instead of now.
            raise CliError("adaln_cache_unreadable",
                           f"--adaln-cache could not be read as an AdaLN table: {self.adaln_cache}",
                           {"path": str(self.adaln_cache)})

        for label, path in (("--image", self.image), ("--end-image", self.end_image)):
            if path is not None and not Path(path).exists():
                raise CliError("image_not_found", f"{label} does not exist: {path}",
                               {"flag": label, "path": str(path)})

    def output_stem(self) -> Path:
        return self.outdir / f"h3-{self.tag}-{self.width}x{self.height}"

    def resume_checkpoint_dir(self) -> Path | None:
        """Directory the resume checkpoint belongs in, or `None` when checkpointing is off."""
        if self.no_checkpoint:
            return None
        return self.checkpoint_dir if self.checkpoint_dir is not None else self.outdir / "checkpoints"


def _add_run_flags(sub: argparse.ArgumentParser) -> None:
    """The flags `generate` and `resume` share — every one of them identifies or locates a run."""
    # `nargs="?"` rather than required: a caller now may pass the prompt via `--prompt-file`
    # instead, and `resolve_prompt` (called from `spec_from_args`) is what actually requires
    # exactly one of the two.
    sub.add_argument("prompt", nargs="?", default=None)
    sub.add_argument("--prompt-file", type=Path, default=None,
                     help="read the prompt from a file instead of the positional argument")
    # `None` rather than the default canvas so `spec_from_args` can tell "the caller wants the
    # default" from "the caller asked for exactly that size" — with a keyframe, the default
    # comes from the frame instead. See `DEFAULT_CANVAS`.
    sub.add_argument("--width", type=int, default=None,
                     help="canvas width (default: 896, or derived from --image)")
    sub.add_argument("--height", type=int, default=None,
                     help="canvas height (default: 512, or derived from --image)")
    sub.add_argument("--duration", type=float, default=5.0)
    sub.add_argument("--steps", type=int, default=BAKED_GRID_POINTS)
    sub.add_argument("--seed", type=int, default=0)
    sub.add_argument("--tag", default="run")
    sub.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT,
                     help="the converted model weights (see `h3 doctor`)")
    sub.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    sub.add_argument("--checkpoint-dir", type=Path, default=None,
                     help="where the resume checkpoint lives (default: <outdir>/checkpoints)")
    sub.add_argument("--image", type=Path, default=None,
                     help="condition the first frame on this image")
    sub.add_argument("--end-image", type=Path, default=None,
                     help="also condition the last frame; requires --image")
    # This sets nothing: the mode is fully determined by --image/--end-image (see `check_mode`
    # below `resolve_prompt`). It exists so a typo in a filename or a flag is refused in the
    # first second, rather than discovered an hour later by watching the finished clip.
    sub.add_argument("--mode", choices=("t2v", "t2va", "i2v", "flf"), default=None,
                     help="assert the mode the image flags imply, refusing before any weight loads")
    # Previews are what makes a multi-hour render watchable, and they stopped being expensive: TAE
    # decodes one in 0.125 s against the real VAE's 49.3 s at 1344x768, so six previews now cost
    # 0.75 s of a run measured in hours. Off by default made sense at 49 s a frame; it does not now.
    sub.add_argument("--preview-every", type=int, default=5, metavar="N",
                     help="decode a preview JPEG every N steps (default: 5); 0 disables previews")
    sub.add_argument("--preview-stem", type=Path, default=None,
                     help="prefix for <stem>-preview-stepNN.jpg (default: the run's output stem)")
    # `tae` by default *because* previews are on by default: at 49.3 s each the real VAE would add
    # five minutes to every run. `vae` remains available for a preview that must be exact — TAE is
    # an approximation for watching progress, never for the delivered clip. Without the weights,
    # `tae` falls back to the VAE-free latent heat map, so this default costs nothing to a reader
    # who never downloads them.
    # Long runs must go through this CLI rather than a throwaway script, because this is where
    # checkpointing lives: a 2h14m run driven by a bare `pipe(...)` call has no resume point and
    # is lost entirely if the process dies. That happened once; hence these flags.
    sub.add_argument("--adaln-cache", type=Path, default=None,
                     help="AdaLN table baked for a different step count (scripts/bake_adaln.py)")
    sub.add_argument("--turbo-lora", type=Path, default=None,
                     help="apply a Turbo LoRA at run time (pairs with a few-step --steps)")
    sub.add_argument("--turbo-strength", type=float, default=1.0,
                     help="LoRA strength (default 1.0; lower it toward 0.8 if the image over-sharpens)")
    sub.add_argument("--preview-decoder", choices=("vae", "tae", "latent"), default="tae",
                     help="decoder for in-flight previews (default: tae, ~400x faster than vae)")
    sub.add_argument("--json", action="store_true", help="emit a machine-readable report")


def _subcommand(sub, name: str, help: str) -> argparse.ArgumentParser:
    """A subparser that requires flags to be spelled in full.

    `allow_abbrev=False` is the point. With argparse's default, `--prompt` is an unambiguous
    abbreviation of `--prompt-file` — so `h3 generate --prompt "a dog"` passes the prompt text to
    the file reader and fails with "no such file", naming the prompt as the missing path.
    `run_bench.py` spells its flag exactly `--prompt`, so this is a command someone will type.
    Refusing every abbreviation is better than adding an alias: it fails on the flag rather than
    on a file, and it removes the same trap from every future flag pair.
    """
    return sub.add_parser(name, help=help, allow_abbrev=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="h3", description="MiniMax H3 on a 48 GB Mac",
                                     allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = _subcommand(sub, "generate", help="generate a clip")
    _add_run_flags(gen)
    # `--restart` is the recovery path for this CLI's own hardest refusal, not a convenience.
    # `checkpoint_mismatch` is a hard stop, and without this flag the only way out is to find and
    # delete a file named `h3-{digest}.safetensors` whose digest the user has no way to compute.
    gen.add_argument("--restart", action="store_true",
                     help="ignore any existing checkpoint and start from step 0 "
                          "(the way out of a checkpoint_mismatch refusal)")
    gen.add_argument("--no-checkpoint", action="store_true",
                     help="do not write a resume checkpoint at all; a crash then costs the whole run")
    # Runs `spec_from_args` -- so every refusal a real run would raise (bad geometry, an
    # unbaked schedule, a missing keyframe, ...) still fires here, with the same code -- but
    # returns before `run_generate` touches a checkpoint or loads any weights. This is what lets
    # a caller validate a request through `RunSpec.__post_init__`'s one set of rules without
    # importing MLX itself: only `generate` (not `resume`) needs it, since a caller validating a
    # request before it exists has no resume target to speak of.
    gen.add_argument("--dry-run", action="store_true",
                     help="report what would run, without loading weights or writing anything")

    res = _subcommand(sub, "resume", help="continue an interrupted run")
    # No `--restart` / `--no-checkpoint` here on purpose: `resume` exists precisely to *assert*
    # that a run is being continued, so a flag that turns it into a fresh start would defeat it.
    _add_run_flags(res)

    lst = _subcommand(sub, "list", help="list finished runs")
    lst.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    lst.add_argument("--json", action="store_true", help="emit a machine-readable report")

    st = _subcommand(sub, "status", help="what is running under an outdir, and how far along")
    st.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    st.add_argument("--json", action="store_true", help="emit a machine-readable report")

    wt = _subcommand(sub, "watch", help="redraw a run's progress until nothing is running")
    wt.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    wt.add_argument("--interval", type=float, default=20.0,
                    help="seconds between redraws (default 20); 0 renders once and exits")

    wk = _subcommand(sub, "worker", help="run queued jobs, one at a time, until stopped")
    wk.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    wk.add_argument("--poll", type=float, default=5.0,
                    help="seconds between checks of an empty queue (default 5)")
    wk.add_argument("--json", action="store_true", help="emit a machine-readable report")

    # No `--host`: the server binds the loopback and only the loopback (`web.LOOPBACK`), and a flag
    # that could hold `0.0.0.0` is a flag someone eventually sets on a machine with no auth in
    # front of it.
    wb = _subcommand(sub, "web", help="serve the queue page on 127.0.0.1 until stopped")
    wb.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    wb.add_argument("--port", type=int, default=8765,
                    help="port on 127.0.0.1 (default 8765); 0 asks the kernel for a free one")

    doc = _subcommand(sub, "doctor", help="verify a converted checkpoint")
    doc.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    doc.add_argument("--json", action="store_true", help="emit a machine-readable report")
    return parser


#: The canvas a text-only request gets. H3's released geometry is 1344x768 and this is not it: at
#: 8 steps that canvas is 31 minutes of diffusion for 2.4 seconds of video, which is the wrong
#: thing to hand someone who did not ask for a size. 896x512 keeps the released 16:9 shape, costs
#: 12.5 minutes, and sits above the resolution where faces start failing — measured over three
#: seeds, 768x768 beat 512x512 on both the mean and, more importantly, a third of the spread, and
#: 896x512 is in that class. Ask for `--width 1344 --height 768` when the render is worth the hour.
DEFAULT_CANVAS = (896, 512)


def resolve_canvas(image: Path | None, width: int | None, height: int | None) -> tuple[int, int]:
    """Decide the canvas, deriving it from the keyframe when the caller did not say.

    A keyframe is the geometry anchor — the reference resolves the canvas from the first frame's
    aspect (`reference/diffusers/modular/before_encoder.py:173`), and for good reason: the first
    keyframe is *stretched* onto whatever canvas it lands on, without preserving aspect. Left at
    the 16:9 default, a portrait photograph is silently squashed into a landscape clip.

    EXIF orientation is applied before the size is read. A camera stores rotation as a tag rather
    than rotating pixels, so a portrait photo reports itself as landscape — and would pick exactly
    the canvas this function exists to avoid.
    """
    if width is not None and height is not None:
        return width, height
    if image is None:
        default_width, default_height = DEFAULT_CANVAS
        return width if width is not None else default_width, \
            height if height is not None else default_height

    # Half a canvas is worse than none: `--width 640` alone against a 3:2 photo used to pair the
    # requested width with the *derived* height, producing a canvas of neither aspect and
    # stretching the frame into it without a word.
    if width is not None or height is not None:
        raise CliError(
            "partial_canvas_with_image",
            "--image derives the canvas from the keyframe, so pass both --width and --height or "
            f"neither. Got only --{'width' if width is not None else 'height'}.",
            {"width": width, "height": height},
        )

    from PIL import Image, ImageOps, UnidentifiedImageError

    from h3_48gb._upstream import ensure_on_path  # noqa: F401  (puts upstream on sys.path)
    from minimax_h3_mlx.packing import resolve_canvas_size

    # `RunSpec.__post_init__` refuses a missing keyframe with `image_not_found`, but it only runs
    # once the canvas is known — so these three failures reach the user from here, and must carry
    # the same codes rather than a raw PIL traceback.
    if not Path(image).exists():
        raise CliError("image_not_found", f"--image does not exist: {image}", {"image": str(image)})
    try:
        with Image.open(image) as raw:
            source = ImageOps.exif_transpose(raw).size
    except (OSError, UnidentifiedImageError) as exc:
        raise CliError("image_unreadable", f"--image could not be read: {image} ({exc})",
                       {"image": str(image)}) from exc
    try:
        derived_height, derived_width = resolve_canvas_size(*source)
    except ValueError as exc:
        raise CliError(
            "image_aspect_unsupported",
            f"--image is {source[0]}x{source[1]}; MiniMax-H3 supports aspect ratios from 1:4 to "
            f"4:1. Crop it, or pass --width and --height explicitly. ({exc})",
            {"image": str(image), "size": list(source)},
        ) from exc
    return derived_width, derived_height


def resolve_prompt(prompt: str | None, prompt_file: Path | None) -> tuple[str, str | None]:
    """The prompt text and the file it came from, or a refusal.

    Trailing newlines are stripped -- all of them, exactly as `$(cat file)` does. This is a
    compatibility requirement, not a preference: every measured run so far was launched through
    that substitution, and one surviving newline would change identity_digest and orphan every
    checkpoint on disk.
    """
    if prompt is not None and prompt_file is not None:
        raise CliError("prompt_both_given",
                       "pass either a positional prompt or --prompt-file, not both")
    if prompt is not None:
        return prompt, None
    if prompt_file is None:
        raise CliError("prompt_missing",
                       "no prompt: pass one positionally or with --prompt-file")
    path = Path(prompt_file)
    if not path.is_file():
        raise CliError("prompt_file_not_found", f"--prompt-file does not exist: {path}",
                       {"prompt_file": str(path)})
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CliError("prompt_file_unreadable",
                       f"--prompt-file could not be read: {path} ({exc})",
                       {"prompt_file": str(path)}) from exc
    text = text.rstrip("\n")
    if not text:
        raise CliError("prompt_file_empty", f"--prompt-file is empty: {path}",
                       {"prompt_file": str(path)})
    return text, str(path.resolve())


def actual_mode(image: Path | None, end_image: Path | None) -> str:
    """The mode these keyframe flags imply, with no reference to `--mode` at all.

    This is the one place that derivation happens; `check_mode` and the run report both call it
    rather than repeating the three-way `if`, so they cannot drift apart.
    """
    return "flf" if end_image else ("i2v" if image else "t2v")


def check_mode(mode: str | None, image: Path | None, end_image: Path | None) -> str:
    """Validate the declared mode against the flags, and return the effective one either way.

    The mode is never *set* by this flag -- it is fully determined by the images. `--mode` exists
    so a typo in a filename or a flag fails in the first second rather than an hour later, when
    the clip turns out to be text-to-video.
    """
    actual = actual_mode(image, end_image)
    if mode is None:
        return actual
    wanted = "t2v" if mode == "t2va" else mode
    if wanted != actual:
        raise CliError(
            "mode_mismatch",
            f"--mode {mode} but the flags say {actual}: "
            f"--image {'given' if image else 'absent'}, "
            f"--end-image {'given' if end_image else 'absent'}",
            {"declared": mode, "actual": actual})
    return actual


def spec_from_args(args: argparse.Namespace) -> RunSpec:
    """Build a `RunSpec` from parsed args, shared by `generate` and `resume` (identical flags).

    Validation lives in `RunSpec.__post_init__`, so it happens here for both subcommands, before
    either one touches a checkpoint path or a weight file. Resolving the prompt happens first of
    all, ahead of the canvas: a bad `--prompt-file` must refuse before anything else does. `--mode`
    is checked next, still ahead of the canvas, so a mismatched `--mode` refuses before
    `resolve_canvas` ever opens the keyframe file.
    """
    prompt, prompt_file = resolve_prompt(args.prompt, args.prompt_file)
    check_mode(getattr(args, "mode", None), args.image, args.end_image)
    width, height = resolve_canvas(args.image, args.width, args.height)
    return RunSpec(
        prompt=prompt, width=width, height=height,
        duration=args.duration, steps=args.steps, seed=args.seed,
        checkpoint=args.checkpoint, outdir=args.outdir, tag=args.tag,
        checkpoint_dir=args.checkpoint_dir,
        no_checkpoint=getattr(args, "no_checkpoint", False),
        image=args.image, end_image=args.end_image,
        preview_every=args.preview_every, preview_stem=args.preview_stem,
        preview_decoder=args.preview_decoder,
        turbo_lora=args.turbo_lora, turbo_strength=args.turbo_strength,
        adaln_cache=args.adaln_cache,
        prompt_file=prompt_file,
    )


def load_keyframes(spec: RunSpec) -> tuple[list, tuple[str, ...]]:
    """Load the conditioning frames and the anchors that place them on the timeline.

    `exif_transpose` is not cosmetic: a camera stores orientation as a tag rather than by
    rotating the pixels, so without it a portrait photo conditions the run on a landscape
    frame — and nothing downstream can tell that happened.

    Frames come back **already on the canvas**, and that is load-bearing rather than tidy: the
    checkpoint's identity digest is taken over what this returns, while the clip is conditioned on
    what the pipeline receives. Returning the raw frame here made those two different images the
    moment the pipeline started preparing them itself, so `h3 resume --image photo.jpg` computed a
    digest no checkpoint had ever been written under and reported `checkpoint_not_found`. Preparing
    once, here, is what keeps the identity and the conditioning describing the same picture.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    images, anchors = [], []
    for path, anchor in ((spec.image, "first"), (spec.end_image, "last")):
        if path is None:
            continue
        # `resolve_canvas` reports the same failure, but only for `--image` and only when it had
        # to derive the canvas. `--end-image`, and any run with an explicit `--width/--height`,
        # reach the decoder for the first time right here.
        try:
            with Image.open(path) as raw:
                oriented = ImageOps.exif_transpose(raw.convert("RGB"))
        except (OSError, UnidentifiedImageError) as exc:
            flag = "--image" if anchor == "first" else "--end-image"
            raise CliError("image_unreadable", f"{flag} could not be read: {path} ({exc})",
                           {"image": str(path)}) from exc

        # Imported here rather than beside `Image`: a text-only run must not pull upstream (and
        # mlx with it) just because this function was called.
        from minimax_h3_mlx.packing import prepare_keyframe_image

        images.append(prepare_keyframe_image(oriented, spec.height, spec.width,
                                             stretch=not images))
        anchors.append(anchor)

    # Checked here rather than in `__post_init__`, which must not import mlx, and only when a
    # keyframe is actually present — a text-only run never reaches the patched line.
    if images:
        from h3_48gb.text_encoder import keyframe_scatter_patch_applied

        if not keyframe_scatter_patch_applied():
            raise CliError(
                "upstream_patch_missing",
                "The vendored `upstream/` checkout has not been patched, so this keyframe would "
                "crash inside the text encoder — after 28.2 GB of weights had loaded. Apply it:\n"
                "    git -C upstream apply ../patches/0001-keyframe-masked-scatter.patch",
                {"patch": "patches/0001-keyframe-masked-scatter.patch"},
            )
    return images, tuple(anchors)


def run_dry(spec: RunSpec) -> dict:
    """Report what `run_generate(spec)` would do, without loading weights or writing anything.

    `spec` has already been through `RunSpec.__post_init__` by the time it reaches here (built by
    `spec_from_args`, same as a real run), so every refusal a real run would raise has already had
    its chance to fire with the same code -- a dry run is not a separate, laxer set of rules.

    The report is a subset of `run_generate`'s: `video`, `audio`, and `generate_seconds` are
    absent rather than empty, because nothing has run yet to fill them in, and a placeholder value
    would be indistinguishable from a real one to a caller that only checks the key exists.
    """
    return {
        "tag": spec.tag,
        "canvas": f"{spec.width}x{spec.height}",
        "duration_seconds": spec.duration,
        "grid_points": spec.steps,
        "forwards": spec.steps - 1,
        "seed": spec.seed,
        "prompt_file": spec.prompt_file,
        "checkpoint": str(spec.checkpoint),
        "adaln_cache": str(spec.adaln_cache) if spec.adaln_cache else None,
        "turbo_lora": str(spec.turbo_lora) if spec.turbo_lora else None,
        "turbo_strength": spec.turbo_strength if spec.turbo_lora else None,
        "mode": actual_mode(spec.image, spec.end_image),
        "dry_run": True,
        "output_stem": str(spec.output_stem()),
    }


def run_generate(spec: RunSpec, pipeline_factory=None, save_mp4_fn=None, save_wav_fn=None,
                  resume: bool = True, verbose: bool = True) -> dict:
    """Generate a video according to the given spec.

    Args:
        spec: RunSpec with all generation parameters
        pipeline_factory: Optional factory function for testing. Defaults to loading
                         the real LazyMiniMaxH3Pipeline.
        save_mp4_fn: Optional override for mp4 saving (for testing).
        save_wav_fn: Optional override for wav saving (for testing).
        resume: Whether to continue from a matching checkpoint under `spec.resume_checkpoint_dir()`
            if one exists, and to write one as the run progresses. On by default, following
            `run_bench.py`: at 586 s/step there is no run short enough for a lost run not to
            matter, and the write costs one small file per step. `h3 generate --restart` passes
            `False` here — that is the supported way out of a `checkpoint_mismatch` refusal, which
            otherwise leaves a user hunting for a file named after a digest they cannot compute.
        verbose: Whether the pipeline (component loads, phase timings) and the checkpoint writer
            (`ResumableRun`'s "checkpoint: N/M steps" line) print progress to stdout. `main`
            passes `not as_json` here: under `--json`, stdout has exactly one contract — one JSON
            document — and both of those write to it by default, which is what made `--json`
            unparseable on a real run before this parameter existed. Left on by default because
            the progress is genuinely useful during a run measured in hours.

    Returns:
        A dict with the generation report (also written to <stem>.json)
    """
    # The schedule and the geometry are already refused by `RunSpec.__post_init__`, so nothing
    # here can start a multi-hour run on a request this build cannot serve.
    spec.outdir.mkdir(parents=True, exist_ok=True)
    stem = spec.output_stem()
    checkpoint_dir = spec.resume_checkpoint_dir()
    preview_stem = spec.preview_stem if spec.preview_stem is not None else stem

    # The single-argument `factory(checkpoint)` contract is kept even for the default factory, so
    # every existing `pipeline_factory=lambda _: ...` test stub keeps working unchanged; `verbose`
    # reaches `from_pretrained` (which does its own printing while loading configs) via closure
    # instead of a second factory argument.
    factory = pipeline_factory or (
        lambda checkpoint: _default_pipeline_factory(checkpoint, verbose=verbose,
                                                     adaln_cache=spec.adaln_cache))
    pipe = install_turbo_lora(factory(spec.checkpoint), spec, verbose=verbose)

    # Refusals from h3_48gb.checkpoint are already precise (which field mismatched, why the file
    # would not read) — CliError just gives them a stable code so `--json` does not have to
    # string-match the sentence. Imported here rather than at module scope to keep `import
    # h3_48gb` from pulling in mlx.core (see the module docstring / test_import_h3_48gb_...).
    from h3_48gb.checkpoint import CheckpointCorrupt, CheckpointLocked, CheckpointMismatch

    started = time.perf_counter()
    try:
        # Load conditioning keyframes if provided.
        images, keyframe_anchors = load_keyframes(spec)

        # `verbose` here is not one of h3_48gb.checkpoint's own kwargs — it is upstream's own
        # `MiniMaxH3Pipeline.__call__` parameter, which `CheckpointingPipeline.__call__` also reads
        # to decide whether `ResumableRun` prints its "checkpoint: N/M steps" line. One flag, both
        # writers.
        result = pipe(prompt=spec.prompt, duration_seconds=spec.duration,
                      num_inference_steps=spec.steps, seed=spec.seed,
                      height=spec.height, width=spec.width,
                      checkpoint_dir=str(checkpoint_dir) if checkpoint_dir else None,
                      resume=resume, verbose=verbose, tag=spec.tag,
                      # `<stem>-preview-stepNN.jpg`, next to where `<stem>.mp4` will land.
                      preview_every=spec.preview_every,
                      preview_stem=str(preview_stem) if spec.preview_every else None,
                      preview_decoder=spec.preview_decoder,
                      images=images or None, keyframe_anchors=keyframe_anchors)
    except CheckpointMismatch as exc:
        # The message from h3_48gb.checkpoint names the file and the fields that differ, but a
        # user reading it has no way to construct that filename themselves — so name the flag.
        raise CliError(
            "checkpoint_mismatch",
            f"{exc}\n  Or re-run with --restart to ignore it and start from step 0.",
        ) from exc
    except CheckpointCorrupt as exc:
        raise CliError("checkpoint_corrupt", str(exc)) from exc
    except CheckpointLocked as exc:
        raise CliError("checkpoint_locked", str(exc)) from exc
    elapsed = time.perf_counter() - started

    # Raw first: an encoder failure then costs seconds, not a fifteen-hour run.
    # Write atomically: temp file, fsync, then rename over destination.
    raw_path = Path(f"{stem}-raw.npz")
    raw_temp = raw_path.with_name(f".{raw_path.stem}.tmp-{os.getpid()}{raw_path.suffix}")
    try:
        np.savez_compressed(str(raw_temp), video=result.video, audio=result.audio,
                            sample_rate=result.sample_rate)
        fd = os.open(raw_temp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(raw_temp, raw_path)
        dir_fd = os.open(raw_path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        raw_temp.unlink(missing_ok=True)
        raise

    # Import media functions inside the function to keep package imports light.
    from minimax_h3_mlx.media import save_mp4 as _save_mp4, save_wav as _save_wav
    from minimax_h3_mlx.packing import FPS

    # Use injected functions for testing, or the real ones.
    save_mp4 = save_mp4_fn or _save_mp4
    save_wav = save_wav_fn or _save_wav

    # Note: fps is the third positional parameter; audio is fourth.
    save_mp4(f"{stem}.mp4", result.video, FPS, result.audio, result.sample_rate)
    save_wav(f"{stem}.wav", result.audio, result.sample_rate)

    report = {
        "tag": spec.tag, "canvas": f"{spec.width}x{spec.height}",
        "duration_seconds": spec.duration, "grid_points": spec.steps,
        # Written out because "steps" is easy to misread: an N-point grid takes N-1 forward
        # passes (the first grid point is the starting noise, not a pass) — this off-by-one is
        # what cost a whole rerun on 2026-08-10, when nothing on disk said which count a "349"
        # or a "265" actually meant.
        "forwards": spec.steps - 1,
        "seed": spec.seed,
        "prompt": spec.prompt,
        # `None` when the prompt came from the positional argument; already a plain string
        # (resolved in `resolve_prompt`) when it came from `--prompt-file`, so no `str()` needed
        # here the way the other optional path fields below need one.
        "prompt_file": spec.prompt_file,
        # Paths as strings: `json.dumps` cannot serialize a `Path`, and `h3 list` /
        # analysis scripts read these back as plain text anyway.
        "checkpoint": str(spec.checkpoint),
        "adaln_cache": str(spec.adaln_cache) if spec.adaln_cache else None,
        "turbo_lora": str(spec.turbo_lora) if spec.turbo_lora else None,
        # `turbo_strength` only describes something that happened when an adapter was actually
        # applied. With no `turbo_lora`, `spec.turbo_strength` is just RunSpec's unused default
        # (1.0) — writing it would look like a real, chosen strength instead of a field the run
        # never touched. `null` here must mean "no LoRA on this run," distinguishable from a
        # forgotten field, so the key stays present rather than being omitted.
        "turbo_strength": spec.turbo_strength if spec.turbo_lora else None,
        "image": str(spec.image) if spec.image else None,
        "end_image": str(spec.end_image) if spec.end_image else None,
        # Derived, not stored: `mode` is fully implied by `image`/`end_image`, which are already
        # part of the run's identity, so a separate `RunSpec` field would just be a second place
        # for the same fact to drift. This is a reader's convenience, nothing more.
        "mode": actual_mode(spec.image, spec.end_image),
        "generate_seconds": round(elapsed, 1),
        "seconds_per_step": round(result.seconds_per_step, 1),
        "frames": int(result.video.shape[0]),
        "video": str(stem) + ".mp4",
    }
    Path(f"{stem}.json").write_text(json.dumps(report, indent=2))
    return report


def _default_pipeline_factory(checkpoint: Path, verbose: bool = True,
                              adaln_cache: Path | None = None):
    """Load the real LazyMiniMaxH3Pipeline from a checkpoint.

    `verbose=False` silences `from_pretrained`'s own "loading MiniMax-H3 configs..." /
    "loaded <component>: ..." lines — the other half of the `--json` stdout leak alongside the
    checkpoint writer's, see `run_generate`.
    """
    from h3_48gb import LazyMiniMaxH3Pipeline

    return LazyMiniMaxH3Pipeline.from_pretrained(str(checkpoint), verbose=verbose,
                                                 adaln_cache=adaln_cache)


def install_turbo_lora(pipe, spec: RunSpec, verbose: bool = True):
    """Apply `spec.turbo_lora` to whatever pipeline the factory produced, and return it.

    A separate step rather than a factory argument, for two reasons: it works with any factory
    (test stubs take one argument and must keep doing so), and `resume` gets it for free.

    Wraps the *loader* rather than the loaded transformer, because the transformer is lazy — it
    does not exist yet here, and a resumed run drops and reloads it.
    """
    if spec.turbo_lora is None:
        return pipe

    # `run_resume` installs, then calls `run_generate`, which installs again — nesting one
    # LoRALinear inside another and doubling the effective strength. Found in review before it
    # cost a run: 0.45 requested would have applied as 0.90, and the numbers this project spent a
    # day measuring would have meant nothing.
    if getattr(pipe, "_turbo_lora", None) is not None:
        return pipe

    from h3_48gb.turbo import apply_backbone_lora, apply_lightx2v_lora

    inner = pipe.dit.__dict__["_loader"]
    apply = (apply_lightx2v_lora if "lightx2v" in Path(spec.turbo_lora).name.lower()
             else apply_backbone_lora)

    def load_with_lora():
        dit = inner()
        apply(dit, spec.turbo_lora, strength=spec.turbo_strength, verbose=verbose)
        return dit

    pipe.dit.__dict__["_loader"] = load_with_lora
    # Recorded so `checkpoint_identity_extra` can bind them: a resume at another strength, or with
    # another adapter, is a different run and must not silently continue this one's latents.
    pipe._turbo_lora = Path(spec.turbo_lora)
    pipe._turbo_strength = spec.turbo_strength
    return pipe


def _checkpoint_path_for(spec: RunSpec, pipe, checkpoint_dir: Path) -> Path:
    """The resume-checkpoint file `run_generate`'s call to `pipe(...)` would use for this spec.

    `h3_48gb.checkpoint.CheckpointingPipeline` names that file after the request's identity
    digest (``h3-{digest}.safetensors``) internally, in a helper it does not export — only the
    identity machinery (`request_identity`, `identity_digest`) is public, so this has to agree
    with that naming rather than reimplement it blind. It binds the same upstream `__call__`
    signature `run_generate` implicitly binds by calling `pipe(...)` with the same arguments, so
    the two stay in lockstep as long as `run_generate` does not start passing extra ones. `tag` is
    passed the same way `run_generate` passes it to `pipe(...)` — as its own `request_identity`
    argument, not through `bound.arguments`, since upstream's `__call__` has no `tag` parameter at
    all.

    `images`/`keyframe_anchors` are loaded and bound here too, mirroring `run_generate`'s call —
    without them a conditioned run and an unconditioned one with the same prompt/geometry/seed/tag
    would resolve to the same checkpoint file, and resuming one would continue the clip from
    latents written for different conditioning.
    """
    import inspect

    from h3_48gb.checkpoint import identity_digest, request_identity
    from minimax_h3_mlx.pipeline import MiniMaxH3Pipeline

    images, keyframe_anchors = load_keyframes(spec)
    bound = inspect.signature(MiniMaxH3Pipeline.__call__).bind(
        pipe, prompt=spec.prompt, duration_seconds=spec.duration,
        num_inference_steps=spec.steps, seed=spec.seed, height=spec.height, width=spec.width,
        images=images or None, keyframe_anchors=keyframe_anchors,
    )
    bound.apply_defaults()
    identity = request_identity(dict(bound.arguments), pipe.checkpoint_identity_extra(), tag=spec.tag)
    return checkpoint_dir / f"h3-{identity_digest(identity)}.safetensors"


def run_resume(spec: RunSpec, pipeline_factory=None, save_mp4_fn=None, save_wav_fn=None,
               verbose: bool = True) -> dict:
    """Continue an interrupted run. Unlike `generate`, this refuses to start over silently.

    `generate` already checkpoints and auto-resumes whenever a matching file exists — `resume`
    exists for the case an operator (or an MCP wrapper) wants to *assert* that a run is being
    continued rather than quietly restarted from step 0, which `checkpoint_not_found` makes a
    machine-checkable fact instead of something only visible in a log line.
    """
    factory = pipeline_factory or (
        lambda checkpoint: _default_pipeline_factory(checkpoint, verbose=verbose,
                                                     adaln_cache=spec.adaln_cache))
    pipe = install_turbo_lora(factory(spec.checkpoint), spec, verbose=verbose)

    checkpoint_dir = spec.resume_checkpoint_dir()
    path = _checkpoint_path_for(spec, pipe, checkpoint_dir)
    if not path.exists():
        raise CliError(
            "checkpoint_not_found",
            f"no checkpoint to resume at {path}; nothing matches this prompt/geometry/seed/tag "
            "under --checkpoint-dir. Run 'generate' first, or check that --checkpoint, --outdir, "
            "--checkpoint-dir, --tag, --width, --height, --duration, --steps and --seed all match "
            "the interrupted run.",
            {"checkpoint_dir": str(checkpoint_dir)},
        )
    # The pipe is already loaded (needed above for the identity check) — reuse it rather than
    # loading it a second time; `run_generate`'s own factory-wrapping is bypassed here, which is
    # why `verbose` had to be threaded through this function's own factory call above instead.
    return run_generate(spec, pipeline_factory=lambda _checkpoint: pipe,
                         save_mp4_fn=save_mp4_fn, save_wav_fn=save_wav_fn, resume=True,
                         verbose=verbose)


def run_list(outdir: Path) -> list[dict]:
    """Every finished run's report under `outdir`, oldest tag first (`Path.glob` is name-sorted)."""
    rows = []
    for report in sorted(Path(outdir).glob("h3-*.json")):
        rows.append(json.loads(report.read_text()))
    return rows


#: States for which `watch` should continue polling the outdir. Only `in_flight` -- the one state
#: backed by an actual liveness signal (a companion lock file a writer holds; see
#: `h3_48gb.runs._writer_alive`). `unknown` means "no lock file to check, nothing to say" and
#: `stale`/`unreadable` are already-settled outcomes; polling any of them again can never turn them
#: into new information; only `in_flight` can, when the lock is eventually released.
WATCH_RUNNING_STATES = {"in_flight"}


def run_status(outdir: Path) -> dict:
    """Every run under `outdir`, scanned recursively — in-flight ones first."""
    from .runs import scan

    outdir = Path(outdir)
    if not outdir.is_dir():
        raise CliError("outdir_not_found", f"--outdir does not exist: {outdir}",
                       {"outdir": str(outdir)})
    runs = scan(outdir)
    # "unknown" ranks ahead of "stale": "confirmed gone" (stale) needs less attention than "cannot
    # be judged at all" (unknown, no lock file to check). See `_state_of` in runs.py.
    order = {"in_flight": 0, "unknown": 1, "stale": 2, "unreadable": 3, "finished": 4}
    runs.sort(key=lambda r: (order.get(r.state, 9), str(r.outdir)))
    return {"ok": True, "outdir": str(outdir), "runs": [r.as_dict() for r in runs]}


def _progress(row: dict) -> str:
    """`completed/total`, with either half rendered as `?` rather than the literal `None` -- a
    run caught by its lock file before its first checkpoint write has neither yet (see
    `h3_48gb.runs.scan`)."""
    completed = row["completed"] if row["completed"] is not None else "?"
    total = row["total"] if row["total"] is not None else "?"
    return f"{completed}/{total}"


def format_status(report: dict) -> str:
    """One block per in-flight run, one line per run whose rate is unknown (forwards done and
    time since the last write, no verdict on whether it is alive), one line per stale run, and
    one line per unreadable run with its error -- every run `scan` found gets one line here, none
    silently dropped, which is the whole point of a status command over a broken checkpoint.
    """
    lines = []
    for row in report["runs"]:
        if row["state"] != "in_flight":
            continue
        pct = f"{row['fraction'] * 100:.0f}%" if row["fraction"] is not None else "?"
        lines.append(f"{Path(row['outdir']).name}    {_progress(row)} форвардов, {pct}")
        # Guard on `eta_seconds` itself, not on `seconds_per_forward`: the rate can be known while
        # `eta_seconds` is still None because `total` is -- a checkpoint missing or zero
        # `total_forwards`. Guarding on the rate alone divides `None / 60` and crashes the whole
        # report over one such file, the same class of regression commit dae41090 fixed for `scan`.
        if row["eta_seconds"] is not None:
            left = row["eta_seconds"] / 60
            lines.append(f"  {row['seconds_per_forward']:.0f} с на форвард, осталось {left:.0f} мин")

    for row in report["runs"]:
        if row["state"] != "unknown":
            continue
        age = (f"{row['age_seconds']:.0f} с назад" if row["age_seconds"] is not None
               else "неизвестно когда")
        lines.append(f"{Path(row['outdir']).name}: {_progress(row)}, последняя запись {age}, "
                     "скорость неизвестна")

    for row in report["runs"]:
        if row["state"] != "stale":
            continue
        lines.append(f"{Path(row['outdir']).name}: остановлен на {_progress(row)}")

    for row in report["runs"]:
        if row["state"] != "unreadable":
            continue
        lines.append(f"{Path(row['outdir']).name}: не читается ({row['error']})")

    if not lines:
        lines.append(f"ничего не идёт в {report['outdir']}")
    return "\n".join(lines)


def run_watch(outdir: Path, interval: float) -> dict:
    """Redraw the status block until no run is `in_flight`, then return the final state.

    A run in `unknown` is printed once and then left alone: there is no lock file to say whether
    its writer is still around, and polling a question with no new answer coming is just spinning.
    `stale` and `unreadable` are already-settled outcomes for the same reason. Only `in_flight` can
    change into something else while `watch` is looking at it -- the lock either stays held or it
    doesn't -- so it is the only state worth another pass. See `WATCH_RUNNING_STATES`.

    Not a TUI: the block is reprinted with a leading escape that clears the previous one, so a
    dumb terminal and a redirected log both stay readable.
    """
    import time as _time

    while True:
        report = run_status(outdir)
        text = format_status(report)
        print(text, flush=True)
        running = [r for r in report["runs"] if r["state"] in WATCH_RUNNING_STATES]
        if not running or interval <= 0:
            return report
        _time.sleep(interval)
        print(f"\033[{text.count(chr(10)) + 1}A\033[J", end="")


def queue_root(outdir) -> Path:
    """Where the queue lives for a given output directory: `<outdir>/queue`.

    One function rather than a `/ "queue"` at each call site, because the worker, the web server
    and every test have to agree on it exactly -- a second spelling would be a second, silently
    empty queue rather than an error.
    """
    return Path(outdir) / "queue"


def run_worker(outdir: Path, poll: float = 5.0) -> dict:
    """Run the queue worker until it is asked to stop, and report how many jobs it got through.

    Imported lazily, like every other heavy dependency here, though for the opposite reason: the
    worker is deliberately cheap (it never imports MLX), but `h3 --help` still has no business
    loading a module it will not use.

    `--outdir` is validated before the queue directory is touched. `main_loop` creates the queue
    layout under it, so an outdir that does not exist would otherwise be silently built out of a
    typo, and the worker would then sit for ever on an empty queue while jobs pile up in the real
    one.

    `outdir=outdir` is passed explicitly rather than left to `main_loop`'s default: it is what
    `provider.load_providers` reads `providers.json` from (the same directory `h3 web` treats as
    its own `--outdir`), and `main_loop`'s `root` here is `queue_root(outdir)`, one level below
    that. Relying on the default (`Path(root).parent`) would happen to work today only because the
    two calls agree by construction -- passing it here says so instead of leaving it implicit.
    """
    from h3_48gb.worker import WorkerAlreadyRunning, main_loop

    outdir = Path(outdir)
    if not outdir.is_dir():
        raise CliError("outdir_not_found", f"--outdir does not exist: {outdir}",
                       {"outdir": str(outdir)})
    root = queue_root(outdir)
    try:
        ran = main_loop(root, poll=poll, outdir=outdir)
    except WorkerAlreadyRunning as exc:
        raise CliError(
            "worker_already_running",
            f"another worker already holds {root / 'worker.lock'}; two workers on this machine "
            "means two 36 GB processes and swap",
            {"queue": str(root)},
        ) from exc
    return {"ok": True, "queue": str(root), "jobs_run": ran}


def run_web(outdir: Path, port: int = 8765) -> dict:
    """Serve the queue page on `127.0.0.1:<port>` until the process is stopped.

    Imported lazily for the same reason `run_worker` imports its module lazily: `h3 --help` has no
    business loading `http.server`. `h3_48gb.web` never imports MLX, so this stays a cheap process
    even though it sits resident all day next to a generation that is not.

    `--outdir` is validated before anything is bound, exactly as `run_worker` validates it and for
    the same reason: `queue.scan` deliberately does not create the directory it reads, so a typo'd
    outdir would otherwise produce a page that serves an empty queue for ever while the real one
    fills up somewhere else. Nothing here creates the queue layout either -- the worker owns that.

    `Ctrl-C` is the documented way to stop it, so `KeyboardInterrupt` is a normal exit rather than
    a traceback: the socket is closed and the report is returned.
    """
    from h3_48gb.web import LOOPBACK, make_server

    outdir = Path(outdir)
    if not outdir.is_dir():
        raise CliError("outdir_not_found", f"--outdir does not exist: {outdir}",
                       {"outdir": str(outdir)})
    root = queue_root(outdir)
    httpd = make_server(root, outdir, port=port, verbose=True)
    # Not `port`: with `--port 0` the kernel picks, and the number a human has to type is the one
    # the socket actually got.
    bound = httpd.server_address[1]
    url = f"http://{LOOPBACK}:{bound}/"
    print(f"страница на {url} — очередь в {root}; остановить: Ctrl-C", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return {"ok": True, "url": url, "queue": str(root)}


def run_doctor(checkpoint: Path) -> dict:
    """Check a converted checkpoint before a multi-hour run rather than during it."""
    checkpoint = Path(checkpoint)
    missing = [name for name in REQUIRED_COMPONENTS if not (checkpoint / name).is_dir()]
    cache = checkpoint / "transformer" / "adaln_cache.safetensors"
    if not cache.exists():
        missing.append("transformer/adaln_cache.safetensors")
    return {"ok": not missing, "checkpoint": str(checkpoint), "missing": missing}


def main(argv: list[str] | None = None) -> int:
    """Entry point for the CLI.

    Every subcommand funnels through one try/except so `--json` gets the same failure shape
    regardless of which one raised: a `CliError` becomes `{"ok": false, "error": {...}}` on
    stdout with exit 1; anything else still becomes valid JSON (`internal_error`) rather than a
    bare traceback splitting stdout's contract, though in human mode it is left to propagate
    normally so a real bug still gets a real traceback. See the module docstring for why the two
    are handled differently.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "json", False))

    try:
        if args.command == "generate":
            spec = spec_from_args(args)
            if getattr(args, "dry_run", False):
                # No weights loaded, nothing written: `spec_from_args` above already ran every
                # refusal a real run would, so reaching here means this request would proceed.
                report = run_dry(spec)
                ok = True
                human = f"would generate -> {report['output_stem']}"
            else:
                # Under --json, stdout has exactly one contract: one JSON document. verbose=False
                # keeps the pipeline and the checkpoint writer from printing progress onto it.
                report = run_generate(spec, resume=not args.restart, verbose=not as_json)
                ok = True
                human = f"done in {report['generate_seconds'] / 60:.1f} min -> {report['video']}"
        elif args.command == "resume":
            report = run_resume(spec_from_args(args), verbose=not as_json)
            ok = True
            human = f"resumed, done in {report['generate_seconds'] / 60:.1f} min -> {report['video']}"
        elif args.command == "list":
            report = run_list(args.outdir)
            ok = True
            human = ("\n".join(f"{row.get('tag', '?')}: {row.get('frames', '?')} frames"
                               for row in report)
                     or f"no finished runs in {args.outdir}")
        elif args.command == "status":
            report = run_status(args.outdir)
            ok = True
            human = format_status(report)
        elif args.command == "watch":
            report = run_watch(args.outdir, args.interval)
            ok = True
            human = ""
        elif args.command == "worker":
            report = run_worker(args.outdir, args.poll)
            ok = True
            human = f"работник остановлен, задач выполнено: {report['jobs_run']}"
        elif args.command == "web":
            report = run_web(args.outdir, args.port)
            ok = True
            human = "сервер остановлен"
        elif args.command == "doctor":
            report = run_doctor(args.checkpoint)
            ok = report["ok"]
            human = (f"checkpoint at {report['checkpoint']} looks complete" if ok else
                      f"checkpoint at {report['checkpoint']} is missing: {', '.join(report['missing'])}")
        else:  # pragma: no cover - argparse's `required=True` on the subcommand rules this out
            raise CliError("internal_error", f"unknown command {args.command!r}")
    except CliError as exc:
        if as_json:
            print(json.dumps(exc.to_dict(), indent=2))
        else:
            print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - last-resort JSON safety net, see module docstring
        if as_json:
            print(json.dumps(CliError("internal_error", str(exc)).to_dict(), indent=2))
            return 1
        raise

    if as_json or human:
        print(json.dumps(report, indent=2) if as_json else human)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
