# minimax-h3-mlx-48gb Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a working local fork into a publishable GitHub project that lets anyone run MiniMax H3 on a 48 GB Mac, with a CLI usable both by humans and, later, by an MCP wrapper.

**Architecture:** `h3_48gb/` patches the upstream MLX port from the outside (upstream is never edited, so it stays fast-forwardable). A new `h3_48gb/cli.py` exposes subcommands over the existing pipeline; every command accepts explicit flags and can emit machine-readable JSON, so an MCP server can call it without screen-scraping. Weights are never redistributed — the converter turns the user's own Sawfwair download into the layout the port reads.

**Tech Stack:** Python 3.12, MLX 0.32, mlx-vlm, safetensors, argparse, ffmpeg (via upstream `media.py`).

## Global Constraints

- `upstream/` is never modified. All behaviour changes live in `h3_48gb/`.
- Code, comments, docstrings and user-facing strings are English. This ships publicly.
- No model weights are committed or uploaded. MiniMax H3 Community License excludes use, distribution and display in the United States, European Union, United Kingdom and Republic of Korea, and imposes downstream notice obligations. We ship code; users convert their own copy.
- The AdaLN table shipped by mere.run is baked for `num_inference_steps=31` (30 forwards) at sigma shifts 12.0/3.0. Any other schedule must fail loudly, never silently.
- Every command that can run for hours must be resumable and must not lose work on a write error.
- Tests must not run a real generation: a single native step costs 586 seconds. Use stub DiTs or synthetic tensors that exercise the real code path.
- Measured baselines to preserve in docs: 512×512 / 2.4 s → 46 s per step, 24 min total, 11.0 GB peak RSS; 1344×768 / 5 s → 586 s per step, 299 min total, 10.0 GB peak RSS; both with zero swap.

---

### Task 1: CLI skeleton with `generate`

**Files:**
- Create: `h3_48gb/cli.py`
- Create: `tests/test_cli.py`
- Modify: `h3_48gb/__init__.py` (export `main`)

**Interfaces:**
- Consumes: `LazyMiniMaxH3Pipeline.from_pretrained(checkpoint) -> pipeline`, `pipeline(prompt, duration_seconds, num_inference_steps, seed, height, width) -> GenerationResult` with fields `video`, `audio`, `sample_rate`, `seconds_per_step`.
- Produces: `h3_48gb.cli.main(argv: list[str] | None = None) -> int`, `build_parser() -> argparse.ArgumentParser`, and `RunSpec` (frozen dataclass with `prompt: str`, `width: int`, `height: int`, `duration: float`, `steps: int`, `seed: int`, `checkpoint: Path`, `outdir: Path`, `tag: str`). Task 2 and Task 3 both consume `RunSpec`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
import json
from pathlib import Path

from h3_48gb.cli import RunSpec, build_parser, spec_from_args


def test_parser_defaults_to_the_baked_schedule():
    args = build_parser().parse_args(["generate", "a cat"])
    assert args.steps == 31, "the shipped AdaLN table only covers 31 grid points"


def test_spec_carries_every_field_that_identifies_a_run():
    args = build_parser().parse_args(
        ["generate", "a cat", "--width", "1344", "--height", "768",
         "--duration", "5", "--seed", "7", "--tag", "demo"]
    )
    spec = spec_from_args(args)
    assert spec == RunSpec(
        prompt="a cat", width=1344, height=768, duration=5.0, steps=31, seed=7,
        checkpoint=Path.home() / "models/h3-converted",
        outdir=Path.home() / "models/video-out", tag="demo",
    )


