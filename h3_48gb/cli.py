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
DEFAULT_OUTDIR = Path.home() / "models/video-out"
BAKED_GRID_POINTS = 31

#: Every machine-readable failure code this CLI can raise under `--json`. The whole contract an
#: MCP wrapper needs is here: match on `error.code`, never on `error.message` — the sentence can
#: be reworded for clarity at any time; the code cannot, without a deliberate, documented break.
ERROR_CODES = {
    "geometry_not_multiple_of_32": "--width or --height is not a multiple of 32; the port cannot pack it",
    "schedule_not_baked": "--steps does not equal the one grid size the baked AdaLN table covers",
    "checkpoint_not_found": "`resume` was asked for, but no checkpoint matches this run's identity",
    "checkpoint_mismatch": "a checkpoint exists but was written for a different request or model",
    "checkpoint_corrupt": "a checkpoint exists but could not be read",
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="h3", description="MiniMax H3 on a 48 GB Mac")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate a clip")
    gen.add_argument("prompt")
    gen.add_argument("--width", type=int, default=1344)
    gen.add_argument("--height", type=int, default=768)
    gen.add_argument("--duration", type=float, default=5.0)
    gen.add_argument("--steps", type=int, default=BAKED_GRID_POINTS)
    gen.add_argument("--seed", type=int, default=0)
    gen.add_argument("--tag", default="run")
    gen.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    gen.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    gen.add_argument("--json", action="store_true", help="emit a machine-readable report")

    res = sub.add_parser("resume", help="continue an interrupted run")
    res.add_argument("prompt")
    res.add_argument("--width", type=int, default=1344)
    res.add_argument("--height", type=int, default=768)
    res.add_argument("--duration", type=float, default=5.0)
    res.add_argument("--steps", type=int, default=BAKED_GRID_POINTS)
    res.add_argument("--seed", type=int, default=0)
    res.add_argument("--tag", default="run")
    res.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    res.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    res.add_argument("--json", action="store_true", help="emit a machine-readable report")

    lst = sub.add_parser("list", help="list finished runs")
    lst.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    lst.add_argument("--json", action="store_true", help="emit a machine-readable report")

    doc = sub.add_parser("doctor", help="verify a converted checkpoint")
    doc.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    doc.add_argument("--json", action="store_true", help="emit a machine-readable report")
    return parser


def spec_from_args(args: argparse.Namespace) -> RunSpec:
    """Build a `RunSpec` from parsed args, shared by `generate` and `resume` (identical flags)."""
    for name in ("width", "height"):
        value = getattr(args, name)
        if value % 32:
            raise CliError(
                "geometry_not_multiple_of_32",
                f"--{name} must be a multiple of 32, got {value}",
                {name: value},
            )
    return RunSpec(
        prompt=args.prompt, width=args.width, height=args.height,
        duration=args.duration, steps=args.steps, seed=args.seed,
        checkpoint=args.checkpoint, outdir=args.outdir, tag=args.tag,
    )


def run_generate(spec: RunSpec, pipeline_factory=None, save_mp4_fn=None, save_wav_fn=None,
                  resume: bool = True, verbose: bool = True) -> dict:
    """Generate a video according to the given spec.

    Args:
        spec: RunSpec with all generation parameters
        pipeline_factory: Optional factory function for testing. Defaults to loading
                         the real LazyMiniMaxH3Pipeline.
        save_mp4_fn: Optional override for mp4 saving (for testing).
        save_wav_fn: Optional override for wav saving (for testing).
        resume: Whether to continue from a matching checkpoint under `<outdir>/checkpoints` if
            one exists, and to write one as the run progresses. On by default, following
            `run_bench.py`: at 586 s/step there is no run short enough for a lost run not to
            matter, and the write costs one small file per step.
        verbose: Whether the pipeline (component loads, phase timings) and the checkpoint writer
            (`ResumableRun`'s "checkpoint: N/M steps" line) print progress to stdout. `main`
            passes `not as_json` here: under `--json`, stdout has exactly one contract — one JSON
            document — and both of those write to it by default, which is what made `--json`
            unparseable on a real run before this parameter existed. Left on by default because
            the progress is genuinely useful during a run measured in hours.

    Returns:
        A dict with the generation report (also written to <stem>.json)
    """
    # Validate the schedule early, before touching any weights.
    if spec.steps != BAKED_GRID_POINTS:
        raise CliError(
            "schedule_not_baked",
            f"steps must be {BAKED_GRID_POINTS} (baked AdaLN schedule covers only that value), "
            f"got {spec.steps}",
            {"steps": spec.steps, "required": BAKED_GRID_POINTS},
        )

    spec.outdir.mkdir(parents=True, exist_ok=True)
    stem = spec.outdir / f"h3-{spec.tag}-{spec.width}x{spec.height}"
    checkpoint_dir = spec.outdir / "checkpoints"

    # The single-argument `factory(checkpoint)` contract is kept even for the default factory, so
    # every existing `pipeline_factory=lambda _: ...` test stub keeps working unchanged; `verbose`
    # reaches `from_pretrained` (which does its own printing while loading configs) via closure
    # instead of a second factory argument.
    factory = pipeline_factory or (lambda checkpoint: _default_pipeline_factory(checkpoint, verbose=verbose))
    pipe = factory(spec.checkpoint)

    # Refusals from h3_48gb.checkpoint are already precise (which field mismatched, why the file
    # would not read) — CliError just gives them a stable code so `--json` does not have to
    # string-match the sentence. Imported here rather than at module scope to keep `import
    # h3_48gb` from pulling in mlx.core (see the module docstring / test_import_h3_48gb_...).
    from h3_48gb.checkpoint import CheckpointCorrupt, CheckpointMismatch

    started = time.perf_counter()
    try:
        # `verbose` here is not one of h3_48gb.checkpoint's own kwargs — it is upstream's own
        # `MiniMaxH3Pipeline.__call__` parameter, which `CheckpointingPipeline.__call__` also reads
        # to decide whether `ResumableRun` prints its "checkpoint: N/M steps" line. One flag, both
        # writers.
        result = pipe(prompt=spec.prompt, duration_seconds=spec.duration,
                      num_inference_steps=spec.steps, seed=spec.seed,
                      height=spec.height, width=spec.width,
                      checkpoint_dir=str(checkpoint_dir), resume=resume, verbose=verbose)
    except CheckpointMismatch as exc:
        raise CliError("checkpoint_mismatch", str(exc)) from exc
    except CheckpointCorrupt as exc:
        raise CliError("checkpoint_corrupt", str(exc)) from exc
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
        "duration_seconds": spec.duration, "grid_points": spec.steps, "seed": spec.seed,
        "generate_seconds": round(elapsed, 1),
        "seconds_per_step": round(result.seconds_per_step, 1),
        "frames": int(result.video.shape[0]),
        "video": str(stem) + ".mp4",
    }
    Path(f"{stem}.json").write_text(json.dumps(report, indent=2))
    return report


def _default_pipeline_factory(checkpoint: Path, verbose: bool = True):
    """Load the real LazyMiniMaxH3Pipeline from a checkpoint.

    `verbose=False` silences `from_pretrained`'s own "loading MiniMax-H3 configs..." /
    "loaded <component>: ..." lines — the other half of the `--json` stdout leak alongside the
    checkpoint writer's, see `run_generate`.
    """
    from h3_48gb import LazyMiniMaxH3Pipeline

    return LazyMiniMaxH3Pipeline.from_pretrained(str(checkpoint), verbose=verbose)


def _checkpoint_path_for(spec: RunSpec, pipe, checkpoint_dir: Path) -> Path:
    """The resume-checkpoint file `run_generate`'s call to `pipe(...)` would use for this spec.

    `h3_48gb.checkpoint.CheckpointingPipeline` names that file after the request's identity
    digest (``h3-{digest}.safetensors``) internally, in a helper it does not export — only the
    identity machinery (`request_identity`, `identity_digest`) is public, so this has to agree
    with that naming rather than reimplement it blind. It binds the same upstream `__call__`
    signature `run_generate` implicitly binds by calling `pipe(...)` with the same arguments, so
    the two stay in lockstep as long as `run_generate` does not start passing extra ones.
    """
    import inspect

    from h3_48gb.checkpoint import identity_digest, request_identity
    from minimax_h3_mlx.pipeline import MiniMaxH3Pipeline

    bound = inspect.signature(MiniMaxH3Pipeline.__call__).bind(
        pipe, prompt=spec.prompt, duration_seconds=spec.duration,
        num_inference_steps=spec.steps, seed=spec.seed, height=spec.height, width=spec.width,
    )
    bound.apply_defaults()
    identity = request_identity(dict(bound.arguments), pipe.checkpoint_identity_extra())
    return checkpoint_dir / f"h3-{identity_digest(identity)}.safetensors"


def run_resume(spec: RunSpec, pipeline_factory=None, save_mp4_fn=None, save_wav_fn=None,
               verbose: bool = True) -> dict:
    """Continue an interrupted run. Unlike `generate`, this refuses to start over silently.

    `generate` already checkpoints and auto-resumes whenever a matching file exists — `resume`
    exists for the case an operator (or an MCP wrapper) wants to *assert* that a run is being
    continued rather than quietly restarted from step 0, which `checkpoint_not_found` makes a
    machine-checkable fact instead of something only visible in a log line.
    """
    factory = pipeline_factory or (lambda checkpoint: _default_pipeline_factory(checkpoint, verbose=verbose))
    pipe = factory(spec.checkpoint)

    checkpoint_dir = spec.outdir / "checkpoints"
    path = _checkpoint_path_for(spec, pipe, checkpoint_dir)
    if not path.exists():
        raise CliError(
            "checkpoint_not_found",
            f"no checkpoint to resume at {path}; nothing matches this prompt/geometry/seed/tag "
            "under --outdir. Run 'generate' first, or check that --checkpoint, --outdir, --tag, "
            "--width, --height, --duration, --steps and --seed all match the interrupted run.",
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
            # Under --json, stdout has exactly one contract: one JSON document. verbose=False
            # keeps the pipeline and the checkpoint writer from printing progress onto it.
            report = run_generate(spec_from_args(args), verbose=not as_json)
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

    print(json.dumps(report, indent=2) if as_json else human)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