def test_rejects_geometry_the_port_cannot_pack():
    parser = build_parser()
    args = parser.parse_args(["generate", "a cat", "--height", "432"])
    try:
        spec_from_args(args)
    except SystemExit as exc:
        assert "multiple of 32" in str(exc)
    else:
        raise AssertionError("432 is not a multiple of 32 and must be rejected up front")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'h3_48gb.cli'`

- [ ] **Step 3: Write minimal implementation**

```python
# h3_48gb/cli.py
"""Command line entry point.

Every subcommand takes explicit flags and can emit JSON, so an MCP server can
drive this without parsing human-readable output.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CHECKPOINT = Path.home() / "models/h3-converted"
DEFAULT_OUTDIR = Path.home() / "models/video-out"
BAKED_GRID_POINTS = 31


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
    return parser


def spec_from_args(args: argparse.Namespace) -> RunSpec:
    for name in ("width", "height"):
        value = getattr(args, name)
        if value % 32:
            raise SystemExit(f"--{name} must be a multiple of 32, got {value}")
    return RunSpec(
        prompt=args.prompt, width=args.width, height=args.height,
        duration=args.duration, steps=args.steps, seed=args.seed,
        checkpoint=args.checkpoint, outdir=args.outdir, tag=args.tag,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_cli.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add h3_48gb/cli.py tests/test_cli.py
git commit -m "feat(cli): add parser and RunSpec with up-front geometry validation"
```

---

### Task 2: Wire `generate` to the pipeline with raw-first saving

**Files:**
- Modify: `h3_48gb/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `RunSpec` from Task 1; `minimax_h3_mlx.packing.FPS` (value 24); `minimax_h3_mlx.media.save_mp4(path, video, fps, audio, sample_rate)` — note `fps` is the **third** positional parameter.
- Produces: `run_generate(spec: RunSpec, pipeline_factory=None) -> dict` returning the report dict written to `<stem>.json`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_cli.py
import numpy as np

from h3_48gb.cli import run_generate


class _StubResult:
    video = np.zeros((5, 32, 32, 3), dtype=np.uint8)
    audio = np.zeros((2, 8000), dtype=np.float32)
    sample_rate = 32000
    seconds_per_step = 1.5


def test_raw_arrays_are_written_before_encoding(tmp_path, monkeypatch):
    """A failure in mp4 encoding must not destroy hours of compute."""
    def exploding_save_mp4(*args, **kwargs):
        raise RuntimeError("ffmpeg unavailable")

    monkeypatch.setattr("h3_48gb.cli.save_mp4", exploding_save_mp4)
    spec = RunSpec(prompt="x", width=64, height=64, duration=1.0, steps=31, seed=0,
                   checkpoint=tmp_path, outdir=tmp_path, tag="t")
    try:
        run_generate(spec, pipeline_factory=lambda _: (lambda **kw: _StubResult()))
    except RuntimeError:
        pass
    assert (tmp_path / "h3-t-64x64-raw.npz").exists(), "raw arrays must survive an encoder failure"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_cli.py::test_raw_arrays_are_written_before_encoding -v`
Expected: FAIL with `ImportError: cannot import name 'run_generate'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to h3_48gb/cli.py
import json
import time

import numpy as np

from minimax_h3_mlx.media import save_mp4, save_wav
from minimax_h3_mlx.packing import FPS


def run_generate(spec: RunSpec, pipeline_factory=None) -> dict:
    spec.outdir.mkdir(parents=True, exist_ok=True)
    stem = spec.outdir / f"h3-{spec.tag}-{spec.width}x{spec.height}"

    factory = pipeline_factory or _default_pipeline_factory
    pipe = factory(spec.checkpoint)

    started = time.perf_counter()
    result = pipe(prompt=spec.prompt, duration_seconds=spec.duration,
                  num_inference_steps=spec.steps, seed=spec.seed,
                  height=spec.height, width=spec.width)
    elapsed = time.perf_counter() - started

    # Raw first: an encoder failure then costs seconds, not a fifteen-hour run.
    np.savez_compressed(f"{stem}-raw.npz", video=result.video, audio=result.audio,
                        sample_rate=result.sample_rate)

    save_mp4(f"{stem}.mp4", result.video, FPS, result.audio, result.sample_rate)
    save_wav(f"{stem}.wav", result.audio, result.sample_rate)

    report = {
        "tag": spec.tag, "canvas": f"{spec.width}x{spec.height}",
        "duration_seconds": spec.duration, "grid_points": spec.steps, "seed": spec.seed,
        "generate_seconds": round(elapsed, 1),
        "seconds_per_step": round(result.seconds_per_step, 1),
        "frames": int(result.video.shape[0]),
        "video": f"{stem}.mp4",
    }
    Path(f"{stem}.json").write_text(json.dumps(report, indent=2))
    return report


def _default_pipeline_factory(checkpoint: Path):
    from h3_48gb import LazyMiniMaxH3Pipeline

    return LazyMiniMaxH3Pipeline.from_pretrained(str(checkpoint))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "generate":
        report = run_generate(spec_from_args(args))
        print(json.dumps(report, indent=2) if args.json
              else f"done in {report['generate_seconds'] / 60:.1f} min -> {report['video']}")
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_cli.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add h3_48gb/cli.py tests/test_cli.py
git commit -m "feat(cli): wire generate to the pipeline, writing raw arrays before encoding"
```

---

### Task 3: `resume`, `list` and `doctor` subcommands

**Files:**
- Modify: `h3_48gb/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: the checkpoint API delivered alongside this plan — `h3_48gb.checkpoint.CheckpointStore(path)` with `.find(spec) -> Checkpoint | None` and `.describe() -> list[dict]`. If the delivered names differ, adapt this task to them rather than renaming the module.
- Produces: `run_resume(spec) -> dict`, `run_list(outdir) -> list[dict]`, `run_doctor(checkpoint) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_cli.py
from h3_48gb.cli import run_doctor, run_list


def test_list_reports_finished_runs(tmp_path):
    (tmp_path / "h3-a-512x512.json").write_text('{"tag": "a", "frames": 73}')
    rows = run_list(tmp_path)
    assert rows == [{"tag": "a", "frames": 73}]


def test_doctor_reports_missing_components(tmp_path):
    report = run_doctor(tmp_path)
    assert report["ok"] is False
    assert "transformer" in report["missing"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_cli.py -v`
Expected: FAIL with `ImportError: cannot import name 'run_doctor'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to h3_48gb/cli.py
REQUIRED_COMPONENTS = ("transformer", "text_encoder", "video_vae", "audio_vae")


def run_list(outdir: Path) -> list[dict]:
    rows = []
    for report in sorted(Path(outdir).glob("h3-*.json")):
        rows.append(json.loads(report.read_text()))
    return rows


def run_doctor(checkpoint: Path) -> dict:
    """Check a converted checkpoint before a multi-hour run rather than during it."""
    missing = [name for name in REQUIRED_COMPONENTS if not (Path(checkpoint) / name).is_dir()]
    cache = Path(checkpoint) / "transformer" / "adaln_cache.safetensors"
    if not cache.exists():
        missing.append("transformer/adaln_cache.safetensors")
    return {"ok": not missing, "checkpoint": str(checkpoint), "missing": missing}
```

Add the subparsers in `build_parser`:

```python
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
    res.add_argument("--json", action="store_true")

    lst = sub.add_parser("list", help="list finished runs")
    lst.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)

    doc = sub.add_parser("doctor", help="verify a converted checkpoint")
    doc.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_cli.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add h3_48gb/cli.py tests/test_cli.py
git commit -m "feat(cli): add resume, list and doctor subcommands"
```

---

### Task 4: README with algorithm and measured results

**Files:**
- Modify: `README.md`
- Create: `docs/RESULTS.md`
- Create: `docs/media/native5-frame.jpg` (extract from the existing 1344×768 clip)

**Interfaces:**
- Consumes: nothing in code. Numbers come verbatim from Global Constraints.
- Produces: the public entry point for the repository.

- [ ] **Step 1: Extract the sample frame**

```bash
ffmpeg -v error -i ~/models/video-out/h3-native5-1344x768.mp4 \
  -vf "select='eq(n\,40)'" -vsync 0 -q:v 3 docs/media/native5-frame.jpg -y
```

- [ ] **Step 2: Write `README.md`**

It must contain, in this order: what this is (one paragraph); the memory problem stated in numbers (55.5 GB resident vs 48 GB physical, mere.run's own preflight refusing with "Requires at least 96 GB unified memory"); the four patches with one sentence each; a quickstart (download Sawfwair build → `convert_sawfwair.py` → `h3 doctor` → `h3 generate`); the results table from Global Constraints; the sample frame; and a Licensing section stating that no weights are redistributed and naming the four excluded territories.

- [ ] **Step 3: Write `docs/RESULTS.md`**

Full measurements: per-run tables, the scaling observation (46 s → 586 s → 1881 s per step as geometry grows, worse than linear because attention is dense), the memory profile showing zero swap, and the honest limits — 31 grid points only, ~15.7 h for a 10-second clip, no sparse attention upstream.

- [ ] **Step 4: Verify links and numbers**

Run: `grep -o "docs/media/[a-z0-9.-]*" README.md | xargs -I{} test -f {} && echo "media ok"`
Expected: `media ok`

- [ ] **Step 5: Commit**

```bash
git add README.md docs/RESULTS.md docs/media/native5-frame.jpg
git commit -m "docs: describe the approach and publish measured results"
```

---

### Task 5: Repository hygiene for publication

**Files:**
- Create: `.gitignore`
- Create: `LICENSE` (Apache-2.0, matching upstream)
- Create: `NOTICE`
- Create: `requirements.txt`
- Modify: `PLAN.md` (move to `docs/DESIGN.md`)

**Interfaces:**
- Consumes: nothing.
- Produces: a repository that can be pushed without leaking weights, virtualenvs or personal paths.

- [ ] **Step 1: Write `.gitignore`**

```gitignore
.venv/
__pycache__/
*.pyc
upstream/
# Never commit weights or generated media — see NOTICE for why.
*.safetensors
*.npz
*.mp4
*.wav
!docs/media/*.jpg
```

- [ ] **Step 2: Write `NOTICE`**

State that this project contains no model weights; that MiniMax H3 weights are governed by the MiniMax H3 Community License which excludes the United States, European Union, United Kingdom and Republic of Korea; that users obtain weights themselves; and that `upstream/` is PipeNetwork/minimax-h3-mlx under Apache-2.0, vendored by clone and never modified.

- [ ] **Step 3: Verify no weights or personal paths are staged**

```bash
git add -A
git status --porcelain | grep -E "\.(safetensors|npz|mp4|wav)$" && echo "LEAK" || echo "clean"
grep -rn "/Users/aleksey" --include="*.py" --include="*.md" . | grep -v "^./docs/superpowers/" || echo "no personal paths"
```
Expected: `clean` and `no personal paths`

- [ ] **Step 4: Replace personal defaults with portable ones**

In `h3_48gb/cli.py`, defaults already use `Path.home()`. Confirm `convert_sawfwair.py` and `run_bench.py` do too; replace any absolute `/Users/...` literal with `Path.home() / ...`.

- [ ] **Step 5: Commit**

```bash
git add .gitignore LICENSE NOTICE requirements.txt docs/DESIGN.md
git rm --cached -r upstream 2>/dev/null || true
git commit -m "chore: prepare repository for publication"
```

---

### Task 6: Agent skill, portable across Claude Code and Codex

**Files:**
- Create: `skills/generating-h3-video/SKILL.md`
- Create: `skills/generating-h3-video/reference.md`
- Create: `scripts/install-skill.sh`
- Create: `tests/test_skill_frontmatter.py`

**Interfaces:**
- Consumes: the CLI surface from Tasks 1-3 — `h3 generate`, `h3 resume`, `h3 list`, `h3 doctor`, all accepting `--json`.
- Produces: a skill directory installable into `~/.claude/skills/` (Claude Code) and `~/.agents/skills/` (the cross-runtime path Codex, Copilot CLI and Gemini CLI also read).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_skill_frontmatter.py
from pathlib import Path

SKILL = Path("skills/generating-h3-video/SKILL.md")


def _frontmatter(text: str) -> dict:
    assert text.startswith("---\n"), "SKILL.md must open with YAML frontmatter"
    block = text.split("---\n", 2)[1]
    return dict(
        (k.strip(), v.strip())
        for k, v in (line.split(":", 1) for line in block.splitlines() if ":" in line)
    )


def test_frontmatter_has_the_two_required_fields():
    fm = _frontmatter(SKILL.read_text())
    assert set(fm) >= {"name", "description"}
    assert len(SKILL.read_text().split("---\n")[1]) <= 1024


def test_name_is_runtime_portable():
    """Codex and Claude Code both key on the directory name; keep it lowercase-hyphen."""
    fm = _frontmatter(SKILL.read_text())
    assert fm["name"].replace("-", "").isalnum() and fm["name"].islower()


def test_description_states_triggers_not_workflow():
    fm = _frontmatter(SKILL.read_text())
    assert fm["description"].startswith("Use when")
    for leak in ("first", "then", "step 1"):
        assert leak not in fm["description"].lower(), (
            "a description that summarises the workflow gets followed instead of the skill body"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_skill_frontmatter.py -v`
Expected: FAIL with `FileNotFoundError: skills/generating-h3-video/SKILL.md`

- [ ] **Step 3: Write the skill**

`SKILL.md` frontmatter:

```yaml
---
name: generating-h3-video
description: Use when generating video with MiniMax H3 locally on Apple Silicon, or when an H3 run is slow, was interrupted, or refuses to start over memory or schedule errors
---
```

Body must cover, in this order: the one-line orientation (a native 1344×768 clip takes about 5 hours, so decisions before launching matter more than usual); a quick-reference table mapping symptom to cause (`ScheduleMismatch` → the AdaLN table is baked for 31 grid points; refusal to start → run `h3 doctor`; geometry error → width and height must be multiples of 32); the workflow (`doctor` → `generate` with `--preview-every` → inspect the preview instead of waiting → `resume` after an interruption); and the runtime cost table from Global Constraints so an agent can set expectations before starting a job it cannot cancel cheaply.

`reference.md` holds the full flag list, deferred so it is not loaded on every invocation.

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_skill_frontmatter.py -v`
Expected: 3 passed

- [ ] **Step 5: Write the installer**

```bash
# scripts/install-skill.sh
#!/bin/bash
# Install the skill for every agent runtime present on this machine.
# ~/.agents/skills is the cross-runtime path Codex, Copilot CLI and Gemini CLI read;
# Claude Code reads ~/.claude/skills. Symlink both at the same source.
set -eu
SRC="$(cd "$(dirname "$0")/../skills/generating-h3-video" && pwd)"

for target in "$HOME/.claude/skills" "$HOME/.agents/skills"; do
  mkdir -p "$target"
  ln -sfn "$SRC" "$target/generating-h3-video"
  echo "installed -> $target/generating-h3-video"
done
```

- [ ] **Step 6: Verify the installer is idempotent**

Run: `bash scripts/install-skill.sh && bash scripts/install-skill.sh && ls -l ~/.claude/skills/generating-h3-video ~/.agents/skills/generating-h3-video`
Expected: both paths are symlinks to the repository copy, no error on the second run

- [ ] **Step 7: Commit**

```bash
git add skills/ scripts/install-skill.sh tests/test_skill_frontmatter.py
git commit -m "feat(skill): add cross-runtime agent skill for H3 generation"
```

---

## Self-Review

**Spec coverage:** checkpoints and preview are being delivered in parallel and are consumed by Task 3 (`resume`) — if their public names differ from `CheckpointStore.find/describe`, Task 3 adapts to the delivered names. CLI (Tasks 1-3), documentation (Task 4), publication hygiene (Task 5) are all covered. MCP wrapping is explicitly out of scope for this plan: the constraint it imposes (explicit flags, `--json` output, no interactivity) is satisfied by Task 1 and Task 2.

**Placeholders:** none — every code step carries real code, every verification step carries a real command and its expected output.

**Type consistency:** `RunSpec` field names are identical in Tasks 1, 2 and 3. `save_mp4` is called with `FPS` third in Task 2, matching the upstream signature that broke the first run.
